"""app.ext.omodul.record_douyin_conversion_workflow — 抖音转化落账。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oskill import calculate_lord_tax, compute_mercenary_bounty_rate


class RecordDouyinConversionWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "record_douyin_conversion_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"order_id"}
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail", "cost"}


class RecordDouyinConversionWorkflowInput(BaseModel):
    order_id: str
    # 触发这笔订单的抖音引荐 UID；没有 (顾客不是从抖音链路进来的) 就传 None——
    # 领主税分支不需要它也照样生效 (见下方 docstring)。
    douyin_uid: str | None = None


async def record_douyin_conversion_workflow(
    config: RecordDouyinConversionWorkflowConfig,
    input_data: RecordDouyinConversionWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """把一笔订单产生的抖音分润 (领主税 + 雇佣兵悬赏) 强制落库到
    douyin_conversion_log，供 affiliate_settlement_engine 夜间清算。

    这个 omodul 是 SPEC v6.0 明确留下的空白："每一笔分润的强制落库" 出现在
    §4 omodul 小节的标题里，但 SPEC 给出的两个函数 (process_cloud_franchise_
    claim_workflow / bind_digital_lord_contract_workflow) 都只管契约的建立，
    没有一处真正往 douyin_conversion_log 写一行——mercenary_routing_engine
    只是把带佣金的 SKU 推给抖音橱窗，真正的成交转化 (谁买了、赚了多少佣金)
    发生在抖音那一侧，SPEC 完全没写"抖音那边买家下单后怎么把归因信息传回
    这个系统"的回调链路。这里按项目一贯的做法把这段缺失的业务事务补齐：
    调用方 (真实场景是抖音回调 / 前端结账时透传的 URL 归因参数) 在订单确认
    后调这个 omodul，由它统一完成"匹配契约 → 算分润 → 落账"。

    两条完全独立的分润判定逻辑，都在同一次调用里各自判断是否适用：

    1. 领主税 (与 douyin_uid 输入无关，只看订单落在哪个节点)：按订单行按
       location_id 分组 (一个节点一组)，每组小计金额分别过一遍
       oskill.calculate_lord_tax——SPEC 原文直接拿整单金额去算领主税，隐含
       假设"一单只会落在一个节点"，这个项目的 schema 允许一单跨节点，逐节点
       分组计算更准确，代价是同样的循环量，不是额外复杂度。命中一个生效中
       的 digital_lord 契约就落一行 conversion log，税打给契约上记录的
       douyin_uid (可能跟这次下单请求带的 douyin_uid 完全不是一个人——领主
       躺赚的就是"不管谁来买都抽成")。

    2. 雇佣兵悬赏 (只有传了 douyin_uid 才判定；没传就跳过，压根没有可归因
       的达人)：逐订单行看该批次距离销毁还有多久 (以订单创建时刻为基准)，
       算 oskill.compute_mercenary_bounty_rate，>0 就按这行小计乘以佣金比例
       算悬赏金额，同时把这次归因写成一条 affiliate_contract (contract_
       type='mercenary')——SPEC 的 schema 要求 douyin_uid NOT NULL，雇佣兵
       契约在真正促成一次转化前压根不知道是哪个达人，没法提前建；只能等
       第一次真实转化发生时才第一次落这行契约 (统一后 id 是 UUID 装不下手拼
       的确定性字符串，改成 (douyin_uid, bound_entity_id, contract_type) 唯一
       索引做 ON CONFLICT 幂等，
       同一个达人对同一个批次多次成交只更新一次)。

    两条分支都不适用时 (最常见的情况——绝大多数订单跟抖音无关) 依旧判
    completed，conversions 为空列表，不是 failed。

    Args:
        config: RecordDouyinConversionWorkflowConfig。
        input_data: order_id / douyin_uid (可选)。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 conversions (每条落账记录的
        明细列表) / total_dividend_cents。
    """
    trail = Trail()
    fp = compute_fingerprint({"order_id": input_data.order_id})

    try:
        if pool is None:
            raise ValueError(
                "pool is required — record_douyin_conversion_workflow always touches persisted stock"
            )

        from obase.uuid7 import uuid7

        async with pool.acquire() as conn:
            order = await conn.fetchrow(
                'SELECT id, created_at FROM "customer_order" WHERE id = $1',
                input_data.order_id,
            )
            if order is None:
                raise ValueError(f"order {input_data.order_id!r} not found")

            line_items = await conn.fetch(
                "SELECT oli.batch_id, oli.line_total_cents, b.location_id, "
                "b.expiration_time "
                'FROM "order_line_item" oli '
                'JOIN "inventory_batch" b ON b.id = oli.batch_id '
                "WHERE oli.order_id = $1",
                input_data.order_id,
            )
        trail.record(event="order_loaded", line_item_count=len(line_items))

        conversions: list[dict[str, Any]] = []
        total_dividend = 0

        # 1. 领主税：按 location_id 分组小计，跟 douyin_uid 输入无关。
        by_location: dict[Any, int] = {}
        for li in line_items:
            by_location[li["location_id"]] = (
                by_location.get(li["location_id"], 0) + li["line_total_cents"]
            )

        for location_id, subtotal in by_location.items():
            async with pool.acquire() as conn:
                lord = await conn.fetchrow(
                    'SELECT id, douyin_uid FROM "affiliate_contract" '
                    "WHERE contract_type = 'digital_lord' AND bound_entity_id = $1 "
                    "AND status = 'active'",
                    location_id,
                )
            if lord is None:
                continue

            dividend = calculate_lord_tax(subtotal, is_active_node=True)
            if dividend <= 0:
                continue

            log_id = uuid7()
            async with pool.acquire() as conn:
                await conn.execute(
                    'INSERT INTO "douyin_conversion_log" '
                    "(id, order_id, douyin_uid, contract_id, dividend_amount_cents) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    log_id,
                    input_data.order_id,
                    lord["douyin_uid"],
                    lord["id"],
                    dividend,
                )
            conversions.append(
                {
                    "log_id": log_id,
                    "type": "digital_lord",
                    "douyin_uid": lord["douyin_uid"],
                    "location_id": str(location_id),
                    "dividend_amount": dividend,
                }
            )
            total_dividend += dividend
            trail.record(
                event="lord_tax_recorded",
                location_id=str(location_id),
                douyin_uid=lord["douyin_uid"],
                dividend_amount=dividend,
            )

        # 2. 雇佣兵悬赏：只有带了 douyin_uid 才判定。
        if input_data.douyin_uid:
            for li in line_items:
                if li["expiration_time"] is None:
                    continue
                hours_to_expiry = (
                    li["expiration_time"] - order["created_at"]
                ).total_seconds() / 3600.0
                bounty_rate = compute_mercenary_bounty_rate(hours_to_expiry)
                if bounty_rate <= 0:
                    continue

                dividend = round(li["line_total_cents"] * bounty_rate)
                commission_logic = {"bounty_rate": bounty_rate}
                async with pool.acquire() as conn:
                    contract_id = await conn.fetchval(
                        'INSERT INTO "affiliate_contract" '
                        "(id, douyin_uid, contract_type, bound_entity_id, commission_logic, status) "
                        "VALUES ($1, $2, 'mercenary', $3, $4, 'active') "
                        "ON CONFLICT (douyin_uid, bound_entity_id, contract_type) "
                        "DO UPDATE SET commission_logic = EXCLUDED.commission_logic "
                        "RETURNING id",
                        uuid7(),
                        input_data.douyin_uid,
                        li["batch_id"],
                        json.dumps(commission_logic),
                    )
                    log_id = uuid7()
                    await conn.execute(
                        'INSERT INTO "douyin_conversion_log" '
                        "(id, order_id, douyin_uid, contract_id, dividend_amount_cents) "
                        "VALUES ($1, $2, $3, $4, $5)",
                        log_id,
                        input_data.order_id,
                        input_data.douyin_uid,
                        contract_id,
                        dividend,
                    )
                conversions.append(
                    {
                        "log_id": log_id,
                        "type": "mercenary",
                        "douyin_uid": input_data.douyin_uid,
                        "batch_id": str(li["batch_id"]),
                        "bounty_rate": bounty_rate,
                        "dividend_amount": dividend,
                    }
                )
                total_dividend += dividend
                trail.record(
                    event="mercenary_bounty_recorded",
                    batch_id=str(li["batch_id"]),
                    bounty_rate=bounty_rate,
                    dividend_amount=dividend,
                )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            cost_usd=0.0,
            conversions=conversions,
            total_dividend_cents=total_dividend,
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
