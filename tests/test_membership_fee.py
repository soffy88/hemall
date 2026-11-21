"""会费账本（199 元/人）测试：服务层状态流转 + HTTP 端点鉴权。

覆盖：默认 unpaid、缴费 paid、减免 waived（须带原因）、
waived → paid 补缴、paid → waived 拒绝、重复记幂等、未知顾客 404、
无 token 401、非商家 403。会费只记账，不设门禁。
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

from app.membership import (
    MEMBERSHIP_FEE_DEFAULT_CENTS,
    ensure_membership_schema,
    get_membership_fee,
    record_membership_fee,
)

TEST_DSN = os.environ.get("TEST_PG_DSN")

needs_db = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set")
DB_TESTS = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


def test_fee_default_amount_is_19900() -> None:
    assert MEMBERSHIP_FEE_DEFAULT_CENTS == 19900


# ── 服务层 ──────────────────────────────────────────────────────────


@pytest.fixture
async def fee_pool():
    from obase.persistence.pool import PgPool

    from app.cangzhu import ensure_cangzhu_schema

    assert TEST_DSN is not None
    pool = await PgPool.create(name="hemall_fee_test", dsn=TEST_DSN, min_size=1, max_size=5)
    await ensure_cangzhu_schema(pool)
    await ensure_membership_schema(pool)
    try:
        yield pool
    finally:
        await pool.close()


class Seed:
    """收集本测试种的行，teardown 时按 FK 安全顺序清掉。"""

    def __init__(self) -> None:
        self.customers: list[str] = []

    def tag(self) -> str:
        return uuid.uuid4().hex[:8]


@pytest.fixture
async def seed(fee_pool):
    s = Seed()
    yield s
    async with fee_pool.acquire() as conn:
        await conn.execute("DELETE FROM membership_fee WHERE customer_id = ANY($1)", s.customers)
        await conn.execute('DELETE FROM "customer" WHERE id = ANY($1)', s.customers)


async def make_customer(pool, seed, tag, *, is_merchant=False):
    cid = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO "customer" (id, email, status, is_merchant, created_at, updated_at)'
            " VALUES ($1, $2, 'active', $3, NOW(), NOW())",
            cid,
            f"fee-{tag}@test.dev",
            is_merchant,
        )
    seed.customers.append(cid)
    return cid


@needs_db
async def test_get_unpaid_default(fee_pool, seed) -> None:
    customer = await make_customer(fee_pool, seed, seed.tag())
    async with fee_pool.acquire() as conn:
        fee = await get_membership_fee(conn, customer)
    assert fee["customer_id"] == customer
    assert fee["status"] == "unpaid"
    assert fee["amount_cents"] == 19900
    assert fee["recorded_by"] is None


@needs_db
async def test_record_paid(fee_pool, seed) -> None:
    tag = seed.tag()
    customer = await make_customer(fee_pool, seed, tag)
    operator = await make_customer(fee_pool, seed, f"op-{tag}", is_merchant=True)
    result = await record_membership_fee(
        fee_pool, customer_id=customer, status="paid", operator_id=operator
    )
    assert result["recorded"] is True
    assert result["status"] == "paid"
    assert result["amount_cents"] == 19900
    assert result["recorded_by"] == operator
    async with fee_pool.acquire() as conn:
        fee = await get_membership_fee(conn, customer)
    assert fee["status"] == "paid"
    assert fee["amount_cents"] == 19900


@needs_db
async def test_record_paid_idempotent(fee_pool, seed) -> None:
    customer = await make_customer(fee_pool, seed, seed.tag())
    first = await record_membership_fee(fee_pool, customer_id=customer, status="paid")
    assert first["recorded"] is True
    second = await record_membership_fee(fee_pool, customer_id=customer, status="paid")
    assert second["recorded"] is False
    assert second["reason"] == "already recorded"
    assert second["status"] == "paid"
    async with fee_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM membership_fee WHERE customer_id = $1", customer
        )
    assert count == 1


@needs_db
async def test_record_waived_requires_reason(fee_pool, seed) -> None:
    customer = await make_customer(fee_pool, seed, seed.tag())
    with pytest.raises(ValueError):
        await record_membership_fee(fee_pool, customer_id=customer, status="waived")
    with pytest.raises(ValueError):
        await record_membership_fee(fee_pool, customer_id=customer, status="waived", note="   ")
    result = await record_membership_fee(
        fee_pool, customer_id=customer, status="waived", note="首批免费"
    )
    assert result["recorded"] is True
    assert result["status"] == "waived"
    assert result["note"] == "首批免费"


@needs_db
async def test_waived_then_paid(fee_pool, seed) -> None:
    customer = await make_customer(fee_pool, seed, seed.tag())
    waived = await record_membership_fee(
        fee_pool, customer_id=customer, status="waived", note="首批免费"
    )
    assert waived["status"] == "waived"
    paid = await record_membership_fee(fee_pool, customer_id=customer, status="paid", note="后补缴")
    assert paid["recorded"] is True
    assert paid["status"] == "paid"
    assert paid["note"] == "后补缴"


@needs_db
async def test_paid_then_waive_rejected(fee_pool, seed) -> None:
    customer = await make_customer(fee_pool, seed, seed.tag())
    await record_membership_fee(fee_pool, customer_id=customer, status="paid")
    with pytest.raises(ValueError):
        await record_membership_fee(fee_pool, customer_id=customer, status="waived", note="想减免")


@needs_db
async def test_record_invalid_status_rejected(fee_pool, seed) -> None:
    customer = await make_customer(fee_pool, seed, seed.tag())
    with pytest.raises(ValueError):
        await record_membership_fee(fee_pool, customer_id=customer, status="unpaid")
    with pytest.raises(ValueError):
        await record_membership_fee(fee_pool, customer_id=customer, status="bogus")
    with pytest.raises(ValueError):
        await record_membership_fee(fee_pool, customer_id=customer, status="paid", amount_cents=0)


@needs_db
async def test_record_custom_amount(fee_pool, seed) -> None:
    customer = await make_customer(fee_pool, seed, seed.tag())
    result = await record_membership_fee(
        fee_pool, customer_id=customer, status="paid", amount_cents=10000
    )
    assert result["recorded"] is True
    assert result["amount_cents"] == 10000


@needs_db
async def test_unknown_customer_raises(fee_pool, seed) -> None:
    ghost = str(uuid.uuid4())
    async with fee_pool.acquire() as conn:
        with pytest.raises(ValueError):
            await get_membership_fee(conn, ghost)
    with pytest.raises(ValueError):
        await record_membership_fee(fee_pool, customer_id=ghost, status="paid")


# ── HTTP 端点 ─────────────────────────────────────────────────────────


@pytest.fixture
async def fee_client(monkeypatch, tmp_path):
    """真实 DB + 可切换顾客主体的 TestClient；商家门禁走真实查库。"""

    from obase.persistence.pool import PgPool

    from app.deps import get_current_customer, get_settings
    from app.ext.schema import ensure_ext_schema
    from app.p0_schema import ensure_p0_schema

    pool = await PgPool.create(name="hemall_fee_http_test", dsn=TEST_DSN, min_size=1, max_size=5)
    await ensure_p0_schema(pool)
    await ensure_ext_schema(pool)
    await ensure_membership_schema(pool)

    monkeypatch.setenv("HEMALL_PG_DSN", TEST_DSN)
    monkeypatch.setenv("HEMALL_OUTPUT_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "HEMALL_REDIS_URL",
        os.environ.get("HEMALL_REDIS_URL", "redis://localhost:6379/0"),
    )
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    state: dict = {"customers": []}
    merchant_id = str(uuid.uuid4())
    plain_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        for cid, merchant in ((merchant_id, True), (plain_id, False)):
            await conn.execute(
                'INSERT INTO "customer" (id, email, status, is_merchant, created_at, updated_at)'
                " VALUES ($1, $2, 'active', $3, NOW(), NOW())",
                cid,
                f"fee-http-{uuid.uuid4().hex[:8]}@test.dev",
                merchant,
            )
    state["customers"].extend([merchant_id, plain_id])

    with TestClient(app) as c:
        db_health = c.get("/health/ready").json()["checks"]["database"]["status"]
        if db_health != "up":
            pytest.skip("database not reachable at TEST_PG_DSN")

        def _as(cid: str) -> None:
            app.dependency_overrides[get_current_customer] = lambda: {
                "customer_id": cid,
                "email": "test@test.dev",
            }

        _as(merchant_id)
        yield SimpleNamespace(
            client=c,
            app=app,
            state=state,
            pool=pool,
            merchant_id=merchant_id,
            plain_id=plain_id,
            as_principal=_as,
        )

    app.dependency_overrides.clear()
    get_settings.cache_clear()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM membership_fee WHERE customer_id = ANY($1)", state["customers"]
        )
        await conn.execute('DELETE FROM "customer" WHERE id = ANY($1)', state["customers"])
    await pool.close()


@DB_TESTS
class TestMembershipFeeHttp:
    def test_merchant_records_paid_and_customer_self_check(self, fee_client):
        c = fee_client.client
        r = c.post(
            f"/store/customers/{fee_client.plain_id}/membership-fee",
            json={"status": "paid"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["recorded"] is True
        assert r.json()["status"] == "paid"
        assert r.json()["amount_cents"] == 19900
        # 顾客自查
        fee_client.as_principal(fee_client.plain_id)
        r = c.get("/store/customers/me/membership-fee")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "paid"
        assert r.json()["amount_cents"] == 19900
        # 商家查任意顾客
        fee_client.as_principal(fee_client.merchant_id)
        r = c.get(f"/store/customers/{fee_client.plain_id}/membership-fee")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "paid"

    def test_self_check_unpaid_default(self, fee_client):
        fee_client.as_principal(fee_client.plain_id)
        r = fee_client.client.get("/store/customers/me/membership-fee")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "unpaid"
        assert r.json()["amount_cents"] == 19900

    def test_non_merchant_cannot_record_or_view_others(self, fee_client):
        fee_client.as_principal(fee_client.plain_id)
        c = fee_client.client
        r = c.post(
            f"/store/customers/{fee_client.plain_id}/membership-fee",
            json={"status": "paid"},
        )
        assert r.status_code == 403, r.text
        r = c.get(f"/store/customers/{fee_client.merchant_id}/membership-fee")
        assert r.status_code == 403, r.text
        # 本人自查仍可
        r = c.get("/store/customers/me/membership-fee")
        assert r.status_code == 200, r.text

    def test_unauthenticated_401(self, fee_client):
        fee_client.app.dependency_overrides.clear()
        try:
            c = fee_client.client
            r = c.post(
                f"/store/customers/{fee_client.plain_id}/membership-fee",
                json={"status": "paid"},
            )
            assert r.status_code == 401, r.text
            r = c.get("/store/customers/me/membership-fee")
            assert r.status_code == 401, r.text
        finally:
            fee_client.as_principal(fee_client.merchant_id)

    def test_waived_requires_reason(self, fee_client):
        c = fee_client.client
        r = c.post(
            f"/store/customers/{fee_client.plain_id}/membership-fee",
            json={"status": "waived"},
        )
        assert r.status_code == 400, r.text
        r = c.post(
            f"/store/customers/{fee_client.plain_id}/membership-fee",
            json={"status": "waived", "note": "首批免费"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "waived"
        assert r.json()["note"] == "首批免费"
        # 减免后可补缴
        r = c.post(
            f"/store/customers/{fee_client.plain_id}/membership-fee",
            json={"status": "paid"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["recorded"] is True
        assert r.json()["status"] == "paid"

    def test_paid_then_waive_rejected_400(self, fee_client):
        c = fee_client.client
        r = c.post(
            f"/store/customers/{fee_client.plain_id}/membership-fee",
            json={"status": "paid"},
        )
        assert r.status_code == 200, r.text
        r = c.post(
            f"/store/customers/{fee_client.plain_id}/membership-fee",
            json={"status": "waived", "note": "想减免"},
        )
        assert r.status_code == 400, r.text

    def test_unknown_customer_404(self, fee_client):
        c = fee_client.client
        ghost = str(uuid.uuid4())
        r = c.get(f"/store/customers/{ghost}/membership-fee")
        assert r.status_code == 404, r.text
        r = c.post(
            f"/store/customers/{ghost}/membership-fee",
            json={"status": "paid"},
        )
        assert r.status_code == 404, r.text
