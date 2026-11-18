"""app.scale_router — 秤/称重/识别/收银/拣货 相关 API 端点。

包含：
- 秤设备 CRUD
- 串口数据解析
- 摄像头识别 (POS 收银 / 拣货称重)
- 收银结算
- 拣货单管理与实称记录
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from PIL import Image

from .deps import require_staff_role
from .scale import (
    ScaleService,
    DeviceSession,
    parse_scale_line,
    get_stable_weight,
    ScaleReading,
    StableWeight,
    DEFAULT_STABLE_COUNT,
    DEFAULT_STABLE_THRESHOLD_G,
    MAX_WEIGHT_GRAMS,
)
from .scale_vision import get_scale_recognizer, ONNXRecognizer
from .weigh_price import weigh_amount_cents, diff_policy, settlement_status_for, validate_weight_grams
from .weigh_settle import record_actual_weight, WeighSettlementError, WeighSettlementNotFound
from .counter_settlement_order import (
    weigh_stock_units,
    require_weigh_line_pricing,
    ORDER_CHANNEL_COUNTER,
    COUNTER_ORDER_STATUS,
)
from .product_photo import add_photo, _output_root, _actor

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
    payment_method: str = "cash"  # cash | qr
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
    request: Request,
    service: ScaleService = Depends(_scale_service),
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    actor = _actor(await require_staff_role(*_STAFF_ROLES)(request))
    return await service.create_device(data.model_dump(), actor)


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
        raise HTTPException(400, f"invalid image: {e}")

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
                LEFT JOIN inventory_batch ib ON ib.variant_id = pv.id AND ib.deleted_at IS NULL AND ib.status = 'active'
                LEFT JOIN product_photo pp ON pp.variant_id = pv.id AND pp.is_primary AND pp.status = 'active' AND pp.deleted_at IS NULL
                WHERE pv.id = $1 AND pv.deleted_at IS NULL
                """,
                c["variant_id"],
            )
            if row:
                enriched.append({
                    "variant_id": str(row["id"]),
                    "score": c["score"],
                    "name": row["title"],
                    "sku": row["sku_code"],
                    "price_cents": int(row["reference_price_cents"] or 0),
                    "pricing_mode": row["pricing_mode"],
                    "image_url": row["image_url"],
                })
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
        raise HTTPException(400, f"invalid image: {e}")

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
                enriched.append({
                    "variant_id": str(row["id"]),
                    "score": c["score"],
                    "name": row["title"],
                    "sku": row["sku_code"],
                    "matched": is_match,
                })
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
            SELECT pv.id as variant_id, pv.sku_code, p.title, pv.reference_price_cents, pv.pricing_mode,
                   COALESCE(ib.shelf_image_url, pp.url) as image_url
            FROM product_variant pv
            JOIN product p ON p.id = pv.product_id
            LEFT JOIN inventory_batch ib ON ib.variant_id = pv.id AND ib.deleted_at IS NULL AND ib.status = 'active'
            LEFT JOIN product_photo pp ON pp.variant_id = pv.id AND pp.is_primary AND pp.status = 'active' AND pp.deleted_at IS NULL
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
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    """POS 收银结算：建单、扣库存、生成线下订单"""
    pool = _pool(request)
    actor = _actor(await require_staff_role(*_STAFF_ROLES)(request))

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
    if data.payment_method == "cash" and data.cash_received < total_cents:
        raise HTTPException(400, "cash received less than total")

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

            # 2. 写入 hardware_event (称重行) 并扣库存
            order_line_ids = []
            for line in data.lines:
                # 硬件事件
                hw_id = await conn.fetchval(
                    """
                    INSERT INTO hardware_event
                    (event_type, device_id, payload, session_id, settlement_status,
                     weight_grams, unit_price_cents, price_basis_grams, amount_cents,
                     source, ticket_id, created_by)
                    VALUES ('weigh', $1, $2, $3, 'settled', $4, $5, $6, $7, 'scale', $8, $9)
                    RETURNING id
                    """,
                    line.get("device_id") or "pos",
                    json.dumps(line),
                    f"pos_{uuid.uuid4().hex[:8]}",
                    line.get("weight_grams"),
                    line.get("unit_price_cents"),
                    line.get("price_basis_grams"),
                    line.get("amount_cents"),
                    ticket_id,
                    actor,
                )

                # 扣库存 (按份)
                if line.get("batch_id") and line.get("weight_grams"):
                    weight_g = int(line["weight_grams"])
                    basis_g = int(line.get("price_basis_grams", 500))
                    stock_units = weigh_stock_units(weight_g, basis_g)
                    # 预留并标记 consumed
                    res_id = await conn.fetchval(
                        """
                        INSERT INTO inventory_reservation
                        (batch_id, qty, status, created_by)
                        VALUES ($1, $2, 'consumed', $3)
                        RETURNING id
                        """,
                        line["batch_id"],
                        stock_units,
                        actor,
                    )
                    # 扣批次库存
                    await conn.execute(
                        """
                        UPDATE inventory_batch
                        SET stock_qty = stock_qty - $1, updated_at = NOW()
                        WHERE id = $2 AND stock_qty >= $1
                        """,
                        stock_units,
                        line["batch_id"],
                    )
                    # 记录 stock_movement
                    await conn.execute(
                        """
                        INSERT INTO stock_movement
                        (batch_id, movement_type, qty, reference_id, reference_type, created_by)
                        VALUES ($1, 'shipment', -$2, $3, 'counter_ticket', $4)
                        """,
                        line["batch_id"],
                        stock_units,
                        ticket_id,
                        actor,
                    )

            # 3. 创建 customer_order (channel=counter, status=completed)
            order_id = await conn.fetchval(
                """
                INSERT INTO customer_order
                (customer_id, channel, status, discount_cents, discount_kind,
                 voucher_redeemed_cents, counter_ticket_id, created_by)
                VALUES ($1, $2, $3, 0, 'none', 0, $4, $5)
                RETURNING id
                """,
                data.customer_id,
                ORDER_CHANNEL_COUNTER,
                COUNTER_ORDER_STATUS,
                ticket_id,
                actor,
            )

            # 4. 创建 order_line_item
            for line in data.lines:
                pricing_mode = line.get("pricing_mode", "piece")
                weight_g = line.get("weight_grams")
                basis_g = line.get("price_basis_grams", 500)
                unit_price = int(line.get("unit_price_cents", 0))
                amount = int(line.get("amount_cents", 0))

                if pricing_mode == "weight":
                    stock_units = weigh_stock_units(weight_g, basis_g)
                    oli_id = await conn.fetchval(
                        """
                        INSERT INTO order_line_item
                        (order_id, batch_id, variant_id, quantity, unit_price_cents,
                         line_total_cents, pricing_mode, unit_price_cents_per_jin,
                         price_basis_grams, estimated_weight_grams, estimated_amount_cents,
                         actual_weight_grams, actual_amount_cents, weight_diff_cents,
                         settlement_status, created_by)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 0, 'not_required', $14)
                        RETURNING id
                        """,
                        order_id,
                        line.get("batch_id"),
                        line.get("variant_id"),
                        stock_units,
                        unit_price,
                        amount,
                        "weight",
                        unit_price,
                        basis_g,
                        weight_g,
                        amount,
                        weight_g,
                        amount,
                        actor,
                    )
                else:
                    oli_id = await conn.fetchval(
                        """
                        INSERT INTO order_line_item
                        (order_id, batch_id, variant_id, quantity, unit_price_cents,
                         line_total_cents, pricing_mode, settlement_status, created_by)
                        VALUES ($1, $2, $3, $4, $5, $6, 'piece', 'not_required', $7)
                        RETURNING id
                        """,
                        order_id,
                        line.get("batch_id"),
                        line.get("variant_id"),
                        1,
                        unit_price,
                        amount,
                        actor,
                    )
                order_line_ids.append(str(oli_id))

            # 5. 更新 ticket 状态
            await conn.execute(
                "UPDATE counter_ticket SET status = 'settled', updated_at = NOW() WHERE id = $1",
                ticket_id,
            )

            # 6. 生成订单号
            order_no = await conn.fetchval(
                "SELECT order_no FROM customer_order WHERE id = $1", order_id
            )

    return {
        "order_id": str(order_id),
        "order_no": order_no,
        "ticket_id": str(ticket_id),
        "total_cents": total_cents,
        "lines": order_line_ids,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 拣货单列表
# ──────────────────────────────────────────────────────────────────────────────

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
        orders.append({
            "id": str(row["id"]),
            "order_no": row["order_no"],
            "customer_name": row["customer_name"],
            "customer_phone": row["customer_phone"],
            "status": row["status"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "lines": [
                {
                    "id": str(l["id"]),
                    "variant_id": str(l["variant_id"]),
                    "name": l["name"],
                    "sku": l["sku"],
                    "quantity": l["quantity"],
                    "unit": "斤" if l["pricing_mode"] == "weight" else "件",
                    "price_cents": int(l["price_cents"] or 0),
                    "pricing_mode": l["pricing_mode"],
                    "estimated_weight_grams": l["estimated_weight_grams"],
                    "estimated_amount_cents": int(l["estimated_amount_cents"] or 0),
                    "actual_weight_grams": l["actual_weight_grams"],
                    "actual_amount_cents": l["actual_amount_cents"],
                    "diff_cents": l["weight_diff_cents"],
                    "settled": l["settlement_status"] in ("auto_settled", "confirmed", "refunded"),
                }
                for l in lines
            ],
        })
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
        raise HTTPException(404, "order line not found")
    except WeighSettlementError as e:
        raise HTTPException(400, str(e))


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
    actor = _actor(await require_staff_role(*_STAFF_ROLES)(request))

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