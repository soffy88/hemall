"""支付 API 路由 — 创建支付 + 回调处理 + 查询 + 退款。"""

from __future__ import annotations

import json
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
from .service import (
    PaymentAmountMismatchError,
    PaymentNotFoundError,
    PaymentService,
    PaymentServiceError,
    PaymentStateError,
    RefundConflictError,
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
    # 兼容旧客户端字段，但它只用于服务端篡改检测，永远不参与金额计算。
    amount: Decimal | None = Field(
        None,
        gt=0,
        description="已弃用：服务端从订单快照计算金额，不信任客户端值",
    )
    provider: PaymentProvider = Field(..., description="支付渠道")
    description: str = Field("", description="商品描述")
    payer_openid: str | None = Field(None, description="微信 openid (微信支付时)")


class CreatePaymentResponse(BaseModel):
    """创建支付会话响应。"""

    payment_id: str
    order_id: str
    amount: str
    currency: str
    status: str
    qr_code: str | None = None
    code_url: str | None = None
    expires_at: str | None = None
    provider_result: dict[str, Any] | None = None


class PaymentQueryResponse(BaseModel):
    """支付查询响应。"""

    payment_id: str
    order_id: str
    amount: str
    currency: str = "CNY"
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
    idempotency_key: str | None = Field(
        None, description="退款幂等键；省略时按 payment_id+amount 派生"
    )


# ── API 端点 ─────────────────────────────────────────────────────────


@router.post("/create", response_model=CreatePaymentResponse)
async def create_payment(
    body: CreatePaymentRequest,
    request: Request,
    # 鉴权先于取池解析, 否则 DB 不可达时匿名请求会先撞 503 而非 401。
    _principal: dict = Depends(get_current_user),
    pool: Any = Depends(get_pool),
) -> CreatePaymentResponse:
    """创建支付 intent；金额只从 customer_order.grand_total_cents 读取。"""
    service = PaymentService(pool, get_settings())
    try:
        intent = await service.create_intent(
            order_id=body.order_id,
            provider=body.provider.value,
            requested_amount=body.amount,
            description=body.description,
            payer_openid=body.payer_openid,
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
        intent = await service.process_intent(intent.intent_id)
    except PaymentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PaymentAmountMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PaymentServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    provider_result = intent.provider_result or {}
    return CreatePaymentResponse(
        payment_id=intent.intent_id,
        order_id=intent.order_id,
        amount=str(Decimal(intent.amount_cents) / 100),
        currency=intent.currency,
        status=intent.status,
        qr_code=provider_result.get("qr_code"),
        code_url=provider_result.get("code_url") or provider_result.get("client_secret"),
        provider_result=provider_result or None,
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
                "SELECT id, order_id, amount_cents, currency, status, provider, provider_trade_no "
                "FROM payment_intent WHERE id = $1",
                payment_id,
            )
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")

    if row is None:
        raise HTTPException(status_code=404, detail="payment not found")

    return PaymentQueryResponse(
        payment_id=str(row["id"]),
        order_id=row["order_id"],
        amount=str(Decimal(row["amount_cents"]) / 100),
        currency=row["currency"],
        status=row["status"],
        provider=row["provider"],
        provider_trade_no=row["provider_trade_no"],
    )


@router.post("/refund")
async def refund_payment(
    body: RefundRequest,
    request: Request,
    _principal: dict = Depends(get_current_user),
    pool: Any = Depends(get_pool),
) -> dict[str, Any]:
    """申请幂等退款；provider 成功但 DB 暂时故障由 outbox 恢复。"""
    service = PaymentService(pool, get_settings())
    try:
        return await service.request_refund(
            payment_id=body.payment_id,
            refund_amount=body.refund_amount,
            reason=body.reason,
            idempotency_key=body.idempotency_key or request.headers.get("Idempotency-Key"),
        )
    except PaymentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (PaymentStateError, RefundConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PaymentServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _wechat_notify_amount_cents(notify: dict[str, Any]) -> int | None:
    """从微信回调解密体提取实付金额 (分)；取不到则返回 None（跳过比对）。"""
    amount = notify.get("amount")
    if isinstance(amount, dict) and amount.get("total") is not None:
        try:
            return int(amount["total"])
        except (TypeError, ValueError):
            return None
    for key in ("total_fee", "total_amount", "amount"):
        if notify.get(key) is not None:
            try:
                return int(notify[key])
            except (TypeError, ValueError):
                return None
    return None


def _alipay_notify_amount_cents(total_amount: str | None) -> int | None:
    """支付宝回调 total_amount（元字符串）→ 分；解析失败返回 None。"""
    if not total_amount:
        return None
    try:
        return int(Decimal(str(total_amount)) * 100)
    except Exception:
        return None


async def _settle_payment_and_order(
    pool: Any,
    session_id: str,
    transaction_id: str | None,
    paid_amount_cents: int | None = None,
    expected_provider: str | None = None,
    paid_currency: str | None = None,
) -> None:
    """支付成功回调的统一收尾：验证 intent 后原子推进订单。

    幂等：仅当会话仍是 pending 时才翻转并推进订单 (RETURNING 拿 order_id);
    重复通知拿不到行 → 不重复确认订单。订单已非可确认态时只记日志不报错
    (避免回调因订单侧状态而失败, 让网关反复重推)。

    金额一致性 (fail-closed)：调用方传入回调方声明的实付金额
    (paid_amount_cents) 时，先比对 payment_session.amount，不一致则拒绝
    落库——防止 HMAC 绕过后用小额回调冒充大额订单已支付。
    """
    if pool is None or paid_amount_cents is None:
        logger.warning("payment settlement rejected: missing verified amount session=%s", session_id)
        return
    async with pool.acquire() as conn:
        async with conn.transaction():
            intent = await conn.fetchrow(
                """
                SELECT p.id, p.order_id, p.amount_cents, p.currency, p.provider,
                       p.status, o.status AS order_status, o.grand_total_cents,
                       o.currency AS order_currency
                FROM payment_intent p
                LEFT JOIN customer_order o ON o.id::text = p.order_id
                WHERE p.id = $1
                FOR UPDATE OF p
                """,
                session_id,
            )
            if intent is None:
                logger.warning("payment intent not found: %s", session_id)
                return
            if expected_provider and intent["provider"] != expected_provider:
                logger.warning("payment provider mismatch: %s", session_id)
                return
            if intent["status"] not in {
                PaymentStatus.PENDING.value,
                "provider_pending",
                PaymentStatus.PAID.value,
            }:
                logger.warning(
                    "payment intent is not settleable: session=%s status=%s",
                    session_id,
                    intent["status"],
                )
                return
            if int(intent["amount_cents"]) != int(paid_amount_cents):
                logger.warning(
                    "payment amount mismatch: session=%s expected=%s got=%s",
                    session_id,
                    intent["amount_cents"],
                    paid_amount_cents,
                )
                return
            callback_currency = (paid_currency or intent["currency"]).upper()
            if callback_currency != str(intent["currency"]).upper():
                logger.warning("payment currency mismatch: %s", session_id)
                return
            if intent["order_id"] is None or intent["grand_total_cents"] is None:
                return
            if int(intent["grand_total_cents"]) != int(intent["amount_cents"]):
                logger.warning("order total no longer matches payment intent: %s", session_id)
                return
            if str(intent["order_currency"]).upper() != str(intent["currency"]).upper():
                logger.warning("order currency no longer matches payment intent: %s", session_id)
                return

            # Duplicate provider callbacks are successful no-ops. The order
            # transition and reservation wiring are in the same DB transaction.
            if intent["status"] != PaymentStatus.PAID.value:
                await conn.execute(
                    """
                    UPDATE payment_intent
                    SET status = 'paid', provider_trade_no = COALESCE($1, provider_trade_no),
                        last_error = NULL, version = version + 1, updated_at = NOW()
                    WHERE id = $2 AND status IN ('pending', 'provider_pending')
                    """,
                    transaction_id,
                    session_id,
                )

            order_id = str(intent["order_id"])
            lines = await conn.fetch(
                """
                SELECT oli.id, oli.batch_id, oli.quantity, ib.variant_id,
                       ib.location_id, pv.product_id
                FROM order_line_item oli
                JOIN inventory_batch ib ON ib.id = oli.batch_id
                JOIN product_variant pv ON pv.id = ib.variant_id
                WHERE oli.order_id = $1
                """,
                order_id,
            )
            for line in lines:
                reservation_key = f"order:{order_id}:line:{line['id']}"
                await conn.execute(
                    """
                    INSERT INTO inventory_reservation
                        (order_id, order_line_item_id, product_id, variant_id,
                         location_id, batch_id, quantity, idempotency_key)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    order_id,
                    str(line["id"]),
                    str(line["product_id"]),
                    str(line["variant_id"]) if line["variant_id"] else None,
                    line["location_id"],
                    line["batch_id"],
                    int(line["quantity"]),
                    reservation_key,
                )
                await conn.execute(
                    """
                    UPDATE order_line_item
                    SET location_id = COALESCE(location_id, $1),
                        reservation_id = COALESCE(
                            reservation_id,
                            (SELECT id FROM inventory_reservation WHERE idempotency_key = $2)
                        )
                    WHERE id = $3
                    """,
                    line["location_id"],
                    reservation_key,
                    line["id"],
                )

            if intent["order_status"] in {"pending", "draft"}:
                await conn.execute(
                    """
                    UPDATE customer_order
                    SET status = 'confirmed', payment_provider_name = $1,
                        payment_intent_id = $2, payment_verified_at = NOW(),
                        version = version + 1, updated_at = NOW()
                    WHERE id = $3 AND status IN ('pending', 'draft')
                    """,
                    intent["provider"],
                    session_id,
                    intent["order_id"],
                )
                await conn.execute(
                    """
                    INSERT INTO order_status_history
                        (order_id, from_status, to_status, reason, metadata)
                    VALUES ($1, $2, 'confirmed', 'payment confirmed', $3::jsonb)
                    """,
                    intent["order_id"],
                    intent["order_status"],
                    json.dumps({"payment_intent_id": session_id}, ensure_ascii=False),
                )


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
                pool,
                out_trade_no,
                notify.get("transaction_id"),
                _wechat_notify_amount_cents(notify),
                expected_provider=PaymentProvider.WECHAT.value,
                paid_currency=(notify.get("amount") or {}).get("currency")
                if isinstance(notify.get("amount"), dict)
                else None,
            )
        return {"code": "SUCCESS", "message": "ok"}

    # 遗留沙箱路径：生产环境拒绝，避免内存态订单被伪造回调标 paid。
    if settings.environment.lower() == "production":
        raise HTTPException(status_code=403, detail="sandbox notify disabled in production")
    notify = await _wechat_provider.handle_notify(headers, body)
    if notify is None:
        raise HTTPException(status_code=400, detail="invalid notify")

    if notify.is_success:
        pool = getattr(request.app.state, "pool", None)
        await _settle_payment_and_order(
            pool,
            notify.out_trade_no,
            notify.transaction_id,
            notify.amount,
            expected_provider=PaymentProvider.WECHAT.value,
        )

    return {"code": "SUCCESS", "message": "ok"}


@router.post("/alipay/notify")
async def alipay_notify(request: Request) -> str:
    """支付宝异步通知处理。"""
    if get_settings().environment.lower() == "production":
        return "fail"
    form = await request.form()
    params = dict(form)

    notify = await _alipay_provider.handle_notify(params)
    if notify is None:
        return "fail"

    if notify.is_success:
        pool = getattr(request.app.state, "pool", None)
        await _settle_payment_and_order(
            pool,
            notify.out_trade_no,
            notify.trade_no,
            _alipay_notify_amount_cents(notify.total_amount),
            expected_provider=PaymentProvider.ALIPAY.value,
            paid_currency="CNY",
        )

    return "success"


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request) -> dict[str, bool]:
    """Stripe PaymentIntent webhook：验签后只接受精确金额/币种的成功事件。"""

    body = await request.body()
    try:
        from obase.provider_registry import ProviderRegistry

        gateway = ProviderRegistry.get().generic("payment_gateway", "stripe")
        event = await gateway.verify_callback(dict(request.headers), body)
    except Exception as exc:  # noqa: BLE001 - webhook must fail closed
        logger.warning("stripe webhook verification failed: %s", exc)
        raise HTTPException(status_code=400, detail="invalid stripe webhook") from exc
    if not event:
        raise HTTPException(status_code=400, detail="invalid stripe webhook")

    if event.get("type") != "payment_intent.succeeded":
        return {"received": True}
    obj = ((event.get("data") or {}).get("object") or {})
    provider_intent_id = str(obj.get("id") or "")
    metadata = obj.get("metadata") or {}
    out_trade_no = str(metadata.get("out_trade_no") or metadata.get("order_ref") or "")
    amount = obj.get("amount_received", obj.get("amount"))
    currency = str(obj.get("currency") or "").upper()
    if not provider_intent_id or amount is None:
        raise HTTPException(status_code=400, detail="stripe event missing payment fields")

    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="database not ready")
    async with pool.acquire() as conn:
        intent_id = await conn.fetchval(
            """
            SELECT id FROM payment_intent
            WHERE provider_intent_id = $1
               OR ($2 <> '' AND id::text = $2)
            """,
            provider_intent_id,
            out_trade_no,
        )
    if intent_id is None:
        logger.warning("stripe payment intent not found: %s", provider_intent_id)
        return {"received": True}
    await _settle_payment_and_order(
        pool,
        str(intent_id),
        provider_intent_id,
        int(amount),
        expected_provider=PaymentProvider.STRIPE.value,
        paid_currency=currency,
    )
    return {"received": True}
