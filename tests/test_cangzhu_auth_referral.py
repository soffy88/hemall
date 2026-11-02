"""auth.py 注册推荐码绑定 — DB 集成测试（需真实 Postgres TEST_PG_DSN，否则跳过）。

覆盖（/auth/customer/register，店面 UI 实际走的 BFF 透传链）：
- 带有效推荐码注册 → 200，invited_by 落库，本人推荐码生成
- 带无效推荐码注册 → 400，且不产生孤儿账号
- 不带推荐码注册 → 200，invited_by 为空，本人推荐码生成
- /store/customers/register 带无效码 → 400（不是 500），不产生孤儿账号

自清理：autouse fixture 按 FK 安全顺序删本文件造的行（cz-auth- 前缀邮箱）。
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
from obase.uuid7 import uuid7

TEST_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


@pytest.fixture
def db_client(monkeypatch):
    monkeypatch.setenv("HEMALL_PG_DSN", TEST_DSN)
    monkeypatch.setenv(
        "HEMALL_REDIS_URL",
        os.environ.get("HEMALL_REDIS_URL", "redis://127.0.0.1:56379/0"),
    )
    from app.deps import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        if c.get("/health/ready").json()["checks"]["database"]["status"] != "up":
            pytest.skip("database not reachable at TEST_PG_DSN")
        yield c
    get_settings.cache_clear()


async def _cleanup_rows() -> None:
    conn = await asyncpg.connect(TEST_DSN)
    try:
        for table in ("voucher_ledger", "member_purchase_request", "invite_gift_claim"):
            await conn.execute(
                f"DELETE FROM {table} WHERE customer_id IN "
                "(SELECT id FROM customer WHERE email LIKE 'cz-auth-%@example.com')"
            )
        await conn.execute(
            "DELETE FROM cart WHERE customer_id IN "
            "(SELECT id FROM customer WHERE email LIKE 'cz-auth-%@example.com')"
        )
        await conn.execute("DELETE FROM customer WHERE email LIKE 'cz-auth-%@example.com'")
    finally:
        await conn.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _cleanup_own_rows():
    yield
    _run(_cleanup_rows())


async def _customer_row(email: str):
    conn = await asyncpg.connect(TEST_DSN)
    try:
        return await conn.fetchrow(
            'SELECT id, invited_by, referral_code FROM "customer" WHERE email = $1',
            email,
        )
    finally:
        await conn.close()


def _email(tag: str) -> str:
    return f"cz-auth-{tag}-{uuid7()}@example.com"


class TestAuthRegisterReferral:
    def test_register_with_valid_referral_code(self, db_client):
        referrer_email = _email("referrer")
        r = db_client.post(
            "/auth/customer/register",
            json={"email": referrer_email, "password": "CzAuthTest123", "name": "CZ Referrer"},
        )
        assert r.status_code == 200, r.text
        referrer = _run(_customer_row(referrer_email))
        assert referrer is not None
        code = referrer["referral_code"]
        assert code

        new_email = _email("referred")
        r = db_client.post(
            "/auth/customer/register",
            json={
                "email": new_email,
                "password": "CzAuthTest123",
                "name": "CZ Referred",
                "referral_code": code,
            },
        )
        assert r.status_code == 200, r.text
        row = _run(_customer_row(new_email))
        assert row is not None
        assert str(row["invited_by"]) == str(referrer["id"])
        assert row["referral_code"]

    def test_register_with_invalid_referral_code(self, db_client):
        email = _email("badcode")
        r = db_client.post(
            "/auth/customer/register",
            json={
                "email": email,
                "password": "CzAuthTest123",
                "name": "CZ Bad",
                "referral_code": "HM000000",
            },
        )
        assert r.status_code == 400, r.text
        assert _run(_customer_row(email)) is None

    def test_register_without_code(self, db_client):
        email = _email("nocode")
        r = db_client.post(
            "/auth/customer/register",
            json={"email": email, "password": "CzAuthTest123", "name": "CZ NoCode"},
        )
        assert r.status_code == 200, r.text
        row = _run(_customer_row(email))
        assert row is not None
        assert row["invited_by"] is None
        assert row["referral_code"]

    def test_storefront_register_with_invalid_referral_code(self, db_client):
        email = _email("badcode-sf")
        r = db_client.post(
            "/store/customers/register",
            json={
                "email": email,
                "password": "CzAuthTest123",
                "name": "CZ Bad SF",
                "referral_code": "HM000000",
            },
        )
        assert r.status_code == 400, r.text
        assert _run(_customer_row(email)) is None
