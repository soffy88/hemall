"""app.ext.omodul.execute_liability_routing_workflow — 全自动客诉退款与责任方扣款。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

#: VLM 判定造假概率超过此阈值，直接拒赔，不管信誉分给出的裁决是什么。
_FRAUD_REJECTION_THRESHOLD = 0.5
#: VLM 判定损坏严重度低于此阈值视为"没有实质损坏"，直接拒赔。
_MIN_DAMAGE_SEVERITY = 0.05
#: 判定为供应商溯源缺陷 (源头问题) 的损坏类型——对应 liable_party="supplier"，
#: 从供应商 escrow_balance 扣款；其余类型 (挤压/破损/丢件等) 视为微仓履约环节
#: 造成，liable_party="location"，记入 location_loss_ledger (该表在 v2.0 第二轮
#: 才补齐；上一轮先诚实记了"只判定不落账"的空白，这里已经跟着补上)。
_SUPPLIER_LIABLE_DAMAGE_TYPES = frozenset(
    {"spoiled", "rotten", "contaminated", "moldy"}
)


class ExecuteLiabilityRoutingWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "execute_liability_routing_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"order_id", "batch_id", "user_id"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail", "cost"}

    payment_provider: str = "manual"
    currency: str = "CNY"


class ExecuteLiabilityRoutingWorkflowInput(BaseModel):
    order_id: str
    batch_id: str
    user_id: str
    evidence_image_url: str
    vlm_damage_type: str
    vlm_severity: float
    fraud_probability: float
    credibility_decision: str  # "instant" | "honeypot" —— oskill 裁决结果，由调用方传入


async def execute_liability_routing_workflow(
    config: ExecuteLiabilityRoutingWorkflowConfig,
    input_data: ExecuteLiabilityRoutingWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """全自动仲裁的执行末端：给定已经算好的 VLM 判损 + 信誉裁决，落地最终决策
    (拒赔/秒退/丢桶验证)，instant 分支立即退款并按损坏类型判责扣供应商 escrow。

    本函数不重复调 oprim.vlm_assess_damage / oskill.evaluate_claim_credibility——
    那是上游 oservi.autonomous_triage_engine 自己的职责 (SPEC §5 原文：引擎自己
    "注入 oprim.vlm_assess_damage...结合 oskill.evaluate_claim_credibility...
    最后拉起 omodul.execute_liability_routing_workflow")。这里只做"给定裁决
    结果，落地执行"：先判定是否造假/无实质损坏 (直接 rejected，不管信誉裁决
    说什么)，instant 分支退款 + 按损坏类型判责扣供应商 escrow，honeypot 分支
    只记录待物理验证、不动钱——真正的秒退发生在用户实际扫码丢桶时，那是既有
    的 v1.0 omodul.process_drop_return 的职责，这里不重复实现。

    Args:
        config: ExecuteLiabilityRoutingWorkflowConfig(payment_provider="manual", currency="CNY")。
        input_data: order_id / batch_id / user_id / evidence_image_url /
            vlm_damage_type / vlm_severity / fraud_probability (VLM 判定结果) /
            credibility_decision ("instant"/"honeypot"，oskill 裁决结果)。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 claim_id / decision
        ("instant_refund"/"drop_to_bin"/"rejected") / liable_party
        ("supplier"/"location"/None) / refund_amount (非 instant_refund 时为 0)。
    """
    trail = Trail()
    fp = compute_fingerprint(
        {
            "order_id": input_data.order_id,
            "batch_id": input_data.batch_id,
            "user_id": input_data.user_id,
        }
    )

    try:
        if pool is None:
            raise ValueError(
                "pool is required — execute_liability_routing_workflow always touches persisted stock"
            )
        if input_data.credibility_decision not in ("instant", "honeypot"):
            raise ValueError(
                f"credibility_decision must be 'instant' or 'honeypot', "
                f"got {input_data.credibility_decision!r}"
            )

        if input_data.fraud_probability > _FRAUD_REJECTION_THRESHOLD:
            decision = "rejected"
            trail.record(
                event="rejected_fraud_suspected",
                fraud_probability=input_data.fraud_probability,
            )
        elif input_data.vlm_severity < _MIN_DAMAGE_SEVERITY:
            decision = "rejected"
            trail.record(
                event="rejected_no_material_damage", severity=input_data.vlm_severity
            )
        elif input_data.credibility_decision == "honeypot":
            decision = "drop_to_bin"
            trail.record(event="honeypot_pending_physical_verification")
        else:
            decision = "instant_refund"
            trail.record(event="instant_refund_approved")

        liable_party: str | None = None
        refund_amount = 0

        if decision == "instant_refund":
            liable_party = (
                "supplier"
                if input_data.vlm_damage_type in _SUPPLIER_LIABLE_DAMAGE_TYPES
                else "location"
            )

            async with pool.acquire() as conn:
                order = await conn.fetchrow(
                    'SELECT payment_provider_name, payment_intent_id FROM "customer_order" WHERE id = $1',
                    input_data.order_id,
                )
                if order is None:
                    raise ValueError(f"order {input_data.order_id!r} not found")
                line_item = await conn.fetchrow(
                    'SELECT line_total_cents FROM "order_line_item" '
                    "WHERE order_id = $1 AND batch_id = $2",
                    input_data.order_id,
                    input_data.batch_id,
                )
                if line_item is None:
                    raise ValueError(
                        f"no order_line_item for order {input_data.order_id!r} "
                        f"batch {input_data.batch_id!r}"
                    )
            refund_amount = line_item["line_total_cents"]

            from obase.provider_registry import ProviderRegistry

            provider = ProviderRegistry.get().generic(
                "payment", order["payment_provider_name"] or config.payment_provider
            )
            refund_result = await provider.refund(
                intent_id=order["payment_intent_id"], amount=refund_amount
            )
            trail.record(
                event="refund_issued",
                intent_id=refund_result["intent_id"],
                amount=refund_amount,
                liable_party=liable_party,
            )

            if liable_party == "supplier":
                async with pool.acquire() as conn:
                    supplier_id = await conn.fetchval(
                        'SELECT supplier_id FROM "inventory_batch" WHERE id = $1',
                        input_data.batch_id,
                    )
                    if supplier_id:
                        await conn.execute(
                            'UPDATE "supplier" '
                            "SET escrow_balance = GREATEST(escrow_balance - $1, 0) "
                            "WHERE id = $2",
                            refund_amount,
                            supplier_id,
                        )
                        trail.record(
                            event="supplier_escrow_debited",
                            supplier_id=str(supplier_id),
                            amount=refund_amount,
                        )
                    else:
                        trail.record(
                            event="supplier_debit_skipped",
                            detail="batch has no supplier_id on file",
                        )
            else:
                from obase.uuid7 import uuid7

                async with pool.acquire() as conn:
                    location_id = await conn.fetchval(
                        'SELECT location_id FROM "inventory_batch" WHERE id = $1',
                        input_data.batch_id,
                    )
                    ledger_id = uuid7()
                    await conn.execute(
                        'INSERT INTO "stock_location_loss_ledger" '
                        "(id, location_id, batch_id, amount_cents, reason) "
                        "VALUES ($1, $2, $3, $4, 'rma_liability')",
                        ledger_id,
                        location_id,
                        input_data.batch_id,
                        refund_amount,
                    )
                trail.record(
                    event="location_loss_recorded",
                    ledger_id=ledger_id,
                    location_id=str(location_id),
                    amount=refund_amount,
                )

        from obase.uuid7 import uuid7

        claim_id = uuid7()
        items = [
            {
                "batch_id": input_data.batch_id,
                "reason": input_data.vlm_damage_type,
            }
        ]
        # claim.status 是共享商城的生命周期状态 (pending/canceled/fulfilled)，
        # 跟这里的仲裁裁决 decision 是两个维度：instant_refund 已经真的退款，
        # 映射成 fulfilled；drop_to_bin 还没物理验证，映射成 pending；rejected
        # 直接映射成 canceled (拒赔即关闭)。
        status = {
            "instant_refund": "fulfilled",
            "drop_to_bin": "pending",
            "rejected": "canceled",
        }[decision]
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO "claim" '
                "(id, order_id, status, claim_type, items, refund_amount_cents, "
                "user_id, batch_id, evidence_image_url, vlm_damage_type, "
                "vlm_severity, decision, liable_party) "
                "VALUES ($1, $2, $3, 'refund', $4, $5, $6, $7, $8, $9, $10, $11, $12)",
                claim_id,
                input_data.order_id,
                status,
                json.dumps(items),
                refund_amount,
                input_data.user_id,
                input_data.batch_id,
                input_data.evidence_image_url,
                input_data.vlm_damage_type,
                float(input_data.vlm_severity),
                decision,
                liable_party,
            )
        trail.record(event="claim_recorded", claim_id=claim_id, decision=decision)

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            cost_usd=0.0,
            claim_id=claim_id,
            decision=decision,
            liable_party=liable_party,
            refund_amount=refund_amount,
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
