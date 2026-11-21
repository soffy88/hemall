"""会员补齐（缺口 4）测试：纯计算单元测试 + DB 集成测试（需 TEST_PG_DSN）。

覆盖：等级晋升边界、积分累积与口径排除（夜市/特价/退款不计——特价经
voucher_excluded 标记排除，夜市未接入故无夜市行可计）、储值记账与余额、
鉴权（无 token 401、非商家 403、非本人不可查他人总览）。
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

from app.membership import (
    accrue_points,
    ensure_membership_schema,
    get_membership_summary,
    list_points_entries,
    list_stored_value_entries,
    points_for_eligible,
    reversal_points,
    reverse_points_for_refund,
    tier_for_lifetime,
    tier_progress,
    topup_stored_value,
)

# ── 纯计算 ──────────────────────────────────────────────────────────────


def test_points_for_eligible() -> None:
    assert points_for_eligible(10000) == 100
    assert points_for_eligible(199) == 1
    assert points_for_eligible(99) == 0
    assert points_for_eligible(0) == 0
    assert points_for_eligible(-50) == 0


def test_tier_boundaries() -> None:
    assert tier_for_lifetime(0) == "regular"
    assert tier_for_lifetime(49999) == "regular"
    assert tier_for_lifetime(50000) == "silver"
    assert tier_for_lifetime(199999) == "silver"
    assert tier_for_lifetime(200000) == "gold"
    assert tier_for_lifetime(999999) == "gold"


def test_tier_progress() -> None:
    assert tier_progress(0) == {
        "current_tier": "regular",
        "next_tier": "silver",
        "next_threshold_cents": 50000,
        "remaining_cents": 50000,
    }
    progress = tier_progress(50000)
    assert progress["current_tier"] == "silver"
    assert progress["next_tier"] == "gold"
    assert progress["remaining_cents"] == 150000
    progress = tier_progress(200000)
    assert progress["current_tier"] == "gold"
    assert progress["next_tier"] is None
    assert progress["remaining_cents"] == 0


def test_reversal_points_proportional_and_capped() -> None:
    assert reversal_points(100, 5000, 10000) == 50
    assert reversal_points(100, 10000, 10000) == 100
    assert reversal_points(100, 20000, 10000) == 100
    assert reversal_points(100, 5000, 0) == 0
    assert reversal_points(0, 5000, 10000) == 0
    assert reversal_points(1, 1, 10000) == 0  # 不足 1 分不冲回


# ── DB 集成 ─────────────────────────────────────────────────────────────

TEST_DSN = os.environ.get("TEST_PG_DSN")

needs_db = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set")
DB_TESTS = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


@pytest.fixture
async def mb_pool():
    from obase.persistence.pool import PgPool

    from app.cangzhu import ensure_cangzhu_schema

    assert TEST_DSN is not None
    pool = await PgPool.create(name="hemall_membership_test", dsn=TEST_DSN, min_size=1, max_size=5)
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
        self.products: list[str] = []
        self.variants: list[str] = []
        self.orders: list[str] = []

    def tag(self) -> str:
        return uuid.uuid4().hex[:8]


@pytest.fixture
async def seed(mb_pool):
    s = Seed()
    yield s
    async with mb_pool.acquire() as conn:
        await conn.execute("DELETE FROM points_ledger WHERE customer_id = ANY($1)", s.customers)
        await conn.execute(
            "DELETE FROM stored_value_ledger WHERE customer_id = ANY($1)", s.customers
        )
        await conn.execute("DELETE FROM order_line_item WHERE order_id = ANY($1)", s.orders)
        await conn.execute("DELETE FROM customer_order WHERE id = ANY($1)", s.orders)
        await conn.execute("DELETE FROM product_variant WHERE id = ANY($1)", s.variants)
        await conn.execute("DELETE FROM product WHERE id = ANY($1)", s.products)
        await conn.execute('DELETE FROM "customer" WHERE id = ANY($1)', s.customers)


async def make_customer(pool, seed: Seed, tag: str, *, is_merchant: bool = False) -> str:
    cid = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO "customer" (id, email, status, is_merchant, created_at, updated_at)'
            " VALUES ($1, $2, 'active', $3, NOW(), NOW())",
            cid,
            f"mb-{tag}@test.dev",
            is_merchant,
        )
    seed.customers.append(cid)
    return cid


async def make_variant(pool, seed: Seed, tag: str, *, excluded: bool = False) -> str:
    pid = str(uuid.uuid4())
    vid = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO product (id, title, slug, status, voucher_excluded,"
            " created_at, updated_at) VALUES ($1, $2, $3, 'published', $4, NOW(), NOW())",
            pid,
            f"mb product {tag}",
            f"mb-slug-{tag}",
            excluded,
        )
        await conn.execute(
            "INSERT INTO product_variant (id, product_id, sku_code, option_values,"
            " status, created_at, updated_at) VALUES ($1, $2, $3, '{}'::jsonb,"
            " 'active', NOW(), NOW())",
            vid,
            pid,
            f"MB-{tag}",
        )
    seed.products.append(pid)
    seed.variants.append(vid)
    return vid


async def make_order(
    pool,
    seed: Seed,
    customer_id: str,
    lines: list[tuple[str, int, int]],
    *,
    status: str = "completed",
    shipping_cents: int = 0,
) -> str:
    oid = str(uuid.uuid4())
    subtotal = sum(qty * price for _, qty, price in lines)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO customer_order (id, customer_id, currency, status,"
            " subtotal_cents, discount_cents, tax_cents, shipping_cents,"
            " grand_total_cents, refunded_cents, created_at, updated_at, version,"
            " voucher_redeemed_cents)"
            " VALUES ($1, $2, 'CNY', $3, $4, 0, 0, $5, $6, 0, NOW(), NOW(), 0, 0)",
            oid,
            customer_id,
            status,
            subtotal,
            shipping_cents,
            subtotal + shipping_cents,
        )
        for variant_id, qty, price in lines:
            await conn.execute(
                "INSERT INTO order_line_item (id, order_id, variant_id, quantity,"
                " unit_price_cents, line_total_cents, created_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, NOW())",
                str(uuid.uuid4()),
                oid,
                variant_id,
                qty,
                price,
                qty * price,
            )
    seed.orders.append(oid)
    return oid


@needs_db
async def test_accrue_excludes_and_deducts_shipping(mb_pool, seed) -> None:
    tag = seed.tag()
    customer = await make_customer(mb_pool, seed, f"buy-{tag}")
    included = await make_variant(mb_pool, seed, f"in-{tag}")
    excluded = await make_variant(mb_pool, seed, f"ex-{tag}", excluded=True)
    order = await make_order(
        mb_pool,
        seed,
        customer,
        [(included, 1, 10000), (excluded, 1, 5000)],
        shipping_cents=500,
    )
    result = await accrue_points(mb_pool, order)
    assert result["accrued"] is True
    assert result["base_cents"] == 9500  # 10000 - 运费 500，排除行不计
    assert result["points"] == 95
    assert result["points_balance"] == 95
    assert result["member_tier"] == "regular"
    async with mb_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT kind, points, balance_after FROM points_ledger WHERE customer_id = $1",
            customer,
        )
        assert len(rows) == 1
        assert rows[0]["kind"] == "accrual"
        assert rows[0]["points"] == 95
        assert rows[0]["balance_after"] == 95


@needs_db
async def test_accrue_idempotent(mb_pool, seed) -> None:
    tag = seed.tag()
    customer = await make_customer(mb_pool, seed, f"buy-{tag}")
    variant = await make_variant(mb_pool, seed, f"v-{tag}")
    order = await make_order(mb_pool, seed, customer, [(variant, 1, 10000)])
    first = await accrue_points(mb_pool, order)
    assert first["accrued"] is True
    second = await accrue_points(mb_pool, order)
    assert second["accrued"] is False
    assert second["reason"] == "already accrued"
    async with mb_pool.acquire() as conn:
        balance = await conn.fetchval(
            'SELECT points_balance FROM "customer" WHERE id = $1', customer
        )
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM points_ledger WHERE customer_id = $1", customer
        )
    assert balance == 100
    assert count == 1


@needs_db
async def test_accrue_skips_non_completed_and_zero_base(mb_pool, seed) -> None:
    tag = seed.tag()
    customer = await make_customer(mb_pool, seed, f"buy-{tag}")
    variant = await make_variant(mb_pool, seed, f"v-{tag}")
    pending = await make_order(mb_pool, seed, customer, [(variant, 1, 10000)], status="confirmed")
    assert (await accrue_points(mb_pool, pending))["accrued"] is False
    excluded = await make_variant(mb_pool, seed, f"ex-{tag}", excluded=True)
    zero_base = await make_order(mb_pool, seed, customer, [(excluded, 1, 10000)])
    result = await accrue_points(mb_pool, zero_base)
    assert result["accrued"] is False
    assert result["reason"] == "zero eligible base"
    async with mb_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM points_ledger WHERE customer_id = $1", customer
        )
    assert count == 0


@needs_db
async def test_tier_promotion_across_thresholds(mb_pool, seed) -> None:
    tag = seed.tag()
    customer = await make_customer(mb_pool, seed, f"buy-{tag}")
    variant = await make_variant(mb_pool, seed, f"v-{tag}")
    first = await make_order(mb_pool, seed, customer, [(variant, 1, 60000)])
    result = await accrue_points(mb_pool, first)
    assert result["member_tier"] == "silver"
    assert result["lifetime_eligible_cents"] == 60000
    second = await make_order(mb_pool, seed, customer, [(variant, 1, 150000)])
    result = await accrue_points(mb_pool, second)
    assert result["member_tier"] == "gold"
    assert result["lifetime_eligible_cents"] == 210000
    assert result["points_balance"] == 2100


@needs_db
async def test_reversal_proportional_and_idempotent(mb_pool, seed) -> None:
    tag = seed.tag()
    customer = await make_customer(mb_pool, seed, f"buy-{tag}")
    variant = await make_variant(mb_pool, seed, f"v-{tag}")
    order = await make_order(mb_pool, seed, customer, [(variant, 1, 10000)])
    await accrue_points(mb_pool, order)
    first = await reverse_points_for_refund(
        mb_pool, refund_id=f"rf-{tag}-1", order_id=order, refund_cents=5000
    )
    assert first["reversed"] is True
    assert first["points"] == 50
    assert first["points_balance"] == 50
    assert first["lifetime_eligible_cents"] == 5000
    repeat = await reverse_points_for_refund(
        mb_pool, refund_id=f"rf-{tag}-1", order_id=order, refund_cents=5000
    )
    assert repeat["reversed"] is False
    assert repeat["reason"] == "already reversed"
    rest = await reverse_points_for_refund(
        mb_pool, refund_id=f"rf-{tag}-2", order_id=order, refund_cents=5000
    )
    assert rest["reversed"] is True
    assert rest["points"] == 50
    assert rest["points_balance"] == 0
    assert rest["lifetime_eligible_cents"] == 0
    assert rest["member_tier"] == "regular"


@needs_db
async def test_reversal_without_accrual(mb_pool, seed) -> None:
    tag = seed.tag()
    customer = await make_customer(mb_pool, seed, f"buy-{tag}")
    variant = await make_variant(mb_pool, seed, f"v-{tag}")
    order = await make_order(mb_pool, seed, customer, [(variant, 1, 10000)])
    result = await reverse_points_for_refund(
        mb_pool, refund_id=f"rf-{tag}", order_id=order, refund_cents=5000
    )
    assert result["reversed"] is False
    assert result["reason"] == "no posted accrual"


@needs_db
async def test_topup_and_duplicate_ref(mb_pool, seed) -> None:
    tag = seed.tag()
    customer = await make_customer(mb_pool, seed, f"buy-{tag}")
    first = await topup_stored_value(
        mb_pool,
        customer_id=customer,
        amount_cents=50000,
        external_payment_ref=f"PAY-{tag}-1",
        operator_id=None,
        note="线下收款",
    )
    assert first["recorded"] is True
    assert first["stored_value_balance_cents"] == 50000
    duplicate = await topup_stored_value(
        mb_pool,
        customer_id=customer,
        amount_cents=50000,
        external_payment_ref=f"PAY-{tag}-1",
    )
    assert duplicate["recorded"] is False
    assert duplicate["reason"] == "already recorded"
    assert duplicate["stored_value_balance_cents"] == 50000
    second = await topup_stored_value(
        mb_pool,
        customer_id=customer,
        amount_cents=40000,
        external_payment_ref=f"PAY-{tag}-2",
    )
    assert second["recorded"] is True
    assert second["stored_value_balance_cents"] == 90000
    async with mb_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM stored_value_ledger WHERE customer_id = $1", customer
        )
    assert count == 2


@needs_db
async def test_topup_validation(mb_pool, seed) -> None:
    tag = seed.tag()
    customer = await make_customer(mb_pool, seed, f"buy-{tag}")
    with pytest.raises(ValueError):
        await topup_stored_value(
            mb_pool, customer_id=customer, amount_cents=0, external_payment_ref="PAY-X"
        )
    with pytest.raises(ValueError):
        await topup_stored_value(
            mb_pool, customer_id=customer, amount_cents=100, external_payment_ref="  "
        )
    with pytest.raises(ValueError):
        await topup_stored_value(
            mb_pool,
            customer_id=customer,
            amount_cents=100,
            external_payment_ref="PAY-X",
            note="x" * 201,
        )
    with pytest.raises(ValueError):
        await topup_stored_value(
            mb_pool,
            customer_id=str(uuid.uuid4()),
            amount_cents=100,
            external_payment_ref="PAY-X",
        )


@needs_db
async def test_summary_and_lists(mb_pool, seed) -> None:
    tag = seed.tag()
    customer = await make_customer(mb_pool, seed, f"buy-{tag}")
    variant = await make_variant(mb_pool, seed, f"v-{tag}")
    order = await make_order(mb_pool, seed, customer, [(variant, 1, 60000)])
    await accrue_points(mb_pool, order)
    await topup_stored_value(
        mb_pool, customer_id=customer, amount_cents=30000, external_payment_ref=f"PAY-{tag}"
    )
    async with mb_pool.acquire() as conn:
        summary = await get_membership_summary(conn, customer)
        points = await list_points_entries(conn, customer, limit=50, offset=0)
        stored = await list_stored_value_entries(conn, customer, limit=50, offset=0)
    assert summary["member_tier"] == "silver"
    assert summary["points_balance"] == 600
    assert summary["stored_value_balance_cents"] == 30000
    assert summary["lifetime_eligible_cents"] == 60000
    assert summary["points_redeemable"] is False
    assert summary["points_expire"] is None
    assert len(points["entries"]) == 1
    assert points["entries"][0]["points"] == 600
    assert len(stored["entries"]) == 1
    assert stored["entries"][0]["amount_cents"] == 30000


# ── HTTP 鉴权 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("get", "/store/customers/me/membership"),
        ("get", "/store/points"),
        ("get", "/store/stored-value"),
        ("post", "/store/customers/00000000-0000-0000-0000-000000000000/stored-value/topup"),
        ("post", "/store/orders/00000000-0000-0000-0000-000000000000/points-accrue"),
    ],
)
def test_missing_token_401(client, method, url):
    if method == "get":
        r = client.get(url)
    else:
        r = getattr(client, method)(url, json={})
    assert r.status_code == 401


@pytest.fixture
async def mb_client(monkeypatch, tmp_path):
    """真实 DB + 可切换顾客主体的 TestClient；商家门禁走真实查库。

    直连池与 TestClient 各用各的连接（同一库），不跨 loop 共用连接池。
    """

    from obase.persistence.pool import PgPool

    from app.deps import get_current_customer, get_settings
    from app.ext.schema import ensure_ext_schema
    from app.p0_schema import ensure_p0_schema

    pool = await PgPool.create(
        name="hemall_membership_http_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
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
    state: dict = {"customers": [], "products": [], "variants": [], "orders": []}
    merchant_id = str(uuid.uuid4())
    plain_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        for cid, merchant in ((merchant_id, True), (plain_id, False)):
            await conn.execute(
                'INSERT INTO "customer" (id, email, status, is_merchant, created_at, updated_at)'
                " VALUES ($1, $2, 'active', $3, NOW(), NOW())",
                cid,
                f"mb-http-{uuid.uuid4().hex[:8]}@test.dev",
                merchant,
            )
    state["customers"].extend([merchant_id, plain_id])
    http_seed = SimpleNamespace(customers=[], products=[], variants=[], orders=[])
    variant = await make_variant(pool, http_seed, f"http-{uuid.uuid4().hex[:8]}")
    state["variants"].extend(http_seed.variants)
    state["products"].extend(http_seed.products)
    order_id = await make_order(pool, http_seed, plain_id, [(variant, 1, 60000)])
    state["orders"].append(order_id)

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
            state=state,
            pool=pool,
            merchant_id=merchant_id,
            plain_id=plain_id,
            order_id=order_id,
            as_principal=_as,
        )

    app.dependency_overrides.clear()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM points_ledger WHERE customer_id = ANY($1)", state["customers"]
        )
        await conn.execute(
            "DELETE FROM stored_value_ledger WHERE customer_id = ANY($1)", state["customers"]
        )
        await conn.execute("DELETE FROM order_line_item WHERE order_id = ANY($1)", state["orders"])
        await conn.execute("DELETE FROM customer_order WHERE id = ANY($1)", state["orders"])
        await conn.execute("DELETE FROM product_variant WHERE id = ANY($1)", state["variants"])
        await conn.execute("DELETE FROM product WHERE id = ANY($1)", state["products"])
        await conn.execute('DELETE FROM "customer" WHERE id = ANY($1)', state["customers"])
    await pool.close()


@DB_TESTS
class TestMembershipHttp:
    def test_merchant_self_summary_and_topup(self, mb_client):
        c = mb_client.client
        r = c.get("/store/customers/me/membership")
        assert r.status_code == 200, r.text
        assert r.json()["member_tier"] == "regular"
        r = c.post(
            f"/store/customers/{mb_client.plain_id}/stored-value/topup",
            json={"amount_cents": 50000, "external_payment_ref": "PAY-HTTP-1"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["recorded"] is True
        assert r.json()["stored_value_balance_cents"] == 50000
        r = c.get(f"/store/customers/{mb_client.plain_id}/membership")
        assert r.status_code == 200, r.text
        assert r.json()["stored_value_balance_cents"] == 50000

    def test_non_merchant_cannot_topup_or_view_others(self, mb_client):
        mb_client.as_principal(mb_client.plain_id)
        c = mb_client.client
        r = c.post(
            f"/store/customers/{mb_client.plain_id}/stored-value/topup",
            json={"amount_cents": 50000, "external_payment_ref": "PAY-HTTP-2"},
        )
        assert r.status_code == 403, r.text
        r = c.get(f"/store/customers/{mb_client.merchant_id}/membership")
        assert r.status_code == 403, r.text
        # 本人总览与账本仍可自查
        r = c.get("/store/customers/me/membership")
        assert r.status_code == 200, r.text
        r = c.get("/store/points")
        assert r.status_code == 200, r.text

    def test_points_accrue_and_reverse_via_http(self, mb_client):
        c = mb_client.client
        r = c.post(f"/store/orders/{mb_client.order_id}/points-accrue")
        assert r.status_code == 200, r.text
        assert r.json()["accrued"] is True
        assert r.json()["points"] == 600
        r = c.post(f"/store/orders/{mb_client.order_id}/points-accrue")
        assert r.json()["accrued"] is False
        r = c.post(
            f"/store/orders/{mb_client.order_id}/points-reverse",
            json={"refund_id": "rf-http-1", "refund_cents": 30000},
        )
        assert r.status_code == 200, r.text
        assert r.json()["reversed"] is True
        assert r.json()["points"] == 300

    def test_unknown_customer_and_order_404(self, mb_client):
        c = mb_client.client
        r = c.get(f"/store/customers/{uuid.uuid4()}/membership")
        assert r.status_code == 404, r.text
        r = c.post(f"/store/orders/{uuid.uuid4()}/points-accrue")
        assert r.status_code == 404, r.text
        r = c.post(
            f"/store/customers/{uuid.uuid4()}/stored-value/topup",
            json={"amount_cents": 100, "external_payment_ref": "PAY-HTTP-404"},
        )
        assert r.status_code == 404, r.text
