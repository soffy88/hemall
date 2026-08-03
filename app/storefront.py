"""hemall 商城编排层 — 公开路由，服务端代客调 omodul。

所有购物车 / 结账操作走 omodul 写引擎，本模块只负责:
  1. HTTP 路由 + 参数校验
  2. 调用 omodul 函数（传入 config / input / output_dir / pool）
  3. 结账完成后签发 JWT 收据 token 供顾客查单
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import queries
from app.deps import get_current_customer
from obase.uuid7 import uuid7

logger = logging.getLogger("hemall.storefront")

router = APIRouter(prefix="/store", tags=["storefront"])


# ── Config ──────────────────────────────────────────────────────────────


def _output_dir(request: Request, op: str) -> Path:
    base = Path(request.app.state.config.output_root)
    out = base / op
    out.mkdir(parents=True, exist_ok=True)
    return out


def _step_dir(out_root: Path, step: str) -> Path:
    """checkout() 多步编排里每一步的独立子目录；调用前必须先建好,否则
    omodul 内部 trail.write() 直接对着不存在的目录写文件会 FileNotFoundError。
    """
    d = out_root / step
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database not ready")
    return pool


def _cfg(request: Request, config_cls: type, **kwargs: Any) -> Any:
    """创建 omodul config，统一注入 redis_url。

    omodul 各 Config 的 redis_url 默认是 redis://localhost:6379/0（容器内
    localhost 是自身而非 redis 服务），这里统一从应用配置注入正确地址。
    """
    settings = request.app.state.config
    return config_cls(redis_url=settings.redis_url, **kwargs)


def _sign_receipt(
    order_id: str, secret: str, algorithm: str, ttl_minutes: int = 43200
) -> str:
    """签发收据 token（30 天有效）。"""
    from obase.crypto.util import CryptoUtil

    return CryptoUtil.jwt_sign(
        payload={"order_id": order_id},
        secret=secret,
        expires_in_minutes=ttl_minutes,
        algorithm=algorithm,
    )


def _sign_customer_token(
    customer_id: str, email: str, secret: str, algorithm: str, ttl_minutes: int = 43200
) -> str:
    """签发顾客登录 token（30 天有效）。typ=customer 跟管理员 token 区分开,
    get_current_customer 靠这个 claim 拒绝管理员 token 冒充顾客主体。
    """
    from obase.crypto.util import CryptoUtil

    return CryptoUtil.jwt_sign(
        payload={"customer_id": customer_id, "email": email, "typ": "customer"},
        secret=secret,
        expires_in_minutes=ttl_minutes,
        algorithm=algorithm,
    )


# ── 购物车 ──────────────────────────────────────────────────────────────


class CreateCartRequest(BaseModel):
    region_code: str = "cn-east"
    currency: str = "CNY"


@router.post("/carts")
async def create_cart(body: CreateCartRequest, request: Request):
    from omodul.create_cart import CreateCartConfig, CreateCartInput, create_cart

    cfg = _cfg(request, CreateCartConfig)
    inp = CreateCartInput(region_code=body.region_code, currency=body.currency)
    result = await create_cart(
        cfg, inp, _output_dir(request, "create_cart"), pool=_pool(request)
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {
        "cart_id": result["cart_id"],
        "currency": result["currency"],
        "region_code": body.region_code,
    }


class AddLineItemRequest(BaseModel):
    cart_id: str
    batch_id: str
    quantity: int = 1


@router.post("/carts/line-items")
async def add_line_item(body: AddLineItemRequest, request: Request):
    from omodul.add_line_item_to_cart import (
        AddLineItemConfig,
        AddLineItemInput,
        add_line_item_to_cart,
    )

    cfg = _cfg(request, AddLineItemConfig)
    inp = AddLineItemInput(
        cart_id=body.cart_id, batch_id=body.batch_id, quantity=body.quantity
    )
    result = await add_line_item_to_cart(
        cfg, inp, _output_dir(request, "add_line_item_to_cart"), pool=_pool(request)
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {
        "line_item_id": result["line_item_id"],
        "new_quantity": result["quantity"],
    }


class UpdateLineItemRequest(BaseModel):
    cart_id: str
    line_item_id: str
    quantity: int


@router.put("/carts/line-items")
async def update_line_item(body: UpdateLineItemRequest, request: Request):
    from omodul.update_line_item_in_cart import (
        UpdateLineItemConfig,
        UpdateLineItemInput,
        update_line_item_in_cart,
    )

    cfg = _cfg(request, UpdateLineItemConfig)
    inp = UpdateLineItemInput(
        cart_id=body.cart_id, line_item_id=body.line_item_id, quantity=body.quantity
    )
    result = await update_line_item_in_cart(
        cfg, inp, _output_dir(request, "update_line_item_in_cart"), pool=_pool(request)
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {
        "line_item_id": result["line_item_id"],
        "new_quantity": result["quantity"],
    }


class DeleteLineItemRequest(BaseModel):
    cart_id: str
    line_item_id: str


@router.delete("/carts/line-items")
async def delete_line_item(body: DeleteLineItemRequest, request: Request):
    from omodul.delete_line_item_from_cart import (
        DeleteLineItemConfig,
        DeleteLineItemInput,
        delete_line_item_from_cart,
    )

    cfg = _cfg(request, DeleteLineItemConfig)
    inp = DeleteLineItemInput(cart_id=body.cart_id, line_item_id=body.line_item_id)
    result = await delete_line_item_from_cart(
        cfg,
        inp,
        _output_dir(request, "delete_line_item_from_cart"),
        pool=_pool(request),
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {"ok": True}


@router.get("/carts/{cart_id}")
async def get_cart(cart_id: str, request: Request):
    cart = await queries.get_cart(_pool(request), cart_id)
    if cart is None:
        raise HTTPException(404, "cart not found")
    return cart


# ── 地址 + 客户 ──────────────────────────────────────────────────────────


class AddressPayload(BaseModel):
    recipient_name: str
    phone: str
    address_line1: str
    address_line2: str = ""
    city: str
    region_code: str = ""
    postal_code: str


class SetCartAddressRequest(BaseModel):
    cart_id: str
    address: AddressPayload


@router.post("/carts/billing-address")
async def set_billing_address(body: SetCartAddressRequest, request: Request):
    from omodul.set_cart_billing_address import (
        SetCartBillingAddressConfig,
        SetCartBillingAddressInput,
        set_cart_billing_address,
    )

    cfg = _cfg(request, SetCartBillingAddressConfig)
    inp = SetCartBillingAddressInput(
        cart_id=body.cart_id,
        recipient_name=body.address.recipient_name,
        phone=body.address.phone,
        address_line1=body.address.address_line1,
        address_line2=body.address.address_line2,
        city=body.address.city,
        region_code=body.address.region_code,
        postal_code=body.address.postal_code,
    )
    result = await set_cart_billing_address(
        cfg, inp, _output_dir(request, "set_cart_billing_address"), pool=_pool(request)
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {"ok": True}


@router.post("/carts/shipping-address")
async def set_shipping_address(body: SetCartAddressRequest, request: Request):
    from omodul.set_cart_shipping_address import (
        SetCartShippingAddressConfig,
        SetCartShippingAddressInput,
        set_cart_shipping_address,
    )

    cfg = _cfg(request, SetCartShippingAddressConfig)
    inp = SetCartShippingAddressInput(
        cart_id=body.cart_id,
        recipient_name=body.address.recipient_name,
        phone=body.address.phone,
        address_line1=body.address.address_line1,
        address_line2=body.address.address_line2,
        city=body.address.city,
        region_code=body.address.region_code,
        postal_code=body.address.postal_code,
    )
    result = await set_cart_shipping_address(
        cfg, inp, _output_dir(request, "set_cart_shipping_address"), pool=_pool(request)
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {"ok": True}


class SetCartCustomerRequest(BaseModel):
    cart_id: str
    customer_id: str


@router.post("/carts/customer")
async def set_cart_customer(body: SetCartCustomerRequest, request: Request):
    from omodul.set_cart_customer import (
        SetCartCustomerConfig,
        SetCartCustomerInput,
        set_cart_customer,
    )

    cfg = _cfg(request, SetCartCustomerConfig)
    inp = SetCartCustomerInput(cart_id=body.cart_id, customer_id=body.customer_id)
    result = await set_cart_customer(
        cfg, inp, _output_dir(request, "set_cart_customer"), pool=_pool(request)
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {"ok": True}


# ── 支付 + 结账 ──────────────────────────────────────────────────────────


class CheckoutRequest(BaseModel):
    """一步完成结账：设置区域 → 建支付会话 → 选定 → 授权 → 完成。"""

    cart_id: str
    billing_address: AddressPayload | None = None
    shipping_address: AddressPayload | None = None
    customer_id: str | None = None


@router.post("/checkout")
async def checkout(body: CheckoutRequest, request: Request):
    pool = _pool(request)
    cfg_store = request.app.state.config
    out_root = _output_dir(request, "checkout")

    # 1. 设置地址
    if body.billing_address:
        from omodul.set_cart_billing_address import (
            SetCartBillingAddressConfig,
            SetCartBillingAddressInput,
            set_cart_billing_address,
        )

        r = await set_cart_billing_address(
            _cfg(request, SetCartBillingAddressConfig),
            SetCartBillingAddressInput(
                cart_id=body.cart_id,
                recipient_name=body.billing_address.recipient_name,
                phone=body.billing_address.phone,
                address_line1=body.billing_address.address_line1,
                address_line2=body.billing_address.address_line2,
                city=body.billing_address.city,
                region_code=body.billing_address.region_code,
                postal_code=body.billing_address.postal_code,
            ),
            _step_dir(out_root, "set_cart_billing_address"),
            pool=pool,
        )
        if r["status"] == "failed":
            raise HTTPException(400, f"billing_address: {r['error']['message']}")

    if body.shipping_address:
        from omodul.set_cart_shipping_address import (
            SetCartShippingAddressConfig,
            SetCartShippingAddressInput,
            set_cart_shipping_address,
        )

        r = await set_cart_shipping_address(
            _cfg(request, SetCartShippingAddressConfig),
            SetCartShippingAddressInput(
                cart_id=body.cart_id,
                recipient_name=body.shipping_address.recipient_name,
                phone=body.shipping_address.phone,
                address_line1=body.shipping_address.address_line1,
                address_line2=body.shipping_address.address_line2,
                city=body.shipping_address.city,
                region_code=body.shipping_address.region_code,
                postal_code=body.shipping_address.postal_code,
            ),
            _step_dir(out_root, "set_cart_shipping_address"),
            pool=pool,
        )
        if r["status"] == "failed":
            raise HTTPException(400, f"shipping_address: {r['error']['message']}")

    # 2. 绑定客户（可选）
    if body.customer_id:
        from omodul.set_cart_customer import (
            SetCartCustomerConfig,
            SetCartCustomerInput,
            set_cart_customer,
        )

        r = await set_cart_customer(
            _cfg(request, SetCartCustomerConfig),
            SetCartCustomerInput(cart_id=body.cart_id, customer_id=body.customer_id),
            _step_dir(out_root, "set_cart_customer"),
            pool=pool,
        )
        if r["status"] == "failed":
            raise HTTPException(400, f"customer: {r['error']['message']}")

    # 3. 建支付会话
    from omodul.create_payment_sessions import (
        CreatePaymentSessionsConfig,
        CreatePaymentSessionsInput,
        create_payment_sessions,
    )

    r = await create_payment_sessions(
        _cfg(request, CreatePaymentSessionsConfig),
        CreatePaymentSessionsInput(
            cart_id=body.cart_id,
            provider_names=[cfg_store.default_payment_provider],
        ),
        _step_dir(out_root, "create_payment_sessions"),
        pool=pool,
    )
    if r["status"] == "failed":
        raise HTTPException(400, f"payment_sessions: {r['error']['message']}")

    sessions = r.get("sessions", [])
    provider_name = cfg_store.default_payment_provider
    selected_session = next(
        (s for s in sessions if s["provider_name"] == provider_name), None
    )
    if selected_session is None or selected_session["status"] == "failed":
        raise HTTPException(400, f"{provider_name} payment session not available")

    # 4. 选定支付会话
    from omodul.set_payment_session import (
        SetPaymentSessionConfig,
        SetPaymentSessionInput,
        set_payment_session,
    )

    r = await set_payment_session(
        _cfg(request, SetPaymentSessionConfig),
        SetPaymentSessionInput(
            cart_id=body.cart_id,
            provider_name=provider_name,
        ),
        _step_dir(out_root, "set_payment_session"),
        pool=pool,
    )
    if r["status"] == "failed":
        raise HTTPException(400, f"set_payment_session: {r['error']['message']}")

    # 5. 授权支付
    from omodul.authorize_payment_for_cart import (
        AuthorizePaymentForCartConfig,
        AuthorizePaymentForCartInput,
        authorize_payment_for_cart,
    )

    r = await authorize_payment_for_cart(
        _cfg(request, AuthorizePaymentForCartConfig),
        AuthorizePaymentForCartInput(cart_id=body.cart_id),
        _step_dir(out_root, "authorize_payment_for_cart"),
        pool=pool,
    )
    if r["status"] == "failed":
        raise HTTPException(400, f"authorize: {r['error']['message']}")

    # 6. 完成结账
    from omodul.complete_checkout import (
        CompleteCheckoutConfig,
        CompleteCheckoutInput,
        complete_checkout,
    )

    r = await complete_checkout(
        CompleteCheckoutConfig(redis_url=cfg_store.redis_url),
        CompleteCheckoutInput(cart_id=body.cart_id),
        _step_dir(out_root, "complete_checkout"),
        pool=pool,
    )
    if r["status"] == "failed":
        raise HTTPException(400, f"checkout: {r['error']['message']}")

    # 7. 签发收据 token
    order_id = str(r["order_id"])
    receipt_token = _sign_receipt(
        order_id,
        cfg_store.jwt_secret,
        cfg_store.jwt_algorithm,
    )

    # Phase 7 Task 3: 行为序列数据源——best-effort 记录零登录设备购买轨迹。
    # 顾客没账号 (nearby-feed 是零登录公开端点)，但限流层一直按 X-Device-Id
    # 头识别设备；这里把同款头 + 成交行项写入 device_purchase_log，feed 就能
    # "知道用户买过什么"，用 oskill 关联度算子把关联商品插队。失败只记日志，
    # 绝不影响下单主流程 (旁路旁得干净)。
    device_id = (request.headers.get("x-device-id") or "").strip()
    if device_id:
        try:
            pool = _pool(request)
            async with pool.acquire() as conn:
                items = await conn.fetch(
                    "SELECT oli.batch_id, ib.product_id, ib.variant_id "
                    'FROM "order_line_item" oli '
                    'JOIN "inventory_batch" ib ON ib.id = oli.batch_id '
                    "WHERE oli.order_id = $1",
                    order_id,
                )
                if items:
                    await conn.executemany(
                        'INSERT INTO "device_purchase_log" '
                        "(id, device_id, order_id, batch_id, product_id, variant_id) "
                        "VALUES ($1, $2, $3, $4, $5, $6)",
                        [
                            (uuid7(), device_id[:64], order_id, str(i["batch_id"]),
                             str(i["product_id"]), str(i["variant_id"]))
                            for i in items
                        ],
                    )
        except Exception as exc:  # noqa: BLE001 - 行为轨迹是旁路，不阻断下单
            logger.warning(
                "device purchase log failed (order=%s): %s", order_id, exc
            )

    return {
        "order_id": order_id,
        "grand_total_cents": r["grand_total_cents"],
        "receipt_token": receipt_token,
    }


# ── 优惠 / 礼品卡 / 配送 / 区域 ────────────────────────────────────────────
# 对应的 omodul 端点在 /cart/* 域下 require_auth=True (顾客浏览器没有管理员
# JWT，直接调不通)，这里做公开代理，逻辑同购物车行项那组端点。


class ApplyDiscountRequest(BaseModel):
    cart_id: str
    code: str


@router.post("/carts/discount")
async def apply_discount(body: ApplyDiscountRequest, request: Request):
    from omodul.apply_discount_to_cart import (
        ApplyDiscountToCartConfig,
        ApplyDiscountToCartInput,
        apply_discount_to_cart,
    )

    cfg = _cfg(request, ApplyDiscountToCartConfig)
    inp = ApplyDiscountToCartInput(cart_id=body.cart_id, code=body.code)
    result = await apply_discount_to_cart(
        cfg, inp, _output_dir(request, "apply_discount_to_cart"), pool=_pool(request)
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {
        "discount_id": result["discount_id"],
        "discount_cents": result["discount_cents"],
        "grand_total_cents": result["grand_total_cents"],
    }


class RemoveDiscountRequest(BaseModel):
    cart_id: str
    discount_id: str


@router.delete("/carts/discount")
async def remove_discount(body: RemoveDiscountRequest, request: Request):
    from omodul.remove_discount_from_cart import (
        RemoveDiscountFromCartConfig,
        RemoveDiscountFromCartInput,
        remove_discount_from_cart,
    )

    cfg = _cfg(request, RemoveDiscountFromCartConfig)
    inp = RemoveDiscountFromCartInput(
        cart_id=body.cart_id, discount_id=body.discount_id
    )
    result = await remove_discount_from_cart(
        cfg, inp, _output_dir(request, "remove_discount_from_cart"), pool=_pool(request)
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {"ok": True}


class ApplyGiftCardRequest(BaseModel):
    cart_id: str
    code: str


@router.post("/carts/gift-card")
async def apply_gift_card(body: ApplyGiftCardRequest, request: Request):
    from omodul.apply_gift_card_to_cart import (
        ApplyGiftCardToCartConfig,
        ApplyGiftCardToCartInput,
        apply_gift_card_to_cart,
    )

    cfg = _cfg(request, ApplyGiftCardToCartConfig)
    inp = ApplyGiftCardToCartInput(cart_id=body.cart_id, code=body.code)
    result = await apply_gift_card_to_cart(
        cfg, inp, _output_dir(request, "apply_gift_card_to_cart"), pool=_pool(request)
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {
        "gift_card_id": result["gift_card_id"],
        "applied_cents": result["applied_cents"],
        "amount_due_cents": result["amount_due_cents"],
    }


class RemoveGiftCardRequest(BaseModel):
    cart_id: str
    gift_card_id: str


@router.delete("/carts/gift-card")
async def remove_gift_card(body: RemoveGiftCardRequest, request: Request):
    from omodul.remove_gift_card_from_cart import (
        RemoveGiftCardFromCartConfig,
        RemoveGiftCardFromCartInput,
        remove_gift_card_from_cart,
    )

    cfg = _cfg(request, RemoveGiftCardFromCartConfig)
    inp = RemoveGiftCardFromCartInput(
        cart_id=body.cart_id, gift_card_id=body.gift_card_id
    )
    result = await remove_gift_card_from_cart(
        cfg,
        inp,
        _output_dir(request, "remove_gift_card_from_cart"),
        pool=_pool(request),
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {"ok": True}


class AddShippingMethodRequest(BaseModel):
    cart_id: str
    method_name: str
    price_cents: int


@router.post("/carts/shipping-method")
async def add_shipping_method(body: AddShippingMethodRequest, request: Request):
    from omodul.add_shipping_method_to_cart import (
        AddShippingMethodToCartConfig,
        AddShippingMethodToCartInput,
        add_shipping_method_to_cart,
    )

    cfg = _cfg(request, AddShippingMethodToCartConfig)
    inp = AddShippingMethodToCartInput(
        cart_id=body.cart_id, method_name=body.method_name, price_cents=body.price_cents
    )
    result = await add_shipping_method_to_cart(
        cfg,
        inp,
        _output_dir(request, "add_shipping_method_to_cart"),
        pool=_pool(request),
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {
        "shipping_cents": result["shipping_cents"],
        "grand_total_cents": result["grand_total_cents"],
    }


class SetCartRegionRequest(BaseModel):
    cart_id: str
    region_code: str
    currency: str


@router.put("/carts/region")
async def set_cart_region_route(body: SetCartRegionRequest, request: Request):
    from omodul.set_cart_region import (
        SetCartRegionConfig,
        SetCartRegionInput,
        set_cart_region,
    )

    cfg = _cfg(request, SetCartRegionConfig)
    inp = SetCartRegionInput(
        cart_id=body.cart_id, region_code=body.region_code, currency=body.currency
    )
    result = await set_cart_region(
        cfg, inp, _output_dir(request, "set_cart_region"), pool=_pool(request)
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {"ok": True}


# ── 公开商品浏览 ──────────────────────────────────────────────────────────


@router.get("/products")
async def storefront_products(
    request: Request,
    search: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    limit: int = 50,
    offset: int = 0,
):
    return await queries.list_storefront_products(
        _pool(request),
        search=search,
        min_price=min_price,
        max_price=max_price,
        limit=limit,
        offset=offset,
    )


@router.get("/products/{product_id}")
async def storefront_product(product_id: str, request: Request):
    product = await queries.get_storefront_product(_pool(request), product_id)
    if product is None:
        raise HTTPException(404, "product not found")
    return product


# ── 收据查单 ──────────────────────────────────────────────────────────────


@router.get("/orders/lookup")
async def lookup_order(token: str, request: Request):
    cfg = request.app.state.config
    order = await queries.get_order_by_receipt_token(
        _pool(request), token, cfg.jwt_secret, cfg.jwt_algorithm
    )
    if order is None:
        raise HTTPException(404, "order not found or token invalid")
    return order


# ── 区域列表 ──────────────────────────────────────────────────────────────


@router.get("/regions")
async def list_regions(request: Request):
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(
            "SELECT code, name, currency FROM region WHERE deleted_at IS NULL ORDER BY name"
        )
        return [dict(r) for r in rows]


# ── 顾客账号 (注册/登录/个人资料/地址簿/订单历史) ─────────────────────────
# customer 表原本没有密码字段 (3O 元素库不提供顾客密码登录原语,只有 app_user
# 管理员登录); password_hash 是 hemall 项目层补的字段 (见 bootstrap.py)。
# 密码哈希用跟管理员登录同一套 obase.auth.password.bcrypt_hash/bcrypt_verify。


class CustomerRegisterRequest(BaseModel):
    email: str
    password: str
    phone: str = ""
    name: str = ""


class CustomerTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    customer_id: str
    email: str


@router.post("/customers/register", response_model=CustomerTokenResponse)
async def register_customer(body: CustomerRegisterRequest, request: Request):
    from obase.auth.password import bcrypt_hash
    from omodul.create_customer import (
        CreateCustomerConfig,
        CreateCustomerInput,
        create_customer,
    )

    cfg = request.app.state.config
    result = await create_customer(
        _cfg(request, CreateCustomerConfig),
        CreateCustomerInput(email=body.email, phone=body.phone, name=body.name),
        _output_dir(request, "create_customer"),
        pool=_pool(request),
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])

    customer_id = str(result["customer_id"])
    password_hash = bcrypt_hash(password=body.password)
    async with _pool(request).acquire() as conn:
        await conn.execute(
            'UPDATE "customer" SET password_hash = $1 WHERE id = $2',
            password_hash,
            customer_id,
        )

    token = _sign_customer_token(
        customer_id, body.email, cfg.jwt_secret, cfg.jwt_algorithm
    )
    return CustomerTokenResponse(
        access_token=token, customer_id=customer_id, email=body.email
    )


class CustomerLoginRequest(BaseModel):
    email: str
    password: str


@router.post("/customers/login", response_model=CustomerTokenResponse)
async def login_customer(body: CustomerLoginRequest, request: Request):
    from obase.auth.password import bcrypt_verify

    cfg = request.app.state.config
    async with _pool(request).acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id, email, password_hash, status FROM "customer" '
            "WHERE email = $1 AND deleted_at IS NULL",
            body.email,
        )
    if row is None or row["password_hash"] is None:
        raise HTTPException(401, "invalid credentials")
    if row["status"] != "active":
        raise HTTPException(403, f"account not active (status={row['status']!r})")
    if not bcrypt_verify(password=body.password, hashed=row["password_hash"]):
        raise HTTPException(401, "invalid credentials")

    token = _sign_customer_token(
        str(row["id"]), row["email"], cfg.jwt_secret, cfg.jwt_algorithm
    )
    return CustomerTokenResponse(
        access_token=token, customer_id=str(row["id"]), email=row["email"]
    )


@router.get("/customers/me")
async def get_my_profile(
    request: Request, principal: dict = Depends(get_current_customer)
):
    customer = await queries.get_customer(_pool(request), principal["customer_id"])
    if customer is None:
        raise HTTPException(404, "customer not found")
    return customer


class UpdateMyProfileRequest(BaseModel):
    email: str | None = None
    phone: str | None = None
    name: str | None = None


@router.put("/customers/me")
async def update_my_profile(
    body: UpdateMyProfileRequest,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    from omodul.update_customer import (
        UpdateCustomerConfig,
        UpdateCustomerInput,
        update_customer,
    )

    result = await update_customer(
        _cfg(request, UpdateCustomerConfig),
        UpdateCustomerInput(
            customer_id=principal["customer_id"],
            email=body.email,
            phone=body.phone,
            name=body.name,
        ),
        _output_dir(request, "update_customer"),
        pool=_pool(request),
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return await queries.get_customer(_pool(request), principal["customer_id"])


@router.get("/customers/me/orders")
async def get_my_orders(
    request: Request, principal: dict = Depends(get_current_customer)
):
    return await queries.list_customer_orders(_pool(request), principal["customer_id"])


@router.get("/customers/me/addresses")
async def list_my_addresses(
    request: Request, principal: dict = Depends(get_current_customer)
):
    return await queries.list_customer_addresses(
        _pool(request), principal["customer_id"]
    )


class MyAddressRequest(BaseModel):
    recipient_name: str
    phone: str
    address_line1: str
    address_line2: str = ""
    city: str
    region_code: str = ""
    postal_code: str
    is_default: bool = False


@router.post("/customers/me/addresses")
async def add_my_address(
    body: MyAddressRequest,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    from omodul.add_customer_address import (
        AddCustomerAddressConfig,
        AddCustomerAddressInput,
        add_customer_address,
    )

    result = await add_customer_address(
        _cfg(request, AddCustomerAddressConfig),
        AddCustomerAddressInput(
            customer_id=principal["customer_id"], **body.model_dump()
        ),
        _output_dir(request, "add_customer_address"),
        pool=_pool(request),
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return await queries.list_customer_addresses(
        _pool(request), principal["customer_id"]
    )


async def _owns_address(pool: Any, customer_id: str, address_id: str) -> bool:
    """地址增删改前的越权校验：omodul 只按 address_id 操作，不知道调用者是谁。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT customer_id FROM "customer_address" WHERE id = $1 AND deleted_at IS NULL',
            address_id,
        )
    return row is not None and str(row["customer_id"]) == customer_id


class MyAddressUpdateRequest(BaseModel):
    recipient_name: str | None = None
    phone: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    region_code: str | None = None
    postal_code: str | None = None
    is_default: bool | None = None


@router.put("/customers/me/addresses/{address_id}")
async def update_my_address(
    address_id: str,
    body: MyAddressUpdateRequest,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    from omodul.update_customer_address import (
        UpdateCustomerAddressConfig,
        UpdateCustomerAddressInput,
        update_customer_address,
    )

    if not await _owns_address(_pool(request), principal["customer_id"], address_id):
        raise HTTPException(404, "address not found")

    result = await update_customer_address(
        _cfg(request, UpdateCustomerAddressConfig),
        UpdateCustomerAddressInput(address_id=address_id, **body.model_dump()),
        _output_dir(request, "update_customer_address"),
        pool=_pool(request),
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return await queries.list_customer_addresses(
        _pool(request), principal["customer_id"]
    )


@router.delete("/customers/me/addresses/{address_id}")
async def delete_my_address(
    address_id: str, request: Request, principal: dict = Depends(get_current_customer)
):
    from omodul.delete_customer_address import (
        DeleteCustomerAddressConfig,
        DeleteCustomerAddressInput,
        delete_customer_address,
    )

    if not await _owns_address(_pool(request), principal["customer_id"], address_id):
        raise HTTPException(404, "address not found")

    result = await delete_customer_address(
        _cfg(request, DeleteCustomerAddressConfig),
        DeleteCustomerAddressInput(address_id=address_id),
        _output_dir(request, "delete_customer_address"),
        pool=_pool(request),
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {"ok": True}


# ── 售后：RMA 客诉自助报案 ──────────────────────────────────────────────
# 走 app.ext 的全自动仲裁引擎 (VLM 判损 → 信誉裁决 → 退款/责任方扣款)，跟
# /admin/aftersales 页面那张"🔥 一键提交客诉"运维卡片调的是同一条底层链路
# (oservi.autonomous_triage_engine)，区别是这里是给真实顾客用的安全入口：
# user_id/user_trust_score 不能像运维卡片那样让调用方随便传——那样任何人
# 都能自己填 trust_score=100 来保证秒退，是真实的信誉造假漏洞。这里改成
# 从已登录顾客的 JWT 主体派生 user_id，trust_score 服务端查 customer.
# base_trust_score，且落地前先校验这笔订单确实属于当前顾客。


async def _owns_order(pool: Any, customer_id: str, order_id: str) -> bool:
    """报案前的越权校验：不能拿别人的 order_id 去骗自动退款。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT customer_id FROM "customer_order" WHERE id = $1', order_id
        )
    return (
        row is not None
        and row["customer_id"] is not None
        and str(row["customer_id"]) == customer_id
    )


class SubmitMyClaimRequest(BaseModel):
    order_id: str
    batch_id: str
    evidence_image_url: str


@router.post("/customers/me/claims")
async def submit_my_claim(
    body: SubmitMyClaimRequest,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    pool = _pool(request)
    customer_id = principal["customer_id"]

    if not await _owns_order(pool, customer_id, body.order_id):
        raise HTTPException(404, "order not found")

    async with pool.acquire() as conn:
        trust_score = await conn.fetchval(
            'SELECT base_trust_score FROM "customer" WHERE id = $1', customer_id
        )

    oservi = getattr(request.app.state, "ext_oservi", None)
    if oservi is None:
        raise HTTPException(503, "ext background engines not ready")

    dispatch_result = await oservi.autonomous_triage.dispatch(
        "rma.claim_submitted",
        {
            "order_id": body.order_id,
            "batch_id": body.batch_id,
            "user_id": customer_id,
            "evidence_image_url": body.evidence_image_url,
            "user_trust_score": trust_score if trust_score is not None else 80,
            "route_risk": 0.0,
        },
    )
    if dispatch_result["errors"]:
        message = dispatch_result["errors"][0].get("error", "claim submission failed")
        raise HTTPException(422, str(message))
    inner = dispatch_result["results"][0] if dispatch_result["results"] else {}
    if inner.get("status") != "completed":
        raise HTTPException(
            422, (inner.get("error") or {}).get("message", "claim submission failed")
        )
    return {
        "claim_id": inner.get("claim_id"),
        "decision": inner.get("decision"),
        "liable_party": inner.get("liable_party"),
        "refund_amount_cents": inner.get("refund_amount"),
    }


@router.get("/customers/me/claims")
async def list_my_claims(
    request: Request, principal: dict = Depends(get_current_customer)
):
    return await queries.list_customer_claims(_pool(request), principal["customer_id"])
