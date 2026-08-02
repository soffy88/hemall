"""Phase 0 Week 2 新功能测试 — 安全加固 + 支付对接。"""

from __future__ import annotations

import pytest
from decimal import Decimal

# ── 支付状态机测试 ────────────────────────────────────────────────────

from app.payments.models import (
    PaymentSession,
    PaymentStatus,
    PaymentProvider,
    InvalidTransitionError,
    validate_transition,
)


class TestPaymentStateMachine:
    """支付状态机转换测试。"""

    def _make_session(self) -> PaymentSession:
        return PaymentSession(
            order_id="order-001",
            amount=Decimal("99.99"),
            provider=PaymentProvider.WECHAT,
        )

    def test_initial_status_is_pending(self):
        session = self._make_session()
        assert session.status == PaymentStatus.PENDING
        assert not session.is_terminal()

    def test_pending_to_paid(self):
        session = self._make_session()
        session.mark_paid(provider_trade_no="tx-123")
        assert session.status == PaymentStatus.PAID
        assert session.provider_trade_no == "tx-123"

    def test_pending_to_failed(self):
        session = self._make_session()
        session.mark_failed("insufficient funds")
        assert session.status == PaymentStatus.FAILED
        assert session.metadata["failure_reason"] == "insufficient funds"
        assert session.is_terminal()

    def test_pending_to_cancelled(self):
        session = self._make_session()
        session.transition_to(PaymentStatus.CANCELLED)
        assert session.status == PaymentStatus.CANCELLED
        assert session.is_terminal()

    def test_paid_to_refunded(self):
        session = self._make_session()
        session.mark_paid()
        session.mark_refunded()
        assert session.status == PaymentStatus.REFUNDED
        assert session.refund_amount == session.amount

    def test_paid_to_partial_refund(self):
        session = self._make_session()
        session.mark_paid()
        session.mark_refunded(refund_amount=Decimal("50.00"))
        assert session.status == PaymentStatus.PARTIAL_REFUND
        assert session.refund_amount == Decimal("50.00")
        assert session.can_refund()  # 仍可追加退款

    def test_partial_refund_to_full_refund(self):
        session = self._make_session()
        session.mark_paid()
        session.mark_refunded(refund_amount=Decimal("50.00"))
        session.mark_refunded()  # 追加到全额
        assert session.status == PaymentStatus.REFUNDED

    def test_invalid_transition_pending_to_refunded(self):
        session = self._make_session()
        with pytest.raises(InvalidTransitionError):
            session.transition_to(PaymentStatus.REFUNDED)

    def test_invalid_transition_failed_to_paid(self):
        session = self._make_session()
        session.mark_failed()
        with pytest.raises(InvalidTransitionError):
            session.mark_paid()

    def test_terminal_states(self):
        for terminal in [
            PaymentStatus.FAILED,
            PaymentStatus.CANCELLED,
            PaymentStatus.REFUNDED,
            PaymentStatus.EXPIRED,
        ]:
            session = self._make_session()
            session.status = terminal
            assert session.is_terminal()


# ── 加密工具测试 ──────────────────────────────────────────────────────

from app.security.crypto import (
    encrypt_field,
    decrypt_field,
    mask_phone,
    mask_id_card,
)


class TestCrypto:
    """敏感数据加密/解密测试。"""

    def test_encrypt_decrypt_roundtrip(self):
        original = "13812345678"
        encrypted = encrypt_field(original)
        assert encrypted != original
        assert len(encrypted) > 20  # Fernet ciphertext 较长
        decrypted = decrypt_field(encrypted)
        assert decrypted == original

    def test_none_passthrough(self):
        assert encrypt_field(None) is None
        assert decrypt_field(None) is None

    def test_empty_string_passthrough(self):
        assert encrypt_field("") == ""
        assert decrypt_field("") == ""

    def test_decrypt_unencrypted_fallback(self):
        # 历史明文数据降级返回
        result = decrypt_field("plaintext-not-encrypted")
        assert result == "plaintext-not-encrypted"

    def test_mask_phone(self):
        assert mask_phone("13812345678") == "138****5678"
        assert mask_phone("12345") == "12345"  # 太短不脱敏
        assert mask_phone(None) == ""

    def test_mask_id_card(self):
        assert mask_id_card("110101199001011234") == "110***********1234"
        assert mask_id_card(None) == ""


# ── 微信支付沙箱测试 ─────────────────────────────────────────────────

from app.payments.wechat import WeChatPayProvider, WeChatPayOrderRequest


class TestWeChatPaySandbox:
    """微信支付沙箱模式测试。"""

    @pytest.mark.asyncio
    async def test_create_order(self):
        provider = WeChatPayProvider()
        resp = await provider.create_order(
            WeChatPayOrderRequest(
                out_trade_no="test-001",
                description="Test Product",
                total_amount=9999,
            )
        )
        assert resp.success
        assert resp.prepay_id is not None
        assert resp.code_url is not None
        assert "weixin://" in resp.code_url

    @pytest.mark.asyncio
    async def test_query_order(self):
        provider = WeChatPayProvider()
        await provider.create_order(
            WeChatPayOrderRequest(
                out_trade_no="test-002",
                description="Test",
                total_amount=100,
            )
        )
        result = await provider.query_order("test-002")
        assert result["trade_state"] == "NOTPAY"

    @pytest.mark.asyncio
    async def test_refund(self):
        provider = WeChatPayProvider()
        await provider.create_order(
            WeChatPayOrderRequest(
                out_trade_no="test-003",
                description="Test",
                total_amount=100,
            )
        )
        result = await provider.refund(
            out_trade_no="test-003",
            out_refund_no="refund-001",
            refund_amount=100,
        )
        assert result["status"] == "SUCCESS"


# ── 支付宝沙箱测试 ────────────────────────────────────────────────────

from app.payments.alipay import AlipayProvider, AlipayOrderRequest


class TestAlipaySandbox:
    """支付宝沙箱模式测试。"""

    @pytest.mark.asyncio
    async def test_create_order(self):
        provider = AlipayProvider()
        resp = await provider.create_order(
            AlipayOrderRequest(
                out_trade_no="ali-001",
                subject="Test Product",
                total_amount="99.99",
            )
        )
        assert resp.success
        assert resp.qr_code is not None
        assert "qr.alipay.com" in resp.qr_code

    @pytest.mark.asyncio
    async def test_query_not_found(self):
        provider = AlipayProvider()
        result = await provider.query_order("nonexistent")
        assert result["trade_status"] == "WAIT_BUYER_PAY"

    @pytest.mark.asyncio
    async def test_refund(self):
        provider = AlipayProvider()
        await provider.create_order(
            AlipayOrderRequest(
                out_trade_no="ali-002",
                subject="Test",
                total_amount="50.00",
            )
        )
        result = await provider.refund(
            out_trade_no="ali-002",
            refund_amount="50.00",
        )
        assert result["code"] == "10000"
