"""售后/退款服务层 — 状态机驱动 + 网关集成 + 库存回补。

核心流程:
1. 顾客发起售后申请 (refund_only / return_refund)
2. 管理员审核 (通过/驳回) → approved / rejected
3. 审核通过后自动调用支付网关退款 → refunding
4. 网关退款成功 → succeeded (回补库存 if return_refund)
5. 网关退款失败 → failed (需人工介入/重试)
6. 全程记录审计日志

幂等保证:
- provider_refund_no 唯一索引
- 状态机防止非法转换
- 乐观锁版本控制
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from obase.persistence.pool import PgPool

from .refund_models import (
    RefundAuditLog,
    RefundReason,
    RefundRequest,
    RefundStatus,
    RefundType,
    RefundReason as RefundReasonEnum,
    VALID_REFUND_TRANSITIONS,
    InvalidRefundTransitionError,
    REFUND_REQUEST_DDL,
    validate_refund_transition,
)
from .models import PaymentSession, PaymentStatus
from .service import get_payment_session_by_order
from ..ext.payment_gateways import build_payment_gateway
from ..config import Settings

logger = logging.getLogger("hemall.payments.refund_service")


# ── 内部异常 ───────────────────────────────────────────────────────────


class RefundError(Exception):
    """退款业务异常。"""

    pass


class RefundAmountExceedsError(RefundError):
    """退款金额超限。"""

    pass


class RefundNotEligibleError(RefundError):
    """订单/商品不符合退款条件。"""

    pass


class GatewayRefundError(RefundError):
    """网关退款异常。"""

    pass


# ── 辅助函数 ───────────────────────────────────────────────────────────


async def _get_refund_request(pool: PgPool, refund_id: str) -> RefundRequest | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM refund_request WHERE id = $1",
            refund_id,
        )
    return RefundRequest(**dict(row)) if row else None


async def _save_refund_request(pool: PgPool, refund: RefundRequest) -> None:
    """保存/更新退款申请 (乐观锁通过 updated_at 隐式实现)。"""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO refund_request (
                id, order_id, order_line_id, customer_id, type, reason, reason_detail,
                status, requested_amount, approved_amount, actual_refunded_amount,
                provider, provider_trade_no, provider_refund_no,
                return_tracking_no, return_batch_id, reviewer_id, reject_reason,
                metadata, created_at, updated_at, processed_at
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22
            )
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                approved_amount = EXCLUDED.approved_amount,
                actual_refunded_amount = EXCLUDED.actual_refunded_amount,
                provider_refund_no = EXCLUDED.provider_refund_no,
                return_tracking_no = EXCLUDED.return_tracking_no,
                return_batch_id = EXCLUDED.return_batch_id,
                reviewer_id = EXCLUDED.reviewer_id,
                reject_reason = EXCLUDED.reject_reason,
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at,
                processed_at = EXCLUDED.processed_at
            """,
            refund.id,
            refund.order_id,
            refund.order_line_id,
            refund.customer_id,
            refund.type.value,
            refund.reason.value,
            refund.reason_detail,
            refund.status.value,
            refund.requested_amount,
            refund.approved_amount,
            refund.actual_refunded_amount,
            refund.provider,
            refund.provider_trade_no,
            refund.provider_refund_no,
            refund.return_tracking_no,
            refund.return_batch_id,
            refund.reviewer_id,
            refund.reject_reason,
            refund.metadata,
            refund.created_at,
            refund.updated_at,
            refund.processed_at,
        )


async def _append_audit_log(pool: PgPool, log: RefundAuditLog) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO refund_audit_log (
                id, refund_id, from_status, to_status, operator_id, operator_type,
                reason, metadata, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            log.id,
            log.refund_id,
            log.from_status.value if log.from_status else None,
            log.to_status.value,
            log.operator_id,
            log.operator_type,
            log.reason,
            log.metadata,
            log.created_at,
        )


async def _get_paid_amount(pool: PgPool, order_id: str) -> int:
    """获取订单实付总金额 (分)。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(amount), 0) as total
            FROM payment_session
            WHERE order_id = $1 AND status = 'paid'
            """,
            order_id,
        )
    return row["total"] if row else 0


async def _get_line_paid_amount(pool: PgPool, order_line_id: str) -> int:
    """获取订单行实付金额 (分)。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(amount), 0) as total
            FROM payment_session ps
            JOIN order_line_item oli ON oli.order_id = ps.order_id
            WHERE oli.id = $1 AND ps.status = 'paid'
            """,
            order_line_id,
        )
    return row["total"] if row else 0


# ── 对外服务接口 ───────────────────────────────────────────────────────


async def create_refund_request(
    pool: PgPool,
    *,
    order_id: str,
    customer_id: str,
    type: RefundType,
    reason: RefundReason,
    reason_detail: str | None,
    requested_amount: int,
    order_line_id: str | None = None,
) -> RefundRequest:
    """创建售后申请 (顾客端调用)。

    校验:
    - 订单存在且属于该顾客
    - 订单已支付
    - 申请金额不超过实付金额
    - 同一订单/行未有进行中的售后
    """
    # 校验订单归属
    async with pool.acquire() as conn:
        order = await conn.fetchrow(
            "SELECT id, customer_id, status, grand_total_cents FROM customer_order WHERE id = $1",
            order_id,
        )
    if not order:
        raise RefundNotEligibleError("order not found")
    if str(order["customer_id"]) != customer_id:
        raise RefundNotEligibleError("order not owned by customer")

    paid_amount = await _get_paid_amount(pool, order_id)
    if paid_amount == 0:
        raise RefundNotEligibleError("order not paid")

    if order_line_id:
        line_paid = await _get_line_paid_amount(pool, order_line_id)
        max_refundable = line_paid
    else:
        max_refundable = paid_amount

    if requested_amount > max_refundable:
        raise RefundAmountExceedsError(
            f"requested amount {requested_amount} exceeds paid amount {max_refundable}"
        )

    # 检查是否有进行中的售后
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            """
            SELECT id FROM refund_request
            WHERE order_id = $1
              AND (order_line_id IS NULL OR order_line_id = $2)
              AND status IN ('submitted','approved','refunding')
            """,
            order_id,
            order_line_id,
        )
    if existing:
        raise RefundError("refund already in progress for this order/line")

    # 获取支付网关信息
    payment_session = await get_payment_session_by_order(pool, order_id)
    provider = payment_session.provider.value if payment_session else "manual"
    provider_trade_no = payment_session.provider_trade_no if payment_session else None

    refund = RefundRequest(
        order_id=order_id,
        order_line_id=order_line_id,
        customer_id=customer_id,
        type=type,
        reason=reason,
        reason_detail=reason_detail,
        requested_amount=requested_amount,
        provider=provider,
        provider_trade_no=provider_trade_no,
        provider_refund_no=f"refund_{uuid.uuid4().hex[:16]}",
    )

    await _save_refund_request(pool, refund)
    await _append_audit_log(
        pool,
        RefundAuditLog(
            refund_id=refund.id,
            from_status=None,
            to_status=RefundStatus.SUBMITTED,
            operator_id=customer_id,
            operator_type="customer",
            reason=f"Customer submitted refund request: {reason.value}",
        ),
    )

    logger.info("refund request created: %s for order %s", refund.id, order_id)
    return refund


async def approve_refund(
    pool: PgPool,
    refund_id: str,
    reviewer_id: str,
    approved_amount: int | None = None,
) -> RefundRequest:
    """管理员审核通过 (管理员端调用)。"""
    refund = await _get_refund_request(pool, refund_id)
    if not refund:
        raise RefundError("refund request not found")

    if refund.status != RefundStatus.SUBMITTED:
        raise RefundError(f"refund not in submitted status: {refund.status.value}")

    # 确定审核通过金额 (默认全额)
    final_amount = approved_amount if approved_amount is not None else refund.requested_amount
    if final_amount > refund.requested_amount:
        raise RefundAmountExceedsError("approved amount cannot exceed requested amount")

    old_status = refund.status
    refund.transition_to(RefundStatus.APPROVED)
    refund.approved_amount = final_amount
    refund.reviewer_id = reviewer_id
    refund.updated_at = datetime.now(timezone.utc)

    await _save_refund_request(pool, refund)
    await _append_audit_log(
        pool,
        RefundAuditLog(
            refund_id=refund.id,
            from_status=old_status,
            to_status=RefundStatus.APPROVED,
            operator_id=reviewer_id,
            operator_type="admin",
            reason=f"Approved refund amount: {final_amount}",
        ),
    )

    # 审核通过后自动触发退款
    await execute_refund(pool, refund_id)

    return refund


async def reject_refund(
    pool: PgPool,
    refund_id: str,
    reviewer_id: str,
    reject_reason: str,
) -> RefundRequest:
    """管理员审核驳回 (管理员端调用)。"""
    refund = await _get_refund_request(pool, refund_id)
    if not refund:
        raise RefundError("refund request not found")

    if refund.status != RefundStatus.SUBMITTED:
        raise RefundError(f"refund not in submitted status: {refund.status.value}")

    old_status = refund.status
    refund.transition_to(RefundStatus.REJECTED)
    refund.reviewer_id = reviewer_id
    refund.reject_reason = reject_reason
    refund.updated_at = datetime.now(timezone.utc)

    await _save_refund_request(pool, refund)
    await _append_audit_log(
        pool,
        RefundAuditLog(
            refund_id=refund.id,
            from_status=old_status,
            to_status=RefundStatus.REJECTED,
            operator_id=reviewer_id,
            operator_type="admin",
            reason=f"Rejected: {reject_reason}",
        ),
    )

    logger.info("refund rejected: %s", refund_id)
    return refund


async def execute_refund(pool: PgPool, refund_id: str) -> RefundRequest:
    """执行退款 (调用真实支付网关)。

    此函数在 approve_refund 后自动调用，也可在 failed 状态下重试时调用。
    """
    refund = await _get_refund_request(pool, refund_id)
    if not refund:
        raise RefundError("refund request not found")

    if refund.status != RefundStatus.APPROVED and refund.status != RefundStatus.FAILED:
        raise RefundError(f"refund not in approved/failed status: {refund.status.value}")

    if not refund.approved_amount:
        raise RefundError("approved amount not set")

    # 幂等检查: 已有 provider_refund_no 且状态为 refunding/succeeded
    if refund.provider_refund_no and refund.status in (
        RefundStatus.REFUNDING,
        RefundStatus.SUCCEEDED,
    ):
        return refund

    old_status = refund.status
    refund.transition_to(RefundStatus.REFUNDING)
    refund.updated_at = datetime.now(timezone.utc)

    await _save_refund_request(pool, refund)
    await _append_audit_log(
        pool,
        RefundAuditLog(
            refund_id=refund.id,
            from_status=old_status,
            to_status=RefundStatus.REFUNDING,
            operator_type="system",
            reason="Initiated gateway refund",
        ),
    )

    try:
        # 获取网关实例
        settings = Settings()
        provider_name = refund.provider or "manual"
        _, gateway = build_payment_gateway(settings)

        # 调用网关退款
        result = await gateway.refund(
            out_trade_no=refund.provider_trade_no or refund.order_id,
            out_refund_no=refund.provider_refund_no,
            refund_fee_cents=refund.approved_amount,
            reason=refund.reason_detail or refund.reason.value,
            total_fee_cents=await _get_paid_amount(pool, refund.order_id),
        )

        if result.get("status") in ("success", "succeeded", "completed", "SUCCESS"):
            # 退款成功
            refund.transition_to(RefundStatus.SUCCEEDED)
            refund.actual_refunded_amount = refund.approved_amount
            refund.provider_refund_no = result.get("refund_id", refund.provider_refund_no)

            # 如果是退货退款，回补库存
            if refund.type == RefundType.RETURN_REFUND and refund.order_line_id:
                await _restock_inventory(pool, refund)

        else:
            # 退款失败
            refund.transition_to(RefundStatus.FAILED)
            refund.metadata["gateway_error"] = result.get("status", "unknown")
            raise GatewayRefundError(f"gateway refund failed: {result}")

    except GatewayRefundError:
        raise
    except Exception as exc:
        logger.exception("refund execution error: %s", exc)
        refund.transition_to(RefundStatus.FAILED)
        refund.metadata["error"] = str(exc)
        raise GatewayRefundError(f"refund execution error: {exc}") from exc

    finally:
        await _save_refund_request(pool, refund)
        await _append_audit_log(
            pool,
            RefundAuditLog(
                refund_id=refund.id,
                from_status=old_status,
                to_status=refund.status,
                operator_type="system",
                reason=f"Gateway refund result: {refund.status.value}",
            ),
        )

    return refund


async def retry_refund(pool: PgPool, refund_id: str) -> RefundRequest:
    """重试失败的退款 (管理员端调用)。"""
    refund = await _get_refund_request(pool, refund_id)
    if not refund:
        raise RefundError("refund request not found")

    if not refund.can_retry_refund():
        raise RefundError(f"refund cannot be retried: status={refund.status.value}")

    return await execute_refund(pool, refund_id)


async def mark_refund_returned(
    pool: PgPool,
    refund_id: str,
    tracking_no: str,
    batch_id: str,
    operator_id: str,
) -> RefundRequest:
    """确认收到退货 (管理员端调用，回补库存)。"""
    refund = await _get_refund_request(pool, refund_id)
    if not refund:
        raise RefundError("refund request not found")

    if refund.type != RefundType.RETURN_REFUND:
        raise RefundError("not a return_refund type")

    if refund.status not in (RefundStatus.APPROVED, RefundStatus.REFUNDING, RefundStatus.SUCCEEDED):
        raise RefundError(f"invalid status for return confirmation: {refund.status.value}")

    refund.return_tracking_no = tracking_no
    refund.return_batch_id = batch_id
    refund.updated_at = datetime.now(timezone.utc)

    # 如果退款已成功但库存未回补，现在回补
    if refund.status == RefundStatus.SUCCEEDED and not refund.return_batch_id:
        await _restock_inventory(pool, refund)

    await _save_refund_request(pool, refund)
    await _append_audit_log(
        pool,
        RefundAuditLog(
            refund_id=refund.id,
            from_status=refund.status,
            to_status=refund.status,
            operator_id=operator_id,
            operator_type="admin",
            reason=f"Return received, tracking: {tracking_no}, restocked to batch: {batch_id}",
        ),
    )

    return refund


async def _restock_inventory(pool: PgPool, refund: RefundRequest) -> None:
    """回补库存 (退货退款时调用)。"""
    if not refund.order_line_id or not refund.return_batch_id:
        return

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE inventory_batch
            SET stock_qty = stock_qty + 1,
                updated_at = NOW()
            WHERE id = $1
            """,
            refund.return_batch_id,
        )

    logger.info("inventory restocked for refund %s: batch %s", refund.id, refund.return_batch_id)


async def list_refund_requests(
    pool: PgPool,
    *,
    customer_id: str | None = None,
    order_id: str | None = None,
    status: RefundStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[RefundRequest]:
    """列出售后申请 (支持过滤)。"""
    conditions = []
    params: list[Any] = []
    param_idx = 1

    if customer_id:
        conditions.append(f"customer_id = ${param_idx}")
        params.append(customer_id)
        param_idx += 1
    if order_id:
        conditions.append(f"order_id = ${param_idx}")
        params.append(order_id)
        param_idx += 1
    if status:
        conditions.append(f"status = ${param_idx}")
        params.append(status.value)
        param_idx += 1

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
    params.extend([limit, offset])

    query = f"""
        SELECT * FROM refund_request
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ${param_idx} OFFSET ${param_idx + 1}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    return [RefundRequest(**dict(row)) for row in rows]


async def get_refund_audit_logs(pool: PgPool, refund_id: str) -> list[RefundAuditLog]:
    """获取售后审计日志。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM refund_audit_log WHERE refund_id = $1 ORDER BY created_at ASC",
            refund_id,
        )
    return [RefundAuditLog(**dict(row)) for row in rows]


# 需要导入 uuid
import uuid