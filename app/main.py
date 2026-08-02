"""hemall 应用入口 — FastAPI app 工厂 + 启动装配 (lifespan)。

启动顺序 (§8 服务层装配):
  1. 注册 fallback provider (不依赖 DB, 总是执行)。
  2. 装配 oservi 事件派发器 (挂 app.state.events，hemall 原有商城域用)。
  3. best-effort 建库 + 建表; DB 不可达则降级——应用照常启动, DB 端点返回 503。
  4. DB 就绪时装配 app.ext 自己的 6 个 oservi 引擎 (挂 app.state.ext_oservi)，
     4 个 CronSchedulerEngine 各起一个 daemon thread 常驻运行 (DB 不可达则跳过，
     这些引擎全都要碰库，没有 pool 空转没意义)。

Phase 0 新增:
  - 结构化日志 (structlog) + TraceID 传播
  - Prometheus 指标采集 (/metrics)
  - OpenTelemetry 链路追踪
  - 健康检查端点 (/health/live, /health/ready)

运行: uvicorn app.main:app  (或 python -m app.main)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from prometheus_client import make_asgi_app

from . import __version__
from .bootstrap import init_db, register_providers
from .deps import ensure_redis_connected, get_settings
from .events import build_event_dispatcher
from .ext.oservi_lifecycle import build_ext_oservi
from .inventory.router import router as inventory_router
from .middleware.logging import LoggingMiddleware, configure_structlog
from .orders.router import router as orders_router
from .middleware.metrics import MetricsMiddleware
from .observability.health import router as health_router
from .observability.tracing import setup_tracing
from .payments.router import router as payment_router
from .push.service import PushManager
from .realtime.hub import manager as realtime_manager
from .realtime.hub import router as realtime_router
from .search.client import ElasticsearchClient
from .search.router import router as search_router
from .cache.service import CacheManager
from .cache.router import router as cache_router
from .push.router import router as push_router
from .eventsourcing.router import router as eventsourcing_router
from .analytics.service import AnalyticsService
from .i18n.service import CurrencyConverter, LocalizedPaymentRouter, TranslationManager
from .recommend.router import router as recommend_router
from .recommend.service import RecommendationService
from .risk.engine import RiskEngine
from .risk.router import router as risk_router
from .ai_assistant.router import router as ai_router
from .ai_assistant.models import AIConfig
from .ai_assistant.service import AIAssistantService


from .cache.service import CacheManager

from .routers import build_router

# Phase 0 Week 2: 限流
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from .security.rate_limit import limiter

# Phase 0 Week 2: 审计日志中间件
from .security.audit import AuditMiddleware

# Phase 0: 配置结构化日志 (进程启动期一次)
configure_structlog()

logger = logging.getLogger("hemall.main")


# ── 应用状态 (供 health 模块访问，避免循环导入) ─────────────────────────


@dataclass
class AppState:
    """应用运行时状态 (供 health/observability 访问)。"""

    pool: object | None = None
    version: str = __version__
    # Phase 1
    realtime_manager: Any = None
    # Phase 2
    cache_manager: CacheManager | None = None
    search_client: ElasticsearchClient | None = None
    push_manager: PushManager | None = None
    # Phase 3
    recommend_service: RecommendationService | None = None
    risk_engine: RiskEngine | None = None
    analytics_service: AnalyticsService | None = None
    translation_manager: TranslationManager | None = None
    currency_converter: CurrencyConverter | None = None
    payment_router: LocalizedPaymentRouter | None = None
    # Phase 4+
    ai_service: AIAssistantService | None = None


_app_state = AppState()


def get_app_state() -> AppState:
    """获取应用运行时状态 (供 health 模块使用)。"""
    return _app_state


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动装配 + 关停清理。"""
    settings = get_settings()
    app.state.config = settings

    # Phase 0: 设置链路追踪 (在 lifespan 中调用，此时 app 已构建完毕)
    try:
        setup_tracing(app, service_name="hemall-backend")
    except Exception as exc:  # noqa: BLE001
        logger.warning("tracing setup failed (non-fatal): %s", exc)

    register_providers(settings)
    app.state.events = build_event_dispatcher(settings)

    # Phase 0: 检查 Redis 连接 (best-effort，不阻止启动)
    try:
        await ensure_redis_connected(settings.redis_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis check failed (non-fatal): %s", exc)

    # Phase 1: 启动 WebSocket 实时推送 (Redis pub/sub)
    try:
        await realtime_manager.start(settings.redis_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("realtime hub start failed (non-fatal): %s", exc)

    # Phase 2: 启动 Redis 缓存层
    try:
        _app_state.cache_manager = await CacheManager.create(settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("CacheManager start failed (non-fatal): %s", exc)

    # Phase 2: 初始化 Elasticsearch 客户端
    try:
        _app_state.search_client = await ElasticsearchClient.create(settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Elasticsearch init failed (search degraded): %s", exc)

    # Phase 2: 初始化推送服务
    try:
        _app_state.push_manager = await PushManager.create(settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("PushManager init failed (push degraded): %s", exc)

    # Phase 3: 初始化推荐系统
    try:
        _app_state.recommend_service = RecommendationService(settings.redis_url)
        await _app_state.recommend_service.start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("RecommendationService init failed: %s", exc)

    # Phase 3: 初始化风控引擎
    try:
        _app_state.risk_engine = RiskEngine()
    except Exception as exc:  # noqa: BLE001
        logger.warning("RiskEngine init failed: %s", exc)

    # Phase 3: 初始化分析服务
    try:
        _app_state.analytics_service = AnalyticsService(settings.clickhouse_url)
        if app.state.pool is not None:
            await _app_state.analytics_service.initialize(app.state.pool)
    except Exception as exc:  # noqa: BLE001
        logger.warning("AnalyticsService init failed: %s", exc)

    # Phase 3: 初始化国际化服务
    try:
        _app_state.translation_manager = TranslationManager()
        _app_state.currency_converter = CurrencyConverter()
        _app_state.payment_router = LocalizedPaymentRouter()
    except Exception as exc:  # noqa: BLE001
        logger.warning("i18n services init failed: %s", exc)

    # Phase 4+: 初始化 AI 助手服务
    try:
        ai_config = AIConfig(
            provider=settings.ai_provider,
            model_name=settings.ai_model_name,
            api_key=settings.ai_api_key,
            api_base=settings.ai_api_base,
            temperature=settings.ai_temperature,
            max_tokens=settings.ai_max_tokens,
            embedding_model=settings.ai_embedding_model,
            top_k_retrieval=settings.ai_top_k_retrieval,
            similarity_threshold=settings.ai_similarity_threshold,
            max_history_turns=settings.ai_max_history_turns,
            timeout_seconds=settings.ai_timeout_seconds,
        )
        _app_state.ai_service = AIAssistantService(ai_config, app.state.pool)
        await _app_state.ai_service.initialize(app.state.pool)
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI Assistant Service init failed: %s", exc)

    try:
        app.state.pool = await init_db(settings)
    except Exception as exc:  # noqa: BLE001 - DB 不可达不阻止应用启动
        logger.warning(
            "database unavailable at startup; DB-backed endpoints will return 503: %s",
            exc,
        )
        app.state.pool = None

    # Phase 0: 更新全局状态供 health 模块访问
    _app_state.pool = app.state.pool

    app.state.ext_oservi = None
    if app.state.pool is not None:
        oservi = build_ext_oservi(app.state.pool, settings)
        oservi.start_cron_engines()
        app.state.ext_oservi = oservi

    logger.info("hemall backend started (version=%s)", __version__)
    yield

    oservi = getattr(app.state, "ext_oservi", None)
    if oservi is not None:
        oservi.stop()
        logger.info("ext oservi cron engines stopped (best-effort)")

    pool = getattr(app.state, "pool", None)
    if pool is not None:
        await pool.close()
        logger.info("database pool closed")

    # Phase 1: 关停 WebSocket 实时推送
    await realtime_manager.stop()

    # Phase 2: 关停各服务
    if _app_state.cache_manager:
        await _app_state.cache_manager.stop()
    if _app_state.search_client:
        await _app_state.search_client.close()

    # Phase 3: 关停推荐系统
    if _app_state.recommend_service:
        await _app_state.recommend_service.stop()

    # Phase 4+: 关停 AI 助手服务
    if _app_state.ai_service:
        await _app_state.ai_service.close()

    # Phase 3: 分析服务无需显式关闭


def create_app() -> FastAPI:
    """构建 FastAPI 应用。"""
    app = FastAPI(
        title="hemall commerce backend",
        version=__version__,
        description=(
            "3O 范式项目服务层 (§8): 把 platform/3O 的 oprim/oskill/omodul/obase/oservi "
            "元素库装配成可运行的 headless commerce 引擎。每个商务 omodul 暴露为 "
            "POST /<domain>/<omodul_name>, 请求体即其 Input 模型。"
        ),
        lifespan=lifespan,
    )

    # CORS: 允许前端 dev server (默认 localhost:3000) 跨域调用。JWT 走 Authorization
    # 头而非 cookie, 故 allow_credentials=False (与具体 origin 列表兼容)。
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Phase 0: 添加可观测性中间件 (顺序很重要：Metrics 在外层，Logging 在内层)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(LoggingMiddleware)

    # Phase 0 Week 2: 审计日志中间件 (记录写操作到数据库)
    app.add_middleware(AuditMiddleware)

    # Phase 0 Week 2: 限流
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Phase 0: 挂载健康检查路由
    app.include_router(health_router)

    # Phase 0: 暴露 Prometheus 指标端点
    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

    # Phase 0 Week 2: 挂载支付路由
    app.include_router(payment_router)

    # Phase 1: 挂载库存管理路由
    app.include_router(inventory_router)

    # Phase 1: 挂载订单生命周期路由
    app.include_router(orders_router)

    # Phase 1: 挂载 WebSocket 实时推送路由
    app.include_router(realtime_router)

    # Phase 2: 挂载 Elasticsearch 搜索路由
    app.include_router(search_router)

    # Phase 2: 挂载 Redis 缓存管理路由
    app.include_router(cache_router)

    # Phase 2: 挂载移动端推送路由
    app.include_router(push_router)

    # Phase 2: 挂载 CQRS 事件溯源路由
    app.include_router(eventsourcing_router)

    # Phase 3: 挂载智能推荐路由
    app.include_router(recommend_router)

    # Phase 3: 挂载风控引擎路由
    app.include_router(risk_router)

    # Phase 4+: 挂载 AI 助手路由
    app.include_router(ai_router)

    app.include_router(build_router())

    @app.get("/", tags=["system"], include_in_schema=False)
    async def root() -> RedirectResponse:
        """根路径友好跳转到 API 文档, 避免浏览器直连后端根时裸 404。"""
        return RedirectResponse(url="/docs")

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
