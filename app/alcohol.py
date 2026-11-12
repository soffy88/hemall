"""酒类销售年龄门禁。

默认关闭酒类销售；即使配置开启，前台列表/详情/加购/结账都要求请求携带
服务端签发的年龄凭证。年龄确认只保存"已满 18 周岁"的短期签名 Cookie，
不持久化用户生日。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import date

from fastapi import HTTPException, Request

from .config import Settings
from .security.tokens import derive_secret

ALCOHOL_CATEGORY_SLUGS = frozenset({"drinks"})
ALCOHOL_AGE_COOKIE = "hemall_alcohol_age"
ALCOHOL_NOTICE = "未成年人禁止购买和饮酒。请理性饮酒，切勿酒后驾车。"


def _age_secret(settings: Settings) -> str:
    return derive_secret(settings.jwt_secret, "alcohol-age-gate")


def issue_age_token(settings: Settings, *, expires_at: int) -> str:
    secret = _age_secret(settings).encode("utf-8")
    signature = hmac.new(secret, str(expires_at).encode("ascii"), hashlib.sha256).hexdigest()
    return f"{expires_at}.{signature}"


def verify_age_token(settings: Settings, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    raw_expiry, signature = token.split(".", 1)
    try:
        expires_at = int(raw_expiry)
    except ValueError:
        return False
    if expires_at <= int(time.time()):
        return False
    expected = issue_age_token(settings, expires_at=expires_at).split(".", 1)[1]
    return hmac.compare_digest(signature, expected)


def age_verified(request: Request) -> bool:
    settings: Settings = request.app.state.config
    return verify_age_token(settings, request.cookies.get(ALCOHOL_AGE_COOKIE))


def alcohol_access_allowed(request: Request) -> bool:
    settings: Settings = request.app.state.config
    return bool(settings.alcohol_sales_enabled and age_verified(request))


def require_alcohol_access(request: Request) -> None:
    settings: Settings = request.app.state.config
    if not settings.alcohol_sales_enabled:
        raise HTTPException(403, "酒类商品当前已按合规要求下架")
    if not age_verified(request):
        raise HTTPException(403, "购买酒类前需完成年龄确认")


def calculate_age(birth_date: date, *, today: date | None = None) -> int:
    current = today or date.today()
    return (
        current.year
        - birth_date.year
        - ((current.month, current.day) < (birth_date.month, birth_date.day))
    )
