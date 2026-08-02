"""app.ext.omodul.batch_settlement — 批次售罄后给供应商秒结货款。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oprim import ext_pay_transfer


class BatchSettlementConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "batch_settlement"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"batch_id", "supplier_account"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail", "cost"}

    payout_provider: str = "manual"


class BatchSettlementInput(BaseModel):
    batch_id: str
    supplier_account: str


async def batch_settlement(
    config: BatchSettlementConfig,
    input_data: BatchSettlementInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """批次售罄后 (available == 0，全部卖出/预留)，按 stock_qty * cost_price
    给供应商结清货款，批次状态转 settled。

    SPEC 的 DDL 里没有"供应商"这个实体，supplier_account 由调用方直接传入
    (打款目标账户标识，如微信商户号/银行账号)——不脱离规范新造一张
    suppliers 表，供应商信息的持久化留给以后需要时再加。

    已经结算过的批次 (status='settled') 不能重复结算，防止货款被打两次。

    Args:
        config: BatchSettlementConfig(payout_provider="manual")。
        input_data: batch_id / supplier_account。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 batch_id / amount / transfer_id。
    """
    trail = Trail()
    fp = compute_fingerprint(
        {
            "batch_id": input_data.batch_id,
            "supplier_account": input_data.supplier_account,
        }
    )

    try:
        if pool is None:
            raise ValueError(
                "pool is required — batch_settlement always touches persisted stock"
            )

        async with pool.acquire() as conn:
            batch = await conn.fetchrow(
                "SELECT id, stock_qty, reserved_qty, cost_price_cents, status "
                'FROM "inventory_batch" WHERE id = $1',
                input_data.batch_id,
            )
        if batch is None:
            raise ValueError(f"batch {input_data.batch_id!r} not found")
        if batch["status"] == "settled":
            raise ValueError(f"batch {input_data.batch_id!r} already settled")
        available = batch["stock_qty"] - batch["reserved_qty"]
        if available > 0:
            raise ValueError(
                f"batch {input_data.batch_id!r} not fully sold/reserved yet (available={available})"
            )
        trail.record(
            event="sellout_verified",
            batch_id=input_data.batch_id,
            stock_qty=batch["stock_qty"],
        )

        amount = batch["stock_qty"] * batch["cost_price_cents"]
        transfer_result = await ext_pay_transfer(
            config.payout_provider, account=input_data.supplier_account, amount=amount
        )
        trail.record(
            event="supplier_paid",
            transfer_id=transfer_result["transfer_id"],
            account=input_data.supplier_account,
            amount=amount,
        )

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE \"inventory_batch\" SET status = 'settled' WHERE id = $1",
                input_data.batch_id,
            )
        trail.record(event="batch_marked_settled", batch_id=input_data.batch_id)

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            cost_usd=0.0,
            batch_id=input_data.batch_id,
            amount=amount,
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
