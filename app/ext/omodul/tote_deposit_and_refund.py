"""app.ext.omodul.tote_deposit_and_refund — 循环筐押金扣除/秒退。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

_DEPOSIT_AMOUNT_CENTS = 1000  # 10 元押金 (SPEC 明确写死的金额)


class ToteDepositAndRefundConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "tote_deposit_and_refund"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"tote_id", "action"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail"}

    payment_provider: str = "manual"
    currency: str = "CNY"


class ToteDepositAndRefundInput(BaseModel):
    tote_id: str
    action: str  # "charge" | "refund"


async def tote_deposit_and_refund(
    config: ToteDepositAndRefundConfig,
    input_data: ToteDepositAndRefundInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """处理循环筐的 10 元押金扣除 (action="charge")，以及用户归还时的极速秒退
    (action="refund")。

    押金走的是"收顾客的钱"这条支付通道 (跟 complete_checkout 一样用
    ProviderRegistry "payment" category)，不是打给供应商/工人的那条 "payout"
    通道——语义上押金最终是要还给顾客的，用 authorize+capture / refund 这套
    正好对齐。

    Args:
        config: ToteDepositAndRefundConfig(payment_provider="manual", currency="CNY")。
        input_data: tote_id / action ("charge"/"refund")。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 tote_id / action / amount。
    """
    trail = Trail()
    fp = compute_fingerprint(
        {"tote_id": input_data.tote_id, "action": input_data.action}
    )

    try:
        if pool is None:
            raise ValueError(
                "pool is required — tote_deposit_and_refund always touches persisted stock"
            )
        if input_data.action not in ("charge", "refund"):
            raise ValueError(
                f"action must be 'charge' or 'refund', got {input_data.action!r}"
            )

        from obase.provider_registry import ProviderRegistry

        provider = ProviderRegistry.get().generic("payment", config.payment_provider)

        if input_data.action == "charge":
            async with pool.acquire() as conn:
                tote = await conn.fetchrow(
                    'SELECT id, status FROM "tote" WHERE id = $1', input_data.tote_id
                )
            if tote is None:
                raise ValueError(f"tote {input_data.tote_id!r} not found")
            if tote["status"] == "with_customer":
                raise ValueError(
                    f"tote {input_data.tote_id!r} already has an active deposit"
                )

            auth_result = await provider.authorize(
                amount=_DEPOSIT_AMOUNT_CENTS,
                currency=config.currency,
                meta={"tote_id": input_data.tote_id},
            )
            capture_result = await provider.capture(intent_id=auth_result["intent_id"])
            trail.record(
                event="deposit_captured",
                intent_id=capture_result["intent_id"],
                amount=_DEPOSIT_AMOUNT_CENTS,
            )

            from obase.uuid7 import uuid7

            deposit_id = uuid7()
            async with pool.acquire() as conn:
                await conn.execute(
                    'INSERT INTO "tote_deposit" (id, tote_id, intent_id, amount_cents, status) '
                    "VALUES ($1, $2, $3, $4, 'charged')",
                    deposit_id,
                    input_data.tote_id,
                    capture_result["intent_id"],
                    _DEPOSIT_AMOUNT_CENTS,
                )
                await conn.execute(
                    "UPDATE \"tote\" SET status = 'with_customer' WHERE id = $1",
                    input_data.tote_id,
                )
            trail.record(
                event="tote_handed_out",
                tote_id=input_data.tote_id,
                deposit_id=deposit_id,
            )

        else:  # refund
            async with pool.acquire() as conn:
                deposit = await conn.fetchrow(
                    'SELECT id, intent_id, amount_cents FROM "tote_deposit" '
                    "WHERE tote_id = $1 AND status = 'charged' ORDER BY created_at DESC LIMIT 1",
                    input_data.tote_id,
                )
            if deposit is None:
                raise ValueError(
                    f"tote {input_data.tote_id!r} has no active (charged) deposit to refund"
                )

            refund_result = await provider.refund(
                intent_id=deposit["intent_id"], amount=deposit["amount_cents"]
            )
            trail.record(
                event="deposit_refunded",
                intent_id=deposit["intent_id"],
                amount=deposit["amount_cents"],
            )

            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE \"tote_deposit\" SET status = 'refunded' WHERE id = $1",
                    deposit["id"],
                )
                await conn.execute(
                    "UPDATE \"tote\" SET status = 'idle' WHERE id = $1",
                    input_data.tote_id,
                )
            trail.record(event="tote_returned", tote_id=input_data.tote_id)

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            tote_id=input_data.tote_id,
            action=input_data.action,
            amount=_DEPOSIT_AMOUNT_CENTS,
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
