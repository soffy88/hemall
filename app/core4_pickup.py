"""自提点配置与取货码管理。

功能：
- 后台配置自提点（名称、地址、坐标、电话、营业时间、时段、容量）
- 顾客端查询自提点列表与可用时段
- 下单时选择自提点与时段，生成取货码（6位数字+二维码）
- 门店端核销取货码（已有 store_ops.verify_pickup 复用）
"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .deps import assert_staff_role, get_current_user

TZ = ZoneInfo("Asia/Shanghai")
SETTING_KEY = "pickup.settings.v1"

router = APIRouter(prefix="/store/pickup", tags=["pickup"])
admin_router = APIRouter(prefix="/admin/pickup", tags=["admin-pickup"])

STAFF_ROLES = frozenset({"admin", "support", "staff"})
ADMIN_ROLES = frozenset({"admin", "support"})


def default_pickup_settings() -> dict[str, Any]:
    """默认自提配置（占位值，需后台确认）。"""
    return {
        "points": [
            {
                "id": "default",
                "name": "惠州仲恺惠环社区生鲜店",
                "address": "广东省惠州市仲恺高新区惠环路XX号",
                "phone": "0752-XXXXXXX",
                "latitude": 23.0996,
                "longitude": 114.4063,
                "business_hours": {"start": "08:00", "end": "21:00"},
                "time_slots": [
                    {"start": "10:00", "end": "12:00", "capacity": 30},
                    {"start": "14:00", "end": "17:00", "capacity": 40},
                    {"start": "17:00", "end": "20:00", "capacity": 30},
                ],
                "is_active": True,
            }
        ],
        "default_point_id": "default",
        "note": "占位配置，请在后台确认自提点信息",
    }


def _clean_config(raw: Any) -> dict[str, Any]:
    """清洗后台写入的配置，只保留合法字段。"""
    base = default_pickup_settings()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    if not isinstance(raw, dict):
        return base

    # 合并 points
    points = raw.get("points")
    if isinstance(points, list):
        cleaned_points = []
        for p in points:
            if not isinstance(p, dict):
                continue
            cleaned_points.append(
                {
                    "id": str(p.get("id", "")) or f"point_{secrets.token_hex(4)}",
                    "name": str(p.get("name", "")).strip() or "未命名自提点",
                    "address": str(p.get("address", "")).strip(),
                    "phone": str(p.get("phone", "")).strip(),
                    "latitude": float(p.get("latitude", 0))
                    if p.get("latitude") is not None
                    else 0.0,
                    "longitude": (
                        float(p.get("longitude", 0)) if p.get("longitude") is not None else 0.0
                    ),
                    "business_hours": {
                        "start": str(p.get("business_hours", {}).get("start", "08:00")).strip(),
                        "end": str(p.get("business_hours", {}).get("end", "21:00")).strip(),
                    },
                    "time_slots": [
                        {
                            "start": str(s.get("start", "")).strip(),
                            "end": str(s.get("end", "")).strip(),
                            "capacity": max(1, int(s.get("capacity", 20))),
                        }
                        for s in p.get("time_slots", [])
                        if isinstance(s, dict) and s.get("start") and s.get("end")
                    ],
                    "is_active": bool(p.get("is_active", True)),
                }
            )
        base["points"] = cleaned_points

    # 合并 default_point_id
    if raw.get("default_point_id"):
        base["default_point_id"] = str(raw["default_point_id"])

    # 合并 note
    if raw.get("note"):
        base["note"] = str(raw["note"])

    return base


async def load_pickup_settings(pool: Any) -> dict[str, Any]:
    if pool is None:
        return default_pickup_settings()
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT value FROM runtime_setting WHERE key = $1", SETTING_KEY
            )
    except Exception:
        return default_pickup_settings()
    return _clean_config(raw)


async def save_pickup_settings(
    pool: Any, cfg: dict[str, Any], *, updated_by: str
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO runtime_setting (key, value, updated_by, updated_at)
            VALUES ($1, $2::jsonb, $3, NOW())
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = NOW()
            """,
            SETTING_KEY,
            json.dumps(cfg, ensure_ascii=False),
            updated_by,
        )
    return cfg


def generate_pickup_code() -> str:
    """生成 6 位数字取货码。"""
    return "".join(str(secrets.randbelow(10)) for _ in range(6))


def generate_qr_data(order_id: str, pickup_code: str) -> str:
    """生成二维码数据：JSON 字符串，含 order_id 和 pickup_code。"""
    import json

    return json.dumps({"order_id": order_id, "pickup_code": pickup_code}, ensure_ascii=False)


async def _get_booked_counts(pool: Any, point_id: str, day: date) -> dict[str, int]:
    """获取某自提点某天各时段已预约数。"""
    counts: dict[str, int] = {}
    if pool is None:
        return counts
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pickup_time_slot_start, COUNT(*) AS n
            FROM customer_order
            WHERE pickup_point_id = $1
              AND pickup_date = $2
              AND status NOT IN ('cancelled', 'refunded')
              AND pickup_time_slot_start IS NOT NULL
            GROUP BY pickup_time_slot_start
            """,
            point_id,
            day,
        )
    for row in rows:
        counts[str(row["pickup_time_slot_start"])] = int(row["n"])
    return counts


def _parse_hm(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def available_pickup_slots(
    point: dict[str, Any],
    *,
    now: datetime,
    booked: dict[str, int],
) -> list[dict[str, Any]]:
    """计算可用自提时段。"""
    if not point.get("is_active"):
        return []

    # 检查营业时间
    start = _parse_hm(point["business_hours"]["start"])
    end = _parse_hm(point["business_hours"]["end"])
    local = now.timetz().replace(tzinfo=None)
    if not (start <= local < end):
        return []

    slots: list[dict[str, Any]] = []
    for slot in point.get("time_slots", []):
        slot_start = _parse_hm(slot["start"])
        slot_start_dt = datetime.combine(now.date(), slot_start, tzinfo=TZ)
        if slot_start_dt <= now:
            continue
        used = booked.get(slot["start"], 0)
        capacity = int(slot.get("capacity", 20))
        if used >= capacity:
            continue
        slots.append(
            {
                "id": f"{point['id']}_{slot['start']}",
                "start_time": slot["start"],
                "end_time": slot["end"],
                "capacity": capacity,
                "booked": used,
                "remaining": capacity - used,
                "is_available": True,
                "label": f"{slot['start']}-{slot['end']}",
            }
        )
    return slots


# ── 后台设置 ──────────────────────────────────────────────────────────


def _admin(principal: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return assert_staff_role(principal, ADMIN_ROLES)


def _staff(principal: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return assert_staff_role(principal, STAFF_ROLES)


@admin_router.get("/settings")
async def get_settings_endpoint(request: Request, _=Depends(_admin)) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    cfg = await load_pickup_settings(pool)
    return {"config": cfg}


@admin_router.put("/settings")
async def put_settings_endpoint(
    request: Request, body: dict[str, Any], principal=Depends(_admin)
) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database not ready")
    cfg = _clean_config(body)
    saved = await save_pickup_settings(
        pool, cfg, updated_by=str(principal.get("user_id") or principal.get("sub") or "")
    )
    return {"ok": True, "config": saved}


# ── 顾客端：自提点列表、时段 ──────────────────────────────────────────


@router.get("/settings")
async def customer_get_settings(request: Request) -> dict[str, Any]:
    """获取自提点配置（含所有自提点及其时段模板）。"""
    pool = getattr(request.app.state, "pool", None)
    cfg = await load_pickup_settings(pool)
    return {"points": cfg["points"], "default_point_id": cfg.get("default_point_id")}


@router.get("/points")
async def get_pickup_points(request: Request) -> dict[str, Any]:
    """获取启用的自提点列表。"""
    pool = getattr(request.app.state, "pool", None)
    cfg = await load_pickup_settings(pool)
    points = [p for p in cfg["points"] if p["is_active"]]
    return {"points": points}


@router.get("/points/{point_id}/slots")
async def get_pickup_slots(
    request: Request,
    point_id: str,
    date: str | None = Query(None, description="日期 YYYY-MM-DD，默认今天"),
) -> dict[str, Any]:
    """获取指定自提点在指定日期的可用时段（含已预约数）。"""
    pool = getattr(request.app.state, "pool", None)
    cfg = await load_pickup_settings(pool)

    point = next((p for p in cfg["points"] if p["id"] == point_id), None)
    if not point:
        raise HTTPException(404, "自提点不存在")
    if not point["is_active"]:
        raise HTTPException(400, "自提点已停用")

    target_date = datetime.fromisoformat(date).date() if date else datetime.now(TZ).date()
    now = (
        datetime.now(TZ)
        if target_date == datetime.now(TZ).date()
        else datetime.combine(target_date, time(0, 0), tzinfo=TZ)
    )

    booked = await _get_booked_counts(pool, point_id, target_date)
    slots = available_pickup_slots(point, now=now, booked=booked)

    return {
        "point": {
            "id": point["id"],
            "name": point["name"],
            "address": point["address"],
            "phone": point["phone"],
            "latitude": point["latitude"],
            "longitude": point["longitude"],
            "business_hours": point["business_hours"],
        },
        "date": target_date.isoformat(),
        "slots": slots,
    }
