"""服务端运费规则。

客户端只提交配送方式代码；价格、免运门槛和时窗由服务端配置或后台
``runtime_setting`` 覆盖计算。任何客户端传入的 ``price_cents`` 都不参与计算。
"""

from __future__ import annotations

import json
from typing import Any

from .config import Settings

SETTING_KEY = "fulfillment.shipping_rules.v1"

_METHOD_ALIASES = {
    "standard": "standard",
    "标准配送": "standard",
    "express": "express",
    "快速配送": "express",
    "next_day": "next_day",
    "次日达": "next_day",
    "pickup": "pickup",
    "到店自提": "pickup",
}


def default_shipping_rules(settings: Settings) -> dict[str, Any]:
    """环境变量提供的安全默认值。"""
    return {
        "service_area": settings.shipping_service_area,
        "delivery_windows": [
            part.strip() for part in settings.shipping_delivery_windows.split(",") if part.strip()
        ],
        "methods": {
            "standard": {
                "name": "标准配送",
                "price_cents": max(0, int(settings.shipping_standard_cents)),
                "free_threshold_cents": max(0, int(settings.shipping_free_threshold_cents)),
            },
            "express": {
                "name": "快速配送",
                "price_cents": max(0, int(settings.shipping_express_cents)),
                "free_threshold_cents": max(0, int(settings.shipping_free_threshold_cents)),
            },
            "next_day": {
                "name": "次日达",
                "price_cents": max(0, int(settings.shipping_next_day_cents)),
                "free_threshold_cents": max(0, int(settings.shipping_free_threshold_cents)),
            },
            "pickup": {
                "name": "到店自提",
                "price_cents": 0,
                "free_threshold_cents": 0,
            },
        },
    }


def _coerce_rules(raw: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    """后台 JSON 只覆盖合法字段，坏值回落到环境默认值。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    if not isinstance(raw, dict):
        return fallback

    methods = raw.get("methods")
    if not isinstance(methods, dict):
        methods = {}
    merged_methods: dict[str, dict[str, Any]] = {}
    for code, default in fallback["methods"].items():
        candidate = methods.get(code)
        if not isinstance(candidate, dict):
            candidate = {}
        try:
            price = max(0, int(candidate.get("price_cents", default["price_cents"])))
            threshold = max(
                0,
                int(candidate.get("free_threshold_cents", default["free_threshold_cents"])),
            )
        except (TypeError, ValueError):
            price = default["price_cents"]
            threshold = default["free_threshold_cents"]
        name = str(candidate.get("name") or default["name"]).strip()[:40]
        merged_methods[code] = {
            "name": name or default["name"],
            "price_cents": price,
            "free_threshold_cents": threshold,
        }

    windows = raw.get("delivery_windows", fallback["delivery_windows"])
    if not isinstance(windows, list):
        windows = fallback["delivery_windows"]
    clean_windows = [str(item).strip()[:40] for item in windows if str(item).strip()]
    service_area = str(raw.get("service_area", fallback["service_area"]) or "").strip()[:200]
    return {
        "service_area": service_area,
        "delivery_windows": clean_windows or fallback["delivery_windows"],
        "methods": merged_methods,
    }


async def load_shipping_rules(pool: Any, settings: Settings) -> dict[str, Any]:
    """读取后台覆盖；数据库不可用时退回环境配置，不阻断购物车。"""
    fallback = default_shipping_rules(settings)
    if pool is None:
        return fallback
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT value FROM runtime_setting WHERE key = $1",
                SETTING_KEY,
            )
    except Exception:
        return fallback
    return _coerce_rules(raw, fallback)


def quote_shipping(
    rules: dict[str, Any],
    *,
    method_name: str,
    subtotal_cents: int,
) -> dict[str, Any]:
    """按方式代码计算运费；未知方式明确拒绝。"""
    code = _METHOD_ALIASES.get(method_name.strip().lower()) or _METHOD_ALIASES.get(
        method_name.strip()
    )
    if code is None:
        raise ValueError(f"unknown shipping method: {method_name}")
    method = rules["methods"][code]
    threshold = int(method.get("free_threshold_cents") or 0)
    base_price = int(method.get("price_cents") or 0)
    free = threshold > 0 and int(subtotal_cents) >= threshold
    price = 0 if free else max(0, base_price)
    return {
        "code": code,
        "name": method["name"],
        "price_cents": price,
        "base_price_cents": base_price,
        "free_threshold_cents": threshold,
        "free_shipping_applied": free,
        "delivery_windows": list(rules.get("delivery_windows") or []),
        "service_area": str(rules.get("service_area") or ""),
        "quoted_by_server": True,
    }
