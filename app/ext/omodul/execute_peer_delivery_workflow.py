"""app.ext.omodul.execute_peer_delivery_workflow — 邻居代送确认。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oprim import ext_pay_transfer


class ExecutePeerDeliveryWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "execute_peer_delivery_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"order_id", "tote_id", "neighbor_id"}
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail", "cost"}

    payment_provider: str = "manual"
    payout_provider: str = "manual"
    currency: str = "CNY"


class ExecutePeerDeliveryWorkflowInput(BaseModel):
    order_id: str
    tote_id: str
    # 代送邻居——跟 worker_id/host_id 同样的简化：没有"邻居档案"表，
    # neighbor_id 本身兼做打款账户 (外部支付渠道 ID)。
    neighbor_id: str
    bounty_amount: int


async def execute_peer_delivery_workflow(
    config: ExecutePeerDeliveryWorkflowConfig,
    input_data: ExecutePeerDeliveryWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """确认邻居扫码接单：释放 Tote (转 in_transit，由邻居顺路带走)，向买家收取
    悬赏金，打入代送邻居余额。

    Args:
        config: ExecutePeerDeliveryWorkflowConfig(payment_provider="manual",
            payout_provider="manual", currency="CNY")。
        input_data: order_id (买家订单，用于扣悬赏金的支付 meta) / tote_id /
            neighbor_id (代送邻居) / bounty_amount (悬赏金，分)。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 tote_id / bounty_amount /
        transfer_id。
    """
    trail = Trail()
    fp = compute_fingerprint(
        {
            "order_id": input_data.order_id,
            "tote_id": input_data.tote_id,
            "neighbor_id": input_data.neighbor_id,
        }
    )

    try:
        if pool is None:
            raise ValueError(
                "pool is required — execute_peer_delivery_workflow always touches persisted stock"
            )
        if input_data.bounty_amount <= 0:
            raise ValueError("bounty_amount must be positive")

        async with pool.acquire() as conn:
            tote = await conn.fetchrow(
                "UPDATE \"tote\" SET status = 'in_transit' WHERE id = $1 RETURNING id",
                input_data.tote_id,
            )
            if tote is None:
                raise ValueError(f"tote {input_data.tote_id!r} not found")
        trail.record(event="tote_released_to_peer", tote_id=input_data.tote_id)

        from obase.provider_registry import ProviderRegistry

        payment_provider = ProviderRegistry.get().generic(
            "payment", config.payment_provider
        )
        auth_result = await payment_provider.authorize(
            amount=input_data.bounty_amount,
            currency=config.currency,
            meta={"order_id": input_data.order_id, "purpose": "peer_delivery_bounty"},
        )
        await payment_provider.capture(intent_id=auth_result["intent_id"])
        trail.record(
            event="bounty_charged_to_buyer",
            intent_id=auth_result["intent_id"],
            amount=input_data.bounty_amount,
        )

        transfer_result = await ext_pay_transfer(
            config.payout_provider,
            account=input_data.neighbor_id,
            amount=input_data.bounty_amount,
        )
        trail.record(
            event="bounty_paid_to_neighbor",
            transfer_id=transfer_result["transfer_id"],
            amount=input_data.bounty_amount,
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            cost_usd=0.0,
            tote_id=input_data.tote_id,
            bounty_amount=input_data.bounty_amount,
            transfer_id=transfer_result["transfer_id"],
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
