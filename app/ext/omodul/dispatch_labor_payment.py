"""app.ext.omodul.dispatch_labor_payment — 聚合结算大妈计件工资。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oprim import ext_pay_transfer


class DispatchLaborPaymentConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "dispatch_labor_payment"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"worker_id"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail", "cost"}

    payout_provider: str = "manual"


class DispatchLaborPaymentInput(BaseModel):
    worker_id: str
    payout_account: str


async def dispatch_labor_payment(
    config: DispatchLaborPaymentConfig,
    input_data: DispatchLaborPaymentInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """聚合大妈 (worker_id) 名下全部 pending 状态的计件工资 (labor_ledger，
    由 confirm_batch_pick 逐笔记账)，一次性结算微信零钱。

    没有 pending 记录时直接判 failed (没什么可结的)，不当作 completed 空转。

    Args:
        config: DispatchLaborPaymentConfig(payout_provider="manual")。
        input_data: worker_id / payout_account (打款目标账户)。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 total_amount / entry_count / transfer_id。
    """
    trail = Trail()
    fp = compute_fingerprint({"worker_id": input_data.worker_id})

    try:
        if pool is None:
            raise ValueError(
                "pool is required — dispatch_labor_payment always touches persisted stock"
            )

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, wage_amount FROM \"labor_ledger\" WHERE worker_id = $1 AND status = 'pending'",
                input_data.worker_id,
            )
        if not rows:
            raise ValueError(
                f"worker {input_data.worker_id!r} has no pending wage entries"
            )
        entry_ids = [r["id"] for r in rows]
        total_amount = sum(r["wage_amount"] for r in rows)
        trail.record(
            event="pending_wages_loaded",
            entry_count=len(rows),
            total_amount=total_amount,
        )

        transfer_result = await ext_pay_transfer(
            config.payout_provider,
            account=input_data.payout_account,
            amount=total_amount,
        )
        trail.record(
            event="wage_transferred",
            transfer_id=transfer_result["transfer_id"],
            amount=total_amount,
        )

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE \"labor_ledger\" SET status = 'paid', payout_transfer_id = $1 WHERE id = ANY($2)",
                transfer_result["transfer_id"],
                entry_ids,
            )
        trail.record(event="ledger_marked_paid", entry_count=len(entry_ids))

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            cost_usd=0.0,
            total_amount=total_amount,
            entry_count=len(rows),
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
