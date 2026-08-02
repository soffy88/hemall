"""登录/注册 API 限流 — 基于 slowapi + Redis 分布式存储。

Phase 1 升级: 从内存态切换为 Redis 后端, 支持多实例分布式限流。
Redis 不可用时自动降级为内存态 (单实例仍可工作)。

用法:
    from app.security.rate_limit import limiter, RATE_LIMIT_EXCEEDED_HANDLER

    # 在 main.py 中:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, RATE_LIMIT_EXCEEDED_HANDLER)

    # 在路由中:
    @router.post("/login")
    @limiter.limit("5/minute")
    async def login(request: Request, ...):
        ...
"""

from __future__ import annotations

import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

logger = logging.getLogger("hemall.security.rate_limit")


def _resolve_storage_uri() -> str:
    """解析限流存储后端 URI。

    优先级:
        1. 环境变量 RATE_LIMIT_STORAGE_URI (显式配置)
        2. REDIS_URL (复用已有 Redis 配置)
        3. memory:// (降级, 单实例)

    生产环境必须配置 Redis, 否则多实例部署时限流会失效。
    """
    explicit = os.getenv("RATE_LIMIT_STORAGE_URI")
    if explicit:
        return explicit

    redis_url = os.getenv("REDIS_URL") or os.getenv("HEMALL_REDIS_URL")
    if redis_url:
        # slowapi 的 Redis 存储需要 redis:// 前缀
        if not redis_url.startswith("redis://"):
            redis_url = f"redis://{redis_url}"
        return redis_url

    logger.warning(
        "rate limiter using in-memory storage (NOT for production multi-instance)"
    )
    return "memory://"


def _key_func(request: Request) -> str:
    """限流键: 优先按用户 (Bearer token 中的 sub), 否则按 IP。"""
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
            return f"user:{payload.get('sub', 'anon')}"
        except Exception:
            pass
    return get_remote_address(request)


#: 全局限流器实例 (Redis 后端, 不可用时降级为内存)
limiter = Limiter(
    key_func=_key_func,
    default_limits=["100/minute"],
    storage_uri=_resolve_storage_uri(),
)


async def RATE_LIMIT_EXCEEDED_HANDLER(
    request: Request, exc: RateLimitExceeded
) -> JSONResponse:
    """限流超限时的标准响应 (429 Too Many Requests)。"""
    return JSONResponse(
        status_code=429,
        content={
            "detail": f"rate limit exceeded: {exc.detail}",
            "retry_after": exc.detail.split("per ")[-1] if "per " in exc.detail else "60s",
        },
    )


def is_redis_backend() -> bool:
    """检查限流器是否使用 Redis 后端。"""
    return not limiter._storage_uri.startswith("memory://")
