"""app.ext.omodul.report_phantom_stock_workflow — 幽灵库存上报与秒退。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint


class ReportPhantomStockWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "report_phantom_stock_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"order_line_item_id", "worker_id"}
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail"}

    payment_provider: str = "manual"


class ReportPhantomStockWorkflowInput(BaseModel):
    order_line_item_id: str
    worker_id: str
    reason: str = "phantom_stock"


async def report_phantom_stock_workflow(
    config: ReportPhantomStockWorkflowConfig,
    input_data: ReportPhantomStockWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """大妈拣货发现实物丢失 (幽灵库存)：清零该批次系统库存 (stock_qty=0，
    对应"账实不符，物理架上什么都没有"这个既成事实——不是只扣这一单的量，
    整批都当场核销)，释放这一单的硬锁份额，秒退顾客，损失记入该节点损耗账本。

    Args:
        config: ReportPhantomStockWorkflowConfig(payment_provider="manual")。
        input_data: order_line_item_id / worker_id (报告人) / reason。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 refund_amount / loss_amount /
        location_id。
    """
    trail = Trail()
    fp = compute_fingerprint(
        {
            "order_line_item_id": input_data.order_line_item_id,
            "worker_id": input_data.worker_id,
        }
    )

    try:
        if pool is None:
            raise ValueError(
                "pool is required — report_phantom_stock_workflow always touches persisted stock"
            )

        async with pool.acquire() as conn:
            oli = await conn.fetchrow(
                "SELECT order_id, batch_id, quantity, line_total_cents "
                'FROM "order_line_item" WHERE id = $1',
                input_data.order_line_item_id,
            )
            if oli is None:
                raise ValueError(
                    f"order_line_item {input_data.order_line_item_id!r} not found"
                )
            batch = await conn.fetchrow(
                'SELECT location_id, cost_price_cents FROM "inventory_batch" WHERE id = $1',
                oli["batch_id"],
            )
            if batch is None:
                raise ValueError(f"batch {oli['batch_id']!r} not found")
            order = await conn.fetchrow(
                'SELECT payment_provider_name, payment_intent_id FROM "customer_order" WHERE id = $1',
                oli["order_id"],
            )
            if order is None:
                raise ValueError(f"order {oli['order_id']!r} not found")

        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE "inventory_batch" '
                "SET stock_qty = 0, reserved_qty = GREATEST(reserved_qty - $1, 0) "
                "WHERE id = $2",
                oli["quantity"],
                oli["batch_id"],
            )
        trail.record(
            event="stock_zeroed",
            batch_id=str(oli["batch_id"]),
            reported_qty=oli["quantity"],
        )

        from obase.provider_registry import ProviderRegistry

        provider = ProviderRegistry.get().generic(
            "payment", order["payment_provider_name"] or config.payment_provider
        )
        refund_result = await provider.refund(
            intent_id=order["payment_intent_id"], amount=oli["line_total_cents"]
        )
        trail.record(
            event="instant_refund_issued",
            intent_id=refund_result["intent_id"],
            amount=oli["line_total_cents"],
        )

        from obase.uuid7 import uuid7

        loss_amount = batch["cost_price_cents"] * oli["quantity"]
        ledger_id = uuid7()
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO "stock_location_loss_ledger" '
                "(id, location_id, batch_id, amount_cents, reason) "
                "VALUES ($1, $2, $3, $4, $5)",
                ledger_id,
                batch["location_id"],
                oli["batch_id"],
                loss_amount,
                input_data.reason,
            )
        trail.record(
            event="loss_recorded",
            ledger_id=ledger_id,
            location_id=str(batch["location_id"]),
            loss_amount=loss_amount,
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            refund_amount=oli["line_total_cents"],
            loss_amount=loss_amount,
            location_id=str(batch["location_id"]),
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
