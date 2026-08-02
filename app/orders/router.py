"""订单管理 API 路由 — 查询、状态转换、历史追踪。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..deps import get_pool
from .models import OrderStatus

router = APIRouter(prefix="/orders", tags=["orders"])

logger = logging.getLogger("hemall.orders.router")


# ── 依赖注入 ────────────────────────────────────────────────────────


def get_order_service(pool: Any = Depends(get_pool)):
    from .service import OrderService

    return OrderService(pool)


# ── 请求/响应模型 ──────────────────────────────────────────────────


class ConfirmOrderRequest(BaseModel):
    """确认订单 (支付成功回调)。"""

    payment_intent_id: str | None = Field(None, description="支付流水号")


class CancelOrderRequest(BaseModel):
    """取消订单。"""

    reason: str = Field(default="user requested", description="取消原因")


class ShipOrderRequest(BaseModel):
    """发货。"""

    tracking_number: str | None = Field(None, description="物流单号")
    carrier: str | None = Field(None, description="物流公司")


# ── API 端点 ───────────────────────────────────────────────────────


@router.get("/")
async def list_orders(
    customer_id: str | None = Query(None, description="按客户过滤"),
    status: str | None = Query(None, description="按状态过滤"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    svc: Any = Depends(get_order_service),
) -> dict[str, Any]:
    """订单列表。"""
    order_status = OrderStatus(status) if status else None
    orders = await svc.list_orders(customer_id, order_status, limit, offset)
    total = await svc.count_orders(customer_id, order_status)

    return {
        "orders": [o.model_dump(mode="json") for o in orders],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{order_id}")
async def get_order(
    order_id: str,
    svc: Any = Depends(get_order_service),
) -> dict[str, Any]:
    """获取订单详情 + 行项目。"""
    from .service import OrderNotFoundError

    try:
        order = await svc.get_order(order_id)
    except OrderNotFoundError:
        raise HTTPException(status_code=404, detail="order not found")

    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    items = await svc.get_order_items(order_id)

    return {
        "order": order.model_dump(mode="json"),
        "items": [dict(id=str(i["id"]), quantity=i["quantity"], unit_price_cents=i["unit_price_cents"],
                       line_total_cents=i["line_total_cents"], product_id=i.get("product_id"),
                       variant_id=i.get("variant_id")) for i in items],
    }


@router.post("/{order_id}/confirm")
async def confirm_order(
    order_id: str,
    body: ConfirmOrderRequest | None = None,
    svc: Any = Depends(get_order_service),
) -> dict[str, Any]:
    """确认订单 (支付成功 → confirmed)。"""
    from .models import InvalidOrderTransitionError
    from .service import OrderNotFoundError

    try:
        order = await svc.confirm_order(
            order_id,
            payment_intent_id=body.payment_intent_id if body else None,
        )
        return {"status": "confirmed", "order": order.model_dump(mode="json")}
    except OrderNotFoundError:
        raise HTTPException(status_code=404, detail="order not found")
    except InvalidOrderTransitionError as e:
        raise HTTPException(
            status_code=409,
            detail=f"cannot confirm: order is {e.from_status.value}",
        )


@router.post("/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    body: CancelOrderRequest | None = None,
    svc: Any = Depends(get_order_service),
) -> dict[str, Any]:
    """取消订单 (自动释放库存)。"""
    from .models import InvalidOrderTransitionError
    from .service import OrderNotFoundError

    try:
        reason = body.reason if body else "user requested"
        order = await svc.cancel_order(order_id, reason=reason)
        return {"status": "cancelled", "order": order.model_dump(mode="json")}
    except OrderNotFoundError:
        raise HTTPException(status_code=404, detail="order not found")
    except InvalidOrderTransitionError as e:
        raise HTTPException(
            status_code=409,
            detail=f"cannot cancel: order is {e.from_status.value}",
        )


@router.post("/{order_id}/ship")
async def ship_order(
    order_id: str,
    body: ShipOrderRequest | None = None,
    svc: Any = Depends(get_order_service),
) -> dict[str, Any]:
    """发货 (自动扣减库存)。"""
    from .models import InvalidOrderTransitionError
    from .service import OrderNotFoundError

    try:
        order = await svc.ship_order(
            order_id,
            tracking_number=body.tracking_number if body else None,
            carrier=body.carrier if body else None,
        )
        return {"status": "shipped", "order": order.model_dump(mode="json")}
    except OrderNotFoundError:
        raise HTTPException(status_code=404, detail="order not found")
    except InvalidOrderTransitionError as e:
        raise HTTPException(
            status_code=409,
            detail=f"cannot ship: order is {e.from_status.value}",
        )


@router.post("/{order_id}/deliver")
async def deliver_order(
    order_id: str,
    svc: Any = Depends(get_order_service),
) -> dict[str, Any]:
    """标记送达。"""
    from .models import InvalidOrderTransitionError
    from .service import OrderNotFoundError

    try:
        order = await svc.deliver_order(order_id)
        return {"status": "delivered", "order": order.model_dump(mode="json")}
    except OrderNotFoundError:
        raise HTTPException(status_code=404, detail="order not found")
    except InvalidOrderTransitionError as e:
        raise HTTPException(
            status_code=409,
            detail=f"cannot deliver: order is {e.from_status.value}",
        )


@router.post("/{order_id}/complete")
async def complete_order(
    order_id: str,
    svc: Any = Depends(get_order_service),
) -> dict[str, Any]:
    """确认收货 (delivered → completed)。"""
    from .models import InvalidOrderTransitionError
    from .service import OrderNotFoundError

    try:
        order = await svc.complete_order(order_id)
        return {"status": "completed", "order": order.model_dump(mode="json")}
    except OrderNotFoundError:
        raise HTTPException(status_code=404, detail="order not found")
    except InvalidOrderTransitionError as e:
        raise HTTPException(
            status_code=409,
            detail=f"cannot complete: order is {e.from_status.value}",
        )


@router.get("/{order_id}/history")
async def order_history(
    order_id: str,
    svc: Any = Depends(get_order_service),
) -> list[dict[str, Any]]:
    """订单状态变更历史。"""
    history = await svc.get_status_history(order_id)
    return [h.model_dump(mode="json") for h in history]


@router.get("/stats/summary")
async def order_stats(
    customer_id: str | None = Query(None),
    svc: Any = Depends(get_order_service),
) -> dict[str, Any]:
    """按状态统计订单数量。"""
    stats = await svc.get_order_stats(customer_id)
    return {"stats": stats}
