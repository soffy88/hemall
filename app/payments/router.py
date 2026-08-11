"""支付 API 路由 — 创建支付 + 回调处理 + 查询 + 退款。"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..deps import get_current_user, get_pool, get_settings
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
    # 鉴权先于取池解析, 否则 DB 不可达时匿名请求会先撞 503 而非 401。
    _principal: dict = Depends(get_current_user),
    pool: Any = Depends(get_pool),
) -> CreatePaymentResponse:
    """创建支付会话 → 获取支付二维码/链接。"""
    # 1. 创建 PaymentSession 记录
    payment_id = str(uuid.uuid4())
    amount_cents = int(body.amount * 100)  # 元 → 分

    qr_code = None
    code_url = None

    # 2. 调用第三方支付接口
    settings = get_settings()
    if body.provider == PaymentProvider.WECHAT:
        if settings.payment_gateway_provider == "wechat":
            # 补天 P0: 真实微信支付 v3 Native (平行替换)。密钥缺失时
            # bootstrap 已回退 manual，这里仍走统一契约。
            from obase.provider_registry import ProviderRegistry

            gw = ProviderRegistry.get().generic("payment_gateway", "wechat")
            prepay = await gw.prepay(
                out_trade_no=payment_id,
                total_fee_cents=amount_cents,
                description=body.description or f"order-{body.order_id}",
                notify_url=settings.wechat_pay_notify_url,
            )
            code_url = prepay.get("code_url")
        else:
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

    elif body.provider == PaymentProvider.STRIPE:
        from obase.provider_registry import ProviderRegistry

        gw = ProviderRegistry.get().generic("payment_gateway", "stripe")
        prepay = await gw.stripe_prepay(
            out_trade_no=payment_id,
            total_fee_cents=amount_cents,
            description=body.description or f"order-{body.order_id}",
        )
        code_url = prepay.get("client_secret")  # 前端 Stripe.js 用 client_secret

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
    _principal: dict = Depends(get_current_user),
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
    _principal: dict = Depends(get_current_user),
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
    settings = get_settings()
    if provider == PaymentProvider.WECHAT.value:
        if settings.payment_gateway_provider == "wechat":
            from obase.provider_registry import ProviderRegistry

            gw = ProviderRegistry.get().generic("payment_gateway", "wechat")
            result = await gw.refund(
                out_trade_no=body.payment_id,
                out_refund_no=f"refund_{uuid.uuid4().hex[:12]}",
                refund_fee_cents=refund_amount_cents,
                reason=body.reason,
                total_fee_cents=int(row["amount"]),
            )
        else:
            result = await _wechat_provider.refund(
                out_trade_no=body.payment_id,
                out_refund_no=f"refund_{uuid.uuid4().hex[:12]}",
                refund_amount=refund_amount_cents,
            )
    elif provider == PaymentProvider.STRIPE.value:
        from obase.provider_registry import ProviderRegistry

        gw = ProviderRegistry.get().generic("payment_gateway", "stripe")
        result = await gw.refund(
            out_trade_no=body.payment_id,
            out_refund_no=f"refund_{uuid.uuid4().hex[:12]}",
            refund_fee_cents=refund_amount_cents,
            reason=body.reason,
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


async def _settle_payment_and_order(
    pool: Any, session_id: str, transaction_id: str | None
) -> None:
    """支付成功回调的统一收尾：标记 payment_session 为 paid 且推进对应订单。

    幂等：仅当会话仍是 pending 时才翻转并推进订单 (RETURNING 拿 order_id);
    重复通知拿不到行 → 不重复确认订单。订单已非可确认态时只记日志不报错
    (避免回调因订单侧状态而失败, 让网关反复重推)。
    """
    if pool is None:
        return
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE payment_session SET status = 'paid', provider_trade_no = $1, "
            "updated_at = NOW() WHERE id = $2 AND status = 'pending' RETURNING order_id",
            transaction_id,
            session_id,
        )
    if row is None or not row.get("order_id"):
        return

    from ..orders.models import InvalidOrderTransitionError
    from ..orders.service import OrderNotFoundError, OrderService

    try:
        await OrderService(pool).confirm_order(
            str(row["order_id"]), payment_intent_id=transaction_id
        )
    except (InvalidOrderTransitionError, OrderNotFoundError) as exc:
        logger.info(
            "payment settled but order %s not confirmed (state/exists): %s",
            row["order_id"],
            exc,
        )
    except Exception as exc:  # noqa: BLE001 - 回调侧订单推进失败不应连累验签成功语义
        logger.warning("order confirm on payment notify failed: %s", exc)


@router.post("/wechat/notify")
async def wechat_pay_notify(request: Request) -> dict[str, str]:
    """微信支付回调通知处理。"""
    body = await request.body()
    headers = dict(request.headers)

    settings = get_settings()
    if settings.payment_gateway_provider == "wechat":
        # 补天 P0: 真实平台证书验签 + AES-GCM 解密。验签失败直接 400，
        # 微信侧会按失败重试；成功再落库。
        from obase.provider_registry import ProviderRegistry

        gw = ProviderRegistry.get().generic("payment_gateway", "wechat")
        notify = await gw.verify_callback(headers, body)
        if notify is None:
            raise HTTPException(status_code=400, detail="invalid wechat notify")
        out_trade_no = notify.get("out_trade_no") or notify.get("transaction_id")
        success = notify.get("trade_state") == "SUCCESS"
        if success and out_trade_no:
            pool = getattr(request.app.state, "pool", None)
            await _settle_payment_and_order(
                pool, out_trade_no, notify.get("transaction_id")
            )
        return {"code": "SUCCESS", "message": "ok"}

    notify = await _wechat_provider.handle_notify(headers, body)
    if notify is None:
        raise HTTPException(status_code=400, detail="invalid notify")

    if notify.is_success:
        pool = getattr(request.app.state, "pool", None)
        await _settle_payment_and_order(
            pool, notify.out_trade_no, notify.transaction_id
        )

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
        pool = getattr(request.app.state, "pool", None)
        await _settle_payment_and_order(pool, notify.out_trade_no, notify.trade_no)

    return "success"
