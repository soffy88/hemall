"""hemall 配置 — 环境变量驱动 (前缀 HEMALL_)。"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """服务层运行配置。全部可用 `HEMALL_<FIELD>` 环境变量或 .env 覆盖。"""

    model_config = SettingsConfigDict(
        env_prefix="HEMALL_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

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
    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_notify_url: str = ""

    # ── omodul 输出 (decision_trail / report 落盘) ─────────────────────
    output_root: Path = Path("./var/omodul_output")

    # ── provider 装配 (obase.ProviderRegistry generic category) ────────
    # 规范名须与 bootstrap 注册名一致: payment/fulfillment="manual", search/notification="log", tax="flat"
    default_payment_provider: str = "manual"
    default_fulfillment_provider: str = "manual"
    default_search_provider: str = "log"
    default_notification_provider: str = "log"
    default_tax_provider: str = "flat"
    # FlatRateTaxProvider 必填 rate_percent (无凭据 fallback 的固定税率)
    flat_tax_rate_percent: float = 10.0

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
