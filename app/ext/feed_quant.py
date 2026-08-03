"""feed_quant — BFF v9.0 做市量化基座 (客户端展示与极速交互)。

纯内存算子，无 IO 依赖，可单测。职责：
  1. tag_type 派生: clearance(暴降大卡) / fresh(溯源大卡) / standard(基础网格)
  2. 直降比例计算 (划线价 vs 现价)
  3. 库存恐慌阈值 (5 份以内触发 🚨)
  4. 实况流速归一化 (observed_velocity, 供前端动效渲染)

设计原则: 前端绝不拉取多余富文本详情——后端在 BFF 层把"做市属性"
(降价幅度/库存恐慌/流速) 全部量化为标量字段，前端根据 tag_type 在
流和网格之间自动切换渲染引擎。

Phase 9: 本模块是新文件，不改动已封板的 oskill.py 核心路径。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

#: 暴降大卡判定阈值: 现价比基准价直降 ≥ 40% 视为清仓暴降。
CLEARANCE_DISCOUNT_RATE = 0.40
#: 临期清仓提前量: 距过期 ≤ 12 小时视为清仓 (即使降价幅度不够)。
CLEARANCE_EXPIRY_HOURS = 12.0
#: 溯源大卡: 批次入库 ≤ 48 小时且降幅 < 20% 视为新品溯源。
FRESH_MAX_AGE_HOURS = 48.0
FRESH_MAX_DISCOUNT_RATE = 0.20
#: 库存恐慌阈值 (份)。
PANIC_STOCK_QTY = 5


def compute_discount_rate(retail_price_cents: int, benchmark_price_cents: int) -> int:
    """直降比例 (0-100 整数百分比)。基准价缺失或 ≤ 现价时返回 0。

    >>> compute_discount_rate(1990, 3590)
    44
    >>> compute_discount_rate(3590, 1990)
    0
    """
    if benchmark_price_cents <= 0 or benchmark_price_cents <= retail_price_cents:
        return 0
    return round((1 - retail_price_cents / benchmark_price_cents) * 100)


def is_panic_stock(stock_qty: int) -> bool:
    """库存恐慌阈值: 5 份以内触发前端 🚨 极度危险渲染。"""
    return stock_qty <= PANIC_STOCK_QTY


def classify_feed_tag(
    retail_price_cents: int,
    benchmark_price_cents: int,
    batch_created_at: datetime | None,
    batch_expiration_time: datetime | None,
    now: datetime | None = None,
) -> str:
    """派生 tag_type: clearance(暴降大卡) / fresh(溯源大卡) / standard(基础网格)。

    优先级: clearance > fresh > standard。

    Args:
        retail_price_cents: 现价 (分)。
        benchmark_price_cents: 爬虫基准价 (分)，0 表示缺失。
        batch_created_at: 批次入库时间。
        batch_expiration_time: 批次过期时间。
        now: 当前时间 (可注入便于测试)，默认 datetime.now(UTC)。

    Returns:
        "clearance" | "fresh" | "standard"
    """
    if now is None:
        from datetime import UTC

        now = datetime.now(UTC)

    discount_rate = compute_discount_rate(retail_price_cents, benchmark_price_cents)

    # 清仓暴降: 降幅达标 或 临期 (12h 内过期)
    if discount_rate >= round(CLEARANCE_DISCOUNT_RATE * 100):
        return "clearance"
    if batch_expiration_time is not None:
        hours_to_expiry = (batch_expiration_time - now).total_seconds() / 3600
        if hours_to_expiry <= CLEARANCE_EXPIRY_HOURS:
            return "clearance"

    # 新品溯源: 入库 ≤ 48h 且降幅 < 20%
    if batch_created_at is not None:
        age_hours = (now - batch_created_at).total_seconds() / 3600
        if 0 <= age_hours <= FRESH_MAX_AGE_HOURS and discount_rate < round(
            FRESH_MAX_DISCOUNT_RATE * 100
        ):
            return "fresh"

    return "standard"


def normalize_velocity(raw_count: int, hours: float = 1.0) -> float:
    """流速归一化: 过去 hours 小时的成交笔数 → 每小时流速。

    供前端 observed_velocity 字段做动效辅助 (数值大小只做相对比较，
    前端不依赖绝对值)。
    """
    if hours <= 0:
        return 0.0
    return round(raw_count / hours, 2)


def build_feed_item(row: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """把 nearby-feed 的批次行量化为 BFF v9.0 feed_item。

    Args:
        row: 后端查询出的批次行 (含 batch_id/variant_id/product_id/title/
            sku_code/retail_price_cents/benchmark_price_cents/stock_qty/
            expiration_time/created_at/video_url/location_name/affinity/
            boosted/observed_velocity)。
        now: 可注入当前时间 (测试)。

    Returns:
        feed_item dict (BFF v9.0 扁平契约)。
    """
    if now is None:
        from datetime import UTC

        now = datetime.now(UTC)

    retail = int(row.get("retail_price_cents") or 0)
    benchmark = int(row.get("benchmark_price_cents") or 0)
    stock = int(row.get("stock_qty") or 0)
    velocity = float(row.get("observed_velocity") or 0.0)
    created_at = row.get("created_at")
    expiration = row.get("expiration_time")
    # asyncpg 返回的 datetime 不带 tzinfo 时补 UTC (防御)
    if isinstance(created_at, datetime) and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=now.tzinfo)
    if isinstance(expiration, datetime) and expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=now.tzinfo)

    return {
        "batch_id": str(row.get("batch_id") or row.get("id") or ""),
        "sku_name": row.get("title") or row.get("sku_code") or "未命名商品",
        "tag_type": classify_feed_tag(
            retail,
            benchmark,
            created_at,
            expiration,
            now,
        ),
        "retail_price": retail,
        "benchmark_price": benchmark,
        "stock_qty": stock,
        "observed_velocity": velocity,
        "media_url": row.get("video_url") or None,
        "affinity_boosted": bool(row.get("boosted") or False),
    }
