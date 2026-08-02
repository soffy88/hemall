"""hemall 鉴权 — 管理员 (app_user) 登录签发 JWT。

3O 元素库没有"按密码登录 app_user"的原语 (create_user 只负责注册并自行哈希密码;
obase 提供 bcrypt_verify + jwt_sign 积木)。本模块在服务层把这两块装配成登录流程:
按 email 查 app_user → 校验状态/密码 → 签发 JWT。属 §8 项目服务层职责, 不入主库。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from obase.crypto.util import CryptoUtil
from obase.persistence.pool import PgPool

from .config import Settings
from .deps import get_pool, get_settings
from .security.rate_limit import limiter

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginInput(BaseModel):
    """登录请求体。"""

    email: str = Field(..., description="app_user 邮箱")
    password: str = Field(..., description="明文密码 (服务端 bcrypt 校验, 不落库/不进轨迹)")


class TokenResponse(BaseModel):
    """登录成功返回。"""

    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
async def login(
    request: Request,
    body: LoginInput,
    pool: PgPool = Depends(get_pool),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """按邮箱+密码登录 app_user, 成功签发 JWT (sub=user_id, email=email)。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id, email, password_hash, status FROM "app_user" '
            "WHERE email = $1 AND deleted_at IS NULL",
            body.email,
        )

    if row is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    if row["status"] != "active":
        raise HTTPException(status_code=403, detail=f"user not active (status={row['status']!r})")

    if not CryptoUtil.verify_password(password=body.password, hashed=row["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid credentials")

    token = CryptoUtil.jwt_sign(
        payload={"sub": str(row["id"]), "email": row["email"]},
        secret=settings.jwt_secret,
        expires_in_minutes=settings.jwt_expires_minutes,
        algorithm=settings.jwt_algorithm,
    )
    return TokenResponse(access_token=token, user_id=str(row["id"]), email=row["email"])
