"""app.ext.omodul.process_drop_return — 回收桶扫码退货，秒退。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint


class ProcessDropReturnConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "process_drop_return"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"order_line_item_id"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail", "cost"}

    payment_provider: str = "manual"


class ProcessDropReturnInput(BaseModel):
    order_line_item_id: str
    reason: str = "quality"


async def process_drop_return(
    config: ProcessDropReturnConfig,
    input_data: ProcessDropReturnInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """用户扫回收桶投掷坏果，系统直接对该行原路秒退，零人工废话。

    退坏果不回补库存 (货已经坏了，不是"退货可再售"的场景)——这里只处理
    退款这一半；如果以后要联动 mark_batch_for_disposal 之类的清点流程，
    是另一个 omodul 的职责，不在这里裸调 (omodul 之间禁止裸调)。

    Args:
        config: ProcessDropReturnConfig(payment_provider="manual")。
        input_data: order_line_item_id / reason。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 refund_amount。
    """
    trail = Trail()
    fp = compute_fingerprint({"order_line_item_id": input_data.order_line_item_id})

    try:
        if pool is None:
            raise ValueError(
                "pool is required — process_drop_return always touches persisted stock"
            )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT oli.line_total_cents, o.payment_intent_id, o.status AS order_status "
                'FROM "order_line_item" oli '
                'JOIN "customer_order" o ON o.id = oli.order_id '
                "WHERE oli.id = $1",
                input_data.order_line_item_id,
            )
        if row is None:
            raise ValueError(
                f"order_line_item {input_data.order_line_item_id!r} not found"
            )
        if row["payment_intent_id"] is None:
            raise ValueError(
                f"order for {input_data.order_line_item_id!r} has no captured payment to refund"
            )
        trail.record(
            event="order_line_item_loaded", refund_amount=row["line_total_cents"]
        )

        from obase.provider_registry import ProviderRegistry

        provider = ProviderRegistry.get().generic("payment", config.payment_provider)
        refund_result = await provider.refund(
            intent_id=row["payment_intent_id"], amount=row["line_total_cents"]
        )
        trail.record(
            event="refunded",
            intent_id=row["payment_intent_id"],
            amount=row["line_total_cents"],
            reason=input_data.reason,
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            cost_usd=0.0,
            refund_amount=row["line_total_cents"],
            intent_id=row["payment_intent_id"],
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
