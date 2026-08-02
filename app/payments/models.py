"""支付状态机 + 数据模型。

PaymentSession 生命周期:

    pending ──pay()──→ paid ──refund()──→ refunded
       │                                    │
       ├──fail()──→ failed                  ├──partial_refund()──→ refunded
       │                                    │
       └──cancel()──→ cancelled             └──expire()──→ expired (超时未支付)

状态转换矩阵 (valid_transitions):
    pending  → paid, failed, cancelled, expired
    paid     → refunded, partial_refund
    failed   → (terminal)
    cancelled → (terminal)
    refunded  → (terminal)
    partial_refund → refunded (追加退款)
    expired   → (terminal)
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


# ── 支付状态枚举 ─────────────────────────────────────────────────────


class PaymentStatus(str, enum.Enum):
    """支付会话状态。"""

    PENDING = "pending"  # 待支付
    PAID = "paid"  # 已支付
    FAILED = "failed"  # 支付失败
    CANCELLED = "cancelled"  # 用户取消
    REFUNDED = "refunded"  # 全额退款
    PARTIAL_REFUND = "partial_refund"  # 部分退款
    EXPIRED = "expired"  # 超时未支付


class PaymentProvider(str, enum.Enum):
    """支付渠道。"""

    WECHAT = "wechat"  # 微信支付
    ALIPAY = "alipay"  # 支付宝
    MANUAL = "manual"  # 手动确认 (测试/内部)


# ── 状态转换矩阵 ─────────────────────────────────────────────────────


VALID_TRANSITIONS: dict[PaymentStatus, set[PaymentStatus]] = {
    PaymentStatus.PENDING: {
        PaymentStatus.PAID,
        PaymentStatus.FAILED,
        PaymentStatus.CANCELLED,
        PaymentStatus.EXPIRED,
    },
    PaymentStatus.PAID: {
        PaymentStatus.REFUNDED,
        PaymentStatus.PARTIAL_REFUND,
    },
    PaymentStatus.PARTIAL_REFUND: {
        PaymentStatus.REFUNDED,
    },
    PaymentStatus.FAILED: set(),  # terminal
    PaymentStatus.CANCELLED: set(),  # terminal
    PaymentStatus.REFUNDED: set(),  # terminal
    PaymentStatus.EXPIRED: set(),  # terminal
}


class InvalidTransitionError(Exception):
    """非法状态转换。"""

    def __init__(self, from_status: PaymentStatus, to_status: PaymentStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"invalid payment transition: {from_status.value} → {to_status.value}"
        )


def validate_transition(from_status: PaymentStatus, to_status: PaymentStatus) -> None:
    """校验状态转换是否合法。"""
    allowed = VALID_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        raise InvalidTransitionError(from_status, to_status)


# ── 数据模型 ─────────────────────────────────────────────────────────


class PaymentSession(BaseModel):
    """支付会话。"""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    order_id: str
    amount: Decimal = Field(..., ge=0, description="支付金额 (分)")
    currency: str = "CNY"
    provider: PaymentProvider
    status: PaymentStatus = PaymentStatus.PENDING
    provider_trade_no: str | None = Field(
        None, description="第三方支付流水号 (微信/支付宝)"
    )
    payer_info: str | None = Field(None, description="付款人信息 (加密存储)")
    refund_amount: Decimal | None = Field(None, description="退款金额")
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = Field(None, description="支付过期时间")

    def transition_to(self, new_status: PaymentStatus) -> None:
        """执行状态转换。"""
        validate_transition(self.status, new_status)
        self.status = new_status
        self.updated_at = datetime.now(timezone.utc)

    def is_terminal(self) -> bool:
        """是否终态。"""
        return not bool(VALID_TRANSITIONS.get(self.status))

    def can_refund(self) -> bool:
        """是否可退款。"""
        return self.status in (PaymentStatus.PAID, PaymentStatus.PARTIAL_REFUND)

    def mark_paid(self, provider_trade_no: str | None = None) -> None:
        """标记为已支付。"""
        self.transition_to(PaymentStatus.PAID)
        if provider_trade_no:
            self.provider_trade_no = provider_trade_no

    def mark_failed(self, reason: str | None = None) -> None:
        """标记为支付失败。"""
        self.transition_to(PaymentStatus.FAILED)
        if reason:
            self.metadata["failure_reason"] = reason

    def mark_refunded(self, refund_amount: Decimal | None = None) -> None:
        """标记为退款。"""
        if refund_amount and refund_amount < self.amount:
            self.transition_to(PaymentStatus.PARTIAL_REFUND)
            self.refund_amount = refund_amount
        else:
            self.transition_to(PaymentStatus.REFUNDED)
            self.refund_amount = self.amount


# ── DDL ──────────────────────────────────────────────────────────────


PAYMENT_SESSION_DDL = """
CREATE TABLE IF NOT EXISTS payment_session (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id TEXT NOT NULL,
    amount BIGINT NOT NULL CHECK (amount >= 0),
    currency TEXT NOT NULL DEFAULT 'CNY',
    provider TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    provider_trade_no TEXT,
    payer_info TEXT,
    refund_amount BIGINT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_payment_order ON payment_session(order_id);
CREATE INDEX IF NOT EXISTS idx_payment_status ON payment_session(status);
CREATE INDEX IF NOT EXISTS idx_payment_provider_trade ON payment_session(provider_trade_no);
CREATE INDEX IF NOT EXISTS idx_payment_expires ON payment_session(expires_at)
    WHERE status = 'pending';
"""
