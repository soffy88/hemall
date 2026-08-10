"""app.ext.hardware_webhook — Phase 10: IoT 边缘网桥防腐层 (ACL) 入口。

架构对齐: Event-Driven Sidecar / Anti-Corruption Layer。

hemall 主干不需要、也不应该知道什么是 MQTT、什么是 QoS 1，更不该去处理
硬件的断线重连——那是独立外挂域 clearnode-iot-bridge (edge_bridge/) 的职责。
主干只在这里暴露两个"第三方系统发来状态变更指令"的标准 REST 端点，由专属
HARDWARE_SECRET (HEMALL_HARDWARE_SECRET) 保护：

    POST /ext/hardware/webhook/pick            — 重力货架 pick (MQTT pick)
    POST /ext/hardware/webhook/gate-reconcile  — 闸口物理对账 (MQTT gate req)

防护与一致性设计：
    1. HARDWARE_SECRET：网桥请求带 ``Authorization: Bearer <secret>``，
       这里用 hmac.compare_digest 常量时间比较，防时序侧信道。
    2. message_id 幂等：hardware_event.message_id UNIQUE 物理约束，网桥重试/
       重发不会把同一物理事件处理两次；竞态由 UniqueViolationError 兜底。
    3. 孤儿队列：pick 事件找不到 shelf → batch 映射时如实落 'orphan' 状态，
       不假装知道商业归属，后续盘点/人工挂账从这里捞。
    4. 闸口状态机：expected_weight = tare + 上次放行后该 tote 全部 pick 的
       qty*unit 累计，reconcile 三档裁决 (pass/recheck/block)，放行时做
       封顶皮重漂移补偿 (compute_tare_adjustment)。
"""

from __future__ import annotations

import hmac
import json
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .oskill import (
    compute_cart_delta,
    compute_tare_adjustment,
    decide_gate_reconcile,
)

#: 闸口对账公差默认值 (克)——payload 不携带时用闸口行上的配置值，首建用此。
DEFAULT_GATE_TOLERANCE_GRAMS = 20
#: 放行后单次允许的最大皮重漂移补偿 (克)。
DEFAULT_MAX_TARE_DRIFT_GRAMS = 15
#: pick 事件只有非零数量增量才计入商业账 (称重噪声可能算出 0 件)。
router = APIRouter(prefix="/ext/hardware/webhook", tags=["iot-acl"])


class PickEvent(BaseModel):
    """物理状态坍缩上报 (MQTT pick 翻译后的标准 payload)。"""

    message_id: str
    node_id: str
    shelf_id: str
    delta_weight: int  # 克，拿走为负、放回为正
    tote_id: str
    timestamp: int | None = None  # 毫秒时间戳 (网桥透传，仅留痕)


class GateReconcileEvent(BaseModel):
    """闸口物理对账 (MQTT gate req 翻译后的标准 payload)。

    node_id 可选——SPEC §3 的 payload 没有它 (网桥 topic 里有但未透传)；
    首建闸口行时存 NULL，后续对账只按 gate_id 定位。
    """

    gate_id: str
    tote_id: str
    raw_weight_grams: int
    node_id: str | None = None


def require_hardware_secret(request: Request) -> None:
    """HARDWARE_SECRET 鉴权：Bearer token 常量时间比较 (防腐层专属密钥)。

    跟顾客 JWT / admin JWT 完全正交——这是机器对机器的信任凭据，由
    HEMALL_HARDWARE_SECRET 配置，网桥侧与这里必须一致。
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing hardware bearer token")
    token = auth.removeprefix("Bearer ")
    secret = request.app.state.config.hardware_secret
    # 常量时间比较，防时序侧信道泄露密钥前缀。
    if not hmac.compare_digest(token.encode(), secret.encode()):
        raise HTTPException(403, "invalid hardware secret")


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database not ready")
    return pool


@router.post("/pick")
async def hardware_pick(
    body: PickEvent,
    request: Request,
    _: None = Depends(require_hardware_secret),
) -> dict:
    """重力货架 pick：把物理重量变化翻译成购物车数量增量并落账。

    处理链：message_id 幂等校验 → shelf 映射解析 (hardware_shelf) →
    compute_cart_delta 把 delta_weight 折算成 qty_delta → 写 hardware_event
    (processed=已挂上商业批次 / orphan=货架位未映射进孤儿队列)。

    响应带 batch_id/qty_delta 供网桥回显；网桥重发同一 message_id 得到
    status='duplicate'，不重复计账。
    """
    pool = _pool(request)
    try:
        async with pool.acquire() as conn:
            # 1. 幂等快查 (UNIQUE 约束兜底竞态)
            existing = await conn.fetchval(
                'SELECT status FROM "hardware_event" WHERE message_id = $1',
                body.message_id,
            )
            if existing is not None:
                return {
                    "status": "duplicate",
                    "message_id": body.message_id,
                    "first_status": existing,
                }

            # 2. shelf → 商业批次映射
            shelf = await conn.fetchrow(
                'SELECT batch_id, unit_weight_grams FROM "hardware_shelf" '
                "WHERE node_id = $1 AND shelf_id = $2 AND status = 'active'",
                body.node_id,
                body.shelf_id,
            )
            batch_id = shelf["batch_id"] if shelf else None
            unit_weight = int(shelf["unit_weight_grams"]) if shelf else 0

            # 3. 重量增量 → 数量增量
            qty_delta = (
                compute_cart_delta(body.delta_weight, unit_weight) if unit_weight else 0
            )
            status = "processed" if batch_id is not None else "orphan"

            # 4. 落账
            await conn.execute(
                'INSERT INTO "hardware_event" '
                "(message_id, event_type, node_id, shelf_id, tote_id, batch_id, "
                "qty_delta, unit_weight_grams, payload, status) "
                "VALUES ($1, 'pick', $2, $3, $4, $5, $6, $7, $8, $9)",
                body.message_id,
                body.node_id,
                body.shelf_id,
                body.tote_id,
                batch_id,
                qty_delta,
                unit_weight,
                json.dumps(body.model_dump(), ensure_ascii=False),
                status,
            )
        return {
            "status": status,
            "message_id": body.message_id,
            "node_id": body.node_id,
            "shelf_id": body.shelf_id,
            "tote_id": body.tote_id,
            "batch_id": str(batch_id) if batch_id else None,
            "qty_delta": qty_delta,
            "unit_weight_grams": unit_weight,
        }
    except asyncpg.UniqueViolationError:
        # 竞态兜底：两个重发同时到达，后到者撞 message_id 唯一约束。
        return {"status": "duplicate", "message_id": body.message_id}


@router.post("/gate-reconcile")
async def gate_reconcile(
    body: GateReconcileEvent,
    request: Request,
    _: None = Depends(require_hardware_secret),
) -> dict:
    """闸口物理对账：实测重量 vs 期望重量 (tare + pick 累计)，三档裁决。

    状态机 (hardware_gate)：
        idle    — 闸口首见该 tote，raw 作为校准基准直接放行 (绿)。
        tracking— pick 事件按 tote 累计 qty*unit。
        pass    — |raw - expected| <= tolerance：放行 + 封顶皮重漂移补偿 +
                  last_pass_at 推进 (累计窗口重置)。
        recheck — 超差一倍内：黄灯复核，不放行。
        block   — 超差一倍外：红灯拦截，闸口行置 blocked。

    同步返回 {"action", "act", "led", "tare_adj", ...}——"action" 是主干 API
    契约 (SPEC §3)，"act" 是网桥侧骨架读取的键 (SPEC §4)，双写兼容。
    """
    pool = _pool(request)
    async with pool.acquire() as conn:
        gate = await conn.fetchrow(
            'SELECT * FROM "hardware_gate" WHERE gate_id = $1', body.gate_id
        )

        if gate is None:
            # 首见：以空筐实测为皮重基线，直接放行 (校准态)。
            await conn.execute(
                'INSERT INTO "hardware_gate" '
                "(gate_id, node_id, tote_id, tare_weight_grams, expected_weight_grams, "
                "tolerance_grams, last_raw_weight_grams, status, updated_at) "
                "VALUES ($1, $2, $3, $4, $4, $5, $4, 'idle', NOW())",
                body.gate_id,
                getattr(body, "node_id", None),
                body.tote_id,
                body.raw_weight_grams,
                DEFAULT_GATE_TOLERANCE_GRAMS,
            )
            return {
                "action": "pass",
                "act": "pass",
                "led": "green",
                "tare_adj": 0,
                "status": "idle",
                "raw_weight_grams": body.raw_weight_grams,
                "expected_weight_grams": body.raw_weight_grams,
                "gate_id": body.gate_id,
            }

        # 期望重量 = 皮重 + 该 tote 上次放行后所有 pick 的净增加重量。
        # qty_delta 为负 = 顾客从货架拿走 (商品进 tote，tote 变重) →
        # -qty_delta*unit 是正的净增量；qty_delta 为正 = 放回货架。
        picked_weight = await conn.fetchval(
            "SELECT COALESCE(SUM(-qty_delta * unit_weight_grams), 0)::int "
            'FROM "hardware_event" '
            "WHERE tote_id = $1 AND event_type = 'pick' AND status = 'processed' "
            "AND created_at > COALESCE($2, '-infinity'::timestamptz)",
            body.tote_id,
            gate["last_pass_at"],
        )
        tare = int(gate["tare_weight_grams"])
        expected = tare + int(picked_weight or 0)

        decision = decide_gate_reconcile(
            body.raw_weight_grams, expected, int(gate["tolerance_grams"])
        )
        led = {"pass": "green", "recheck": "yellow", "block": "red"}[decision]

        if decision == "pass":
            tare_adj = compute_tare_adjustment(
                body.raw_weight_grams, expected, DEFAULT_MAX_TARE_DRIFT_GRAMS
            )
            await conn.execute(
                'UPDATE "hardware_gate" SET '
                "tare_weight_grams = $1, expected_weight_grams = $1, "
                "last_raw_weight_grams = $2, last_pass_at = NOW(), "
                "status = 'idle', updated_at = NOW() WHERE gate_id = $3",
                tare + tare_adj,
                body.raw_weight_grams,
                body.gate_id,
            )
        else:
            # 持久化状态用语义化词 (block → blocked)，裁决结果键仍是 decision。
            await conn.execute(
                'UPDATE "hardware_gate" SET '
                "last_raw_weight_grams = $1, status = $2, updated_at = NOW() "
                "WHERE gate_id = $3",
                body.raw_weight_grams,
                "blocked" if decision == "block" else decision,
                body.gate_id,
            )

        return {
            "action": decision,
            "act": decision,
            "led": led,
            "tare_adj": tare_adj if decision == "pass" else 0,
            "status": decision,
            "raw_weight_grams": body.raw_weight_grams,
            "expected_weight_grams": expected,
            "tolerance_grams": int(gate["tolerance_grams"]),
            "gate_id": body.gate_id,
            "tote_id": body.tote_id,
        }
