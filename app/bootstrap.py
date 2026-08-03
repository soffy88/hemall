"""hemall 启动装配 — 注册 provider + 初始化数据库 schema。

服务层职责 (§8): 把 obase 的 fallback provider 注册进 ProviderRegistry
(omodul 在调用期按名取用, 如 create_region 校验 payment provider 已注册),
并在启动期幂等建齐商务表 (obase.commerce_batch_schema)。
"""

from __future__ import annotations

import asyncio
import logging

from obase.commerce_batch_schema import ensure_commerce_batch_schema
from obase.fulfillment_providers import ManualFulfillmentProvider
from obase.notification_providers import LogNotificationProvider
from obase.payment_providers import ManualPaymentProvider
from obase.persistence.pool import PgPool
from obase.provider_registry import ProviderRegistry
from obase.search_providers import LogSearchProvider
from obase.tax_providers import FlatRateTaxProvider

from .config import Settings
from .ext.cv_provider import ManualCVProvider
from .ext.douyin_provider import ManualDouyinProvider
from .ext.llm_provider import ManualLLMProvider
from .ext.payment_gateways import build_payment_gateway
from .ext.payout_provider import ManualPaymentGateway, ManualPayoutProvider
from .ext.schema import ensure_ext_schema
from .ext.spider_provider import ManualSpiderProvider
from .ext.spider_targets import LayeredSpiderProvider
from .ext.vlm_provider import ManualVLMProvider
from .ext.weather_provider import ManualWeatherProvider
from .ext.wechat_provider import ManualWeChatChannelProvider

# Phase 0: 导入指标辅助函数
from app.middleware.metrics import update_db_pool_metrics

logger = logging.getLogger("hemall.bootstrap")


def register_providers(settings: Settings) -> None:
    """注册无凭据 fallback provider (幂等)。规范名须与 omodul 期望一致。

    payment/fulfillment="manual", search/notification="log", tax="flat"。
    这些 in-memory 实现的状态机镜像真实 vendor 语义, 供本地/测试与无凭据部署使用;
    接真实 Stripe/UPS/… 时在此追加 register_generic(..., replace=True) 即可。
    """
    reg = ProviderRegistry.get()
    reg.register_generic("payment", "manual", ManualPaymentProvider(), replace=True)
    reg.register_generic(
        "fulfillment", "manual", ManualFulfillmentProvider(), replace=True
    )
    reg.register_generic("search", "log", LogSearchProvider(), replace=True)
    reg.register_generic("notification", "log", LogNotificationProvider(), replace=True)
    reg.register_generic(
        "tax",
        "flat",
        FlatRateTaxProvider(rate_percent=settings.flat_tax_rate_percent),
        replace=True,
    )
    reg.register_generic("payout", "manual", ManualPayoutProvider(), replace=True)
    # 补天计划 Task 1.3 → P0 冲刺: 收款网关 (统一下单/退款)。真实通道
    # (wechat/stripe) 平行替换：build_payment_gateway 按 HEMALL_PAYMENT_GATEWAY_PROVIDER
    # 装配，密钥缺失诚实回退 manual；始终保留 manual 名兼容旧调用点。
    gateway_provider, gateway = build_payment_gateway(settings)
    reg.register_generic(
        "payment_gateway", gateway_provider, gateway, replace=True
    )
    if gateway_provider != "manual":
        reg.register_generic(
            "payment_gateway", "manual", ManualPaymentGateway(), replace=True
        )
    reg.register_generic("weather", "manual", ManualWeatherProvider(), replace=True)
    reg.register_generic("vlm", "manual", ManualVLMProvider(), replace=True)
    reg.register_generic("cv", "manual", ManualCVProvider(), replace=True)
    reg.register_generic("llm", "manual", ManualLLMProvider(), replace=True)
    reg.register_generic(
        "wechat_channel", "manual", ManualWeChatChannelProvider(), replace=True
    )
    reg.register_generic("spider", "manual", ManualSpiderProvider(), replace=True)
    # Phase 7 Task 2: 具象化真实爬虫 (苏宁/京东到家/OpenFoodFacts)。默认走
    # layered——测试覆写 (set_results) 仍然生效，关键词命中真实目标时发真实
    # HTTP 请求；manual 保留兼容旧调用点。
    reg.register_generic("spider", "layered", LayeredSpiderProvider(), replace=True)
    reg.register_generic("douyin", "manual", ManualDouyinProvider(), replace=True)
    logger.info(
        "registered fallback providers: payment/payout/weather/vlm/cv/llm/"
        "wechat_channel/spider/douyin/fulfillment/search/notification/tax"
    )


async def init_db(settings: Settings) -> PgPool:
    """创建命名连接池并幂等建齐商务表。DB 不可达时向上抛, 由调用方决定降级。

    并发保护: 生产环境 gunicorn 多 worker 同时启动时, 每个 worker 都会执行
    schema 初始化。PostgreSQL 的 CREATE TABLE IF NOT EXISTS 在并发场景下存在
    竞态窗口(两事务同时判定"表不存在"并创建, 后到者因同名复合类型冲突报错),
    故用 advisory lock 将 schema 初始化串行化——先到者建表, 后到者等锁释放后
    跳过已存在的表。
    """
    pool = await PgPool.create(
        name=settings.pg_pool_name,
        dsn=settings.pg_dsn,
        min_size=settings.pg_pool_min,
        max_size=settings.pg_pool_max,
    )

    # 固定锁 ID (hemall_prod 库私有, 无跨项目冲突)
    SCHEMA_LOCK_ID = 824701
    async with pool.acquire() as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock($1)", SCHEMA_LOCK_ID)
        try:
            await ensure_commerce_batch_schema(pool)
            await _ensure_hemall_local_schema(pool)
            await ensure_ext_schema(pool)

            # Phase 0 Week 2: 安全 + 支付表结构
            from .security.audit import AUDIT_LOG_DDL
            from .payments.models import PAYMENT_SESSION_DDL

            await _ensure_security_payment_schema(pool, AUDIT_LOG_DDL, PAYMENT_SESSION_DDL)

            # Phase 1: 库存管理表结构
            from .inventory.models import STOCK_MOVEMENT_DDL
            await _ensure_inventory_schema(pool, STOCK_MOVEMENT_DDL)

            # Phase 1: 订单生命周期表结构
            from .orders.models import ORDER_LIFECYCLE_DDL
            await _ensure_order_lifecycle_schema(pool, ORDER_LIFECYCLE_DDL)
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock($1)", SCHEMA_LOCK_ID)
    
    # Phase 0: 更新 DB 连接池指标
    async def update_metrics():
        while True:
            try:
                used = len(pool._used_connections) if hasattr(pool, '_used_connections') else 0
                update_db_pool_metrics(settings.pg_pool_name, settings.pg_pool_max, used)
            except Exception as e:
                logger.warning("Failed to update DB pool metrics: %s", e)
            await asyncio.sleep(10)  # 每 10 秒更新一次
    
    # 启动后台任务更新指标
    asyncio.create_task(update_metrics())
    
    logger.info(
        "database pool '%s' ready; commerce + ext schema ensured",
        settings.pg_pool_name,
    )
    return pool


async def _ensure_hemall_local_schema(pool: PgPool) -> None:
    """hemall 项目层的补丁表结构 — 顾客密码登录不在 obase 共享的
    commerce_batch_schema 里 (那是跨项目库, 故意不在这加字段影响其他消费者);
    这里做 hemall 本地的幂等 ALTER, 只影响这一个数据库。
    """
    async with pool.acquire() as conn:
        await conn.execute(
            'ALTER TABLE "customer" ADD COLUMN IF NOT EXISTS password_hash TEXT'
        )


async def _ensure_security_payment_schema(
    pool: PgPool, audit_ddl: str, payment_ddl: str
) -> None:
    """Phase 0 Week 2: 幂等创建审计日志表 + 支付会话表。"""
    async with pool.acquire() as conn:
        await conn.execute(audit_ddl)
        await conn.execute(payment_ddl)
    logger.info("security + payment schema ensured (audit_log, payment_session)")


async def _ensure_inventory_schema(pool: PgPool, inventory_ddl: str) -> None:
    """Phase 1: 幂等创建库存流水表 + 安全库存配置表。"""
    async with pool.acquire() as conn:
        await conn.execute(inventory_ddl)
    logger.info("inventory schema ensured (stock_movement, product_safety_stock)")


async def _ensure_order_lifecycle_schema(pool: PgPool, order_ddl: str) -> None:
    """Phase 1: 幂等创建订单状态历史表。"""
    async with pool.acquire() as conn:
        await conn.execute(order_ddl)
    logger.info("order lifecycle schema ensured (order_status_history)")
