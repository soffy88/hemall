"""hemall 配置 — 环境变量驱动 (前缀 HEMALL_)。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 禁止在 production 下裸用的开发默认密钥 (须与下方 Settings 默认值保持一致)。
_INSECURE_DEFAULTS: dict[str, str] = {
    "jwt_secret": "change-me-in-production",
    "webhook_secret": "dev-webhook-secret-change-me",
    "hardware_secret": "dev-hardware-secret-change-me",
}

#: 支持 *_FILE 后缀读取密钥文件的字段映射 (env 名 -> settings 字段名)
_SECRET_FILE_MAP: dict[str, str] = {
    "HEMALL_JWT_SECRET_FILE": "jwt_secret",
    "HEMALL_PG_PASSWORD_FILE": "pg_password",
    "HEMALL_PG_DSN_FILE": "pg_dsn",
    "ENCRYPTION_KEY_FILE": "encryption_key",
    "HEMALL_WEBHOOK_SECRET_FILE": "webhook_secret",
    "HEMALL_HARDWARE_SECRET_FILE": "hardware_secret",
    "HEMALL_WECHAT_PAY_API_V3_KEY_FILE": "wechat_pay_api_v3_key",
    "HEMALL_WECHAT_PAY_PRIVATE_KEY_FILE": "wechat_pay_private_key_path",
    "HEMALL_WECHAT_PAY_PLATFORM_CERT_FILE": "wechat_pay_platform_cert_path",
    "HEMALL_STRIPE_SECRET_KEY_FILE": "stripe_secret_key",
    "HEMALL_STRIPE_WEBHOOK_SECRET_FILE": "stripe_webhook_secret",
    "HEMALL_REDIS_URL_FILE": "redis_url",
}


def _read_secret_file(file_path: str) -> str:
    """读取密钥文件内容，去除首尾空白。"""
    path = Path(file_path)
    if not path.is_file():
        raise RuntimeError(f"Secret file not found: {file_path}")
    return path.read_text(encoding="utf-8").strip()


def _load_secrets_from_files(values: dict[str, Any]) -> dict[str, Any]:
    """从 *_FILE 环境变量加载密钥到对应字段。

    优先级: 显式环境变量 > *_FILE > 默认值。
    只在字段未通过环境变量显式设置时才读取文件。
    """
    # 获取实际的环境变量 (不带前缀处理，直接读 os.environ)
    for env_name, field_name in _SECRET_FILE_MAP.items():
        file_env = os.getenv(env_name)
        if not file_env:
            continue
        # 检查对应的直接环境变量是否已设置 (HEMALL_ 前缀 + 大写字段名)
        direct_env = f"HEMALL_{field_name.upper()}"
        if os.getenv(direct_env):
            continue  # 显式环境变量优先
        try:
            values[field_name] = _read_secret_file(file_env)
        except RuntimeError as e:
            raise RuntimeError(f"Failed to load secret from {env_name}: {e}") from e
    return values


class Settings(BaseSettings):
    """服务层运行配置。全部可用 `HEMALL_<FIELD>` 环境变量或 .env 覆盖。

    额外支持 *_FILE 后缀环境变量 (如 HEMALL_JWT_SECRET_FILE=/run/secrets/jwt_secret)
    读取 Docker secrets 或文件挂载的密钥。显式环境变量优先于 *_FILE。
    """

    model_config = SettingsConfigDict(
        env_prefix="HEMALL_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    @model_validator(mode="before")
    @classmethod
    def _load_file_secrets(cls, values: dict[str, Any]) -> dict[str, Any]:
        return _load_secrets_from_files(values)

    # ── 运行环境 ───────────────────────────────────────────────────────
    # development | staging | production。production 下 validate_production_security()
    # 强制拒绝弱默认密钥启动 (fail-closed); 非 production 保留开发默认值方便本地/CI。
    environment: str = "development"
    # 公开 (无鉴权) 创建 app_user (管理员) 的引导开关。默认关闭——create_user 需
    # 管理员 JWT。仅在首个管理员引导期临时置 true, 建成后立即关回 false。
    allow_public_admin_registration: bool = False

    # ── 数据库 (obase.persistence.PgPool) ──────────────────────────────
    pg_dsn: str = "postgresql://postgres:test@localhost:5432/hemall"
    pg_password: str = ""  # 单独的密码字段，支持 HEMALL_PG_PASSWORD_FILE
    pg_sslmode: str = "prefer"  # disable|allow|prefer|require|verify-ca|verify-full
    pg_pool_name: str = "hemall"
    pg_pool_min: int = 2
    pg_pool_max: int = 10

    # ── Redis (DistributedLock 防超卖 / EventBus) ──────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── 加密 (Fernet PII 加密) ───────────────────────────────────────────
    encryption_key: str = ""  # 支持 ENCRYPTION_KEY_FILE

    # ── 鉴权 (obase.crypto.CryptoUtil JWT) ─────────────────────────────
    jwt_secret: str = "change-me-in-production"
    # Comma-separated app_user IDs allowed to perform privileged staff operations.
    privileged_staff_ids: str = ""
    # Comma-separated app_user IDs granted the `support` role — 可读后台 PII
    # (顾客/员工/地址/礼品卡) 但不可执行管理员专属写操作。空 = 无人是 support。
    support_staff_ids: str = ""
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
    # 微信视频号（channels）access_token。社交播报引擎的凭据——刻意不给默认值，
    # 留空表示"未接入"，由调用方显式传下去，别让硬编码占位符进生产。
    wechat_channel_access_token: str = ""
    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_notify_url: str = ""

    # ── omodul 输出 (decision_trail / report 落盘) ─────────────────────
    output_root: Path = Path("./var/omodul_output")
    # 根日志级别 (INFO/WARNING/DEBUG)。引擎 tick 状态日志 (爬虫/证书轮换)
    # 依赖它不被 WARNING 默认值吞掉。
    log_level: str = "INFO"

    @property
    def effective_pg_dsn(self) -> str:
        """返回带 sslmode 的有效 DSN。

        - 如果 pg_dsn 已包含 sslmode 参数，直接返回。
        - 否则根据 pg_sslmode 追加 sslmode 参数。
        - pg_password 如果设置，会替换 DSN 中的密码部分。
        """
        dsn = self.pg_dsn

        # 替换密码 (如果 pg_password 单独设置)
        if self.pg_password and "://" in dsn:
            from urllib.parse import urlparse, urlunparse

            parsed = urlparse(dsn)
            if parsed.password is not None or self.pg_password:
                # 重建 netloc with new password
                user = parsed.username or "postgres"
                host_port = parsed.hostname
                if parsed.port:
                    host_port += f":{parsed.port}"
                new_netloc = f"{user}:{self.pg_password}@{host_port}"
                dsn = urlunparse(parsed._replace(netloc=new_netloc))

        # 添加 sslmode (如果 DSN 中没有)
        if "sslmode=" not in dsn:
            separator = "&" if "?" in dsn else "?"
            dsn = f"{dsn}{separator}sslmode={self.pg_sslmode}"

        return dsn

    @field_validator("pg_sslmode")
    @classmethod
    def _validate_sslmode(cls, v: str) -> str:
        allowed = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
        if v not in allowed:
            raise ValueError(f"pg_sslmode must be one of {allowed}")
        return v

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
    # AI 成本闸: /ai/* 每个端点都真金白银调 LLM。鉴权只回答"是不是员工"，
    # 不回答"这个员工今天烧了多少"——按已认证主体的双窗口配额。
    ai_quota_calls_per_minute: int = 10
    ai_quota_calls_per_hour: int = 120
    # 单条 prompt 长度上限: token 按字符计价，超长既是成本炸弹也会把 RAG
    # 上下文顶出窗口。
    ai_max_message_chars: int = 8000
    # 进程内会话上限: _sessions 只增不减会让内存被慢慢吃光。
    ai_max_sessions: int = 1000

    # ── Elasticsearch (搜索层; 缺省时搜索降级) ─────────────────────────
    # 二选一: ELASTICSEARCH_URL 环境变量 / 这两个字段之一。留空则用代码内默认地址。
    elasticsearch_url: str = ""
    hemall_elasticsearch_url: str = ""
    elasticsearch_username: str = ""
    elasticsearch_password: str = ""
    elasticsearch_ca_cert: str = ""
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
        for field in ("jwt_secret", "webhook_secret", "hardware_secret"):
            value = str(getattr(self, field) or "")
            if len(value.encode("utf-8")) < 32:
                problems.append(
                    f"HEMALL_{field.upper()} 必须至少 32 字节，避免弱 HMAC 密钥"
                )
        if "test@" in self.pg_dsn or self.pg_dsn.startswith(
            "postgresql://postgres:test@"
        ):
            problems.append("HEMALL_PG_DSN 仍是开发默认凭据 (postgres:test)")

        # 生产环境连接非本机/非容器内网 PG 时，默认要求 sslmode=require
        # 判断：如果 host 不是 localhost/127.0.0.1/::1 且不是 docker 内部网络 (db/redis 等服务名)
        # 且 sslmode 不是 require/verify-ca/verify-full，则报错
        from urllib.parse import urlparse

        try:
            parsed = urlparse(self.pg_dsn)
            host = parsed.hostname or ""
            # 常见的内部/本地主机名模式
            internal_host_patterns = (
                "localhost",
                "127.0.0.1",
                "::1",
                "postgres",
                "db",
                "database",
                "postgresql",
            )
            is_local = host in internal_host_patterns
            # Docker Compose 服务名通常不含点号，或以 .local/.internal 结尾
            is_docker_internal = (
                "." not in host
                or host.endswith(".local")
                or host.endswith(".internal")
                or host.endswith(".svc.cluster.local")
            )
            is_internal = is_local or is_docker_internal

            if not is_internal:
                effective_sslmode = self.pg_sslmode
                # 检查 DSN 中是否已显式设置 sslmode
                if "sslmode=" in self.pg_dsn:
                    import re

                    m = re.search(r"sslmode=([^&]+)", self.pg_dsn)
                    if m:
                        effective_sslmode = m.group(1)
                if effective_sslmode not in ("require", "verify-ca", "verify-full"):
                    problems.append(
                        f"HEMALL_PG_SSLMODE 必须为 require/verify-ca/verify-full "
                        f"当连接外部数据库 (host={host})，当前值: {effective_sslmode}"
                    )
        except Exception:
            # 解析失败时不阻塞，由 asyncpg 连接时报错
            pass

        if "*" in self.cors_origins:
            problems.append(
                "HEMALL_CORS_ORIGINS 含通配 '*' (生产必须收紧为具体来源列表)"
            )

        if not self.privileged_staff_ids.strip():
            problems.append("HEMALL_PRIVILEGED_STAFF_IDS must list at least one privileged app_user ID")

        provider = self.payment_gateway_provider.strip().lower()
        if provider not in {"wechat", "stripe"}:
            problems.append(
                "HEMALL_PAYMENT_GATEWAY_PROVIDER 必须是已配置的真实网关 wechat 或 stripe；"
                "production 禁止 manual 或未知 provider"
            )
        elif provider == "wechat":
            required = {
                "wechat_pay_appid": self.wechat_pay_appid,
                "wechat_pay_mchid": self.wechat_pay_mchid,
                "wechat_pay_serial_no": self.wechat_pay_serial_no,
                "wechat_pay_private_key_path": self.wechat_pay_private_key_path,
                "wechat_pay_api_v3_key": self.wechat_pay_api_v3_key,
                "wechat_pay_platform_cert_path": self.wechat_pay_platform_cert_path,
            }
            missing = [name for name, value in required.items() if not str(value).strip()]
            if missing:
                problems.append(
                    "微信支付生产配置缺失: " + ", ".join(missing)
                )
        elif provider == "stripe":
            required = {
                "stripe_secret_key": self.stripe_secret_key,
                "stripe_webhook_secret": self.stripe_webhook_secret,
            }
            missing = [name for name, value in required.items() if not str(value).strip()]
            if missing:
                problems.append(
                    "Stripe 生产配置缺失: " + ", ".join(missing)
                )

        es_url = self.elasticsearch_url or self.hemall_elasticsearch_url
        if es_url.lower().startswith("https://"):
            es_missing = [
                name
                for name, value in {
                    "elasticsearch_username": self.elasticsearch_username,
                    "elasticsearch_password": self.elasticsearch_password,
                    "elasticsearch_ca_cert": self.elasticsearch_ca_cert,
                }.items()
                if not str(value).strip()
            ]
            if es_missing:
                problems.append(
                    "Elasticsearch HTTPS 生产认证配置缺失: " + ", ".join(es_missing)
                )

        encryption_key = self.encryption_key
        if not encryption_key:
            problems.append(
                "ENCRYPTION_KEY 未设置 (缺失时每次重启生成新 Fernet key, 已加密 PII 将无法解密)"
            )
        else:
            try:
                from cryptography.fernet import Fernet

                Fernet(encryption_key.encode("ascii"))
            except (ValueError, TypeError, UnicodeEncodeError):
                problems.append("ENCRYPTION_KEY 必须是有效的 Fernet base64 key")

        # 遗留支付 SDK (app/payments/{wechat,alipay}.py) 的生产分支是 8 处
        # NotImplementedError: 回调不验签 → 支付状态永不回写 / 任何人都能把订单
        # 标 paid; 退款必 500。真正的收款走 HEMALL_PAYMENT_GATEWAY_PROVIDER
        # + app/ext/payment_gateways.py。生产下配了这两个遗留开关 = 选到了
        # 未实现的分支，必须在启动期就拒绝。
        for legacy_env, provider_cls in (
            ("WECHAT_PAY_MCH_ID", "WeChatPayProvider"),
            ("ALIPAY_APP_ID", "AlipayProvider"),
        ):
            if os.getenv(legacy_env, "").strip():
                problems.append(
                    f"{legacy_env} 已设置，但 app/payments 的遗留 {provider_cls} 生产分支"
                    f"（回调验签 / 查单 / 退款）全部是 NotImplementedError；"
                    f"生产收款必须走 HEMALL_PAYMENT_GATEWAY_PROVIDER"
                    f"（当前={self.payment_gateway_provider.strip().lower()}）"
                )

        if problems:
            raise RuntimeError(
                "生产环境安全基线未满足, 拒绝启动:\n  - "
                + "\n  - ".join(problems)
                + "\n(如为非生产部署, 请设 HEMALL_ENVIRONMENT=development/staging)"
            )
