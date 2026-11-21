"""库存管理 API 路由 — 库存查询、预留/扣减/入库/盘点、安全库存预警。"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..deps import get_current_user, get_pool

# 后台库存管理 API：预留/扣减/入库/盘点直接改动可售库存 (可超卖或清空)，
# 一律要求员工 JWT。零登录商城只读展示走 /store/products。
router = APIRouter(
    prefix="/inventory", tags=["inventory"], dependencies=[Depends(get_current_user)]
)

logger = logging.getLogger("hemall.inventory.router")


# ── 依赖注入 ────────────────────────────────────────────────────────


def get_inventory_service(request: Request, pool: Any = Depends(get_pool)):
    """延迟导入 InventoryService (避免循环依赖)，注入事件引擎用于安全库存告警派发。"""
    from .service import InventoryService

    event_engine = getattr(request.app.state, "events", None)
    return InventoryService(pool, event_engine)


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
    reservation_key: str | None = Field(None, description="重试幂等键")


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


class UpdateInspectionRequest(BaseModel):
    """批次质检状态变更请求。"""

    inspection_status: Literal["passed", "failed", "pending"] = Field(
        ..., description="质检状态: passed 通过 / failed 不通过 / pending 待检"
    )


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
        allocations = await svc.reserve_stock_allocations(
            order_id=body.order_id,
            product_id=body.product_id,
            quantity=body.quantity,
            location_id=body.location_id,
            variant_id=body.variant_id,
            reservation_key=body.reservation_key,
        )
        return {
            "status": "reserved",
            "order_id": body.order_id,
            "reserved_qty": body.quantity,
            "batch_ids": [str(item["batch_id"]) for item in allocations],
            "allocations": allocations,
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
        ) from e


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
    return await svc.get_movement_history(product_id, location_id, variant_id, limit, offset)


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


@router.get("/batches")
async def list_batches(
    inspection_status: str | None = Query(None, description="按质检状态过滤"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    pool: Any = Depends(get_pool),
) -> list[dict[str, Any]]:
    """批次列表（后台质检用）：含商品/变体/仓位名称与质检状态，新批次在前。"""
    clauses = ["b.deleted_at IS NULL"]
    params: list[Any] = []
    if inspection_status is not None:
        params.append(inspection_status)
        clauses.append(f"b.inspection_status = ${len(params)}")
    params.append(limit)
    limit_idx = len(params)
    params.append(offset)
    offset_idx = len(params)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT b.id, b.batch_no, b.variant_id, pv.product_id,
                   p.title AS product_title, pv.sku_code,
                   b.location_id, sl.name AS location_name,
                   b.stock_qty, b.reserved_qty,
                   GREATEST(b.stock_qty - b.reserved_qty, 0) AS available_qty,
                   b.status, b.inspection_status, b.created_at
            FROM inventory_batch b
            JOIN product_variant pv ON pv.id = b.variant_id
            JOIN product p ON p.id = pv.product_id
            LEFT JOIN stock_location sl ON sl.id = b.location_id
            WHERE {" AND ".join(clauses)}
            ORDER BY b.created_at DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
            """,
            *params,
        )
    return [dict(r) for r in rows]


@router.patch("/batches/{batch_id}/inspection")
async def update_batch_inspection(
    batch_id: str,
    body: UpdateInspectionRequest,
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    """变更批次质检状态（质检门的人工通道，此前全系统无任何写入口）。

    待检（pending）批次通过后即可正常售卖与配货；置 failed 后加购与
    结算都拒绝；不存在 404，软删批次拒绝变更。
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT deleted_at FROM inventory_batch WHERE id = $1", batch_id)
        if row is None:
            raise HTTPException(status_code=404, detail="batch not found")
        if row["deleted_at"] is not None:
            raise HTTPException(status_code=400, detail="batch is deleted")
        updated = await conn.fetchrow(
            "UPDATE inventory_batch SET inspection_status = $1, updated_at = NOW() "
            "WHERE id = $2 RETURNING id, inspection_status",
            body.inspection_status,
            batch_id,
        )
    return {"id": str(updated["id"]), "inspection_status": updated["inspection_status"]}
