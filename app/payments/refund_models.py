"""售后/退款状态机 + 数据模型。

RefundRequest 生命周期 (售后申请):

    submitted ──approve()──→ approved ──refund()──→ refunding ──succeed()──→ succeeded
           │                                        │
           ├──reject()──→ rejected                  └──fail()──→ failed

状态转换矩阵:
    submitted  → approved, rejected
    approved   → refunding
    refunding  → succeeded, failed
    rejected   → (terminal)
    succeeded  → (terminal)
    failed     → (terminal)

关键约束:
- 退款金额不超过实付金额
- 部分退款支持
- 退货时回补库存
- 全程审计日志
- 幂等: 外部退款单号唯一
- 接入真实网关退款 (payment_gateways)
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


# ── 售后状态枚举 ─────────────────────────────────────────────────────


class RefundStatus(str, enum.Enum):
    """售后/退款申请状态。"""

    SUBMITTED = "submitted"   # 已提交，待审核
    APPROVED = "approved"     # 审核通过，准备退款
    REFUNDING = "refunding"   # 退款处理中 (调用网关)
    SUCCEEDED = "succeeded"   # 退款成功
    FAILED = "failed"         # 退款失败 (需人工介入)
    REJECTED = "rejected"     # 审核驳回


class RefundType(str, enum.Enum):
    """售后类型。"""

    REFUND_ONLY = "refund_only"      # 仅退款 (不退货)
    RETURN_REFUND = "return_refund"  # 退货退款


class RefundReason(str, enum.Enum):
    """退款原因。"""

    BUYER_REMORSE = "buyer_remorse"           # 买家后悔/不想要了
    PRODUCT_ISSUE = "product_issue"           # 商品问题 (破损/瑕疵/错发)
    NOT_AS_DESCRIBED = "not_as_described"     # 与描述不符
    SHIPPING_DELAY = "shipping_delay"         # 发货延迟
    OTHER = "other"                           # 其他


# ── 状态转换矩阵 ─────────────────────────────────────────────────────


VALID_REFUND_TRANSITIONS: dict[RefundStatus, set[RefundStatus]] = {
    RefundStatus.SUBMITTED: {
        RefundStatus.APPROVED,
        RefundStatus.REJECTED,
    },
    RefundStatus.APPROVED: {
        RefundStatus.REFUNDING,
    },
    RefundStatus.REFUNDING: {
        RefundStatus.SUCCEEDED,
        RefundStatus.FAILED,
    },
    RefundStatus.REJECTED: set(),   # terminal
    RefundStatus.SUCCEEDED: set(),  # terminal
    RefundStatus.FAILED: set(),     # terminal
}


class InvalidRefundTransitionError(Exception):
    """非法售后状态转换。"""

    def __init__(self, refund_id: str, from_status: RefundStatus, to_status: RefundStatus) -> None:
        self.refund_id = refund_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"invalid refund transition for {refund_id}: "
            f"{from_status.value} → {to_status.value}"
        )


def validate_refund_transition(
    refund_id: str, from_status: RefundStatus, to_status: RefundStatus
) -> None:
    """校验售后状态转换是否合法。"""
    allowed = VALID_REFUND_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        raise InvalidRefundTransitionError(refund_id, from_status, to_status)


# ── 数据模型 ─────────────────────────────────────────────────────────


class RefundRequest(BaseModel):
    """售后/退款申请。"""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    order_id: str
    order_line_id: str | None = Field(None, description="关联的订单行 ID (部分退款时必填)")
    customer_id: str
    type: RefundType = RefundType.REFUND_ONLY
    reason: RefundReason = RefundReason.OTHER
    reason_detail: str | None = Field(None, description="退款原因详细说明")
    status: RefundStatus = RefundStatus.SUBMITTED

    # 金额 (分)
    requested_amount: int = Field(..., ge=0, description="申请退款金额 (分)")
    approved_amount: int | None = Field(None, ge=0, description="审核通过金额 (分)")
    actual_refunded_amount: int = Field(0, ge=0, description="实际已退金额 (分)")

    # 网关相关
    provider: str | None = Field(None, description="支付网关 (wechat/alipay/stripe/manual)")
    provider_trade_no: str | None = Field(None, description="原支付流水号")
    provider_refund_no: str | None = Field(None, description="网关退款流水号 (幂等键)")

    # 退货相关
    return_tracking_no: str | None = Field(None, description="退货物流单号")
    return_batch_id: str | None = Field(None, description="回补库存的批次 ID")

    # 审核
    reviewer_id: str | None = Field(None, description="审核人 ID")
    reject_reason: str | None = Field(None, description="驳回原因")

    # 审计
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    processed_at: datetime | None = Field(None, description="最终处理完成时间")

    def transition_to(self, new_status: RefundStatus) -> None:
        """执行状态转换。"""
        validate_refund_transition(self.id, self.status, new_status)
        self.status = new_status
        self.updated_at = datetime.now(timezone.utc)
        if new_status in (RefundStatus.SUCCEEDED, RefundStatus.FAILED, RefundStatus.REJECTED):
            self.processed_at = self.updated_at

    def is_terminal(self) -> bool:
        return not bool(VALID_REFUND_TRANSITIONS.get(self.status))

    def can_retry_refund(self) -> bool:
        """失败后是否可重试。"""
        return self.status == RefundStatus.FAILED


class RefundAuditLog(BaseModel):
    """售后审计日志。"""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    refund_id: str
    from_status: RefundStatus | None = None
    to_status: RefundStatus
    operator_id: str | None = Field(None, description="操作人 (管理员/系统)")
    operator_type: str = Field("admin", description="admin|system|customer")
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── DDL ──────────────────────────────────────────────────────────────


REFUND_REQUEST_DDL = """
-- 售后/退款申请表
CREATE TABLE IF NOT EXISTS refund_request (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL REFERENCES customer_order(id),
    order_line_id UUID REFERENCES order_line_item(id),
    customer_id UUID NOT NULL REFERENCES customer(id),
    type TEXT NOT NULL DEFAULT 'refund_only',
    reason TEXT NOT NULL DEFAULT 'other',
    reason_detail TEXT,
    status TEXT NOT NULL DEFAULT 'submitted',
    requested_amount BIGINT NOT NULL CHECK (requested_amount >= 0),
    approved_amount BIGINT,
    actual_refunded_amount BIGINT NOT NULL DEFAULT 0 CHECK (actual_refunded_amount >= 0),
    provider TEXT,
    provider_trade_no TEXT,
    provider_refund_no TEXT UNIQUE,
    return_tracking_no TEXT,
    return_batch_id UUID,
    reviewer_id UUID,
    reject_reason TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_refund_order ON refund_request(order_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_refund_customer ON refund_request(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_refund_status ON refund_request(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_refund_provider_refund ON refund_request(provider_refund_no)
    WHERE provider_refund_no IS NOT NULL;

-- 售后审计日志表
CREATE TABLE IF NOT EXISTS refund_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    refund_id UUID NOT NULL REFERENCES refund_request(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    operator_id UUID,
    operator_type TEXT NOT NULL DEFAULT 'admin',
    reason TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_refund_audit_refund ON refund_audit_log(refund_id, created_at DESC);
"""
