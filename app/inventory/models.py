"""库存数据模型 — 出入库流水、库存快照、安全库存预警。"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


# ── 库存变动类型 ─────────────────────────────────────────────────────


class StockMovementType(str, enum.Enum):
    """库存变动类型。"""

    # 入库
    RECEIPT = "receipt"  # 采购入库
    RETURN_IN = "return_in"  # 退货入库
    TRANSFER_IN = "transfer_in"  # 调拨入库
    ADJUSTMENT_UP = "adjustment_up"  # 盘点上调

    # 出库
    SHIPMENT = "shipment"  # 发货出库
    RETURN_OUT = "return_out"  # 退货出库
    TRANSFER_OUT = "transfer_out"  # 调拨出库
    ADJUSTMENT_DOWN = "adjustment_down"  # 盘点下调
    DAMAGE = "damage"  # 报损

    # 预留 (不改变库存, 改变 reserved_qty)
    RESERVE = "reserve"  # 订单预留
    UNRESERVE = "unreserve"  # 释放预留

    @property
    def is_inbound(self) -> bool:
        return self in (
            StockMovementType.RECEIPT,
            StockMovementType.RETURN_IN,
            StockMovementType.TRANSFER_IN,
            StockMovementType.ADJUSTMENT_UP,
        )

    @property
    def is_outbound(self) -> bool:
        return self in (
            StockMovementType.SHIPMENT,
            StockMovementType.RETURN_OUT,
            StockMovementType.TRANSFER_OUT,
            StockMovementType.ADJUSTMENT_DOWN,
            StockMovementType.DAMAGE,
        )

    @property
    def is_reservation(self) -> bool:
        return self in (
            StockMovementType.RESERVE,
            StockMovementType.UNRESERVE,
        )


# ── 数据模型 ─────────────────────────────────────────────────────────


class StockMovement(BaseModel):
    """库存出入库流水记录。"""

    id: str | None = None
    product_id: str
    variant_id: str | None = None
    location_id: str
    batch_id: str | None = None
    movement_type: StockMovementType
    quantity: int = Field(..., gt=0, description="变动数量 (正数=入库, 负数=出库)")
    unit_cost: Decimal | None = Field(None, description="单位成本 (元)")
    reference_type: str | None = Field(
        None, description="关联单据类型 (order/transfer/adjustment/purchase)"
    )
    reference_id: str | None = Field(None, description="关联单据 ID")
    reason: str | None = Field(None, description="变动原因/备注")
    operator_id: str | None = Field(None, description="操作人 ID")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def net_quantity(self) -> int:
        """净变动量 (入库为正, 出库为负)。"""
        if self.is_inbound:
            return self.quantity
        elif self.is_outbound:
            return -self.quantity
        return 0  # reservation 类型不影响库存

    @property
    def is_inbound(self) -> bool:
        return self.movement_type.is_inbound

    @property
    def is_outbound(self) -> bool:
        return self.movement_type.is_outbound

    @property
    def is_reservation(self) -> bool:
        return self.movement_type.is_reservation


class StockSnapshot(BaseModel):
    """实时库存快照。"""

    product_id: str
    variant_id: str | None = None
    location_id: str
    location_name: str | None = None
    total_qty: int = Field(..., ge=0, description="总库存")
    available_qty: int = Field(..., ge=0, description="可用库存 (total - reserved)")
    reserved_qty: int = Field(..., ge=0, description="已预留")
    batch_id: str | None = None
    product_name: str | None = None
    sku: str | None = None


class StockAlert(BaseModel):
    """安全库存预警。"""

    product_id: str
    variant_id: str | None = None
    location_id: str
    safety_threshold: int
    current_available: int
    alert_type: str = Field(
        ..., description="critical (<=0), warning (<=threshold), normal (>threshold)"
    )


# ── DDL ──────────────────────────────────────────────────────────────


STOCK_MOVEMENT_DDL = """
CREATE TABLE IF NOT EXISTS stock_movement (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id TEXT NOT NULL,
    variant_id TEXT,
    location_id UUID NOT NULL REFERENCES stock_location(id),
    batch_id UUID REFERENCES inventory_batch(id),
    movement_type TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_cost DECIMAL(12,2),
    reference_type TEXT,
    reference_id TEXT,
    reason TEXT,
    operator_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stock_mov_product ON stock_movement(product_id, variant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stock_mov_location ON stock_movement(location_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stock_mov_reference ON stock_movement(reference_type, reference_id);
CREATE INDEX IF NOT EXISTS idx_stock_mov_type ON stock_movement(movement_type, created_at DESC);

-- 安全库存阈值表
CREATE TABLE IF NOT EXISTS product_safety_stock (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id TEXT NOT NULL,
    variant_id TEXT,
    location_id UUID REFERENCES stock_location(id),
    safety_threshold INTEGER NOT NULL DEFAULT 10 CHECK (safety_threshold >= 0),
    alert_email TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (product_id, variant_id, location_id)
);
"""
