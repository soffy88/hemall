"""hemall 鉴权 — 管理员 (app_user) 登录签发 JWT + Cookie 会话。

3O 元素库没有"按密码登录 app_user"的原语 (create_user 只负责注册并自行哈希密码;
obase 提供 bcrypt_verify + jwt_sign 积木)。本模块在服务层把这两块装配成登录流程:
按 email 查 app_user → 校验状态/密码 → 签发 JWT。属 §8 项目服务层职责, 不入主库。

Cookie 会话设计 (F-01/F-02/F-03):
- 登录成功设置 HttpOnly + Secure + SameSite=Lax 的 access_token / refresh_token cookie
- 短期 access token (15min) + 长期 refresh token (7d)，401 时前端自动刷新
- 保留 Bearer 头兼容 API 客户端 (Authorization: Bearer <token>)
- CSRF 保护: 双重提交 token (X-CSRF-Token header + csrf_token cookie) + Origin/Sec-Fetch-Site 校验
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from obase.crypto.util import CryptoUtil
from obase.persistence.pool import PgPool

from .config import Settings
from .deps import get_pool, get_settings
from .security.rate_limit import limiter
from .security.tokens import (
    ACCESS_TOKEN_COOKIE,
    CSRF_TOKEN_COOKIE,
    REFRESH_TOKEN_COOKIE,
    TOKEN_TYPE_STAFF,
    TOKEN_TYPE_CUSTOMER,
    decode_token,
    sign_token,
    TokenTypeError,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Token 过期时间
ACCESS_TOKEN_EXPIRES_MINUTES = 15
REFRESH_TOKEN_EXPIRES_DAYS = 7


class LoginInput(BaseModel):
    """登录请求体。"""

    email: str = Field(..., description="app_user 邮箱")
    password: str = Field(..., description="明文密码 (服务端 bcrypt 校验, 不落库/不进轨迹)")


class TokenResponse(BaseModel):
    """登录成功返回 (兼容 Bearer API 客户端)。"""

    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str


class MeResponse(BaseModel):
    """当前用户信息返回。"""

    user_id: str
    email: str
    role: str


class RefreshResponse(BaseModel):
    """刷新成功返回。"""

    access_token: str
    token_type: str = "bearer"


#: 全部合法的 staff 角色。校验端一律按这张表做**白名单**——未知/缺失的 role
#: 一律 403，不会因为"没写判断"而默认放行。
STAFF_ROLES = frozenset({"admin", "support", "staff"})


def _staff_role_for_user(user_id: str, settings: Settings) -> str:
    privileged_ids = {
        value.strip() for value in settings.privileged_staff_ids.split(",") if value.strip()
    }
    if user_id in privileged_ids:
        return "admin"
    support_ids = {
        value.strip() for value in settings.support_staff_ids.split(",") if value.strip()
    }
    if user_id in support_ids:
        return "support"
    return "staff"


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str, csrf_token: str, settings: Settings) -> None:
    """设置认证相关 cookies (HttpOnly, Secure, SameSite=Lax)。"""
    is_secure = settings.environment.lower() == "production"
    # access token: 短期 (15min)
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=access_token,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRES_MINUTES * 60,
        path="/",
    )
    # refresh token: 长期 (7d)
    response.set_cookie(
        key=REFRESH_TOKEN_COOKIE,
        value=refresh_token,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRES_DAYS * 24 * 60 * 60,
        path="/",
    )
    # csrf token: 双重提交模式，非 HttpOnly 供前端读取
    response.set_cookie(
        key=CSRF_TOKEN_COOKIE,
        value=csrf_token,
        httponly=False,
        secure=is_secure,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRES_DAYS * 24 * 60 * 60,
        path="/",
    )


def _clear_auth_cookies(response: Response, settings: Settings) -> None:
    """清除认证相关 cookies。"""
    is_secure = settings.environment.lower() == "production"
    for key in (ACCESS_TOKEN_COOKIE, REFRESH_TOKEN_COOKIE, CSRF_TOKEN_COOKIE):
        response.delete_cookie(key=key, path="/", secure=is_secure, samesite="lax")


def _generate_csrf_token() -> str:
    """生成 CSRF token (双重提交模式)。"""
    return secrets.token_urlsafe(32)


def _get_token_from_cookie_or_header(request: Request, cookie_name: str) -> str | None:
    """从 cookie 或 Authorization 头获取 token (优先 cookie，兼容 Bearer)。"""
    # 优先从 cookie 读取
    token = request.cookies.get(cookie_name)
    if token:
        return token
    # 兼容 Bearer 头
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


async def _get_current_user_from_token(token: str, settings: Settings) -> dict | None:
    """从 token 解析用户信息，返回 None 表示 token 无效。"""
    try:
        payload = decode_token(
            token,
            typ=TOKEN_TYPE_STAFF,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        return {
            "user_id": payload["sub"],
            "email": payload.get("email"),
            "role": payload.get("role"),
        }
    except (TokenTypeError, Exception):
        return None


def _decode_session_token(token: str, settings: Settings) -> tuple[str, dict] | None:
    """解会话 refresh token；仅接受显式白名单 {staff, customer}。

    返回 ``(typ, payload)``；签名无效/类型不符/能力令牌 (cart/receipt/pickup)
    一律 None——校验只认白名单，与 tokens.py 的类型隔离约定一致。
    """
    for typ in (TOKEN_TYPE_STAFF, TOKEN_TYPE_CUSTOMER):
        try:
            payload = decode_token(
                token,
                typ=typ,
                secret=settings.jwt_secret,
                algorithm=settings.jwt_algorithm,
            )
        except Exception:  # noqa: BLE001 - 换下一个白名单类型继续
            continue
        return typ, payload
    return None


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
async def login(
    request: Request,
    response: Response,
    body: LoginInput,
    pool: PgPool = Depends(get_pool),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """按邮箱+密码登录 app_user，成功设置 HttpOnly Cookie 会话并返回 Bearer token (兼容 API 客户端)。"""
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

    user_id = str(row["id"])
    role = _staff_role_for_user(user_id, settings)

    # 签发短期 access token + 长期 refresh token
    access_token = sign_token(
        {"sub": user_id, "email": row["email"], "role": role},
        typ=TOKEN_TYPE_STAFF,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=ACCESS_TOKEN_EXPIRES_MINUTES,
    )
    refresh_token = sign_token(
        {"sub": user_id, "email": row["email"], "role": role},
        typ=TOKEN_TYPE_STAFF,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=REFRESH_TOKEN_EXPIRES_DAYS * 24 * 60,
    )
    csrf_token = _generate_csrf_token()

    # 设置 HttpOnly cookies
    _set_auth_cookies(response, access_token, refresh_token, csrf_token, settings)

    # 兼容 Bearer API 客户端
    return TokenResponse(access_token=access_token, user_id=user_id, email=row["email"])


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """登出：清除认证 cookies。"""
    _clear_auth_cookies(response, settings)
    return {"message": "logged out"}


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> RefreshResponse:
    """刷新 access token：用 refresh token cookie 签发新的 access token + csrf token。

    会话白名单为 ``{staff, customer}``（两种已注册的身份令牌类型），按原 typ
    重签——顾客 refresh 签出的还是顾客 access token，类型隔离不破。
    """
    refresh_token = request.cookies.get(REFRESH_TOKEN_COOKIE)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="no refresh token")

    decoded = _decode_session_token(refresh_token, settings)
    if not decoded:
        _clear_auth_cookies(response, settings)
        raise HTTPException(status_code=401, detail="invalid or expired refresh token")

    typ, payload = decoded
    if typ == TOKEN_TYPE_STAFF:
        claims = {"sub": payload["sub"], "email": payload.get("email"), "role": payload.get("role")}
    else:
        claims = {"customer_id": payload["customer_id"], "email": payload.get("email")}

    # 签发新的 access token + csrf token
    access_token = sign_token(
        claims,
        typ=typ,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=ACCESS_TOKEN_EXPIRES_MINUTES,
    )
    csrf_token = _generate_csrf_token()

    # 更新 cookies (保留原有 refresh token，仅更新 access token 和 csrf token)
    is_secure = settings.environment.lower() == "production"
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=access_token,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRES_MINUTES * 60,
        path="/",
    )
    response.set_cookie(
        key=CSRF_TOKEN_COOKIE,
        value=csrf_token,
        httponly=False,
        secure=is_secure,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRES_DAYS * 24 * 60 * 60,
        path="/",
    )

    return RefreshResponse(access_token=access_token)


@router.get("/me", response_model=MeResponse)
async def me(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> MeResponse:
    """获取当前登录用户信息 (从 access token cookie 或 Bearer 头)。"""
    access_token = _get_token_from_cookie_or_header(request, ACCESS_TOKEN_COOKIE)
    if not access_token:
        raise HTTPException(status_code=401, detail="not authenticated")

    user = await _get_current_user_from_token(access_token, settings)
    if not user:
        raise HTTPException(status_code=401, detail="invalid or expired token")

    return MeResponse(**user)


@router.get("/csrf-token")
async def get_csrf_token(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """获取 CSRF token (前端首次加载或 token 过期时调用)。"""
    # 如果已有有效 csrf token cookie，直接返回
    existing = request.cookies.get(CSRF_TOKEN_COOKIE)
    if existing:
        return {"csrf_token": existing}

    # 否则生成新的
    csrf_token = _generate_csrf_token()
    is_secure = settings.environment.lower() == "production"
    response.set_cookie(
        key=CSRF_TOKEN_COOKIE,
        value=csrf_token,
        httponly=False,
        secure=is_secure,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRES_DAYS * 24 * 60 * 60,
        path="/",
    )
    return {"csrf_token": csrf_token}


# ── 顾客认证端点 (Cookie 会话，复用相同的 cookie 设施) ───────────────────

from .security.tokens import TOKEN_TYPE_CUSTOMER


class CustomerLoginInput(BaseModel):
    email: str = Field(..., description="顾客邮箱")
    password: str = Field(..., description="明文密码")


class CustomerRegisterInput(BaseModel):
    email: str = Field(..., description="顾客邮箱")
    password: str = Field(..., description="明文密码")
    phone: str | None = Field(None, description="手机号")
    name: str | None = Field(None, description="姓名")


class CustomerTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    customer_id: str
    email: str


class CustomerMeResponse(BaseModel):
    customer_id: str
    email: str


async def _get_current_customer_from_token(token: str, settings: Settings) -> dict | None:
    """从 token 解析顾客信息，返回 None 表示 token 无效。"""
    try:
        payload = decode_token(
            token,
            typ=TOKEN_TYPE_CUSTOMER,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        return {
            "customer_id": payload["customer_id"],
            "email": payload.get("email"),
        }
    except (TokenTypeError, Exception):
        return None


def _set_customer_auth_cookies(response: Response, access_token: str, refresh_token: str, csrf_token: str, settings: Settings) -> None:
    """设置顾客认证相关 cookies (复用相同 cookie 名称，但 typ=customer)。"""
    is_secure = settings.environment.lower() == "production"
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=access_token,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRES_MINUTES * 60,
        path="/",
    )
    response.set_cookie(
        key=REFRESH_TOKEN_COOKIE,
        value=refresh_token,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRES_DAYS * 24 * 60 * 60,
        path="/",
    )
    response.set_cookie(
        key=CSRF_TOKEN_COOKIE,
        value=csrf_token,
        httponly=False,
        secure=is_secure,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRES_DAYS * 24 * 60 * 60,
        path="/",
    )


@router.post("/customer/login", response_model=CustomerTokenResponse)
@limiter.limit("10/minute")
async def customer_login(
    request: Request,
    response: Response,
    body: CustomerLoginInput,
    pool: PgPool = Depends(get_pool),
    settings: Settings = Depends(get_settings),
) -> CustomerTokenResponse:
    """顾客登录：按 email+密码验证，成功设置 HttpOnly Cookie 会话 (typ=customer)。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id, email, password_hash, status FROM "customer" '
            "WHERE email = $1 AND deleted_at IS NULL",
            body.email,
        )

    if row is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    if row["status"] != "active":
        raise HTTPException(status_code=403, detail=f"customer not active (status={row['status']!r})")

    if not CryptoUtil.verify_password(password=body.password, hashed=row["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid credentials")

    customer_id = str(row["id"])

    access_token = sign_token(
        {"customer_id": customer_id, "email": row["email"]},
        typ=TOKEN_TYPE_CUSTOMER,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=ACCESS_TOKEN_EXPIRES_MINUTES,
    )
    refresh_token = sign_token(
        {"customer_id": customer_id, "email": row["email"]},
        typ=TOKEN_TYPE_CUSTOMER,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=REFRESH_TOKEN_EXPIRES_DAYS * 24 * 60,
    )
    csrf_token = _generate_csrf_token()

    _set_customer_auth_cookies(response, access_token, refresh_token, csrf_token, settings)

    return CustomerTokenResponse(access_token=access_token, customer_id=customer_id, email=row["email"])


@router.post("/customer/register", response_model=CustomerTokenResponse)
@limiter.limit("5/minute")
async def customer_register(
    request: Request,
    response: Response,
    body: CustomerRegisterInput,
    pool: PgPool = Depends(get_pool),
    settings: Settings = Depends(get_settings),
) -> CustomerTokenResponse:
    """顾客注册：创建 customer 记录并直接登录。"""
    # 检查邮箱是否已存在
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            'SELECT id FROM "customer" WHERE email = $1 AND deleted_at IS NULL',
            body.email,
        )
    if existing:
        raise HTTPException(status_code=409, detail="email already registered")

    # 创建 customer
    password_hash = CryptoUtil.hash_password(body.password)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'INSERT INTO "customer" (email, password_hash, phone, name, status) '
            'VALUES ($1, $2, $3, $4, \'active\') RETURNING id, email',
            body.email, password_hash, body.phone, body.name,
        )

    customer_id = str(row["id"])

    access_token = sign_token(
        {"customer_id": customer_id, "email": row["email"]},
        typ=TOKEN_TYPE_CUSTOMER,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=ACCESS_TOKEN_EXPIRES_MINUTES,
    )
    refresh_token = sign_token(
        {"customer_id": customer_id, "email": row["email"]},
        typ=TOKEN_TYPE_CUSTOMER,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=REFRESH_TOKEN_EXPIRES_DAYS * 24 * 60,
    )
    csrf_token = _generate_csrf_token()

    _set_customer_auth_cookies(response, access_token, refresh_token, csrf_token, settings)

    return CustomerTokenResponse(access_token=access_token, customer_id=customer_id, email=row["email"])


@router.post("/customer/logout")
async def customer_logout(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """顾客登出：清除认证 cookies。"""
    _clear_auth_cookies(response, settings)
    return {"message": "logged out"}


@router.get("/customer/me", response_model=CustomerMeResponse)
async def customer_me(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> CustomerMeResponse:
    """获取当前登录顾客信息 (从 access token cookie 或 Bearer 头)。"""
    access_token = _get_token_from_cookie_or_header(request, ACCESS_TOKEN_COOKIE)
    if not access_token:
        raise HTTPException(status_code=401, detail="not authenticated")

    customer = await _get_current_customer_from_token(access_token, settings)
    if not customer:
        raise HTTPException(status_code=401, detail="invalid or expired token")

    return CustomerMeResponse(**customer)