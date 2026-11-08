"""app.weigh_settle — 线上称重订单的履约称重与多退少补结算。

流程：
    下单（预估重量/预估金额） → 商家录入实称重量 → 计算实际金额与差额
    → 容差内自动多退少补；超出容差需顾客确认 → 退款走既有 refund_service；
      多收部分在未接预授权时只记“待补收”，绝不伪造二次扣款。

幂等：order_line_item_id 在 weigh_settlement 上唯一；重复提交同一重量
返回原记录，不同重量返回冲突。金额一律整数分，规则见 app.weigh_price。
"""

from __future__ import annotations

import logging
from typing import Any, cast

from .payments.refund_models import RefundReason, RefundType
from .payments.refund_service import (
    RefundError,
    approve_refund,
    create_refund_request,
)
from .weigh_price import diff_policy, weigh_amount_cents

logger = logging.getLogger("hemall.weigh_settle")


class WeighSettlementError(Exception):
    """可安全返回给 API 的称重结算错误。"""


class WeighSettlementNotFound(WeighSettlementError):
    pass


class WeighSettlementConflict(WeighSettlementError):
    pass


def _settlement_dict(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "order_id": str(row["order_id"]),
        "order_line_item_id": str(row["order_line_item_id"]),
        "batch_id": str(row["batch_id"]) if row["batch_id"] else None,
        "weight_grams": int(row["weight_grams"]),
        "unit_price_cents_per_jin": int(row["unit_price_cents_per_jin"]),
        "price_basis_grams": int(row["price_basis_grams"]),
        "estimated_weight_grams": (
            int(row["estimated_weight_grams"]) if row["estimated_weight_grams"] else None
        ),
        "estimated_amount_cents": int(row["estimated_amount_cents"] or 0),
        "actual_amount_cents": int(row["actual_amount_cents"]),
        "diff_cents": int(row["diff_cents"]),
        "tolerance_percent": int(row["tolerance_percent"]),
        "within_tolerance": bool(row["within_tolerance"]),
        "status": str(row["status"]),
        "refund_id": str(row["refund_id"]) if row["refund_id"] else None,
        "note": str(row["note"] or ""),
        "created_by": str(row["created_by"] or ""),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "settled_at": row["settled_at"].isoformat() if row["settled_at"] else None,
    }


async def _paid_amount(pool: Any, order_id: str) -> int:
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                "SELECT COALESCE(SUM(amount_cents), 0) FROM payment_intent "
                "WHERE order_id = $1 AND status = 'paid'",
                order_id,
            )
            or 0
        )


async def _apply_refund(
    pool: Any,
    *,
    order_id: str,
    order_line_item_id: str,
    customer_id: str | None,
    amount_cents: int,
    settlement_id: str,
) -> tuple[str, str | None, str]:
    """对差额退款；返回 (settlement_status, refund_id, note)。失败只记录不抛。"""
    if amount_cents <= 0:
        return "recorded", None, ""
    if customer_id is None:
        return "manual_refund_pending", None, "散客订单无顾客账号，退款待人工处理"
    paid = await _paid_amount(pool, order_id)
    if paid <= 0:
        return "manual_refund_pending", None, "订单尚未确认收款，退款待人工处理"
    try:
        refund = await create_refund_request(
            pool,
            order_id=order_id,
            customer_id=customer_id,
            type=RefundType.REFUND_ONLY,
            reason=RefundReason.OTHER,
            reason_detail=f"称重差额退款（结算单 {settlement_id}）",
            requested_amount=amount_cents,
            order_line_id=order_line_item_id,
        )
        executed = await approve_refund(
            pool,
            refund.id,
            reviewer_id="00000000-0000-4000-8000-000000000000",
            approved_amount=amount_cents,
        )
        status = "refunded" if executed.status.value == "succeeded" else "manual_refund_pending"
        note = "" if status == "refunded" else "manual 支付模式：退款待线下确认"
        return status, executed.id, note
    except (RefundError, Exception) as exc:  # noqa: BLE001 - 退款失败必须留痕，不吞单
        logger.warning("weigh settlement refund failed: %s", exc)
        return "manual_refund_pending", None, f"自动退款失败，待人工处理：{exc}"


async def record_actual_weight(
    pool: Any,
    *,
    order_line_item_id: str,
    weight_grams: int,
    tolerance_percent: int = 10,
    created_by: str = "",
    note: str = "",
) -> dict[str, Any]:
    """录入实称重量并结算一行；幂等，重复同重量返回原记录。"""
    from .weigh_price import validate_weight_grams

    validate_weight_grams(weight_grams, field="actual_weight_grams")

    async with pool.acquire() as conn:
        async with conn.transaction():
            line = await conn.fetchrow(
                """
                SELECT oli.id, oli.order_id, oli.batch_id, oli.pricing_mode,
                       oli.unit_price_cents_per_jin, oli.price_basis_grams,
                       oli.estimated_weight_grams, oli.estimated_amount_cents,
                       oli.line_total_cents, oli.settlement_status,
                       o.customer_id, o.status AS order_status
                FROM order_line_item oli
                JOIN customer_order o ON o.id = oli.order_id
                WHERE oli.id = $1
                FOR UPDATE OF oli, o
                """,
                order_line_item_id,
            )
            if line is None:
                raise WeighSettlementNotFound("order line not found")
            if line["pricing_mode"] != "weight":
                raise WeighSettlementError("order line is not weight-priced")
            unit_per_jin = int(line["unit_price_cents_per_jin"] or 0)
            basis = int(line["price_basis_grams"] or 500)
            estimated_amount = int(
                line["estimated_amount_cents"]
                if line["estimated_amount_cents"] is not None
                else line["line_total_cents"]
            )
            actual_amount = weigh_amount_cents(weight_grams, unit_per_jin, price_basis_grams=basis)
            policy = diff_policy(
                estimated_amount, actual_amount, tolerance_percent=tolerance_percent
            )
            existing = await conn.fetchrow(
                "SELECT * FROM weigh_settlement WHERE order_line_item_id = $1",
                order_line_item_id,
            )
            if existing is not None:
                if int(existing["weight_grams"]) != weight_grams:
                    raise WeighSettlementConflict(
                        f"line already settled at {existing['weight_grams']}g"
                    )
                return _settlement_dict(existing)

            diff_cents = int(cast(int, policy["diff_cents"]))
            tolerance = int(cast(int, policy["tolerance_percent"]))
            within_tolerance = bool(policy["within_tolerance"])
            action = str(policy["action"])
            if line["order_status"] in ("cancelled", "refunded", "failed"):
                raise WeighSettlementError(
                    f"order is {line['order_status']}; cannot settle weighing"
                )
            if action in ("confirm_refund", "confirm_charge"):
                settlement_status = "needs_customer_confirmation"
                line_status = "needs_confirmation"
            elif action == "refund":
                settlement_status = "auto_refund"
                line_status = "auto_settled"
            elif action == "charge":
                settlement_status = "charge_pending"
                line_status = "charge_pending"
            else:
                settlement_status = "recorded"
                line_status = "auto_settled"

            row = await conn.fetchrow(
                """
                INSERT INTO weigh_settlement
                    (order_id, order_line_item_id, batch_id, weight_grams,
                     unit_price_cents_per_jin, price_basis_grams,
                     estimated_weight_grams, estimated_amount_cents,
                     actual_amount_cents, diff_cents, tolerance_percent,
                     within_tolerance, status, note, created_by,
                     settled_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        $13, $14, $15, CASE WHEN $13 IN ('recorded', 'auto_refund',
                        'charge_pending') THEN NOW() ELSE NULL END)
                RETURNING *
                """,
                line["order_id"],
                order_line_item_id,
                line["batch_id"],
                weight_grams,
                unit_per_jin,
                basis,
                line["estimated_weight_grams"],
                estimated_amount,
                actual_amount,
                diff_cents,
                tolerance,
                within_tolerance,
                settlement_status,
                note,
                created_by,
            )
            await conn.execute(
                """
                UPDATE order_line_item
                SET actual_weight_grams = $1, actual_amount_cents = $2,
                    weight_diff_cents = $3, settlement_status = $4
                WHERE id = $5
                """,
                weight_grams,
                actual_amount,
                diff_cents,
                line_status,
                order_line_item_id,
            )

    # 退款在事务外走既有 refund_service（内部自己开连接/行锁）。
    if action == "refund":
        status, refund_id, refund_note = await _apply_refund(
            pool,
            order_id=str(line["order_id"]),
            order_line_item_id=order_line_item_id,
            customer_id=str(line["customer_id"]) if line["customer_id"] else None,
            amount_cents=-diff_cents,
            settlement_id=str(row["id"]),
        )
        async with pool.acquire() as conn:
            refreshed = await conn.fetchrow(
                """
                UPDATE weigh_settlement
                SET status = $1, refund_id = $2::uuid,
                    note = CASE WHEN $3 = '' THEN note ELSE $3 END,
                    updated_at = NOW()
                WHERE id = $4 RETURNING *
                """,
                status,
                refund_id,
                refund_note,
                row["id"],
            )
        row = refreshed

    return _settlement_dict(row)


async def confirm_settlement(
    pool: Any,
    *,
    settlement_id: str,
    customer_id: str | None,
    accept: bool,
    note: str = "",
) -> dict[str, Any]:
    """顾客确认超出容差的差额。accept=False 转人工争议，不自动退款/补收。"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT ws.*, o.customer_id
                FROM weigh_settlement ws
                JOIN customer_order o ON o.id = ws.order_id
                WHERE ws.id = $1
                FOR UPDATE OF ws
                """,
                settlement_id,
            )
            if row is None:
                raise WeighSettlementNotFound("settlement not found")
            if customer_id is not None and str(row["customer_id"] or "") != str(customer_id):
                raise WeighSettlementError("settlement does not belong to customer")
            if row["status"] not in (
                "needs_customer_confirmation",
                "customer_accepted",
                "customer_rejected",
            ):
                return _settlement_dict(row)
            if row["status"] == "needs_customer_confirmation":
                if not accept:
                    updated = await conn.fetchrow(
                        """
                        UPDATE weigh_settlement
                        SET status = 'customer_rejected',
                            note = CASE WHEN $1 = '' THEN note ELSE $1 END,
                            updated_at = NOW(), settled_at = NOW()
                        WHERE id = $2 RETURNING *
                        """,
                        note or "顾客拒绝称重差额，转人工处理",
                        settlement_id,
                    )
                    await conn.execute(
                        "UPDATE order_line_item SET settlement_status = 'disputed' WHERE id = $1",
                        row["order_line_item_id"],
                    )
                    return _settlement_dict(updated)
                updated = await conn.fetchrow(
                    """
                    UPDATE weigh_settlement
                    SET status = 'customer_accepted',
                        note = CASE WHEN $1 = '' THEN note ELSE $1 END,
                        updated_at = NOW()
                    WHERE id = $2 RETURNING *
                    """,
                    note,
                    settlement_id,
                )
                await conn.execute(
                    "UPDATE order_line_item SET settlement_status = 'confirmed' WHERE id = $1",
                    row["order_line_item_id"],
                )
                row = updated

    diff = int(row["diff_cents"])
    if diff < 0:
        status, refund_id, refund_note = await _apply_refund(
            pool,
            order_id=str(row["order_id"]),
            order_line_item_id=str(row["order_line_item_id"]),
            customer_id=str(row["customer_id"]) if row["customer_id"] else None,
            amount_cents=-diff,
            settlement_id=str(row["id"]),
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE weigh_settlement
                SET status = $1, refund_id = $2::uuid,
                    note = CASE WHEN $3 = '' THEN note ELSE $3 END,
                    updated_at = NOW() RETURNING *
                """,
                status,
                refund_id,
                refund_note,
                row["id"],
            )
    elif diff > 0:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE weigh_settlement
                SET status = 'charge_pending', updated_at = NOW()
                WHERE id = $1 RETURNING *
                """,
                row["id"],
            )
    else:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE weigh_settlement SET status = 'recorded', updated_at = NOW(), "
                "settled_at = NOW() WHERE id = $1 RETURNING *",
                row["id"],
            )
    return _settlement_dict(row)


async def mark_charge_collected(
    pool: Any, *, settlement_id: str, collected_by: str = "", note: str = ""
) -> dict[str, Any]:
    """商家确认多出部分已线下补收（manual 模式的闭环）。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE weigh_settlement
            SET status = 'auto_refund', note = CASE WHEN $1 = '' THEN note ELSE $1 END,
                updated_at = NOW(), settled_at = NOW()
            WHERE id = $2 AND status = 'charge_pending'
            RETURNING *
            """,
            note,
            settlement_id,
        )
        if row is None:
            raise WeighSettlementConflict("settlement is not awaiting charge")
        await conn.execute(
            "UPDATE order_line_item SET settlement_status = 'auto_settled' WHERE id = $1",
            row["order_line_item_id"],
        )
    return _settlement_dict(row)
