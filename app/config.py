"""hemall 配置 — 环境变量驱动 (前缀 HEMALL_)。"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

#: 禁止在 production 下裸用的开发默认密钥 (须与下方 Settings 默认值保持一致)。
_INSECURE_DEFAULTS: dict[str, str] = {
    "jwt_secret": "change-me-in-production",
    "webhook_secret": "dev-webhook-secret-change-me",
    "hardware_secret": "dev-hardware-secret-change-me",
}


class Settings(BaseSettings):
    """服务层运行配置。全部可用 `HEMALL_<FIELD>` 环境变量或 .env 覆盖。"""

    model_config = SettingsConfigDict(
        env_prefix="HEMALL_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── 运行环境 ───────────────────────────────────────────────────────
    # development | staging | production。production 下 validate_production_security()
    # 强制拒绝弱默认密钥启动 (fail-closed); 非 production 保留开发默认值方便本地/CI。
    environment: str = "development"
    # 公开 (无鉴权) 创建 app_user (管理员) 的引导开关。默认关闭——create_user 需
    # 管理员 JWT。仅在首个管理员引导期临时置 true, 建成后立即关回 false。
    allow_public_admin_registration: bool = False

    # ── 数据库 (obase.persistence.PgPool) ──────────────────────────────
    pg_dsn: str = "postgresql://postgres:test@localhost:5432/hemall"
    pg_pool_name: str = "hemall"
    pg_pool_min: int = 2
    pg_pool_max: int = 10

    # ── Redis (DistributedLock 防超卖 / EventBus) ──────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── 鉴权 (obase.crypto.CryptoUtil JWT) ─────────────────────────────
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 720
    # 伪名化盐: 服务层把 user_id 转成稳定不可逆引用再传给 omodul (§5.5.1 禁真实 PII)
    anon_salt: str = "hemall-anon-salt"

    # ── 补天计划 Task 1.1: Webhook 验签密钥 (抖音/支付回调 HMAC-SHA256) ──
    # 生产环境必须用 HEMALL_WEBHOOK_SECRET 覆盖，与抖音/支付网关侧配置一致。
    webhook_secret: str = "dev-webhook-secret-change-me"

    # ── Phase 10: IoT 边桥防腐层专属密钥 (网桥 Bearer 凭据) ───────────
    # clearnode-iot-bridge (edge_bridge/) 调 /ext/hardware/webhook/* 时携带
    # Authorization: Bearer <此值>。与顾客/admin JWT 完全正交，机器对机器。
    hardware_secret: str = "dev-hardware-secret-change-me"

    # ── 补天计划 Task 2 / P0 冲刺: 真实收款网关 (微信 Native / Stripe) ──
    # provider: manual | wechat | stripe。沙盒/生产无缝切换 = 改这一个变量 +
    # 对应密钥，bootstrap 按此注册；真实密钥缺失时诚实回退 manual。
    payment_gateway_provider: str = "manual"
    # 微信支付 v3 (值可为文件路径或内联 PEM 内容；用 str 而非 Path——
    # pathlib 会把 base64 里的 // 折叠成 / 破坏密钥)
    wechat_pay_appid: str = ""
    wechat_pay_mchid: str = ""
    wechat_pay_serial_no: str = ""
    wechat_pay_private_key_path: str = ""
    wechat_pay_api_v3_key: str = ""
    wechat_pay_platform_cert_path: str = ""
    wechat_pay_notify_url: str = ""
    # 证书轮换引擎的持久化落盘路径 (JSON: serial_no/expire_time/pem)。
    # 轮换引擎每 12h 拉 /v3/certificates 热更新内存网关 + 写此文件，重启后
    # bootstrap 读文件继续用，消灭"到期运维手动更新"。
    wechat_platform_cert_store: str = "var/wechat_platform_cert.json"
    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_notify_url: str = ""

    # ── omodul 输出 (decision_trail / report 落盘) ─────────────────────
    output_root: Path = Path("./var/omodul_output")
    # 根日志级别 (INFO/WARNING/DEBUG)。引擎 tick 状态日志 (爬虫/证书轮换)
    # 依赖它不被 WARNING 默认值吞掉。
    log_level: str = "INFO"

    # ── provider 装配 (obase.ProviderRegistry generic category) ────────
    # 规范名须与 bootstrap 注册名一致: payment/fulfillment="manual", search/notification="log", tax="flat"
    default_payment_provider: str = "manual"
    default_fulfillment_provider: str = "manual"
    default_search_provider: str = "log"
    default_notification_provider: str = "log"
    default_tax_provider: str = "flat"
    # FlatRateTaxProvider 必填 rate_percent (无凭据 fallback 的固定税率)
    flat_tax_rate_percent: float = 10.0

    # ── 限流 (公开端点令牌桶) ──────────────────────────────────────────
    # 前置可信反代到公网之间的层数; >0 时按 X-Forwarded-For 倒数第 N 跳取真实
    # 对端 IP 做限流键。0 = 直连 (只认 ASGI 对端)。无可信反代切勿开启 (XFF 可伪造)。
    # 典型: 单层 Caddy/Nginx = 1。
    ratelimit_trusted_proxy_hops: int = 0

    # ── CORS (前端 dev server 跨域对接) ────────────────────────────────
    # 允许的前端来源; Next dev 默认 http://localhost:3000。env 覆盖用 JSON 数组。
    cors_origins: list[str] = ["http://localhost:3000"]

    # ── Phase 4+: AI 助手 (LLM) ───────────────────────────────────────
    ai_provider: str = "openai"  # openai / anthropic / local
    ai_model_name: str = "gpt-4"
    ai_api_key: str = ""
    ai_api_base: str = "https://api.openai.com/v1"
    ai_temperature: float = 0.7
    ai_max_tokens: int = 1000
    ai_embedding_model: str = "text-embedding-ada-002"
    ai_top_k_retrieval: int = 5
    ai_similarity_threshold: float = 0.7
    ai_max_history_turns: int = 10
    ai_timeout_seconds: int = 30

    # ── Elasticsearch (搜索层; 缺省时搜索降级) ─────────────────────────
    # 二选一: ELASTICSEARCH_URL 环境变量 / 这两个字段之一。留空则用代码内默认地址。
    elasticsearch_url: str = ""
    hemall_elasticsearch_url: str = ""
    es_index_name: str = "hemall_products"
    es_autocomplete_index: str = "hemall_autocomplete"
    es_max_connections: int = 10
    es_request_timeout: int = 30

    # ── 推送 (FCM / APNs; 缺省时推送降级为 sandbox) ────────────────────
    firebase_credentials: str = ""  # service-account JSON 路径或内联 JSON
    apns_bundle_id: str = ""
    apns_cert_base: str = ""  # APNs 证书路径/base
    apns_key_id: str = ""
    apns_team_id: str = ""
    apns_sandbox: str = "1"  # "1"=sandbox 网关, 生产置 "0" (代码按字符串 "1" 判定)

    # ── 分析 (ClickHouse; 缺省时降级为 PostgreSQL 查询) ────────────────
    clickhouse_url: str = ""

    # ── 事件溯源 (CQRS event store; 每 N 个版本创建一次快照) ──────────
    event_snapshot_threshold: int = 10

    # ── 生产环境安全基线 (fail-closed) ─────────────────────────────────

    def validate_production_security(self) -> None:
        """production 环境启动前的安全基线校验; 不满足直接抛错拒绝启动。

        由 lifespan 在装配早期调用。development/staging 下不做校验 (返回)，
        以免打断本地/CI (它们本就用开发默认密钥)。
        """
        if self.environment.lower() != "production":
            return

        import os

        problems: list[str] = []
        for field, insecure in _INSECURE_DEFAULTS.items():
            if getattr(self, field) == insecure:
                problems.append(
                    f"HEMALL_{field.upper()} 仍是开发默认值 (必须显式设置强密钥)"
                )
        if "test@" in self.pg_dsn or self.pg_dsn.startswith(
            "postgresql://postgres:test@"
        ):
            problems.append("HEMALL_PG_DSN 仍是开发默认凭据 (postgres:test)")
        if not os.getenv("ENCRYPTION_KEY"):
            problems.append(
                "ENCRYPTION_KEY 未设置 (缺失时每次重启生成新 Fernet key, 已加密 PII 将无法解密)"
            )

        if problems:
            raise RuntimeError(
                "生产环境安全基线未满足, 拒绝启动:\n  - "
                + "\n  - ".join(problems)
                + "\n(如为非生产部署, 请设 HEMALL_ENVIRONMENT=development/staging)"
            )
