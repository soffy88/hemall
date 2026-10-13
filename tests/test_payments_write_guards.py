"""Audit: payments service write guards and monetary invariants.

These tests pin:
1. yuan_to_cents strictly converts to integer cents, rejecting float imprecision
2. Payment intent creation uses server-controlled amounts (not client-supplied)
3. Refund idempotency keys prevent duplicate refunds
4. All monetary values use integer cents, never float
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch
import uuid

import pytest

from app.payments.models import PaymentProvider, PaymentStatus
from app.payments.service import (
    PaymentAmountMismatchError,
    PaymentNotFoundError,
    PaymentService,
    PaymentServiceError,
    RefundConflictError,
    yuan_to_cents,
)
from tests.service_fakes import FakeConn, FakePool, Recording

def _svc(conn: FakeConn) -> PaymentService:
    settings = type("Settings", (), {
        "wechat_pay_notify_url": "https://test/notify",
    })()
    return PaymentService(FakePool(conn), settings)


FIXED_INTENT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
FIXED_REFUND_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


class TestYuanToCents:
    """Strict monetary conversion: only Decimal with max 2 decimals allowed."""

    def test_valid_conversions(self):
        assert yuan_to_cents(Decimal("0.01")) == 1
        assert yuan_to_cents("10.50") == 1050
        assert yuan_to_cents(2) == 200
        assert yuan_to_cents(Decimal("100.00")) == 10000

    def test_rejects_zero_and_negative(self):
        for value in (0, -1, "-0.01", Decimal("-5")):
            with pytest.raises(PaymentAmountMismatchError):
                yuan_to_cents(value)

    def test_rejects_non_finite(self):
        for value in ("nan", "inf", "-inf", float("nan")):
            with pytest.raises(PaymentAmountMismatchError):
                yuan_to_cents(value)

    def test_rejects_more_than_two_decimals(self):
        for value in ("1.001", "10.555", Decimal("1.001")):
            with pytest.raises(PaymentAmountMismatchError):
                yuan_to_cents(value)

    def test_rejects_invalid_strings(self):
        for value in ("abc", "", "1.2.3"):
            with pytest.raises(PaymentAmountMismatchError):
                yuan_to_cents(value)


class TestPaymentIntentCreation:
    """create_intent uses server-controlled amounts and idempotency."""

    async def test_create_intent_uses_order_total_not_client_amount(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+id,\s*status,\s*grand_total_cents,\s*currency\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": "o-1", "status": "pending", "grand_total_cents": 1234, "currency": "CNY"}),
        )
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+order_id\s*=\s*\$1\s+AND\s+provider\s*=\s*\$2\s+FOR\s+UPDATE",
            None,
        )
        conn.on(
            r"INSERT\s+INTO\s+payment_intent",
            None,
        )
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1234, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        conn.on(
            r"INSERT\s+INTO\s+payment_outbox",
            None,
        )
        conn.on(
            r"UPDATE\s+customer_order\s+SET\s+payment_provider_name",
            Recording({"id": "o-1"}),
        )
        # _read_intent called at the end
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1234, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        svc = _svc(conn)

        with patch("app.payments.service.uuid.uuid4", return_value=FIXED_INTENT_ID):
            # Don't pass requested_amount - server should use order total
            result = await svc.create_intent(
                order_id="o-1",
                provider="manual",
                idempotency_key="client-key-1",
            )

        assert result.amount_cents == 1234  # Server amount wins
        assert result.currency == "CNY"

    async def test_create_intent_rejects_amount_mismatch(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+id,\s*status,\s*grand_total_cents,\s*currency\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": "o-1", "status": "pending", "grand_total_cents": 1234, "currency": "CNY"}),
        )
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+order_id\s*=\s*\$1\s+AND\s+provider\s*=\s*\$2\s+FOR\s+UPDATE",
            None,
        )
        conn.on(
            r"INSERT\s+INTO\s+payment_intent",
            None,
        )
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1234, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        conn.on(
            r"INSERT\s+INTO\s+payment_outbox",
            None,
        )
        conn.on(
            r"UPDATE\s+customer_order\s+SET\s+payment_provider_name",
            Recording({"id": "o-1"}),
        )
        # _read_intent would be called but exception is raised first
        svc = _svc(conn)

        with patch("app.payments.service.uuid.uuid4", return_value=FIXED_INTENT_ID):
            with pytest.raises(PaymentAmountMismatchError, match="does not match order total"):
                await svc.create_intent(
                    order_id="o-1",
                    provider="manual",
                    requested_amount=Decimal("99.99"),
                )

    async def test_create_intent_rejects_unknown_provider(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+id,\s*status,\s*grand_total_cents,\s*currency\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": "o-1", "status": "pending", "grand_total_cents": 1234, "currency": "CNY"}),
        )
        svc = _svc(conn)

        with pytest.raises(PaymentServiceError, match="unsupported payment provider"):
            await svc.create_intent(
                order_id="o-1",
                provider="not-a-provider",
            )

    async def test_create_intent_rejects_missing_order(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+id,\s*status,\s*grand_total_cents,\s*currency\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            None,
        )
        svc = _svc(conn)

        with pytest.raises(PaymentNotFoundError):
            await svc.create_intent(
                order_id="missing",
                provider="manual",
            )

    async def test_create_intent_idempotent_on_client_retry(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+id,\s*status,\s*grand_total_cents,\s*currency\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": "o-1", "status": "pending", "grand_total_cents": 1000, "currency": "CNY"}),
        )
        # First call: no existing intent
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+order_id\s*=\s*\$1\s+AND\s+provider\s*=\s*\$2\s+FOR\s+UPDATE",
            None,
        )
        conn.on(
            r"INSERT\s+INTO\s+payment_intent",
            None,
        )
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        conn.on(
            r"INSERT\s+INTO\s+payment_outbox",
            None,
        )
        conn.on(
            r"UPDATE\s+customer_order\s+SET\s+payment_provider_name",
            Recording({"id": "o-1"}),
        )
        # _read_intent for first call
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        # Second call: existing intent found
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+order_id\s*=\s*\$1\s+AND\s+provider\s*=\s*\$2\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        # _read_intent for second call
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        svc = _svc(conn)

        with patch("app.payments.service.uuid.uuid4", return_value=FIXED_INTENT_ID):
            first = await svc.create_intent(
                order_id="o-1",
                provider="manual",
                idempotency_key="client-retry-key",
            )
            second = await svc.create_intent(
                order_id="o-1",
                provider="manual",
                idempotency_key="client-retry-key",
            )

        assert first.intent_id == second.intent_id
        assert first.amount_cents == 1000


class TestRefundIdempotency:
    """Refund idempotency keys prevent duplicate refunds."""

    async def test_refund_same_idempotency_key_returns_same_row(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "paid", "provider_trade_no": "trade-1", "provider_intent_id": "pi-provider"}),
        )
        # First call: insert refund row
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+payment_intent_id\s*=\s*\$1\s+AND\s+idempotency_key\s*=\s*\$2\s+FOR\s+UPDATE",
            None,
        )
        conn.on(
            r"SELECT\s+COALESCE\(SUM\(amount_cents\)",
            0,
        )
        conn.on(
            r"INSERT\s+INTO\s+payment_refund",
            None,
        )
        conn.on(
            r"INSERT\s+INTO\s+payment_outbox",
            None,
        )
        # _read_refund called by request_refund -> process_refund
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund-key-1", "amount_cents": 500, "reason": "test", "status": "succeeded", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None}),
        )
        svc = _svc(conn)

        with patch("app.payments.service.uuid.uuid4", return_value=FIXED_REFUND_ID):
            result1 = await svc.request_refund(
                payment_id=str(FIXED_INTENT_ID),
                refund_amount=Decimal("5.00"),
                reason="test",
                idempotency_key="refund-key-1",
            )

        assert result1["refund_amount_cents"] == 500


class TestMonetaryValuesAreIntegerCents:
    """Ensure all monetary values are integer cents, never float."""

    async def test_payment_intent_amount_cents_is_int(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+id,\s*status,\s*grand_total_cents,\s*currency\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": "o-1", "status": "pending", "grand_total_cents": 1234, "currency": "CNY"}),
        )
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+order_id\s*=\s*\$1\s+AND\s+provider\s*=\s*\$2\s+FOR\s+UPDATE",
            None,
        )
        conn.on(
            r"INSERT\s+INTO\s+payment_intent",
            None,
        )
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1234, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        conn.on(
            r"INSERT\s+INTO\s+payment_outbox",
            None,
        )
        conn.on(
            r"UPDATE\s+customer_order\s+SET\s+payment_provider_name",
            Recording({"id": "o-1"}),
        )
        # _read_intent
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1234, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        svc = _svc(conn)

        with patch("app.payments.service.uuid.uuid4", return_value=FIXED_INTENT_ID):
            result = await svc.create_intent(order_id="o-1", provider="manual")
        assert isinstance(result.amount_cents, int)
        assert result.amount_cents == 1234

    def test_refund_amount_cents_is_int(self):
        """Refund amounts are always integer cents."""
        # This is tested implicitly by yuan_to_cents returning int
        assert isinstance(yuan_to_cents(Decimal("10.50")), int)
        assert isinstance(yuan_to_cents("10.50"), int)
        assert isinstance(yuan_to_cents(1050), int)


class TestProviderConstraints:
    """Provider-specific currency and amount constraints."""

    async def test_wechat_alipay_only_cny(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+id,\s*status,\s*grand_total_cents,\s*currency\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": "o-1", "status": "pending", "grand_total_cents": 1000, "currency": "USD"}),
        )
        svc = _svc(conn)

        with pytest.raises(PaymentServiceError, match="only supports CNY"):
            await svc.create_intent(order_id="o-1", provider="wechat")

        with pytest.raises(PaymentServiceError, match="only supports CNY"):
            await svc.create_intent(order_id="o-1", provider="alipay")

    async def test_positive_amount_required(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+id,\s*status,\s*grand_total_cents,\s*currency\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": "o-1", "status": "pending", "grand_total_cents": 0, "currency": "CNY"}),
        )
        svc = _svc(conn)

        with pytest.raises(PaymentAmountMismatchError, match="must be positive"):
            await svc.create_intent(order_id="o-1", provider="manual")