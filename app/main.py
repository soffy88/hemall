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

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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
from .observability.health import check_redis_health
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
from .recommend.models import ProductFeatures
from . import queries
from .risk.engine import RiskEngine
from .risk.router import router as risk_router
from .ai_assistant.router import router as ai_router
from .ai_assistant.models import AIConfig
from .ai_assistant.service import AIAssistantService

# Phase 8 Task 2: LLM 智能体运维中枢
from .ext.admin_agent_chat import router as agent_chat_router


from .cache.service import CacheManager

from .routers import build_router, public_zero_login_paths

# 补天计划 Task 1.1: Webhook 验签装甲 (HMAC-SHA256, ASGI 层强制)
from .security.webhook import WebhookSignatureMiddleware

# 补天计划 Task 1.2: 零登录公开端点令牌桶限流 (IP + Device_ID)
from .middleware.ratelimit import TokenBucketRateLimitMiddleware

# Phase 0 Week 2: 限流
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from .security.rate_limit import limiter

# Phase 0 Week 2: 审计日志中间件
from .security.audit import AuditMiddleware

# Phase 0: 配置结构化日志 (进程启动期一次)
configure_structlog()

logger = logging.getLogger("hemall.main")


# ── 门店落地页 (根路径) ────────────────────────────────────────────────


_LANDING_CSS = """
:root { color-scheme: light dark; }
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  min-height: 100vh;
  display: flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
  color: #e2e8f0;
  padding: 2rem 1rem;
}
.card {
  width: 100%; max-width: 560px;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 16px;
  padding: 2.5rem 2rem;
  backdrop-filter: blur(8px);
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.35);
}
.brand { font-size: 1.6rem; font-weight: 700; letter-spacing: 0.02em; }
.brand em { font-style: normal; color: #38bdf8; }
.subtitle { margin-top: 0.4rem; color: #94a3b8; font-size: 0.9rem; }
.badge {
  display: inline-block; margin-top: 1.2rem; padding: 0.3rem 0.8rem;
  border-radius: 999px; font-size: 0.8rem; font-weight: 600;
}
.badge.healthy { background: rgba(34, 197, 94, 0.15); color: #4ade80; }
.badge.degraded { background: rgba(234, 179, 8, 0.15); color: #facc15; }
.status {
  display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 1.6rem;
}
.status-item {
  background: rgba(0, 0, 0, 0.25);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 12px; padding: 0.9rem 1rem;
  display: flex; align-items: center; gap: 0.6rem;
}
.status-item .label { color: #94a3b8; font-size: 0.8rem; }
.status-item .value { font-size: 0.95rem; font-weight: 600; }
.status-item .stack { display: flex; flex-direction: column; gap: 0.15rem; }
.dot { width: 10px; height: 10px; border-radius: 50%; flex: 0 0 auto; }
.dot-up { background: #22c55e; box-shadow: 0 0 8px rgba(34, 197, 94, 0.7); }
.dot-down { background: #ef4444; box-shadow: 0 0 8px rgba(239, 68, 68, 0.7); }
.links { margin-top: 1.8rem; display: flex; flex-wrap: wrap; gap: 0.7rem; }
.links a {
  color: #e2e8f0; text-decoration: none; font-size: 0.85rem;
  padding: 0.5rem 1rem; border-radius: 8px;
  background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.3);
  transition: background 0.2s ease;
}
.links a:hover { background: rgba(56, 189, 248, 0.25); }
.footer { margin-top: 1.8rem; color: #64748b; font-size: 0.75rem; }
"""


def _render_landing(
    version: str,
    badge: str,
    status_class: str,
    db_dot: str,
    db_text: str,
    redis_dot: str,
    redis_text: str,
) -> str:
    """渲染根路径门店落地页 HTML (自包含, 无外部依赖)。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hemall 商城</title>
<style>{_LANDING_CSS}</style>
</head>
<body>
  <main class="card">
    <div class="brand">Hemall <em>商城</em></div>
    <div class="subtitle">Headless Commerce 服务端 · v{version}</div>
    <span class="badge {status_class}">{badge}</span>
    <div class="status">
      <div class="status-item">{db_dot}<div class="stack">
        <span class="label">数据库</span><span class="value">{db_text}</span>
      </div></div>
      <div class="status-item">{redis_dot}<div class="stack">
        <span class="label">Redis</span><span class="value">{redis_text}</span>
      </div></div>
    </div>
    <nav class="links">
      <a href="/store/products">商品目录</a>
      <a href="/store/regions">配送区域</a>
      <a href="/health/ready">健康检查</a>
      <a href="/docs">API 文档</a>
    </nav>
    <div class="footer">mall.sxueji.com · 由 Cloudflare Tunnel 提供公网访问</div>
  </main>
</body>
</html>"""


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


def _recommend_price_bucket(min_price_cents: int | None) -> str:
    """按真实售价分档，满足 ProductFeatures.price_range 必填字段。"""
    if min_price_cents is None:
        return "mid"
    if min_price_cents < 10000:
        return "budget"
    if min_price_cents < 50000:
        return "mid"
    if min_price_cents < 200000:
        return "high"
    return "premium"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动装配 + 关停清理。"""
    settings = get_settings()
    app.state.config = settings

    # 安全基线 (fail-closed): production 下裸用开发默认密钥 / 缺 ENCRYPTION_KEY 直接拒启动。
    # development/staging 无操作，不影响本地/CI。
    settings.validate_production_security()

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

    # Phase 3: 初始化国际化服务
    try:
        _app_state.translation_manager = TranslationManager()
        _app_state.currency_converter = CurrencyConverter()
        _app_state.payment_router = LocalizedPaymentRouter()
    except Exception as exc:  # noqa: BLE001
        logger.warning("i18n services init failed: %s", exc)

    metrics_task: asyncio.Task | None = None
    try:
        app.state.pool, metrics_task = await init_db(settings)
    except Exception as exc:  # noqa: BLE001 - DB 不可达不阻止应用启动
        logger.warning(
            "database unavailable at startup; DB-backed endpoints will return 503: %s",
            exc,
        )
        app.state.pool = None

    # Phase 0: 更新全局状态供 health 模块访问
    _app_state.pool = app.state.pool

    # Phase 3: 初始化分析服务 (依赖 app.state.pool, 须在 init_db 之后)
    try:
        _app_state.analytics_service = AnalyticsService(settings.clickhouse_url)
        if app.state.pool is not None:
            await _app_state.analytics_service.initialize(app.state.pool)
    except Exception as exc:  # noqa: BLE001
        logger.warning("AnalyticsService init failed: %s", exc)

    # Phase 4+: 初始化 AI 助手服务 (依赖 app.state.pool, 须在 init_db 之后)
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

    # Phase 3: 用真实商城目录数据灌入推荐引擎 (否则 /recommend/* 一直返回空列表)
    if app.state.pool is not None and _app_state.recommend_service is not None:
        try:
            products = await queries.list_storefront_products(app.state.pool, limit=500)
            features = [
                ProductFeatures(
                    product_id=p["id"],
                    category_id=p.get("category_id") or "uncategorized",
                    brand_id=None,
                    price_range=_recommend_price_bucket(p.get("min_price_cents")),
                    tags=[],
                    min_price_cents=p.get("min_price_cents"),
                    max_price_cents=p.get("min_price_cents"),
                    avg_rating=0.0,
                    review_count=0,
                    sold_count=0,
                )
                for p in products
            ]
            await _app_state.recommend_service.register_product_features(features)
            await _app_state.recommend_service.train()
        except Exception as exc:  # noqa: BLE001
            logger.warning("recommend product feature seeding failed: %s", exc)

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

    if metrics_task is not None:
        metrics_task.cancel()

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
    # gunicorn 启动路径 (生产) 不走 __main__ 的 basicConfig, root logger 默认
    # WARNING——引擎 tick 状态日志 (spider/证书轮换等 INFO 级) 会被吞。这里统一
    # 按 Settings.log_level 配置, 开发/生产日志口径一致。
    logging.basicConfig(level=get_settings().log_level.upper())

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

    # 补天计划 Task 1.2: 零登录公开端点令牌桶限流 (最后添加 = 最外层网关)。
    # 精确路径来自 ext registry 公开端点推导 + 手写公开端点；/store/* 前缀
    # 覆盖整个零登录商城前端 (加车/结算/下单/查库存)。
    app.add_middleware(
        TokenBucketRateLimitMiddleware,
        public_paths=public_zero_login_paths(),
        path_prefixes={"/store/"},
        trusted_proxy_hops=settings.ratelimit_trusted_proxy_hops,
    )

    # 补天计划 Task 1.1: Webhook 验签装甲 (最外层)——抖音/支付回调在 ASGI 层
    # 强制 HMAC-SHA256 验签，非法请求 403 丢弃，绝不触碰 omodul 层。
    # 路径→校验器按路径注册：真实微信模式 (HEMALL_PAYMENT_GATEWAY_PROVIDER
    # =wechat) 下 /payments/wechat/notify 改由平台证书验签 (WechatPayNative
    # Gateway.verify_callback 在 handler 内完成)，不再套本域 HMAC 契约。
    protected_webhook_paths = {
        "/payments/alipay/notify",
        "/growth/douyin_callback",
    }
    if settings.payment_gateway_provider != "wechat":
        protected_webhook_paths.add("/payments/wechat/notify")
    app.add_middleware(
        WebhookSignatureMiddleware,
        secret=settings.webhook_secret,
        protected_paths=protected_webhook_paths,
    )

    # Phase 0 Week 2: 限流
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Phase 0: 挂载健康检查路由
    app.include_router(health_router)

    # Phase 0: 暴露 Prometheus 指标端点
    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

    # 智能体传货媒体 (agent_ingest 输出): /media/agent_ingest/<user>/<file>
    from fastapi.staticfiles import StaticFiles

    # 实际保存路径: <output_root>/ext_bespoke/agent_ingest/agent_ingest/<user>/<file>
    ingest_root = settings.output_root / "ext_bespoke" / "agent_ingest" / "agent_ingest"
    ingest_root.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/media/agent_ingest", StaticFiles(directory=ingest_root), name="agent_ingest"
    )

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

    # Phase 8 Task 2: LLM 智能体运维中枢 (/admin/agent-chat)
    app.include_router(agent_chat_router)

    app.include_router(build_router())

    @app.get("/", tags=["system"], include_in_schema=False)
    async def root(request: Request) -> HTMLResponse:
        """门店落地页 — 无独立前端时展示服务状态与入口 (不跳 /docs)。"""
        pool = getattr(app.state, "pool", None)
        settings = getattr(app.state, "config", None)

        db_up = pool is not None
        redis_up = False
        if settings is not None:
            try:
                redis_up = (await check_redis_health(settings.redis_url))[
                    "status"
                ] == "up"
            except Exception:  # noqa: BLE001 - 落地页状态降级显示
                redis_up = False

        status = "healthy" if (db_up and redis_up) else "degraded"
        badge = "运行正常" if status == "healthy" else "部分降级"

        def _dot(up: bool) -> str:
            return (
                '<span class="dot dot-up"></span>'
                if up
                else '<span class="dot dot-down"></span>'
            )

        html = _render_landing(
            version=__version__,
            badge=badge,
            status_class=status,
            db_dot=_dot(db_up),
            db_text="已连接" if db_up else "未连接",
            redis_dot=_dot(redis_up),
            redis_text="已连接" if redis_up else "未连接",
        )
        return HTMLResponse(content=html, status_code=200)

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
