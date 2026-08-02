"""库存管理 API 路由 — 库存查询、预留/扣减/入库/盘点、安全库存预警。"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..deps import get_pool

router = APIRouter(prefix="/inventory", tags=["inventory"])

logger = logging.getLogger("hemall.inventory.router")


# ── 依赖注入 ────────────────────────────────────────────────────────


def get_inventory_service(pool: Any = Depends(get_pool)):
    """延迟导入 InventoryService (避免循环依赖)。"""
    from .service import InventoryService

    return InventoryService(pool)


# ── 请求/响应模型 ──────────────────────────────────────────────────


class StockQueryRequest(BaseModel):
    """库存查询请求。"""

    product_id: str
    variant_id: str | None = None
    location_id: str


class ReserveStockRequest(BaseModel):
    """预留库存请求。"""

    order_id: str = Field(..., description="订单 ID")
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(..., gt=0, description="预留数量")
    location_id: str = Field(..., description="仓库 ID")


class DeductStockRequest(BaseModel):
    """扣减库存请求 (发货时调用)。"""

    order_id: str
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(..., gt=0, description="扣减数量")
    location_id: str


class ReleaseReservationRequest(BaseModel):
    """释放预留请求 (取消订单时调用)。"""

    order_id: str
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(..., gt=0, description="释放数量")
    location_id: str


class ReceiveStockRequest(BaseModel):
    """入库请求。"""

    product_id: str
    variant_id: str | None = None
    batch_id: str = Field(..., description="入库批次 ID")
    quantity: int = Field(..., gt=0, description="入库数量")
    location_id: str
    movement_type: str = Field(
        default="receipt",
        description="入库类型: receipt/return_in/transfer_in/adjustment_up",
    )
    unit_cost: Decimal | None = Field(None, description="单位成本")
    reference_type: str | None = None
    reference_id: str | None = None
    reason: str | None = None
    operator_id: str | None = None


class SetSafetyThresholdRequest(BaseModel):
    """设置安全库存阈值。"""

    product_id: str
    variant_id: str | None = None
    location_id: str
    threshold: int = Field(..., ge=0, description="安全库存阈值")
    alert_email: str | None = None


# ── API 端点 ───────────────────────────────────────────────────────


@router.get("/stock")
async def query_stock(
    product_id: str,
    location_id: str,
    variant_id: str | None = None,
    svc: Any = Depends(get_inventory_service),
) -> list[dict[str, Any]]:
    """查询商品在指定仓库的库存快照 (按批次)。"""
    snapshots = await svc.get_stock(product_id, location_id, variant_id)
    return [s.model_dump() for s in snapshots]


@router.get("/stock/available")
async def query_available_stock(
    product_id: str,
    location_id: str,
    variant_id: str | None = None,
    svc: Any = Depends(get_inventory_service),
) -> dict[str, Any]:
    """查询可用库存总量。"""
    available = await svc.get_available_stock(product_id, location_id, variant_id)
    return {
        "product_id": product_id,
        "variant_id": variant_id,
        "location_id": location_id,
        "available_qty": available,
    }


@router.get("/stock/summary")
async def stock_summary(
    product_id: str | None = Query(None),
    location_id: str | None = Query(None),
    low_stock_only: bool = Query(False, description="只返回低库存商品"),
    svc: Any = Depends(get_inventory_service),
) -> list[dict[str, Any]]:
    """库存汇总 (跨批次/跨仓库)。"""
    return await svc.get_stock_summary(product_id, location_id, low_stock_only)


@router.post("/reserve")
async def reserve_stock(
    body: ReserveStockRequest,
    svc: Any = Depends(get_inventory_service),
) -> dict[str, Any]:
    """为订单预留库存 (FIFO: 老批次优先)。

    库存不足时返回 409 Conflict。
    """
    from .service import InsufficientStockError

    try:
        batches = await svc.reserve_stock(
            order_id=body.order_id,
            product_id=body.product_id,
            quantity=body.quantity,
            location_id=body.location_id,
            variant_id=body.variant_id,
        )
        return {
            "status": "reserved",
            "order_id": body.order_id,
            "reserved_qty": body.quantity,
            "batch_ids": batches,
        }
    except InsufficientStockError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "insufficient_stock",
                "product_id": e.product_id,
                "requested": e.requested,
                "available": e.available,
            },
        )


@router.post("/deduct")
async def deduct_stock(
    body: DeductStockRequest,
    svc: Any = Depends(get_inventory_service),
) -> dict[str, Any]:
    """扣减已预留库存 (发货时调用)。"""
    await svc.deduct_stock(
        order_id=body.order_id,
        product_id=body.product_id,
        quantity=body.quantity,
        location_id=body.location_id,
        variant_id=body.variant_id,
    )
    return {
        "status": "deducted",
        "order_id": body.order_id,
        "deducted_qty": body.quantity,
    }


@router.post("/release")
async def release_reservation(
    body: ReleaseReservationRequest,
    svc: Any = Depends(get_inventory_service),
) -> dict[str, Any]:
    """释放订单预留 (取消订单/超时未支付时调用)。"""
    await svc.release_reservation(
        order_id=body.order_id,
        product_id=body.product_id,
        quantity=body.quantity,
        location_id=body.location_id,
        variant_id=body.variant_id,
    )
    return {
        "status": "released",
        "order_id": body.order_id,
        "released_qty": body.quantity,
    }


@router.post("/receive")
async def receive_stock(
    body: ReceiveStockRequest,
    svc: Any = Depends(get_inventory_service),
) -> dict[str, Any]:
    """入库操作 (采购入库/退货入库/调拨入库)。"""
    from .models import StockMovementType

    movement_type = StockMovementType(body.movement_type)
    if not movement_type.is_inbound:
        raise HTTPException(
            status_code=400,
            detail=f"movement_type must be inbound, got {body.movement_type}",
        )

    movement_id = await svc.receive_stock(
        product_id=body.product_id,
        quantity=body.quantity,
        location_id=body.location_id,
        movement_type=movement_type,
        variant_id=body.variant_id,
        batch_id=body.batch_id,
        unit_cost=float(body.unit_cost) if body.unit_cost else None,
        reference_type=body.reference_type,
        reference_id=body.reference_id,
        reason=body.reason,
        operator_id=body.operator_id,
    )
    return {
        "status": "received",
        "movement_id": movement_id,
        "received_qty": body.quantity,
    }


@router.get("/movements/{product_id}")
async def movement_history(
    product_id: str,
    location_id: str | None = Query(None),
    variant_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    svc: Any = Depends(get_inventory_service),
) -> list[dict[str, Any]]:
    """查询库存变动历史。"""
    return await svc.get_movement_history(
        product_id, location_id, variant_id, limit, offset
    )


@router.get("/alerts")
async def check_stock_alerts(
    svc: Any = Depends(get_inventory_service),
) -> list[dict[str, Any]]:
    """检查所有商品的安全库存预警。"""
    alerts = await svc.check_all_safety_stock()
    return [a.model_dump() for a in alerts]


@router.put("/safety-threshold")
async def set_safety_threshold(
    body: SetSafetyThresholdRequest,
    svc: Any = Depends(get_inventory_service),
) -> dict[str, Any]:
    """设置商品的安全库存阈值。"""
    await svc.set_safety_threshold(
        product_id=body.product_id,
        location_id=body.location_id,
        threshold=body.threshold,
        variant_id=body.variant_id,
        alert_email=body.alert_email,
    )
    return {
        "status": "updated",
        "product_id": body.product_id,
        "location_id": body.location_id,
        "threshold": body.threshold,
    }
