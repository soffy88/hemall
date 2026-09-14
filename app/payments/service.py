"""支付权威、outbox 和退款幂等服务。

支付的正确顺序是：

    DB intent + outbox -> provider (固定幂等键) -> DB result

第三方调用和本地数据库不可能共享同一个事务，所以 provider 成功后 DB
短暂故障必须由 outbox/reconciliation 重试；任何金额都只从订单快照读取。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .models import PaymentProvider, PaymentStatus

logger = logging.getLogger("hemall.payments.service")


class PaymentServiceError(Exception):
    """可安全返回给 API 的支付业务错误。"""


class PaymentNotFoundError(PaymentServiceError):
    pass


class PaymentAmountMismatchError(PaymentServiceError):
    pass


class PaymentStateError(PaymentServiceError):
    pass


class RefundConflictError(PaymentServiceError):
    pass


@dataclass(frozen=True)
class PaymentIntentResult:
    intent_id: str
    order_id: str
    amount_cents: int
    currency: str
    provider: str
    status: str
    provider_result: dict[str, Any] | None = None


def yuan_to_cents(value: Decimal | str | int | float) -> int:
    """严格把元转换成分，拒绝超过两位小数和非正金额。"""

    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PaymentAmountMismatchError("invalid monetary amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise PaymentAmountMismatchError("amount must be positive")
    rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded != amount:
        raise PaymentAmountMismatchError("amount must have at most two decimals")
    return int(rounded * 100)


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


class PaymentService:
    """所有支付写入的单一入口。"""

    def __init__(self, pool: Any, settings: Any) -> None:
        self._pool = pool
        self._settings = settings

    async def create_intent(
        self,
        *,
        order_id: str,
        provider: str,
        requested_amount: Decimal | None = None,
        description: str = "",
        payer_openid: str | None = None,
        idempotency_key: str | None = None,
    ) -> PaymentIntentResult:
        """从 customer_order 读取权威金额并写入 intent/outbox。

        requested_amount 只用于兼容旧客户端的篡改检测，绝不参与计算。
        """

        provider = str(provider)
        if provider not in {p.value for p in PaymentProvider}:
            raise PaymentServiceError(f"unsupported payment provider: {provider}")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                order = await conn.fetchrow(
                    """
                    SELECT id, status, grand_total_cents, currency
                    FROM customer_order
                    WHERE id = $1
                    FOR UPDATE
                    """,
                    order_id,
                )
                if order is None:
                    raise PaymentNotFoundError(f"order not found: {order_id}")

                amount_cents = int(order["grand_total_cents"] or 0)
                currency = str(order["currency"] or "CNY").upper()
                if amount_cents <= 0:
                    raise PaymentAmountMismatchError("order total must be positive")
                if (
                    provider
                    in {
                        PaymentProvider.WECHAT.value,
                        PaymentProvider.ALIPAY.value,
                    }
                    and currency != "CNY"
                ):
                    raise PaymentServiceError(f"{provider} only supports CNY payment intents")
                if requested_amount is not None:
                    supplied_cents = yuan_to_cents(requested_amount)
                    if supplied_cents != amount_cents:
                        raise PaymentAmountMismatchError(
                            "payment amount is server controlled and does not match order total"
                        )

                # A customer may retry the same order/provider. Reuse the
                # existing intent instead of creating a second payable amount.
                intent = await conn.fetchrow(
                    """
                    SELECT id, order_id, amount_cents, currency, provider, status,
                           metadata
                    FROM payment_intent
                    WHERE order_id = $1 AND provider = $2
                    FOR UPDATE
                    """,
                    str(order["id"]),
                    provider,
                )
                if intent is None:
                    intent_id = uuid.uuid4()
                    await conn.execute(
                        """
                        INSERT INTO payment_intent
                            (id, order_id, amount_cents, currency, provider, status, metadata)
                        VALUES ($1, $2, $3, $4, $5, 'pending', $6::jsonb)
                        """,
                        intent_id,
                        str(order["id"]),
                        amount_cents,
                        currency,
                        provider,
                        _json(
                            {
                                "description": description or f"order-{order_id}",
                                "payer_openid": payer_openid,
                                "idempotency_key": idempotency_key,
                            }
                        ),
                    )
                    intent = await conn.fetchrow(
                        """
                        SELECT id, order_id, amount_cents, currency, provider, status,
                               metadata
                        FROM payment_intent WHERE id = $1 FOR UPDATE
                        """,
                        intent_id,
                    )
                else:
                    if (
                        int(intent["amount_cents"]) != amount_cents
                        or str(intent["currency"]).upper() != currency
                    ):
                        raise PaymentAmountMismatchError(
                            "persisted payment intent no longer matches order total"
                        )
                    intent_id = intent["id"]

                if intent["status"] not in {
                    PaymentStatus.PAID.value,
                    PaymentStatus.REFUNDED.value,
                    PaymentStatus.PARTIAL_REFUND.value,
                }:
                    await conn.execute(
                        """
                        INSERT INTO payment_outbox
                            (payment_intent_id, kind, idempotency_key, payload)
                        VALUES ($1, 'prepay', $2, $3::jsonb)
                        ON CONFLICT (idempotency_key) DO UPDATE
                        SET status = CASE
                                WHEN payment_outbox.status = 'failed' THEN 'pending'
                                ELSE payment_outbox.status
                            END,
                            next_attempt_at = CASE
                                WHEN payment_outbox.status = 'failed' THEN NOW()
                                ELSE payment_outbox.next_attempt_at
                            END,
                            updated_at = NOW()
                        """,
                        intent_id,
                        f"prepay:{intent_id}",
                        _json(
                            {
                                "description": description or f"order-{order_id}",
                                "payer_openid": payer_openid,
                            }
                        ),
                    )

                # The intent is the order's sole payment authority. Link it
                # before calling the provider so every later callback/query
                # resolves the same durable intent.
                await conn.execute(
                    """
                    UPDATE customer_order
                    SET payment_provider_name = $1,
                        payment_intent_id = $2,
                        updated_at = NOW()
                    WHERE id = $3
                    """,
                    provider,
                    str(intent_id),
                    order["id"],
                )

        return await self._read_intent(str(intent_id))

    async def process_intent(self, intent_id: str) -> PaymentIntentResult:
        """执行一次 prepay outbox；失败只留下可重试的 durable 状态。"""

        job = await self._claim_outbox(intent_id=intent_id, kind="prepay")
        if job is None:
            return await self._read_intent(intent_id)

        intent = await self._read_intent(intent_id)
        payload = job.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        try:
            result = await self._provider_prepay(
                provider=intent.provider,
                out_trade_no=intent.intent_id,
                total_fee_cents=intent.amount_cents,
                currency=intent.currency,
                description=str(payload.get("description") or f"order-{intent.order_id}"),
                payer_openid=payload.get("payer_openid"),
            )
        except Exception as exc:  # noqa: BLE001 - outbox must remain retryable
            await self._mark_outbox_failed(job["id"], str(exc))
            logger.warning("payment prepay failed intent=%s: %s", intent_id, exc)
            return await self._read_intent(intent_id)

        # If this transaction fails after the provider accepted the fixed
        # idempotency key, the outbox remains processing and reconciliation will
        # call the same provider operation again rather than creating a new key.
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE payment_intent
                        SET status = CASE
                                WHEN status = 'paid' THEN 'paid'
                                ELSE 'provider_pending'
                            END,
                            provider_intent_id = COALESCE($1, provider_intent_id),
                            metadata = metadata || $2::jsonb,
                            last_error = NULL,
                            version = version + 1,
                            updated_at = NOW()
                        WHERE id = $3
                        """,
                        result.get("payment_intent_id") or result.get("prepay_id"),
                        _json({"provider_result": result}),
                        intent_id,
                    )
                    await conn.execute(
                        """
                        UPDATE payment_outbox
                        SET status = 'completed', locked_at = NULL, updated_at = NOW()
                        WHERE id = $1
                        """,
                        job["id"],
                    )
        except Exception:  # noqa: BLE001 - leave processing row for recovery
            logger.exception(
                "provider succeeded but intent result could not be persisted: %s", intent_id
            )
        return await self._read_intent(intent_id)

    async def request_refund(
        self,
        *,
        payment_id: str,
        refund_amount: Decimal | None,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """建立幂等退款 intent；同一个请求永远复用同一 refund row。"""

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                intent = await conn.fetchrow(
                    """
                    SELECT * FROM payment_intent WHERE id = $1 FOR UPDATE
                    """,
                    payment_id,
                )
                if intent is None:
                    raise PaymentNotFoundError(f"payment not found: {payment_id}")
                if intent["status"] not in {
                    PaymentStatus.PAID.value,
                    PaymentStatus.PARTIAL_REFUND.value,
                }:
                    raise PaymentStateError(f"cannot refund payment in state {intent['status']}")

                # The default key is stable across retries. In particular,
                # ``refund_amount=None`` must not derive from a balance that
                # changes after the first pending refund is inserted.
                requested_cents = (
                    yuan_to_cents(refund_amount) if refund_amount is not None else None
                )
                key = idempotency_key or (
                    f"refund:{payment_id}:full"
                    if requested_cents is None
                    else f"refund:{payment_id}:{requested_cents}"
                )
                refund = await conn.fetchrow(
                    """
                    SELECT * FROM payment_refund
                    WHERE payment_intent_id = $1 AND idempotency_key = $2
                    FOR UPDATE
                    """,
                    intent["id"],
                    key,
                )
                if refund is not None:
                    amount_cents = int(refund["amount_cents"])
                    if requested_cents is not None and requested_cents != amount_cents:
                        raise RefundConflictError("idempotency key reused with another amount")
                    if refund["status"] == "succeeded":
                        return self._refund_result(refund)
                    refund_id = refund["id"]
                    await conn.execute(
                        "UPDATE payment_refund SET status = 'pending', last_error = NULL, "
                        "updated_at = NOW() WHERE id = $1 AND status = 'failed'",
                        refund_id,
                    )
                else:
                    original = int(intent["amount_cents"])
                    already = int(
                        await conn.fetchval(
                            """
                            SELECT COALESCE(SUM(amount_cents), 0)
                            FROM payment_refund
                            WHERE payment_intent_id = $1
                              AND status IN ('pending', 'processing', 'succeeded')
                            """,
                            intent["id"],
                        )
                        or 0
                    )
                    amount_cents = (
                        original - already if requested_cents is None else requested_cents
                    )
                    if amount_cents <= 0 or amount_cents > original - already:
                        raise RefundConflictError("refund amount exceeds refundable balance")
                    refund_id = uuid.uuid4()
                    await conn.execute(
                        """
                        INSERT INTO payment_refund
                            (id, payment_intent_id, idempotency_key, amount_cents, reason)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        refund_id,
                        intent["id"],
                        key,
                        amount_cents,
                        reason,
                    )

                outbox_key = f"refund:{refund_id}"
                await conn.execute(
                    """
                    INSERT INTO payment_outbox
                        (payment_intent_id, refund_id, kind, idempotency_key, payload)
                    VALUES ($1, $2, 'refund', $3, $4::jsonb)
                    ON CONFLICT (idempotency_key) DO UPDATE
                    SET status = CASE
                            WHEN payment_outbox.status = 'failed' THEN 'pending'
                            ELSE payment_outbox.status
                        END,
                        next_attempt_at = CASE
                            WHEN payment_outbox.status = 'failed' THEN NOW()
                            ELSE payment_outbox.next_attempt_at
                        END,
                        updated_at = NOW()
                    """,
                    intent["id"],
                    refund_id,
                    outbox_key,
                    _json({"reason": reason}),
                )

        result = await self.process_refund(str(refund_id))
        return result

    async def process_refund(self, refund_id: str) -> dict[str, Any]:
        job = await self._claim_outbox(refund_id=refund_id, kind="refund")
        if job is None:
            return await self._read_refund(refund_id)

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT r.*, p.provider, p.order_id, p.provider_trade_no,
                       p.provider_intent_id AS original_provider_intent_id,
                       p.amount_cents AS original_amount_cents
                FROM payment_refund r
                JOIN payment_intent p ON p.id = r.payment_intent_id
                WHERE r.id = $1
                """,
                refund_id,
            )
        if row is None:
            raise PaymentNotFoundError(f"refund not found: {refund_id}")

        refund_no = f"refund_{refund_id.replace('-', '')}"
        try:
            provider_result = await self._provider_refund(
                provider=row["provider"],
                out_trade_no=str(row["payment_intent_id"]),
                provider_intent_id=row["original_provider_intent_id"],
                out_refund_no=refund_no,
                refund_fee_cents=int(row["amount_cents"]),
                reason=str(row["reason"] or ""),
                total_fee_cents=int(row["original_amount_cents"]),
            )
        except Exception as exc:  # noqa: BLE001
            await self._mark_refund_failed(job["id"], refund_id, str(exc))
            logger.warning("payment refund failed refund=%s: %s", refund_id, exc)
            return await self._read_refund(refund_id)

        provider_status = str(provider_result.get("status", "")).lower()
        if provider_status in {"pending", "processing", "requires_action"}:
            # The provider accepted the idempotent refund request but has not
            # settled it yet. Keep the local state non-terminal and leave a
            # retryable outbox row; a later attempt uses the same refund key.
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE payment_refund
                        SET status = 'processing',
                            provider_refund_no = COALESCE($1, provider_refund_no),
                            provider_result = $2::jsonb, last_error = NULL, updated_at = NOW()
                        WHERE id = $3
                        """,
                        provider_result.get("refund_id") or provider_result.get("id"),
                        _json(provider_result),
                        refund_id,
                    )
                    await conn.execute(
                        """
                        UPDATE payment_outbox
                        SET status = 'pending', locked_at = NULL,
                            next_attempt_at = NOW() + INTERVAL '5 seconds', updated_at = NOW()
                        WHERE id = $1
                        """,
                        job["id"],
                    )
            return await self._read_refund(refund_id)

        if provider_status in {"failed", "canceled", "cancelled", "closed", "error"}:
            await self._mark_refund_failed(job["id"], refund_id, "payment provider refund failed")
            return await self._read_refund(refund_id)
        if provider_status not in {"", "success", "succeeded", "finished", "completed"}:
            await self._mark_refund_failed(
                job["id"], refund_id, f"unknown payment refund status: {provider_status}"
            )
            return await self._read_refund(refund_id)

        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE payment_refund
                        SET status = 'succeeded', provider_refund_no = $1,
                            provider_result = $2::jsonb, last_error = NULL,
                            version = version + 1, updated_at = NOW()
                        WHERE id = $3
                        """,
                        refund_no,
                        _json(provider_result),
                        refund_id,
                    )
                    refunded = await conn.fetchval(
                        """
                        SELECT COALESCE(SUM(amount_cents), 0)
                        FROM payment_refund
                        WHERE payment_intent_id = (
                            SELECT payment_intent_id FROM payment_refund WHERE id = $1
                        )
                          AND status = 'succeeded'
                        """,
                        refund_id,
                    )
                    await conn.execute(
                        """
                        UPDATE payment_intent
                        SET refund_amount_cents = $1,
                            status = CASE WHEN $1 >= amount_cents THEN 'refunded'
                                          ELSE 'partial_refund' END,
                            version = version + 1, updated_at = NOW()
                        WHERE id = (SELECT payment_intent_id FROM payment_refund WHERE id = $2)
                        """,
                        int(refunded or 0),
                        refund_id,
                    )
                    await conn.execute(
                        "UPDATE payment_outbox SET status = 'completed', locked_at = NULL, "
                        "updated_at = NOW() WHERE id = $1",
                        job["id"],
                    )
        except Exception:  # noqa: BLE001
            logger.exception(
                "provider refund succeeded but result could not be persisted: %s", refund_id
            )
        return await self._read_refund(refund_id)

    async def reconcile(self, *, limit: int = 100) -> int:
        """重置卡死 job 并重试，供启动任务和运维命令调用。"""

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE payment_outbox
                SET status = 'pending', locked_at = NULL, next_attempt_at = NOW(),
                    updated_at = NOW()
                WHERE status = 'processing' AND locked_at < NOW() - INTERVAL '5 minutes'
                """
            )
            rows = await conn.fetch(
                """
                SELECT id, payment_intent_id, refund_id, kind
                FROM payment_outbox
                WHERE status IN ('pending', 'failed') AND next_attempt_at <= NOW()
                ORDER BY created_at
                LIMIT $1
                """,
                limit,
            )
        processed = 0
        for row in rows:
            if row["kind"] == "prepay":
                await self.process_intent(str(row["payment_intent_id"]))
            elif row["kind"] == "refund" and row["refund_id"]:
                await self.process_refund(str(row["refund_id"]))
            processed += 1
        return processed

    async def _read_intent(self, intent_id: str) -> PaymentIntentResult:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, order_id, amount_cents, currency, provider, status, metadata "
                "FROM payment_intent WHERE id = $1",
                intent_id,
            )
        if row is None:
            raise PaymentNotFoundError(f"payment not found: {intent_id}")
        metadata = row["metadata"] or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return PaymentIntentResult(
            intent_id=str(row["id"]),
            order_id=str(row["order_id"]),
            amount_cents=int(row["amount_cents"]),
            currency=str(row["currency"]),
            provider=str(row["provider"]),
            status=str(row["status"]),
            provider_result=metadata.get("provider_result"),
        )

    async def _read_refund(self, refund_id: str) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM payment_refund WHERE id = $1", refund_id)
        if row is None:
            raise PaymentNotFoundError(f"refund not found: {refund_id}")
        return self._refund_result(row)

    @staticmethod
    def _refund_result(row: Any) -> dict[str, Any]:
        return {
            "refund_id": str(row["id"]),
            "payment_id": str(row["payment_intent_id"]),
            "status": str(row["status"]),
            "refund_amount_cents": int(row["amount_cents"]),
            "refund_amount": str(Decimal(int(row["amount_cents"])) / 100),
            "provider_refund_no": row.get("provider_refund_no") if hasattr(row, "get") else None,
            "provider_result": row.get("provider_result", {}) if hasattr(row, "get") else {},
        }

    async def _claim_outbox(
        self,
        *,
        intent_id: str | None = None,
        refund_id: str | None = None,
        kind: str,
    ) -> dict[str, Any] | None:
        condition = "payment_intent_id = $2" if intent_id else "refund_id = $2"
        value = intent_id or refund_id
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    UPDATE payment_outbox
                    SET status = 'processing', attempts = attempts + 1,
                        locked_at = NOW(), updated_at = NOW()
                    WHERE id = (
                        SELECT id FROM payment_outbox
                        WHERE {condition} AND kind = $1
                          AND status IN ('pending', 'failed')
                          AND next_attempt_at <= NOW()
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED LIMIT 1
                    )
                    RETURNING id, payload
                    """,
                    kind,
                    value,
                )
        return dict(row) if row is not None else None

    async def _mark_outbox_failed(self, job_id: Any, error: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE payment_outbox
                SET status = 'failed', locked_at = NULL, last_error = $1,
                    next_attempt_at = NOW() + LEAST(INTERVAL '1 hour',
                        INTERVAL '5 seconds' * POWER(2, LEAST(attempts, 8))),
                    updated_at = NOW()
                WHERE id = $2
                """,
                error[:2000],
                job_id,
            )

    async def _mark_refund_failed(self, job_id: Any, refund_id: str, error: str) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE payment_refund SET status = 'failed', last_error = $1, "
                    "updated_at = NOW() WHERE id = $2",
                    error[:2000],
                    refund_id,
                )
                await conn.execute(
                    "UPDATE payment_outbox SET status = 'failed', locked_at = NULL, "
                    "last_error = $1, next_attempt_at = NOW() + INTERVAL '5 seconds', "
                    "updated_at = NOW() WHERE id = $2",
                    error[:2000],
                    job_id,
                )

    async def _provider_prepay(
        self,
        *,
        provider: str,
        out_trade_no: str,
        total_fee_cents: int,
        currency: str,
        description: str,
        payer_openid: str | None,
    ) -> dict[str, Any]:
        gateway = self._gateway(provider)
        if provider == PaymentProvider.STRIPE.value:
            return await gateway.stripe_prepay(
                out_trade_no=out_trade_no,
                total_fee_cents=total_fee_cents,
                description=description,
                currency=currency,
            )
        if provider == PaymentProvider.ALIPAY.value:
            from .alipay import AlipayOrderRequest

            response = await gateway.create_order(
                AlipayOrderRequest(
                    out_trade_no=out_trade_no,
                    subject=description,
                    total_amount=str(Decimal(total_fee_cents) / 100),
                )
            )
            if not response.success:
                raise PaymentServiceError(response.err_msg or "alipay prepay failed")
            return {"qr_code": response.qr_code, "gateway": "alipay"}
        if hasattr(gateway, "prepay"):
            return await gateway.prepay(
                out_trade_no=out_trade_no,
                total_fee_cents=total_fee_cents,
                description=description,
                notify_url=self._settings.wechat_pay_notify_url,
            )
        if provider == PaymentProvider.WECHAT.value and hasattr(gateway, "create_order"):
            from .wechat import WeChatPayOrderRequest

            response = await gateway.create_order(
                WeChatPayOrderRequest(
                    out_trade_no=out_trade_no,
                    description=description,
                    total_amount=total_fee_cents,
                    payer_openid=payer_openid,
                )
            )
            if not response.success:
                raise PaymentServiceError(response.err_msg or "wechat prepay failed")
            return {
                "code_url": response.code_url,
                "prepay_id": response.prepay_id,
                "gateway": "wechat",
            }
        raise PaymentServiceError(f"provider {provider} does not support prepay")

    async def _provider_refund(
        self,
        *,
        provider: str,
        out_trade_no: str,
        provider_intent_id: str | None,
        out_refund_no: str,
        refund_fee_cents: int,
        reason: str,
        total_fee_cents: int,
    ) -> dict[str, Any]:
        gateway = self._gateway(provider)
        if provider == PaymentProvider.ALIPAY.value:
            result = await gateway.refund(
                out_trade_no=out_trade_no,
                refund_amount=str(Decimal(refund_fee_cents) / 100),
                out_request_no=out_refund_no,
            )
            if result.get("code") not in {None, "10000"}:
                raise PaymentServiceError(result.get("msg") or "alipay refund failed")
            return result
        if provider == PaymentProvider.STRIPE.value:
            result = await gateway.refund(
                out_trade_no=out_trade_no,
                out_refund_no=out_refund_no,
                refund_fee_cents=refund_fee_cents,
                reason=reason,
                total_fee_cents=total_fee_cents,
                payment_intent_id=provider_intent_id,
            )
            if result.get("status") in {"failed", "canceled"}:
                raise PaymentServiceError("stripe refund failed")
            return result
        try:
            result = await gateway.refund(
                out_trade_no=out_trade_no,
                out_refund_no=out_refund_no,
                refund_fee_cents=refund_fee_cents,
                reason=reason,
                total_fee_cents=total_fee_cents,
            )
            if str(result.get("status", "")).lower() in {
                "error",
                "failed",
                "closed",
            }:
                raise PaymentServiceError("payment provider refund failed")
            return result
        except TypeError:
            # Legacy sandbox WeChat provider uses refund_amount as its
            # parameter name; the production gateway uses refund_fee_cents.
            return await gateway.refund(
                out_trade_no=out_trade_no,
                out_refund_no=out_refund_no,
                refund_amount=refund_fee_cents,
            )

    def _gateway(self, provider: str) -> Any:
        try:
            from obase.provider_registry import ProviderRegistry

            return ProviderRegistry.get().generic("payment_gateway", provider)
        except Exception:
            # Unit tests and maintenance commands may construct the service
            # without running the full app lifespan. Keep the fallback explicit.
            if provider == PaymentProvider.WECHAT.value:
                from .wechat import WeChatPayProvider

                return WeChatPayProvider()
            if provider == PaymentProvider.ALIPAY.value:
                from .alipay import AlipayProvider

                return AlipayProvider()
            from app.ext.payout_provider import ManualPaymentGateway

            return ManualPaymentGateway()


async def reconcile_payments(pool: Any, settings: Any) -> int:
    """便于启动任务/CLI 调用的薄封装。"""

    return await PaymentService(pool, settings).reconcile()
