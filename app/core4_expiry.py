"""临期自动降价与损耗（core4 B）。

规则在 ``runtime_setting[pricing.expiry_rules.v1]`` 后台可改，默认：
- 剩余 ≤ 24 小时：7 折；
- 当天 20:00 之后：晚间清仓 5 折；
- 折扣价不得低于成本价；成本缺失/为 0 时用当前售价 50% 兜底（报告里标注）；
- 只对 active、未过期、有可用库存的批次生效。

折扣价写回 ``inventory_batch.retail_price_cents``（所有既有下单/购物车路径
无需改价逻辑），原价存 ``list_price_cents``，标签存 ``discount_label``。
不满足条件时恢复原价。已有的 2:00 reaper 在规则开启时不再自行改价（避免
两套价格互相覆盖），过期销毁逻辑不变。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .deps import assert_staff_role, get_current_user

TZ = ZoneInfo("Asia/Shanghai")
SETTING_KEY = "pricing.expiry_rules.v1"
logger = logging.getLogger("hemall.core4.expiry")

router = APIRouter(prefix="/admin/expiry", tags=["core4-expiry"])
ADMIN_ROLES = frozenset({"admin", "support"})

LOSS_LABELS = {
    "expired": "过期",
    "damaged": "破损",
    "tasting": "试吃",
    "other": "其他",
}


def default_expiry_rules() -> dict[str, Any]:
    """默认规则（占位值，后台可改）。"""
    return {
        "enabled": True,
        "rules": [
            {"id": "last_day", "label": "临期特价", "remaining_hours_lte": 24, "percent_off": 30},
            {
                "id": "evening",
                "label": "晚间清仓",
                "after_time": "20:00",
                "before_time": "23:59",
                "percent_off": 50,
            },
        ],
        "cost_fallback_ratio": 0.5,
        "note": "默认值：剩余24小时内7折；20:00后5折；不低于成本价。成本缺失时按售价50%兜底。",
    }


def _clean_rules(raw: Any) -> dict[str, Any]:
    base = default_expiry_rules()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    if not isinstance(raw, dict):
        return base
    rules: list[dict[str, Any]] = []
    for rule in raw.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        try:
            percent = int(rule.get("percent_off", 0))
        except (TypeError, ValueError):
            continue
        if not 1 <= percent <= 90:
            continue
        cleaned: dict[str, Any] = {
            "id": str(rule.get("id") or f"rule-{len(rules)}")[:40],
            "label": str(rule.get("label") or "临期特价")[:20],
            "percent_off": percent,
        }
        if rule.get("remaining_hours_lte") is not None:
            try:
                hours = float(rule["remaining_hours_lte"])
            except (TypeError, ValueError):
                continue
            if hours <= 0:
                continue
            cleaned["remaining_hours_lte"] = hours
        if rule.get("after_time"):
            cleaned["after_time"] = str(rule["after_time"])[:5]
        if rule.get("before_time"):
            cleaned["before_time"] = str(rule["before_time"])[:5]
        rules.append(cleaned)
    try:
        ratio = float(raw.get("cost_fallback_ratio", base["cost_fallback_ratio"]))
    except (TypeError, ValueError):
        ratio = base["cost_fallback_ratio"]
    return {
        "enabled": bool(raw.get("enabled", True)),
        "rules": rules or base["rules"],
        "cost_fallback_ratio": min(max(ratio, 0.1), 1.0),
        "note": str(raw.get("note") or base["note"])[:200],
    }


async def load_expiry_rules(pool: Any) -> dict[str, Any]:
    if pool is None:
        return default_expiry_rules()
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT value FROM runtime_setting WHERE key = $1", SETTING_KEY
            )
    except Exception:  # noqa: BLE001 - 规则读不到按默认，不阻断下单
        return default_expiry_rules()
    return _clean_rules(raw)


async def save_expiry_rules(pool: Any, rules: dict[str, Any], *, updated_by: str) -> dict[str, Any]:
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
            json.dumps(rules, ensure_ascii=False),
            updated_by,
        )
    return rules


def _parse_hhmm(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def _now_tz(now: datetime | None = None) -> datetime:
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=TZ)
    return now.astimezone(TZ)


def compute_expiry_offer(
    *,
    original_price_cents: int | None,
    cost_price_cents: int | None,
    expiration_time: datetime | None,
    now: datetime | None = None,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回某批次当前应展示/收取的临期价；不满足条件时 price=原价。"""
    rules = rules or default_expiry_rules()
    now = _now_tz(now)
    original = int(original_price_cents) if original_price_cents else 0
    result = {
        "original_price_cents": original,
        "price_cents": original,
        "is_expiring": False,
        "label": None,
        "percent_off": 0,
        "floor_cents": None,
        "floor_source": None,
    }
    if not rules.get("enabled") or original <= 0 or expiration_time is None:
        return result

    expiration = expiration_time
    if expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=UTC)
    if expiration <= now.astimezone(UTC):
        return result

    remaining_hours = (expiration - now.astimezone(UTC)).total_seconds() / 3600
    percent_off = 0
    label = None
    for rule in rules.get("rules") or []:
        threshold = rule.get("remaining_hours_lte")
        after = rule.get("after_time")
        before = rule.get("before_time") or "23:59"
        matched = False
        if threshold is not None and remaining_hours <= float(threshold):
            matched = True
        if after:
            local_t = now.timetz().replace(tzinfo=None)
            if _parse_hhmm(after) <= local_t <= _parse_hhmm(before):
                matched = True
        if matched and int(rule.get("percent_off") or 0) > percent_off:
            percent_off = int(rule["percent_off"])
            label = str(rule.get("label") or "临期特价")

    if percent_off <= 0:
        return result

    cost = int(cost_price_cents) if cost_price_cents else 0
    if cost > 0:
        floor_cents = cost
        floor_source = "cost"
    else:
        floor_cents = max(1, round(original * float(rules.get("cost_fallback_ratio") or 0.5)))
        floor_source = "retail_ratio"
    discounted = round(original * (100 - percent_off) / 100)
    price = max(discounted, floor_cents)
    # 兜底价本身不得高于原价；若成本高于原价，按原价卖（仍可能亏，但显示不乱）。
    price = min(price, original)
    return {
        "original_price_cents": original,
        "price_cents": price,
        "is_expiring": price < original,
        "label": label if price < original else None,
        "percent_off": percent_off,
        "floor_cents": floor_cents,
        "floor_source": floor_source,
    }


# ── 定时刷新：把规则落地为批次价格 ───────────────────────────────────


async def refresh_expiry_discounts(
    pool: Any,
    *,
    now: datetime | None = None,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按规则刷新 active 批次价格；返回标记/恢复数量。"""
    now = _now_tz(now)
    rules = rules or await load_expiry_rules(pool)
    applied = 0
    restored = 0
    skipped = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, retail_price_cents, list_price_cents, cost_price_cents,
                   expiration_time, discount_label
            FROM inventory_batch
            WHERE status = 'active' AND deleted_at IS NULL
              AND expiration_time IS NOT NULL
              AND (stock_qty - reserved_qty) > 0
            """
        )
        for row in rows:
            base_price = int(row["list_price_cents"] or row["retail_price_cents"] or 0)
            offer = compute_expiry_offer(
                original_price_cents=base_price,
                cost_price_cents=row["cost_price_cents"],
                expiration_time=row["expiration_time"],
                now=now,
                rules=rules,
            )
            if offer["is_expiring"]:
                new_price = int(offer["price_cents"])
                if (
                    int(row["retail_price_cents"] or 0) == new_price
                    and row["discount_label"] == offer["label"]
                    and row["list_price_cents"] is not None
                ):
                    skipped += 1
                    continue
                await conn.execute(
                    """
                    UPDATE inventory_batch
                    SET list_price_cents = COALESCE(list_price_cents, $1),
                        retail_price_cents = $2,
                        discount_label = $3,
                        discount_reason = $4,
                        discount_updated_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $5
                    """,
                    base_price,
                    new_price,
                    offer["label"],
                    "expiry_rule",
                    row["id"],
                )
                applied += 1
            elif row["list_price_cents"] is not None and row["discount_label"] is not None:
                # 之前由本规则打过折，现在条件不再满足 → 恢复原价。
                await conn.execute(
                    """
                    UPDATE inventory_batch
                    SET retail_price_cents = list_price_cents,
                        list_price_cents = NULL,
                        discount_label = NULL,
                        discount_reason = NULL,
                        discount_updated_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    row["id"],
                )
                restored += 1
    if applied or restored:
        logger.info("expiry discounts refreshed: applied=%s restored=%s", applied, restored)
    return {"applied": applied, "restored": restored, "skipped": skipped, "at": now.isoformat()}


def build_expiry_discount_engine(pool: Any) -> Any:
    """5 分钟一次的轻量定时器：只扫临期批次，不占磁盘、不发外部消息。"""
    from .ext.oservi_compat import CronSchedulerEngine

    async def expiry_tick(**_: Any) -> dict[str, Any]:
        return await refresh_expiry_discounts(pool)

    expiry_tick.__name__ = "expiry_tick"
    return CronSchedulerEngine(
        tasks=[expiry_tick],
        trigger={"on_interval": 300},
        config={"interval_seconds": 300},
        name="ext-expiry-discount",
    )


# ── 后台规则 ─────────────────────────────────────────────────────────


def _admin(principal: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return assert_staff_role(principal, ADMIN_ROLES)


class ExpiryRulesBody(BaseModel):
    enabled: bool = True
    rules: list[dict[str, Any]] = []
    cost_fallback_ratio: float = 0.5
    note: str = ""


@router.get("/settings")
async def get_expiry_rules(request: Request, _=Depends(_admin)) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    return {"config": await load_expiry_rules(pool), "loss_categories": LOSS_LABELS}


@router.put("/settings")
async def put_expiry_rules(
    request: Request, body: ExpiryRulesBody, principal=Depends(_admin)
) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database not ready")
    cleaned = _clean_rules(body.model_dump())
    saved = await save_expiry_rules(
        pool, cleaned, updated_by=str(principal.get("user_id") or principal.get("sub") or "")
    )
    refreshed = await refresh_expiry_discounts(pool, rules=saved)
    return {"ok": True, "config": saved, "refresh": refreshed}


@router.get("/preview")
async def preview_expiry(request: Request, limit: int = 50, _=Depends(_admin)) -> dict[str, Any]:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database not ready")
    rules = await load_expiry_rules(pool)
    now = datetime.now(TZ)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ib.id, ib.batch_no, ib.retail_price_cents, ib.list_price_cents,
                   ib.cost_price_cents, ib.expiration_time, ib.discount_label,
                   ib.stock_qty, ib.reserved_qty, p.title, pv.sku_code
            FROM inventory_batch ib
            JOIN product_variant pv ON pv.id = ib.variant_id
            JOIN product p ON p.id = pv.product_id
            WHERE ib.status = 'active' AND ib.deleted_at IS NULL
              AND ib.expiration_time IS NOT NULL
            ORDER BY ib.expiration_time ASC
            LIMIT $1
            """,
            max(1, min(int(limit), 200)),
        )
    items = []
    for row in rows:
        base = int(row["list_price_cents"] or row["retail_price_cents"] or 0)
        offer = compute_expiry_offer(
            original_price_cents=base,
            cost_price_cents=row["cost_price_cents"],
            expiration_time=row["expiration_time"],
            now=now,
            rules=rules,
        )
        items.append(
            {
                "batch_id": str(row["id"]),
                "batch_no": row["batch_no"],
                "title": row["title"],
                "sku_code": row["sku_code"],
                "stock_qty": int(row["stock_qty"] or 0),
                "available_qty": int(row["stock_qty"] or 0) - int(row["reserved_qty"] or 0),
                "expiration_time": row["expiration_time"].isoformat(),
                "current_price_cents": int(row["retail_price_cents"] or 0),
                "floor_cents": offer["floor_cents"],
                "floor_source": offer["floor_source"],
                **offer,
            }
        )
    return {"now": now.isoformat(), "rules": rules, "items": items}
