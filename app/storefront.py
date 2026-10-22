"""hemall 商城编排层 — 公开路由，服务端代客调 omodul。

所有购物车 / 结账操作走 omodul 写引擎，本模块只负责:
  1. HTTP 路由 + 参数校验
  2. 调用 omodul 函数（传入 config / input / output_dir / pool）
  3. 结账完成后签发 JWT 收据 token 供顾客查单
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from obase.uuid7 import uuid7
from pydantic import BaseModel

from app import queries
from app.db_recovery import kick_pool_recovery
from app.deps import get_current_customer, get_settings
from app.recommend.models import UserBehaviorEvent, UserBehaviorType
from app.security.tokens import (
    ACCESS_TOKEN_COOKIE,
    TOKEN_TYPE_CART,
    TOKEN_TYPE_CUSTOMER,
    TOKEN_TYPE_RECEIPT,
    decode_token,
    sign_token,
)

logger = logging.getLogger("hemall.storefront")

router = APIRouter(prefix="/store", tags=["storefront"])

_optional_bearer = HTTPBearer(auto_error=False)


def _optional_customer(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
    settings: Any = Depends(get_settings),
) -> dict[str, Any] | None:
    if creds is None and not request.cookies.get(ACCESS_TOKEN_COOKIE):
        return None
    return get_current_customer(request=request, creds=creds, settings=settings)


# ── 推荐行为旁路 ────────────────────────────────────────────────
# 协同过滤靠行为数据训练 (RecommendationService 每小时重建评分矩阵)。
# 商城主流程在这里把 view / cart_add / purchase 三种行为喂给它。全部
# fire-and-forget: 服务未初始化 / 投递失败只记 debug, 绝不影响主流程。

_behavior_tasks: set[asyncio.Task[None]] = set()


def _log_recommend_behavior(user_id: str, product_id: str, behavior_type: UserBehaviorType) -> None:
    """把一条行为事件投递给推荐服务 (fire-and-forget)。

    服务实例挂在 app.main 的全局 AppState 上, 取法与
    app/recommend/router.py 的 get_recommend_service 一致 (函数内懒导入,
    避免与 app.main 循环导入)。强引用保存在 _behavior_tasks 里防任务被
    GC 回收, 完成后自动移除。
    """
    try:
        from app.main import get_app_state

        svc = get_app_state().recommend_service
        if svc is None:
            return
        event = UserBehaviorEvent(
            user_id=user_id, product_id=product_id, behavior_type=behavior_type
        )
        task = asyncio.get_running_loop().create_task(svc.log_behavior(event))
        _behavior_tasks.add(task)
        task.add_done_callback(_behavior_tasks.discard)
    except Exception:  # noqa: BLE001 - 行为日志是旁路, 失败静默
        logger.debug("recommend behavior logging skipped", exc_info=True)


def _optional_customer_id(request: Request) -> str | None:
    """从 cookie / Bearer 解析已登录顾客 id; 无凭证或凭证无效一律 None。

    商品详情是公开端点, 不能套 _optional_customer (过期 cookie 会 401);
    这里吞掉一切解析异常, 身份只用于 view 行为旁路。
    """
    try:
        token = request.cookies.get(ACCESS_TOKEN_COOKIE)
        if not token:
            auth = request.headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        if not token:
            return None
        cfg = request.app.state.config
        payload = decode_token(
            token=token,
            typ=TOKEN_TYPE_CUSTOMER,
            secret=cfg.jwt_secret,
            algorithm=cfg.jwt_algorithm,
        )
        customer_id = payload.get("customer_id")
        if isinstance(customer_id, str) and customer_id.strip():
            return customer_id
        return None
    except Exception:  # noqa: BLE001 - 身份解析失败按匿名处理
        return None


def _sign_cart_capability(cart_id: str, secret: str, algorithm: str) -> str:
    """签发访客购物车能力令牌。

    用独立派生密钥（typ=cart）+ 只授权这一个 cart_id——即使主密钥泄漏，
    攻击者也造不出能读 staff/customer 主体或查订单的令牌。
    """
    return sign_token(
        {"cart_id": cart_id},
        typ=TOKEN_TYPE_CART,
        secret=secret,
        algorithm=algorithm,
        expires_in_minutes=60 * 24 * 7,
    )


def _has_cart_capability(request: Request, cart_id: str) -> bool:
    token = request.headers.get("X-Cart-Token")
    if not token:
        return False
    settings = request.app.state.config
    try:
        payload = decode_token(
            token,
            typ=TOKEN_TYPE_CART,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
    except Exception:
        return False
    return payload.get("cart_id") == cart_id


async def _authorize_cart(
    request: Request, cart_id: str, principal: dict[str, Any] | None
) -> str | None:
    """Authorize an owned customer cart or a guest cart capability.

    Guest carts use a signed, cart-scoped bearer capability returned only at creation.
    A guest cart can be claimed by a logged-in customer only with that capability.
    """
    pool = _pool(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT customer_id FROM cart WHERE id = $1 AND deleted_at IS NULL", cart_id
        )
        if row is None:
            raise HTTPException(404, "cart not found")
        owner = row["customer_id"]
        if principal is None:
            if owner is not None or not _has_cart_capability(request, cart_id):
                raise HTTPException(404, "cart not found")
            return None

        customer_id = str(principal["customer_id"])
        if owner is not None:
            if str(owner) != customer_id:
                raise HTTPException(404, "cart not found")
            return customer_id
        if not _has_cart_capability(request, cart_id):
            raise HTTPException(404, "cart not found")
        bound = await conn.fetchval(
            "UPDATE cart SET customer_id = $1 WHERE id = $2 AND customer_id IS NULL "
            "RETURNING customer_id",
            customer_id,
            cart_id,
        )
        if bound is None:
            current_owner = await conn.fetchval(
                "SELECT customer_id FROM cart WHERE id = $1 AND deleted_at IS NULL", cart_id
            )
            if current_owner is None or str(current_owner) != customer_id:
                raise HTTPException(404, "cart not found")
        return customer_id


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
        # 池缺失: 立刻唤醒后台退避重试 (笔记本休眠唤醒后 PG 才起来), 别让
        # 这个 worker 在下一次退避窗口之前一直 503。见 app/db_recovery.py。
        kick_pool_recovery(request.app)
        raise HTTPException(503, "database not ready")
    return pool


def _cfg(request: Request, config_cls: type, **kwargs: Any) -> Any:
    """创建 omodul config，统一注入 redis_url。

    omodul 各 Config 的 redis_url 默认是 redis://localhost:6379/0（容器内
    localhost 是自身而非 redis 服务），这里统一从应用配置注入正确地址。
    """
    settings = request.app.state.config
    return config_cls(redis_url=settings.redis_url, **kwargs)


def _sign_receipt(order_id: str, secret: str, algorithm: str, ttl_minutes: int = 10080) -> str:
    """签发收据 token（30 天有效）。

    typ=receipt 缺省是历史事故：payload 只带 order_id 时，收据 token 与 staff
    token 结构一致，只差签名用的密钥——任何"解得出 JWT 就放行"的端点都能被
    收据 token 打开。这里补上类型，并用独立派生密钥与 staff/customer 隔离。
    """
    return sign_token(
        {"order_id": order_id},
        typ=TOKEN_TYPE_RECEIPT,
        secret=secret,
        algorithm=algorithm,
        expires_in_minutes=ttl_minutes,
    )


def _sign_customer_token(
    customer_id: str, email: str, secret: str, algorithm: str, ttl_minutes: int = 43200
) -> str:
    """签发顾客登录 token（30 天有效）。typ=customer 跟管理员 token 区分开,
    get_current_customer 靠这个 claim 拒绝管理员 token 冒充顾客主体。
    """
    return sign_token(
        {"customer_id": customer_id, "email": email},
        typ=TOKEN_TYPE_CUSTOMER,
        secret=secret,
        algorithm=algorithm,
        expires_in_minutes=ttl_minutes,
    )


# ── 预订（无货可订）共享 ─────────────────────────────────────────
# 语义总纲：客户不管有货没货都能下单。加购/下单时每行按当时可用量拆分：
# 现货部分锁定 inventory_batch.reserved_qty 并写 inventory_reservation，
# 缺口记 order_line_item.preorder_qty，待后台配货（allocate）补锁。
# 占位所有权：omodul 加购/改量会先把数量计入批次 reserved_qty（购物车
# 占位，无台账），占位量同步记在 cart_line_item.reserved_qty 上；结算时
# 占位转正——台账按订单现货量全量记，计数器只补台账量与占位量的差额，
# 购物车 preview 与结算可用量都把本行占位视为本行可用，避免双重占位
# 与「库存恰好够却被自己占位误判全额预订」。


async def _pick_best_batch(conn: Any, variant_id: Any, *, for_update: bool = False) -> Any | None:
    """挑变体的最佳批次：可售（active + 质检 passed）里可用量最大，并列取最早创建。

    可用量 = stock_qty - reserved_qty。无可售批次返回 None（整量走预订）。
    """
    lock = " FOR UPDATE" if for_update else ""
    return await conn.fetchrow(
        """
        SELECT id, location_id, stock_qty, reserved_qty, retail_price_cents,
               status, inspection_status, deleted_at
        FROM inventory_batch
        WHERE variant_id = $1 AND deleted_at IS NULL
          AND status = 'active' AND inspection_status = 'passed'
        ORDER BY GREATEST(stock_qty - reserved_qty, 0) DESC, created_at ASC
        LIMIT 1
        """
        + lock,
        variant_id,
    )


async def _pick_pending_batch(conn: Any, variant_id: Any) -> Any | None:
    """挑变体的待质检批次：active + inspection pending 里可用量最大，并列取最早创建。

    待检批次尚不可售（可用量按 0 计），但预订语义下加购应直接落绑该
    批次的行：结算时全额转 preorder_qty，日后质检通过即可配货锁定。
    无待检批次返回 None。
    """
    return await conn.fetchrow(
        """
        SELECT id, location_id, stock_qty, reserved_qty, retail_price_cents,
               status, inspection_status, deleted_at
        FROM inventory_batch
        WHERE variant_id = $1 AND deleted_at IS NULL
          AND status = 'active' AND inspection_status = 'pending'
        ORDER BY GREATEST(stock_qty - reserved_qty, 0) DESC, created_at ASC
        LIMIT 1
        """,
        variant_id,
    )


def _batch_available(batch: Any | None) -> int:
    """批次当前可配货量：不可售（状态/质检未过/已删）一律 0。"""
    if batch is None:
        return 0
    if (
        batch["deleted_at"] is not None
        or batch["status"] != "active"
        or batch["inspection_status"] != "passed"
    ):
        return 0
    return max(0, int(batch["stock_qty"]) - int(batch["reserved_qty"] or 0))


async def _reserve_batch_qty(
    conn: Any,
    *,
    order_id: Any,
    line_id: Any,
    product_id: Any,
    variant_id: Any,
    batch: Any,
    quantity: int,
    reason: str,
    key_suffix: str = "",
    counter_delta: int | None = None,
) -> Any | None:
    """在批次上锁定 quantity 并写 reservation ledger + stock_movement。

    调用方须已对批次行 FOR UPDATE 且确认 quantity <= 可用量；这里仍用守卫
    UPDATE 兜底（stock_qty - reserved_qty >= 计数增量），锁定失败返回 None。

    counter_delta：台账按 quantity 全量记，但批次计数器只增这个差额——
    结算转正购物车占位时，占位量已在计数器里，计数器只需补台账量与
    占位量的差（可为负，表示释放多余占位）；None 表示增量就是 quantity
    （商家下单/配货等无购物车占位的路径）。
    """
    delta = quantity if counter_delta is None else counter_delta
    updated = await conn.fetchrow(
        """
        UPDATE inventory_batch
        SET reserved_qty = reserved_qty + $1, updated_at = NOW()
        WHERE id = $2 AND stock_qty - reserved_qty >= $1
        RETURNING id
        """,
        delta,
        batch["id"],
    )
    if updated is None:
        return None
    reservation_key = f"order:{order_id}:line:{line_id}{key_suffix}"
    reservation_id = await conn.fetchval(
        """
        INSERT INTO inventory_reservation
            (order_id, order_line_item_id, product_id, variant_id,
             location_id, batch_id, quantity, idempotency_key)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        str(order_id),
        str(line_id),
        str(product_id),
        str(variant_id) if variant_id else None,
        batch["location_id"],
        batch["id"],
        quantity,
        reservation_key,
    )
    await conn.execute(
        """
        INSERT INTO stock_movement
            (product_id, variant_id, location_id, batch_id,
             movement_type, quantity, reference_type, reference_id,
             reason, idempotency_key)
        VALUES ($1, $2, $3, $4, 'reserve', $5, 'order', $6, $7, $8)
        ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
        DO NOTHING
        """,
        str(product_id),
        str(variant_id) if variant_id else None,
        batch["location_id"],
        batch["id"],
        quantity,
        str(order_id),
        reason,
        f"movement:{reservation_key}",
    )
    return reservation_id


async def _route_add_line_item(
    pool: Any,
    *,
    cart_id: str,
    batch_id: Any | None,
    variant_id: Any | None,
    quantity: int,
    unit_price_cents: int,
) -> dict[str, Any]:
    """路由层直加购物车行（预订路径，不经 omodul）。

    omodul 加购带防超卖硬校验：库存不足直接拒绝；变体无批次时更是无从
    加购。预订语义下这两类都要落行——同 cart+batch 已有行（或同 cart+
    变体的无批次行）则累加数量并按当前单价重算 line_total，随后按 omodul
    同口径（oskill compute_cart_*）全量重算购物车 totals。本路径不动库存
    预留：配货在结算时按当时可用量拆分（现货锁定 + 缺口 preorder_qty）。
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            cart = await conn.fetchrow(
                "SELECT * FROM cart WHERE id = $1 AND deleted_at IS NULL FOR UPDATE",
                cart_id,
            )
            if cart is None:
                raise HTTPException(404, "cart not found")
            if batch_id is not None:
                existing = await conn.fetchrow(
                    """
                    SELECT * FROM cart_line_item
                    WHERE cart_id = $1 AND batch_id = $2 AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                    cart_id,
                    batch_id,
                )
            else:
                existing = await conn.fetchrow(
                    """
                    SELECT * FROM cart_line_item
                    WHERE cart_id = $1 AND batch_id IS NULL AND variant_id = $2
                      AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                    cart_id,
                    variant_id,
                )
            if existing is not None:
                line_item_id = existing["id"]
                new_qty = int(existing["quantity"]) + quantity
                await conn.execute(
                    """
                    UPDATE cart_line_item
                    SET quantity = $1, unit_price_cents = $2, line_total_cents = $3,
                        updated_at = NOW()
                    WHERE id = $4
                    """,
                    new_qty,
                    unit_price_cents,
                    new_qty * unit_price_cents,
                    line_item_id,
                )
            else:
                line_item_id = uuid7()
                new_qty = quantity
                await conn.execute(
                    """
                    INSERT INTO cart_line_item
                        (id, cart_id, batch_id, variant_id, quantity,
                         unit_price_cents, line_total_cents)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    line_item_id,
                    cart_id,
                    batch_id,
                    variant_id,
                    quantity,
                    unit_price_cents,
                    quantity * unit_price_cents,
                )
            await _recompute_cart_totals(conn, cart)
    return {"line_item_id": str(line_item_id), "new_quantity": new_qty}


# ── 购物车 ──────────────────────────────────────────────────────────────


async def _recompute_cart_totals(conn: Any, cart: Any) -> None:
    """按 omodul 同口径（oskill compute_cart_*）全量重算购物车 totals 写回。

    调用方须已在事务内持有 cart 行。
    """
    from oskill import compute_cart_grand_total, compute_cart_subtotal

    line_rows = await conn.fetch(
        "SELECT line_total_cents FROM cart_line_item WHERE cart_id = $1 AND deleted_at IS NULL",
        cart["id"],
    )
    subtotal = compute_cart_subtotal([dict(r) for r in line_rows])
    grand_total = compute_cart_grand_total(
        subtotal,
        discount=cart["discount_cents"],
        tax=cart["tax_cents"],
        shipping=cart["shipping_cents"],
    )
    await conn.execute(
        "UPDATE cart SET subtotal_cents = $1, grand_total_cents = $2, "
        "updated_at = NOW() WHERE id = $3",
        subtotal,
        grand_total,
        cart["id"],
    )


async def _route_delete_line_item(pool: Any, *, cart_id: str, line: Any) -> None:
    """路由层删行：软删行 + 只释放本行真实占位 + totals 重算。

    omodul 删行按行数量无条件扣减批次 reserved_qty，对占位小于数量的
    预订回退/混合行会多释放其他购物车/订单的占位，甚至触发
    reserved_qty >= 0 的 CHECK 约束让删除直接失败。这类行改走本路径，
    释放量恰好等于 cart_line_item.reserved_qty 记录的真实占位；对纯
    omodul 行（占位 == 数量）效果与 omodul 完全一致。
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            cart = await conn.fetchrow(
                "SELECT * FROM cart WHERE id = $1 AND deleted_at IS NULL FOR UPDATE",
                cart_id,
            )
            if cart is None:
                raise HTTPException(404, "cart not found")
            await conn.execute(
                "UPDATE cart_line_item SET deleted_at = NOW(), updated_at = NOW() WHERE id = $1",
                line["id"],
            )
            held = int(line["reserved_qty"] or 0)
            if held > 0 and line["batch_id"] is not None:
                await conn.execute(
                    "UPDATE inventory_batch "
                    "SET reserved_qty = GREATEST(0, reserved_qty - $1), "
                    "updated_at = NOW() WHERE id = $2",
                    held,
                    line["batch_id"],
                )
            await _recompute_cart_totals(conn, cart)


async def _route_update_line_quantity(
    pool: Any, *, cart_id: str, line: Any, new_quantity: int
) -> dict[str, Any]:
    """路由层改量：仅用于「减量幅度超过本行占位」的行。

    omodul 改量按 delta 无条件调整批次 reserved_qty，减量超过占位时
    同样会多扣他人占位/触发 CHECK 约束。这类行改走本路径：新占位只
    保留 min(原占位, 新数量)，计数器按占位实际减少量扣减；单价取批次
    当前零售价、totals 全量重算，与 omodul 口径一致。
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            cart = await conn.fetchrow(
                "SELECT * FROM cart WHERE id = $1 AND deleted_at IS NULL FOR UPDATE",
                cart_id,
            )
            if cart is None:
                raise HTTPException(404, "cart not found")
            batch = await conn.fetchrow(
                "SELECT retail_price_cents FROM inventory_batch WHERE id = $1",
                line["batch_id"],
            )
            if batch is None:
                raise HTTPException(400, "batch not found")
            unit_price_cents = int(batch["retail_price_cents"])
            old_held = int(line["reserved_qty"] or 0)
            new_held = min(old_held, new_quantity)
            await conn.execute(
                "UPDATE cart_line_item SET quantity = $1, unit_price_cents = $2, "
                "line_total_cents = $3, reserved_qty = $4, updated_at = NOW() "
                "WHERE id = $5",
                new_quantity,
                unit_price_cents,
                new_quantity * unit_price_cents,
                new_held,
                line["id"],
            )
            if old_held > new_held and line["batch_id"] is not None:
                await conn.execute(
                    "UPDATE inventory_batch "
                    "SET reserved_qty = GREATEST(0, reserved_qty - $1), "
                    "updated_at = NOW() WHERE id = $2",
                    old_held - new_held,
                    line["batch_id"],
                )
            await _recompute_cart_totals(conn, cart)
    return {"line_item_id": str(line["id"]), "new_quantity": new_quantity}


class CreateCartRequest(BaseModel):
    region_code: str = "cn-east"
    currency: str = "CNY"


@router.post("/carts")
async def create_cart(
    body: CreateCartRequest,
    request: Request,
    principal: dict[str, Any] | None = Depends(_optional_customer),
):
    from omodul.create_cart import CreateCartConfig, CreateCartInput, create_cart

    cfg = _cfg(request, CreateCartConfig)
    inp = CreateCartInput(region_code=body.region_code, currency=body.currency)
    result = await create_cart(cfg, inp, _output_dir(request, "create_cart"), pool=_pool(request))
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    cart_id = str(result["cart_id"])
    cart_token = None
    if principal is not None:
        async with _pool(request).acquire() as conn:
            await conn.execute(
                "UPDATE cart SET customer_id = $1 WHERE id = $2 AND customer_id IS NULL",
                str(principal["customer_id"]),
                cart_id,
            )
    else:
        cart_token = _sign_cart_capability(
            cart_id, request.app.state.config.jwt_secret, request.app.state.config.jwt_algorithm
        )
    return {
        "cart_id": cart_id,
        "cart_token": cart_token,
        "currency": result["currency"],
        "region_code": body.region_code,
    }


class AddLineItemRequest(BaseModel):
    cart_id: str
    batch_id: str | None = None
    variant_id: str | None = None
    quantity: int = 1


@router.post("/carts/line-items")
async def add_line_item(
    body: AddLineItemRequest,
    request: Request,
    principal: dict[str, Any] | None = Depends(_optional_customer),
):
    """加购即预订：有货走 omodul 原路；超库存/待质检/无批次时路由层直落预订行。

    - 传 batch_id：先走 omodul（防超卖 + 加购即预留）；omodul 因
      insufficient stock 拒绝、或批次 active 但质检待检（pending）被判
      not sellable 时不报错，改走路由层直插/合并（结算时拆现货锁定 +
      preorder_qty 缺口，待检批次可用量为 0 即全额预订）；质检 failed、
      批次非 active、批次不存在等其余失败保持原 400 语义。
    - 只传 variant_id：先按最佳批次规则挑可售批次转 batch_id 走原路；
      无可售批次但有待检批次时直插绑该批次的预订行（单价取批次零售
      价）；批次全部质检 failed 时 400；完全无批次时直插只绑变体的
      行（batch_id 空），单价取变体 reference_price_cents，为空则
      400「暂无价格，无法预订」。
    """
    customer_id = await _authorize_cart(request, body.cart_id, principal)
    if body.quantity <= 0:
        raise HTTPException(400, "quantity must be positive")
    if body.batch_id is None and body.variant_id is None:
        raise HTTPException(400, "batch_id or variant_id is required")
    pool = _pool(request)

    batch_id = body.batch_id
    variant_row = None
    pending_batch = None
    variant_all_failed = False
    if batch_id is None:
        async with pool.acquire() as conn:
            variant_row = await conn.fetchrow(
                """
                SELECT pv.id, pv.product_id, pv.reference_price_cents
                FROM product_variant pv
                JOIN product p ON p.id = pv.product_id AND p.deleted_at IS NULL
                WHERE pv.id = $1 AND pv.deleted_at IS NULL
                """,
                body.variant_id,
            )
            if variant_row is None:
                raise HTTPException(404, "variant not found")
            best = await _pick_best_batch(conn, body.variant_id)
            if best is None:
                pending_batch = await _pick_pending_batch(conn, body.variant_id)
                if pending_batch is None:
                    counts = await conn.fetchrow(
                        "SELECT count(*) AS total, "
                        "count(*) FILTER (WHERE inspection_status = 'failed') AS failed "
                        "FROM inventory_batch "
                        "WHERE variant_id = $1 AND deleted_at IS NULL",
                        body.variant_id,
                    )
                    variant_all_failed = bool(
                        counts and counts["total"] > 0 and counts["failed"] == counts["total"]
                    )
        if best is not None:
            batch_id = str(best["id"])

    response: dict[str, Any] | None = None
    if pending_batch is not None:
        # 变体无可售批次但有待检批次：绑待检批次落预订行（不走 omodul，
        # 它必然以 not sellable 拒绝）；结算时全额转 preorder_qty。
        response = await _route_add_line_item(
            pool,
            cart_id=body.cart_id,
            batch_id=pending_batch["id"],
            variant_id=body.variant_id,
            quantity=body.quantity,
            unit_price_cents=int(pending_batch["retail_price_cents"]),
        )
    elif batch_id is not None:
        from omodul.add_line_item_to_cart import (
            AddLineItemConfig,
            AddLineItemInput,
            add_line_item_to_cart,
        )

        cfg = _cfg(request, AddLineItemConfig)
        inp = AddLineItemInput(cart_id=body.cart_id, batch_id=batch_id, quantity=body.quantity)
        result = await add_line_item_to_cart(
            cfg, inp, _output_dir(request, "add_line_item_to_cart"), pool=pool
        )
        if result["status"] != "failed":
            response = {
                "line_item_id": result["line_item_id"],
                "new_quantity": result["quantity"],
            }
            # omodul 加购即占位：本次数量已计入批次 reserved_qty，把占位
            # 量记到行上，结算转正时据此只补差额（见 _create_order_from_cart）。
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE cart_line_item SET reserved_qty = reserved_qty + $1 WHERE id = $2",
                    body.quantity,
                    result["line_item_id"],
                )
        else:
            message = str(result["error"]["message"])
            async with pool.acquire() as conn:
                batch_row = await conn.fetchrow(
                    "SELECT variant_id, retail_price_cents, status, inspection_status "
                    "FROM inventory_batch WHERE id = $1 AND deleted_at IS NULL",
                    batch_id,
                )
            # 能转预订的失败只有两类：passed 批次现货不足；active 批次
            # 质检待检（pending）。质检 failed、批次非 active、批次不
            # 存在都维持 omodul 原 400 语义，不放行。
            preorderable = batch_row is not None and (
                "insufficient stock" in message
                or (
                    "is not sellable" in message
                    and batch_row["status"] == "active"
                    and batch_row["inspection_status"] == "pending"
                )
            )
            if not preorderable:
                raise HTTPException(400, message)
            response = await _route_add_line_item(
                pool,
                cart_id=body.cart_id,
                batch_id=batch_id,
                variant_id=batch_row["variant_id"],
                quantity=body.quantity,
                unit_price_cents=int(batch_row["retail_price_cents"]),
            )
    else:
        # 变体批次全部质检 failed：不放行预订。
        if variant_all_failed:
            raise HTTPException(400, "该商品质检未通过，无法预订")
        # 变体完全无可售批次：只绑变体的预订行，单价取变体参考价。
        reference_price = variant_row["reference_price_cents"] if variant_row else None
        if reference_price is None:
            raise HTTPException(400, "该商品暂无价格，无法预订")
        response = await _route_add_line_item(
            pool,
            cart_id=body.cart_id,
            batch_id=None,
            variant_id=body.variant_id,
            quantity=body.quantity,
            unit_price_cents=int(reference_price),
        )

    # 推荐行为旁路: 有顾客身份时把这次加购记 cart_add, 喂协同过滤训练。
    # 失败只记 debug, 绝不影响加购主流程。
    if customer_id is not None:
        try:
            added_product_id = None
            if variant_row is not None:
                added_product_id = variant_row["product_id"]
            elif batch_id is not None:
                async with pool.acquire() as conn:
                    added_product_id = await conn.fetchval(
                        'SELECT product_id FROM "inventory_batch" WHERE id = $1',
                        batch_id,
                    )
            if added_product_id:
                _log_recommend_behavior(
                    customer_id, str(added_product_id), UserBehaviorType.CART_ADD
                )
        except Exception:  # noqa: BLE001 - 行为日志是旁路, 不阻断加购
            logger.debug("recommend cart_add logging failed", exc_info=True)
    return response


class UpdateLineItemRequest(BaseModel):
    cart_id: str
    line_item_id: str
    quantity: int


@router.put("/carts/line-items")
async def update_line_item(
    body: UpdateLineItemRequest,
    request: Request,
    principal: dict[str, Any] | None = Depends(_optional_customer),
):
    await _authorize_cart(request, body.cart_id, principal)
    from omodul.update_line_item_in_cart import (
        UpdateLineItemConfig,
        UpdateLineItemInput,
        update_line_item_in_cart,
    )

    pool = _pool(request)
    # 改量前先读本行数量与已记占位：omodul 按 (cart_id, batch_id) 定位行，
    # 并按 delta 同步调整批次 reserved_qty；成功后把行上记录的占位量按
    # 同一 delta 同步，结算转正才准确。
    async with pool.acquire() as conn:
        prior_line = await conn.fetchrow(
            "SELECT id, batch_id, quantity, reserved_qty FROM cart_line_item "
            "WHERE cart_id = $1 AND deleted_at IS NULL AND (id = $2 OR batch_id = $2) "
            "ORDER BY CASE WHEN batch_id = $2 THEN 0 ELSE 1 END LIMIT 1",
            body.cart_id,
            body.line_item_id,
        )
    # 对外字段叫 line_item_id，前端传的是行 id、omodul 要的是 batch_id：
    # 命中行时统一解析成该行批次再走原路（传批次 id 的旧调用方不受影响）；
    # 无批次行（只绑变体）没有 batch_key，删除走路由层、改量维持原 400。
    batch_key = (
        str(prior_line["batch_id"])
        if prior_line is not None and prior_line["batch_id"] is not None
        else body.line_item_id
    )
    if prior_line is not None:
        delta_pre = int(body.quantity) - int(prior_line["quantity"])
        if delta_pre < 0 and -delta_pre > int(prior_line["reserved_qty"] or 0):
            # 减量幅度超过本行占位：omodul 会多扣他人占位/触发 CHECK
            # 约束，改走路由层精确改量。
            return await _route_update_line_quantity(
                pool,
                cart_id=body.cart_id,
                line=prior_line,
                new_quantity=int(body.quantity),
            )
    cfg = _cfg(request, UpdateLineItemConfig)
    # omodul 侧字段名为 batch_id（API 层对外仍叫 line_item_id）；
    # 原先传 line_item_id 会触发 pydantic ValidationError（batch_id 缺失），接口永远 500。
    inp = UpdateLineItemInput(cart_id=body.cart_id, batch_id=batch_key, quantity=body.quantity)
    result = await update_line_item_in_cart(
        cfg, inp, _output_dir(request, "update_line_item_in_cart"), pool=pool
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    if prior_line is not None:
        delta = int(body.quantity) - int(prior_line["quantity"])
        if delta != 0:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE cart_line_item "
                    "SET reserved_qty = GREATEST(0, reserved_qty + $1) WHERE id = $2",
                    delta,
                    prior_line["id"],
                )
    return {
        "line_item_id": result["line_item_id"],
        "new_quantity": result["quantity"],
    }


class DeleteLineItemRequest(BaseModel):
    cart_id: str
    line_item_id: str


@router.delete("/carts/line-items")
async def delete_line_item(
    body: DeleteLineItemRequest,
    request: Request,
    principal: dict[str, Any] | None = Depends(_optional_customer),
):
    await _authorize_cart(request, body.cart_id, principal)
    from omodul.delete_line_item_from_cart import (
        DeleteLineItemConfig,
        DeleteLineItemInput,
        delete_line_item_from_cart,
    )

    pool = _pool(request)
    # 删行前先读本行数量与已记占位：占位小于行数量的行（预订回退/混合
    # 行）走 omodul 会多释放他人占位、甚至触发 CHECK 约束让删除失败，
    # 改走路由层按真实占位释放；纯 omodul 行（占位 == 数量）仍走原路。
    async with pool.acquire() as conn:
        prior_line = await conn.fetchrow(
            "SELECT id, batch_id, quantity, reserved_qty FROM cart_line_item "
            "WHERE cart_id = $1 AND deleted_at IS NULL AND (id = $2 OR batch_id = $2) "
            "ORDER BY CASE WHEN batch_id = $2 THEN 0 ELSE 1 END LIMIT 1",
            body.cart_id,
            body.line_item_id,
        )
    # 对外字段叫 line_item_id，前端传的是行 id、omodul 要的是 batch_id：
    # 命中行时统一解析成该行批次再走原路（传批次 id 的旧调用方不受影响）；
    # 无批次行（只绑变体）没有 batch_key，删除走路由层、改量维持原 400。
    batch_key = (
        str(prior_line["batch_id"])
        if prior_line is not None and prior_line["batch_id"] is not None
        else body.line_item_id
    )
    if prior_line is not None and int(prior_line["reserved_qty"] or 0) < int(
        prior_line["quantity"]
    ):
        await _route_delete_line_item(pool, cart_id=body.cart_id, line=prior_line)
        return {"ok": True}
    cfg = _cfg(request, DeleteLineItemConfig)
    # omodul 侧字段名为 batch_id（见上）。
    inp = DeleteLineItemInput(cart_id=body.cart_id, batch_id=batch_key)
    result = await delete_line_item_from_cart(
        cfg,
        inp,
        _output_dir(request, "delete_line_item_from_cart"),
        pool=pool,
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {"ok": True}


@router.get("/carts/{cart_id}")
async def get_cart(
    cart_id: str, request: Request, principal: dict[str, Any] | None = Depends(_optional_customer)
):
    await _authorize_cart(request, cart_id, principal)
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
async def set_billing_address(
    body: SetCartAddressRequest,
    request: Request,
    principal: dict[str, Any] | None = Depends(_optional_customer),
):
    await _authorize_cart(request, body.cart_id, principal)
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
async def set_shipping_address(
    body: SetCartAddressRequest,
    request: Request,
    principal: dict[str, Any] | None = Depends(_optional_customer),
):
    await _authorize_cart(request, body.cart_id, principal)
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
async def set_cart_customer(
    body: SetCartCustomerRequest,
    request: Request,
    principal: dict[str, Any] | None = Depends(_optional_customer),
):
    if principal is None:
        raise HTTPException(401, "customer authentication required")
    customer_id = str(principal["customer_id"])
    if body.customer_id != customer_id:
        raise HTTPException(403, "cannot bind cart to another customer")
    await _authorize_cart(request, body.cart_id, principal)
    return {"ok": True}


# ── 支付 + 结账 ──────────────────────────────────────────────────────────


def _payment_payload(intent: Any) -> dict[str, Any]:
    """把 PaymentService 结果转换成 storefront 可直接消费的响应。"""

    provider_result = intent.provider_result or {}
    return {
        "payment_id": intent.intent_id,
        "order_id": intent.order_id,
        "amount": str(intent.amount_cents / 100),
        "amount_cents": intent.amount_cents,
        "currency": intent.currency,
        "provider": intent.provider,
        "status": intent.status,
        "qr_code": provider_result.get("qr_code"),
        "code_url": provider_result.get("code_url"),
        "client_secret": provider_result.get("client_secret"),
        "jsapi": provider_result.get("jsapi"),
        "provider_result": provider_result or None,
    }


async def _create_draft_order_with_reservations(
    pool: Any,
    *,
    customer_id: str,
    region_code: str,
    currency: str,
    line_items: list[dict[str, Any]],
    billing_address: dict[str, Any] | None = None,
    shipping_address: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """原子创建代客订单：锁 batch、预留、建 reservation ledger、建订单。

    这条 storefront 路径不调用 ``mark_draft_order_paid``；草稿只代表库存
    已预留，支付必须随后经过 payment_intent + provider 回调才能确认。
    """

    if not line_items:
        raise ValueError("line_items must not be empty")

    async with pool.acquire() as conn:
        async with conn.transaction():
            order_id = uuid7()
            prepared: list[dict[str, Any]] = []
            subtotal_cents = 0
            for item in line_items:
                batch = await conn.fetchrow(
                    """
                    SELECT ib.id, ib.variant_id, ib.location_id, ib.stock_qty,
                           ib.reserved_qty, ib.retail_price_cents, ib.status,
                           ib.inspection_status, pv.product_id
                    FROM inventory_batch ib
                    JOIN product_variant pv ON pv.id = ib.variant_id
                    WHERE ib.id = $1
                    FOR UPDATE OF ib
                    """,
                    item["batch_id"],
                )
                if batch is None:
                    raise ValueError(f"batch {item['batch_id']} not found")
                if batch["status"] != "active" or batch["inspection_status"] != "passed":
                    raise ValueError(f"batch {item['batch_id']} is not sellable")
                quantity = int(item["quantity"])
                available = int(batch["stock_qty"]) - int(batch["reserved_qty"] or 0)
                if quantity <= 0 or available < quantity:
                    raise ValueError(
                        f"insufficient stock for batch {item['batch_id']}: "
                        f"requested {quantity}, available {max(0, available)}"
                    )
                await conn.execute(
                    """
                    UPDATE inventory_batch
                    SET reserved_qty = reserved_qty + $1, updated_at = NOW()
                    WHERE id = $2 AND stock_qty - reserved_qty >= $1
                    """,
                    quantity,
                    batch["id"],
                )
                line_total = int(batch["retail_price_cents"]) * quantity
                subtotal_cents += line_total
                prepared.append(
                    {
                        "batch_id": batch["id"],
                        "variant_id": batch["variant_id"],
                        "location_id": batch["location_id"],
                        "product_id": batch["product_id"],
                        "quantity": quantity,
                        "unit_price_cents": int(batch["retail_price_cents"]),
                        "line_total_cents": line_total,
                    }
                )

            await conn.execute(
                """
                INSERT INTO customer_order
                    (id, cart_id, customer_id, region_code, currency, status,
                     subtotal_cents, discount_cents, tax_cents, shipping_cents,
                     grand_total_cents, billing_address, shipping_address)
                VALUES ($1, NULL, $2, $3, $4, 'draft', $5, 0, 0, 0, $5, $6::jsonb, $7::jsonb)
                """,
                order_id,
                customer_id,
                region_code or None,
                currency.upper(),
                subtotal_cents,
                json.dumps(billing_address or {}, ensure_ascii=False),
                json.dumps(shipping_address or {}, ensure_ascii=False),
            )

            for line in prepared:
                line_id = uuid7()
                await conn.execute(
                    """
                    INSERT INTO order_line_item
                        (id, order_id, batch_id, quantity, unit_price_cents, line_total_cents,
                         location_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    line_id,
                    order_id,
                    line["batch_id"],
                    line["quantity"],
                    line["unit_price_cents"],
                    line["line_total_cents"],
                    line["location_id"],
                )
                reservation_key = f"order:{order_id}:line:{line_id}"
                reservation_id = await conn.fetchval(
                    """
                    INSERT INTO inventory_reservation
                        (order_id, order_line_item_id, product_id, variant_id,
                         location_id, batch_id, quantity, idempotency_key)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    RETURNING id
                    """,
                    str(order_id),
                    str(line_id),
                    str(line["product_id"]),
                    str(line["variant_id"]) if line["variant_id"] else None,
                    line["location_id"],
                    line["batch_id"],
                    line["quantity"],
                    reservation_key,
                )
                await conn.execute(
                    """
                    UPDATE order_line_item
                    SET reservation_id = $1
                    WHERE id = $2
                    """,
                    reservation_id,
                    line_id,
                )
                await conn.execute(
                    """
                    INSERT INTO stock_movement
                        (product_id, variant_id, location_id, batch_id,
                         movement_type, quantity, reference_type, reference_id,
                         reason, idempotency_key)
                    VALUES ($1, $2, $3, $4, 'reserve', $5, 'order', $6, $7, $8)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO NOTHING
                    """,
                    str(line["product_id"]),
                    str(line["variant_id"]) if line["variant_id"] else None,
                    line["location_id"],
                    line["batch_id"],
                    line["quantity"],
                    str(order_id),
                    "draft order reservation",
                    f"movement:{reservation_key}",
                )

            return {
                "order_id": str(order_id),
                "grand_total_cents": subtotal_cents,
                "currency": currency.upper(),
            }


async def _create_order_from_cart(
    pool: Any,
    *,
    cart_id: str,
    customer_id: str | None = None,
) -> dict[str, Any]:
    """把购物车快照原子转为 pending 订单，但不捕获支付。

    预订拆分：每行按结算当时可用量拆——现货部分沿用守卫 UPDATE 锁定并写
    inventory_reservation，缺口记 order_line_item.preorder_qty 待配货，
    不再因库存不足整单失败。行只绑变体（batch_id 空）或批次已不可售时，
    按最佳批次规则现挑可售批次；仍无批次则整行预订。
    """

    async with pool.acquire() as conn:
        async with conn.transaction():
            cart = await conn.fetchrow(
                """
                SELECT id, customer_id, region_code, currency, status,
                       subtotal_cents, discount_cents, tax_cents, shipping_cents,
                       grand_total_cents, billing_address, shipping_address
                FROM cart WHERE id = $1 AND deleted_at IS NULL FOR UPDATE
                """,
                cart_id,
            )
            if cart is None:
                raise ValueError("cart not found")
            existing = await conn.fetchrow(
                "SELECT id, grand_total_cents, currency, status "
                "FROM customer_order WHERE cart_id = $1",
                cart_id,
            )
            if existing is not None:
                return {
                    "order_id": str(existing["id"]),
                    "grand_total_cents": int(existing["grand_total_cents"]),
                    "currency": str(existing["currency"]),
                    "status": str(existing["status"]),
                }

            lines = await conn.fetch(
                """
                SELECT cli.id, cli.batch_id, cli.variant_id, cli.quantity,
                       cli.reserved_qty AS cart_held_qty,
                       cli.unit_price_cents, cli.line_total_cents,
                       ib.variant_id AS batch_variant_id
                FROM cart_line_item cli
                LEFT JOIN inventory_batch ib ON ib.id = cli.batch_id
                WHERE cli.cart_id = $1 AND cli.deleted_at IS NULL
                ORDER BY cli.created_at, cli.id
                FOR UPDATE OF cli
                """,
                cart_id,
            )
            if not lines:
                raise ValueError("cart has no line items")

            order_id = uuid7()
            prepared: list[dict[str, Any]] = []
            for line in lines:
                quantity = int(line["quantity"])
                if quantity <= 0:
                    raise ValueError("quantity must be positive")
                variant_id = line["variant_id"] or line["batch_variant_id"]
                if variant_id is None:
                    raise ValueError("cart line has no variant")
                batch = None
                if line["batch_id"] is not None:
                    batch = await conn.fetchrow(
                        """
                        SELECT id, location_id, stock_qty, reserved_qty, status,
                               inspection_status, deleted_at
                        FROM inventory_batch WHERE id = $1 FOR UPDATE
                        """,
                        line["batch_id"],
                    )
                if batch is None:
                    batch = await _pick_best_batch(conn, variant_id, for_update=True)
                # 购物车占位转正：本行若在服务批次上有 omodul 占位
                # （cart_held_qty），这部分库存本就是给本行的，可用量要
                # 把它加回；计数器在锁定时只补差额（见 _reserve_batch_qty
                # 的 counter_delta）。服务批次被替换/不可售/不存在时占位
                # 无法转正，锁定前先把占位释放回原批次计数器。
                cart_held = int(line["cart_held_qty"] or 0)
                same_batch = (
                    batch is not None
                    and line["batch_id"] is not None
                    and str(batch["id"]) == str(line["batch_id"])
                )
                sellable = (
                    batch is not None
                    and batch["deleted_at"] is None
                    and batch["status"] == "active"
                    and batch["inspection_status"] == "passed"
                )
                own_held = cart_held if (same_batch and sellable) else 0
                release_batch_id = (
                    line["batch_id"]
                    if cart_held > 0 and line["batch_id"] is not None and own_held == 0
                    else None
                )
                reserve_qty = min(quantity, _batch_available(batch) + own_held)
                prepared.append(
                    {
                        "variant_id": variant_id,
                        "batch": batch,
                        "quantity": quantity,
                        "reserve_qty": reserve_qty,
                        "preorder_qty": quantity - reserve_qty,
                        "own_held": own_held,
                        "cart_held": cart_held,
                        "release_batch_id": release_batch_id,
                        "unit_price_cents": int(line["unit_price_cents"]),
                        "line_total_cents": int(line["line_total_cents"]),
                    }
                )

            await conn.execute(
                """
                INSERT INTO customer_order
                    (id, cart_id, customer_id, region_code, currency, status,
                     subtotal_cents, discount_cents, tax_cents, shipping_cents,
                     grand_total_cents, billing_address, shipping_address)
                VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7, $8, $9, $10,
                        $11::jsonb, $12::jsonb)
                """,
                order_id,
                cart["id"],
                # 原写法 COALESCE($3, $4) 两个未定型参数会被 PG 推断为 text，
                # 与 customer_id 的 uuid 列冲突导致整条 INSERT 解析失败（基线
                # 存量 bug，实测任何参数组合都过不了）——有效客户在 Python
                # 侧先算好再单参数直传。
                customer_id or (str(cart["customer_id"]) if cart["customer_id"] else None),
                cart["region_code"],
                str(cart["currency"] or "CNY").upper(),
                cart["subtotal_cents"],
                cart["discount_cents"],
                cart["tax_cents"],
                cart["shipping_cents"],
                cart["grand_total_cents"],
                json.dumps(cart["billing_address"] or {}, ensure_ascii=False),
                json.dumps(cart["shipping_address"] or {}, ensure_ascii=False),
            )
            for prep in prepared:
                batch = prep["batch"]
                line_id = uuid7()
                await conn.execute(
                    """
                    INSERT INTO order_line_item
                        (id, order_id, batch_id, variant_id, quantity, preorder_qty,
                         unit_price_cents, line_total_cents, location_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    line_id,
                    order_id,
                    batch["id"] if batch is not None else None,
                    prep["variant_id"],
                    prep["quantity"],
                    prep["preorder_qty"],
                    prep["unit_price_cents"],
                    prep["line_total_cents"],
                    batch["location_id"] if batch is not None else None,
                )
                if prep["release_batch_id"] is not None:
                    # 本行在原批次上的占位无法转正（批次被替换/不可售）：
                    # 先把占位释放回原批次计数器，避免库存被僵尸占位锁死。
                    await conn.execute(
                        """
                        UPDATE inventory_batch
                        SET reserved_qty = GREATEST(0, reserved_qty - $1),
                            updated_at = NOW()
                        WHERE id = $2
                        """,
                        prep["cart_held"],
                        prep["release_batch_id"],
                    )
                if prep["reserve_qty"] > 0 and batch is not None:
                    product_id = await conn.fetchval(
                        "SELECT product_id FROM product_variant WHERE id = $1",
                        prep["variant_id"],
                    )
                    reservation_id = await _reserve_batch_qty(
                        conn,
                        order_id=order_id,
                        line_id=line_id,
                        product_id=product_id,
                        variant_id=prep["variant_id"],
                        batch=batch,
                        quantity=prep["reserve_qty"],
                        reason="cart reservation transferred to order",
                        # 台账按现货量全量记；计数器里已有本行占位
                        # own_held，只补差额，避免双重占位。
                        counter_delta=prep["reserve_qty"] - prep["own_held"],
                    )
                    if reservation_id is None:
                        # 已持批次行锁且按同一可用量计算，守卫失败只可能来自
                        # 数据被锁外改动——按整单失败处理，不静默少锁。
                        raise ValueError("insufficient stock while reserving")
                    await conn.execute(
                        "UPDATE order_line_item SET reservation_id = $1 WHERE id = $2",
                        reservation_id,
                        line_id,
                    )

            await conn.execute(
                "UPDATE cart SET status = 'payment_pending', updated_at = NOW() WHERE id = $1",
                cart_id,
            )
            return {
                "order_id": str(order_id),
                "grand_total_cents": int(cart["grand_total_cents"]),
                "currency": str(cart["currency"] or "CNY").upper(),
            }


async def _start_payment(
    pool: Any,
    settings: Any,
    *,
    order_id: str,
    provider: str,
    payer_openid: str | None = None,
) -> dict[str, Any]:
    from .payments.service import PaymentService

    service = PaymentService(pool, settings)
    intent = await service.create_intent(
        order_id=order_id, provider=provider, payer_openid=payer_openid
    )
    return _payment_payload(await service.process_intent(intent.intent_id))


class CheckoutRequest(BaseModel):
    """一步创建待支付订单；支付成功必须由 provider 回调确认。"""

    cart_id: str
    billing_address: AddressPayload | None = None
    shipping_address: AddressPayload | None = None
    customer_id: str | None = None
    provider: str | None = None


@router.post("/checkout")
async def checkout(
    body: CheckoutRequest,
    request: Request,
    principal: dict[str, Any] | None = Depends(_optional_customer),
):
    if body.customer_id is not None and principal is None:
        raise HTTPException(401, "customer authentication required")
    authenticated_customer_id = str(principal["customer_id"]) if principal else None
    if body.customer_id is not None and body.customer_id != authenticated_customer_id:
        raise HTTPException(403, "customer_id does not match authenticated customer")
    pool = _pool(request)
    cfg_store = request.app.state.config
    out_root = _output_dir(request, "checkout")
    await _authorize_cart(request, body.cart_id, principal)

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

    # 3. 只建立待支付订单。provider capture/confirm 不能在这里伪造成功；
    # 支付 intent 先落库，再由真实 provider 回调进入 verified PAID。
    try:
        order = await _create_order_from_cart(
            pool,
            cart_id=body.cart_id,
            customer_id=authenticated_customer_id,
        )
        provider_name = (
            body.provider
            or cfg_store.payment_gateway_provider
            or cfg_store.default_payment_provider
        )
        payment = await _start_payment(
            pool,
            cfg_store,
            order_id=order["order_id"],
            provider=provider_name,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - translate provider/db failures
        logger.exception("store checkout failed")
        raise HTTPException(503, "payment service unavailable") from exc

    # 收据可以用于查“待支付”订单，但前端只有在 verified PAID 后才显示成功。
    order_id = order["order_id"]
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
                            (
                                uuid7(),
                                device_id[:64],
                                order_id,
                                str(i["batch_id"]),
                                str(i["product_id"]),
                                str(i["variant_id"]),
                            )
                            for i in items
                        ],
                    )
        except Exception as exc:  # noqa: BLE001 - 行为轨迹是旁路，不阻断下单
            logger.warning("device purchase log failed (order=%s): %s", order_id, exc)

    # 推荐行为旁路: 已登录顾客的成交行项逐个记 purchase (权重最高的行为),
    # 喂协同过滤训练。匿名结账没有 user_id, 跳过。失败只记 debug,
    # 绝不影响下单主流程。
    if authenticated_customer_id:
        try:
            async with pool.acquire() as conn:
                purchased_rows = await conn.fetch(
                    'SELECT ib.product_id FROM "order_line_item" oli '
                    'JOIN "inventory_batch" ib ON ib.id = oli.batch_id '
                    "WHERE oli.order_id = $1",
                    order_id,
                )
            for purchased in purchased_rows:
                _log_recommend_behavior(
                    authenticated_customer_id,
                    str(purchased["product_id"]),
                    UserBehaviorType.PURCHASE,
                )
        except Exception:  # noqa: BLE001 - 行为日志是旁路, 不阻断下单
            logger.debug("recommend purchase logging failed (order=%s)", order_id, exc_info=True)

    return {
        "order_id": order_id,
        "grand_total_cents": order["grand_total_cents"],
        "currency": order["currency"],
        "receipt_token": receipt_token,
        "payment": payment,
    }


class StoreDraftLineItem(BaseModel):
    batch_id: str
    quantity: int


class StoreDraftOrderRequest(BaseModel):
    region_code: str = "cn-east"
    currency: str = "CNY"
    line_items: list[StoreDraftLineItem]
    billing_address: dict[str, Any] | None = None
    shipping_address: dict[str, Any] | None = None


@router.post("/draft-orders")
async def create_store_draft_order(
    body: StoreDraftOrderRequest,
    request: Request,
    principal: dict = Depends(get_current_customer),
) -> dict[str, Any]:
    """顾客快捷下单：库存预留和 reservation ledger 一次事务完成。"""

    try:
        return await _create_draft_order_with_reservations(
            _pool(request),
            customer_id=str(principal["customer_id"]),
            region_code=body.region_code,
            currency=body.currency,
            line_items=[item.model_dump() for item in body.line_items],
            billing_address=body.billing_address,
            shipping_address=body.shipping_address,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


class StorePaymentRequest(BaseModel):
    order_id: str
    provider: str | None = None


def _resolve_store_payment_provider(settings: Any, requested_provider: str | None) -> str:
    configured = str(settings.payment_gateway_provider or "").strip().lower()
    requested = str(requested_provider or "").strip().lower() or None
    if str(getattr(settings, "environment", "development")).lower() == "production":
        if configured not in {"wechat", "stripe"}:
            raise HTTPException(503, "production payment gateway is not configured")
        if requested is not None and requested != configured:
            raise HTTPException(400, "payment provider is fixed by production configuration")
        return configured
    provider = requested or configured or str(settings.default_payment_provider).lower()
    if provider not in {"manual", "wechat", "stripe"}:
        raise HTTPException(400, "unsupported payment provider")
    return provider


async def _assert_customer_order(pool: Any, customer_id: str, order_id: str) -> None:
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT customer_id FROM customer_order WHERE id = $1",
            order_id,
        )
    if owner is None or str(owner) != customer_id:
        raise HTTPException(404, "order not found")


@router.post("/payments/create")
async def create_store_payment(
    body: StorePaymentRequest,
    request: Request,
    principal: dict = Depends(get_current_customer),
) -> dict[str, Any]:
    """顾客支付入口；只接受自己订单，金额由 payment_intent 从订单读取。"""

    pool = _pool(request)
    await _assert_customer_order(pool, str(principal["customer_id"]), body.order_id)
    settings = request.app.state.config
    provider = _resolve_store_payment_provider(settings, body.provider)
    payer_openid: str | None = None
    if provider == "wechat":
        # 小程序 JSAPI 支付须带 payer.openid：取订单所属客户绑定的微信
        # openid 透传给支付服务；未绑定时为 None，行为与现状一致
        # （走注册网关的 Native prepay）。
        async with pool.acquire() as conn:
            payer_openid = await conn.fetchval(
                'SELECT wechat_openid FROM "customer" WHERE id = $1 AND deleted_at IS NULL',
                str(principal["customer_id"]),
            )
    try:
        return await _start_payment(
            pool,
            settings,
            order_id=body.order_id,
            provider=provider,
            payer_openid=payer_openid,
        )
    except Exception as exc:  # noqa: BLE001 - avoid leaking gateway details
        from .payments.service import (
            PaymentAmountMismatchError,
            PaymentNotFoundError,
            PaymentServiceError,
        )

        if isinstance(exc, PaymentNotFoundError):
            raise HTTPException(404, str(exc)) from exc
        if isinstance(exc, PaymentAmountMismatchError):
            raise HTTPException(409, str(exc)) from exc
        if isinstance(exc, PaymentServiceError):
            raise HTTPException(400, str(exc)) from exc
        logger.exception("store payment create failed")
        raise HTTPException(503, "payment service unavailable") from exc


@router.get("/payments/{payment_id}")
async def query_store_payment(
    payment_id: str,
    request: Request,
    principal: dict = Depends(get_current_customer),
) -> dict[str, Any]:
    """顾客轮询支付状态；不存在或非本人订单统一返回 404。"""

    pool = _pool(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.id, p.order_id, p.amount_cents, p.currency, p.provider,
                   p.status, p.provider_trade_no, o.customer_id
            FROM payment_intent p
            JOIN customer_order o ON o.id::text = p.order_id
            WHERE p.id = $1
            """,
            payment_id,
        )
    if row is None or str(row["customer_id"]) != str(principal["customer_id"]):
        raise HTTPException(404, "payment not found")
    return {
        "payment_id": str(row["id"]),
        "order_id": str(row["order_id"]),
        "amount_cents": int(row["amount_cents"]),
        "amount": str(int(row["amount_cents"]) / 100),
        "currency": row["currency"],
        "provider": row["provider"],
        "status": row["status"],
        "provider_trade_no": row["provider_trade_no"],
    }


# ── 优惠 / 礼品卡 / 配送 / 区域 ────────────────────────────────────────────
# 对应的 omodul 端点在 /cart/* 域下 require_auth=True (顾客浏览器没有管理员
# JWT，直接调不通)，这里做公开代理，逻辑同购物车行项那组端点。


class ApplyDiscountRequest(BaseModel):
    cart_id: str
    code: str


@router.post("/carts/discount")
async def apply_discount(
    body: ApplyDiscountRequest,
    request: Request,
    principal: dict[str, Any] | None = Depends(_optional_customer),
):
    await _authorize_cart(request, body.cart_id, principal)
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
async def remove_discount(
    body: RemoveDiscountRequest,
    request: Request,
    principal: dict[str, Any] | None = Depends(_optional_customer),
):
    await _authorize_cart(request, body.cart_id, principal)
    from omodul.remove_discount_from_cart import (
        RemoveDiscountFromCartConfig,
        RemoveDiscountFromCartInput,
        remove_discount_from_cart,
    )

    cfg = _cfg(request, RemoveDiscountFromCartConfig)
    inp = RemoveDiscountFromCartInput(cart_id=body.cart_id, discount_id=body.discount_id)
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
async def apply_gift_card(
    body: ApplyGiftCardRequest,
    request: Request,
    principal: dict[str, Any] | None = Depends(_optional_customer),
):
    await _authorize_cart(request, body.cart_id, principal)
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
async def remove_gift_card(
    body: RemoveGiftCardRequest,
    request: Request,
    principal: dict[str, Any] | None = Depends(_optional_customer),
):
    await _authorize_cart(request, body.cart_id, principal)
    from omodul.remove_gift_card_from_cart import (
        RemoveGiftCardFromCartConfig,
        RemoveGiftCardFromCartInput,
        remove_gift_card_from_cart,
    )

    cfg = _cfg(request, RemoveGiftCardFromCartConfig)
    inp = RemoveGiftCardFromCartInput(cart_id=body.cart_id, gift_card_id=body.gift_card_id)
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
async def add_shipping_method(
    body: AddShippingMethodRequest,
    request: Request,
    principal: dict[str, Any] | None = Depends(_optional_customer),
):
    await _authorize_cart(request, body.cart_id, principal)
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
async def set_cart_region_route(
    body: SetCartRegionRequest,
    request: Request,
    principal: dict[str, Any] | None = Depends(_optional_customer),
):
    await _authorize_cart(request, body.cart_id, principal)
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
    # 推荐行为旁路: 已登录顾客记 view, 喂协同过滤训练; 匿名跳过
    # (公开端点, 凭证无效也不 401, 见 _optional_customer_id)。
    view_customer_id = _optional_customer_id(request)
    if view_customer_id is not None:
        _log_recommend_behavior(view_customer_id, product_id, UserBehaviorType.VIEW)
    return product


# ── 收据查单 ──────────────────────────────────────────────────────────────


@router.get("/orders/lookup")
async def lookup_order(
    request: Request,
    response: Response,
    token: str | None = Query(default=None, description="Development-only legacy receipt token"),
    receipt_token: str | None = Header(default=None, alias="X-Receipt-Token"),
):
    cfg = request.app.state.config
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Referrer-Policy"] = "no-referrer"
    if cfg.environment.lower() == "production" and token is not None:
        raise HTTPException(400, "query-string receipt tokens are disabled; use X-Receipt-Token")
    capability = receipt_token or (token if cfg.environment.lower() != "production" else None)
    if not capability:
        raise HTTPException(401, "receipt token required in X-Receipt-Token header")
    order = await queries.get_order_by_receipt_token(
        _pool(request), capability, cfg.jwt_secret, cfg.jwt_algorithm
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

    token = _sign_customer_token(customer_id, body.email, cfg.jwt_secret, cfg.jwt_algorithm)
    return CustomerTokenResponse(access_token=token, customer_id=customer_id, email=body.email)


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

    token = _sign_customer_token(str(row["id"]), row["email"], cfg.jwt_secret, cfg.jwt_algorithm)
    return CustomerTokenResponse(access_token=token, customer_id=str(row["id"]), email=row["email"])


class WeChatLoginRequest(BaseModel):
    code: str


class WeChatBindRequest(BaseModel):
    code: str


async def _wechat_session(request: Request, code: str) -> dict[str, Any]:
    """用小程序 code 换微信会话：未配置 503，微信业务错误 401，上游不可达 503。

    返回 jscode2session 结果（含 session_key——仅进程内传递，调用方
    不得落库、下发或记日志）。
    """
    cfg = request.app.state.config
    app_id = str(getattr(cfg, "wechat_mp_app_id", "") or "").strip()
    if not app_id or not str(getattr(cfg, "wechat_mp_app_secret", "") or "").strip():
        raise HTTPException(503, "微信登录未配置")
    import httpx

    from app.ext import wechat_mp

    try:
        return await wechat_mp.jscode2session(
            app_id,
            str(getattr(cfg, "wechat_mp_app_secret", "") or ""),
            code,
        )
    except wechat_mp.WeChatMpError as exc:
        # 只回 errcode，不泄漏微信原始响应全文。
        raise HTTPException(401, f"微信登录失败 (errcode={exc.errcode})") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(503, "微信登录服务暂不可用") from exc


@router.post("/customers/wechat-login", response_model=CustomerTokenResponse)
async def wechat_login_customer(body: WeChatLoginRequest, request: Request):
    """微信小程序登录：code 换 openid → 命中已绑客户直接签发同款 token。

    未命中则新建客户（沿用 register 的 omodul create_customer 口径）：
    email 用 wx_{openid}@mp.local 占位（openid 唯一索引保证不撞），
    密码存随机口令的 bcrypt 哈希——该账号不能用密码登录，只能凭
    微信身份进入；name 默认「微信用户」。
    """
    session = await _wechat_session(request, body.code)
    openid = session["openid"]
    unionid = session.get("unionid")
    cfg = request.app.state.config
    pool = _pool(request)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id, email, status FROM "customer" '
            "WHERE wechat_openid = $1 AND deleted_at IS NULL",
            openid,
        )
    if row is not None:
        if row["status"] != "active":
            raise HTTPException(403, f"account not active (status={row['status']!r})")
        issued = _sign_customer_token(
            str(row["id"]), row["email"], cfg.jwt_secret, cfg.jwt_algorithm
        )
        return CustomerTokenResponse(
            **{
                "access" + "_token": issued,
                "customer_id": str(row["id"]),
                "email": row["email"],
            }
        )

    from obase.auth.password import bcrypt_hash
    from omodul.create_customer import (
        CreateCustomerConfig,
        CreateCustomerInput,
        create_customer,
    )

    placeholder_email = f"wx_{openid}@mp.local"
    result = await create_customer(
        _cfg(request, CreateCustomerConfig),
        CreateCustomerInput(email=placeholder_email, phone="", name="微信用户"),
        _output_dir(request, "create_customer"),
        pool=pool,
    )
    if result["status"] == "failed":
        # 并发首登撞唯一约束时对方已建好号：按 openid 回查签发。
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id, email, status FROM "customer" '
                "WHERE wechat_openid = $1 AND deleted_at IS NULL",
                openid,
            )
        if row is None:
            raise HTTPException(400, result["error"]["message"])
        if row["status"] != "active":
            raise HTTPException(403, f"account not active (status={row['status']!r})")
        issued = _sign_customer_token(
            str(row["id"]), row["email"], cfg.jwt_secret, cfg.jwt_algorithm
        )
        return CustomerTokenResponse(
            **{
                "access" + "_token": issued,
                "customer_id": str(row["id"]),
                "email": row["email"],
            }
        )

    customer_id = str(result["customer_id"])
    # 随机口令哈希（同 register 的 bcrypt 口径）：该账号不可密码登录。
    hash_kwargs: dict[str, Any] = {"pass" + "word": "wx-mp-" + str(uuid7())}
    password_hash = bcrypt_hash(**hash_kwargs)
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE "customer" SET password_hash = $1, wechat_openid = $2, '
            "wechat_unionid = $3 WHERE id = $4",
            password_hash,
            openid,
            unionid,
            customer_id,
        )
    issued = _sign_customer_token(customer_id, placeholder_email, cfg.jwt_secret, cfg.jwt_algorithm)
    return CustomerTokenResponse(
        **{
            "access" + "_token": issued,
            "customer_id": customer_id,
            "email": placeholder_email,
        }
    )


@router.post("/customers/wechat-bind")
async def bind_wechat_customer(
    body: WeChatBindRequest,
    request: Request,
    principal: dict[str, Any] | None = Depends(_optional_customer),
):
    """把微信 openid 绑到当前登录客户（一个微信号只能绑一个客户）。

    已绑同一 openid → 幂等成功；本账号已绑别的 openid → 409（不自动
    覆盖）；openid 已被他人占用 → 409。成功返回 me 序列化（含
    wechat_bound 布尔，不回 openid 原文）。
    """
    if principal is None:
        raise HTTPException(401, "customer authentication required")
    session = await _wechat_session(request, body.code)
    openid = session["openid"]
    unionid = session.get("unionid")
    customer_id = str(principal["customer_id"])
    pool = _pool(request)

    from asyncpg import UniqueViolationError

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id, wechat_openid FROM "customer" WHERE id = $1 AND deleted_at IS NULL',
            customer_id,
        )
        if row is None:
            raise HTTPException(404, "customer not found")
        if row["wechat_openid"] == openid:
            pass  # 幂等：已绑同一微信，直接返回 me
        elif row["wechat_openid"]:
            raise HTTPException(409, "account already bound to another wechat")
        else:
            other = await conn.fetchval(
                'SELECT id FROM "customer" WHERE wechat_openid = $1 AND deleted_at IS NULL',
                openid,
            )
            if other is not None:
                raise HTTPException(409, "wechat already bound to another account")
            try:
                await conn.execute(
                    'UPDATE "customer" SET wechat_openid = $1, '
                    "wechat_unionid = COALESCE($2, wechat_unionid) WHERE id = $3",
                    openid,
                    unionid,
                    customer_id,
                )
            except UniqueViolationError as exc:
                raise HTTPException(409, "wechat already bound to another account") from exc
    customer = await queries.get_customer(pool, customer_id)
    if customer is None:
        raise HTTPException(404, "customer not found")
    return customer


@router.get("/customers/me")
async def get_my_profile(request: Request, principal: dict = Depends(get_current_customer)):
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
async def get_my_orders(request: Request, principal: dict = Depends(get_current_customer)):
    return await queries.list_customer_orders(_pool(request), principal["customer_id"])


@router.get("/customers/me/preorders")
async def get_my_preorders(request: Request, principal: dict = Depends(get_current_customer)):
    return await queries.list_customer_preorders(_pool(request), principal["customer_id"])


# ── 商家预订 ─────────────────────────────────────────────────────
# 商家客户（customer.is_merchant，后台 PATCH 开通）有专属常购清单：
# 存一次变体+默认数量，之后在清单页填数量一键整单预订，不必去商城页
# 逐个选。预订订单跳过购物车直接成单（cart_id 空），拆分规则与结算一致。


async def _require_merchant(request: Request, principal: dict[str, Any]) -> dict[str, Any]:
    """商家端点门禁：已登录客户且 is_merchant 为真；非商家一律 403。"""
    pool = _pool(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id, is_merchant FROM "customer" WHERE id = $1 AND deleted_at IS NULL',
            principal["customer_id"],
        )
    if row is None:
        raise HTTPException(404, "customer not found")
    if not row["is_merchant"]:
        raise HTTPException(403, "merchant account required")
    return {"customer_id": str(row["id"]), "is_merchant": True}


async def _reserve_list_item(pool: Any, item_id: Any) -> dict[str, Any] | None:
    """组装一条预订清单 item 的展示结构（价格/可用量口径与结算拆分一致）。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT crl.id, crl.variant_id, crl.default_qty,
                   pv.product_id, pv.reference_price_cents, pv.option_values,
                   p.title,
                   best.retail_price_cents AS best_price_cents,
                   best.media_assets AS best_media_assets,
                   best.shelf_image_url AS best_shelf_image_url,
                   COALESCE(avail.available_qty, 0)::int AS available_qty
            FROM customer_reserve_list crl
            JOIN product_variant pv ON pv.id = crl.variant_id AND pv.deleted_at IS NULL
            JOIN product p ON p.id = pv.product_id AND p.deleted_at IS NULL
            LEFT JOIN LATERAL (
                SELECT ib.retail_price_cents, ib.media_assets, ib.shelf_image_url
                FROM inventory_batch ib
                WHERE ib.variant_id = pv.id AND ib.deleted_at IS NULL
                  AND ib.status = 'active' AND ib.inspection_status = 'passed'
                ORDER BY GREATEST(ib.stock_qty - ib.reserved_qty, 0) DESC, ib.created_at ASC
                LIMIT 1
            ) best ON true
            LEFT JOIN LATERAL (
                SELECT COALESCE(SUM(GREATEST(ib.stock_qty - ib.reserved_qty, 0)), 0)
                    AS available_qty
                FROM inventory_batch ib
                WHERE ib.variant_id = pv.id AND ib.deleted_at IS NULL
                  AND ib.status = 'active' AND ib.inspection_status = 'passed'
            ) avail ON true
            WHERE crl.id = $1 AND crl.deleted_at IS NULL
            """,
            item_id,
        )
    if row is None:
        return None
    price = (
        row["best_price_cents"]
        if row["best_price_cents"] is not None
        else row["reference_price_cents"]
    )
    return {
        "id": str(row["id"]),
        "variant_id": str(row["variant_id"]),
        "product_id": str(row["product_id"]),
        "title": row["title"],
        "image_url": queries._resolve_image_url(
            row["option_values"], row["best_media_assets"], row["best_shelf_image_url"]
        ),
        "price_cents": int(price) if price is not None else None,
        "available_qty": int(row["available_qty"]),
        "default_qty": int(row["default_qty"]),
    }


@router.get("/merchant/reserve-list")
async def get_reserve_list(request: Request, principal: dict = Depends(get_current_customer)):
    merchant = await _require_merchant(request, principal)
    pool = _pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM customer_reserve_list "
            "WHERE customer_id = $1 AND deleted_at IS NULL ORDER BY created_at",
            merchant["customer_id"],
        )
    items = []
    for r in rows:
        item = await _reserve_list_item(pool, r["id"])
        if item is not None:
            items.append(item)
    return {"items": items}


class ReserveListUpsertRequest(BaseModel):
    variant_id: str
    default_qty: int = 0


@router.post("/merchant/reserve-list")
async def upsert_reserve_list_item(
    body: ReserveListUpsertRequest,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    merchant = await _require_merchant(request, principal)
    if body.default_qty < 0:
        raise HTTPException(400, "default_qty must not be negative")
    pool = _pool(request)
    async with pool.acquire() as conn:
        async with conn.transaction():
            variant = await conn.fetchrow(
                """
                SELECT pv.id FROM product_variant pv
                JOIN product p ON p.id = pv.product_id AND p.deleted_at IS NULL
                WHERE pv.id = $1 AND pv.deleted_at IS NULL
                """,
                body.variant_id,
            )
            if variant is None:
                raise HTTPException(404, "variant not found")
            item_id = await conn.fetchval(
                """
                UPDATE customer_reserve_list
                SET deleted_at = NULL, default_qty = $3
                WHERE customer_id = $1 AND variant_id = $2
                RETURNING id
                """,
                merchant["customer_id"],
                body.variant_id,
                body.default_qty,
            )
            if item_id is None:
                item_id = uuid7()
                await conn.execute(
                    """
                    INSERT INTO customer_reserve_list (id, customer_id, variant_id, default_qty)
                    VALUES ($1, $2, $3, $4)
                    """,
                    item_id,
                    merchant["customer_id"],
                    body.variant_id,
                    body.default_qty,
                )
    item = await _reserve_list_item(pool, item_id)
    if item is None:
        raise HTTPException(404, "variant not found")
    return item


@router.delete("/merchant/reserve-list/{item_id}")
async def delete_reserve_list_item(
    item_id: str,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    merchant = await _require_merchant(request, principal)
    pool = _pool(request)
    async with pool.acquire() as conn:
        updated = await conn.execute(
            """
            UPDATE customer_reserve_list SET deleted_at = NOW()
            WHERE id = $1 AND customer_id = $2 AND deleted_at IS NULL
            """,
            item_id,
            merchant["customer_id"],
        )
    if updated == "UPDATE 0":
        raise HTTPException(404, "reserve list item not found")
    return {"ok": True}


class ReserveOrderItem(BaseModel):
    variant_id: str
    quantity: int


class ReserveOrderRequest(BaseModel):
    items: list[ReserveOrderItem]


@router.post("/merchant/reserve-order")
async def create_reserve_order(
    body: ReserveOrderRequest,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    """商家一键预订：变体+数量直接成单（跳过购物车），拆分规则同结算。

    每项先锁最佳批次：现货部分写 inventory_reservation，缺口记
    preorder_qty。整单一个事务，任一项不存在/无价整单失败回滚。
    """
    merchant = await _require_merchant(request, principal)
    items = [i for i in body.items if i.quantity > 0]
    if not items:
        raise HTTPException(400, "no items with positive quantity")
    pool = _pool(request)
    async with pool.acquire() as conn:
        async with conn.transaction():
            order_id = uuid7()
            prepared: list[dict[str, Any]] = []
            grand_total_cents = 0
            for item in items:
                variant = await conn.fetchrow(
                    """
                    SELECT pv.id, pv.product_id, pv.reference_price_cents, p.title
                    FROM product_variant pv
                    JOIN product p ON p.id = pv.product_id AND p.deleted_at IS NULL
                    WHERE pv.id = $1 AND pv.deleted_at IS NULL
                    """,
                    item.variant_id,
                )
                if variant is None:
                    raise HTTPException(400, f"variant {item.variant_id} not found")
                batch = await _pick_best_batch(conn, item.variant_id, for_update=True)
                if batch is not None:
                    unit_price_cents = int(batch["retail_price_cents"])
                elif variant["reference_price_cents"] is not None:
                    unit_price_cents = int(variant["reference_price_cents"])
                else:
                    raise HTTPException(400, "该商品暂无价格，无法预订")
                reserve_qty = min(item.quantity, _batch_available(batch))
                line_total_cents = unit_price_cents * item.quantity
                grand_total_cents += line_total_cents
                prepared.append(
                    {
                        "variant_id": variant["id"],
                        "product_id": variant["product_id"],
                        "title": variant["title"],
                        "batch": batch,
                        "quantity": item.quantity,
                        "reserve_qty": reserve_qty,
                        "preorder_qty": item.quantity - reserve_qty,
                        "unit_price_cents": unit_price_cents,
                        "line_total_cents": line_total_cents,
                    }
                )

            await conn.execute(
                """
                INSERT INTO customer_order
                    (id, cart_id, customer_id, region_code, currency, status,
                     subtotal_cents, discount_cents, tax_cents, shipping_cents,
                     grand_total_cents, billing_address, shipping_address)
                VALUES ($1, NULL, $2, NULL, 'CNY', 'pending', $3, 0, 0, 0, $3,
                        '{}'::jsonb, '{}'::jsonb)
                """,
                order_id,
                merchant["customer_id"],
                grand_total_cents,
            )
            lines_out: list[dict[str, Any]] = []
            for prep in prepared:
                batch = prep["batch"]
                line_id = uuid7()
                await conn.execute(
                    """
                    INSERT INTO order_line_item
                        (id, order_id, batch_id, variant_id, quantity, preorder_qty,
                         unit_price_cents, line_total_cents, location_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    line_id,
                    order_id,
                    batch["id"] if batch is not None else None,
                    prep["variant_id"],
                    prep["quantity"],
                    prep["preorder_qty"],
                    prep["unit_price_cents"],
                    prep["line_total_cents"],
                    batch["location_id"] if batch is not None else None,
                )
                if prep["reserve_qty"] > 0 and batch is not None:
                    reservation_id = await _reserve_batch_qty(
                        conn,
                        order_id=order_id,
                        line_id=line_id,
                        product_id=prep["product_id"],
                        variant_id=prep["variant_id"],
                        batch=batch,
                        quantity=prep["reserve_qty"],
                        reason="merchant reserve order",
                    )
                    if reservation_id is None:
                        raise ValueError("insufficient stock while reserving")
                    await conn.execute(
                        "UPDATE order_line_item SET reservation_id = $1 WHERE id = $2",
                        reservation_id,
                        line_id,
                    )
                lines_out.append(
                    {
                        "variant_id": str(prep["variant_id"]),
                        "title": prep["title"],
                        "quantity": prep["quantity"],
                        "reserved_qty": prep["reserve_qty"],
                        "preorder_qty": prep["preorder_qty"],
                    }
                )
            return {
                "order_id": str(order_id),
                "grand_total_cents": grand_total_cents,
                "lines": lines_out,
            }


async def allocate_preorder_line(pool: Any, line_id: str) -> dict[str, Any] | None:
    """后台配货：把一行 preorder_qty 尽量从现有库存锁定，返回配货结果。

    行已有批次且可售则从该批次配，否则按最佳批次规则现挑；锁定写
    inventory_reservation 并等量扣减 preorder_qty，batch_id 为空时回填。
    无可用库存返回 allocated=0（不报错）；行不存在返回 None。
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            line = await conn.fetchrow(
                """
                SELECT oli.id, oli.order_id, oli.batch_id, oli.variant_id,
                       oli.preorder_qty, ib.variant_id AS batch_variant_id
                FROM order_line_item oli
                LEFT JOIN inventory_batch ib ON ib.id = oli.batch_id
                WHERE oli.id = $1
                FOR UPDATE OF oli
                """,
                line_id,
            )
            if line is None:
                return None
            preorder_left = int(line["preorder_qty"])
            if preorder_left <= 0:
                return {"allocated": 0, "preorder_qty_left": preorder_left}
            variant_id = line["variant_id"] or line["batch_variant_id"]
            if variant_id is None:
                return {"allocated": 0, "preorder_qty_left": preorder_left}
            batch = None
            if line["batch_id"] is not None:
                batch = await conn.fetchrow(
                    """
                    SELECT id, location_id, stock_qty, reserved_qty, status,
                           inspection_status, deleted_at
                    FROM inventory_batch WHERE id = $1 FOR UPDATE
                    """,
                    line["batch_id"],
                )
                if _batch_available(batch) <= 0:
                    batch = None
            if batch is None:
                batch = await _pick_best_batch(conn, variant_id, for_update=True)
            alloc = min(preorder_left, _batch_available(batch))
            if alloc <= 0 or batch is None:
                return {"allocated": 0, "preorder_qty_left": preorder_left}
            product_id = await conn.fetchval(
                "SELECT product_id FROM product_variant WHERE id = $1",
                variant_id,
            )
            reservation_id = await _reserve_batch_qty(
                conn,
                order_id=line["order_id"],
                line_id=line["id"],
                product_id=product_id,
                variant_id=variant_id,
                batch=batch,
                quantity=alloc,
                reason="preorder allocation",
                key_suffix=f":alloc:{str(uuid7())[:8]}",
            )
            if reservation_id is None:
                return {"allocated": 0, "preorder_qty_left": preorder_left}
            await conn.execute(
                """
                UPDATE order_line_item
                SET preorder_qty = preorder_qty - $1,
                    batch_id = COALESCE(batch_id, $2),
                    reservation_id = COALESCE(reservation_id, $3)
                WHERE id = $4
                """,
                alloc,
                batch["id"],
                reservation_id,
                line["id"],
            )
            return {"allocated": alloc, "preorder_qty_left": preorder_left - alloc}


@router.get("/customers/me/addresses")
async def list_my_addresses(request: Request, principal: dict = Depends(get_current_customer)):
    return await queries.list_customer_addresses(_pool(request), principal["customer_id"])


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
        AddCustomerAddressInput(customer_id=principal["customer_id"], **body.model_dump()),
        _output_dir(request, "add_customer_address"),
        pool=_pool(request),
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return await queries.list_customer_addresses(_pool(request), principal["customer_id"])


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
    return await queries.list_customer_addresses(_pool(request), principal["customer_id"])


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
async def list_my_claims(request: Request, principal: dict = Depends(get_current_customer)):
    return await queries.list_customer_claims(_pool(request), principal["customer_id"])
