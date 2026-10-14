"""hemall ClickHouse 分析平台 — 实时 OLAP 查询、销售分析、用户行为分析。

Phase 3 Priority 3: 为业务团队提供实时数据分析和可视化能力。

核心功能:
    - 销售实时分析 (GMV/订单量/客单价趋势)
    - 商品销售排行 (日/周/月)
    - 用户行为漏斗 (浏览→加购→购买)
    - 用户留存分析 (D1/D7/D30)
    - 热力图数据
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger("hemall.analytics")


# ── 分析维度 ──────────────────────────────────────────────────────


class TimeGranularity(Enum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class MetricType(Enum):
    GMV = "gmv"  # 总交易额
    ORDER_COUNT = "order_count"  # 订单数
    AOV = "aov"  # 客单价
    USER_COUNT = "user_count"  # 用户数
    NEW_USER = "new_user"  # 新用户数
    CONVERSION_RATE = "conv_rate"  # 转化率
    REFUND_RATE = "refund_rate"  # 退款率


# ── 数据模型 ──────────────────────────────────────────────────────


class SalesSummary(BaseModel):
    """销售总览。"""

    date: str
    gmv_cents: int = 0
    order_count: int = 0
    aov_cents: int = 0
    user_count: int = 0
    new_user_count: int = 0
    refund_rate: float = 0.0


class ProductSalesRanking(BaseModel):
    """商品销售排行。"""

    product_id: str
    product_name: str
    sold_count: int
    gmv_cents: int
    avg_price_cents: int
    rank: int


class FunnelData(BaseModel):
    """用户行为漏斗数据。"""

    stage: str
    user_count: int
    conversion_rate: float


class UserRetention(BaseModel):
    """用户留存数据。"""

    cohort_date: str
    total_users: int
    d1_retention: float | None
    d7_retention: float | None
    d30_retention: float | None


# ── 分析服务 ──────────────────────────────────────────────────────


class AnalyticsService:
    """分析平台服务 — 提供 OLAP 查询能力。

    在 ClickHouse 不可用时降级为 PostgreSQL 查询。
    """

    def __init__(self, clickhouse_url: str | None = None) -> None:
        self._clickhouse_url = clickhouse_url
        self._initialized = False

    async def initialize(self, pool: Any) -> None:
        """初始化分析服务 (尝试连接 ClickHouse)。"""
        self._pool = pool
        await self._create_analytics_tables()
        self._initialized = True
        logger.info("AnalyticsService initialized")

    async def _create_analytics_tables(self) -> None:
        """创建分析用表 (PostgreSQL 方言; asyncpg 单次 execute 只接受单条语句)。"""
        statements = [
            """
            CREATE TABLE IF NOT EXISTS analytics_sales_daily (
                date DATE NOT NULL,
                gmv_cents BIGINT DEFAULT 0,
                order_count INT DEFAULT 0,
                user_count INT DEFAULT 0,
                aov_cents BIGINT DEFAULT 0,
                PRIMARY KEY (date)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS analytics_product_ranking (
                product_id VARCHAR(36) NOT NULL,
                product_name VARCHAR(255) NOT NULL,
                period VARCHAR(20) NOT NULL,
                sold_count INT DEFAULT 0,
                gmv_cents BIGINT DEFAULT 0,
                rank_pos INT NOT NULL,
                PRIMARY KEY (product_id, period)
            )
            """,
        ]
        async with self._pool.acquire() as conn:
            for ddl in statements:
                await conn.execute(ddl)

    async def get_sales_summary(
        self, start_date: str, end_date: str, granularity: TimeGranularity = TimeGranularity.DAY
    ) -> list[SalesSummary]:
        """获取销售汇总。"""
        try:
            sql = """
                SELECT date, gmv_cents, order_count, user_count, aov_cents
                FROM analytics_sales_daily
                WHERE date >= $1 AND date <= $2
                ORDER BY date ASC
            """
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, start_date, end_date)
            return [SalesSummary(**dict(r)) for r in rows]
        except Exception:
            logger.warning("Sales query failed, returning empty")
            return []

    async def get_product_ranking(
        self, period: str = "day", top_k: int = 20
    ) -> list[ProductSalesRanking]:
        """获取商品销售排行。"""
        try:
            sql = """
                SELECT product_id, product_name, sold_count, gmv_cents, rank_pos
                FROM analytics_product_ranking
                WHERE period = $1 AND rank_pos <= $2
                ORDER BY rank_pos ASC
            """
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, period, top_k)
            return [
                ProductSalesRanking(
                    product_id=r["product_id"],
                    product_name=r["product_name"],
                    sold_count=r["sold_count"],
                    gmv_cents=r["gmv_cents"],
                    avg_price_cents=r["gmv_cents"] // max(r["sold_count"], 1),
                    rank=r["rank_pos"],
                )
                for r in rows
            ]
        except Exception:
            logger.warning("Product ranking query failed, returning empty")
            return []

    async def get_funnel(self, start_date: str, end_date: str) -> list[FunnelData]:
        """Return no funnel until page-view/cart interaction events are ingested.

        Order and payment tables alone cannot truthfully provide the view and add-to-cart
        stages. Returning an empty series is intentional; fabricated rates are not data.
        """
        logger.info(
            "Conversion funnel unavailable: no page-view/add-to-cart event source is configured "
            "for %s..%s",
            start_date,
            end_date,
        )
        return []

    async def get_retention(self, cohort_date: str, lookback_days: int = 30) -> list[UserRetention]:
        """Compute purchase-cohort retention from real paid orders only."""
        from datetime import date

        cohort_day = date.fromisoformat(cohort_date)
        available_days = max(0, (date.today() - cohort_day).days)
        try:
            sql = """
                WITH paid_orders AS (
                    SELECT o.customer_id, o.created_at::date AS order_date
                    FROM customer_order o
                    WHERE o.customer_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM payment_intent p
                          WHERE p.order_id = o.id::text AND p.status = 'paid'
                      )
                ), cohort AS (
                    SELECT customer_id
                    FROM paid_orders
                    GROUP BY customer_id
                    HAVING MIN(order_date) = $1::date
                )
                SELECT
                    COUNT(*)::int AS total_users,
                    COUNT(*) FILTER (WHERE EXISTS (
                        SELECT 1 FROM paid_orders p
                        WHERE p.customer_id = c.customer_id
                          AND p.order_date = $1::date + 1
                    ))::int AS d1_users,
                    COUNT(*) FILTER (WHERE EXISTS (
                        SELECT 1 FROM paid_orders p
                        WHERE p.customer_id = c.customer_id
                          AND p.order_date = $1::date + 7
                    ))::int AS d7_users,
                    COUNT(*) FILTER (WHERE EXISTS (
                        SELECT 1 FROM paid_orders p
                        WHERE p.customer_id = c.customer_id
                          AND p.order_date = $1::date + 30
                    ))::int AS d30_users
                FROM cohort c
            """
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(sql, cohort_day)
            if row is None or int(row["total_users"] or 0) == 0:
                return []
            total = int(row["total_users"])
            d1 = (
                round(int(row["d1_users"] or 0) / total, 4)
                if available_days >= 1 and lookback_days >= 1
                else None
            )
            d7 = (
                round(int(row["d7_users"] or 0) / total, 4)
                if available_days >= 7 and lookback_days >= 7
                else None
            )
            d30 = (
                round(int(row["d30_users"] or 0) / total, 4)
                if available_days >= 30 and lookback_days >= 30
                else None
            )
            return [
                UserRetention(
                    cohort_date=cohort_day.isoformat(),
                    total_users=total,
                    d1_retention=d1,
                    d7_retention=d7,
                    d30_retention=d30,
                )
            ]
        except Exception:
            logger.warning("Purchase retention query failed; returning unavailable", exc_info=True)
            return []

    async def get_dashboard_overview(self) -> dict[str, Any]:
        """Return live paid-order totals; expose untracked metrics as unavailable."""
        try:
            sql = """
                WITH today_paid_orders AS (
                    SELECT o.id, o.customer_id, o.grand_total_cents
                    FROM customer_order o
                    WHERE o.created_at >= CURRENT_DATE
                      AND o.created_at < CURRENT_DATE + INTERVAL '1 day'
                      AND EXISTS (
                          SELECT 1 FROM payment_intent p
                          WHERE p.order_id = o.id::text AND p.status = 'paid'
                      )
                )
                SELECT
                    COALESCE(SUM(grand_total_cents), 0)::bigint AS today_gmv_cents,
                    COUNT(*)::int AS today_orders,
                    COALESCE(AVG(grand_total_cents), 0)::bigint AS avg_order_value_cents
                FROM today_paid_orders
            """
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(sql)
            if row is None:
                row = {"today_gmv_cents": 0, "today_orders": 0, "avg_order_value_cents": 0}
            return {
                "today_gmv_cents": int(row["today_gmv_cents"] or 0),
                "today_orders": int(row["today_orders"] or 0),
                "today_active_users": None,
                "today_new_users": None,
                "conversion_rate": None,
                "avg_order_value_cents": int(row["avg_order_value_cents"] or 0),
                "refund_rate": None,
                "unavailable_metrics": [
                    "today_active_users",
                    "today_new_users",
                    "conversion_rate",
                    "refund_rate",
                ],
            }
        except Exception:
            logger.warning("Dashboard order query failed; returning unavailable", exc_info=True)
            return {
                "today_gmv_cents": None,
                "today_orders": None,
                "today_active_users": None,
                "today_new_users": None,
                "conversion_rate": None,
                "avg_order_value_cents": None,
                "refund_rate": None,
                "unavailable_metrics": [
                    "today_gmv_cents",
                    "today_orders",
                    "today_active_users",
                    "today_new_users",
                    "conversion_rate",
                    "avg_order_value_cents",
                    "refund_rate",
                ],
            }
