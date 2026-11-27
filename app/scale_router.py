"""app.scale_router — 秤/称重/识别/收银/拣货 相关 API 端点。

包含：
- 秤设备 CRUD
- 串口数据解析
- 摄像头识别 (POS 收银 / 拣货称重)
- 收银结算
- 拣货单管理与实称记录
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from PIL import Image
from pydantic import BaseModel

from .counter_settlement_order import require_weigh_line_pricing
from .deps import require_staff_role
from .product_photo import _actor
from .scale import (
    DEFAULT_STABLE_COUNT,
    DEFAULT_STABLE_THRESHOLD_G,
    MAX_WEIGHT_GRAMS,
    ScaleService,
    parse_scale_line,
)
from .scale_vision import ONNXRecognizer, get_scale_recognizer
from .weigh_price import (
    validate_weight_grams,
)
from .weigh_settle import WeighSettlementError, WeighSettlementNotFound, record_actual_weight

logger = logging.getLogger("hemall.scale_router")

router = APIRouter(prefix="/admin/scale", tags=["scale"])

_STAFF_ROLES = ("admin", "support", "staff")


# ──────────────────────────────────────────────────────────────────────────────
# 依赖注入
# ──────────────────────────────────────────────────────────────────────────────


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        from .db_recovery import kick_pool_recovery

        kick_pool_recovery(request.app)
        raise HTTPException(503, "database not ready")
    return pool


def _scale_service(request: Request) -> ScaleService:
    return ScaleService(_pool(request))


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic 模型
# ──────────────────────────────────────────────────────────────────────────────


class DeviceCreate(BaseModel):
    name: str
    port_path: str
    baud_rate: int = 9600
    protocol: str = "auto"
    parser_config: dict = {}
    stable_count: int = DEFAULT_STABLE_COUNT
    stable_threshold_g: int = DEFAULT_STABLE_THRESHOLD_G
    tare_enabled: bool = True
    zero_enabled: bool = True
    bridge_enabled: bool = False
    bridge_ws_port: int | None = None
    location_id: str | None = None
    note: str = ""


class DeviceUpdate(BaseModel):
    name: str | None = None
    port_path: str | None = None
    baud_rate: int | None = None
    protocol: str | None = None
    parser_config: dict | None = None
    stable_count: int | None = None
    stable_threshold_g: int | None = None
    tare_enabled: bool | None = None
    zero_enabled: bool | None = None
    bridge_enabled: bool | None = None
    bridge_ws_port: int | None = None
    location_id: str | None = None
    note: str | None = None


class ParseLineRequest(BaseModel):
    line: str
    device_id: str


class SettleRequest(BaseModel):
    lines: list[dict]
    customer_id: str | None = None
    payment_method: str = "cash"  # cash only；qr 已下掉（无支付核验，等接核验再放行）
    cash_received: int = 0


class RecordPickWeightRequest(BaseModel):
    order_id: str
    line_id: str
    weight_grams: int
    confirmed_variant_id: str


class SkipPickLineRequest(BaseModel):
    order_id: str
    line_id: str
    reason: str


# ──────────────────────────────────────────────────────────────────────────────
# 秤设备 CRUD
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/devices")
async def list_devices(
    service: ScaleService = Depends(_scale_service),
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    return await service.list_devices()


@router.post("/devices")
async def create_device(
    data: DeviceCreate,
    service: ScaleService = Depends(_scale_service),
    principal: dict = Depends(require_staff_role(*_STAFF_ROLES)),
):
    return await service.create_device(data.model_dump(), _actor(principal))


@router.patch("/devices/{device_id}")
async def update_device(
    device_id: str,
    data: DeviceUpdate,
    service: ScaleService = Depends(_scale_service),
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    result = await service.update_device(device_id, data.model_dump(exclude_unset=True))
    if not result:
        raise HTTPException(404, "device not found")
    return result


@router.delete("/devices/{device_id}")
async def delete_device(
    device_id: str,
    service: ScaleService = Depends(_scale_service),
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    ok = await service.delete_device(device_id)
    if not ok:
        raise HTTPException(404, "device not found")
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────────
# 串口数据解析 (供前端 Web Serial 实时解析)
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/parse")
async def parse_line(
    data: ParseLineRequest,
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    """解析单行秤数据，返回标准化读数"""
    reading = parse_scale_line(data.line)
    if reading is None:
        return {"reading": None}
    # 校验重量范围
    if reading.weight_grams > MAX_WEIGHT_GRAMS:
        raise HTTPException(400, f"weight exceeds max {MAX_WEIGHT_GRAMS}g")
    return {
        "reading": {
            "weight_grams": reading.weight_grams,
            "unit": reading.unit,
            "raw": reading.raw,
            "stable": reading.stable,
            "timestamp": reading.timestamp,
        }
    }


# ──────────────────────────────────────────────────────────────────────────────
# 摄像头识别 (POS 收银模式)
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/recognize")
async def recognize_frame(
    request: Request,
    file: UploadFile = File(...),
    weight_grams: int = Form(...),
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    """收银模式识别：上传帧 + 重量，返回 Top3 候选商品"""
    # 校验图片
    raw = await file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "file too large (max 10MB)")
    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()
    except Exception as e:
        raise HTTPException(400, f"invalid image: {e}") from e

    # 获取识别器
    pool = _pool(request)
    recognizer: ONNXRecognizer = await get_scale_recognizer(pool)
    if not recognizer or not recognizer._initialized:
        # 未配置模型时返回空候选，前端退回手工搜索
        return {"candidates": [], "model_configured": False}

    # 识别
    candidates = await recognizer.recognize(raw, weight_grams)
    # 补充商品详情
    enriched = []
    async with pool.acquire() as conn:
        for c in candidates:
            row = await conn.fetchrow(
                """
                SELECT pv.id, pv.sku_code, p.title, pv.reference_price_cents, pv.pricing_mode,
                       COALESCE(ib.shelf_image_url, pp.url) as image_url
                FROM product_variant pv
                JOIN product p ON p.id = pv.product_id
                LEFT JOIN inventory_batch ib ON ib.variant_id = pv.id
                    AND ib.deleted_at IS NULL AND ib.status = 'active'
                LEFT JOIN product_photo pp ON pp.variant_id = pv.id
                    AND pp.is_primary AND pp.status = 'active' AND pp.deleted_at IS NULL
                WHERE pv.id = $1 AND pv.deleted_at IS NULL
                """,
                c["variant_id"],
            )
            if row:
                enriched.append(
                    {
                        "variant_id": str(row["id"]),
                        "score": c["score"],
                        "name": row["title"],
                        "sku": row["sku_code"],
                        "price_cents": int(row["reference_price_cents"] or 0),
                        "pricing_mode": row["pricing_mode"],
                        "image_url": row["image_url"],
                    }
                )
    return {"candidates": enriched, "model_configured": True}


# ──────────────────────────────────────────────────────────────────────────────
# 摄像头识别 (拣货称重模式) - 校验是否拿对货
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/recognize-pick")
async def recognize_pick_frame(
    request: Request,
    file: UploadFile = File(...),
    weight_grams: int = Form(...),
    expected_variant_id: str = Form(...),
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    """拣货模式识别：上传帧 + 重量 + 预期 variant_id，返回候选并标记是否匹配"""
    raw = await file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "file too large (max 10MB)")
    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()
    except Exception as e:
        raise HTTPException(400, f"invalid image: {e}") from e

    pool = _pool(request)
    recognizer: ONNXRecognizer = await get_scale_recognizer(pool)
    if not recognizer or not recognizer._initialized:
        return {"candidates": [], "model_configured": False, "matched": False}

    candidates = await recognizer.recognize(raw, weight_grams)
    enriched = []
    matched = False
    async with pool.acquire() as conn:
        for c in candidates:
            row = await conn.fetchrow(
                """
                SELECT pv.id, pv.sku_code, p.title
                FROM product_variant pv
                JOIN product p ON p.id = pv.product_id
                WHERE pv.id = $1 AND pv.deleted_at IS NULL
                """,
                c["variant_id"],
            )
            if row:
                is_match = str(row["id"]) == expected_variant_id
                if is_match:
                    matched = True
                enriched.append(
                    {
                        "variant_id": str(row["id"]),
                        "score": c["score"],
                        "name": row["title"],
                        "sku": row["sku_code"],
                        "matched": is_match,
                    }
                )
    return {"candidates": enriched, "model_configured": True, "matched": matched}


# ──────────────────────────────────────────────────────────────────────────────
# 商品搜索 (收银模式手工搜索)
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/search-product")
async def search_product(
    request: Request,
    q: str = Query(..., min_length=1),
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    """按拼音/名称/SKU 搜索商品"""
    pool = _pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pv.id as variant_id, pv.sku_code, p.title,
                pv.reference_price_cents, pv.pricing_mode,
                   COALESCE(ib.shelf_image_url, pp.url) as image_url
            FROM product_variant pv
            JOIN product p ON p.id = pv.product_id
            LEFT JOIN inventory_batch ib ON ib.variant_id = pv.id
                AND ib.deleted_at IS NULL AND ib.status = 'active'
            LEFT JOIN product_photo pp ON pp.variant_id = pv.id
                AND pp.is_primary AND pp.status = 'active' AND pp.deleted_at IS NULL
            WHERE pv.deleted_at IS NULL AND pv.status = 'active'
              AND (p.title ILIKE $1 OR pv.sku_code ILIKE $1 OR p.title ILIKE $2)
            LIMIT 20
            """,
            f"%{q}%",
            f"{q}%",
        )
    return {
        "products": [
            {
                "variant_id": str(r["variant_id"]),
                "sku": r["sku_code"],
                "name": r["title"],
                "price_cents": int(r["reference_price_cents"] or 0),
                "pricing_mode": r["pricing_mode"],
                "image_url": r["image_url"],
            }
            for r in rows
        ]
    }


# ──────────────────────────────────────────────────────────────────────────────
# 收银结算
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/settle")
async def settle_pos(
    request: Request,
    data: SettleRequest,
    principal: dict = Depends(require_staff_role(*_STAFF_ROLES)),
):
    """POS 收银结算：复用柜台结算建单（建单 + 直接扣库存），只收现金。

    本端点不再自写建单/扣库存：先把行项写成已计价 hardware_event，再调
    app.storefront._create_counter_order（与柜台结算同一函数），行为与柜台
    一致——建 customer_order（渠道 counter、状态 completed）+ 直接扣库存，
    有顾客时同一事务内累积分；另落一条现金结算记录（counter_settlement）。
    qr 已下掉：此前只收参数、无任何支付核验；聚合支付无供应商，等接了核验
    再放行。拣货称重逻辑（record-pick-weight 等）不在本端点，不动。
    """
    from asyncpg import UniqueViolationError

    from app.counter_closeout import compute_change_cents
    from app.storefront import _create_counter_order

    pool = _pool(request)
    actor = _actor(principal)

    if data.payment_method != "cash":
        raise HTTPException(
            422, f"only cash settlement is supported, got payment_method={data.payment_method}"
        )
    if not data.lines:
        raise HTTPException(400, "empty ticket")

    # 校验每行计价完整性
    for line in data.lines:
        require_weigh_line_pricing(
            batch_id=line.get("batch_id"),
            weight_grams=line.get("weight_grams"),
            unit_price_cents=line.get("unit_price_cents"),
            price_basis_grams=line.get("price_basis_grams"),
            amount_cents=line.get("amount_cents"),
        )

    # 计算合计
    total_cents = sum(int(line.get("amount_cents", 0)) for line in data.lines)
    if data.cash_received < total_cents:
        raise HTTPException(400, "cash received less than total")

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1. 创建 counter_ticket
                ticket_id = await conn.fetchval(
                    """
                    INSERT INTO counter_ticket (status, tote_id, customer_id, created_by, note)
                    VALUES ('pending_settlement', $1, $2, $3, $4)
                    RETURNING id
                    """,
                    None,  # tote_id
                    data.customer_id,
                    actor,
                    f"POS 结算: {len(data.lines)} 项",
                )
                ticket = await conn.fetchrow(
                    'SELECT * FROM "counter_ticket" WHERE id = $1', ticket_id
                )

                # 2. 写入 hardware_event（已计价行），供柜台建单函数消费
                for line in data.lines:
                    await conn.execute(
                        """
                        INSERT INTO hardware_event
                        (message_id, event_type, payload, session_id,
                         settlement_status, weight_grams, unit_price_cents,
                         price_basis_grams, amount_cents, source, ticket_id, batch_id, status)
                        VALUES ($1, 'weigh', $2, $3, 'unsettled', $4, $5, $6, $7,
                            'scale', $8, $9, 'processed')
                        """,
                        f"pos-{uuid.uuid4().hex}",
                        json.dumps(line),
                        f"pos_{uuid.uuid4().hex[:8]}",
                        line.get("weight_grams"),
                        line.get("unit_price_cents"),
                        line.get("price_basis_grams"),
                        line.get("amount_cents"),
                        ticket_id,
                        line.get("batch_id"),
                    )

                # 3. 复用柜台结算建单：建 customer_order（渠道 counter、状态
                # completed）+ 直接扣库存；有顾客时同一事务内累积分。
                try:
                    order = await _create_counter_order(conn, ticket=ticket, due_cents=total_cents)
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from exc
                if order is None:
                    raise HTTPException(409, "ticket has no priced lines")

                # 4. 现金结算记录（与柜台结算同一本账）
                change = compute_change_cents(total_cents, data.cash_received)
                await conn.execute(
                    'INSERT INTO "counter_settlement" '
                    "(ticket_id, cashier_id, method, amount_due_cents, "
                    "amount_received_cents, change_cents) VALUES ($1, $2, 'cash', $3, $4, $5)",
                    ticket_id,
                    actor,
                    total_cents,
                    data.cash_received,
                    change,
                )

                # 5. 称重行与单据落 settled
                await conn.execute(
                    "UPDATE \"hardware_event\" SET settlement_status = 'settled' "
                    "WHERE ticket_id = $1 AND event_type = 'weigh' AND status = 'processed'",
                    ticket_id,
                )
                await conn.execute(
                    "UPDATE counter_ticket SET status = 'settled', updated_at = NOW() "
                    "WHERE id = $1",
                    ticket_id,
                )

    except UniqueViolationError:
        raise HTTPException(409, "ticket already settled: duplicate settlement or order") from None

    return {
        "order_id": str(order["order_id"]),
        # customer_order 在仓内 schema 里没有 order_no 列（旧代码查的是不存在的列）；
        # 订单号机制另立项前返回 None，键保留以免调用方 KeyError。
        "order_no": None,
        "ticket_id": str(ticket_id),
        "total_cents": total_cents,
        "lines": [str(item["line_id"]) for item in order["lines"]],
    }


@router.get("/pick-orders")
async def list_pick_orders(
    request: Request,
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    """获取待拣货订单列表"""
    pool = _pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT o.id, o.order_no, o.status, o.created_at,
                   c.name as customer_name, c.phone as customer_phone
            FROM customer_order o
            JOIN "customer" c ON c.id = o.customer_id
            WHERE o.channel = 'online'
              AND o.status IN ('paid', 'confirmed', 'pending_pick')
              AND o.deleted_at IS NULL
            ORDER BY o.created_at ASC
            LIMIT 50
            """
        )
    orders = []
    for row in rows:
        # 获取订单行
        lines = await conn.fetch(
            """
            SELECT oli.id, oli.variant_id, p.title as name, pv.sku_code as sku,
                   oli.quantity, oli.pricing_mode, oli.unit_price_cents_per_jin as price_cents,
                   oli.estimated_weight_grams, oli.estimated_amount_cents,
                   oli.actual_weight_grams, oli.actual_amount_cents,
                   oli.weight_diff_cents, oli.settlement_status
            FROM order_line_item oli
            JOIN product_variant pv ON pv.id = oli.variant_id
            JOIN product p ON p.id = pv.product_id
            WHERE oli.order_id = $1 AND oli.deleted_at IS NULL
            ORDER BY oli.created_at
            """,
            row["id"],
        )
        orders.append(
            {
                "id": str(row["id"]),
                "order_no": row["order_no"],
                "customer_name": row["customer_name"],
                "customer_phone": row["customer_phone"],
                "status": row["status"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "lines": [
                    {
                        "id": str(line["id"]),
                        "variant_id": str(line["variant_id"]),
                        "name": line["name"],
                        "sku": line["sku"],
                        "quantity": line["quantity"],
                        "unit": "斤" if line["pricing_mode"] == "weight" else "件",
                        "price_cents": int(line["price_cents"] or 0),
                        "pricing_mode": line["pricing_mode"],
                        "estimated_weight_grams": line["estimated_weight_grams"],
                        "estimated_amount_cents": int(line["estimated_amount_cents"] or 0),
                        "actual_weight_grams": line["actual_weight_grams"],
                        "actual_amount_cents": line["actual_amount_cents"],
                        "diff_cents": line["weight_diff_cents"],
                        "settled": line["settlement_status"]
                        in ("auto_settled", "confirmed", "refunded"),
                    }
                    for line in lines
                ],
            }
        )
    return orders


# ──────────────────────────────────────────────────────────────────────────────
# 记录拣货实称重量
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/record-pick-weight")
async def record_pick_weight(
    request: Request,
    data: RecordPickWeightRequest,
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    """记录拣货行的实称重量，生成多退少补结算"""
    pool = _pool(request)
    actor = _actor(await require_staff_role(*_STAFF_ROLES)(request))

    # 校验重量
    validate_weight_grams(data.weight_grams, field="weight_grams")

    # 获取订单行信息
    async with pool.acquire() as conn:
        line = await conn.fetchrow(
            """
            SELECT oli.*, o.customer_id, o.status as order_status
            FROM order_line_item oli
            JOIN customer_order o ON o.id = oli.order_id
            WHERE oli.id = $1
            """,
            data.line_id,
        )
        if not line:
            raise HTTPException(404, "order line not found")
        if line["pricing_mode"] != "weight":
            raise HTTPException(400, "order line is not weight-priced")
        if line["order_status"] in ("cancelled", "refunded", "failed"):
            raise HTTPException(400, f"order is {line['order_status']}; cannot settle")

    # 调用结算服务
    try:
        result = await record_actual_weight(
            pool,
            order_line_item_id=data.line_id,
            weight_grams=data.weight_grams,
            tolerance_percent=10,
            created_by=actor,
            note=f"拣货实称: {data.weight_grams}g",
        )
        return result
    except WeighSettlementNotFound:
        raise HTTPException(404, "order line not found") from None
    except WeighSettlementError as e:
        raise HTTPException(400, str(e)) from e


# ──────────────────────────────────────────────────────────────────────────────
# 跳过拣货行 (人工处理)
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/skip-pick-line")
async def skip_pick_line(
    request: Request,
    data: SkipPickLineRequest,
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    """跳过拣货行，标记为人工处理"""
    pool = _pool(request)
    _ = _actor(await require_staff_role(*_STAFF_ROLES)(request))

    async with pool.acquire() as conn:
        async with conn.transaction():
            # 更新订单行状态
            await conn.execute(
                """
                UPDATE order_line_item
                SET settlement_status = 'disputed',
                    note = COALESCE(note, '') || $1
                WHERE id = $2
                """,
                f"\n[跳过] {data.reason}",
                data.line_id,
            )
            # 更新订单状态
            await conn.execute(
                """
                UPDATE customer_order
                SET weigh_status = 'disputed'
                WHERE id = (SELECT order_id FROM order_line_item WHERE id = $1)
                """,
                data.line_id,
            )
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────────────
# 识别模型管理：重新构建索引、添加确认帧
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/vision/rebuild-index")
async def rebuild_vision_index(
    request: Request,
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    """重新从商品图片构建向量索引"""
    pool = _pool(request)
    recognizer: ONNXRecognizer = await get_scale_recognizer(pool)
    if not recognizer:
        raise HTTPException(503, "recognizer not available")
    await recognizer._build_initial_index()
    recognizer._save_index()
    return {"ok": True, "entries": recognizer.index.index.ntotal if recognizer.index else 0}


@router.post("/vision/add-confirmed")
async def add_confirmed_frame(
    request: Request,
    file: UploadFile = File(...),
    variant_id: str = Form(...),
    source: str = Form("confirmed"),
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    """店员确认识别结果后，将帧加入向量库"""
    pool = _pool(request)
    raw = await file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "file too large")

    recognizer: ONNXRecognizer = await get_scale_recognizer(pool)
    if not recognizer:
        raise HTTPException(503, "recognizer not available")
    await recognizer.add_confirmed_frame(raw, variant_id, source)
    return {"ok": True}
