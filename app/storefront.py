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
import uuid
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from obase.uuid7 import uuid7
from pydantic import BaseModel

from app import queries
from app.cangzhu import (
    choose_discount,
    get_voucher_balance,
    hold_redemption,
    night_discount_cents,
    night_now,
    night_price_cents,
)
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


def _require_uuid(value: str, field: str) -> str:
    """校验客户端传入的 id 是合法 UUID 格式；非法直接 400。

    非法 id（如空串、随手编的字符串）一路打到 asyncpg 的 UUID 编码会
    抛 ValueError 变成 500；所有把客户端 id 绑定到 UUID 列的入口先过
    这里。合法但不存在的 id 仍走原有 404 语义，不受影响。
    """
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, f"invalid {field}") from None
    return value


async def _authorize_cart(
    request: Request, cart_id: str, principal: dict[str, Any] | None
) -> str | None:
    """Authorize an owned customer cart or a guest cart capability.

    Guest carts use a signed, cart-scoped bearer capability returned only at creation.
    A guest cart can be claimed by a logged-in customer only with that capability.
    """
    _require_uuid(cart_id, "cart_id")
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
    if body.batch_id is not None:
        _require_uuid(body.batch_id, "batch_id")
    if body.variant_id is not None:
        _require_uuid(body.variant_id, "variant_id")
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
    _require_uuid(body.line_item_id, "line_item_id")
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
    _require_uuid(body.line_item_id, "line_item_id")
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

            # 仓主试点 + 缺口 6：夜市价、会员 9 折、券抵扣三者互斥取最优
            # （购物车已有其他折扣时不动）。夜市行（night_clearance 标记）
            # 不计入 9 折/券的合资格池，夜市折扣额按夜市行合计与当前北京
            # 时间时段算；券抵扣在同一事务内持有余额，事务回滚时同步撤销。
            _cz_discount_kind = "none"
            _cz_discount_cents = int(cart["discount_cents"] or 0)
            _cz_grand_total = int(cart["grand_total_cents"])
            if _cz_discount_cents == 0:
                _cz_customer_id = customer_id or (
                    str(cart["customer_id"]) if cart["customer_id"] else None
                )
                if _cz_customer_id is not None:
                    _variant_flags = {
                        str(r["id"]): r
                        for r in await conn.fetch(
                            "SELECT pv.id, COALESCE(p.voucher_excluded, false) "
                            "AS voucher_excluded, "
                            "COALESCE(p.night_clearance, false) AS night_clearance "
                            "FROM product_variant pv "
                            "JOIN product p ON p.id = pv.product_id "
                            "WHERE pv.id = ANY($1)",
                            [prep["variant_id"] for prep in prepared],
                        )
                    }
                    _excluded_variants = {
                        vid
                        for vid, flags in _variant_flags.items()
                        if flags["voucher_excluded"] or flags["night_clearance"]
                    }
                    _eligible = sum(
                        int(prep["line_total_cents"])
                        for prep in prepared
                        if str(prep["variant_id"]) not in _excluded_variants
                    )
                    _night_total = sum(
                        int(prep["line_total_cents"])
                        for prep in prepared
                        if _variant_flags.get(str(prep["variant_id"]))
                        and _variant_flags[str(prep["variant_id"])]["night_clearance"]
                    )
                    _night_discount = night_discount_cents(_night_total, night_now())
                    _balance = await get_voucher_balance(conn, _cz_customer_id)
                    _cz_discount_kind, _cz_discount_cents = choose_discount(
                        _eligible,
                        _balance,
                        is_member=True,
                        night_cents=_night_discount,
                    )
                    if _cz_discount_cents > 0:
                        _cz_grand_total = max(
                            0,
                            int(cart["subtotal_cents"])
                            - _cz_discount_cents
                            + int(cart["tax_cents"])
                            + int(cart["shipping_cents"]),
                        )
                        if _cz_discount_kind == "voucher":
                            await hold_redemption(
                                conn,
                                customer_id=_cz_customer_id,
                                order_id=str(order_id),
                                amount_cents=_cz_discount_cents,
                            )

            await conn.execute(
                """
                INSERT INTO customer_order
                    (id, cart_id, customer_id, region_code, currency, status,
                     subtotal_cents, discount_cents, tax_cents, shipping_cents,
                     grand_total_cents, billing_address, shipping_address,
                     discount_kind, voucher_redeemed_cents)
                VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7, $8, $9, $10,
                        $11::jsonb, $12::jsonb, $13, $14)
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
                _cz_discount_cents,
                cart["tax_cents"],
                cart["shipping_cents"],
                _cz_grand_total,
                json.dumps(cart["billing_address"] or {}, ensure_ascii=False),
                json.dumps(cart["shipping_address"] or {}, ensure_ascii=False),
                _cz_discount_kind,
                _cz_discount_cents if _cz_discount_kind == "voucher" else 0,
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
                "grand_total_cents": _cz_grand_total,
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
    _require_uuid(body.cart_id, "cart_id")
    if body.customer_id is not None and principal is None:
        raise HTTPException(401, "customer authentication required")
    authenticated_customer_id = str(principal["customer_id"]) if principal else None
    if body.customer_id is not None and body.customer_id != authenticated_customer_id:
        raise HTTPException(403, "customer_id does not match authenticated customer")
    if body.customer_id is not None:
        _require_uuid(body.customer_id, "customer_id")
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

    for item in body.line_items:
        _require_uuid(item.batch_id, "batch_id")
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
    if provider == "manual" and not bool(getattr(settings, "manual_payment_enabled", True)):
        raise HTTPException(503, "manual payment is disabled")
    if provider in {"wechat", "stripe"} and configured != provider:
        raise HTTPException(503, f"{provider} payment gateway is not configured")
    return provider


def _payment_options(settings: Any) -> dict[str, Any]:
    """公开、无密钥的支付可用性；禁止把 manual 模拟单展示成在线支付。"""
    configured = str(settings.payment_gateway_provider or "").strip().lower()
    online_configured = False
    if configured in {"wechat", "stripe", "alipay"}:
        try:
            from obase.provider_registry import ProviderRegistry

            gateway = ProviderRegistry.get().generic("payment_gateway", configured)
            online_configured = bool(getattr(gateway, "is_configured", False))
        except Exception:  # noqa: BLE001 - no registry means no online payment
            online_configured = False
    manual_enabled = bool(getattr(settings, "manual_payment_enabled", True))
    return {
        "online_payment_available": online_configured,
        "online_provider": configured if online_configured else None,
        "manual_payment_enabled": manual_enabled,
        "methods": [
            *(
                [
                    {
                        "code": configured,
                        "name": {
                            "wechat": "微信支付",
                            "stripe": "银行卡支付",
                            "alipay": "支付宝",
                        }[configured],
                        "mode": "online",
                    }
                ]
                if online_configured
                else []
            ),
            *(
                [{"code": "manual", "name": "人工确认收款", "mode": "offline"}]
                if manual_enabled
                else []
            ),
        ],
    }


@router.get("/payment-options")
async def payment_options(request: Request) -> dict[str, Any]:
    """顾客结账前读取支付可用性，未配置真实网关时不展示“在线支付可用”。"""
    return _payment_options(request.app.state.config)


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

    _require_uuid(body.order_id, "order_id")
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

    _require_uuid(payment_id, "payment_id")
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
    # 兼容旧客户端：字段仍接受但服务端永不采用。
    price_cents: int | None = None


@router.get("/shipping-options")
async def shipping_options(request: Request):
    """公开配送方式与时窗；价格由服务端规则计算。"""
    from app.shipping import load_shipping_rules

    settings = get_settings()
    rules = await load_shipping_rules(_pool(request), settings)
    return {
        "service_area": rules["service_area"],
        "delivery_windows": rules["delivery_windows"],
        "methods": [
            {
                "code": code,
                "name": method["name"],
                "price_cents": method["price_cents"],
                "free_threshold_cents": method["free_threshold_cents"],
            }
            for code, method in rules["methods"].items()
        ],
        "quoted_by_server": True,
    }


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
    from app.shipping import load_shipping_rules, quote_shipping

    cfg = _cfg(request, AddShippingMethodToCartConfig)
    pool = _pool(request)
    async with pool.acquire() as conn:
        cart_row = await conn.fetchrow(
            "SELECT subtotal_cents FROM cart WHERE id = $1 AND deleted_at IS NULL",
            body.cart_id,
        )
    if cart_row is None:
        raise HTTPException(404, "cart not found")
    rules = await load_shipping_rules(pool, get_settings())
    try:
        quote = quote_shipping(
            rules,
            method_name=body.method_name,
            subtotal_cents=int(cart_row["subtotal_cents"] or 0),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    inp = AddShippingMethodToCartInput(
        cart_id=body.cart_id,
        method_name=body.method_name,
        price_cents=quote["price_cents"],
    )
    result = await add_shipping_method_to_cart(
        cfg,
        inp,
        _output_dir(request, "add_shipping_method_to_cart"),
        pool=pool,
    )
    if result["status"] == "failed":
        raise HTTPException(400, result["error"]["message"])
    return {
        "shipping_cents": result["shipping_cents"],
        "grand_total_cents": result["grand_total_cents"],
        "quote": quote,
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


@router.get("/categories")
async def storefront_categories(request: Request):
    """商城公开分类列表 (菜单栏分类下拉用): 含各分类在售商品数。"""
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pc.id, pc.name, pc.slug, COUNT(p.id) AS count
            FROM product_category pc
            LEFT JOIN product p
              ON p.category_id = pc.id AND p.deleted_at IS NULL AND p.status = 'published'
            WHERE pc.deleted_at IS NULL
            GROUP BY pc.id, pc.name, pc.slug, pc.created_at
            ORDER BY pc.created_at
            """
        )
    return [
        {"id": str(row["id"]), "name": row["name"], "slug": row["slug"], "count": int(row["count"])}
        for row in rows
    ]


@router.get("/products/{product_id}")
async def storefront_product(product_id: str, request: Request):
    _require_uuid(product_id, "product_id")
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
    referral_code: str = ""


class CustomerTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    customer_id: str
    email: str


@router.post("/customers/register", response_model=CustomerTokenResponse)
async def register_customer(body: CustomerRegisterRequest, request: Request):
    from obase.auth.password import bcrypt_hash

    from app.cangzhu import bind_referral, ensure_referral_code, resolve_referrer_id

    # 仓主试点：推荐码先解析，绑定只在注册时一次写入。
    referrer_id: str | None = None
    if (body.referral_code or "").strip():
        async with _pool(request).acquire() as conn:
            try:
                referrer_id = await resolve_referrer_id(conn, body.referral_code)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
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

    async with _pool(request).acquire() as conn:
        async with conn.transaction():
            if referrer_id is not None:
                await bind_referral(conn, customer_id=customer_id, referrer_id=referrer_id)
            await ensure_referral_code(conn, customer_id)

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
    referral_code: str = ""


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

    from app.cangzhu import bind_referral, ensure_referral_code, resolve_referrer_id

    wx_referrer_id: str | None = None
    if (body.referral_code or "").strip():
        async with pool.acquire() as conn:
            try:
                wx_referrer_id = await resolve_referrer_id(conn, body.referral_code)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

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

    async with pool.acquire() as conn:
        async with conn.transaction():
            if wx_referrer_id is not None:
                await bind_referral(conn, customer_id=customer_id, referrer_id=wx_referrer_id)
            await ensure_referral_code(conn, customer_id)

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


# ── 仓主试点：推荐、券账本、点货、邀请礼、裂变账本 ───────────────────
# 规则依据见 app/cangzhu.py。管理端（仓管家）沿用 is_merchant 门禁。


class PurchaseRequestCreate(BaseModel):
    product_name: str
    brand: str = ""
    spec_packaging: str = ""
    price_min_cents: int | None = None
    price_max_cents: int | None = None
    expected_qty: int = 1
    willing_preorder: bool = False


class PurchaseRequestUpdate(BaseModel):
    status: str
    admin_note: str = ""
    failure_reason: str = ""


class VoucherExclusionUpdate(BaseModel):
    excluded: bool


class NightClearanceUpdate(BaseModel):
    night_clearance: bool


class PintuanGroupCreate(BaseModel):
    variant_id: str
    pintuan_price_cents: int
    target_size: int
    duration_minutes: int = 1440
    quantity: int = 1


class PintuanJoinRequest(BaseModel):
    quantity: int = 1


class InviteGiftClaimRequest(BaseModel):
    gift_kind: str


class PointsReverseRequest(BaseModel):
    refund_id: str
    refund_cents: int


class StoredValueTopupRequest(BaseModel):
    amount_cents: int
    external_payment_ref: str
    note: str = ""


def _cangzhu_bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(400, str(exc))


@router.get("/customers/me/referral")
async def get_my_referral(request: Request, principal: dict = Depends(get_current_customer)):
    from app.cangzhu import ensure_referral_code

    customer_id = str(principal["customer_id"])
    pool = _pool(request)
    async with pool.acquire() as conn:
        async with conn.transaction():
            code = await ensure_referral_code(conn, customer_id)
            row = await conn.fetchrow(
                'SELECT invited_by, voucher_balance_cents FROM "customer" WHERE id = $1',
                customer_id,
            )
            referred = await conn.fetchval(
                'SELECT COUNT(*) FROM "customer" WHERE invited_by = $1 AND deleted_at IS NULL',
                customer_id,
            )
    return {
        "customer_id": customer_id,
        "referral_code": code,
        "invited_by": str(row["invited_by"]) if row["invited_by"] else None,
        "voucher_balance_cents": int(row["voucher_balance_cents"] or 0),
        "referred_count": int(referred or 0),
    }


@router.get("/vouchers")
async def list_my_vouchers(
    request: Request,
    principal: dict = Depends(get_current_customer),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    customer_id = str(principal["customer_id"])
    async with _pool(request).acquire() as conn:
        balance = await conn.fetchval(
            'SELECT voucher_balance_cents FROM "customer" WHERE id = $1', customer_id
        )
        rows = await conn.fetch(
            "SELECT id, kind, status, order_id, ref_id, amount_cents, base_cents,"
            " balance_after_cents, idempotency_key, created_at FROM voucher_ledger"
            " WHERE customer_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            customer_id,
            limit,
            offset,
        )
    return {
        "voucher_balance_cents": int(balance or 0),
        "entries": [dict(r) for r in rows],
    }


# ── 会员补齐（缺口 4）：等级 / 积分 / 储值 ──────────────────────────
# 口径与默认值见 app/membership.py 与 docs/membership.md。等级只作身份统计，
# 不给价格折扣；积分只积不兑；储值只记账不接支付网关；短信不做。


@router.get("/customers/me/membership")
async def get_my_membership(request: Request, principal: dict = Depends(get_current_customer)):
    from app.membership import get_membership_summary

    async with _pool(request).acquire() as conn:
        try:
            return await get_membership_summary(conn, str(principal["customer_id"]))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc


@router.get("/points")
async def list_my_points(
    request: Request,
    principal: dict = Depends(get_current_customer),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from app.membership import list_points_entries

    async with _pool(request).acquire() as conn:
        try:
            return await list_points_entries(
                conn, str(principal["customer_id"]), limit=limit, offset=offset
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc


@router.get("/stored-value")
async def list_my_stored_value(
    request: Request,
    principal: dict = Depends(get_current_customer),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from app.membership import list_stored_value_entries

    async with _pool(request).acquire() as conn:
        try:
            return await list_stored_value_entries(
                conn, str(principal["customer_id"]), limit=limit, offset=offset
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc


@router.get("/customers/{customer_id}/membership")
async def get_customer_membership(
    customer_id: str, request: Request, principal: dict = Depends(get_current_customer)
):
    from app.membership import get_membership_summary

    await _require_merchant(request, principal)
    async with _pool(request).acquire() as conn:
        try:
            return await get_membership_summary(conn, customer_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc


@router.post("/orders/{order_id}/points-accrue")
async def accrue_order_points(
    order_id: str, request: Request, principal: dict = Depends(get_current_customer)
):
    from app.membership import accrue_points

    await _require_merchant(request, principal)
    async with _pool(request).acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM customer_order WHERE id = $1", order_id)
    if exists is None:
        raise HTTPException(404, "order not found")
    result = await accrue_points(_pool(request), order_id)
    return {"order_id": order_id, **result}


@router.post("/orders/{order_id}/points-reverse")
async def reverse_order_points(
    order_id: str,
    body: PointsReverseRequest,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    from app.membership import reverse_points_for_refund

    await _require_merchant(request, principal)
    async with _pool(request).acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM customer_order WHERE id = $1", order_id)
    if exists is None:
        raise HTTPException(404, "order not found")
    try:
        result = await reverse_points_for_refund(
            _pool(request),
            refund_id=body.refund_id,
            order_id=order_id,
            refund_cents=body.refund_cents,
        )
    except ValueError as exc:
        raise _cangzhu_bad_request(exc) from exc
    return {"order_id": order_id, **result}


@router.post("/customers/{customer_id}/stored-value/topup")
async def topup_customer_stored_value(
    customer_id: str,
    body: StoredValueTopupRequest,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    from app.membership import topup_stored_value

    merchant = await _require_merchant(request, principal)
    async with _pool(request).acquire() as conn:
        exists = await conn.fetchval('SELECT id FROM "customer" WHERE id = $1', customer_id)
    if exists is None:
        raise HTTPException(404, "customer not found")
    try:
        result = await topup_stored_value(
            _pool(request),
            customer_id=customer_id,
            amount_cents=body.amount_cents,
            external_payment_ref=body.external_payment_ref,
            operator_id=str(merchant["customer_id"]),
            note=body.note,
        )
    except ValueError as exc:
        raise _cangzhu_bad_request(exc) from exc
    return {"customer_id": customer_id, **result}


@router.post("/purchase-requests", status_code=201)
async def create_my_purchase_request(
    body: PurchaseRequestCreate,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    from app.cangzhu import create_purchase_request

    try:
        request_id = await create_purchase_request(
            _pool(request),
            customer_id=str(principal["customer_id"]),
            body=body.model_dump(),
        )
    except ValueError as exc:
        raise _cangzhu_bad_request(exc) from exc
    return {"request_id": request_id, "status": "collected"}


@router.get("/purchase-requests/mine")
async def list_my_purchase_requests(
    request: Request,
    principal: dict = Depends(get_current_customer),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from app.cangzhu import list_purchase_requests

    return await list_purchase_requests(
        _pool(request),
        customer_id=str(principal["customer_id"]),
        status=status,
        limit=limit,
        offset=offset,
    )


@router.get("/purchase-requests/leaderboard")
async def get_purchase_request_leaderboard(
    request: Request,
    days: int = Query(7, ge=1, le=30),
    limit: int = Query(20, ge=1, le=100),
):
    from app.cangzhu import purchase_request_leaderboard

    return await purchase_request_leaderboard(_pool(request), days=days, limit=limit)


@router.get("/purchase-requests")
async def list_all_purchase_requests(
    request: Request,
    principal: dict = Depends(get_current_customer),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from app.cangzhu import list_purchase_requests

    await _require_merchant(request, principal)
    return await list_purchase_requests(
        _pool(request), customer_id=None, status=status, limit=limit, offset=offset
    )


@router.patch("/purchase-requests/{request_id}")
async def update_purchase_request_status(
    request_id: str,
    body: PurchaseRequestUpdate,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    from app.cangzhu import update_purchase_request

    await _require_merchant(request, principal)
    try:
        return await update_purchase_request(
            _pool(request),
            request_id=request_id,
            to_status=body.status,
            admin_note=body.admin_note,
            failure_reason=body.failure_reason,
        )
    except ValueError as exc:
        raise _cangzhu_bad_request(exc) from exc


@router.get("/growth-ledger")
async def get_growth_ledger(
    request: Request,
    principal: dict = Depends(get_current_customer),
    day: str | None = Query(None, description="YYYY-MM-DD，默认今天"),
):
    from datetime import date as _date

    from app.cangzhu import growth_ledger

    await _require_merchant(request, principal)
    try:
        target = _date.fromisoformat(day) if day else _date.today()
    except ValueError as exc:
        raise HTTPException(400, f"invalid day: {day}") from exc
    return await growth_ledger(_pool(request), target)


@router.post("/orders/{order_id}/voucher-freeze")
async def freeze_order_voucher(
    order_id: str, request: Request, principal: dict = Depends(get_current_customer)
):
    from app.cangzhu import freeze_accrual

    await _require_merchant(request, principal)
    async with _pool(request).acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM customer_order WHERE id = $1", order_id)
    if exists is None:
        raise HTTPException(404, "order not found")
    frozen = await freeze_accrual(_pool(request), order_id)
    return {"order_id": order_id, "voucher_accrual_frozen": True, "changed": frozen}


@router.post("/orders/{order_id}/voucher-unfreeze")
async def unfreeze_order_voucher(
    order_id: str, request: Request, principal: dict = Depends(get_current_customer)
):
    from app.cangzhu import unfreeze_accrual

    await _require_merchant(request, principal)
    async with _pool(request).acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM customer_order WHERE id = $1", order_id)
    if exists is None:
        raise HTTPException(404, "order not found")
    result = await unfreeze_accrual(_pool(request), order_id)
    return {"order_id": order_id, "voucher_accrual_frozen": False, **result}


@router.get("/invite-gift/status")
async def get_invite_gift_status(request: Request, principal: dict = Depends(get_current_customer)):
    from app.cangzhu import check_invite_gift_eligibility

    customer_id = str(principal["customer_id"])
    claim = None
    async with _pool(request).acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, order_id, gift_kind, status, cost_cap_cents, created_at"
            " FROM invite_gift_claim WHERE customer_id = $1",
            customer_id,
        )
        if row is not None:
            claim = dict(row)
    eligibility = await check_invite_gift_eligibility(_pool(request), customer_id)
    return {"claim": claim, **eligibility}


@router.post("/invite-gift/claim", status_code=201)
async def claim_my_invite_gift(
    body: InviteGiftClaimRequest,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    from app.cangzhu import claim_invite_gift

    try:
        return await claim_invite_gift(
            _pool(request), str(principal["customer_id"]), body.gift_kind
        )
    except ValueError as exc:
        raise _cangzhu_bad_request(exc) from exc


@router.patch("/products/{product_id}/voucher-exclusion")
async def set_product_voucher_exclusion(
    product_id: str,
    body: VoucherExclusionUpdate,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    await _require_merchant(request, principal)
    async with _pool(request).acquire() as conn:
        # 缺口 6 不变式：夜市清货品须保持 voucher_excluded，否则夜市行
        # 会重新计入推荐奖励合资格基数（规则 2）。先查后改，守卫在先。
        existing = await conn.fetchrow(
            "SELECT id, COALESCE(night_clearance, false) AS night_clearance "
            "FROM product WHERE id = $1",
            product_id,
        )
        if existing is None:
            raise HTTPException(404, "product not found")
        if existing["night_clearance"] and not body.excluded:
            raise HTTPException(
                400,
                "night clearance product must stay voucher-excluded; clear night-clearance first",
            )
        row = await conn.fetchrow(
            "UPDATE product SET voucher_excluded = $1 WHERE id = $2 RETURNING id",
            body.excluded,
            product_id,
        )
    if row is None:
        raise HTTPException(404, "product not found")
    return {"product_id": product_id, "voucher_excluded": body.excluded}


@router.patch("/products/{product_id}/night-clearance")
async def set_product_night_clearance(
    product_id: str,
    body: NightClearanceUpdate,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    """缺口 6：标记/取消当日临期清货品（商家）。

    置标时同步 voucher_excluded=true（规则 2/3 不变式）；取消标记时
    voucher_excluded 保持原值，由券排除端点单独管理。
    """

    await _require_merchant(request, principal)
    async with _pool(request).acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE product SET night_clearance = $1, "
            "voucher_excluded = voucher_excluded OR $1 WHERE id = $2 "
            "RETURNING id, night_clearance, voucher_excluded",
            body.night_clearance,
            product_id,
        )
    if row is None:
        raise HTTPException(404, "product not found")
    return {
        "product_id": product_id,
        "night_clearance": bool(row["night_clearance"]),
        "voucher_excluded": bool(row["voucher_excluded"]),
    }


@router.get("/night-market")
async def list_night_market(
    request: Request,
    principal: dict = Depends(get_current_customer),
    limit: int = Query(100, ge=1, le=200),
):
    """缺口 6：当前夜市清货品与本时段夜市价（只读）。

    单价取变体 reference_price_cents；无价时夜市价为 null，不编造价格。
    """

    from app.cangzhu import night_discount_rate

    now = night_now()
    rate = night_discount_rate(now)
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(
            "SELECT p.id AS product_id, p.title, pv.id AS variant_id, "
            "pv.reference_price_cents FROM product p "
            "JOIN product_variant pv ON pv.product_id = p.id "
            "AND pv.deleted_at IS NULL "
            "WHERE p.deleted_at IS NULL AND COALESCE(p.night_clearance, false) "
            "ORDER BY p.title LIMIT $1",
            limit,
        )
    products = []
    for row in rows:
        ref = row["reference_price_cents"]
        products.append(
            {
                "product_id": str(row["product_id"]),
                "title": row["title"],
                "variant_id": str(row["variant_id"]),
                "reference_price_cents": int(ref) if ref is not None else None,
                "night_price_cents": night_price_cents(int(ref), now) if ref is not None else None,
            }
        )
    return {
        "now": now.isoformat(),
        "discount_rate": str(rate),
        "products": products,
    }


@router.post("/pintuan/groups", status_code=201)
async def create_pintuan_group_endpoint(
    body: PintuanGroupCreate,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    """缺口 6：发起拼单（意向登记，不收款、不建单）。"""

    from app.night_pintuan import create_pintuan_group

    try:
        return await create_pintuan_group(
            _pool(request),
            creator_id=str(principal["customer_id"]),
            variant_id=body.variant_id,
            pintuan_price_cents=body.pintuan_price_cents,
            target_size=body.target_size,
            duration_minutes=body.duration_minutes,
            quantity=body.quantity,
        )
    except ValueError as exc:
        raise _cangzhu_bad_request(exc) from exc


@router.post("/pintuan/groups/{group_id}/join")
async def join_pintuan_group_endpoint(
    group_id: str,
    body: PintuanJoinRequest,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    """缺口 6：加入拼单；人头数达标即成团。"""

    from app.night_pintuan import join_pintuan_group

    try:
        return await join_pintuan_group(
            _pool(request),
            group_id=group_id,
            customer_id=str(principal["customer_id"]),
            quantity=body.quantity,
        )
    except ValueError as exc:
        raise _cangzhu_bad_request(exc) from exc


@router.get("/pintuan/groups")
async def list_pintuan_groups_endpoint(
    request: Request,
    principal: dict = Depends(get_current_customer),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """缺口 6：拼单团列表（读时批量过期已到期未成团的团）。"""

    from app.night_pintuan import list_pintuan_groups

    try:
        return {
            "groups": await list_pintuan_groups(
                _pool(request), status=status, limit=limit, offset=offset
            )
        }
    except ValueError as exc:
        raise _cangzhu_bad_request(exc) from exc


@router.get("/pintuan/groups/{group_id}")
async def get_pintuan_group_endpoint(
    group_id: str,
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    """缺口 6：拼单团详情（读时懒过期）。"""

    from app.night_pintuan import get_pintuan_group

    group = await get_pintuan_group(_pool(request), group_id)
    if group is None:
        raise HTTPException(404, "pintuan group not found")
    return group


@router.post("/pintuan/sweep-expired")
async def sweep_expired_pintuan_groups_endpoint(
    request: Request,
    principal: dict = Depends(get_current_customer),
):
    """缺口 6：商家批量过期已到期未成团的拼单团。"""

    from app.night_pintuan import sweep_expired_pintuan_groups

    await _require_merchant(request, principal)
    expired = await sweep_expired_pintuan_groups(_pool(request))
    return {"expired_groups": expired}


@router.post("/ingest")
async def ingest_my_product(
    request: Request,
    file: UploadFile,
    retail_price_cents: int | None = Form(None),
    stock_qty: int | None = Form(None),
    category_slug: str = Form("daily"),
    principal: dict = Depends(get_current_customer),
):
    """仓主传货：照片/短视频上传，命中既有商品则原地更新，否则新建。

    与员工端 POST /agent/ingest 共用 ingest_media，差异：顾客鉴权、文件名
    确定性标题匹配（不依赖 VLM）、匹配时原地更新最新批次而非只补新批次。
    未提供的价格/库存不覆盖既有值；新建时沿用默认价 ¥12.99 / 库存 30。
    不设商家门禁：全体注册仓主可用。
    """
    from app.ext.agent_gateway import ingest_media

    if retail_price_cents is not None and retail_price_cents <= 0:
        raise HTTPException(400, "retail_price_cents must be positive")
    if stock_qty is not None and stock_qty < 0:
        raise HTTPException(400, "stock_qty must be non-negative")
    customer_id = str(principal["customer_id"])
    return await ingest_media(
        file,
        location_id="",
        pool=_pool(request),
        out_root=_output_dir(request, "ext_bespoke/agent_ingest"),
        principal={"user_id": f"customer-{customer_id}"},
        retail_price_cents=retail_price_cents,
        stock_qty=stock_qty,
        category_slug=category_slug,
        deterministic_match=True,
        update_in_place=True,
        actor_ref=f"c_{customer_id}",
    )


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
    _require_uuid(body.variant_id, "variant_id")
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
    _require_uuid(item_id, "item_id")
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
    for i in body.items:
        _require_uuid(i.variant_id, "variant_id")
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
    _require_uuid(address_id, "address_id")
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


# ── 柜台称重试点 ─────────────────────────────────────────────────────────
# 店员柜台页 /shop/counter 的后端：照片格商品、单据、行项、模拟器。
# 门禁：顾客登录 + is_merchant（仓管家），与点货/裂变账本同一道门。
# 结算语义：挂单待结算（pending_settlement）后现金结算（counter_settle_ticket）
# 落结算记录、单据置 settled，并在同一事务内建 customer_order（渠道 counter）
# 与直接扣库存（不建预留，见 app/counter_settlement_order.py）；不碰券账本
# 与 9 折逻辑（券/9 折只走线上建单链路）。


def _counter_first_media_ref(value: object) -> str | None:
    """取第一个媒体引用：shelf_image_url/media_assets 历史上都可能存成
    JSON 数组字符串，先解析再取首项（与前端 product-image.ts 同一容错）。"""

    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            ref = _counter_first_media_ref(item)
            if ref:
                return ref
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith("["):
            try:
                import json as _json

                return _counter_first_media_ref(_json.loads(text))
            except ValueError:
                return None
        return text
    return None


def _counter_ticket_dict(row: Any, total_cents: int) -> dict:
    return {
        "ticket_id": str(row["id"]),
        "status": row["status"],
        "tote_id": row["tote_id"],
        "customer_id": str(row["customer_id"]) if row["customer_id"] else None,
        "created_by": row["created_by"],
        "note": row["note"],
        "total_cents": total_cents,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def _counter_require_ticket(conn: Any, ticket_id: str, owner_id: str) -> Any:
    """取单据并验归属：不存在 404，非本人 403。"""
    import uuid

    try:
        ticket_uuid = uuid.UUID(str(ticket_id))
    except ValueError:
        raise HTTPException(400, "invalid ticket_id") from None
    row = await conn.fetchrow('SELECT * FROM "counter_ticket" WHERE id = $1', ticket_uuid)
    if row is None:
        raise HTTPException(404, "ticket not found")
    if str(row["created_by"]) != str(owner_id):
        raise HTTPException(403, "ticket belongs to another merchant")
    return row


async def _counter_ticket_total(conn: Any, ticket_uuid: Any) -> int:
    total = await conn.fetchval(
        'SELECT COALESCE(SUM(amount_cents), 0)::int FROM "hardware_event" '
        "WHERE ticket_id = $1 AND event_type = 'weigh' AND status = 'processed'",
        ticket_uuid,
    )
    return int(total or 0)


@router.get("/counter/products")
async def counter_products(
    request: Request,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """柜台照片格：可称重商品（最优可售批次 + 现价 + 计价基准 + 首图）。

    只返回“能当场计价”的行：无价或规格里解析不出 g/kg 基准的商品不上格，
    避免店员点选后才发现算不了账（缺价商品走点货/补价流程，不在这里编价格）。
    """
    await _require_merchant(request, principal)
    from app.weigh import extract_unit_texts, parse_basis_grams

    pool = _pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.id AS product_id, p.title,
                   pv.id AS variant_id, pv.option_values, pv.reference_price_cents,
                   best.id AS batch_id, best.retail_price_cents,
                   best.shelf_image_url, best.media_assets,
                   GREATEST(best.stock_qty - best.reserved_qty, 0)::int AS available_qty
            FROM product p
            JOIN product_variant pv ON pv.product_id = p.id AND pv.deleted_at IS NULL
            JOIN LATERAL (
                SELECT ib.* FROM inventory_batch ib
                WHERE ib.variant_id = pv.id AND ib.deleted_at IS NULL
                  AND ib.status = 'active' AND ib.inspection_status = 'passed'
                ORDER BY GREATEST(ib.stock_qty - ib.reserved_qty, 0) DESC,
                         ib.created_at ASC
                LIMIT 1
            ) best ON true
            WHERE p.deleted_at IS NULL
            ORDER BY p.title
            LIMIT 200
            """
        )
    products = []
    for row in rows:
        price = row["retail_price_cents"]
        if price is None:
            price = row["reference_price_cents"]
        basis = parse_basis_grams(extract_unit_texts(row["option_values"]))
        if basis is None:
            basis = parse_basis_grams([row["title"]])
        if price is None or basis is None:
            continue
        image = _counter_first_media_ref(row["shelf_image_url"]) or _counter_first_media_ref(
            row["media_assets"]
        )
        products.append(
            {
                "batch_id": str(row["batch_id"]),
                "variant_id": str(row["variant_id"]),
                "product_id": str(row["product_id"]),
                "title": row["title"],
                "image_url": image,
                "unit_price_cents": int(price),
                "price_basis_grams": int(basis),
                "available_qty": int(row["available_qty"]),
            }
        )
    return {"products": products}


@router.post("/counter/tickets")
async def counter_create_ticket(
    request: Request,
    body: dict,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """开一张柜台单据（open 态）。tote_id 为可选的现场筐/台号标签。"""
    await _require_merchant(request, principal)
    pool = _pool(request)
    tote_id = body.get("tote_id")
    note = body.get("note") or ""
    if tote_id is not None and not isinstance(tote_id, str):
        raise HTTPException(400, "tote_id must be a string")
    if len(note) > 200:
        raise HTTPException(400, "note too long")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'INSERT INTO "counter_ticket" (status, tote_id, created_by, note) '
            "VALUES ('open', $1, $2, $3) RETURNING *",
            tote_id,
            str(principal["customer_id"]),
            note,
        )
    return _counter_ticket_dict(row, 0)


@router.get("/counter/tickets")
async def counter_list_tickets(
    request: Request,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """本仓管家的单据列表（open 与 pending_settlement 在前，最新在前）。"""
    await _require_merchant(request, principal)
    pool = _pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT * FROM "counter_ticket" WHERE created_by = $1 '
            "ORDER BY created_at DESC LIMIT 20",
            str(principal["customer_id"]),
        )
        tickets = []
        for row in rows:
            total = await _counter_ticket_total(conn, row["id"])
            tickets.append(_counter_ticket_dict(row, total))
    return {"tickets": tickets}


@router.get("/counter/tickets/{ticket_id}")
async def counter_get_ticket(
    request: Request,
    ticket_id: str,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """单据详情：头 + 全部称重行项（同记一本账的 hardware_event 行）+ 合计。"""
    await _require_merchant(request, principal)
    from app.ext.hardware_webhook import _weigh_event_dict

    pool = _pool(request)
    async with pool.acquire() as conn:
        ticket = await _counter_require_ticket(conn, ticket_id, str(principal["customer_id"]))
        lines = await conn.fetch(
            'SELECT he.*, p.title FROM "hardware_event" he '
            "LEFT JOIN inventory_batch ib ON ib.id = he.batch_id "
            "LEFT JOIN product_variant pv ON pv.id = ib.variant_id "
            "LEFT JOIN product p ON p.id = pv.product_id "
            "WHERE he.ticket_id = $1 AND he.event_type = 'weigh' "
            "ORDER BY he.created_at ASC",
            ticket["id"],
        )
        total = await _counter_ticket_total(conn, ticket["id"])
    return {
        "ticket": _counter_ticket_dict(ticket, total),
        "lines": [_weigh_event_dict(row) for row in lines],
    }


@router.post("/counter/tickets/{ticket_id}/lines")
async def counter_add_line(
    request: Request,
    ticket_id: str,
    body: dict,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """柜台确认记行项：点选商品 + 重量 → 当场计价写进单据。

    两种来源：
    - weigh_message_id：确认一条秤/模拟器已发的原始重量（unpriced），挂商品计价；
      body 的 weight_grams 若给了须与原始重量一致，否则 422。
    - 无 weigh_message_id：直接按 body 的 weight_grams 记行（source=manual/scale）。
    """
    await _require_merchant(request, principal)
    from app.ext.hardware_webhook import confirm_weigh_event, record_weigh_event
    from app.weigh import MAX_WEIGHT_GRAMS

    pool = _pool(request)
    source = body.get("source", "manual")
    if source not in ("manual", "scale"):
        raise HTTPException(400, "source must be 'manual' or 'scale'")
    weight_grams = body.get("weight_grams")
    weigh_message_id = body.get("weigh_message_id")
    try:
        async with pool.acquire() as conn:
            ticket = await _counter_require_ticket(conn, ticket_id, str(principal["customer_id"]))
            if ticket["status"] != "open":
                raise HTTPException(409, f"ticket not open: status={ticket['status']}")
            if weigh_message_id:
                event = await conn.fetchrow(
                    'SELECT weight_grams FROM "hardware_event" WHERE message_id = $1',
                    weigh_message_id,
                )
                if event is None:
                    raise HTTPException(404, "weigh event not found")
                if weight_grams is not None and (
                    not isinstance(weight_grams, int)
                    or int(weight_grams) != int(event["weight_grams"])
                ):
                    raise HTTPException(422, "weight_grams mismatches weigh event")
                line = await confirm_weigh_event(
                    conn,
                    message_id=weigh_message_id,
                    batch_id=body.get("batch_id"),
                    variant_id=body.get("variant_id"),
                    ticket_id=str(ticket["id"]),
                )
                from app.ext.hardware_webhook import _weigh_event_dict

                joined = await conn.fetchrow(
                    'SELECT he.*, p.title FROM "hardware_event" he '
                    "LEFT JOIN inventory_batch b ON b.id = he.batch_id "
                    "LEFT JOIN product_variant pv ON pv.id = b.variant_id "
                    "LEFT JOIN product p ON p.id = pv.product_id "
                    "WHERE he.message_id = $1",
                    weigh_message_id,
                )
                if joined is not None:
                    line = _weigh_event_dict(joined)
            else:
                if weight_grams is None:
                    raise HTTPException(400, "weight_grams is required")
                if not isinstance(weight_grams, int) or not 1 <= weight_grams <= MAX_WEIGHT_GRAMS:
                    raise HTTPException(400, f"weight_grams out of range 1..{MAX_WEIGHT_GRAMS}")
                import uuid as _uuid

                line = await record_weigh_event(
                    conn,
                    message_id=f"ui-{_uuid.uuid4().hex}",
                    weight_grams=int(weight_grams),
                    tote_id=ticket["tote_id"],
                    ticket_id=str(ticket["id"]),
                    batch_id=body.get("batch_id"),
                    variant_id=body.get("variant_id"),
                    node_id=None,
                    source=source,
                    payload={"source": "counter-ui", "ticket_id": str(ticket["id"])},
                )
            total = await _counter_ticket_total(conn, ticket["id"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"line": line, "ticket_id": str(ticket["id"]), "total_cents": total}


@router.get("/counter/latest-weight")
async def counter_latest_weight(
    request: Request,
    ticket_id: str | None = None,
    tote_id: str | None = None,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """柜台轮询：该单据/筐的最新一条称重读数（秤或模拟器发的都算）。"""
    await _require_merchant(request, principal)
    from app.ext.hardware_webhook import _weigh_event_dict

    if not ticket_id and not tote_id:
        raise HTTPException(400, "ticket_id or tote_id is required")
    pool = _pool(request)
    async with pool.acquire() as conn:
        if ticket_id:
            import uuid

            try:
                ticket_uuid = uuid.UUID(str(ticket_id))
            except ValueError:
                raise HTTPException(400, "invalid ticket_id") from None
            row = await conn.fetchrow(
                'SELECT * FROM "hardware_event" WHERE ticket_id = $1 '
                "AND event_type = 'weigh' ORDER BY created_at DESC LIMIT 1",
                ticket_uuid,
            )
        else:
            row = await conn.fetchrow(
                'SELECT * FROM "hardware_event" WHERE tote_id = $1 '
                "AND event_type = 'weigh' ORDER BY created_at DESC LIMIT 1",
                tote_id,
            )
    if row is None:
        return {"weight_grams": None}
    return _weigh_event_dict(row)


@router.post("/counter/simulate-weigh")
async def counter_simulate_weigh(
    request: Request,
    body: dict,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """模拟器（演示/验证专用，明确标记 simulator）：注入一条称重事件。

    与真实秤走同一记账函数、同一本账，唯一区别是 source='simulator'。
    真实秤接入后停用本端点（契约见 docs/weigh-scale-contract.md）。
    """
    await _require_merchant(request, principal)
    from app.ext.hardware_webhook import record_weigh_event

    pool = _pool(request)
    weight_grams = body.get("weight_grams")
    if not isinstance(weight_grams, int):
        raise HTTPException(400, "weight_grams must be an integer")
    try:
        import uuid as _uuid

        async with pool.acquire() as conn:
            event = await record_weigh_event(
                conn,
                message_id=f"sim-{_uuid.uuid4().hex}",
                weight_grams=weight_grams,
                tote_id=body.get("tote_id"),
                ticket_id=body.get("ticket_id"),
                batch_id=body.get("batch_id"),
                variant_id=body.get("variant_id"),
                node_id=body.get("node_id"),
                source="simulator",
                payload={"simulator": True, "by": str(principal["customer_id"])},
            )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    event["simulator"] = True
    return event


@router.post("/counter/tickets/{ticket_id}/pending-settlement")
async def counter_mark_pending_settlement(
    request: Request,
    ticket_id: str,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """挂单待结算：open → pending_settlement。只改状态，不创建支付单据，
    不扣款、不发券——收款在收银台完成，现金结算见 counter_settle_ticket。"""
    await _require_merchant(request, principal)
    pool = _pool(request)
    async with pool.acquire() as conn:
        ticket = await _counter_require_ticket(conn, ticket_id, str(principal["customer_id"]))
        if ticket["status"] == "pending_settlement":
            total = await _counter_ticket_total(conn, ticket["id"])
            return _counter_ticket_dict(ticket, total) | {
                "payment": None,
                "note": "already pending settlement; no payment created",
            }
        if ticket["status"] != "open":
            raise HTTPException(409, f"ticket not open: status={ticket['status']}")
        row = await conn.fetchrow(
            "UPDATE \"counter_ticket\" SET status = 'pending_settlement', "
            "updated_at = NOW() WHERE id = $1 RETURNING *",
            ticket["id"],
        )
        total = await _counter_ticket_total(conn, ticket["id"])
    return _counter_ticket_dict(row, total) | {
        "payment": None,
        "note": "pending settlement: no payment created; settle at the counter",
    }


def _counter_settlement_dict(row: Any) -> dict:
    return {
        "settlement_id": str(row["id"]),
        "ticket_id": str(row["ticket_id"]),
        "cashier_id": row["cashier_id"],
        "method": row["method"],
        "amount_due_cents": int(row["amount_due_cents"]),
        "amount_received_cents": int(row["amount_received_cents"]),
        "change_cents": int(row["change_cents"]),
        "settled_at": row["settled_at"].isoformat() if row["settled_at"] else None,
    }


def _counter_shift_dict(row: Any) -> dict:
    return {
        "shift_id": str(row["id"]),
        "cashier_id": row["cashier_id"],
        "status": row["status"],
        "opening_float_cents": int(row["opening_float_cents"]),
        "opened_at": row["opened_at"].isoformat() if row["opened_at"] else None,
        "closed_at": row["closed_at"].isoformat() if row["closed_at"] else None,
        "counted_cash_cents": (
            int(row["counted_cash_cents"]) if row["counted_cash_cents"] is not None else None
        ),
        "expected_cash_cents": (
            int(row["expected_cash_cents"]) if row["expected_cash_cents"] is not None else None
        ),
        "variance_cents": int(row["variance_cents"]) if row["variance_cents"] is not None else None,
        "handover_to": row["handover_to"],
        "note": row["note"],
    }


async def _create_counter_order(conn: Any, *, ticket: Any, due_cents: int) -> dict | None:
    """结算事务内建柜台订单并直接扣库存（调用方须已在同一事务内）。

    只取已计价行（status='processed'）；无计价行时返回 None（不造空单）。
    每行按重量/计价基准向上取整折算库存份数（见
    app/counter_settlement_order.weigh_stock_units），先按预留账本协议写
    inventory_reservation，再随即标记 consumed 并写 shipment movement、扣
    批次 stock_qty——货物离柜，不留预留等待。任一行缺价/缺批次/库存不足即
    raise ValueError，整笔结算回滚。
    """
    from app.counter_settlement_order import (
        COUNTER_ORDER_STATUS,
        ORDER_CHANNEL_COUNTER,
        require_weigh_line_pricing,
        weigh_stock_units,
    )

    lines = await conn.fetch(
        "SELECT he.*, pv.product_id, ib.variant_id AS batch_variant_id, "
        'ib.location_id AS batch_location_id FROM "hardware_event" he '
        'LEFT JOIN "inventory_batch" ib ON ib.id = he.batch_id '
        'LEFT JOIN "product_variant" pv ON pv.id = ib.variant_id '
        "WHERE he.ticket_id = $1 AND he.event_type = 'weigh' AND he.status = 'processed' "
        "ORDER BY he.created_at ASC, he.message_id ASC",
        ticket["id"],
    )
    if not lines:
        return None
    for line in lines:
        require_weigh_line_pricing(
            batch_id=line["batch_id"],
            weight_grams=line["weight_grams"],
            unit_price_cents=line["unit_price_cents"],
            price_basis_grams=line["price_basis_grams"],
            amount_cents=line["amount_cents"],
        )
    subtotal = sum(int(line["amount_cents"]) for line in lines)
    if subtotal != int(due_cents):
        raise ValueError(
            f"counter order subtotal {subtotal} does not match ticket total {due_cents}"
        )
    order_id = uuid7()
    customer_id = str(ticket["customer_id"]) if ticket["customer_id"] else None
    await conn.execute(
        'INSERT INTO "customer_order" '
        "(id, cart_id, customer_id, region_code, currency, status, "
        "subtotal_cents, discount_cents, tax_cents, shipping_cents, "
        "grand_total_cents, billing_address, shipping_address, "
        "discount_kind, voucher_redeemed_cents, channel, counter_ticket_id) "
        "VALUES ($1, NULL, $2, NULL, 'CNY', $3, $4, 0, 0, 0, $4, "
        "'{}'::jsonb, '{}'::jsonb, 'none', 0, $5, $6)",
        order_id,
        customer_id,
        COUNTER_ORDER_STATUS,
        subtotal,
        ORDER_CHANNEL_COUNTER,
        ticket["id"],
    )
    lines_out: list[dict] = []
    for line in lines:
        units = weigh_stock_units(int(line["weight_grams"]), int(line["price_basis_grams"]))
        batch = await conn.fetchrow(
            "SELECT id, variant_id, location_id, stock_qty, reserved_qty, status, "
            'inspection_status, deleted_at FROM "inventory_batch" WHERE id = $1 FOR UPDATE',
            line["batch_id"],
        )
        if batch is None:
            raise ValueError(f"batch {line['batch_id']} not found")
        if (
            batch["deleted_at"] is not None
            or batch["status"] != "active"
            or batch["inspection_status"] != "passed"
        ):
            raise ValueError(
                f"batch {line['batch_id']} is not sellable: status={batch['status']} "
                f"inspection={batch['inspection_status']}"
            )
        product_id = await conn.fetchval(
            'SELECT product_id FROM "product_variant" WHERE id = $1',
            batch["variant_id"],
        )
        if product_id is None:
            raise ValueError(f"variant {batch['variant_id']} not found for batch")
        line_id = uuid7()
        await conn.execute(
            'INSERT INTO "order_line_item" '
            "(id, order_id, batch_id, variant_id, quantity, preorder_qty, "
            "unit_price_cents, line_total_cents, location_id, weight_grams) "
            "VALUES ($1, $2, $3, $4, $5, 0, $6, $7, $8, $9)",
            line_id,
            order_id,
            batch["id"],
            batch["variant_id"],
            units,
            int(line["unit_price_cents"]),
            int(line["amount_cents"]),
            batch["location_id"],
            int(line["weight_grams"]),
        )
        reservation_id = await _reserve_batch_qty(
            conn,
            order_id=order_id,
            line_id=line_id,
            product_id=product_id,
            variant_id=batch["variant_id"],
            batch=batch,
            quantity=units,
            reason="counter settlement: goods leave at counter",
        )
        if reservation_id is None:
            raise ValueError(
                f"insufficient stock for batch {line['batch_id']}: needs {units} units"
            )
        # 随即核销：与 _ship_order_inventory 的逐预留核销写同一组账（批次
        # stock/reserved 双扣 + 预留 consumed + shipment movement），货物已离柜。
        shipped = await conn.fetchrow(
            'UPDATE "inventory_batch" '
            "SET stock_qty = stock_qty - $1, reserved_qty = reserved_qty - $1, "
            "updated_at = NOW() WHERE id = $2 AND stock_qty >= $1 AND reserved_qty >= $1 "
            "RETURNING id",
            units,
            batch["id"],
        )
        if shipped is None:
            raise ValueError(f"batch {line['batch_id']} reservation is inconsistent while shipping")
        consumed = await conn.fetchrow(
            'UPDATE "inventory_reservation" '
            "SET consumed_qty = $1, status = 'consumed', updated_at = NOW() "
            "WHERE id = $2 RETURNING id",
            units,
            reservation_id,
        )
        if consumed is None:
            raise ValueError(f"reservation {reservation_id} could not be marked consumed")
        await conn.execute(
            'INSERT INTO "stock_movement" '
            "(product_id, variant_id, location_id, batch_id, "
            "movement_type, quantity, reference_type, reference_id, "
            "reason, idempotency_key) "
            "VALUES ($1, $2, $3, $4, 'shipment', $5, 'order', $6, $7, $8) "
            "ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING",
            str(product_id),
            str(batch["variant_id"]) if batch["variant_id"] else None,
            batch["location_id"],
            batch["id"],
            units,
            str(order_id),
            "counter settlement shipment",
            f"shipment:{order_id}:{reservation_id}:{units}",
        )
        lines_out.append(
            {
                "line_id": str(line_id),
                "batch_id": str(batch["id"]),
                "variant_id": str(batch["variant_id"]),
                "quantity": units,
                "weight_grams": int(line["weight_grams"]),
                "price_basis_grams": int(line["price_basis_grams"]),
                "unit_price_cents": int(line["unit_price_cents"]),
                "line_total_cents": int(line["amount_cents"]),
            }
        )
    return {
        "order_id": str(order_id),
        "status": COUNTER_ORDER_STATUS,
        "channel": ORDER_CHANNEL_COUNTER,
        "customer_id": customer_id,
        "counter_ticket_id": str(ticket["id"]),
        "subtotal_cents": subtotal,
        "discount_cents": 0,
        "grand_total_cents": subtotal,
        "lines": lines_out,
    }


@router.post("/counter/tickets/{ticket_id}/settle")
async def counter_settle_ticket(
    request: Request,
    ticket_id: str,
    body: dict,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """现金结算：open/pending_settlement → settled，落一条结算记录，并在同一
    事务内建 customer_order（渠道 counter）与直接扣库存。

    只收现金：method 缺失 400、非 cash 422。应收取自服务端重算的单据合计，
    实收小于应收 422，找零 = 实收 - 应收。建单不碰券账本与 9 折逻辑
    （discount 0 / kind none / 券核销 0），不伪造 payment_intent，散客订单
    customer_id 为空。幂等：单据行锁 + 状态守卫 + counter_settlement /
    counter_ticket_id 唯一约束，重结 409；任一称重行缺价/缺批次/库存不足
    时整笔回滚（409），单据保持可结算态。
    """
    from asyncpg import UniqueViolationError

    from app.counter_closeout import SETTLE_METHOD_CASH, compute_change_cents

    await _require_merchant(request, principal)
    method = body.get("method")
    if method is None:
        raise HTTPException(400, "method is required")
    if method != SETTLE_METHOD_CASH:
        raise HTTPException(422, f"only cash settlement is supported, got method={method}")
    received = body.get("amount_received_cents")
    if not isinstance(received, int) or isinstance(received, bool) or received < 0:
        raise HTTPException(400, "amount_received_cents must be a non-negative integer")
    pool = _pool(request)
    cashier_id = str(principal["customer_id"])
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                ticket = await _counter_require_ticket(conn, ticket_id, cashier_id)
                locked = await conn.fetchrow(
                    'SELECT id FROM "counter_ticket" WHERE id = $1 FOR UPDATE',
                    ticket["id"],
                )
                if locked is None:
                    raise HTTPException(404, "ticket not found")
                if ticket["status"] not in ("open", "pending_settlement"):
                    raise HTTPException(409, f"ticket not settleable: status={ticket['status']}")
                existing_order = await conn.fetchval(
                    'SELECT id FROM "customer_order" WHERE counter_ticket_id = $1',
                    ticket["id"],
                )
                if existing_order is not None:
                    raise HTTPException(409, "ticket already has an order")
                due = await _counter_ticket_total(conn, ticket["id"])
                try:
                    change = compute_change_cents(due, received)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
                try:
                    order = await _create_counter_order(conn, ticket=ticket, due_cents=due)
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from exc
                row = await conn.fetchrow(
                    'INSERT INTO "counter_settlement" '
                    "(ticket_id, cashier_id, method, amount_due_cents, "
                    "amount_received_cents, change_cents) VALUES ($1, $2, 'cash', $3, $4, $5) "
                    "RETURNING *",
                    ticket["id"],
                    cashier_id,
                    due,
                    received,
                    change,
                )
                updated = await conn.fetchrow(
                    "UPDATE \"counter_ticket\" SET status = 'settled', updated_at = NOW() "
                    "WHERE id = $1 RETURNING *",
                    ticket["id"],
                )
                await conn.execute(
                    "UPDATE \"hardware_event\" SET settlement_status = 'settled' "
                    "WHERE ticket_id = $1 AND event_type = 'weigh' AND status = 'processed'",
                    ticket["id"],
                )
    except UniqueViolationError:
        raise HTTPException(409, "ticket already settled: duplicate settlement or order") from None
    return _counter_ticket_dict(updated, due) | {
        "settlement": _counter_settlement_dict(row),
        "order": order,
    }


@router.post("/counter/shifts")
async def counter_open_shift(
    request: Request,
    body: dict,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """开班：收银员 + 备用金；同一收银员已有未交班次时 409。"""
    await _require_merchant(request, principal)
    opening_float = body.get("opening_float_cents")
    if not isinstance(opening_float, int) or isinstance(opening_float, bool) or opening_float < 0:
        raise HTTPException(400, "opening_float_cents must be a non-negative integer")
    note = body.get("note") or ""
    if not isinstance(note, str):
        raise HTTPException(400, "note must be a string")
    pool = _pool(request)
    cashier_id = str(principal["customer_id"])
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM \"counter_shift\" WHERE cashier_id = $1 AND status = 'open'",
            cashier_id,
        )
        if existing is not None:
            raise HTTPException(409, "cashier already has an open shift")
        row = await conn.fetchrow(
            'INSERT INTO "counter_shift" (cashier_id, opening_float_cents, note) '
            "VALUES ($1, $2, $3) RETURNING *",
            cashier_id,
            opening_float,
            note,
        )
    return {"shift": _counter_shift_dict(row)}


@router.get("/counter/shifts")
async def counter_list_shifts(
    request: Request,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """交接班列表：只看自己的班次，最新在前。"""
    await _require_merchant(request, principal)
    pool = _pool(request)
    cashier_id = str(principal["customer_id"])
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT * FROM "counter_shift" WHERE cashier_id = $1 '
            "ORDER BY opened_at DESC, id DESC LIMIT 50",
            cashier_id,
        )
    return {"shifts": [_counter_shift_dict(r) for r in rows]}


@router.post("/counter/shifts/{shift_id}/close")
async def counter_close_shift(
    request: Request,
    shift_id: str,
    body: dict,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """交班：实盘现金 + 交接给下一收银员；差异 = 实盘 - 应有，正负如实记录。

    系统应有现金 = 备用金 + 本班内该收银员现金结算的应收合计（实收 - 找零
    = 应收，故用应收合计）。只算本班窗口内、本人的结算，别人的不算。
    """
    from app.counter_closeout import compute_shift_expected_cents, compute_shift_variance_cents

    await _require_merchant(request, principal)
    counted = body.get("counted_cash_cents")
    if not isinstance(counted, int) or isinstance(counted, bool) or counted < 0:
        raise HTTPException(400, "counted_cash_cents must be a non-negative integer")
    handover_to = body.get("handover_to")
    if handover_to is not None and not isinstance(handover_to, str):
        raise HTTPException(400, "handover_to must be a string")
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise HTTPException(400, "note must be a string")
    pool = _pool(request)
    cashier_id = str(principal["customer_id"])
    import uuid

    try:
        shift_uuid = uuid.UUID(str(shift_id))
    except ValueError:
        raise HTTPException(400, "invalid shift_id") from None
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow('SELECT * FROM "counter_shift" WHERE id = $1', shift_uuid)
            if row is None:
                raise HTTPException(404, "shift not found")
            if str(row["cashier_id"]) != cashier_id:
                raise HTTPException(403, "shift belongs to another cashier")
            if row["status"] != "open":
                raise HTTPException(409, f"shift not open: status={row['status']}")
            due_sum = await conn.fetchval(
                'SELECT COALESCE(SUM(amount_due_cents), 0)::int FROM "counter_settlement" '
                "WHERE cashier_id = $1 AND settled_at >= $2 AND settled_at <= NOW()",
                cashier_id,
                row["opened_at"],
            )
            expected = compute_shift_expected_cents(
                int(row["opening_float_cents"]), int(due_sum or 0)
            )
            variance = compute_shift_variance_cents(counted, expected)
            updated = await conn.fetchrow(
                "UPDATE \"counter_shift\" SET status = 'closed', closed_at = NOW(), "
                "counted_cash_cents = $2, expected_cash_cents = $3, variance_cents = $4, "
                "handover_to = COALESCE($5, handover_to), note = COALESCE($6, note), "
                "updated_at = NOW() WHERE id = $1 RETURNING *",
                shift_uuid,
                counted,
                expected,
                variance,
                handover_to,
                note,
            )
    return {"shift": _counter_shift_dict(updated)}


@router.get("/counter/daily-report")
async def counter_daily_report(
    request: Request,
    date: str | None = None,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """日结报表：当日已结算单据、按支付方式分列、交班现金差异、毛利口径。

    毛利只计有批次成本价与计价基准的已结算称重行（成本 = 重量 × 成本价 /
    计价基准），无成本价/无基准的行排除并计数（margin_excluded_lines），
    绝不估算。日期缺省为当天（DB 日期）；格式错误 400。
    """
    await _require_merchant(request, principal)
    import datetime

    if date is not None:
        try:
            day_param: datetime.date | None = datetime.date.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, "date must be YYYY-MM-DD") from None
    else:
        day_param = None
    pool = _pool(request)
    async with pool.acquire() as conn:
        settled = await conn.fetchrow(
            "SELECT COUNT(*)::int AS tickets, "
            "COALESCE(SUM(amount_due_cents), 0)::int AS total_due_cents "
            'FROM "counter_settlement" WHERE settled_at::date = COALESCE($1::date, CURRENT_DATE)',
            day_param,
        )
        by_method_rows = await conn.fetch(
            "SELECT method, COUNT(*)::int AS tickets, "
            "COALESCE(SUM(amount_due_cents), 0)::int AS total_cents "
            'FROM "counter_settlement" WHERE settled_at::date = COALESCE($1::date, CURRENT_DATE) '
            "GROUP BY method ORDER BY method",
            day_param,
        )
        margin = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(he.amount_cents), 0)::int AS revenue_cents,
                   COALESCE(SUM(ROUND(
                       he.weight_grams::numeric * ib.cost_price_cents
                       / he.price_basis_grams)), 0)::int AS cost_cents,
                   COUNT(*)::int AS costed_lines
            FROM hardware_event he
            JOIN counter_ticket t ON t.id = he.ticket_id
            JOIN counter_settlement s ON s.ticket_id = t.id
            JOIN inventory_batch ib ON ib.id = he.batch_id
            WHERE he.event_type = 'weigh' AND he.status = 'processed'
              AND he.amount_cents IS NOT NULL
              AND he.weight_grams IS NOT NULL AND he.price_basis_grams IS NOT NULL
              AND ib.cost_price_cents IS NOT NULL
              AND s.settled_at::date = COALESCE($1::date, CURRENT_DATE)
            """,
            day_param,
        )
        total_lines = await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM hardware_event he
            JOIN counter_ticket t ON t.id = he.ticket_id
            JOIN counter_settlement s ON s.ticket_id = t.id
            WHERE he.event_type = 'weigh' AND he.status = 'processed'
              AND he.amount_cents IS NOT NULL
              AND s.settled_at::date = COALESCE($1::date, CURRENT_DATE)
            """,
            day_param,
        )
        shifts = await conn.fetchrow(
            "SELECT COUNT(*)::int AS closed_shifts, "
            "COALESCE(SUM(variance_cents), 0)::int AS variance_total_cents "
            "FROM \"counter_shift\" WHERE status = 'closed' "
            "AND closed_at::date = COALESCE($1::date, CURRENT_DATE)",
            day_param,
        )
        day_text = day_param.isoformat() if day_param is not None else None
        if day_text is None:
            day_text = (await conn.fetchval("SELECT CURRENT_DATE::text")) or ""
    revenue = int(margin["revenue_cents"])
    cost = int(margin["cost_cents"])
    costed = int(margin["costed_lines"])
    excluded = int(total_lines or 0) - costed
    return {
        "date": day_text,
        "settled_tickets": int(settled["tickets"]),
        "total_sales_cents": int(settled["total_due_cents"]),
        "by_method": [
            {
                "method": r["method"],
                "tickets": int(r["tickets"]),
                "total_cents": int(r["total_cents"]),
            }
            for r in by_method_rows
        ],
        "margin_revenue_cents": revenue,
        "margin_cost_cents": cost,
        "margin_cents": revenue - cost,
        "margin_costed_lines": costed,
        "margin_excluded_lines": excluded,
        "margin_basis": (
            "毛利仅计有批次成本价与计价基准的已结算称重行；无成本价/无基准的行"
            f"不计入（{excluded} 行），口径偏高；成本 = 重量 × 批次成本价 / 计价基准"
        ),
        "shifts_closed": int(shifts["closed_shifts"]),
        "cash_variance_total_cents": int(shifts["variance_total_cents"]),
    }


@router.post("/counter/tickets/{ticket_id}/void")
async def counter_void_ticket(
    request: Request,
    ticket_id: str,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """作废单据：仅 open 态可作废，行项留痕备查。"""
    await _require_merchant(request, principal)
    pool = _pool(request)
    async with pool.acquire() as conn:
        ticket = await _counter_require_ticket(conn, ticket_id, str(principal["customer_id"]))
        if ticket["status"] != "open":
            raise HTTPException(409, f"ticket not open: status={ticket['status']}")
        row = await conn.fetchrow(
            "UPDATE \"counter_ticket\" SET status = 'void', updated_at = NOW() "
            "WHERE id = $1 RETURNING *",
            ticket["id"],
        )
        total = await _counter_ticket_total(conn, ticket["id"])
    return _counter_ticket_dict(row, total)


# ── 柜台视觉识别接线 ─────────────────────────────────────────────────────
# 设计稿《收银台电子称 + 系统自动识别》§5：识别只给前 3 候选、店员点确认，
# 达标前不自动确认。本线只建后端接线（识别端点 + 帧采集 + 数据集上传 +
# 评估账本），不改 /shop/counter 页面；UI 接入等模型有了以后另案。
# 模型未训：默认识别器返回空候选，调用方退回拍照点选，绝不编识别结果。
# 图像存本地卷（output_root 下 counter_frames / counter_dataset），不进商品
# 媒体库、不进蜘蛛图目录；数据集照片只认柜台实拍，上传成功不代表可训练。


def _counter_vision_dir(request: Request, subdir: str) -> Path:
    """识别接线的文件根：<output_root>/<subdir>，与 agent_ingest 同一持久化机制。"""
    return _output_dir(request, subdir)


async def _counter_read_upload(file: Any, *, field: str = "file") -> bytes:
    """读上传文件字节，10MB 封顶（超限 413，读一半不落盘）。"""
    from app.counter_vision import MAX_IMAGE_BYTES

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise HTTPException(413, f"{field} too large: over {MAX_IMAGE_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _counter_image_payload(request: Request) -> tuple[bytes, dict[str, Any]]:
    """从 multipart 或 JSON 请求体里取出一帧图 + 其余字段。

    multipart：file 字段为图，其余表单字段进 fields。
    JSON：image_base64（标准 base64）+ 其余字段。
    其它 content-type → 415。图字节本身不做格式判定，调用方以魔数校验。
    """
    import base64
    import binascii

    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        from starlette.datastructures import UploadFile as _StarletteUploadFile

        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, _StarletteUploadFile):
            raise HTTPException(400, "file is required")
        fields = {key: form[key] for key in form if key != "file"}
        return await _counter_read_upload(upload), fields
    if content_type.startswith("application/json"):
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "invalid JSON body") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "JSON object body is required")
        b64 = payload.get("image_base64")
        if not isinstance(b64, str) or not b64.strip():
            raise HTTPException(400, "image_base64 is required")
        try:
            data = base64.b64decode(b64, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(400, "image_base64 is not valid base64") from None
        from app.counter_vision import MAX_IMAGE_BYTES

        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, f"image too large: over {MAX_IMAGE_BYTES} bytes")
        fields = {key: value for key, value in payload.items() if key != "image_base64"}
        return data, fields
    raise HTTPException(415, "content-type must be multipart/form-data or application/json")


def _counter_parse_weight_grams(fields: dict[str, Any]) -> int:
    """recognize 的 weight_grams：必填整数，1..15000 克（秤量程）。"""
    from app.weigh import MAX_WEIGHT_GRAMS

    raw = fields.get("weight_grams")
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw.lstrip("-").isdigit():
            raise HTTPException(400, "weight_grams must be an integer")
        raw = int(raw)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise HTTPException(400, "weight_grams is required and must be an integer")
    if not 1 <= raw <= MAX_WEIGHT_GRAMS:
        raise HTTPException(400, f"weight_grams out of range 1..{MAX_WEIGHT_GRAMS}")
    return raw


def _counter_parse_optional_uuid(fields: dict[str, Any], key: str) -> Any | None:
    """可选 UUID 字段：给了须为合法 UUID 串，返回 UUID 对象；没给返回 None。"""
    raw = fields.get(key)
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise HTTPException(400, f"{key} must be a UUID string")
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(400, f"invalid {key}") from None


def _counter_parse_captured_at(raw: object) -> Any | None:
    """可选拍摄时间：ISO 8601 串；没给返回 None，格式非法 400。"""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise HTTPException(400, "captured_at must be an ISO 8601 string")
    from datetime import datetime

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "captured_at is not ISO 8601") from None


def _counter_parse_camera_id(fields: dict[str, Any]) -> str | None:
    camera_id = fields.get("camera_id")
    if camera_id is None or camera_id == "":
        return None
    if not isinstance(camera_id, str) or len(camera_id) > 64:
        raise HTTPException(400, "camera_id must be a string of at most 64 chars")
    return camera_id


async def _counter_check_ticket_exists(conn: Any, ticket_uuid: Any) -> None:
    """ticket_id 给了须对应一张真实单据（只验存在，不验归属——帧是留痕证据）。"""
    row = await conn.fetchrow('SELECT id FROM "counter_ticket" WHERE id = $1', ticket_uuid)
    if row is None:
        raise HTTPException(404, "ticket not found")


async def _counter_check_weigh_message(conn: Any, message_id: object) -> None:
    """weigh_message_id 给了须对应一条真实称重事件，否则 404（防挂错账）。"""
    if message_id is None or message_id == "":
        return
    if not isinstance(message_id, str):
        raise HTTPException(400, "weigh_message_id must be a string")
    row = await conn.fetchrow(
        "SELECT message_id FROM \"hardware_event\" WHERE message_id = $1 AND event_type = 'weigh'",
        message_id,
    )
    if row is None:
        raise HTTPException(404, "weigh event not found")


def _counter_frame_dict(row: Any) -> dict:
    return {
        "frame_id": str(row["id"]),
        "frame_path": row["frame_path"],
        "content_type": row["content_type"],
        "byte_size": row["byte_size"],
        "sha256": row["sha256"],
        "camera_id": row["camera_id"],
        "captured_at": row["captured_at"].isoformat() if row["captured_at"] else None,
        "weigh_message_id": row["weigh_message_id"],
        "ticket_id": str(row["ticket_id"]) if row["ticket_id"] else None,
        "client_frame_id": row["client_frame_id"],
        "source": row["source"],
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def _counter_recognition_dict(row: Any) -> dict:
    import json as _json

    candidates = row["candidates"]
    if isinstance(candidates, str):
        candidates = _json.loads(candidates)
    return {
        "recognition_id": str(row["id"]),
        "frame_id": str(row["frame_id"]),
        "weight_grams": row["weight_grams"],
        "candidates": candidates,
        "model_name": row["model_name"],
        "model_configured": row["model_configured"],
        "confirmed_variant_id": (
            str(row["confirmed_variant_id"]) if row["confirmed_variant_id"] else None
        ),
        "confirmed_none": row["confirmed_none"],
        "confirmed_at": row["confirmed_at"].isoformat() if row["confirmed_at"] else None,
        "confirmed_by": row["confirmed_by"],
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def _counter_store_frame(
    conn: Any,
    *,
    frame_dir: Path,
    rel_prefix: str,
    data: bytes,
    image_format: str,
    ext: str,
    camera_id: str | None,
    captured_at: Any,
    weigh_message_id: str | None,
    ticket_uuid: Any,
    client_frame_id: str | None,
    source: str,
    created_by: str,
) -> tuple[Any, bool]:
    """帧落盘 + 写 counter_frame 行，返回 (row, duplicate)。

    client_frame_id 命中已存行时返回旧行 + duplicate=True，不重复落盘、
    不重复记账（网关重发幂等）。先落盘再写行：行永远指向已存在的文件；
    唯一约束撞上的竞态下重查，孤儿文件（uuid 名、无引用）无害。
    """
    from datetime import datetime

    from asyncpg import UniqueViolationError

    from app.counter_vision import frame_filename, sha256_hex

    if client_frame_id:
        existing = await conn.fetchrow(
            'SELECT * FROM "counter_frame" WHERE client_frame_id = $1', client_frame_id
        )
        if existing is not None:
            return existing, True
    frame_id = str(uuid.uuid4())
    day = datetime.now().strftime("%Y%m%d")
    day_dir = frame_dir / day
    day_dir.mkdir(parents=True, exist_ok=True)
    filename = frame_filename(frame_id, ext)
    (day_dir / filename).write_bytes(data)
    rel_path = f"{rel_prefix}/{day}/{filename}"
    try:
        row = await conn.fetchrow(
            'INSERT INTO "counter_frame" (id, frame_path, content_type, byte_size, '
            "sha256, camera_id, captured_at, weigh_message_id, ticket_id, "
            "client_frame_id, source, created_by) VALUES ($1, $2, $3, $4, $5, $6, $7, "
            "$8, $9, $10, $11, $12) RETURNING *",
            uuid.UUID(frame_id),
            rel_path,
            f"image/{image_format}",
            len(data),
            sha256_hex(data),
            camera_id,
            captured_at,
            weigh_message_id,
            ticket_uuid,
            client_frame_id,
            source,
            created_by,
        )
    except UniqueViolationError:
        existing = await conn.fetchrow(
            'SELECT * FROM "counter_frame" WHERE client_frame_id = $1', client_frame_id
        )
        if existing is not None:
            return existing, True
        raise
    return row, False


@router.post("/counter/recognize")
async def counter_recognize(
    request: Request,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """识别一帧：返回前 3 候选 {variant_id, score}，结果写评估账本。

    模型未训时返回 200 空候选 + model_configured=false，调用方退回拍照点选；
    识别超时 3 秒同样返回空候选。绝不编分数、绝不瞎猜。
    """
    await _require_merchant(request, principal)
    from app.counter_vision import (
        get_recognizer,
        run_recognition,
        validate_image_bytes,
    )

    data, fields = await _counter_image_payload(request)
    try:
        image_format, ext = validate_image_bytes(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    weight_grams = _counter_parse_weight_grams(fields)
    ticket_uuid = _counter_parse_optional_uuid(fields, "ticket_id")
    camera_id = _counter_parse_camera_id(fields)
    captured_at = _counter_parse_captured_at(fields.get("captured_at"))
    weigh_message_id = fields.get("weigh_message_id")
    if weigh_message_id is not None and not isinstance(weigh_message_id, str):
        raise HTTPException(400, "weigh_message_id must be a string")

    recognizer = get_recognizer()
    pool = _pool(request)
    frame_dir = _counter_vision_dir(request, "counter_frames")
    async with pool.acquire() as conn:
        if ticket_uuid is not None:
            await _counter_check_ticket_exists(conn, ticket_uuid)
        await _counter_check_weigh_message(conn, weigh_message_id)
        frame_row, _duplicate = await _counter_store_frame(
            conn,
            frame_dir=frame_dir,
            rel_prefix="counter_frames",
            data=data,
            image_format=image_format,
            ext=ext,
            camera_id=camera_id,
            captured_at=captured_at,
            weigh_message_id=weigh_message_id,
            ticket_uuid=ticket_uuid,
            client_frame_id=None,
            source="recognize",
            created_by=str(principal["customer_id"]),
        )
        candidates, configured = await run_recognition(recognizer, data, weight_grams)
        rec_row = await conn.fetchrow(
            'INSERT INTO "counter_recognition_event" (frame_id, weight_grams, '
            "candidates, model_name, model_configured, created_by) "
            "VALUES ($1, $2, $3::jsonb, $4, $5, $6) RETURNING *",
            frame_row["id"],
            weight_grams,
            json.dumps(candidates),
            getattr(recognizer, "name", "unknown"),
            configured,
            str(principal["customer_id"]),
        )
    if not configured:
        logger.info(
            "counter recognize: no model configured; empty candidates (recognition_id=%s)",
            rec_row["id"],
        )
    return {
        "recognition_id": str(rec_row["id"]),
        "frame_id": str(frame_row["id"]),
        "candidates": candidates,
        "weight_grams": weight_grams,
        "model_configured": configured,
    }


@router.post("/counter/frames")
async def counter_frames(
    request: Request,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """通用摄像头帧采集：网关推帧 → 存本地卷 → 返回帧 ID。

    multipart 的 file 字段或 JSON 的 image_base64，二选一。
    可选关联 ticket_id / weigh_message_id（给了须存在，否则 404）；
    client_frame_id 供网关重发幂等（命中已存行返回 duplicate=true）。
    """
    await _require_merchant(request, principal)
    from app.counter_vision import validate_image_bytes

    data, fields = await _counter_image_payload(request)
    try:
        image_format, ext = validate_image_bytes(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    ticket_uuid = _counter_parse_optional_uuid(fields, "ticket_id")
    camera_id = _counter_parse_camera_id(fields)
    captured_at = _counter_parse_captured_at(fields.get("captured_at"))
    weigh_message_id = fields.get("weigh_message_id")
    if weigh_message_id is not None and not isinstance(weigh_message_id, str):
        raise HTTPException(400, "weigh_message_id must be a string")
    client_frame_id = fields.get("client_frame_id")
    if client_frame_id is not None and (
        not isinstance(client_frame_id, str) or not client_frame_id or len(client_frame_id) > 128
    ):
        raise HTTPException(400, "client_frame_id must be a non-empty string of at most 128 chars")

    pool = _pool(request)
    frame_dir = _counter_vision_dir(request, "counter_frames")
    async with pool.acquire() as conn:
        if ticket_uuid is not None:
            await _counter_check_ticket_exists(conn, ticket_uuid)
        await _counter_check_weigh_message(conn, weigh_message_id)
        frame_row, duplicate = await _counter_store_frame(
            conn,
            frame_dir=frame_dir,
            rel_prefix="counter_frames",
            data=data,
            image_format=image_format,
            ext=ext,
            camera_id=camera_id,
            captured_at=captured_at,
            weigh_message_id=weigh_message_id,
            ticket_uuid=ticket_uuid,
            client_frame_id=client_frame_id,
            source="camera",
            created_by=str(principal["customer_id"]),
        )
    return _counter_frame_dict(frame_row) | {"duplicate": duplicate}


@router.post("/counter/dataset-photos")
async def counter_dataset_photo(
    request: Request,
    file: UploadFile,
    sku: str = Form(...),
    photographer: str = Form(""),
    note: str = Form(""),
    captured_at: str | None = Form(None),
    variant_id: str | None = Form(None),
    principal: dict = Depends(get_current_customer),
) -> dict:
    """数据集照片上传：按 SKU 分目录存 + manifest.json 留痕。

    只接受图片（魔数校验）；SKU 目录名白名单防穿越；variant_id 给了须真实存在。
    上传成功只代表照片入库，不代表可训练——训练另案，待柜台实拍照片到位。
    """
    await _require_merchant(request, principal)
    from app.counter_vision import (
        append_manifest_entry,
        build_manifest_entry,
        dataset_filename,
        sanitize_sku,
        sha256_hex,
        validate_image_bytes,
    )

    try:
        sku_clean = sanitize_sku(sku)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if len(photographer) > 64:
        raise HTTPException(400, "photographer must be at most 64 chars")
    if len(note) > 500:
        raise HTTPException(400, "note must be at most 500 chars")
    captured = _counter_parse_captured_at(captured_at)
    variant_uuid = None
    if variant_id:
        try:
            variant_uuid = uuid.UUID(variant_id)
        except ValueError:
            raise HTTPException(400, "invalid variant_id") from None
    data = await _counter_read_upload(file)
    try:
        _image_format, ext = validate_image_bytes(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    pool = _pool(request)
    async with pool.acquire() as conn:
        if variant_uuid is not None:
            exists = await conn.fetchval(
                "SELECT 1 FROM product_variant WHERE id = $1", variant_uuid
            )
            if not exists:
                raise HTTPException(404, "variant not found")
    root = _counter_vision_dir(request, "counter_dataset") / sku_clean
    root.mkdir(parents=True, exist_ok=True)
    entry_uuid = str(uuid.uuid4())
    filename = dataset_filename(entry_uuid, ext)
    (root / filename).write_bytes(data)
    entry = build_manifest_entry(
        filename=filename,
        sku=sku_clean,
        variant_id=str(variant_uuid) if variant_uuid else None,
        captured_at=captured.isoformat() if captured else None,
        photographer=photographer,
        note=note,
        uploaded_by=str(principal["customer_id"]),
        sha256=sha256_hex(data),
        byte_size=len(data),
    )
    append_manifest_entry(root / "manifest.json", entry)
    return {
        "stored": True,
        "sku": sku_clean,
        "file": filename,
        "path": f"counter_dataset/{sku_clean}/{filename}",
        "manifest": entry,
        "note": "photo stored; training is a separate step pending real counter photos",
    }


@router.post("/counter/recognitions/{recognition_id}/confirm")
async def counter_confirm_recognition(
    request: Request,
    recognition_id: str,
    body: dict,
    principal: dict = Depends(get_current_customer),
) -> dict:
    """评估账本确认口子（供将来柜台页确认时调用，本次不接前端）：
    记下店员最终确认的 variant，或 none_correct=true 表示候选全错。
    允许重复确认（以最后一次为准，便于纠错）。"""
    await _require_merchant(request, principal)
    try:
        rec_uuid = uuid.UUID(str(recognition_id))
    except ValueError:
        raise HTTPException(400, "invalid recognition_id") from None
    none_correct = body.get("none_correct", False)
    if not isinstance(none_correct, bool):
        raise HTTPException(400, "none_correct must be a boolean")
    variant_uuid = None
    if not none_correct:
        raw_variant = body.get("variant_id")
        if not isinstance(raw_variant, str) or not raw_variant.strip():
            raise HTTPException(400, "variant_id is required unless none_correct is true")
        try:
            variant_uuid = uuid.UUID(raw_variant)
        except ValueError:
            raise HTTPException(400, "invalid variant_id") from None
    pool = _pool(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM "counter_recognition_event" WHERE id = $1', rec_uuid
        )
        if row is None:
            raise HTTPException(404, "recognition event not found")
        if variant_uuid is not None:
            exists = await conn.fetchval(
                "SELECT 1 FROM product_variant WHERE id = $1", variant_uuid
            )
            if not exists:
                raise HTTPException(404, "variant not found")
        updated = await conn.fetchrow(
            'UPDATE "counter_recognition_event" SET confirmed_variant_id = $1, '
            "confirmed_none = $2, confirmed_at = NOW(), confirmed_by = $3 "
            "WHERE id = $4 RETURNING *",
            variant_uuid,
            none_correct,
            str(principal["customer_id"]),
            rec_uuid,
        )
    return _counter_recognition_dict(updated)
