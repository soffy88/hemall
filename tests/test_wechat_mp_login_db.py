"""微信小程序登录/绑定 — DB 集成测试（需真实 Postgres TEST_PG_DSN，否则跳过）。

jscode2session 全程 mock（monkeypatch app.ext.wechat_mp.jscode2session），
不打外网。覆盖：
- wechat-login 未命中 → 新建客户（占位邮箱 + openid 落库）+ token 可调 me，
  me 只回 wechat_bound 布尔、不带 openid 专用字段，session_key 不出响应；
- 二次登录命中同一客户；
- 微信 errcode → 401（detail 带 errcode）；
- 未配置 appid/secret → 503；
- wechat-bind 成功/幂等；绑他人 openid → 409；本账号改绑 → 409；匿名 → 401。

自清理：autouse fixture 按 FK 安全顺序删本文件造的行（标记 wxmp- 邮箱 /
wx_openid-*@mp.local 占位邮箱），不给共享测试库留残留。
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest

TEST_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")

_FAKE_SESSIONS = {
    "code-alice": {
        "openid": "openid-alice-wxmp",
        "unionid": "union-alice-wxmp",
        "session_key": "sk-alice-should-never-leak",
    },
    "code-bob": {
        "openid": "openid-bob-wxmp",
        "unionid": None,
        "session_key": "sk-bob-should-never-leak",
    },
    "code-carol": {
        "openid": "openid-carol-wxmp",
        "unionid": None,
        "session_key": "sk-carol-should-never-leak",
    },
}


async def _fake_jscode2session(app_id, secret_value, code, transport=None):
    from app.ext.wechat_mp import WeChatMpError

    data = _FAKE_SESSIONS.get(code)
    if data is None:
        raise WeChatMpError(40029, "invalid code")
    return dict(data)


def _build_app(monkeypatch, *, configured: bool):
    monkeypatch.setenv("HEMALL_PG_DSN", TEST_DSN)
    monkeypatch.setenv(
        "HEMALL_REDIS_URL",
        os.environ.get("HEMALL_REDIS_URL", "redis://127.0.0.1:56379/0"),
    )
    monkeypatch.setenv("HEMALL_WECHAT_MP_APP_ID", "wxmp-test-appid" if configured else "")
    monkeypatch.setenv("HEMALL_WECHAT_MP_APP_SECRET", "wxmp-test-secret" if configured else "")
    from app.deps import get_settings

    get_settings.cache_clear()

    from app.main import create_app

    return create_app()


def _enter_app(monkeypatch, *, configured: bool):
    from fastapi.testclient import TestClient

    app = _build_app(monkeypatch, configured=configured)
    client = TestClient(app)
    client.__enter__()
    db_health = client.get("/health/ready").json()["checks"]["database"]["status"]
    if db_health != "up":
        client.__exit__(None, None, None)
        pytest.skip("database not reachable at TEST_PG_DSN")
    return client


@pytest.fixture
def db_client(monkeypatch):
    from app.ext import wechat_mp

    monkeypatch.setattr(wechat_mp, "jscode2session", _fake_jscode2session)
    client = _enter_app(monkeypatch, configured=True)
    try:
        yield client
    finally:
        client.__exit__(None, None, None)
        from app.deps import get_settings

        get_settings.cache_clear()


@pytest.fixture
def unconfigured_client(monkeypatch):
    client = _enter_app(monkeypatch, configured=False)
    try:
        yield client
    finally:
        client.__exit__(None, None, None)
        from app.deps import get_settings

        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _cleanup_own_rows():
    yield
    _run(_cleanup_rows())


async def _connect():
    return await asyncpg.connect(TEST_DSN)


async def _cleanup_rows():
    conn = await _connect()
    try:
        async with conn.transaction():
            cust = (
                "SELECT id FROM customer WHERE email LIKE 'wxmp-%@test.dev' "
                "OR email LIKE 'wx\\_openid-%@mp.local'"
            )
            orders = f"SELECT id FROM customer_order WHERE customer_id IN ({cust})"
            orders_text = f"SELECT id::text FROM customer_order WHERE customer_id IN ({cust})"
            await conn.execute(f"DELETE FROM order_line_item WHERE order_id IN ({orders})")
            await conn.execute(
                f"DELETE FROM inventory_reservation WHERE order_id IN ({orders_text})"
            )
            await conn.execute(f"DELETE FROM customer_order WHERE customer_id IN ({cust})")
            await conn.execute(
                "DELETE FROM cart_line_item WHERE cart_id IN "
                f"(SELECT id FROM cart WHERE customer_id IN ({cust}))"
            )
            await conn.execute(f"DELETE FROM cart WHERE customer_id IN ({cust})")
            await conn.execute(f"DELETE FROM customer_reserve_list WHERE customer_id IN ({cust})")
            await conn.execute(
                "DELETE FROM customer WHERE email LIKE 'wxmp-%@test.dev' "
                "OR email LIKE 'wx\\_openid-%@mp.local'"
            )
    finally:
        await conn.close()


def _run(coro):
    return asyncio.run(coro)


def _register(client, email: str) -> dict:
    resp = client.post(
        "/store/customers/register",
        json={
            "email": email,
            "password": "WxmpTest!234",
            "name": "WXMP Tester",
            "phone": "",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── wechat-login ────────────────────────────────────────────────────


def test_wechat_login_creates_customer_and_token_works(db_client):
    resp = db_client.post("/store/customers/wechat-login", json={"code": "code-alice"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email"] == "wx_openid-alice-wxmp@mp.local"
    assert body["access_token"]
    assert "sk-alice" not in resp.text

    me = db_client.get("/store/customers/me", headers=_auth(body["access_token"]))
    assert me.status_code == 200, me.text
    me_body = me.json()
    assert me_body["id"] == body["customer_id"]
    assert me_body["wechat_bound"] is True
    assert "wechat_openid" not in me_body
    assert "sk-alice" not in me.text


def test_wechat_login_second_time_same_customer(db_client):
    first = db_client.post("/store/customers/wechat-login", json={"code": "code-alice"}).json()
    second = db_client.post("/store/customers/wechat-login", json={"code": "code-alice"})
    assert second.status_code == 200, second.text
    assert second.json()["customer_id"] == first["customer_id"]


def test_wechat_login_wechat_errcode_is_401(db_client):
    resp = db_client.post("/store/customers/wechat-login", json={"code": "code-bad"})
    assert resp.status_code == 401, resp.text
    assert "40029" in resp.text


def test_wechat_login_unconfigured_is_503(unconfigured_client):
    resp = unconfigured_client.post("/store/customers/wechat-login", json={"code": "code-alice"})
    assert resp.status_code == 503, resp.text
    assert "微信登录未配置" in resp.text


# ── wechat-bind ─────────────────────────────────────────────────────


def test_wechat_bind_success_and_idempotent(db_client):
    account = _register(db_client, "wxmp-bind@test.dev")
    headers = _auth(account["access_token"])

    resp = db_client.post(
        "/store/customers/wechat-bind", json={"code": "code-bob"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["wechat_bound"] is True
    assert "openid-bob-wxmp" not in resp.text
    assert "sk-bob" not in resp.text

    again = db_client.post(
        "/store/customers/wechat-bind", json={"code": "code-bob"}, headers=headers
    )
    assert again.status_code == 200, again.text
    assert again.json()["wechat_bound"] is True


def test_wechat_bind_rebind_other_openid_conflict(db_client):
    account = _register(db_client, "wxmp-rebind@test.dev")
    headers = _auth(account["access_token"])
    assert (
        db_client.post(
            "/store/customers/wechat-bind", json={"code": "code-bob"}, headers=headers
        ).status_code
        == 200
    )
    resp = db_client.post(
        "/store/customers/wechat-bind", json={"code": "code-carol"}, headers=headers
    )
    assert resp.status_code == 409, resp.text


def test_wechat_bind_openid_taken_by_other_conflict(db_client):
    first = _register(db_client, "wxmp-first@test.dev")
    assert (
        db_client.post(
            "/store/customers/wechat-bind",
            json={"code": "code-bob"},
            headers=_auth(first["access_token"]),
        ).status_code
        == 200
    )
    second = _register(db_client, "wxmp-second@test.dev")
    resp = db_client.post(
        "/store/customers/wechat-bind",
        json={"code": "code-bob"},
        headers=_auth(second["access_token"]),
    )
    assert resp.status_code == 409, resp.text


def test_wechat_bind_anonymous_is_401(db_client):
    resp = db_client.post("/store/customers/wechat-bind", json={"code": "code-bob"})
    assert resp.status_code == 401, resp.text


def test_wechat_login_after_bind_hits_same_customer(db_client):
    account = _register(db_client, "wxmp-link@test.dev")
    assert (
        db_client.post(
            "/store/customers/wechat-bind",
            json={"code": "code-carol"},
            headers=_auth(account["access_token"]),
        ).status_code
        == 200
    )
    login = db_client.post("/store/customers/wechat-login", json={"code": "code-carol"})
    assert login.status_code == 200, login.text
    assert login.json()["customer_id"] == account["customer_id"]
    assert login.json()["email"] == "wxmp-link@test.dev"
