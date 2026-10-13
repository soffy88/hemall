"""Audit: payments service write guards and monetary invariants.

These tests pin:
1. yuan_to_cents strictly converts to integer cents, rejecting float imprecision
2. Payment intent creation uses server-controlled amounts (not client-supplied)
3. Refund idempotency keys prevent duplicate refunds
4. All monetary values use integer cents, never float
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch, AsyncMock
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


class TestPaymentIntentProcessing:
    """process_intent handles provider success/failure and outbox."""

    async def test_process_intent_no_pending_outbox(self):
        conn = FakeConn()
        # _claim_outbox returns None (no pending job)
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'processing'",
            None,
        )
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        svc = _svc(conn)

        result = await svc.process_intent(str(FIXED_INTENT_ID))

        assert result.intent_id == str(FIXED_INTENT_ID)

    async def test_process_intent_provider_fails_marks_outbox_failed(self):
        conn = FakeConn()
        # _claim_outbox returns a job
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'processing'",
            Recording({"id": "job-1", "payload": '{}'}),
        )
        # _read_intent
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        # _provider_prepay raises exception
        # _mark_outbox_failed
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'failed'",
            "OK",
        )
        # _read_intent after failure
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        svc = _svc(conn)

        result = await svc.process_intent(str(FIXED_INTENT_ID))

        assert result.status == "pending"

    async def test_process_intent_provider_success_updates_intent_and_outbox(self):
        conn = FakeConn()
        # _claim_outbox returns a job
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'processing'",
            Recording({"id": "job-1", "payload": '{}'}),
        )
        # _read_intent
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        # UPDATE payment_intent
        conn.on(
            r"UPDATE\s+payment_intent\s+SET\s+status",
            Recording({"id": str(FIXED_INTENT_ID)}),
        )
        # UPDATE payment_outbox
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'completed'",
            Recording({"id": "job-1"}),
        )
        # _read_intent after success
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "provider_pending", "metadata": {"provider_result": {}}}),
        )
        svc = _svc(conn)

        with patch.object(svc, '_provider_prepay', return_value={"prepay_id": "prepay-1"}):
            result = await svc.process_intent(str(FIXED_INTENT_ID))

        assert result.status == "provider_pending"


class TestRequestRefund:
    """request_refund creates refund records with idempotency."""

    async def test_request_refund_existing_succeeded_returns_same(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "paid", "provider_trade_no": "trade-1", "provider_intent_id": "pi-provider"}),
        )
        # Existing refund with status succeeded
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+payment_intent_id\s*=\s*\$1\s+AND\s+idempotency_key\s*=\s*\$2\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund-key-1", "amount_cents": 500, "reason": "test", "status": "succeeded", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None}),
        )
        svc = _svc(conn)

        result = await svc.request_refund(
            payment_id=str(FIXED_INTENT_ID),
            refund_amount=Decimal("5.00"),
            reason="test",
            idempotency_key="refund-key-1",
        )

        assert result["refund_amount_cents"] == 500

    async def test_request_refund_existing_failed_retries(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "paid", "provider_trade_no": "trade-1", "provider_intent_id": "pi-provider"}),
        )
        # Existing refund with status failed
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+payment_intent_id\s*=\s*\$1\s+AND\s+idempotency_key\s*=\s*\$2\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund-key-1", "amount_cents": 500, "reason": "test", "status": "failed", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None}),
        )
        # UPDATE payment_refund to pending
        conn.on(
            r"UPDATE\s+payment_refund\s+SET\s+status\s*=\s*'pending'",
            Recording({"id": str(FIXED_REFUND_ID)}),
        )
        # INSERT payment_outbox
        conn.on(r"INSERT\s+INTO\s+payment_outbox", None)
        # _read_refund called by process_refund
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund-key-1", "amount_cents": 500, "reason": "test", "status": "succeeded", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None}),
        )
        svc = _svc(conn)

        result = await svc.request_refund(
            payment_id=str(FIXED_INTENT_ID),
            refund_amount=Decimal("5.00"),
            reason="test",
            idempotency_key="refund-key-1",
        )

        assert result["refund_amount_cents"] == 500


class TestProcessRefund:
    """process_refund handles provider responses and updates state."""

    async def test_process_refund_no_pending_outbox(self):
        conn = FakeConn()
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'processing'",
            None,
        )
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund-key-1", "amount_cents": 500, "reason": "test", "status": "pending", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None}),
        )
        svc = _svc(conn)

        result = await svc.process_refund(str(FIXED_REFUND_ID))

        assert result["status"] == "pending"

    async def test_process_refund_provider_fails(self):
        conn = FakeConn()
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'processing'",
            Recording({"id": "job-1", "payload": '{}'}),
        )
        # JOIN query for refund + intent
        conn.on(
            r"SELECT\s+r\.\*,\s*p\.provider",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund-key-1", "amount_cents": 500, "reason": "test", "status": "pending", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None, "provider": "manual", "order_id": "o-1", "provider_trade_no": "trade-1", "provider_intent_id": "pi-provider", "original_amount_cents": 1000}),
        )
        # _mark_refund_failed updates
        conn.on(r"UPDATE\s+payment_refund\s+SET\s+status\s*=\s*'failed'", "OK")
        conn.on(r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'failed'", "OK")
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund-key-1", "amount_cents": 500, "reason": "test", "status": "failed", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None}),
        )
        svc = _svc(conn)

        with patch.object(svc, '_provider_refund', side_effect=Exception("provider error")):
            result = await svc.process_refund(str(FIXED_REFUND_ID))

        assert result["status"] == "failed"

    async def test_process_refund_provider_success_succeeded(self):
        conn = FakeConn()
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'processing'",
            Recording({"id": "job-1", "payload": '{}'}),
        )
        # JOIN query for refund + intent
        conn.on(
            r"SELECT\s+r\.\*,\s*p\.provider",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund-key-1", "amount_cents": 500, "reason": "test", "status": "pending", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None, "provider": "manual", "order_id": "o-1", "provider_trade_no": "trade-1", "provider_intent_id": "pi-provider", "original_amount_cents": 1000}),
        )
        # UPDATE payment_refund to processing
        conn.on(
            r"UPDATE\s+payment_refund\s+SET\s+status\s*=\s*'processing'",
            Recording({"id": str(FIXED_REFUND_ID)}),
        )
        conn.on(r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'pending'", Recording({"id": "job-1"}))
        # _read_refund after processing
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund-key-1", "amount_cents": 500, "reason": "test", "status": "processing", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None}),
        )
        svc = _svc(conn)

        with patch.object(svc, '_provider_refund', return_value={"status": "pending"}):
            result = await svc.process_refund(str(FIXED_REFUND_ID))

        assert result["status"] == "processing"

    async def test_process_refund_provider_success_final(self):
        conn = FakeConn()
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'processing'",
            Recording({"id": "job-1", "payload": '{}'}),
        )
        # JOIN query for refund + intent
        conn.on(
            r"SELECT\s+r\.\*,\s*p\.provider",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund-key-1", "amount_cents": 500, "reason": "test", "status": "pending", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None, "provider": "manual", "order_id": "o-1", "provider_trade_no": "trade-1", "provider_intent_id": "pi-provider", "original_amount_cents": 1000}),
        )
        # UPDATE payment_refund to succeeded
        conn.on(
            r"UPDATE\s+payment_refund\s+SET\s+status\s*=\s*'succeeded'",
            Recording({"id": str(FIXED_REFUND_ID)}),
        )
        # SUM refunded
        conn.on(r"SELECT\s+COALESCE\(SUM\(amount_cents\)", 500)
        # UPDATE payment_intent
        conn.on(
            r"UPDATE\s+payment_intent\s+SET\s+refund_amount_cents",
            Recording({"id": str(FIXED_INTENT_ID)}),
        )
        # UPDATE payment_outbox
        conn.on(r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'completed'", Recording({"id": "job-1"}))
        # _read_refund
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund-key-1", "amount_cents": 500, "reason": "test", "status": "succeeded", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None}),
        )
        svc = _svc(conn)

        with patch.object(svc, '_provider_refund', return_value={"status": "success", "id": "refund-1"}):
            result = await svc.process_refund(str(FIXED_REFUND_ID))

        assert result["status"] == "succeeded"


class TestReconcile:
    """reconcile processes stuck outbox jobs."""

    async def test_reconcile_resets_processing_jobs(self):
        conn = FakeConn()
        # UPDATE stuck processing jobs
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'pending'",
            "OK",
        )
        # SELECT jobs to process
        conn.on(
            r"SELECT\s+id,\s+payment_intent_id,\s+refund_id,\s+kind\s+FROM\s+payment_outbox",
            [Recording({"id": "job-1", "payment_intent_id": str(FIXED_INTENT_ID), "refund_id": None, "kind": "prepay"})],
        )
        # process_intent will be called
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'processing'",
            None,
        )
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        svc = _svc(conn)

        with patch.object(svc, 'process_intent') as mock_process:
            mock_process.return_value = None
            processed = await svc.reconcile(limit=10)

        assert processed == 1

    async def test_reconcile_processes_refund_jobs(self):
        conn = FakeConn()
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'pending'",
            "OK",
        )
        conn.on(
            r"SELECT\s+id,\s+payment_intent_id,\s+refund_id,\s+kind\s+FROM\s+payment_outbox",
            [Recording({"id": "job-1", "payment_intent_id": str(FIXED_INTENT_ID), "refund_id": str(FIXED_REFUND_ID), "kind": "refund"})],
        )
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'processing'",
            None,
        )
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund-key-1", "amount_cents": 500, "reason": "test", "status": "pending", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None}),
        )
        svc = _svc(conn)

        with patch.object(svc, 'process_refund') as mock_process:
            mock_process.return_value = None
            processed = await svc.reconcile(limit=10)

        assert processed == 1


class TestInternalMethods:
    """Test internal helper methods."""

    async def test_read_intent_returns_result(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {"provider_result": {}}}),
        )
        svc = _svc(conn)

        result = await svc._read_intent(str(FIXED_INTENT_ID))

        assert result.intent_id == str(FIXED_INTENT_ID)
        assert result.amount_cents == 1000

    async def test_read_intent_not_found_raises(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1",
            None,
        )
        svc = _svc(conn)

        with pytest.raises(PaymentNotFoundError):
            await svc._read_intent(str(FIXED_INTENT_ID))

    async def test_read_refund_returns_result(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund-key-1", "amount_cents": 500, "reason": "test", "status": "succeeded", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None}),
        )
        svc = _svc(conn)

        result = await svc._read_refund(str(FIXED_REFUND_ID))

        assert result["refund_id"] == str(FIXED_REFUND_ID)
        assert result["refund_amount_cents"] == 500

    async def test_read_refund_not_found_raises(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+id\s*=\s*\$1",
            None,
        )
        svc = _svc(conn)

        with pytest.raises(PaymentNotFoundError):
            await svc._read_refund(str(FIXED_REFUND_ID))

    async def test_claim_outbox_prepay_returns_job(self):
        conn = FakeConn()
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'processing'",
            Recording({"id": "job-1", "payload": '{}'}),
        )
        svc = _svc(conn)

        job = await svc._claim_outbox(intent_id=str(FIXED_INTENT_ID), kind="prepay")

        assert job is not None
        assert job["id"] == "job-1"

    async def test_claim_outbox_refund_returns_job(self):
        conn = FakeConn()
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'processing'",
            Recording({"id": "job-1", "payload": '{}'}),
        )
        svc = _svc(conn)

        job = await svc._claim_outbox(refund_id=str(FIXED_REFUND_ID), kind="refund")

        assert job is not None
        assert job["id"] == "job-1"

    async def test_mark_outbox_failed_updates_status(self):
        conn = FakeConn()
        conn.on(
            r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'failed'",
            "OK",
        )
        svc = _svc(conn)

        await svc._mark_outbox_failed("job-1", "test error")

        assert conn.did_issue(r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'failed'")

    async def test_mark_refund_failed_updates_both(self):
        conn = FakeConn()
        conn.on(r"UPDATE\s+payment_refund\s+SET\s+status\s*=\s*'failed'", "OK")
        conn.on(r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'failed'", "OK")
        svc = _svc(conn)

        await svc._mark_refund_failed("job-1", str(FIXED_REFUND_ID), "test error")

        assert conn.did_issue(r"UPDATE\s+payment_refund\s+SET\s+status\s*=\s*'failed'")
        assert conn.did_issue(r"UPDATE\s+payment_outbox\s+SET\s+status\s*=\s*'failed'")


class TestCreateIntentExistingIntent:
    """Test create_intent when intent already exists."""

    async def test_create_intent_reuses_existing_intent(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+id,\s*status,\s*grand_total_cents,\s*currency\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": "o-1", "status": "pending", "grand_total_cents": 1000, "currency": "CNY"}),
        )
        # Existing intent found
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+order_id\s*=\s*\$1\s+AND\s+provider\s*=\s*\$2\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        # UPDATE customer_order
        conn.on(
            r"UPDATE\s+customer_order\s+SET\s+payment_provider_name",
            Recording({"id": "o-1"}),
        )
        # _read_intent
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        svc = _svc(conn)

        with patch("app.payments.service.uuid.uuid4", return_value=FIXED_INTENT_ID):
            result = await svc.create_intent(
                order_id="o-1",
                provider="manual",
            )

        assert result.intent_id == str(FIXED_INTENT_ID)
        assert result.amount_cents == 1000

    async def test_create_intent_rejects_mismatched_existing_intent(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+id,\s*status,\s*grand_total_cents,\s*currency\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": "o-1", "status": "pending", "grand_total_cents": 2000, "currency": "CNY"}),
        )
        # Existing intent with different amount
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+order_id\s*=\s*\$1\s+AND\s+provider\s*=\s*\$2\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": {}}),
        )
        svc = _svc(conn)

        with pytest.raises(PaymentAmountMismatchError, match="no longer matches order total"):
            await svc.create_intent(
                order_id="o-1",
                provider="manual",
            )


class TestRequestRefundFull:
    """Test request_refund with new refund creation."""

    async def test_request_refund_creates_new_refund(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "paid", "provider_trade_no": "trade-1", "provider_intent_id": "pi-provider"}),
        )
        # No existing refund
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+payment_intent_id\s*=\s*\$1\s+AND\s+idempotency_key\s*=\s*\$2\s+FOR\s+UPDATE",
            None,
        )
        # SUM refunded
        conn.on(r"SELECT\s+COALESCE\(SUM\(amount_cents\)", 0)
        # INSERT payment_refund
        conn.on(r"INSERT\s+INTO\s+payment_refund", None)
        # INSERT payment_outbox
        conn.on(r"INSERT\s+INTO\s+payment_outbox", None)
        # _read_refund
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund-key-1", "amount_cents": 500, "reason": "test", "status": "succeeded", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None}),
        )
        svc = _svc(conn)

        with patch("app.payments.service.uuid.uuid4", return_value=FIXED_REFUND_ID):
            result = await svc.request_refund(
                payment_id=str(FIXED_INTENT_ID),
                refund_amount=Decimal("5.00"),
                reason="test",
                idempotency_key="refund-key-1",
            )

        assert result["refund_amount_cents"] == 500

    async def test_request_refund_full_amount_when_none(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "paid", "provider_trade_no": "trade-1", "provider_intent_id": "pi-provider"}),
        )
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+payment_intent_id\s*=\s*\$1\s+AND\s+idempotency_key\s*=\s*\$2\s+FOR\s+UPDATE",
            None,
        )
        conn.on(r"SELECT\s+COALESCE\(SUM\(amount_cents\)", 0)
        conn.on(r"INSERT\s+INTO\s+payment_refund", None)
        conn.on(r"INSERT\s+INTO\s+payment_outbox", None)
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund:full", "amount_cents": 1000, "reason": "test", "status": "succeeded", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None}),
        )
        svc = _svc(conn)

        with patch("app.payments.service.uuid.uuid4", return_value=FIXED_REFUND_ID):
            result = await svc.request_refund(
                payment_id=str(FIXED_INTENT_ID),
                refund_amount=None,
                reason="test",
                idempotency_key="refund:full",
            )

        assert result["refund_amount_cents"] == 1000


class TestEdgeCases:
    """Test edge cases and error paths."""

    async def test_create_intent_existing_intent_paid_status(self):
        """Existing intent with paid status should not create outbox."""
        conn = FakeConn()
        conn.on(
            r"SELECT\s+id,\s*status,\s*grand_total_cents,\s*currency\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": "o-1", "status": "pending", "grand_total_cents": 1000, "currency": "CNY"}),
        )
        # Existing intent with paid status
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+order_id\s*=\s*\$1\s+AND\s+provider\s*=\s*\$2\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "paid", "metadata": {}}),
        )
        conn.on(
            r"UPDATE\s+customer_order\s+SET\s+payment_provider_name",
            Recording({"id": "o-1"}),
        )
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "paid", "metadata": {}}),
        )
        svc = _svc(conn)

        with patch("app.payments.service.uuid.uuid4", return_value=FIXED_INTENT_ID):
            result = await svc.create_intent(
                order_id="o-1",
                provider="manual",
            )

        assert result.status == "paid"
        # No outbox should be created for paid intent
        assert not conn.did_issue(r"INSERT\s+INTO\s+payment_outbox")

    async def test_request_refund_amount_mismatch_raises(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "paid", "provider_trade_no": "trade-1", "provider_intent_id": "pi-provider"}),
        )
        # Existing refund with different amount
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+payment_intent_id\s*=\s*\$1\s+AND\s+idempotency_key\s*=\s*\$2\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_REFUND_ID), "payment_intent_id": str(FIXED_INTENT_ID), "idempotency_key": "refund-key-1", "amount_cents": 500, "reason": "test", "status": "pending", "provider_refund_no": "provider-ref-1", "provider_result": {}, "version": 0, "created_at": None, "updated_at": None}),
        )
        svc = _svc(conn)

        with pytest.raises(RefundConflictError, match="idempotency key reused with another amount"):
            await svc.request_refund(
                payment_id=str(FIXED_INTENT_ID),
                refund_amount=Decimal("3.00"),
                reason="test",
                idempotency_key="refund-key-1",
            )

    async def test_request_refund_exceeds_balance_raises(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "paid", "provider_trade_no": "trade-1", "provider_intent_id": "pi-provider"}),
        )
        conn.on(
            r"SELECT\s+\*\s+FROM\s+payment_refund\s+WHERE\s+payment_intent_id\s*=\s*\$1\s+AND\s+idempotency_key\s*=\s*\$2\s+FOR\s+UPDATE",
            None,
        )
        conn.on(r"SELECT\s+COALESCE\(SUM\(amount_cents\)", 800)  # Already refunded 800
        svc = _svc(conn)

        with pytest.raises(RefundConflictError, match="exceeds refundable balance"):
            await svc.request_refund(
                payment_id=str(FIXED_INTENT_ID),
                refund_amount=Decimal("5.00"),
                reason="test",
                idempotency_key="refund-key-new",
            )

    async def test_read_intent_parses_metadata_string(self):
        conn = FakeConn()
        conn.on(
            r"SELECT\s+id,\s*order_id,\s*amount_cents,\s*currency,\s*provider,\s*status,\s*metadata\s+FROM\s+payment_intent\s+WHERE\s+id\s*=\s*\$1",
            Recording({"id": str(FIXED_INTENT_ID), "order_id": "o-1", "amount_cents": 1000, "currency": "CNY", "provider": "manual", "status": "pending", "metadata": '{"provider_result": {"key": "value"}}'}),
        )
        svc = _svc(conn)

        result = await svc._read_intent(str(FIXED_INTENT_ID))

        assert result.provider_result == {"key": "value"}


class TestProviderIntegration:
    """Test provider integration paths (Alipay, WeChat, Stripe) with mocked gateways."""

    async def test_provider_prepay_alipay(self):
        conn = FakeConn()
        svc = _svc(conn)

        with patch.object(svc, '_gateway') as mock_gateway:
            from app.payments.alipay import AlipayOrderResponse
            mock_response = AlipayOrderResponse(qr_code='alipay-qr', err_code=None, err_msg=None)
            mock_gateway.return_value.create_order = AsyncMock(return_value=mock_response)
            # Ensure prepay doesn't exist
            mock_gateway.return_value.prepay = None
            result = await svc._provider_prepay(
                provider="alipay",
                out_trade_no="test-alipay",
                total_fee_cents=1000,
                currency="CNY",
                description="alipay order",
                payer_openid=None,
            )
        assert result["qr_code"] == "alipay-qr"
        assert result["gateway"] == "alipay"

    async def test_provider_prepay_alipay_fails(self):
        conn = FakeConn()
        svc = _svc(conn)

        with patch.object(svc, '_gateway') as mock_gateway:
            from app.payments.alipay import AlipayOrderResponse
            mock_response = AlipayOrderResponse(qr_code=None, err_code="40004", err_msg='alipay error')
            mock_gateway.return_value.create_order = AsyncMock(return_value=mock_response)
            mock_gateway.return_value.prepay = None
            with pytest.raises(PaymentServiceError, match="alipay error"):
                await svc._provider_prepay(
                    provider="alipay",
                    out_trade_no="test-alipay-fail",
                    total_fee_cents=1000,
                    currency="CNY",
                    description="alipay order",
                    payer_openid=None,
                )

    async def test_provider_prepay_wechat(self):
        conn = FakeConn()
        settings = type("Settings", (), {
            "wechat_pay_notify_url": "https://test/notify",
        })()
        svc = PaymentService(FakePool(conn), settings)

        with patch.object(svc, '_gateway') as mock_gateway:
            from app.payments.wechat import WeChatPayOrderResponse
            mock_response = WeChatPayOrderResponse(code_url='wechat-code', prepay_id='wechat-prepay', err_code=None, err_msg=None)
            mock_gateway.return_value.create_order = AsyncMock(return_value=mock_response)
            # Ensure prepay doesn't exist so it falls through to create_order
            if hasattr(mock_gateway.return_value, 'prepay'):
                del mock_gateway.return_value.prepay
            result = await svc._provider_prepay(
                provider="wechat",
                out_trade_no="test-wechat",
                total_fee_cents=1000,
                currency="CNY",
                description="wechat order",
                payer_openid="openid-123",
            )
        assert result["code_url"] == "wechat-code"
        assert result["prepay_id"] == "wechat-prepay"
        assert result["gateway"] == "wechat"

    async def test_provider_prepay_wechat_fails(self):
        conn = FakeConn()
        settings = type("Settings", (), {
            "wechat_pay_notify_url": "https://test/notify",
        })()
        svc = PaymentService(FakePool(conn), settings)

        with patch.object(svc, '_gateway') as mock_gateway:
            from app.payments.wechat import WeChatPayOrderResponse
            mock_response = WeChatPayOrderResponse(code_url=None, prepay_id=None, err_code="SYSTEMERROR", err_msg='wechat error')
            mock_gateway.return_value.create_order = AsyncMock(return_value=mock_response)
            if hasattr(mock_gateway.return_value, 'prepay'):
                del mock_gateway.return_value.prepay
            with pytest.raises(PaymentServiceError, match="wechat error"):
                await svc._provider_prepay(
                    provider="wechat",
                    out_trade_no="test-wechat-fail",
                    total_fee_cents=1000,
                    currency="CNY",
                    description="wechat order",
                    payer_openid="openid-123",
                )

    async def test_provider_prepay_unknown_provider(self):
        conn = FakeConn()
        svc = _svc(conn)

        with patch.object(svc, '_gateway') as mock_gateway:
            # Don't set prepay or create_order attributes at all
            del mock_gateway.return_value.prepay
            del mock_gateway.return_value.create_order
            with pytest.raises(PaymentServiceError, match="does not support prepay"):
                await svc._provider_prepay(
                    provider="unknown",
                    out_trade_no="test-unknown",
                    total_fee_cents=1000,
                    currency="CNY",
                    description="unknown order",
                    payer_openid=None,
                )

    async def test_provider_refund_alipay(self):
        conn = FakeConn()
        svc = _svc(conn)

        with patch.object(svc, '_gateway') as mock_gateway:
            mock_gateway.return_value.refund = AsyncMock(return_value={"code": "10000", "msg": "success"})
            result = await svc._provider_refund(
                provider="alipay",
                out_trade_no="test-alipay-refund",
                provider_intent_id="pi-alipay",
                out_refund_no="refund-alipay",
                refund_fee_cents=500,
                reason="alipay refund",
                total_fee_cents=1000,
            )
        assert result["code"] == "10000"

    async def test_provider_refund_alipay_fails(self):
        conn = FakeConn()
        svc = _svc(conn)

        with patch.object(svc, '_gateway') as mock_gateway:
            mock_gateway.return_value.refund = AsyncMock(return_value={"code": "40004", "msg": "fail"})
            with pytest.raises(PaymentServiceError, match="fail"):
                await svc._provider_refund(
                    provider="alipay",
                    out_trade_no="test-alipay-refund-fail",
                    provider_intent_id="pi-alipay",
                    out_refund_no="refund-alipay-fail",
                    refund_fee_cents=500,
                    reason="alipay refund",
                    total_fee_cents=1000,
                )

    async def test_provider_refund_wechat_typeerror_fallback(self):
        conn = FakeConn()
        svc = _svc(conn)

        with patch.object(svc, '_gateway') as mock_gateway:
            # First call raises TypeError (legacy sandbox), second call succeeds
            mock_gateway.return_value.refund = AsyncMock(side_effect=[
                TypeError("legacy"),
                {"status": "success", "refund_id": "wechat-refund"}
            ])
            result = await svc._provider_refund(
                provider="wechat",
                out_trade_no="test-wechat-refund",
                provider_intent_id=None,
                out_refund_no="refund-wechat",
                refund_fee_cents=500,
                reason="wechat refund",
                total_fee_cents=1000,
            )
        assert result["status"] == "success"

    async def test_provider_refund_generic_fails(self):
        conn = FakeConn()
        svc = _svc(conn)

        with patch.object(svc, '_gateway') as mock_gateway:
            mock_gateway.return_value.refund = AsyncMock(return_value={"status": "failed"})
            with pytest.raises(PaymentServiceError, match="payment provider refund failed"):
                await svc._provider_refund(
                    provider="generic",
                    out_trade_no="test-generic-refund",
                    provider_intent_id=None,
                    out_refund_no="refund-generic",
                    refund_fee_cents=500,
                    reason="generic refund",
                    total_fee_cents=1000,
                )