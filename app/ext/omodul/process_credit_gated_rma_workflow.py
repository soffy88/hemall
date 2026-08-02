"""app.ext.omodul.process_credit_gated_rma_workflow — 轻量客诉仲裁 (无需视觉判损)。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oskill import evaluate_claim_credibility


class ProcessCreditGatedRmaWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "process_credit_gated_rma_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"order_id", "batch_id", "user_id"}
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail", "cost"}

    payment_provider: str = "manual"


class ProcessCreditGatedRmaWorkflowInput(BaseModel):
    order_id: str
    batch_id: str
    user_id: str
    user_trust_score: int
    batch_anomaly_rate: float = 0.0
    route_risk: float = 0.0


async def process_credit_gated_rma_workflow(
    config: ProcessCreditGatedRmaWorkflowConfig,
    input_data: ProcessCreditGatedRmaWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """统管客诉的轻量入口——不需要视觉证据 (缺件/错发/不想要了这类不需要拍照
    判损的客诉)，只调 oskill.evaluate_claim_credibility 做信誉裁决。instant
    直接秒退；honeypot 挂起退款，生成 24 小时有效的"退货桶投掷凭证"(用
    created_at + 24h 现算，不额外加过期时间列——claim 表的诚实空白约定跟
    execute_liability_routing_workflow 一致)。

    这是 execute_liability_routing_workflow 的轻量姊妹函数：后者要先跑
    oprim.vlm_assess_damage 判定损坏类型/责任方，适合"东西坏了、有图有真相"
    的场景；这个函数没有 VLM 步骤、也不做责任方扣款，适合"东西没坏、就是
    不想要了/发错了"这类场景。两者共用同一张统一后的 claim 表，vlm_damage_type/
    vlm_severity/liable_party 在这个路径下留 NULL。

    Args:
        config: ProcessCreditGatedRmaWorkflowConfig(payment_provider="manual")。
        input_data: order_id / batch_id / user_id / user_trust_score /
            batch_anomaly_rate (默认 0) / route_risk (默认 0)。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 claim_id / decision
        ("instant_refund"/"drop_to_bin") / refund_amount (honeypot 时为 0)。
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
                "pool is required — process_credit_gated_rma_workflow always touches persisted stock"
            )

        credibility_decision = evaluate_claim_credibility(
            input_data.user_trust_score,
            batch_anomaly_rate=input_data.batch_anomaly_rate,
            route_risk=input_data.route_risk,
        )
        trail.record(
            event="credibility_evaluated", credibility_decision=credibility_decision
        )

        refund_amount = 0
        if credibility_decision == "instant":
            decision = "instant_refund"

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
                event="instant_refund_issued",
                intent_id=refund_result["intent_id"],
                amount=refund_amount,
            )
        else:
            decision = "drop_to_bin"
            trail.record(event="honeypot_pending_physical_verification")

        from obase.uuid7 import uuid7

        claim_id = uuid7()
        items = [{"batch_id": input_data.batch_id, "reason": "credit_gated_rma"}]
        status = "fulfilled" if decision == "instant_refund" else "pending"
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO "claim" '
                "(id, order_id, status, claim_type, items, refund_amount_cents, "
                "user_id, batch_id, decision) "
                "VALUES ($1, $2, $3, 'refund', $4, $5, $6, $7, $8)",
                claim_id,
                input_data.order_id,
                status,
                json.dumps(items),
                refund_amount,
                input_data.user_id,
                input_data.batch_id,
                decision,
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
