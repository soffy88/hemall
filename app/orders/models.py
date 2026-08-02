"""订单数据模型 — 状态机、历史追踪、订单快照。"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


# ── 订单状态枚举 ─────────────────────────────────────────────────────


class OrderStatus(str, enum.Enum):
    """订单生命周期状态。

    完整流转:
        pending      → 购物车 checkout 后生成 (等待支付)
        confirmed    → 支付成功
        processing   → 仓库接单处理中
        packed       → 已打包
        shipped      → 已发货
        delivered    → 已送达
        completed    → 客户确认收货 / 自动完成

    异常分支:
        pending      → cancelled (取消/超时未支付)
        confirmed    → cancelled (商家取消)
        processing   → cancelled (商家取消)
        shipped      → returning (退货中)
        delivered    → returning (退货中)
        returning    → refunded (退款完成)
        returning    → completed (退货拒绝, 交易完成)
        any          → failed (系统异常)
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    PROCESSING = "processing"
    PACKED = "packed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    RETURNING = "returning"
    REFUNDED = "refunded"
    FAILED = "failed"


# ── 状态转换矩阵 ─────────────────────────────────────────────────────


VALID_ORDER_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {
        OrderStatus.CONFIRMED,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
    },
    OrderStatus.CONFIRMED: {
        OrderStatus.PROCESSING,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
    },
    OrderStatus.PROCESSING: {
        OrderStatus.PACKED,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
    },
    OrderStatus.PACKED: {
        OrderStatus.SHIPPED,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
    },
    OrderStatus.SHIPPED: {
        OrderStatus.DELIVERED,
        OrderStatus.RETURNING,
        OrderStatus.FAILED,
    },
    OrderStatus.DELIVERED: {
        OrderStatus.COMPLETED,
        OrderStatus.RETURNING,
        OrderStatus.FAILED,
    },
    OrderStatus.RETURNING: {
        OrderStatus.REFUNDED,
        OrderStatus.COMPLETED,  # 退货被拒, 交易完成
    },
    OrderStatus.COMPLETED: set(),  # terminal
    OrderStatus.CANCELLED: set(),  # terminal
    OrderStatus.REFUNDED: set(),  # terminal
    OrderStatus.FAILED: set(),  # terminal
}

#: 终态集合
TERMINAL_STATUSES = {
    OrderStatus.COMPLETED,
    OrderStatus.CANCELLED,
    OrderStatus.REFUNDED,
    OrderStatus.FAILED,
}


class InvalidOrderTransitionError(Exception):
    """非法订单状态转换。"""

    def __init__(self, order_id: str, from_status: OrderStatus, to_status: OrderStatus) -> None:
        self.order_id = order_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"invalid order transition for {order_id}: "
            f"{from_status.value} → {to_status.value}"
        )


def validate_order_transition(
    order_id: str, from_status: OrderStatus, to_status: OrderStatus
) -> None:
    """校验订单状态转换是否合法。"""
    allowed = VALID_ORDER_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        raise InvalidOrderTransitionError(order_id, from_status, to_status)


# ── 数据模型 ─────────────────────────────────────────────────────────


class OrderSnapshot(BaseModel):
    """订单快照 (查询用)。"""

    id: str
    cart_id: str | None = None
    customer_id: str | None = None
    status: OrderStatus
    currency: str = "CNY"
    subtotal_cents: int = 0
    discount_cents: int = 0
    tax_cents: int = 0
    shipping_cents: int = 0
    grand_total_cents: int = 0
    payment_provider_name: str | None = None
    payment_intent_id: str | None = None
    billing_address: dict[str, Any] | None = None
    shipping_address: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def grand_total_yuan(self) -> Decimal:
        return Decimal(self.grand_total_cents) / 100

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def can_cancel(self) -> bool:
        return self.status in (
            OrderStatus.PENDING,
            OrderStatus.CONFIRMED,
            OrderStatus.PROCESSING,
        )

    @property
    def can_return(self) -> bool:
        return self.status in (OrderStatus.SHIPPED, OrderStatus.DELIVERED)


class OrderLineItem(BaseModel):
    """订单行项目。"""

    id: str
    order_id: str
    batch_id: str
    quantity: int
    unit_price_cents: int
    line_total_cents: int

    @property
    def unit_price_yuan(self) -> Decimal:
        return Decimal(self.unit_price_cents) / 100


class StatusHistoryEntry(BaseModel):
    """状态变更历史记录。"""

    id: str | None = None
    order_id: str
    from_status: str | None = None
    to_status: str
    reason: str | None = None
    operator_id: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── DDL ──────────────────────────────────────────────────────────────


ORDER_LIFECYCLE_DDL = """
-- 订单状态历史表
CREATE TABLE IF NOT EXISTS order_status_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL REFERENCES customer_order(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT,
    operator_id TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_order_history_order ON order_status_history(order_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_order_history_status ON order_status_history(to_status, created_at DESC);

-- 给 customer_order 加 status 索引 (如果还没有)
CREATE INDEX IF NOT EXISTS idx_customer_order_status ON customer_order(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_customer_order_customer ON customer_order(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_customer_order_created ON customer_order(created_at DESC);
"""
