"""售后/退款服务层 — 状态机驱动 + 网关集成 + 库存回补。

核心流程:
1. 顾客发起售后申请 (refund_only / return_refund)
2. 管理员审核 (通过/驳回) → approved / rejected
3. 审核通过后自动调用支付网关退款 → refunding
4. 网关退款成功 → succeeded (回补库存 if return_refund)
5. 网关退款失败 → failed (需人工介入/重试)
6. 全程记录审计日志

并发与幂等保证:
- provider_refund_no 唯一索引 (创建时即生成, 崩溃也不会重复)
- execute_refund 在事务内 SELECT ... FOR UPDATE 行锁上重判状态:
  并发两次退款只有一笔能 approved → refunding, 另一笔直接报错
- 累计退款金额在行锁保护下重算: 已成功/在途 + 本次 <= 实付
- 网关退款回调必须先验签, 再按 provider_refund_no 匹配推进状态 (幂等)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from obase.persistence.pool import PgPool

from ..config import Settings
from ..ext.payment_gateways import build_payment_gateway
from .refund_models import (
    RefundAuditLog,
    RefundReason,
    RefundRequest,
    RefundStatus,
    RefundType,
)

logger = logging.getLogger("hemall.payments.refund_service")


# ── 内部异常 ───────────────────────────────────────────────────────────


class RefundError(Exception):
    """退款业务异常。"""


class RefundAmountExceedsError(RefundError):
    """退款金额超限。"""


class RefundNotEligibleError(RefundError):
    """订单/商品不符合退款条件。"""


class GatewayRefundError(RefundError):
    """网关退款异常。"""


# ── 辅助函数 ───────────────────────────────────────────────────────────


async def _get_refund_request(pool: PgPool, refund_id: str) -> RefundRequest | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM refund_request WHERE id = $1",
            refund_id,
        )
    return RefundRequest.from_row(row) if row else None


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
            json.dumps(refund.metadata),
            refund.created_at,
            refund.updated_at,
            refund.processed_at,
        )


async def _reverse_voucher_for_refund(pool: PgPool, refund: RefundRequest) -> None:
    """仓主试点：退款成功后按比例冲回已发推荐奖励。

    失败只记日志，不动退款结果；冲回本身幂等（reversal:{refund_id}）。
    """

    try:
        from app.cangzhu import reverse_for_refund

        await reverse_for_refund(
            pool,
            refund_id=refund.id,
            order_id=refund.order_id,
            refund_cents=int(refund.actual_refunded_amount or 0),
        )
    except Exception:
        logger.exception("cangzhu voucher reversal failed for refund %s", refund.id)


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
            json.dumps(log.metadata),
            log.created_at,
        )


async def _get_paid_amount(pool: PgPool, order_id: str) -> int:
    """获取订单实付总金额 (分)。

    实付以 payment_intent (app 层支付意图, 回调成功后置 paid) 为准;
    共享 schema 的 payment_session 状态机只有 authorized/selected/canceled/failed,
    没有 paid, 不能用它判断订单是否已支付。
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(amount_cents), 0) as total
            FROM payment_intent
            WHERE order_id = $1 AND status = 'paid'
            """,
            str(order_id),
        )
    return int(row["total"]) if row else 0


async def _reserved_refund_amount(
    pool: PgPool, order_id: str, *, exclude_refund_id: str | None = None
) -> int:
    """订单已被占用的退款额度 (分): 已成功实退 + 在途 (submitted/approved/refunding)
    的申请/批准金额。行锁保护下调用, 结果才是可信的。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(CASE WHEN status = 'succeeded'
                                   THEN actual_refunded_amount
                                   ELSE COALESCE(approved_amount, requested_amount)
                              END), 0) AS reserved
            FROM refund_request
            WHERE order_id = $1
              AND status IN ('submitted', 'approved', 'refunding', 'succeeded')
              AND ($2::uuid IS NULL OR id <> $2)
            """,
            order_id,
            exclude_refund_id,
        )
    return int(row["reserved"]) if row else 0


def _refund_to_dict(refund: RefundRequest) -> dict[str, Any]:
    """序列化为 API 响应 dict (前端 api-client 用)。"""
    return {
        "id": refund.id,
        "order_id": refund.order_id,
        "order_line_id": refund.order_line_id,
        "customer_id": refund.customer_id,
        "type": refund.type.value,
        "reason": refund.reason.value,
        "reason_detail": refund.reason_detail,
        "status": refund.status.value,
        "requested_amount": refund.requested_amount,
        "approved_amount": refund.approved_amount,
        "actual_refunded_amount": refund.actual_refunded_amount,
        "provider": refund.provider,
        "provider_trade_no": refund.provider_trade_no,
        "provider_refund_no": refund.provider_refund_no,
        "return_tracking_no": refund.return_tracking_no,
        "return_batch_id": refund.return_batch_id,
        "reviewer_id": refund.reviewer_id,
        "reject_reason": refund.reject_reason,
        "metadata": refund.metadata,
        "created_at": refund.created_at.isoformat(),
        "updated_at": refund.updated_at.isoformat(),
        "processed_at": refund.processed_at.isoformat() if refund.processed_at else None,
    }


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

    校验 (在订单行锁保护的事务内完成, 并发创建也只会有一笔超额被拒):
    - 订单存在且属于该顾客
    - 订单已支付
    - 申请金额不超过可退额度 (行级申请按行金额占比折算)
    - 同一订单/行未有进行中的售后
    """
    if requested_amount <= 0:
        raise RefundAmountExceedsError("requested amount must be positive")
    if type == RefundType.RETURN_REFUND and not order_line_id:
        raise RefundNotEligibleError(
            "return_refund requires order_line_id so inventory can be restored to the original batch"
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            # 锁订单行, 把同一订单的并发创建/退款串行化
            order = await conn.fetchrow(
                "SELECT id, customer_id, status, grand_total_cents "
                "FROM customer_order WHERE id = $1 FOR UPDATE",
                order_id,
            )
            if not order:
                raise RefundNotEligibleError("order not found")
            if str(order["customer_id"]) != customer_id:
                raise RefundNotEligibleError("order not owned by customer")

            row = await conn.fetchrow(
                "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM payment_intent "
                "WHERE order_id = $1 AND status = 'paid'",
                order_id,
            )
            paid_amount = int(row["total"]) if row else 0
            if paid_amount == 0:
                raise RefundNotEligibleError("order not paid")

            if order_line_id:
                line = await conn.fetchrow(
                    "SELECT id, line_total_cents FROM order_line_item WHERE id = $1",
                    order_line_id,
                )
                if not line or str(line["id"]) != order_line_id:
                    raise RefundNotEligibleError("order line not found")
                grand_total = int(order["grand_total_cents"] or 0)
                line_total = int(line["line_total_cents"] or 0)
                line_cap = (
                    paid_amount * line_total // grand_total if grand_total > 0 else paid_amount
                )
            else:
                line_cap = paid_amount

            # 金额上限按请求范围校验:
            #   订单级 → 全单累计 (含行级退款) ≤ 实付
            #   行级   → 本行+订单级退款累计 ≤ 行分摊上限, 且全单累计 ≤ 实付
            reserved_sql = """
                SELECT COALESCE(SUM(
                    CASE WHEN status = 'succeeded' THEN actual_refunded_amount
                         ELSE COALESCE(approved_amount, requested_amount) END), 0)
                FROM refund_request
                WHERE order_id = $1
                  AND status IN ('submitted', 'approved', 'refunding', 'succeeded')
            """
            order_reserved = int(await conn.fetchval(reserved_sql, order_id) or 0)

            if order_line_id:
                line_reserved = int(
                    await conn.fetchval(
                        reserved_sql + " AND (order_line_id IS NULL OR order_line_id = $2)",
                        order_id,
                        order_line_id,
                    )
                    or 0
                )
                if line_reserved + requested_amount > line_cap:
                    raise RefundAmountExceedsError(
                        f"requested amount {requested_amount} exceeds line refundable "
                        f"{max(line_cap - line_reserved, 0)} (paid={paid_amount}, "
                        f"line_cap={line_cap}, line_reserved={line_reserved})"
                    )

            if order_reserved + requested_amount > paid_amount:
                raise RefundAmountExceedsError(
                    f"requested amount {requested_amount} exceeds refundable "
                    f"{max(paid_amount - order_reserved, 0)} (paid={paid_amount}, "
                    f"reserved={order_reserved})"
                )

            # 检查是否有进行中的售后 (同订单/同行)
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

            # 获取支付网关信息 (同连接内查, 避免事务内二次acquire)
            payment = await conn.fetchrow(
                "SELECT provider, provider_trade_no FROM payment_intent "
                "WHERE order_id = $1 AND status = 'paid' ORDER BY created_at DESC LIMIT 1",
                str(order_id),
            )
            provider = payment["provider"] if payment else "manual"
            provider_trade_no = payment["provider_trade_no"] if payment else None

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
                # 幂等键: 创建时即生成并落库 (唯一索引兜底), 崩溃重试也不会重复退款
                provider_refund_no=f"refund_{uuid.uuid4().hex[:16]}",
            )

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
                json.dumps(refund.metadata),
                refund.created_at,
                refund.updated_at,
                refund.processed_at,
            )

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
    """管理员审核通过 (管理员端调用)。通过后自动触发网关退款。"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            refund = await _load_refund_locked(conn, refund_id)

            if refund.status != RefundStatus.SUBMITTED:
                raise RefundError(f"refund not in submitted status: {refund.status.value}")

            # 确定审核通过金额 (默认全额)
            final_amount = (
                approved_amount if approved_amount is not None else refund.requested_amount
            )
            if final_amount <= 0:
                raise RefundAmountExceedsError("approved amount must be positive")
            if final_amount > refund.requested_amount:
                raise RefundAmountExceedsError("approved amount cannot exceed requested amount")

            # 行锁内重算累计额度: 别的售后可能已经占了额度
            paid = int(
                await conn.fetchval(
                    "SELECT COALESCE(SUM(amount_cents), 0) FROM payment_intent "
                    "WHERE order_id = $1 AND status = 'paid'",
                    refund.order_id,
                )
                or 0
            )
            reserved = int(
                await conn.fetchval(
                    """
                    SELECT COALESCE(SUM(
                        CASE WHEN status = 'succeeded' THEN actual_refunded_amount
                             ELSE COALESCE(approved_amount, requested_amount) END), 0)
                    FROM refund_request
                    WHERE order_id = $1
                      AND status IN ('submitted', 'approved', 'refunding', 'succeeded')
                      AND id <> $2
                    """,
                    refund.order_id,
                    refund.id,
                )
                or 0
            )
            if reserved + final_amount > paid:
                raise RefundAmountExceedsError(
                    f"approved amount {final_amount} exceeds refundable "
                    f"{paid - reserved} (paid={paid}, reserved={reserved})"
                )

            old_status = refund.status
            refund.transition_to(RefundStatus.APPROVED)
            refund.approved_amount = final_amount
            refund.reviewer_id = reviewer_id
            refund.updated_at = datetime.now(UTC)

            await _update_refund_locked(conn, refund)

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

    # 审核通过后自动触发退款 (返回执行后的最终状态: succeeded/failed)
    return await execute_refund(pool, refund_id)


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
    refund.updated_at = datetime.now(UTC)

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

    事务 + 行锁保证并发安全: 两个并发退款只有一笔能进入 refunding,
    另一笔看到状态已变直接报错; 累计金额在锁内重算, 不超过实付。
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            refund = await _load_refund_locked(conn, refund_id)

            if refund.status not in (RefundStatus.APPROVED, RefundStatus.FAILED):
                # 并发重入 (双击/重试) 或已退款 → 幂等拒绝
                raise RefundError(f"refund not in approved/failed status: {refund.status.value}")

            if not refund.approved_amount:
                raise RefundError("approved amount not set")

            # 累计退款上限 (行锁保护下重算)
            paid = int(
                await conn.fetchval(
                    "SELECT COALESCE(SUM(amount_cents), 0) FROM payment_intent "
                    "WHERE order_id = $1 AND status = 'paid'",
                    refund.order_id,
                )
                or 0
            )
            already = int(
                await conn.fetchval(
                    """
                    SELECT COALESCE(SUM(
                        CASE WHEN status = 'succeeded' THEN actual_refunded_amount
                             ELSE COALESCE(approved_amount, requested_amount) END), 0)
                    FROM refund_request
                    WHERE order_id = $1
                      AND status IN ('submitted', 'approved', 'refunding', 'succeeded')
                      AND id <> $2
                    """,
                    refund.order_id,
                    refund.id,
                )
                or 0
            )
            if already + refund.approved_amount > paid:
                raise RefundAmountExceedsError(
                    f"refund {refund.approved_amount} exceeds refundable "
                    f"{paid - already} (paid={paid}, already_refunded={already})"
                )

            old_status = refund.status
            refund.transition_to(RefundStatus.REFUNDING)
            refund.updated_at = datetime.now(UTC)
            await _update_refund_locked(conn, refund)

    # 进入 refunding 的事实入审计 (事务已提交)
    await _append_audit_log(
        pool,
        RefundAuditLog(
            refund_id=refund.id,
            from_status=old_status,
            to_status=RefundStatus.REFUNDING,
            operator_type="system",
            reason="Gateway refund initiated",
        ),
    )

    # 事务外调网关 (网络 IO 不占着行锁); 失败在下方落 failed
    try:
        if refund.provider == "manual":
            # 人工收款没有可调用的退款 API。绝不能把内存桩的 success 当成
            # 已退款；保持 refunding，等待管理员线下转账后显式确认。
            refund.metadata["manual_confirmation_required"] = True
            refund.metadata["manual_confirmation_status"] = "pending"
            refund.updated_at = datetime.now(UTC)
            await _save_refund_request(pool, refund)
            await _append_audit_log(
                pool,
                RefundAuditLog(
                    refund_id=refund.id,
                    from_status=RefundStatus.REFUNDING,
                    to_status=RefundStatus.REFUNDING,
                    operator_type="system",
                    reason="Manual refund requires offline transfer and admin confirmation",
                ),
            )
            return refund

        settings = Settings()
        _, gateway = build_payment_gateway(settings)

        result = await gateway.refund(
            out_trade_no=refund.provider_trade_no or refund.order_id,
            out_refund_no=refund.provider_refund_no,
            refund_fee_cents=refund.approved_amount,
            reason=refund.reason_detail or refund.reason.value,
            total_fee_cents=paid,
        )

        if result.get("status") in ("success", "succeeded", "completed", "SUCCESS"):
            refund.transition_to(RefundStatus.SUCCEEDED)
            refund.actual_refunded_amount = refund.approved_amount
            refund.provider_refund_no = result.get("refund_id") or refund.provider_refund_no
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
        refund.updated_at = datetime.now(UTC)
        await _save_refund_request(pool, refund)
        await _append_audit_log(
            pool,
            RefundAuditLog(
                refund_id=refund.id,
                from_status=RefundStatus.REFUNDING,
                to_status=refund.status,
                operator_type="system",
                reason=f"Gateway refund result: {refund.status.value}",
            ),
        )

    if refund.status == RefundStatus.SUCCEEDED:
        await _reverse_voucher_for_refund(pool, refund)

    return refund


async def _load_refund_locked(conn, key: str, field: str = "id") -> RefundRequest:
    """在事务内加行锁读取退款单 (并发退款的串行化点)。"""
    row = await conn.fetchrow(
        f"SELECT * FROM refund_request WHERE {field} = $1 FOR UPDATE",
        key,
    )
    if not row:
        raise RefundError("refund request not found")
    return RefundRequest.from_row(row)


async def _update_refund_locked(conn, refund: RefundRequest) -> None:
    """事务内更新退款单 (调用方持有行锁)。"""
    await conn.execute(
        """
        UPDATE refund_request SET
            status = $2, approved_amount = $3, actual_refunded_amount = $4,
            provider_refund_no = $5, return_tracking_no = $6, return_batch_id = $7,
            reviewer_id = $8, reject_reason = $9, metadata = $10,
            updated_at = $11, processed_at = $12
        WHERE id = $1
        """,
        refund.id,
        refund.status.value,
        refund.approved_amount,
        refund.actual_refunded_amount,
        refund.provider_refund_no,
        refund.return_tracking_no,
        refund.return_batch_id,
        refund.reviewer_id,
        refund.reject_reason,
        json.dumps(refund.metadata),
        refund.updated_at,
        refund.processed_at,
    )


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
    operator_id: str,
    quantity: int | None = None,
) -> RefundRequest:
    """确认收到退货 (管理员端调用)。

    批次从原订单行自动解析，管理员不再手填 batch_id。只有这一刻才回补库存
    (货真到了仓); 幂等 —— 同一退款单只回补一次,
    重复收货/回调重放都不会双倍回补 (metadata.restocked 标记 + 审计日志)。
    """
    refund = await _get_refund_request(pool, refund_id)
    if not refund:
        raise RefundError("refund request not found")

    if refund.type != RefundType.RETURN_REFUND:
        raise RefundError("not a return_refund type")

    if refund.status not in (RefundStatus.APPROVED, RefundStatus.REFUNDING, RefundStatus.SUCCEEDED):
        raise RefundError(f"invalid status for return confirmation: {refund.status.value}")
    if not refund.order_line_id:
        raise RefundError("return refund is not linked to an order line")
    if quantity is not None and quantity <= 0:
        raise RefundError("return quantity must be positive")

    from_status = refund.status
    refund.return_tracking_no = tracking_no
    refund.updated_at = datetime.now(UTC)

    # 回补库存 (幂等): 未回补过才补
    restocked = False
    if not refund.metadata.get("restocked"):
        batch_id, restocked_qty = await _restock_inventory(pool, refund, quantity=quantity)
        refund.return_batch_id = batch_id
        refund.metadata["restocked_qty"] = restocked_qty
        refund.metadata["restocked"] = True
        restocked = True

    await _save_refund_request(pool, refund)
    await _append_audit_log(
        pool,
        RefundAuditLog(
            refund_id=refund.id,
            from_status=from_status,
            to_status=refund.status,
            operator_id=operator_id,
            operator_type="admin",
            reason=(
                f"Return received, tracking: {tracking_no}, "
                f"restocked {refund.metadata.get('restocked_qty')} to batch: "
                f"{refund.return_batch_id}"
                if restocked
                else f"Return received again (already restocked), tracking: {tracking_no}"
            ),
            metadata={
                "batch_id": refund.return_batch_id,
                "restocked_qty": refund.metadata.get("restocked_qty", 0),
                "restocked": restocked,
            },
        ),
    )

    return refund


async def _restock_inventory(
    pool: PgPool,
    refund: RefundRequest,
    *,
    quantity: int | None = None,
) -> tuple[str, int]:
    """按退款单关联订单行的真实 batch 和数量回补库存。"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            line = await conn.fetchrow(
                """
                SELECT oli.id, oli.batch_id, oli.quantity, oli.fulfilled_qty,
                       ib.variant_id, ib.location_id, pv.product_id
                FROM order_line_item oli
                JOIN inventory_batch ib ON ib.id = oli.batch_id
                JOIN product_variant pv ON pv.id = ib.variant_id
                WHERE oli.id = $1 AND oli.order_id = $2
                """,
                refund.order_line_id,
                refund.order_id,
            )
            if line is None or line["batch_id"] is None:
                raise RefundError("original order line or inventory batch not found")

            line_qty = int(line["quantity"] or 0)
            restock_qty = int(quantity) if quantity is not None else line_qty
            if restock_qty <= 0 or restock_qty > line_qty:
                raise RefundError(
                    f"return quantity {restock_qty} must be between 1 and "
                    f"original quantity {line_qty}"
                )

            # stock_movement 的幂等键是第二道防线：即使退款单 metadata 写入
            # 前进程崩溃，重试也不会再次加库存。
            movement_id = await conn.fetchval(
                """
                INSERT INTO stock_movement (
                    product_id, variant_id, location_id, batch_id,
                    movement_type, quantity, reference_type, reference_id,
                    reason, operator_id, idempotency_key
                ) VALUES ($1,$2,$3,$4,'return',$5,'refund',$6,$7,$8,$9)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id
                """,
                str(line["product_id"]),
                str(line["variant_id"]),
                line["location_id"],
                line["batch_id"],
                restock_qty,
                refund.id,
                f"return received for refund {refund.id}",
                refund.reviewer_id,
                f"refund:{refund.id}:restock",
            )
            if movement_id is None:
                return str(line["batch_id"]), 0

            updated = await conn.fetchrow(
                """
                UPDATE inventory_batch
                SET stock_qty = stock_qty + $2,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                line["batch_id"],
                restock_qty,
            )
            if updated is None:
                raise RefundError("inventory batch not found")
    logger.info(
        "inventory restocked for refund %s: batch %s qty=%s",
        refund.id,
        line["batch_id"],
        restock_qty,
    )
    return str(line["batch_id"]), restock_qty


async def confirm_manual_refund(
    pool: PgPool,
    refund_id: str,
    operator_id: str,
) -> RefundRequest:
    """管理员确认线下退款已实际完成；只有这一刻 manual 退款才变 succeeded。"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            refund = await _load_refund_locked(conn, refund_id)
            if refund.provider != "manual":
                raise RefundError("not a manual refund")
            if refund.status != RefundStatus.REFUNDING:
                raise RefundError(f"manual refund is not pending: {refund.status.value}")
            if not refund.metadata.get("manual_confirmation_required"):
                raise RefundError("refund is not awaiting manual confirmation")
            if not refund.approved_amount:
                raise RefundError("approved amount not set")

            old_status = refund.status
            refund.transition_to(RefundStatus.SUCCEEDED)
            refund.actual_refunded_amount = refund.approved_amount
            refund.metadata["manual_confirmation_status"] = "confirmed"
            refund.metadata["manual_confirmed_by"] = operator_id
            refund.metadata["manual_confirmed_at"] = datetime.now(UTC).isoformat()
            refund.updated_at = datetime.now(UTC)
            await _update_refund_locked(conn, refund)

    await _append_audit_log(
        pool,
        RefundAuditLog(
            refund_id=refund.id,
            from_status=old_status,
            to_status=RefundStatus.SUCCEEDED,
            operator_id=operator_id,
            operator_type="admin",
            reason="Manual offline refund confirmed by admin",
        ),
    )
    await _reverse_voucher_for_refund(pool, refund)
    return refund


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

    return [RefundRequest.from_row(row) for row in rows]


async def get_refund_audit_logs(pool: PgPool, refund_id: str) -> list[RefundAuditLog]:
    """获取售后审计日志。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM refund_audit_log WHERE refund_id = $1 ORDER BY created_at ASC",
            refund_id,
        )
    return [RefundAuditLog.from_row(row) for row in rows]


# ── 网关退款回调 (验签 → 匹配 → 推进状态机) ─────────────────────────────


def _normalize_refund_callback(provider: str, verified: dict[str, Any]) -> tuple[str | None, str]:
    """从验签后的回调负载提取 (本地退款单号 provider_refund_no, 退款状态)。

    状态归一: SUCCESS / FAILED (其余视为 UNKNOWN, 不改状态只记审计)。
    """
    if provider == "wechat":
        # v3 退款回调解密后的 resource 明文
        out_refund_no = verified.get("out_refund_no")
        raw = str(verified.get("refund_status") or "").upper()
        return out_refund_no, "SUCCESS" if raw == "SUCCESS" else "FAILED" if raw in (
            "CLOSED",
            "FAILED",
        ) else "UNKNOWN"
    if provider == "alipay":
        # 退款异步通知 (form 参数): out_request_no = 我们的 provider_refund_no
        out_refund_no = verified.get("out_request_no")
        if str(verified.get("code") or "") == "10000":
            return out_refund_no, "SUCCESS"
        raw = str(verified.get("refund_status") or "").upper()
        return out_refund_no, "FAILED" if raw in ("FAIL", "REFUNDFAIL") else "UNKNOWN"
    if provider == "stripe":
        # charge.refunded 事件: metadata.out_refund_no
        obj = (verified.get("data") or {}).get("object") or {}
        out_refund_no = (obj.get("metadata") or {}).get("out_refund_no")
        if verified.get("type") == "charge.refunded":
            return out_refund_no, "SUCCESS"
        return out_refund_no, "UNKNOWN"
    return None, "UNKNOWN"


async def process_refund_callback(
    pool: PgPool,
    *,
    provider: str,
    headers: dict[str, str],
    body: bytes,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """处理网关退款回调: 验签 → 按 provider_refund_no 匹配 → 推进状态机。

    伪造签名 (verify_callback 返回 None) 直接拒绝; 已处理过的回调幂等
    (状态机只接受 refunding → succeeded/failed)。
    """
    settings = settings or Settings()
    _, gateway = build_payment_gateway(settings)

    verified = await gateway.verify_callback(headers, body)
    if not verified:
        raise GatewayRefundError("invalid refund callback signature")

    out_refund_no, callback_status = _normalize_refund_callback(provider, verified)
    if not out_refund_no:
        raise GatewayRefundError("refund callback missing out_refund_no")
    if callback_status == "UNKNOWN":
        # 认不出的事件类型 (如测试/其他事件): 不动状态, 记账即可
        logger.info("refund callback ignored (unknown status): %s", out_refund_no)
        return {"status": "ignored", "out_refund_no": out_refund_no}

    async with pool.acquire() as conn:
        async with conn.transaction():
            refund = await _load_refund_locked(conn, str(out_refund_no), field="provider_refund_no")

            target = RefundStatus.SUCCEEDED if callback_status == "SUCCESS" else RefundStatus.FAILED

            if refund.status == target:
                # 幂等: 回调重放, 状态没变
                logger.info(
                    "refund callback duplicate (already %s): %s",
                    target.value,
                    out_refund_no,
                )
                return {"status": "duplicate", "refund_id": refund.id}

            if refund.status != RefundStatus.REFUNDING:
                # 状态机只接受 refunding → succeeded/failed; 其他状态不动
                logger.warning(
                    "refund callback out of order: %s is %s, skipping %s",
                    out_refund_no,
                    refund.status.value,
                    target.value,
                )
                return {"status": "out_of_order", "refund_id": refund.id}

            from_status = refund.status
            refund.transition_to(target)
            if target == RefundStatus.SUCCEEDED:
                # 回调可能早于 approve 落 approved_amount 之外的字段, 兜底用请求金额
                refund.actual_refunded_amount = (
                    refund.approved_amount
                    if refund.approved_amount is not None
                    else refund.requested_amount
                )
            else:
                refund.metadata["gateway_callback"] = callback_status
            await _update_refund_locked(conn, refund)

    await _append_audit_log(
        pool,
        RefundAuditLog(
            refund_id=refund.id,
            from_status=from_status,
            to_status=target,
            operator_type="system",
            reason=f"Gateway refund callback: {callback_status}",
            metadata={"provider": provider, "callback_status": callback_status},
        ),
    )

    if target == RefundStatus.SUCCEEDED:
        await _reverse_voucher_for_refund(pool, refund)

    # 退货退款: 回调成功后若已登记收货批次且未回补, 由 mark_refund_returned 负责
    return {"status": "processed", "refund_id": refund.id, "to": target.value}
