"""hemall FastAPI 依赖装配 — 连接池 / 鉴权主体 / 输出目录。

服务层职责 (§8): 拼 output_dir (含伪名化 user 引用) / 鉴权 / 取池。
算 fingerprint / 写 trail / 写 report 仍是 omodul 的事, 这里不替代。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from obase.persistence.pool import PgPool

# Phase 0: 导入 Redis 指标辅助函数
from app.middleware.metrics import update_redis_connected

from .config import Settings
from .security.tokens import (
    ACCESS_TOKEN_COOKIE,
    TOKEN_TYPE_CUSTOMER,
    TOKEN_TYPE_STAFF,
    decode_token,
)

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
    except Exception:
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


def _token_from_request(
    request: Request,
    creds: HTTPAuthorizationCredentials | None,
) -> str | None:
    """取会话 token：Authorization Bearer 优先，否则回退 HttpOnly 会话 cookie。

    F-01/F-03: 浏览器经同源 BFF 反代访问时只带 cookie（前端 JS 接触不到 token）；
    API 客户端继续用 Bearer 头。两条路径最终都在这里按 typ 白名单解码。
    """
    if creds is not None:
        return creds.credentials
    return request.cookies.get(ACCESS_TOKEN_COOKIE)


def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """从 Bearer JWT 或会话 cookie 解析员工主体; 缺失/无效/类型不符 → 401。

    类型白名单只认 ``typ=staff``——顾客/能力令牌即使从 cookie 进来也一律 401。
    """
    token = _token_from_request(request, creds)
    if token is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        payload = decode_token(
            token=token,
            typ=TOKEN_TYPE_STAFF,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
    except Exception as exc:  # noqa: BLE001 - 翻译为 HTTP 语义
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc
    if not isinstance(payload.get("sub"), str) or not payload["sub"].strip():
        raise HTTPException(status_code=401, detail="staff token missing subject")
    return {
        "user_id": payload["sub"],
        "email": payload.get("email"),
        "claims": payload,
    }


#: 公开端点 (登录/买家自助注册) 用的匿名主体。
ANONYMOUS_PRINCIPAL: dict[str, Any] = {
    "user_id": "anonymous",
    "email": None,
    "claims": {},
}


def assert_staff_role(
    principal: dict[str, Any], allowed_roles: Iterable[str]
) -> dict[str, Any]:
    """白名单检查角色 claim；不在集合内 → 403。返回 principal 便于链式使用。

    角色集合是**白名单**：没有 role claim、role 是未知字符串、role 拼错，
    一律 403。想要"默认放行"的端点请显式把角色名写进 allowed_roles。
    """
    if principal["claims"].get("role") not in frozenset(allowed_roles):
        raise HTTPException(status_code=403, detail="insufficient staff role")
    return principal


def require_staff_role(*allowed_roles: str):
    """Require a staff token carrying one of the explicitly allowed role claims."""
    allowed = frozenset(allowed_roles)

    async def _require_role(principal: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        return assert_staff_role(principal, allowed)

    return _require_role


def get_current_customer(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """从 Bearer JWT 或会话 cookie 解析顾客主体 (typ=customer); 缺失/无效/类型不符 → 401。

    员工 token 必须精确匹配 typ=staff，顾客 token 必须精确匹配 typ=customer；
    两类 token 均要求有对应主体标识，避免跨用途复用。
    """
    token = _token_from_request(request, creds)
    if token is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        payload = decode_token(
            token=token,
            typ=TOKEN_TYPE_CUSTOMER,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
    except Exception as exc:  # noqa: BLE001 - 翻译为 HTTP 语义
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc
    customer_id = payload.get("customer_id")
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise HTTPException(status_code=401, detail="customer token missing subject")
    return {"customer_id": customer_id, "email": payload.get("email")}


def build_output_dir(
    settings: Settings, principal: dict[str, Any], omodul_name: str
) -> Path:
    """omodul 输出目录: <output_root>/<omodul_name>/<伪名化用户引用>。"""
    ref = anon_ref(str(principal.get("user_id") or "anonymous"), settings)
    return settings.output_root / omodul_name / ref
