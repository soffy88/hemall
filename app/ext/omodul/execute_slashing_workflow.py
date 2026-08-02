"""app.ext.omodul.execute_slashing_workflow — 供应商斩仓处罚。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

#: 信誉分红线——跌破这个分数直接冻结供应商账号 (status='slashed')。
_TRUST_SCORE_RED_LINE = 60
#: 斩仓罚金倍数——SPEC 明确写死 3 倍。
_SLASHING_MULTIPLIER = 3


class ExecuteSlashingWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "execute_slashing_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"supplier_id", "order_id"}
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail", "cost"}

    payment_provider: str = "manual"


class ExecuteSlashingWorkflowInput(BaseModel):
    supplier_id: str
    order_id: str
    penalty_base_amount: int
    # 调用方 (仲裁引擎) 用 oskill.compute_supplier_trust_score 算好的最新信誉分——
    # 这个 omodul 只管"给定新分数，落地扣款+按分数决定是否冻结"，不重复算分。
    new_trust_score: int
    reason: str


async def execute_slashing_workflow(
    config: ExecuteSlashingWorkflowConfig,
    input_data: ExecuteSlashingWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """接收仲裁引擎的斩仓指令，直接从 suppliers.escrow_balance 扣除 3 倍罚金，
    原路退给消费者；信誉分跌破红线 (60) 直接冻结账号 (status='slashed')。

    "原路退给消费者"受限于支付网关只能退不超过原订单实付金额——3 倍罚金如果
    超过订单实付总额，超出部分只体现为对供应商 escrow_balance 的扣款 (真实的
    资金惩罚)，不会凭空多退给消费者 (支付网关的 refund 语义就是不允许超退，
    这是诚实的约束，不是漏做)。

    Args:
        config: ExecuteSlashingWorkflowConfig(payment_provider="manual")。
        input_data: supplier_id / order_id (原路退款用) / penalty_base_amount
            (罚金基数，分) / new_trust_score (仲裁引擎算好的最新信誉分) / reason。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 penalty_amount (3 倍罚金总额) /
        refund_amount (实际原路退款额，可能小于 penalty_amount) /
        escrow_balance (扣款后余额) / frozen (bool，是否触发冻结)。
    """
    trail = Trail()
    fp = compute_fingerprint(
        {"supplier_id": input_data.supplier_id, "order_id": input_data.order_id}
    )

    try:
        if pool is None:
            raise ValueError(
                "pool is required — execute_slashing_workflow always touches persisted stock"
            )
        if input_data.penalty_base_amount <= 0:
            raise ValueError("penalty_base_amount must be positive")

        async with pool.acquire() as conn:
            supplier = await conn.fetchrow(
                'SELECT escrow_balance, status FROM "supplier" WHERE id = $1',
                input_data.supplier_id,
            )
            if supplier is None:
                raise ValueError(f"supplier {input_data.supplier_id!r} not found")
            order = await conn.fetchrow(
                "SELECT payment_provider_name, payment_intent_id, grand_total_cents "
                'FROM "customer_order" WHERE id = $1',
                input_data.order_id,
            )
            if order is None:
                raise ValueError(f"order {input_data.order_id!r} not found")

        penalty_amount = input_data.penalty_base_amount * _SLASHING_MULTIPLIER
        new_escrow = max(supplier["escrow_balance"] - penalty_amount, 0)
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE "supplier" SET escrow_balance = $1, trust_score = $2 WHERE id = $3',
                new_escrow,
                input_data.new_trust_score,
                input_data.supplier_id,
            )
        trail.record(
            event="escrow_debited",
            penalty_amount=penalty_amount,
            escrow_balance=new_escrow,
            new_trust_score=input_data.new_trust_score,
        )

        refund_amount = min(penalty_amount, order["grand_total_cents"])
        from obase.provider_registry import ProviderRegistry

        provider = ProviderRegistry.get().generic(
            "payment", order["payment_provider_name"] or config.payment_provider
        )
        refund_result = await provider.refund(
            intent_id=order["payment_intent_id"], amount=refund_amount
        )
        trail.record(
            event="consumer_refunded",
            intent_id=refund_result["intent_id"],
            refund_amount=refund_amount,
        )

        frozen = input_data.new_trust_score < _TRUST_SCORE_RED_LINE
        if frozen:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE \"supplier\" SET status = 'slashed' WHERE id = $1",
                    input_data.supplier_id,
                )
            trail.record(event="supplier_frozen", supplier_id=input_data.supplier_id)

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            cost_usd=0.0,
            penalty_amount=penalty_amount,
            refund_amount=refund_amount,
            escrow_balance=new_escrow,
            frozen=frozen,
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
