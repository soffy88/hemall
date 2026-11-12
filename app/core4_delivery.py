"""自营即时配送（core4 A）。

不接第三方配送。所有开关/门店坐标/范围/起送价/运费阶梯/营业时间/时段与
每时段单量上限都在 ``runtime_setting`` 里由后台改，**默认关闭**；关闭时
下单仍走原有 standard/express/next_day/pickup 逻辑，完全不变。

范围判定不依赖地理编码服务：顾客端提交浏览器定位坐标（lat/lon），服务端
按门店坐标计算半径，或按后台录入的多边形做点在多边形内判定。
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from .deps import assert_staff_role, get_current_customer, get_current_user

TZ = ZoneInfo("Asia/Shanghai")
SETTING_KEY = "delivery.local.v1"
logger = logging.getLogger("hemall.core4.delivery")

store_router = APIRouter(prefix="/store", tags=["core4-delivery"])
admin_router = APIRouter(prefix="/admin/delivery", tags=["core4-delivery"])
staff_router = APIRouter(prefix="/staff/delivery", tags=["core4-delivery"])

STAFF_ROLES = frozenset({"admin", "support", "staff"})
ADMIN_ROLES = frozenset({"admin", "support"})
TASK_STATUSES = ("pending", "claimed", "picked", "delivered", "cancelled")
ACTIVE_TASK_STATUSES = ("pending", "claimed", "picked", "delivered")


def default_delivery_config() -> dict[str, Any]:
    """占位默认值（全部待确认）。开关默认 false，不改变现有下单。"""
    return {
        "enabled": False,
        "store": {
            "name": "惠州仲恺惠环社区生鲜店",
            "lat": None,
            "lon": None,
            "address": "待确认：后台填写门店经纬度后才能开启自营配送",
        },
        "area": {
            "mode": "radius",
            "radius_km": 3.0,
            "polygon": [],
            "note": "待确认：默认半径 3 公里为占位值；多边形为 [lat, lon] 数组",
        },
        "min_order_cents": 2000,
        "fee_tiers": [
            {"min_subtotal_cents": 0, "fee_cents": 300},
            {"min_subtotal_cents": 5000, "fee_cents": 0},
        ],
        "business_hours": {"start": "08:00", "end": "21:00"},
        "slots": [
            {"start": "10:00", "end": "12:00", "capacity": 20},
            {"start": "14:00", "end": "18:00", "capacity": 30},
        ],
        "slot_advance_minutes": 30,
        "note": "占位值待确认：起送价/运费/时段单量上限均为默认占位",
    }


def _to_int(value: Any, fallback: int, *, low: int = 0, high: int = 10_000_000) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return fallback


def _to_float(value: Any, fallback: float | None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _clean_hhmm(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    try:
        datetime.strptime(text, "%H:%M")
        return text
    except ValueError:
        return fallback


def _clean_config(raw: Any) -> dict[str, Any]:
    """后台写入的 JSON 只覆盖合法字段；坏值回落到默认占位。"""
    base = default_delivery_config()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    if not isinstance(raw, dict):
        return base

    store = raw.get("store") if isinstance(raw.get("store"), dict) else {}
    area = raw.get("area") if isinstance(raw.get("area"), dict) else {}
    hours = raw.get("business_hours") if isinstance(raw.get("business_hours"), dict) else {}

    polygon: list[list[float]] = []
    for point in area.get("polygon") or []:
        if isinstance(point, (list, tuple)) and len(point) == 2:
            lat = _to_float(point[0], None)
            lon = _to_float(point[1], None)
            if lat is not None and lon is not None:
                polygon.append([lat, lon])

    tiers: list[dict[str, int]] = []
    for tier in raw.get("fee_tiers") or []:
        if isinstance(tier, dict):
            tiers.append(
                {
                    "min_subtotal_cents": _to_int(tier.get("min_subtotal_cents"), 0),
                    "fee_cents": _to_int(tier.get("fee_cents"), 0),
                }
            )
    tiers.sort(key=lambda item: item["min_subtotal_cents"])
    if not tiers:
        tiers = base["fee_tiers"]

    slots: list[dict[str, Any]] = []
    for slot in raw.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        start = _clean_hhmm(slot.get("start"), "")
        end = _clean_hhmm(slot.get("end"), "")
        if not start or not end or start >= end:
            continue
        slots.append(
            {
                "start": start,
                "end": end,
                "capacity": _to_int(slot.get("capacity"), 20, low=1, high=10_000),
            }
        )
    if not slots:
        slots = base["slots"]

    mode = str(area.get("mode") or "radius").strip().lower()
    if mode not in ("radius", "polygon"):
        mode = "radius"

    return {
        "enabled": bool(raw.get("enabled", False)),
        "store": {
            "name": str(store.get("name") or base["store"]["name"])[:80],
            "lat": _to_float(store.get("lat"), None),
            "lon": _to_float(store.get("lon"), None),
            "address": str(store.get("address") or base["store"]["address"])[:200],
        },
        "area": {
            "mode": mode,
            "radius_km": _to_float(area.get("radius_km"), 3.0) or 3.0,
            "polygon": polygon,
            "note": str(area.get("note") or base["area"]["note"])[:200],
        },
        "min_order_cents": _to_int(raw.get("min_order_cents"), base["min_order_cents"]),
        "fee_tiers": tiers,
        "business_hours": {
            "start": _clean_hhmm(hours.get("start"), base["business_hours"]["start"]),
            "end": _clean_hhmm(hours.get("end"), base["business_hours"]["end"]),
        },
        "slots": slots,
        "slot_advance_minutes": _to_int(
            raw.get("slot_advance_minutes"), base["slot_advance_minutes"], high=24 * 60
        ),
        "note": str(raw.get("note") or base["note"])[:200],
    }


def validate_for_enable(cfg: dict[str, Any]) -> list[str]:
    """开启前必须有可用门店坐标与范围；返回缺失项。"""
    problems: list[str] = []
    lat, lon = cfg["store"]["lat"], cfg["store"]["lon"]
    if lat is None or lon is None:
        problems.append("门店经纬度未填写")
    elif not (-90 <= lat <= 90 and -180 <= lon <= 180):
        problems.append("门店经纬度超出合法范围")
    area = cfg["area"]
    if area["mode"] == "radius":
        if not (0 < float(area["radius_km"] or 0) <= 50):
            problems.append("配送半径需在 0-50 公里之间")
    elif len(area["polygon"]) < 3:
        problems.append("多边形配送范围至少需要 3 个点")
    if not cfg["slots"]:
        problems.append("至少需要一个配送时段")
    if cfg["min_order_cents"] < 0:
        problems.append("起送价不能为负")
    return problems


async def load_delivery_config(pool: Any) -> dict[str, Any]:
    if pool is None:
        return default_delivery_config()
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT value FROM runtime_setting WHERE key = $1", SETTING_KEY
            )
    except Exception:  # noqa: BLE001 - 设置读不到不能阻断下单
        return default_delivery_config()
    return _clean_config(raw)


async def save_delivery_config(
    pool: Any, cfg: dict[str, Any], *, updated_by: str
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO runtime_setting (key, value, updated_by, updated_at)
            VALUES ($1, $2::jsonb, $3, NOW())
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                  updated_at = NOW()
            """,
            SETTING_KEY,
            json.dumps(cfg, ensure_ascii=False),
            updated_by,
        )
    return cfg


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def point_in_polygon(lat: float, lon: float, polygon: list[list[float]]) -> bool:
    """射线法；polygon 为 [[lat, lon], ...]，边界视为在内。"""
    if len(polygon) < 3:
        return False
    inside = False
    j = len(polygon) - 1
    for i, (pi_lat, pi_lon) in enumerate(polygon):
        pj_lat, pj_lon = polygon[j]
        intersects = (pi_lon > lon) != (pj_lon > lon)
        if intersects:
            slope = (pj_lat - pi_lat) * (lon - pi_lon) / (pj_lon - pi_lon) + pi_lat
            if lat < slope:
                inside = not inside
        j = i
    return inside


def range_check(cfg: dict[str, Any], lat: float | None, lon: float | None) -> dict[str, Any]:
    store = cfg["store"]
    store_lat, store_lon = store["lat"], store["lon"]
    result: dict[str, Any] = {
        "in_range": False,
        "distance_km": None,
        "reason": "",
    }
    if store_lat is None or store_lon is None:
        result["reason"] = "门店坐标未配置"
        return result
    if lat is None or lon is None:
        result["reason"] = "缺少收货定位"
        return result
    distance = round(haversine_km(store_lat, store_lon, lat, lon), 2)
    result["distance_km"] = distance
    area = cfg["area"]
    if area["mode"] == "polygon":
        result["in_range"] = point_in_polygon(lat, lon, area["polygon"])
        result["reason"] = "" if result["in_range"] else "收货地址不在配送多边形内"
    else:
        result["in_range"] = distance <= float(area["radius_km"])
        result["reason"] = (
            "" if result["in_range"] else f"收货地址超出 {area['radius_km']:g} 公里配送范围"
        )
    return result


def fee_for_subtotal(cfg: dict[str, Any], subtotal_cents: int) -> int:
    fee = 0
    for tier in cfg["fee_tiers"]:
        if subtotal_cents >= int(tier["min_subtotal_cents"]):
            fee = int(tier["fee_cents"])
        else:
            break
    return max(0, fee)


def _parse_hm(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def business_open(cfg: dict[str, Any], now: datetime) -> bool:
    start = _parse_hm(cfg["business_hours"]["start"])
    end = _parse_hm(cfg["business_hours"]["end"])
    local = now.timetz().replace(tzinfo=None)
    return start <= local < end


async def booked_counts(pool: Any, day: date) -> dict[str, int]:
    counts: dict[str, int] = {}
    if pool is None:
        return counts
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT slot_start, COUNT(*) AS n FROM core4_delivery_task "
            "WHERE slot_date = $1 AND status = ANY($2::text[]) GROUP BY slot_start",
            day,
            list(ACTIVE_TASK_STATUSES),
        )
    for row in rows:
        counts[str(row["slot_start"])] = int(row["n"])
    return counts


def available_slots(
    cfg: dict[str, Any], *, now: datetime, booked: dict[str, int]
) -> list[dict[str, Any]]:
    """今天剩余可下单时段；已过开始时间+提前量的时段不放。"""
    if not business_open(cfg, now):
        return []
    slots: list[dict[str, Any]] = []
    advance = int(cfg.get("slot_advance_minutes") or 0)
    for slot in cfg["slots"]:
        start = _parse_hm(slot["start"])
        start_dt = datetime.combine(now.date(), start, tzinfo=TZ)
        if start_dt <= now:
            continue
        if (start_dt - now).total_seconds() < advance * 60:
            continue
        used = int(booked.get(slot["start"], 0))
        capacity = int(slot["capacity"])
        if used >= capacity:
            continue
        slots.append(
            {
                "date": now.date().isoformat(),
                "start": slot["start"],
                "end": slot["end"],
                "capacity": capacity,
                "booked": used,
                "remaining": capacity - used,
                "label": f"{slot['start']}-{slot['end']}",
                "eta_text": f"{slot['end']} 前送达",
            }
        )
    return slots


def quote_delivery(
    cfg: dict[str, Any],
    *,
    lat: float | None,
    lon: float | None,
    subtotal_cents: int,
    slot_start: str | None,
    now: datetime | None = None,
    booked: dict[str, int] | None = None,
) -> dict[str, Any]:
    """服务端配送报价与资格校验；不抛异常，由调用方决定是否拒绝下单。"""
    now = now or datetime.now(TZ)
    booked = booked or {}
    if not cfg.get("enabled"):
        return {
            "enabled": False,
            "ok": False,
            "reason": "自营配送未开启",
            "fee_cents": 0,
            "distance_km": None,
            "in_range": False,
            "min_order_cents": int(cfg["min_order_cents"]),
            "subtotal_cents": int(subtotal_cents),
            "slot": None,
            "open_now": False,
        }
    check = range_check(cfg, lat, lon)
    open_now = business_open(cfg, now)
    slots = available_slots(cfg, now=now, booked=booked)
    chosen = None
    if slot_start:
        for slot in slots:
            if slot["start"] == slot_start:
                chosen = slot
                break
    min_order_cents = int(cfg["min_order_cents"])
    fee = fee_for_subtotal(cfg, int(subtotal_cents))
    reason = ""
    if not check["in_range"]:
        reason = check["reason"]
    elif not open_now:
        reason = "当前不在营业时间"
    elif int(subtotal_cents) < min_order_cents:
        reason = f"未达起送价 ¥{min_order_cents / 100:.2f}"
    elif not chosen:
        reason = "所选配送时段不可用"
    return {
        "enabled": True,
        "ok": not reason,
        "reason": reason,
        "fee_cents": fee,
        "distance_km": check["distance_km"],
        "in_range": bool(check["in_range"]),
        "min_order_cents": min_order_cents,
        "subtotal_cents": int(subtotal_cents),
        "open_now": open_now,
        "slot": chosen,
        "slots": slots,
        "store": {
            "name": cfg["store"]["name"],
            "address": cfg["store"]["address"],
        },
        "note": cfg.get("note") or "",
    }


# ── 后台设置 ──────────────────────────────────────────────────────────


def _admin(principal: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return assert_staff_role(principal, ADMIN_ROLES)


def _staff(principal: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return assert_staff_role(principal, STAFF_ROLES)


@admin_router.get("/settings")
async def get_settings_endpoint(request: Request, _=Depends(_admin)) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    cfg = await load_delivery_config(pool)
    return {"config": cfg, "problems": validate_for_enable(cfg)}


@admin_router.put("/settings")
async def put_settings_endpoint(
    request: Request, body: dict[str, Any], principal=Depends(_admin)
) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database not ready")
    cfg = _clean_config(body)
    if cfg["enabled"]:
        problems = validate_for_enable(cfg)
        if problems:
            raise HTTPException(400, "；".join(problems))
    saved = await save_delivery_config(
        pool, cfg, updated_by=str(principal.get("user_id") or principal.get("sub") or "")
    )
    return {"ok": True, "config": saved, "problems": validate_for_enable(saved)}


# ── 顾客端：范围/时段/运费 ────────────────────────────────────────────


@store_router.get("/delivery/options")
async def delivery_options(
    request: Request,
    lat: float | None = Query(None, ge=-90, le=90),
    lon: float | None = Query(None, ge=-180, le=180),
    subtotal_cents: int = Query(0, ge=0),
    slot_start: str | None = None,
) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    cfg = await load_delivery_config(pool)
    today = datetime.now(TZ).date()
    booked = await booked_counts(pool, today) if cfg.get("enabled") else {}
    return quote_delivery(
        cfg,
        lat=lat,
        lon=lon,
        subtotal_cents=subtotal_cents,
        slot_start=slot_start,
        booked=booked,
    )


@store_router.get("/orders/{order_id}/delivery")
async def customer_order_delivery(
    request: Request,
    order_id: str,
    principal: dict[str, Any] = Depends(get_current_customer),
) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database not ready")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT t.status, t.slot_date, t.slot_start, t.slot_end, t.distance_km,
                   t.fee_cents, t.proof_photo_url, t.claimed_at, t.picked_at,
                   t.delivered_at, o.customer_id
            FROM core4_delivery_task t
            JOIN customer_order o ON o.id = t.order_id
            WHERE t.order_id = $1
            """,
            order_id,
        )
    if row is None:
        return {"delivery": None}
    if str(row["customer_id"]) != str(principal["customer_id"]):
        raise HTTPException(403, "not your order")
    status_labels = {
        "pending": "待接单",
        "claimed": "骑手已接单",
        "picked": "已取货，配送中",
        "delivered": "已送达",
        "cancelled": "已取消",
    }
    return {
        "delivery": {
            "status": row["status"],
            "status_label": status_labels.get(row["status"], row["status"]),
            "slot_date": row["slot_date"].isoformat(),
            "slot": f"{row['slot_start']}-{row['slot_end']}",
            "distance_km": float(row["distance_km"]) if row["distance_km"] is not None else None,
            "fee_cents": int(row["fee_cents"] or 0),
            "proof_photo_url": row["proof_photo_url"],
            "claimed_at": row["claimed_at"].isoformat() if row["claimed_at"] else None,
            "picked_at": row["picked_at"].isoformat() if row["picked_at"] else None,
            "delivered_at": row["delivered_at"].isoformat() if row["delivered_at"] else None,
        }
    }


# ── 店员/骑手端 ───────────────────────────────────────────────────────


@staff_router.get("/tasks")
async def list_tasks(
    request: Request,
    status: str | None = Query(None),
    _=Depends(_staff),
) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database not ready")
    if status is not None and status not in TASK_STATUSES:
        raise HTTPException(400, "unknown status")
    params: list[Any] = []
    where = "WHERE 1=1"
    if status:
        params.append(status)
        where += f" AND t.status = ${len(params)}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT t.*, o.status AS order_status, o.grand_total_cents, o.currency,
                   o.shipping_address
            FROM core4_delivery_task t
            JOIN customer_order o ON o.id = t.order_id
            {where}
            ORDER BY (t.status IN ('pending','claimed','picked')) DESC,
                     t.slot_date, t.slot_start, t.created_at
            LIMIT 200
            """,
            *params,
        )
    tasks = []
    for row in rows:
        address = row["shipping_address"] or {}
        if isinstance(address, str):
            try:
                address = json.loads(address)
            except (TypeError, ValueError):
                address = {}
        tasks.append(
            {
                "id": str(row["id"]),
                "order_id": str(row["order_id"]),
                "status": row["status"],
                "slot": f"{row['slot_start']}-{row['slot_end']}",
                "slot_date": row["slot_date"].isoformat(),
                "distance_km": float(row["distance_km"])
                if row["distance_km"] is not None
                else None,
                "fee_cents": int(row["fee_cents"] or 0),
                "assigned_to": row["assigned_to"],
                "proof_photo_url": row["proof_photo_url"],
                "recipient_name": address.get("recipient_name") or "",
                "phone": address.get("phone") or "",
                "address": " ".join(
                    part
                    for part in [
                        address.get("city"),
                        address.get("address_line1"),
                        address.get("address_line2"),
                    ]
                    if part
                ),
                "note": row["note"],
                "order_status": row["order_status"],
                "grand_total_cents": int(row["grand_total_cents"] or 0),
                "currency": row["currency"],
                "created_at": row["created_at"].isoformat(),
                "claimed_at": row["claimed_at"].isoformat() if row["claimed_at"] else None,
                "picked_at": row["picked_at"].isoformat() if row["picked_at"] else None,
                "delivered_at": row["delivered_at"].isoformat() if row["delivered_at"] else None,
            }
        )
    return {"tasks": tasks}


async def _transition(
    pool: Any, task_id: str, *, from_status: tuple[str, ...], to_status: str, staff_id: str
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM core4_delivery_task WHERE id = $1 FOR UPDATE", task_id
            )
            if row is None:
                raise HTTPException(404, "task not found")
            if row["status"] not in from_status:
                raise HTTPException(
                    409, f"task status is {row['status']}, cannot move to {to_status}"
                )
            stamp = {
                "claimed": "claimed_at = NOW(), assigned_to = $2",
                "picked": "picked_at = NOW(), assigned_to = COALESCE(NULLIF(assigned_to, ''), $2)",
                "delivered": "delivered_at = NOW(), assigned_to = COALESCE(NULLIF(assigned_to, ''), $2)",
            }[to_status]
            updated = await conn.fetchrow(
                f"UPDATE core4_delivery_task SET status = $3, {stamp}, "
                "updated_at = NOW() WHERE id = $1 RETURNING *",
                task_id,
                staff_id,
                to_status,
            )
            await conn.execute(
                "UPDATE customer_order SET delivery_status = $1 WHERE id = $2",
                to_status,
                row["order_id"],
            )
    return {"ok": True, "status": updated["status"], "task_id": task_id}


@staff_router.post("/tasks/{task_id}/claim")
async def claim_task(request: Request, task_id: str, principal=Depends(_staff)) -> dict[str, Any]:
    return await _transition(
        request.app.state.pool,
        task_id,
        from_status=("pending",),
        to_status="claimed",
        staff_id=str(principal.get("user_id") or principal.get("sub") or ""),
    )


@staff_router.post("/tasks/{task_id}/pick")
async def pick_task(request: Request, task_id: str, principal=Depends(_staff)) -> dict[str, Any]:
    return await _transition(
        request.app.state.pool,
        task_id,
        from_status=("claimed",),
        to_status="picked",
        staff_id=str(principal.get("user_id") or principal.get("sub") or ""),
    )


@staff_router.post("/tasks/{task_id}/deliver")
async def deliver_task(
    request: Request,
    task_id: str,
    photo: UploadFile | None = File(None),
    note: str = Form(""),
    principal=Depends(_staff),
) -> dict[str, Any]:
    """送达：必须上传送达照片（复用 core3 的压缩/去 EXIF/内容寻址）。"""
    pool = request.app.state.pool
    if photo is None:
        raise HTTPException(400, "送达需要拍照")
    data = await photo.read()
    from .product_photo import process_image, store_image

    try:
        processed = process_image(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    settings = getattr(request.app.state, "config", None)
    if settings is None:
        raise HTTPException(503, "service not ready")
    url, _thumb = store_image(settings.output_root, processed)
    staff_id = str(principal.get("user_id") or principal.get("sub") or "")
    result = await _transition(
        pool,
        task_id,
        from_status=("picked",),
        to_status="delivered",
        staff_id=staff_id,
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE core4_delivery_task SET proof_photo_url = $1, note = $2, "
            "updated_at = NOW() WHERE id = $3",
            url,
            note[:500],
            task_id,
        )
    result["proof_photo_url"] = url
    return result


# ── 供 checkout / shipping-method 复用 ─────────────────────────────────


async def quote_for_cart(
    pool: Any,
    cfg: dict[str, Any],
    *,
    cart_id: str,
    lat: float | None,
    lon: float | None,
    slot_start: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        subtotal = await conn.fetchval(
            "SELECT COALESCE(subtotal_cents, 0) FROM cart WHERE id = $1 AND deleted_at IS NULL",
            cart_id,
        )
    if subtotal is None:
        raise HTTPException(404, "cart not found")
    today = (now or datetime.now(TZ)).date()
    booked = await booked_counts(pool, today)
    return quote_delivery(
        cfg,
        lat=lat,
        lon=lon,
        subtotal_cents=int(subtotal),
        slot_start=slot_start,
        now=now,
        booked=booked,
    )


async def create_delivery_task(
    conn: Any,
    *,
    order_id: str,
    quote: dict[str, Any],
    note: str = "",
) -> None:
    """下单事务内建任务；同一订单号幂等。"""
    slot = quote.get("slot") or {}
    if not slot:
        raise HTTPException(400, "缺少配送时段")
    day = datetime.fromisoformat(slot["date"]).date()
    # 同一时段串行计数，避免并发超卖。
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"core4-delivery:{slot['date']}:{slot['start']}",
    )
    used = await conn.fetchval(
        "SELECT COUNT(*) FROM core4_delivery_task WHERE slot_date = $1 AND slot_start = $2 "
        "AND status = ANY($3::text[])",
        day,
        slot["start"],
        list(ACTIVE_TASK_STATUSES),
    )
    capacity = int(quote.get("slot", {}).get("capacity") or 0)
    if int(used or 0) >= capacity:
        raise HTTPException(409, "该配送时段已满，请选择其他时段")
    await conn.execute(
        """
        INSERT INTO core4_delivery_task
            (order_id, slot_date, slot_start, slot_end, status, distance_km, fee_cents, note)
        VALUES ($1, $2, $3, $4, 'pending', $5, $6, $7)
        ON CONFLICT (order_id) DO NOTHING
        """,
        order_id,
        day,
        slot["start"],
        slot["end"],
        quote.get("distance_km"),
        int(quote.get("fee_cents") or 0),
        note[:500],
    )
    await conn.execute(
        "UPDATE customer_order SET delivery_status = 'pending', delivery_slot = $1 WHERE id = $2",
        f"{slot['date']} {slot['start']}-{slot['end']}",
        order_id,
    )
