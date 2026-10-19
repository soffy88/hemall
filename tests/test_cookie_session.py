"""Cookie 会话集成测试 — F-01/F-02/F-03 (HttpOnly Cookie 会话 + CSRF + typ 隔离)。

覆盖:
- 登录签发 HttpOnly + SameSite=Lax 会话 cookie (access/refresh/csrf 三类)
- /auth/me、/auth/refresh 走 cookie 的端到端路径 (含 typ 互斥)
- CSRF 中间件 (生产模式): 双重提交 token + Origin + Sec-Fetch-Site
- 会话 cookie 在受保护端点上的主体解析与类型隔离
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI, Response
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import Settings
from app.deps import get_current_customer, get_current_user, get_settings
from app.security.csrf import CSRFMiddleware
from app.security.tokens import (
    ACCESS_TOKEN_COOKIE,
    CSRF_TOKEN_COOKIE,
    REFRESH_TOKEN_COOKIE,
    TOKEN_TYPE_CART,
    TOKEN_TYPE_CUSTOMER,
    TOKEN_TYPE_STAFF,
    sign_token,
)

TEST_DSN = os.environ.get("TEST_PG_DSN")

_PROD_SECRETS = {
    "jwt_secret": "test-only-jwt-secret-with-32-bytes!",
    "webhook_secret": "test-only-webhook-secret-32-bytes!",
    "hardware_secret": "test-only-hardware-secret-32-bytes!",
}


def _req(cookie: str | None = None) -> Request:
    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "headers": headers,
            "path": "/",
            "scheme": "http",
            "server": ("t", 80),
            "client": ("c", 1),
        }
    )


def _sign(payload: dict, settings: Settings, *, typ: str, minutes: int = 60) -> str:
    return sign_token(
        payload,
        typ=typ,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in_minutes=minutes,
    )


# ── 1. 登录 cookie 签发 ──────────────────────────────────────────────────


def test_set_auth_cookies_flags_development():
    """development: HttpOnly + SameSite=Lax + path=/；无 Secure；csrf cookie 非 HttpOnly。"""
    from app.auth import _set_auth_cookies

    resp = Response()
    _set_auth_cookies(resp, "acc", "ref", "csrftok", Settings())
    raw = resp.headers.getlist("set-cookie")
    by_name: dict[str, str] = {}
    for item in raw:
        name = item.split("=", 1)[0]
        by_name[name] = item

    access = by_name[ACCESS_TOKEN_COOKIE]
    assert "HttpOnly" in access
    assert "SameSite=lax" in access
    assert "Path=/" in access
    assert "Secure" not in access  # development 不设 Secure
    assert "Max-Age=900" in access  # 15 分钟

    refresh = by_name[REFRESH_TOKEN_COOKIE]
    assert "HttpOnly" in refresh
    assert "SameSite=lax" in refresh
    assert str(7 * 24 * 3600) in refresh

    csrf = by_name[CSRF_TOKEN_COOKIE]
    assert "HttpOnly" not in csrf  # 双重提交要求前端可读
    assert "SameSite=lax" in csrf


def test_set_auth_cookies_flags_production():
    """production: 三类 cookie 都带 Secure。"""
    from app.auth import _set_auth_cookies

    settings = Settings(**_PROD_SECRETS, environment="production", privileged_staff_ids="u1")
    resp = Response()
    _set_auth_cookies(resp, "acc", "ref", "csrftok", settings)
    for item in resp.headers.getlist("set-cookie"):
        assert "Secure" in item, item


# ── 2. /auth/me — cookie / Bearer / typ 隔离 ──────────────────────────────


def test_me_with_bearer_token(client):
    s = get_settings()
    token = _sign({"sub": "u1", "email": "a@b.com", "role": "admin"}, s, typ=TOKEN_TYPE_STAFF)
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["user_id"] == "u1"
    assert r.json()["role"] == "admin"


def test_me_with_session_cookie(client):
    """HttpOnly 会话 cookie 直接认证 (浏览器经同源 BFF 反代的路径)。"""
    s = get_settings()
    token = _sign({"sub": "u1", "email": "a@b.com", "role": "staff"}, s, typ=TOKEN_TYPE_STAFF)
    r = client.get("/auth/me", headers={"Cookie": f"{ACCESS_TOKEN_COOKIE}={token}"})
    assert r.status_code == 200
    assert r.json()["user_id"] == "u1"


def test_me_without_credentials_401(client):
    assert client.get("/auth/me").status_code == 401


def test_me_with_garbage_cookie_401(client):
    r = client.get("/auth/me", headers={"Cookie": f"{ACCESS_TOKEN_COOKIE}=not-a-jwt"})
    assert r.status_code == 401


def test_me_with_customer_cookie_401(client):
    """typ 隔离: 顾客 cookie 不能换员工主体。"""
    s = get_settings()
    token = _sign({"customer_id": "c1", "email": "c@b.com"}, s, typ=TOKEN_TYPE_CUSTOMER)
    r = client.get("/auth/me", headers={"Cookie": f"{ACCESS_TOKEN_COOKIE}={token}"})
    assert r.status_code == 401


def test_me_with_derived_key_capability_cookie_401(client):
    """能力令牌 (派生密钥) 即使签名有效也不是会话令牌。"""
    s = get_settings()
    token = sign_token(
        {"cart_id": "cart-1"},
        typ=TOKEN_TYPE_CART,
        secret=s.jwt_secret,
        algorithm=s.jwt_algorithm,
        expires_in_minutes=60,
    )
    r = client.get("/auth/me", headers={"Cookie": f"{ACCESS_TOKEN_COOKIE}={token}"})
    assert r.status_code == 401


# ── 3. /auth/refresh — staff / customer / 拒绝 ────────────────────────────


def test_refresh_with_staff_cookie(client):
    s = get_settings()
    refresh = _sign(
        {"sub": "u9", "email": "x@y.com", "role": "support"},
        s,
        typ=TOKEN_TYPE_STAFF,
        minutes=7 * 24 * 60,
    )
    r = client.post("/auth/refresh", headers={"Cookie": f"{REFRESH_TOKEN_COOKIE}={refresh}"})
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"]
    # 新 access token 写回 HttpOnly cookie
    set_cookie = r.headers.get("set-cookie", "")
    assert ACCESS_TOKEN_COOKIE in set_cookie
    assert "HttpOnly" in set_cookie


def test_refresh_with_customer_cookie_reissues_customer_token(client):
    """顾客 refresh → 新 access token 仍是 typ=customer (类型隔离不破)。"""
    from app.security.tokens import decode_token

    s = get_settings()
    refresh = _sign(
        {"customer_id": "c9", "email": "c@y.com"},
        s,
        typ=TOKEN_TYPE_CUSTOMER,
        minutes=7 * 24 * 60,
    )
    r = client.post("/auth/refresh", headers={"Cookie": f"{REFRESH_TOKEN_COOKIE}={refresh}"})
    assert r.status_code == 200
    payload = decode_token(
        r.json()["access_token"],
        typ=TOKEN_TYPE_CUSTOMER,
        secret=s.jwt_secret,
        algorithm=s.jwt_algorithm,
    )
    assert payload["customer_id"] == "c9"
    assert payload["typ"] == TOKEN_TYPE_CUSTOMER


def test_refresh_with_garbage_clears_cookies(client):
    r = client.post("/auth/refresh", headers={"Cookie": f"{REFRESH_TOKEN_COOKIE}=garbage"})
    assert r.status_code == 401
    # 失败时清会话 cookie
    for item in r.headers.get_list("set-cookie"):
        assert "Max-Age=0" in item or "=;" in item or '""' in item


def test_refresh_with_cart_capability_token_401(client):
    s = get_settings()
    token = sign_token(
        {"cart_id": "cart-9"},
        typ=TOKEN_TYPE_CART,
        secret=s.jwt_secret,
        algorithm=s.jwt_algorithm,
        expires_in_minutes=7 * 24 * 60,
    )
    r = client.post("/auth/refresh", headers={"Cookie": f"{REFRESH_TOKEN_COOKIE}={token}"})
    assert r.status_code == 401


def test_refresh_without_cookie_401(client):
    assert client.post("/auth/refresh").status_code == 401


# ── 4. CSRF 中间件 (生产模式) ─────────────────────────────────────────────


def _prod_app() -> FastAPI:
    settings = Settings(
        **_PROD_SECRETS,
        environment="production",
        privileged_staff_ids="u1",
        payment_gateway_provider="stripe",
        # 与仓库默认保持一致: 避免本机 .env 的 HEMALL_CORS_ORIGINS 污染测试
        cors_origins=["http://localhost:3000"],
    )
    app = FastAPI()
    app.add_middleware(CSRFMiddleware, settings=settings)

    @app.post("/things")
    def create_thing():
        return {"ok": True}

    @app.post("/auth/login")
    def login():
        return {"ok": True}

    return app


def test_csrf_production_rejects_missing_double_submit():
    with TestClient(_prod_app()) as c:
        r = c.post("/things")
        assert r.status_code == 403
        assert "CSRF" in r.json()["detail"]


def test_csrf_production_rejects_token_mismatch():
    with TestClient(_prod_app()) as c:
        r = c.post(
            "/things",
            headers={
                "X-CSRF-Token": "header-token",
                "Cookie": f"{CSRF_TOKEN_COOKIE}=cookie-token",
                "Origin": "http://localhost:3000",
            },
        )
        assert r.status_code == 403


def test_csrf_production_rejects_cross_origin():
    with TestClient(_prod_app()) as c:
        r = c.post(
            "/things",
            headers={
                "X-CSRF-Token": "same",
                "Cookie": f"{CSRF_TOKEN_COOKIE}=same",
                "Origin": "http://evil.example",
            },
        )
        assert r.status_code == 403
        assert "origin" in r.json()["detail"]


def test_csrf_production_accepts_double_submit_same_origin():
    with TestClient(_prod_app()) as c:
        r = c.post(
            "/things",
            headers={
                "X-CSRF-Token": "same",
                "Cookie": f"{CSRF_TOKEN_COOKIE}=same",
                "Origin": "http://localhost:3000",
            },
        )
        assert r.status_code == 200


def test_csrf_production_rejects_cross_site_even_with_token():
    with TestClient(_prod_app()) as c:
        r = c.post(
            "/things",
            headers={
                "X-CSRF-Token": "same",
                "Cookie": f"{CSRF_TOKEN_COOKIE}=same",
                "Origin": "http://localhost:3000",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert r.status_code == 403


def test_csrf_exempt_path_skips_check():
    """/auth/login 是公开端点，豁免 CSRF (但仍受其他中间件约束)。"""
    with TestClient(_prod_app()) as c:
        assert c.post("/auth/login").status_code == 200


def test_csrf_get_requests_not_checked():
    with TestClient(_prod_app()) as c:
        # GET 非状态改变方法，不查 CSRF
        assert c.get("/things").status_code in (200, 405)


# ── 5. 依赖层: cookie 主体解析 + typ 隔离 ────────────────────────────────


def test_get_current_user_from_cookie():
    s = Settings()
    token = _sign({"sub": "u7", "email": "e@f.com", "role": "staff"}, s, typ=TOKEN_TYPE_STAFF)
    principal = get_current_user(
        request=_req(f"{ACCESS_TOKEN_COOKIE}={token}"),
        creds=None,
        settings=s,
    )
    assert principal["user_id"] == "u7"
    assert principal["claims"]["typ"] == TOKEN_TYPE_STAFF


def test_get_current_user_cookie_typ_mismatch_401():
    """员工依赖拿顾客 cookie → 401 (不是"默认放行")。"""
    s = Settings()
    token = _sign({"customer_id": "c3"}, s, typ=TOKEN_TYPE_CUSTOMER)
    with pytest.raises(Exception) as ei:
        get_current_user(
            request=_req(f"{ACCESS_TOKEN_COOKIE}={token}"),
            creds=None,
            settings=s,
        )
    assert ei.value.status_code == 401


def test_get_current_customer_from_cookie():
    s = Settings()
    token = _sign({"customer_id": "c7", "email": "c@d.com"}, s, typ=TOKEN_TYPE_CUSTOMER)
    principal = get_current_customer(
        request=_req(f"{ACCESS_TOKEN_COOKIE}={token}"),
        creds=None,
        settings=s,
    )
    assert principal["customer_id"] == "c7"


def test_get_current_customer_staff_cookie_401():
    """顾客依赖拿员工 cookie → 401。"""
    s = Settings()
    token = _sign({"sub": "u8"}, s, typ=TOKEN_TYPE_STAFF)
    with pytest.raises(Exception) as ei:
        get_current_customer(
            request=_req(f"{ACCESS_TOKEN_COOKIE}={token}"),
            creds=None,
            settings=s,
        )
    assert ei.value.status_code == 401


def test_bearer_still_wins_over_cookie():
    """同时带 Bearer 与 cookie 时 Bearer 优先 (兼容 API 客户端)。"""
    s = Settings()
    bearer = _sign({"sub": "u-bearer", "role": "staff"}, s, typ=TOKEN_TYPE_STAFF)
    cookie = _sign({"sub": "u-cookie", "role": "staff"}, s, typ=TOKEN_TYPE_STAFF)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=bearer)
    principal = get_current_user(
        request=_req(f"{ACCESS_TOKEN_COOKIE}={cookie}"),
        creds=creds,
        settings=s,
    )
    assert principal["user_id"] == "u-bearer"


# ── 6. DB 集成: 登录 → cookie 会话端到端 ─────────────────────────────────

pytestmark_db = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration"
)


@pytestmark_db
def test_login_cookie_session_end_to_end(monkeypatch):
    """注册 → 登录 (Set-Cookie 标志) → /auth/me 走 cookie → refresh 续期。"""
    import uuid

    monkeypatch.setenv("HEMALL_PG_DSN", TEST_DSN)
    # 公开引导注册 (与 test_integration_optional 同路径; 生产默认关闭)
    monkeypatch.setenv("HEMALL_ALLOW_PUBLIC_ADMIN_REGISTRATION", "true")
    get_settings.cache_clear()
    from app.main import create_app

    email = f"cookie-{uuid.uuid4().hex[:8]}@hemall.test"
    with TestClient(create_app()) as c:
        r = c.post(
            "/customers/create_user",
            json={"email": email, "password": "supersecret", "name": "Cookie"},
        )
        assert r.status_code == 201, r.text

        r = c.post("/auth/login", json={"email": email, "password": "supersecret"})
        assert r.status_code == 200, r.text
        for item in r.headers.get_list("set-cookie"):
            if item.startswith(ACCESS_TOKEN_COOKIE):
                assert "HttpOnly" in item and "SameSite=lax" in item
            if item.startswith(CSRF_TOKEN_COOKIE):
                assert "HttpOnly" not in item

        # 后续请求自动带 cookie (TestClient cookie jar)
        r = c.get("/auth/me")
        assert r.status_code == 200
        assert r.json()["email"] == email

        # refresh: 旧 refresh cookie 换出新 access token
        r = c.post("/auth/refresh")
        assert r.status_code == 200
        assert r.json()["access_token"]
        assert c.get("/auth/me").status_code == 200

        # 登出清 cookie
        r = c.post("/auth/logout")
        assert r.status_code == 200
        assert c.get("/auth/me").status_code == 401
    get_settings.cache_clear()
