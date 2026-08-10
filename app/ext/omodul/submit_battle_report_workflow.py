"""app.ext.omodul.submit_battle_report_workflow — 全自动战报式评价提交事务。

Phase 9 (补天)：取代传统 5 星评价。用户提交订单履约后的真实凭证 (吐槽文字 +
可选实拍图)，系统用 VLM/LLM 提纯成客观物理指标 (freshness_index /
sentiment_polarity / keywords)，写进 batch_battle_report 账本，发放算力金
奖励 (customer.system_balance)，并联动供应商信誉奖惩——差评战报 (极性 < -0.5)
追溯扣减该批次供应商 trust_score。

防刷设计 (SPEC 核心诉求"彻底杜绝云评价和刷单")：
  1. 物理锁校验：订单必须真实存在、属于提交用户、且状态已物理履约
     (delivered/completed)，未履约/不属于自己的订单一律拒绝。
  2. 批次锚定：战报死死锚定在 batch_id 上；batch_id 必须真的出现在该订单的
     行项里 (order_line_item)，买都没买过的批次不允许评价。
  3. 一单一报：batch_battle_report.order_id UNIQUE 唯一约束物理防重——重复
     提交由数据库兜底拒绝，且插入与发奖在同一事务里，重复提交不会重复发钱。
  4. 发奖不看极性：差评战报同样有价值 (帮系统排雷)，奖励只按是否带图区分
     (20 分基础 + 30 分带图，封顶 50 分)，不以"好评"为奖励条件。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import asyncpg
from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oprim import ext_vlm_parse_battle_report
from ..oskill import calculate_battle_report_reward

#: 可产生战报的订单状态。SPEC 原文写的是 ["delivered", "settled"]——本项目的
#: 订单状态机 (app/orders/models.py) 没有 "settled"，物理履约后的终态是
#: "delivered" (已送达) 和 "completed" (顾客确认收货/自动完成)，映射关系：
#: delivered → delivered，settled → completed。pending/confirmed/paid 等
#: 未履约状态一律不允许评价 (没拿到货没资格评价)。
_REPORTABLE_ORDER_STATUSES = frozenset({"delivered", "completed"})

#: 触发供应商信誉扣减的极性阈值 (SPEC 写死 -0.5)。
_SUPPLIER_PENALTY_POLARITY_THRESHOLD = -0.5
#: 单次差评战报扣减的信誉分 (SPEC 写死 2 分)。
_SUPPLIER_PENALTY_TRUST_POINTS = 2


class SubmitBattleReportWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "submit_battle_report_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"order_id", "user_id"}
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail", "cost"}

    llm_provider: str = "manual"


class SubmitBattleReportWorkflowInput(BaseModel):
    user_id: str
    order_id: str
    # 战报正文 (吐槽/评价文字)。text 与 image_url 至少填一个。
    text: str = ""
    # 实拍图 URL (可选)。带图战报奖励更高，且图会拼进多模态上下文做交叉验证。
    image_url: str | None = None
    # 批次锚定 (可选)。订单是多行项结构 (一个订单可含多个批次)，前端在哪个
    # 批次卡片上发起评价就传哪个 batch_id；不传时订单恰好只有单一批次则自动
    # 取该批次，多批次订单必须显式传 (避免歧义)。
    batch_id: str | None = None


def _fail(
    trail: Trail,
    output_dir: Path | None,
    fingerprint: str,
    *,
    reason: str,
    detail: str = "",
) -> dict:
    """业务校验失败的标准返回：status=failed + 机器可读 reason + 人类可读 error。"""
    trail.record(event="failed", reason=reason, detail=detail)
    trail_path = trail.write(output_dir) if output_dir else None
    return build_result(
        status="failed",
        error={"type": "BusinessRuleViolation", "message": detail or reason},
        fingerprint=fingerprint,
        trail=trail,
        trail_path=trail_path,
        reason=reason,
    )


async def submit_battle_report_workflow(
    config: SubmitBattleReportWorkflowConfig,
    input_data: SubmitBattleReportWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """战报提交流水线：订单溯源校验 → LLM/VLM 量化 → 写账本 → 发算力金 →
    供应商信誉联动，一气呵成。

    Args:
        config: SubmitBattleReportWorkflowConfig(llm_provider="manual")。
        input_data: user_id / order_id / text / image_url / batch_id (可选)。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 report_id / reward_granted /
        freshness_index / sentiment_polarity / keywords / supplier_penalized；
        failed 时含 reason (invalid_or_incomplete_order / empty_report /
        batch_not_in_order / ambiguous_batch / vlm_parsing_error /
        already_reported / ...)。
    """
    trail = Trail()
    fp = compute_fingerprint(
        {"order_id": input_data.order_id, "user_id": input_data.user_id}
    )

    try:
        if pool is None:
            raise ValueError(
                "pool is required — submit_battle_report_workflow always touches persisted stock"
            )

        if not input_data.text and not input_data.image_url:
            return _fail(
                trail, output_dir, fp,
                reason="empty_report",
                detail="at least one of text/image_url is required",
            )

        # ── 1. 物理锁校验：订单属于该用户 + 已物理履约 + 未评价过 ──
        async with pool.acquire() as conn:
            order = await conn.fetchrow(
                'SELECT id, customer_id, status FROM "customer_order" '
                "WHERE id = $1",
                input_data.order_id,
            )
            if order is None or str(order["customer_id"]) != input_data.user_id:
                return _fail(
                    trail, output_dir, fp,
                    reason="invalid_or_incomplete_order",
                    detail="order does not exist or does not belong to this user",
                )
            if str(order["status"]) not in _REPORTABLE_ORDER_STATUSES:
                return _fail(
                    trail, output_dir, fp,
                    reason="invalid_or_incomplete_order",
                    detail=f"order status={order['status']} is not physically settled "
                    f"(need one of {sorted(_REPORTABLE_ORDER_STATUSES)})",
                )

            already = await conn.fetchval(
                'SELECT 1 FROM "batch_battle_report" WHERE order_id = $1', 
                input_data.order_id,
            )
            if already:
                return _fail(
                    trail, output_dir, fp,
                    reason="already_reported",
                    detail="one order can only produce one battle report (anti-spam)",
                )

            # 批次锚定：order_line_item 是该订单真实买过的批次集合。
            line_items = await conn.fetch(
                'SELECT batch_id FROM "order_line_item" WHERE order_id = $1',
                input_data.order_id,
            )
            batch_ids = {str(r["batch_id"]) for r in line_items}

        if not batch_ids:
            return _fail(
                trail, output_dir, fp,
                reason="invalid_or_incomplete_order",
                detail="order has no line items",
            )

        if input_data.batch_id:
            if input_data.batch_id not in batch_ids:
                return _fail(
                    trail, output_dir, fp,
                    reason="batch_not_in_order",
                    detail="the reviewed batch was not actually purchased in this order",
                )
            batch_id = input_data.batch_id
        elif len(batch_ids) == 1:
            batch_id = next(iter(batch_ids))
        else:
            return _fail(
                trail, output_dir, fp,
                reason="ambiguous_batch",
                detail="order contains multiple batches; batch_id is required",
            )
        trail.record(event="order_verified", order_id=input_data.order_id, batch_id=batch_id)

        # ── 2. VLM/LLM 提纯与量化 (oprim) ──
        try:
            quantified = await ext_vlm_parse_battle_report(
                config.llm_provider,
                text=input_data.text,
                image_url=input_data.image_url,
            )
        except Exception as exc:
            return _fail(
                trail, output_dir, fp,
                reason="vlm_parsing_error",
                detail=f"battle report parsing failed: {exc}",
            )
        trail.record(
            event="report_quantified",
            freshness_index=quantified["freshness_index"],
            sentiment_polarity=quantified["sentiment_polarity"],
            keywords=quantified["keywords"],
        )

        # ── 3. 算力金奖励 (oskill)：只看是否带图，不看极性 ──
        reward_cents = calculate_battle_report_reward(has_image=bool(input_data.image_url))
        trail.record(event="reward_computed", reward_cents=reward_cents)

        # ── 4. DB 事务：写战报账本 + 发算力金 + 供应商信誉联动 ──
        from obase.uuid7 import uuid7

        report_id = uuid7()
        supplier_penalized = False
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # A. 插入战报账本 (order_id UNIQUE 约束防刷单重发)
                    await conn.execute(
                        'INSERT INTO "batch_battle_report" '
                        "(id, batch_id, order_id, user_id, raw_text, raw_image_url, "
                        "freshness_index, sentiment_polarity, keywords, reward_granted, status) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'published')",
                        report_id,
                        batch_id,
                        input_data.order_id,
                        input_data.user_id,
                        input_data.text or None,
                        input_data.image_url,
                        quantified["freshness_index"],
                        quantified["sentiment_polarity"],
                        json.dumps(quantified["keywords"], ensure_ascii=False),
                        reward_cents,
                    )

                    # B. 发放 UGC 物理激励 (算力金 1:1 进 customer.system_balance)
                    credited = await conn.fetchval(
                        'UPDATE "customer" SET system_balance = system_balance + $1 '
                        "WHERE id = $2 RETURNING id",
                        reward_cents,
                        input_data.user_id,
                    )
                    if credited is None:
                        raise ValueError(
                            f"customer {input_data.user_id!r} not found"
                        )

                    # C. 联动供应商惩罚：极性严重为负 → 追溯该批次供应商扣信誉分
                    if quantified["sentiment_polarity"] < _SUPPLIER_PENALTY_POLARITY_THRESHOLD:
                        await conn.execute(
                            'UPDATE "supplier" '
                            "SET trust_score = GREATEST(trust_score - $1, 0) "
                            "WHERE id = (SELECT supplier_id FROM \"inventory_batch\" "
                            "WHERE id = $2)",
                            _SUPPLIER_PENALTY_TRUST_POINTS,
                            batch_id,
                        )
                        supplier_penalized = True
        except asyncpg.UniqueViolationError:
            # 竞态兜底：两个请求同时提交同一订单，后到者撞 order_id 唯一约束，
            # 事务整体回滚 (算力金不会重复发放)。
            return _fail(
                trail, output_dir, fp,
                reason="already_reported",
                detail=f"duplicate battle report for order {input_data.order_id!r}",
            )
        trail.record(
            event="report_persisted",
            report_id=report_id,
            reward_credited=reward_cents,
            supplier_penalized=supplier_penalized,
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            cost_usd=0.0,
            report_id=report_id,
            batch_id=batch_id,
            reward_granted=reward_cents,
            freshness_index=quantified["freshness_index"],
            sentiment_polarity=quantified["sentiment_polarity"],
            keywords=quantified["keywords"],
            supplier_penalized=supplier_penalized,
        )

    except Exception as exc:
        trail.record(event="error", detail=str(exc))
        trail_path = trail.write(output_dir) if output_dir else None
        return build_result(
            status="failed",
            error={"type": type(exc).__name__, "message": str(exc)},
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
        )
