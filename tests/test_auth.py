"""鉴权单元测试 — get_current_user / anon_ref / 登录端点降级行为。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from obase.crypto.util import CryptoUtil

from app.config import Settings
from app.deps import anon_ref, get_current_user, get_settings


def test_no_credentials_401():
    with pytest.raises(HTTPException) as ei:
        get_current_user(creds=None, settings=Settings())
    assert ei.value.status_code == 401


def test_invalid_token_401():
    bad = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not-a-jwt")
    with pytest.raises(HTTPException) as ei:
        get_current_user(creds=bad, settings=Settings())
    assert ei.value.status_code == 401


def test_valid_token_resolves_principal():
    s = Settings()
    token = CryptoUtil.jwt_sign(payload={"sub": "u42", "email": "a@b.com"}, secret=s.jwt_secret)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    principal = get_current_user(creds=creds, settings=s)
    assert principal["user_id"] == "u42"
    assert principal["email"] == "a@b.com"


def test_anon_ref_deterministic_and_24_chars():
    s = Settings()
    r1 = anon_ref("user-1", s)
    r2 = anon_ref("user-1", s)
    assert r1 == r2
    assert len(r1) == 24
    assert anon_ref("user-2", s) != r1  # 不同身份 → 不同引用


def test_login_without_db_returns_503(client):
    """无 Postgres 时 get_pool → 503 (登录也走连接池)。"""
    r = client.post("/auth/login", json={"email": "x@y.com", "password": "zzzzzzzz"})
    assert r.status_code == 503
