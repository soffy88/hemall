"""hemall FastAPI 依赖装配 — 连接池 / 鉴权主体 / 输出目录。

服务层职责 (§8): 拼 output_dir (含伪名化 user 引用) / 鉴权 / 取池。
算 fingerprint / 写 trail / 写 report 仍是 omodul 的事, 这里不替代。
"""

from __future__ import annotations

import asyncio
import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from obase.crypto.util import CryptoUtil
from obase.persistence.pool import PgPool

from .config import Settings

# Phase 0: 导入 Redis 指标辅助函数
from app.middleware.metrics import update_redis_connected

# ── 配置单例 ────────────────────────────────────────────────────────────


@lru_cache
def get_settings() -> Settings:
    """进程级配置单例。"""
    return Settings()


# ── 数据库池 ────────────────────────────────────────────────────────────


async def get_pool(settings: Settings = Depends(get_settings)) -> PgPool:
    """取启动期 bootstrap 注册好的连接池; 池不在 (DB 不可达) → 503。"""
    try:
        return PgPool.get(settings.pg_pool_name)
    except Exception as exc:  # noqa: BLE001 - 翻译为 HTTP 语义
        raise HTTPException(
            status_code=503, detail=f"database pool unavailable: {exc}"
        ) from exc


# ── Redis 连接 ──────────────────────────────────────────────────────────

_redis_connected = False


async def ensure_redis_connected(redis_url: str) -> None:
    """确保 Redis 连接并更新指标 (由 lifespan 调用)。"""
    global _redis_connected
    try:
        import redis.asyncio as redis
        client = redis.from_url(redis_url)
        await client.ping()
        await client.close()
        _redis_connected = True
        update_redis_connected(True)
    except Exception as e:
        _redis_connected = False
        update_redis_connected(False)


async def get_redis_connection(redis_url: str) -> Any:
    """获取 Redis 连接 (简单封装，实际应用中应使用连接池)。"""
    import redis.asyncio as redis
    return redis.from_url(redis_url)


# ── 鉴权主体 (JWT) ──────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


def anon_ref(identity: str, settings: Settings) -> str:
    """把用户标识伪名化为稳定不可逆引用 (§5.5.1)。

    同用户恒得同引用 (幂等去重/记轨迹不变), 但不可反推真实身份——
    omodul 的 fingerprint / decision_trail 因此不接触真实 PII。
    """
    return hashlib.sha256((settings.anon_salt + identity).encode("utf-8")).hexdigest()[
        :24
    ]


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """从 Bearer JWT 解析主体; 缺失/无效 → 401。"""
    if creds is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        payload = CryptoUtil.jwt_decode(
            token=creds.credentials,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc
    return {
        "user_id": payload.get("sub"),
        "email": payload.get("email"),
        "claims": payload,
    }


#: 公开端点 (登录/买家自助注册) 用的匿名主体。
ANONYMOUS_PRINCIPAL: dict[str, Any] = {
    "user_id": "anonymous",
    "email": None,
    "claims": {},
}


def get_current_customer(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """从 Bearer JWT 解析顾客主体 (typ=customer); 缺失/无效/类型不符 → 401。

    跟 get_current_user 共用同一份 JWT secret/algorithm, 靠 typ claim 区分顾客
    token 和管理员 token —— 防止顾客 token 被误当管理员主体使用 (反之亦然,
    虽然 get_current_user 本身不校验 typ, 但顾客端路由只认 get_current_customer)。
    """
    if creds is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        payload = CryptoUtil.jwt_decode(
            token=creds.credentials,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc
    if payload.get("typ") != "customer":
        raise HTTPException(status_code=401, detail="not a customer token")
    return {"customer_id": payload.get("customer_id"), "email": payload.get("email")}


def build_output_dir(
    settings: Settings, principal: dict[str, Any], omodul_name: str
) -> Path:
    """omodul 输出目录: <output_root>/<omodul_name>/<伪名化用户引用>。"""
    ref = anon_ref(str(principal.get("user_id") or "anonymous"), settings)
    return settings.output_root / omodul_name / ref
