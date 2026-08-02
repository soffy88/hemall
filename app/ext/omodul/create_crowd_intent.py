"""app.ext.omodul.create_crowd_intent — C2B 逆向集单。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oprim import ext_notify_send

#: 达到这么多有效预付意向单即触发底层规模化采购通知。
_REPLENISH_THRESHOLD = 500


class CreateCrowdIntentConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "create_crowd_intent"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"variant_id", "customer_id"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail"}

    notification_provider: str = "log"


class CreateCrowdIntentInput(BaseModel):
    variant_id: str
    # 统一之前是自由字符串 customer_ref；统一之后 hemall 已经有真实顾客账户
    # 体系，改成真 customer.id FK，不再靠约定俗成的字符串对齐。
    customer_id: str
    prepaid_amount_cents: int


async def create_crowd_intent(
    config: CreateCrowdIntentConfig,
    input_data: CreateCrowdIntentInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """记录用户预付款意向；同一 variant 的 pending 意向单达到 500 单，
    触发向下游产地的规模化采购通知 (不在本函数内真的拉起采购单，那是
    demand_aggregator_engine (oservi) 的职责——这里只负责触发信号)。

    Args:
        config: CreateCrowdIntentConfig(notification_provider="log")。
        input_data: variant_id / customer_id / prepaid_amount_cents。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 intent_id / variant_id /
        pending_count / threshold_triggered (bool)。variant_id 顺带带出，
        是 oservi.demand_aggregator_engine 的信号 payload 需要的最小信息。
    """
    trail = Trail()
    fp = compute_fingerprint(
        {"variant_id": input_data.variant_id, "customer_id": input_data.customer_id}
    )

    try:
        if pool is None:
            raise ValueError(
                "pool is required — create_crowd_intent always touches persisted stock"
            )
        if input_data.prepaid_amount_cents <= 0:
            raise ValueError("prepaid_amount_cents must be positive")

        from obase.uuid7 import uuid7

        intent_id = uuid7()
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO "crowd_intent" '
                "(id, variant_id, customer_id, prepaid_amount_cents, status) "
                "VALUES ($1, $2, $3, $4, 'pending')",
                intent_id,
                input_data.variant_id,
                input_data.customer_id,
                input_data.prepaid_amount_cents,
            )
            pending_count = await conn.fetchval(
                "SELECT COUNT(*) FROM \"crowd_intent\" WHERE variant_id = $1 AND status = 'pending'",
                input_data.variant_id,
            )
        trail.record(
            event="intent_recorded", intent_id=intent_id, pending_count=pending_count
        )

        threshold_triggered = pending_count >= _REPLENISH_THRESHOLD
        if threshold_triggered:
            await ext_notify_send(
                config.notification_provider,
                channel="email",
                template="crowd_intent_threshold_reached",
                data={
                    "to": "supply-chain@hemall.internal",
                    "variant_id": input_data.variant_id,
                    "count": pending_count,
                },
            )
            trail.record(
                event="replenish_notification_sent",
                variant_id=input_data.variant_id,
                count=pending_count,
            )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            intent_id=intent_id,
            variant_id=input_data.variant_id,
            pending_count=pending_count,
            threshold_triggered=threshold_triggered,
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
