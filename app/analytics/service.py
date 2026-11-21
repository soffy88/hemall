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

import json
import logging
import re
from enum import Enum
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from pydantic import BaseModel

logger = logging.getLogger("hemall.analytics")

_CH_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _qs_first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


class _ClickHouseClient:
    """ClickHouse HTTP 接口的最小客户端 (基于 httpx, 不引入新依赖)。

    URL 形如 http://[<user>:<secret>@]host[:8123][/database], 也兼容
    查询参数写法 (?user= / ?password= / ?database=)。

    支持: ping、DDL 执行、{name:Type} 参数化查询 (JSONEachRow)、
    批量 INSERT (JSONEachRow)。
    """

    def __init__(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"ClickHouse URL 不合法: {url!r}")
        port = parsed.port or 8123
        self._base_url = f"{parsed.scheme}://{parsed.hostname}:{port}"
        query = parse_qs(parsed.query)
        database = (parsed.path or "").lstrip("/") or _qs_first(query, "database") or "default"
        if not _CH_IDENTIFIER_RE.match(database):
            raise ValueError(f"ClickHouse 数据库名不合法: {database!r}")
        self._database = database
        user = (
            unquote(parsed.username) if parsed.username else _qs_first(query, "user") or "default"
        )
        password = (
            unquote(parsed.password) if parsed.password else _qs_first(query, "password") or ""
        )
        self._auth = (user, password)
        self._client: httpx.AsyncClient | None = None

    @property
    def database(self) -> str:
        return self._database

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                auth=self._auth,
                timeout=httpx.Timeout(15.0, connect=3.0),
            )
        return self._client

    async def ping(self) -> bool:
        """GET /ping; 任何网络错误都视为不通 (返回 False, 不抛错)。"""
        try:
            resp = await self._http().get("/ping")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def execute(self, sql: str) -> None:
        """执行无返回的语句 (DDL / TRUNCATE)。"""
        resp = await self._http().post("/", content=sql.encode("utf-8"))
        resp.raise_for_status()

    async def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """参数化查询; sql 需自带 FORMAT JSONEachRow, 占位符写 {name:Type}。"""
        url_params = {f"param_{k}": str(v) for k, v in (params or {}).items()}
        resp = await self._http().post("/", params=url_params, content=sql.encode("utf-8"))
        resp.raise_for_status()
        rows: list[dict[str, Any]] = []
        for line in resp.text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        """批量 INSERT (JSONEachRow); 空列表直接跳过。"""
        if not rows:
            return
        payload = f"INSERT INTO {table} FORMAT JSONEachRow\n" + "\n".join(
            json.dumps(r, ensure_ascii=False) for r in rows
        )
        resp = await self._http().post("/", content=payload.encode("utf-8"))
        resp.raise_for_status()


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
        # settings.clickhouse_url 缺省是空串, 统一当未配置处理
        self._clickhouse_url = clickhouse_url or None
        self._initialized = False
        self._pool: Any = None
        self._ch: _ClickHouseClient | None = None
        self._ch_available = False

    async def initialize(self, pool: Any) -> None:
        """初始化分析服务: PG 聚合表必建; 配了 ClickHouse 且 ping 通时建 CH 表并同步存量。"""
        self._pool = pool
        await self._create_analytics_tables()
        if self._clickhouse_url:
            await self._init_clickhouse()
        self._initialized = True
        logger.info("AnalyticsService initialized (clickhouse_available=%s)", self._ch_available)

    async def _init_clickhouse(self) -> None:
        """尝试接入 ClickHouse; 任何失败都只记 warning 并降级纯 PG, 不抛错。"""
        assert self._clickhouse_url is not None
        try:
            client = _ClickHouseClient(self._clickhouse_url)
        except ValueError as exc:
            logger.warning("ClickHouse URL 不可用, 降级纯 PG: %s", exc)
            return
        if not await client.ping():
            logger.warning("ClickHouse ping 不通 (%s), 降级纯 PG", self._clickhouse_url)
            return
        self._ch = client
        try:
            await self._create_clickhouse_tables()
            synced = await self.sync_aggregates_to_clickhouse()
            self._ch_available = True
            logger.info("ClickHouse 已接入, 存量聚合数据同步完成: %s", synced)
        except Exception:
            logger.warning("ClickHouse 建表/同步失败, 降级纯 PG", exc_info=True)
            self._ch_available = False

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

    async def _create_clickhouse_tables(self) -> None:
        """在 ClickHouse 建与 PG 两张聚合表同构的 MergeTree 表。"""
        assert self._ch is not None
        db = self._ch.database
        await self._ch.execute(f"CREATE DATABASE IF NOT EXISTS {db}")
        statements = [
            f"""
            CREATE TABLE IF NOT EXISTS {db}.analytics_sales_daily (
                date Date,
                gmv_cents Int64 DEFAULT 0,
                order_count Int32 DEFAULT 0,
                user_count Int32 DEFAULT 0,
                aov_cents Int64 DEFAULT 0
            ) ENGINE = MergeTree ORDER BY date
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {db}.analytics_product_ranking (
                product_id String,
                product_name String,
                period String,
                sold_count Int32 DEFAULT 0,
                gmv_cents Int64 DEFAULT 0,
                rank_pos Int32
            ) ENGINE = MergeTree ORDER BY (period, rank_pos)
            """,
        ]
        for ddl in statements:
            await self._ch.execute(ddl)

    async def sync_aggregates_to_clickhouse(self) -> dict[str, int]:
        """把 PG 两张聚合表的全量数据同步到 ClickHouse (先清空再写入, 幂等)。

        供 initialize() 与外部定时任务复用; 返回各表同步行数。
        未配置 ClickHouse 时抛 RuntimeError; CH 侧错误原样抛出。
        """
        if self._ch is None:
            raise RuntimeError("ClickHouse 未配置, 无法同步聚合数据")
        db = self._ch.database

        async with self._pool.acquire() as conn:
            sales_rows = await conn.fetch(
                "SELECT date, gmv_cents, order_count, user_count, aov_cents "
                "FROM analytics_sales_daily ORDER BY date ASC"
            )
            ranking_rows = await conn.fetch(
                "SELECT product_id, product_name, period, sold_count, gmv_cents, rank_pos "
                "FROM analytics_product_ranking ORDER BY period ASC, rank_pos ASC"
            )

        sales_payload: list[dict[str, Any]] = [
            {
                "date": r["date"].isoformat()
                if hasattr(r["date"], "isoformat")
                else str(r["date"]),
                "gmv_cents": int(r["gmv_cents"]),
                "order_count": int(r["order_count"]),
                "user_count": int(r["user_count"]),
                "aov_cents": int(r["aov_cents"]),
            }
            for r in sales_rows
        ]
        ranking_payload: list[dict[str, Any]] = [
            {
                "product_id": str(r["product_id"]),
                "product_name": str(r["product_name"]),
                "period": str(r["period"]),
                "sold_count": int(r["sold_count"]),
                "gmv_cents": int(r["gmv_cents"]),
                "rank_pos": int(r["rank_pos"]),
            }
            for r in ranking_rows
        ]

        await self._ch.execute(f"TRUNCATE TABLE {db}.analytics_sales_daily")
        await self._ch.insert_rows(f"{db}.analytics_sales_daily", sales_payload)
        await self._ch.execute(f"TRUNCATE TABLE {db}.analytics_product_ranking")
        await self._ch.insert_rows(f"{db}.analytics_product_ranking", ranking_payload)
        counts = {
            "analytics_sales_daily": len(sales_payload),
            "analytics_product_ranking": len(ranking_payload),
        }
        logger.info("ClickHouse 聚合数据同步完成: %s", counts)
        return counts

    async def get_sales_summary(
        self, start_date: str, end_date: str, granularity: TimeGranularity = TimeGranularity.DAY
    ) -> list[SalesSummary]:
        """获取销售汇总: ClickHouse 可用时优先查 CH, 查询失败自动回退 PG。"""
        if self._ch_available:
            try:
                return await self._get_sales_summary_clickhouse(start_date, end_date)
            except Exception:
                logger.warning("ClickHouse 销售汇总查询失败, 回退 PG", exc_info=True)
        return await self._get_sales_summary_pg(start_date, end_date, granularity)

    async def _get_sales_summary_clickhouse(
        self, start_date: str, end_date: str
    ) -> list[SalesSummary]:
        """从 ClickHouse 读销售汇总 (analytics_sales_daily 镜像表)。"""
        assert self._ch is not None
        rows = await self._ch.query(
            f"""
            SELECT date, gmv_cents, order_count, user_count, aov_cents
            FROM {self._ch.database}.analytics_sales_daily
            WHERE date >= {{start:Date}} AND date <= {{end:Date}}
            ORDER BY date ASC
            FORMAT JSONEachRow
            """,
            {"start": start_date, "end": end_date},
        )
        return [SalesSummary(**r) for r in rows]

    async def _get_sales_summary_pg(
        self, start_date: str, end_date: str, granularity: TimeGranularity = TimeGranularity.DAY
    ) -> list[SalesSummary]:
        """从 PostgreSQL 读销售汇总 (ClickHouse 不可用时的降级路径)。"""
        try:
            sql = """
                SELECT date, gmv_cents, order_count, user_count, aov_cents
                FROM analytics_sales_daily
                WHERE date >= $1 AND date <= $2
                ORDER BY date ASC
            """
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, start_date, end_date)
            summaries: list[SalesSummary] = []
            for r in rows:
                d = dict(r)
                # asyncpg 返回 datetime.date, SalesSummary.date 是 str, 需先转 ISO 串
                # (否则 pydantic ValidationError 被外层 except 吞掉, 永远返回空)
                if hasattr(d["date"], "isoformat"):
                    d["date"] = d["date"].isoformat()
                summaries.append(SalesSummary(**d))
            return summaries
        except Exception:
            logger.warning("Sales query failed, returning empty")
            return []

    async def get_product_ranking(
        self, period: str = "day", top_k: int = 20
    ) -> list[ProductSalesRanking]:
        """获取商品销售排行: ClickHouse 可用时优先查 CH, 查询失败自动回退 PG。"""
        if self._ch_available:
            try:
                return await self._get_product_ranking_clickhouse(period, top_k)
            except Exception:
                logger.warning("ClickHouse 商品排行查询失败, 回退 PG", exc_info=True)
        return await self._get_product_ranking_pg(period, top_k)

    async def _get_product_ranking_clickhouse(
        self, period: str, top_k: int
    ) -> list[ProductSalesRanking]:
        """从 ClickHouse 读商品排行 (analytics_product_ranking 镜像表)。"""
        assert self._ch is not None
        rows = await self._ch.query(
            f"""
            SELECT product_id, product_name, sold_count, gmv_cents, rank_pos
            FROM {self._ch.database}.analytics_product_ranking
            WHERE period = {{period:String}} AND rank_pos <= {{top:Int32}}
            ORDER BY rank_pos ASC
            FORMAT JSONEachRow
            """,
            {"period": period, "top": top_k},
        )
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

    async def _get_product_ranking_pg(
        self, period: str = "day", top_k: int = 20
    ) -> list[ProductSalesRanking]:
        """从 PostgreSQL 读商品排行 (ClickHouse 不可用时的降级路径)。"""
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
