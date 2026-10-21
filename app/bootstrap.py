"""hemall 启动装配 — 注册 provider + 初始化数据库 schema。

服务层职责 (§8): 把 obase 的 fallback provider 注册进 ProviderRegistry
(omodul 在调用期按名取用, 如 create_region 校验 payment provider 已注册),
并在启动期幂等建齐商务表 (obase.commerce_batch_schema)。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from obase.commerce_batch_schema import ensure_commerce_batch_schema
from obase.fulfillment_providers import ManualFulfillmentProvider
from obase.notification_providers import LogNotificationProvider
from obase.payment_providers import ManualPaymentProvider
from obase.persistence.pool import PgPool
from obase.provider_registry import ProviderRegistry
from obase.search_providers import LogSearchProvider
from obase.tax_providers import FlatRateTaxProvider

# Phase 0: 导入指标辅助函数
from app.middleware.metrics import update_db_pool_metrics

from .config import Settings
from .ext.cv_provider import ManualCVProvider
from .ext.douyin_provider import ManualDouyinProvider
from .ext.llm_provider import ManualLLMProvider
from .ext.payment_gateways import build_payment_gateway
from .ext.payout_provider import ManualPaymentGateway, ManualPayoutProvider
from .ext.schema import ensure_ext_schema
from .ext.spider_provider import ManualSpiderProvider
from .ext.spider_targets import LayeredSpiderProvider
from .ext.vlm_ollama_provider import OllamaVLMProvider
from .ext.vlm_provider import ManualVLMProvider
from .ext.weather_provider import ManualWeatherProvider
from .ext.wechat_provider import ManualWeChatChannelProvider

logger = logging.getLogger("hemall.bootstrap")


def register_providers(settings: Settings) -> None:
    """注册无凭据 fallback provider (幂等)。规范名须与 omodul 期望一致。

    payment/fulfillment="manual", search/notification="log", tax="flat"。
    这些 in-memory 实现的状态机镜像真实 vendor 语义, 供本地/测试与无凭据部署使用;
    接真实 Stripe/UPS/… 时在此追加 register_generic(..., replace=True) 即可。
    """
    reg = ProviderRegistry.get()
    reg.register_generic("payment", "manual", ManualPaymentProvider(), replace=True)
    reg.register_generic("fulfillment", "manual", ManualFulfillmentProvider(), replace=True)
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
    reg.register_generic("payment_gateway", gateway_provider, gateway, replace=True)
    if gateway_provider != "manual":
        reg.register_generic("payment_gateway", "manual", ManualPaymentGateway(), replace=True)
    reg.register_generic("weather", "manual", ManualWeatherProvider(), replace=True)
    reg.register_generic("vlm", "manual", ManualVLMProvider(), replace=True)
    # VLM 真模型: 宿主机 Ollama 上的 MiniCPM-V (拍照识货/以图比对/判损)。
    # 注册本身不发请求; 模型不可达时调用方按异常降级, 不影响启动。
    reg.register_generic(
        "vlm",
        "ollama",
        OllamaVLMProvider(
            base_url=settings.vlm_ollama_base_url,
            model=settings.vlm_ollama_model,
            timeout_s=settings.vlm_ollama_timeout_s,
            output_root=settings.output_root,
        ),
        replace=True,
    )
    reg.register_generic("cv", "manual", ManualCVProvider(), replace=True)
    reg.register_generic("llm", "manual", ManualLLMProvider(), replace=True)
    reg.register_generic("wechat_channel", "manual", ManualWeChatChannelProvider(), replace=True)
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


async def init_db(
    settings: Settings, *, apply_runtime_role: bool | None = None
) -> tuple[PgPool, asyncio.Task]:
    """创建命名连接池并幂等建齐商务表。DB 不可达时向上抛, 由调用方决定降级。

    返回 (pool, metrics_task) —— 后台指标更新任务的引用交回调用方, 使其能在
    应用关停时显式 cancel (否则 lifespan 结束后这个 while True 循环永远不退,
    进程内泄漏一个协程任务)。

    并发保护: 生产环境 gunicorn 多 worker 同时启动时, 每个 worker 都会执行
    schema 初始化。PostgreSQL 的 CREATE TABLE IF NOT EXISTS 在并发场景下存在
    竞态窗口(两事务同时判定"表不存在"并创建, 后到者因同名复合类型冲突报错),
    故用 advisory lock 将 schema 初始化串行化——先到者建表, 后到者等锁释放后
    跳过已存在的表。

    权限分离: DDL (迁移) 一律以连接用户 (owner) 身份执行; 全部 schema 就绪后,
    生产环境再把连接池切到 hemall_runtime (仅 DML, 无 DDL)。迁移容器
    (python -m app.cli migrate) 传 apply_runtime_role=False 显式跳过切换。
    """
    if apply_runtime_role is None:
        apply_runtime_role = settings.environment.lower() == "production"

    # 生产环境使用 effective_pg_dsn (含 sslmode)，迁移时由 CLI 显式传 pg_dsn
    dsn = (
        settings.effective_pg_dsn
        if settings.environment.lower() == "production"
        else settings.pg_dsn
    )

    pool = await PgPool.create(
        name=settings.pg_pool_name,
        dsn=dsn,
        min_size=settings.pg_pool_min,
        max_size=settings.pg_pool_max,
    )

    try:
        await _ensure_schema_and_verify(pool, apply_runtime_role=apply_runtime_role)
    except BaseException:
        # 建表阶段失败 (PG 起来了但 schema 半成品 / 权限不足 / 网络中途断) 时,
        # 池已经进了 PgPool._registry 却没人 close: 进程内泄漏连接, 且下一次
        # 重试的 PgPool.create 会因重名直接抛 ValueError —— 自愈重试因此永远
        # 无法成功。这里显式关掉并从注册表摘除, 让调用方可以干净地重试。
        with contextlib.suppress(Exception):
            await pool.close()
        raise

    # Phase 0: 更新 DB 连接池指标
    async def update_metrics():
        while True:
            try:
                used = len(pool._used_connections) if hasattr(pool, "_used_connections") else 0
                update_db_pool_metrics(settings.pg_pool_name, settings.pg_pool_max, used)
            except Exception as e:
                logger.warning("Failed to update DB pool metrics: %s", e)
            await asyncio.sleep(10)  # 每 10 秒更新一次

    # 启动后台任务更新指标
    metrics_task = asyncio.create_task(update_metrics())

    logger.info(
        "database pool '%s' ready; commerce + ext schema ensured",
        settings.pg_pool_name,
    )
    return pool, metrics_task


async def _ensure_schema_and_verify(pool: PgPool, *, apply_runtime_role: bool) -> None:
    """在 advisory lock 内幂等建齐全部 schema 并做启动自检。"""
    # 固定锁 ID (hemall_prod 库私有, 无跨项目冲突)
    SCHEMA_LOCK_ID = 824701
    async with pool.acquire() as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock($1)", SCHEMA_LOCK_ID)
        try:
            await ensure_commerce_batch_schema(pool)
            await _ensure_hemall_local_schema(pool)
            await _ensure_app_user_totp_schema(pool)
            await ensure_ext_schema(pool)

            # Phase 0 Week 2: 安全 + 支付表结构
            from .payments.models import PAYMENT_SESSION_DDL
            from .security.audit import AUDIT_LOG_DDL

            await _ensure_security_payment_schema(pool, AUDIT_LOG_DDL, PAYMENT_SESSION_DDL)

            # Phase 1: 库存管理表结构
            from .inventory.models import STOCK_MOVEMENT_DDL

            await _ensure_inventory_schema(pool, STOCK_MOVEMENT_DDL)

            # Phase 1: 订单生命周期表结构
            from .orders.models import ORDER_LIFECYCLE_DDL

            await _ensure_order_lifecycle_schema(pool, ORDER_LIFECYCLE_DDL)

            # 售后/退款表结构
            from .payments.refund_models import REFUND_REQUEST_DDL

            async with pool.acquire() as conn:
                await conn.execute(REFUND_REQUEST_DDL)
            logger.info("refund schema ensured (refund_request, refund_audit_log)")

            # P0: payment authority/outbox, exact inventory reservations and
            # versioned order transitions.  Keep this project-local rather than
            # modifying the shared 3O schema used by other services.
            from .p0_schema import ensure_p0_schema

            await ensure_p0_schema(pool)

            # 启动自检：验证关键列和约束已就位，缺失则 fail-fast
            await _verify_critical_schema(pool)
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock($1)", SCHEMA_LOCK_ID)

    # 生产环境：schema 全部就绪后，把连接池切到 hemall_runtime (仅 DML)。
    # 必须在 DDL 之后——运行时角色没有 CREATE 权限，提前切换会让迁移本身失败。
    if apply_runtime_role:
        _switch_pool_to_runtime_role(pool)


def _switch_pool_to_runtime_role(pool: PgPool) -> None:
    """把连接池的所有 (含未来) 连接切到 hemall_runtime 角色 (仅 DML, 无 DDL)。

    SET ROLE 失败 (角色不存在/未授权) 只记 warning 不阻塞启动——由运维补跑
    `python -m app.cli grant-runtime`。acquire 包装必须返回异步上下文管理器
    (与 asyncpg Pool.acquire 契约一致)，直接返回协程会让调用方
    `async with pool.acquire()` 全部崩溃。
    """
    import contextlib

    async def _init_runtime_role(conn) -> None:
        try:
            await conn.execute("SET ROLE hemall_runtime")
        except Exception as e:
            logger.warning(
                "Failed to SET ROLE hemall_runtime: %s. "
                "Run 'python -m app.cli grant-runtime' as owner.",
                e,
            )

    original_acquire = pool.acquire

    @contextlib.asynccontextmanager
    async def acquire_with_role(*args, **kwargs):
        async with original_acquire(*args, **kwargs) as conn:
            await _init_runtime_role(conn)
            yield conn

    pool.acquire = acquire_with_role  # type: ignore[method-assign]


async def _ensure_hemall_local_schema(pool: PgPool) -> None:
    """hemall 项目层的补丁表结构 — 顾客密码登录不在 obase 共享的
    commerce_batch_schema 里 (那是跨项目库, 故意不在这加字段影响其他消费者);
    这里做 hemall 本地的幂等 ALTER, 只影响这一个数据库。
    """
    async with pool.acquire() as conn:
        await conn.execute('ALTER TABLE "customer" ADD COLUMN IF NOT EXISTS password_hash TEXT')


async def _ensure_app_user_totp_schema(pool: PgPool) -> None:
    """为 app_user 表添加 TOTP 两步验证所需列。"""
    async with pool.acquire() as conn:
        await conn.execute('ALTER TABLE "app_user" ADD COLUMN IF NOT EXISTS totp_secret TEXT')
        await conn.execute(
            'ALTER TABLE "app_user" ADD COLUMN IF NOT EXISTS totp_recovery_codes_hash TEXT'
        )
        await conn.execute(
            'ALTER TABLE "app_user" ADD COLUMN IF NOT EXISTS '
            "totp_last_used_counter BIGINT DEFAULT -1"
        )
        await conn.execute(
            'ALTER TABLE "app_user" ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN DEFAULT false'
        )
    logger.info(
        "app_user TOTP schema ensured "
        "(totp_secret, totp_recovery_codes_hash, totp_last_used_counter, totp_enabled)"
    )


async def _ensure_security_payment_schema(pool: PgPool, audit_ddl: str, payment_ddl: str) -> None:
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


async def _verify_critical_schema(pool: PgPool) -> None:
    """启动自检：验证关键表的必需列和索引存在，缺失则 fail-fast。

    这能在应用启动时捕获 DDL 漂移（如 stock_movement 缺 idempotency_key
    或唯一索引），而不是等到运行时抛 UndefinedColumn/唯一约束冲突。
    """
    async with pool.acquire() as conn:
        # 检查 stock_movement.idempotency_key 列
        has_idempotency_key = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'stock_movement' AND column_name = 'idempotency_key'
            )
            """
        )
        if not has_idempotency_key:
            raise RuntimeError(
                "stock_movement 表缺少 idempotency_key 列，请运行迁移或检查 DDL 顺序"
            )

        # 检查唯一索引 uq_stock_movement_idempotency
        has_unique_index = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE indexname = 'uq_stock_movement_idempotency'
            )
            """
        )
        if not has_unique_index:
            raise RuntimeError("stock_movement 表缺少 uq_stock_movement_idempotency 唯一索引")

        # 检查 order_line_item.fulfilled_qty CHECK 约束
        has_fulfilled_check = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'order_line_item_fulfilled_qty_check'
            )
            """
        )
        if not has_fulfilled_check:
            raise RuntimeError("order_line_item 表缺少 fulfilled_qty CHECK 约束")

    logger.info(
        "critical schema verification passed (stock_movement.idempotency_key, "
        "uq_stock_movement_idempotency, order_line_item_fulfilled_qty_check)"
    )
