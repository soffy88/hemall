"""hemall 鉴权 — 管理员 (app_user) 登录签发 JWT + Cookie 会话。

3O 元素库没有"按密码登录 app_user"的原语 (create_user 只负责注册并自行哈希密码;
obase 提供 bcrypt_verify + jwt_sign 积木)。本模块在服务层把这两块装配成登录流程:
按 email 查 app_user → 校验状态/密码 → 签发 JWT。属 §8 项目服务层职责, 不入主库。

Cookie 会话设计 (F-01/F-02/F-03):
- 登录成功设置 HttpOnly + Secure + SameSite=Lax 的 access_token / refresh_token cookie
- 短期 access token (15min) + 长期 refresh token (7d)，401 时前端自动刷新
- 保留 Bearer 头兼容 API 客户端 (Authorization: Bearer <token>)
- CSRF 保护: 双重提交 token (X-CSRF-Token header + csrf_token cookie) + Origin/Sec-Fetch-Site 校验

TOTP 两步验证 (RFC 6238):
- 仅用标准库 hmac/hashlib/base64/time 实现，无额外依赖
- 绑定接口返回 otpauth:// URI，前端显示二维码或文本
- 登录第二步校验 TOTP 码，防重放 (同一时间步只能用一次)
- 哈希存储一次性恢复码 (bcrypt)
- 错误次数限流 (配合现有 rate limiter)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
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


class LoginTotpRequiredResponse(BaseModel):
    """登录第一步成功但需要 TOTP 第二步验证。"""

    requires_totp: bool = True
    user_id: str
    email: str
    message: str = "TOTP verification required"


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


# ── TOTP 两步验证 (RFC 6238) ──────────────────────────────────────────────
# 仅用标准库 hmac/hashlib/base64/time 实现，无额外依赖
# 时间步: 30 秒，算法: HMAC-SHA1，位数: 6
_TOTP_TIME_STEP = 30
_TOTP_DIGITS = 6
_TOTP_ALGORITHM = hashlib.sha1
_TOTP_ISSUER = "Hemall"
_RECOVERY_CODE_COUNT = 10
_RECOVERY_CODE_LENGTH = 16


def _totp_generate_secret() -> str:
    """生成 TOTP 密钥 (base32 编码，RFC 3548)。"""
    # 20 字节 = 160 位，base32 编码后 32 字符
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _totp_counter(timestamp: int | None = None) -> int:
    """计算当前时间步计数器。"""
    return (timestamp or int(time.time())) // _TOTP_TIME_STEP


def _totp_hmac_sha1(secret_b32: str, counter: int) -> int:
    """计算 HMAC-SHA1 并截取动态截断得到 6 位数字。"""
    # base32 解码密钥
    key = base64.b32decode(secret_b32.upper() + "=" * ((8 - len(secret_b32) % 8) % 8))
    # counter 编码为 8 字节大端序
    msg = counter.to_bytes(8, "big")
    # HMAC-SHA1
    hmac_digest = hmac.new(key, msg, _TOTP_ALGORITHM).digest()
    # 动态截断 (RFC 4226)
    offset = hmac_digest[-1] & 0x0F
    code = (
        ((hmac_digest[offset] & 0x7F) << 24)
        | ((hmac_digest[offset + 1] & 0xFF) << 16)
        | ((hmac_digest[offset + 2] & 0xFF) << 8)
        | (hmac_digest[offset + 3] & 0xFF)
    )
    return code % (10 ** _TOTP_DIGITS)


def _totp_generate_code(secret_b32: str, timestamp: int | None = None) -> str:
    """生成当前时间步的 TOTP 码 (6 位数字字符串)。"""
    counter = _totp_counter(timestamp)
    code = _totp_hmac_sha1(secret_b32, counter)
    return f"{code:0{_TOTP_DIGITS}d}"


def _totp_verify_code(secret_b32: str, code: str, timestamp: int | None = None, window: int = 1) -> bool:
    """验证 TOTP 码，允许前后各 window 个时间步 (默认 ±1 步 = ±30s)。

    防重放: 调用方需记录已用的 (counter, code) 对，同一 counter 不能重复使用。
    """
    if not code.isdigit() or len(code) != _TOTP_DIGITS:
        return False
    current_counter = _totp_counter(timestamp)
    for delta in range(-window, window + 1):
        counter = current_counter + delta
        expected = _totp_hmac_sha1(secret_b32, counter)
        if f"{expected:0{_TOTP_DIGITS}d}" == code:
            return True
    return False


def _totp_uri(secret_b32: str, account: str) -> str:
    """生成 otpauth:// URI (用于二维码)。"""
    # otpauth://totp/Label?secret=XXX&issuer=YYY&algorithm=SHA1&digits=6&period=30
    label = f"{_TOTP_ISSUER}:{account}"
    return (
        f"otpauth://totp/{label}?secret={secret_b32}"
        f"&issuer={_TOTP_ISSUER}&algorithm=SHA1&digits=6&period=30"
    )


def _generate_recovery_codes(count: int = _RECOVERY_CODE_COUNT, length: int = _RECOVERY_CODE_LENGTH) -> list[str]:
    """生成一次性恢复码 (十六进制字符串)。"""
    return [secrets.token_hex(length // 2) for _ in range(count)]


def _hash_recovery_codes(codes: list[str]) -> list[str]:
    """对恢复码列表进行 bcrypt 哈希。"""
    return [CryptoUtil.hash_password(password=code) for code in codes]


def _verify_recovery_code(hashed_codes: list[str], code: str) -> tuple[bool, int]:
    """验证恢复码，返回 (是否匹配, 匹配的索引)。

    一次性使用: 验证成功后调用方应从列表中移除该哈希。
    """
    for i, hashed in enumerate(hashed_codes):
        if CryptoUtil.verify_password(password=code, hashed=hashed):
            return True, i
    return False, -1


# TOTP 相关请求/响应模型
class TotpBindInput(BaseModel):
    """绑定 TOTP: 用户提交当前 TOTP 码确认绑定。"""

    code: str = Field(..., min_length=6, max_length=6, description="6 位 TOTP 码")


class TotpBindResponse(BaseModel):
    """绑定 TOTP 返回: otpauth URI 和恢复码。"""

    otpauth_uri: str
    recovery_codes: list[str]
    secret: str  # 仅首次绑定时返回，供前端显示/备份


class TotpVerifyInput(BaseModel):
    """验证 TOTP 码 (登录第二步或解绑确认)。"""

    code: str = Field(..., min_length=6, max_length=6, description="6 位 TOTP 码")


class TotpRecoveryInput(BaseModel):
    """使用恢复码登录。"""

    recovery_code: str = Field(..., min_length=_RECOVERY_CODE_LENGTH, max_length=_RECOVERY_CODE_LENGTH)


class LoginTotpInput(BaseModel):
    """登录第二步: 提交 TOTP 码或恢复码。"""

    user_id: str = Field(..., description="第一步登录返回的 user_id")
    code: str | None = Field(None, description="6 位 TOTP 码")
    recovery_code: str | None = Field(None, description="恢复码")

    def __init__(self, **data):
        super().__init__(**data)
        if not data.get("code") and not data.get("recovery_code"):
            raise ValueError("code 或 recovery_code 必填其一")


async def _get_user_totp_info(pool: PgPool, user_id: str) -> dict | None:
    """获取用户的 TOTP 信息 (secret, recovery_codes_hash, last_used_counter)。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT totp_secret, totp_recovery_codes_hash, totp_last_used_counter, totp_enabled '
            'FROM "app_user" WHERE id = $1 AND deleted_at IS NULL',
            user_id,
        )
    return dict(row) if row else None


async def _set_user_totp_secret(pool: PgPool, user_id: str, secret: str, recovery_hashes: list[str]) -> None:
    """设置用户的 TOTP 密钥和恢复码哈希。"""
    import json

    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE "app_user" SET totp_secret = $1, totp_recovery_codes_hash = $2, '
            'totp_last_used_counter = -1, totp_enabled = true WHERE id = $3',
            secret, json.dumps(recovery_hashes), user_id,
        )


async def _update_user_totp_counter(pool: PgPool, user_id: str, counter: int) -> None:
    """更新用户最后使用的 TOTP 计数器 (防重放)。"""
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE "app_user" SET totp_last_used_counter = $1 WHERE id = $2',
            counter, user_id,
        )


async def _consume_recovery_code(pool: PgPool, user_id: str, index: int) -> None:
    """消费恢复码: 从哈希列表中移除指定索引的码。"""
    import json

    info = await _get_user_totp_info(pool, user_id)
    if not info:
        return
    codes = json.loads(info["totp_recovery_codes_hash"] or "[]")
    if 0 <= index < len(codes):
        codes.pop(index)
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE "app_user" SET totp_recovery_codes_hash = $1 WHERE id = $2',
                json.dumps(codes), user_id,
            )


async def _disable_user_totp(pool: PgPool, user_id: str) -> None:
    """禁用用户的 TOTP (清除密钥和恢复码)。"""
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE "app_user" SET totp_secret = NULL, totp_recovery_codes_hash = NULL, '
            'totp_last_used_counter = -1, totp_enabled = false WHERE id = $1',
            user_id,
        )


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


@router.post("/login")
@limiter.limit("5/minute")
async def login(
    request: Request,
    response: Response,
    body: LoginInput,
    pool: PgPool = Depends(get_pool),
    settings: Settings = Depends(get_settings),
) -> TokenResponse | LoginTotpRequiredResponse:
    """按邮箱+密码登录 app_user。

    - 密码正确且未启用 TOTP: 直接签发完整会话
    - 密码正确且已启用 TOTP: 返回 requires_totp=true，前端需调用 /auth/totp/verify
    - 生产环境下 admin 和 privileged_staff_ids 必须启用 TOTP 才能登录
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id, email, password_hash, status, totp_enabled, totp_secret '
            'FROM "app_user" WHERE email = $1 AND deleted_at IS NULL',
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

    # 检查 TOTP 状态
    totp_enabled = row.get("totp_enabled", False)

    # 生产环境: admin 和 privileged_staff_ids 必须启用 TOTP
    is_privileged = role == "admin" or user_id in settings.privileged_staff_ids.split(",") if settings.privileged_staff_ids else False
    if settings.environment.lower() == "production" and is_privileged and not totp_enabled:
        raise HTTPException(
            status_code=403,
            detail="TOTP required for privileged accounts in production. Enable TOTP first."
        )

    if totp_enabled:
        # 已启用 TOTP: 返回需要第二步验证，不签发完整 token
        return LoginTotpRequiredResponse(
            user_id=user_id,
            email=row["email"],
            message="TOTP verification required. Call /auth/totp/verify with TOTP code or recovery code."
        )

    # 未启用 TOTP: 直接签发完整会话
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

    _set_auth_cookies(response, access_token, refresh_token, csrf_token, settings)

    return TokenResponse(access_token=access_token, user_id=user_id, email=row["email"])


# ── TOTP 两步验证端点 ─────────────────────────────────────────────────────


@router.get("/totp/setup", response_model=TotpBindResponse)
async def totp_setup(
    request: Request,
    settings: Settings = Depends(get_settings),
    pool: PgPool = Depends(get_pool),
) -> TotpBindResponse:
    """获取 TOTP 绑定信息: 生成密钥并返回 otpauth URI + 恢复码。

    用户需用 Authenticator App 扫码或手输密钥，然后调用 /auth/totp/bind 确认。
    """
    access_token = _get_token_from_cookie_or_header(request, ACCESS_TOKEN_COOKIE)
    if not access_token:
        raise HTTPException(status_code=401, detail="not authenticated")

    user = await _get_current_user_from_token(access_token, settings)
    if not user:
        raise HTTPException(status_code=401, detail="invalid or expired token")

    user_id = user["user_id"]

    # 生成新密钥和恢复码
    secret = _totp_generate_secret()
    recovery_codes = _generate_recovery_codes()
    recovery_hashes = _hash_recovery_codes(recovery_codes)

    # 暂存密钥 (待用户确认后正式启用)
    # 这里先不写库，由 bind 端点写入
    otpauth_uri = _totp_uri(secret, user["email"])

    return TotpBindResponse(
        otpauth_uri=otpauth_uri,
        recovery_codes=recovery_codes,
        secret=secret,
    )


@router.post("/totp/bind")
async def totp_bind(
    request: Request,
    body: TotpBindInput,
    settings: Settings = Depends(get_settings),
    pool: PgPool = Depends(get_pool),
) -> dict[str, str]:
    """确认绑定 TOTP: 用户提交 Authenticator 显示的码，校验通过后正式启用。"""
    access_token = _get_token_from_cookie_or_header(request, ACCESS_TOKEN_COOKIE)
    if not access_token:
        raise HTTPException(status_code=401, detail="not authenticated")

    user = await _get_current_user_from_token(access_token, settings)
    if not user:
        raise HTTPException(status_code=401, detail="invalid or expired token")

    user_id = user["user_id"]

    # 从请求头获取临时密钥 (前端在 setup 时获得，bind 时回传)
    # 这里简化: 要求前端在 bind 时同时提交 secret (或从 session 取)
    # 为简化实现，要求客户端在 bind 时传回 secret
    # 实际生产建议用短期加密 token 传递 secret
    secret = request.headers.get("X-TOTP-Secret")
    if not secret:
        raise HTTPException(status_code=400, detail="missing X-TOTP-Secret header")

    # 验证码
    if not _totp_verify_code(secret, body.code):
        raise HTTPException(status_code=400, detail="invalid TOTP code")

    # 生成恢复码并哈希
    recovery_codes = _generate_recovery_codes()
    recovery_hashes = _hash_recovery_codes(recovery_codes)

    # 写库启用
    await _set_user_totp_secret(pool, user_id, secret, recovery_hashes)

    return {"message": "TOTP enabled", "recovery_codes": recovery_codes}


@router.post("/totp/verify", response_model=TokenResponse)
async def totp_verify(
    request: Request,
    body: LoginTotpInput,
    response: Response,
    pool: PgPool = Depends(get_pool),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """登录第二步: 验证 TOTP 码或恢复码，成功签发完整会话。"""
    # 这里假设第一步登录已验证密码，返回了 user_id 但未签发完整 token
    # 实际流程: login 返回需 TOTP 的标记 + 临时凭据，这里用临时凭据换正式 token
    # 简化实现: 直接用 user_id 查用户，验证 TOTP，签发 token
    user_id = body.user_id

    info = await _get_user_totp_info(pool, user_id)
    if not info or not info["totp_enabled"]:
        raise HTTPException(status_code=403, detail="TOTP not enabled for this user")

    # 获取用户详细信息
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id, email, password_hash, status FROM "app_user" WHERE id = $1 AND deleted_at IS NULL',
            user_id,
        )
    if not row or row["status"] != "active":
        raise HTTPException(status_code=403, detail="user not active")

    verified = False
    used_counter = -1

    if body.code:
        # 验证 TOTP 码
        secret = info["totp_secret"]
        last_counter = info["totp_last_used_counter"] or -1
        current_counter = _totp_counter()

        if not _totp_verify_code(secret, body.code):
            raise HTTPException(status_code=401, detail="invalid TOTP code")

        # 防重放: 同一 counter 不能重复使用
        if current_counter <= last_counter:
            raise HTTPException(status_code=401, detail="TOTP code already used (replay)")

        verified = True
        used_counter = current_counter

    elif body.recovery_code:
        # 验证恢复码
        import json

        recovery_hashes = json.loads(info["totp_recovery_codes_hash"] or "[]")
        ok, index = _verify_recovery_code(recovery_hashes, body.recovery_code)
        if not ok:
            raise HTTPException(status_code=401, detail="invalid recovery code")

        # 消费恢复码
        await _consume_recovery_code(pool, user_id, index)
        verified = True

    if not verified:
        raise HTTPException(status_code=401, detail="invalid credentials")

    # 更新最后使用的 counter (防重放)
    if used_counter >= 0:
        await _update_user_totp_counter(pool, user_id, used_counter)

    role = _staff_role_for_user(user_id, settings)

    # 签发完整会话 token
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

    _set_auth_cookies(response, access_token, refresh_token, csrf_token, settings)

    return TokenResponse(access_token=access_token, user_id=user_id, email=row["email"])


@router.delete("/totp")
async def totp_disable(
    request: Request,
    body: TotpVerifyInput,
    settings: Settings = Depends(get_settings),
    pool: PgPool = Depends(get_pool),
) -> dict[str, str]:
    """解绑 TOTP: 验证当前 TOTP 码后禁用。"""
    access_token = _get_token_from_cookie_or_header(request, ACCESS_TOKEN_COOKIE)
    if not access_token:
        raise HTTPException(status_code=401, detail="not authenticated")

    user = await _get_current_user_from_token(access_token, settings)
    if not user:
        raise HTTPException(status_code=401, detail="invalid or expired token")

    user_id = user["user_id"]

    info = await _get_user_totp_info(pool, user_id)
    if not info or not info["totp_enabled"]:
        raise HTTPException(status_code=400, detail="TOTP not enabled")

    if not _totp_verify_code(info["totp_secret"], body.code):
        raise HTTPException(status_code=401, detail="invalid TOTP code")

    await _disable_user_totp(pool, user_id)

    return {"message": "TOTP disabled"}


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
        # 注意: raise HTTPException 会走异常处理器、丢弃注入 response 上的
        # Set-Cookie——清会话必须直接返回 JSONResponse 才生效。
        resp = JSONResponse(status_code=401, content={"detail": "invalid or expired refresh token"})
        _clear_auth_cookies(resp, settings)
        return resp

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