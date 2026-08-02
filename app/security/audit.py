"""审计日志 — 表 + 中间件 + 装饰器。

审计日志独立于业务日志 (structlog)，专门记录"谁、何时、对什么资源、做了什么操作、结果如何"。
用于合规审计 (等保 2.0 / GDPR)、安全事件溯源、运营分析。

审计日志表 (audit_log):
    id, actor_id, actor_type, action, resource_type, resource_id,
    request_method, request_path, request_body_hash,
    response_status, ip_address, user_agent,
    created_at, metadata (JSONB)

用法:
    # 1. 装饰器 (推荐 — 精确控制记录粒度)
    from app.security.audit import audit

    @router.post("/orders/complete_checkout")
    @audit(action="complete_checkout", resource_type="order")
    async def complete_checkout(request: Request, body: CheckoutInput, ...):
        ...

    # 2. 中间件 (自动记录所有 POST/PUT/DELETE 请求)
    from app.security.audit import AuditMiddleware
    app.add_middleware(AuditMiddleware)

    # 3. 手动记录
    from app.security.audit import log_audit_event
    await log_audit_event(
        pool=pool,
        actor_id="user-123",
        action="refund_approved",
        resource_type="order",
        resource_id="order-456",
        metadata={"amount": 99.99},
    )
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("hemall.security.audit")


# ── 数据库表 DDL ──────────────────────────────────────────────────────


AUDIT_LOG_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id TEXT,
    actor_type TEXT DEFAULT 'user',
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    request_method TEXT,
    request_path TEXT,
    request_body_hash TEXT,
    response_status INTEGER,
    ip_address TEXT,
    user_agent TEXT,
    duration_ms REAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'::jsonb
);

-- 常用查询索引
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_log(resource_type, resource_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
"""


# ── 核心记录函数 ─────────────────────────────────────────────────────


async def log_audit_event(
    pool: Any,
    *,
    actor_id: str | None = None,
    actor_type: str = "user",
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    request_method: str | None = None,
    request_path: str | None = None,
    request_body_hash: str | None = None,
    response_status: int | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    duration_ms: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """异步写入审计日志 (best-effort, 失败不阻塞业务)。"""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO audit_log (
                    actor_id, actor_type, action, resource_type, resource_id,
                    request_method, request_path, request_body_hash,
                    response_status, ip_address, user_agent, duration_ms, metadata
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8,
                    $9, $10, $11, $12, $13
                )
                """,
                actor_id,
                actor_type,
                action,
                resource_type,
                resource_id,
                request_method,
                request_path,
                request_body_hash,
                response_status,
                ip_address,
                user_agent,
                duration_ms,
                json.dumps(metadata or {}),
            )
    except Exception as exc:
        # 审计日志写入失败不应影响业务
        logger.warning("audit log write failed (non-fatal): %s", exc)


def _hash_body(body: bytes) -> str:
    """SHA-256 摘要请求体 (不存原文, 避免泄露敏感数据)。"""
    return hashlib.sha256(body).hexdigest()[:32]


def _extract_actor(request: Request) -> tuple[str | None, str]:
    """从 JWT 中提取 actor 信息。"""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        try:
            from ..deps import get_settings
            from obase.crypto.util import CryptoUtil

            settings = get_settings()
            payload = CryptoUtil.jwt_decode(
                token=auth[7:],
                secret=settings.jwt_secret,
                algorithms=[settings.jwt_algorithm],
            )
            return payload.get("sub"), "user"
        except Exception:
            pass
    return None, "anonymous"


# ── 装饰器 ───────────────────────────────────────────────────────────


def audit(
    action: str,
    resource_type: str | None = None,
    extract_resource_id: Callable[[Any], str | None] | None = None,
) -> Callable:
    """审计日志装饰器。

    用法:
        @router.post("/orders/complete")
        @audit(action="complete_checkout", resource_type="order")
        async def complete_order(request: Request, body: CheckoutInput, ...):
            result = ...
            return result
    """

    def decorator(func: Callable) -> Callable:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # 找到 Request 对象
            request: Request | None = None
            for arg in args:
                if isinstance(arg, Request):
                    request = arg
                    break
            if request is None:
                request = kwargs.get("request")

            start_time = time.perf_counter()
            result = None
            status = 200

            try:
                result = await func(*args, **kwargs)
                return result
            except Exception as exc:
                status = getattr(exc, "status_code", 500)
                raise
            finally:
                duration_ms = (time.perf_counter() - start_time) * 1000

                if request is not None:
                    try:
                        pool = getattr(request.app.state, "pool", None)
                        if pool is not None:
                            actor_id, actor_type = _extract_actor(request)
                            rid = None
                            if extract_resource_id and result is not None:
                                try:
                                    rid = extract_resource_id(result)
                                except Exception:
                                    pass

                            await log_audit_event(
                                pool,
                                actor_id=actor_id,
                                actor_type=actor_type,
                                action=action,
                                resource_type=resource_type or action,
                                resource_id=rid,
                                request_method=request.method,
                                request_path=request.url.path,
                                ip_address=(
                                    request.client.host if request.client else None
                                ),
                                user_agent=request.headers.get("user-agent"),
                                response_status=status,
                                duration_ms=round(duration_ms, 2),
                            )
                    except Exception as exc:
                        logger.warning("audit decorator failed (non-fatal): %s", exc)

        return wrapper

    return decorator


# ── 中间件 (自动记录写操作) ───────────────────────────────────────────


#: 需要审计的 HTTP 方法
_AUDITED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

#: 排除的路径前缀 (不需要审计)
_AUDIT_EXCLUDED_PREFIXES = ("/docs", "/redoc", "/openapi.json", "/health", "/metrics")


class AuditMiddleware(BaseHTTPMiddleware):
    """自动记录所有写操作的审计日志。

    与 LoggingMiddleware 和 MetricsMiddleware 协同工作:
    - LoggingMiddleware: 记录所有请求 (含 GET) 的运行日志
    - MetricsMiddleware: 采集性能指标
    - AuditMiddleware: 只记录写操作 (POST/PUT/PATCH/DELETE) 到数据库
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 只审计写操作 + 排除系统路径
        if request.method not in _AUDITED_METHODS or any(
            request.url.path.startswith(prefix) for prefix in _AUDIT_EXCLUDED_PREFIXES
        ):
            return await call_next(request)

        start_time = time.perf_counter()

        # 读取请求体用于 hash (注意: 只能读一次，需要缓存)
        body_hash = None
        try:
            body = await request.body()
            if body:
                body_hash = _hash_body(body)
        except Exception:
            pass

        response = await call_next(request)
        duration_ms = (time.perf_counter() - start_time) * 1000

        # best-effort 写审计日志
        try:
            pool = getattr(request.app.state, "pool", None)
            if pool is not None:
                actor_id, actor_type = _extract_actor(request)
                await log_audit_event(
                    pool,
                    actor_id=actor_id,
                    actor_type=actor_type,
                    action=f"{request.method} {request.url.path}",
                    resource_type=request.url.path.strip("/").split("/")[0],
                    request_method=request.method,
                    request_path=request.url.path,
                    request_body_hash=body_hash,
                    response_status=response.status_code,
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                    duration_ms=round(duration_ms, 2),
                )
        except Exception as exc:
            logger.warning("audit middleware failed (non-fatal): %s", exc)

        return response
