"""令牌类型隔离 — 签发端统一盖 ``typ``，校验端统一按白名单。

设计约定（安全域硬规则）：
  - **签发必带 typ。** 没有 typ 的令牌 = 无类型令牌 = 一律拒绝。历史版本
    ``_sign_receipt`` 漏掉 typ 时，收据 token 与 staff token 结构完全一致，
    只差一个字段——这正是黑名单式校验（"只要不是 customer 就放行"）被绕过的根因。
  - **校验只认白名单。** 调用方显式声明"我要哪一种"，本模块负责验签 + 比对
    ``typ``，不一致抛 :class:`TokenTypeError`。
  - **能力令牌用独立派生密钥。** cart / receipt 这类无主体、只授权单一资源的
    能力令牌用 HKDF 从主密钥派生独立签名密钥（RFC 5869 Extract+Expand），
    因此它们**用主密钥解不出来**：主密钥泄漏不等于能力令牌泄漏，且能力令牌
    的签名材料与 staff/customer 完全不共享。

身份类令牌（staff / customer / pickup）继续用主密钥签发，以免无谓地作废
存量会话；它们的隔离由 ``typ`` 白名单承担（pickup 令牌无 ``sub``，
``get_current_user`` 的主体检查再兜一层）。
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from typing import Any

TOKEN_TYPE_STAFF = "staff"
TOKEN_TYPE_CUSTOMER = "customer"
TOKEN_TYPE_CART = "cart"
TOKEN_TYPE_RECEIPT = "receipt"
TOKEN_TYPE_PICKUP = "pickup"

#: 本仓库会签发的全部令牌类型。新增类型必须登记到这里（测试会遍历全集）。
ALL_TOKEN_TYPES: frozenset[str] = frozenset(
    {
        TOKEN_TYPE_STAFF,
        TOKEN_TYPE_CUSTOMER,
        TOKEN_TYPE_CART,
        TOKEN_TYPE_RECEIPT,
        TOKEN_TYPE_PICKUP,
    }
)

#: 无主体、只授权单一资源的能力令牌——用独立派生密钥签发。
DERIVED_KEY_TOKEN_TYPES: frozenset[str] = frozenset({TOKEN_TYPE_CART, TOKEN_TYPE_RECEIPT})

#: 允许出现在 /ws 实时推送通道上的主体类型。
WS_TOKEN_TYPES: frozenset[str] = frozenset({TOKEN_TYPE_STAFF, TOKEN_TYPE_CUSTOMER})

#: 会话 Cookie 名 (F-01/F-03)。access/refresh token 以 HttpOnly Cookie 承载，
#: CSRF token 为非 HttpOnly 的双重提交 cookie (前端读后放 X-CSRF-Token 头)。
ACCESS_TOKEN_COOKIE = "hemall_access_token"
REFRESH_TOKEN_COOKIE = "hemall_refresh_token"
CSRF_TOKEN_COOKIE = "hemall_csrf_token"

_HKDF_SALT = b"hemall/jwt/v1/salt"
_HKDF_INFO_PREFIX = b"hemall/jwt/v1/purpose/"


class TokenTypeError(Exception):
    """令牌类型不匹配（或缺少 typ）——调用方应翻译成 401/403。"""


def derive_secret(base_secret: str, purpose: str) -> str:
    """从主密钥按用途派生独立签名密钥（HKDF-SHA256, RFC 5869）。

    Extract 步用固定 salt，Expand 步把用途标签放进 info，因此
    ``derive_secret(s, "cart")`` 与 ``derive_secret(s, "receipt")`` 是两把
    互不相干的密钥；同一个主密钥在任何用途下派生出稳定可复现的结果。
    """
    if not base_secret:
        raise ValueError("base_secret must not be empty")
    if not purpose:
        raise ValueError("purpose must not be empty")

    # Extract: PRK = HMAC(salt, IKM)
    prk = hmac.new(_HKDF_SALT, base_secret.encode("utf-8"), hashlib.sha256).digest()
    # Expand: OKM = T(1) || T(2) ...，单块 (L=32) 够 HMAC-SHA256 用。
    info = _HKDF_INFO_PREFIX + purpose.encode("utf-8")
    okm = hmac.new(prk, info + b"\x01", hashlib.sha256).digest()
    return okm.hex()


def secret_for_type(base_secret: str, typ: str) -> str:
    """按令牌类型选签名密钥：能力令牌派生，身份令牌用主密钥。"""
    if typ in DERIVED_KEY_TOKEN_TYPES:
        return derive_secret(base_secret, typ)
    return base_secret


def sign_token(
    payload: dict[str, Any],
    *,
    typ: str,
    secret: str,
    algorithm: str,
    expires_in_minutes: int,
) -> str:
    """按类型签发令牌——``typ`` 由本函数盖章，调用方不得自带。

    Raises:
        ValueError: ``typ`` 未登记，或 payload 自带 ``typ``（防止静默覆盖）。
    """
    if typ not in ALL_TOKEN_TYPES:
        raise ValueError(f"unregistered token type: {typ!r}")
    if "typ" in payload:
        raise ValueError("sign_token stamps typ itself; do not pass it in payload")
    from obase.crypto.util import CryptoUtil

    return CryptoUtil.jwt_sign(
        payload={**payload, "typ": typ},
        secret=secret_for_type(secret, typ),
        expires_in_minutes=expires_in_minutes,
        algorithm=algorithm,
    )


def decode_token(token: str, *, typ: str, secret: str, algorithm: str) -> dict[str, Any]:
    """验签并要求 ``typ`` 精确匹配。类型不符/缺 typ 一律抛 TokenTypeError。

    验签失败抛 obase 的 ObaseAuthError（由调用方翻译成 401）；本函数只在
    "签名有效但类型/主体不符"时抛 TokenTypeError。
    """
    if typ not in ALL_TOKEN_TYPES:
        raise ValueError(f"unregistered token type: {typ!r}")
    from obase.crypto.util import CryptoUtil

    payload = CryptoUtil.jwt_decode(
        token=token, secret=secret_for_type(secret, typ), algorithm=algorithm
    )
    if payload.get("typ") != typ:
        raise TokenTypeError(f"expected token type {typ!r}, got {payload.get('typ')!r}")
    return payload


def has_typ(payload: dict[str, Any], typ: str) -> bool:
    """仅看 claims 的类型判断（用于审计/限流这类 best-effort 旁路）。"""
    t = payload.get("typ")
    if t is None:
        return False
    if isinstance(typ, str):
        return t in typ
    return t in set(typ)


def typ_in(payload: dict[str, Any], allowed: Iterable[str]) -> bool:
    return payload.get("typ") in frozenset(allowed)
