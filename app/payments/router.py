"""支付 API 路由 — 创建支付 + 回调处理 + 查询 + 退款。"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..deps import get_pool, get_settings
from .alipay import AlipayOrderRequest, AlipayProvider
from .models import (
    PaymentProvider,
    PaymentSession,
    PaymentStatus,
    validate_transition,
)
from .wechat import WeChatPayOrderRequest, WeChatPayProvider

logger = logging.getLogger("hemall.payments.router")

router = APIRouter(prefix="/payments", tags=["payments"])

# ── Provider 单例 ────────────────────────────────────────────────────

_wechat_provider = WeChatPayProvider()
_alipay_provider = AlipayProvider()


# ── 请求/响应模型 ─────────────────────────────────────────────────────


class CreatePaymentRequest(BaseModel):
    """创建支付会话。"""

    order_id: str = Field(..., description="业务订单 ID")
    amount: Decimal = Field(..., gt=0, description="支付金额 (元)")
    provider: PaymentProvider = Field(..., description="支付渠道")
    description: str = Field("", description="商品描述")
    payer_openid: str | None = Field(None, description="微信 openid (微信支付时)")


class CreatePaymentResponse(BaseModel):
    """创建支付会话响应。"""

    payment_id: str
    status: str
    qr_code: str | None = None
    code_url: str | None = None
    expires_at: str | None = None


class PaymentQueryResponse(BaseModel):
    """支付查询响应。"""

    payment_id: str
    order_id: str
    amount: str
    status: str
    provider: str
    provider_trade_no: str | None = None


class RefundRequest(BaseModel):
    """退款请求。"""

    payment_id: str
    refund_amount: Decimal | None = Field(
        None, description="退款金额 (元), None=全额退款"
    )
    reason: str = Field("", description="退款原因")


# ── API 端点 ─────────────────────────────────────────────────────────


@router.post("/create", response_model=CreatePaymentResponse)
async def create_payment(
    body: CreatePaymentRequest,
    request: Request,
    pool: Any = Depends(get_pool),
) -> CreatePaymentResponse:
    """创建支付会话 → 获取支付二维码/链接。"""
    # 1. 创建 PaymentSession 记录
    payment_id = str(uuid.uuid4())
    amount_cents = int(body.amount * 100)  # 元 → 分

    qr_code = None
    code_url = None

    # 2. 调用第三方支付接口
    if body.provider == PaymentProvider.WECHAT:
        resp = await _wechat_provider.create_order(
            WeChatPayOrderRequest(
                out_trade_no=payment_id,
                description=body.description or f"order-{body.order_id}",
                total_amount=amount_cents,
                payer_openid=body.payer_openid,
            )
        )
        if not resp.success:
            raise HTTPException(
                status_code=502,
                detail=f"wechat pay create failed: {resp.err_msg}",
            )
        code_url = resp.code_url

    elif body.provider == PaymentProvider.ALIPAY:
        resp = await _alipay_provider.create_order(
            AlipayOrderRequest(
                out_trade_no=payment_id,
                subject=body.description or f"order-{body.order_id}",
                total_amount=str(body.amount),
            )
        )
        if not resp.success:
            raise HTTPException(
                status_code=502,
                detail=f"alipay create failed: {resp.err_msg}",
            )
        qr_code = resp.qr_code

    elif body.provider == PaymentProvider.MANUAL:
        # 手动模式: 不生成二维码
        pass

    # 3. 写入数据库
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO payment_session (id, order_id, amount, provider, status, metadata)
                VALUES ($1, $2, $3, $4, 'pending', $5)
                """,
                payment_id,
                body.order_id,
                amount_cents,
                body.provider.value,
                '{"description": "' + (body.description or "") + '"}',
            )
    except Exception as exc:
        logger.warning("payment session DB write failed (non-fatal): %s", exc)

    return CreatePaymentResponse(
        payment_id=payment_id,
        status="pending",
        qr_code=qr_code,
        code_url=code_url,
    )


@router.get("/query/{payment_id}", response_model=PaymentQueryResponse)
async def query_payment(
    payment_id: str,
    pool: Any = Depends(get_pool),
) -> PaymentQueryResponse:
    """查询支付状态。"""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, order_id, amount, status, provider, provider_trade_no "
                "FROM payment_session WHERE id = $1",
                payment_id,
            )
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")

    if row is None:
        raise HTTPException(status_code=404, detail="payment not found")

    return PaymentQueryResponse(
        payment_id=str(row["id"]),
        order_id=row["order_id"],
        amount=str(Decimal(row["amount"]) / 100),
        status=row["status"],
        provider=row["provider"],
        provider_trade_no=row["provider_trade_no"],
    )


@router.post("/refund")
async def refund_payment(
    body: RefundRequest,
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    """申请退款。"""
    # 查询支付会话
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, amount, status, provider, provider_trade_no "
                "FROM payment_session WHERE id = $1",
                body.payment_id,
            )
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")

    if row is None:
        raise HTTPException(status_code=404, detail="payment not found")

    current_status = PaymentStatus(row["status"])
    if current_status not in (PaymentStatus.PAID, PaymentStatus.PARTIAL_REFUND):
        raise HTTPException(
            status_code=409,
            detail=f"cannot refund: current status is {current_status.value}",
        )

    # 调用退款
    refund_amount = body.refund_amount or Decimal(row["amount"]) / 100
    refund_amount_cents = int(refund_amount * 100)

    provider = row["provider"]
    if provider == PaymentProvider.WECHAT.value:
        result = await _wechat_provider.refund(
            out_trade_no=body.payment_id,
            out_refund_no=f"refund_{uuid.uuid4().hex[:12]}",
            refund_amount=refund_amount_cents,
        )
    elif provider == PaymentProvider.ALIPAY.value:
        result = await _alipay_provider.refund(
            out_trade_no=body.payment_id,
            refund_amount=str(refund_amount),
        )
    else:
        result = {"status": "SUCCESS"}  # manual

    # 更新数据库状态
    new_status = (
        PaymentStatus.PARTIAL_REFUND
        if refund_amount_cents < row["amount"]
        else PaymentStatus.REFUNDED
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE payment_session SET status = $1, refund_amount = $2, "
                "updated_at = NOW() WHERE id = $3",
                new_status.value,
                refund_amount_cents,
                body.payment_id,
            )
    except Exception as exc:
        logger.warning("refund DB update failed (non-fatal): %s", exc)

    return {
        "payment_id": body.payment_id,
        "status": new_status.value,
        "refund_amount": str(refund_amount),
        "provider_result": result,
    }


@router.post("/wechat/notify")
async def wechat_pay_notify(request: Request) -> dict[str, str]:
    """微信支付回调通知处理。"""
    body = await request.body()
    headers = dict(request.headers)

    notify = await _wechat_provider.handle_notify(headers, body)
    if notify is None:
        raise HTTPException(status_code=400, detail="invalid notify")

    if notify.is_success:
        # 更新支付会话状态
        try:
            pool = getattr(request.app.state, "pool", None)
            if pool is not None:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE payment_session SET status = 'paid', "
                        "provider_trade_no = $1, updated_at = NOW() "
                        "WHERE id = $2 AND status = 'pending'",
                        notify.transaction_id,
                        notify.out_trade_no,
                    )
        except Exception as exc:
            logger.warning("wechat notify DB update failed: %s", exc)

    return {"code": "SUCCESS", "message": "ok"}


@router.post("/alipay/notify")
async def alipay_notify(request: Request) -> str:
    """支付宝异步通知处理。"""
    form = await request.form()
    params = dict(form)

    notify = await _alipay_provider.handle_notify(params)
    if notify is None:
        return "fail"

    if notify.is_success:
        try:
            pool = getattr(request.app.state, "pool", None)
            if pool is not None:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE payment_session SET status = 'paid', "
                        "provider_trade_no = $1, updated_at = NOW() "
                        "WHERE id = $2 AND status = 'pending'",
                        notify.trade_no,
                        notify.out_trade_no,
                    )
        except Exception as exc:
            logger.warning("alipay notify DB update failed: %s", exc)

    return "success"
