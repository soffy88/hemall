"""售后/退款 API 路由 — 顾客申请 + 后台审核处理 + 网关退款回调。

端点:
- POST   /aftersales/refunds                      顾客发起售后申请
- GET    /aftersales/refunds/my                   顾客查看自己的售后
- GET    /aftersales/refunds                      后台列出售后 (status 过滤)
- POST   /aftersales/refunds/{id}/approve         后台审核通过 (自动触发网关退款)
- POST   /aftersales/refunds/{id}/reject          后台驳回
- POST   /aftersales/refunds/{id}/retry           后台重试失败的退款
- POST   /aftersales/refunds/{id}/return-received 后台确认收货 (回补库存, 幂等)
- GET    /aftersales/refunds/{id}/audit           后台查看审计日志
- POST   /aftersales/refunds/gateway/notify       网关退款回调 (验签, 无会话鉴权)

挂在 /aftersales 顶层前缀下 (而非新建 /refunds): 网关 Caddyfile/nginx 的
反代白名单已覆盖 /aftersales, 无需改任何网关配置。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..deps import get_current_customer, get_pool, get_settings, require_staff_role
from .refund_models import RefundReason, RefundStatus, RefundType
from .refund_service import (
    GatewayRefundError,
    RefundError,
    _refund_to_dict,
    approve_refund,
    create_refund_request,
    get_refund_audit_logs,
    list_refund_requests,
    mark_refund_returned,
    process_refund_callback,
    reject_refund,
    retry_refund,
)

logger = logging.getLogger("hemall.payments.refund_router")

router = APIRouter(prefix="/aftersales/refunds", tags=["refunds"])


# ── 请求模型 ─────────────────────────────────────────────────────────────


class RefundCreateBody(BaseModel):
    """顾客发起售后申请。"""

    order_id: str
    order_line_id: str | None = Field(None, description="部分退款时填订单行 ID")
    type: RefundType = RefundType.REFUND_ONLY
    reason: RefundReason = RefundReason.OTHER
    reason_detail: str | None = None
    requested_amount: int = Field(..., gt=0, description="申请退款金额 (分)")


class RefundApproveBody(BaseModel):
    approved_amount: int | None = Field(None, gt=0, description="审核通过金额 (分), 缺省=申请金额")


class RefundRejectBody(BaseModel):
    reject_reason: str = Field(..., min_length=1)


class RefundReturnBody(BaseModel):
    tracking_no: str = Field(..., min_length=1, description="退货物流单号")
    batch_id: str = Field(..., min_length=1, description="回补库存的批次 ID")


# ── 顾客端 ─────────────────────────────────────────────────────────────


@router.post("")
async def create_refund(
    body: RefundCreateBody,
    customer: dict = Depends(get_current_customer),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    """顾客对已支付订单发起售后申请 (金额不超过可退额度)。"""
    try:
        refund = await create_refund_request(
            pool,
            order_id=body.order_id,
            customer_id=str(customer["customer_id"]),
            type=body.type,
            reason=body.reason,
            reason_detail=body.reason_detail,
            requested_amount=body.requested_amount,
            order_line_id=body.order_line_id,
        )
    except RefundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _refund_to_dict(refund)


@router.get("/my")
async def my_refunds(
    customer: dict = Depends(get_current_customer),
    pool: Any = Depends(get_pool),
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """顾客查看自己的售后申请。"""
    refunds = await list_refund_requests(
        pool, customer_id=str(customer["customer_id"]), limit=limit, offset=offset
    )
    return [_refund_to_dict(r) for r in refunds]


# ── 后台 ───────────────────────────────────────────────────────────────


@router.get("")
async def list_refunds(
    status: RefundStatus | None = None,
    order_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    _principal: dict = Depends(require_staff_role("admin", "support")),
    pool: Any = Depends(get_pool),
) -> list[dict[str, Any]]:
    """后台列出售后申请 (按状态/订单过滤)。"""
    refunds = await list_refund_requests(
        pool, status=status, order_id=order_id, limit=limit, offset=offset
    )
    return [_refund_to_dict(r) for r in refunds]


@router.post("/{refund_id}/approve")
async def approve_refund_endpoint(
    refund_id: str,
    body: RefundApproveBody,
    principal: dict = Depends(require_staff_role("admin")),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    """后台审核通过 → 自动触发网关退款。"""
    try:
        refund = await approve_refund(
            pool, refund_id, str(principal["user_id"]), approved_amount=body.approved_amount
        )
    except RefundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _refund_to_dict(refund)


@router.post("/{refund_id}/reject")
async def reject_refund_endpoint(
    refund_id: str,
    body: RefundRejectBody,
    principal: dict = Depends(require_staff_role("admin")),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    """后台驳回售后申请。"""
    try:
        refund = await reject_refund(pool, refund_id, str(principal["user_id"]), body.reject_reason)
    except RefundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _refund_to_dict(refund)


@router.post("/{refund_id}/retry")
async def retry_refund_endpoint(
    refund_id: str,
    principal: dict = Depends(require_staff_role("admin")),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    """后台重试失败的退款。"""
    try:
        refund = await retry_refund(pool, refund_id)
    except RefundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _refund_to_dict(refund)


@router.post("/{refund_id}/return-received")
async def return_received_endpoint(
    refund_id: str,
    body: RefundReturnBody,
    principal: dict = Depends(require_staff_role("admin")),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    """后台确认收到退货 → 回补库存 (幂等, 重复收货不会双倍回补)。"""
    try:
        refund = await mark_refund_returned(
            pool,
            refund_id,
            body.tracking_no,
            body.batch_id,
            str(principal["user_id"]),
        )
    except RefundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _refund_to_dict(refund)


@router.get("/{refund_id}/audit")
async def refund_audit_endpoint(
    refund_id: str,
    _principal: dict = Depends(require_staff_role("admin", "support")),
    pool: Any = Depends(get_pool),
) -> list[dict[str, Any]]:
    """后台查看售后审计日志。"""
    logs = await get_refund_audit_logs(pool, refund_id)
    return [
        {
            "id": log.id,
            "refund_id": log.refund_id,
            "from_status": log.from_status.value if log.from_status else None,
            "to_status": log.to_status.value,
            "operator_id": log.operator_id,
            "operator_type": log.operator_type,
            "reason": log.reason,
            "metadata": log.metadata,
            "created_at": log.created_at.isoformat(),
        }
        for log in logs
    ]


# ── 网关退款回调 (验签, 无会话鉴权 — 网关没有用户令牌) ──────────────────


@router.post("/gateway/notify")
async def refund_gateway_notify(
    request: Request,
    pool: Any = Depends(get_pool),
    settings=Depends(get_settings),
) -> dict[str, Any]:
    """支付网关退款回调: 先验签 (伪造签名直接拒), 再推进本地状态机。"""
    body = await request.body()
    headers = dict(request.headers)
    provider = settings.payment_gateway_provider.strip().lower()
    try:
        result = await process_refund_callback(
            pool, provider=provider, headers=headers, body=body, settings=settings
        )
    except GatewayRefundError as exc:
        logger.warning("refund callback rejected: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result
