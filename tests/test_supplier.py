"""供应商管理（B 部分）测试：纯计算 + DB 集成 + HTTP 鉴权。

覆盖：主备唯一性（同一品类第二家主要应被拒）、加权分计算正确、
无鉴权 401、越权 403；另覆盖报价须商品存在、周评分重复录入 409、
带依赖删除 409。
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.supplier import (
    SupplierConflictError,
    SupplierNotFoundError,
    create_supplier,
    delete_supplier,
    ensure_supplier_schema,
    get_evaluation,
    get_supplier,
    list_evaluations,
    list_prices,
    list_suppliers,
    primary_backup_for_category,
    record_evaluation,
    record_price,
    update_evaluation,
    update_supplier,
    validate_week,
    weighted_total,
)

# ── 纯计算 ──────────────────────────────────────────────────────────────


def _scores(price=80, quality=90, punctuality=70, stability=60, after=50, coop=40):
    return {
        "price_score": price,
        "quality_score": quality,
        "punctuality_score": punctuality,
        "stability_score": stability,
        "aftersales_score": after,
        "cooperation_score": coop,
    }


def test_weighted_total() -> None:
    # 80*.25 + 90*.30 + 70*.15 + 60*.15 + 50*.10 + 40*.05 = 73.50
    assert weighted_total(_scores()) == Decimal("73.50")
    assert weighted_total(_scores(100, 100, 100, 100, 100, 100)) == Decimal("100.00")
    assert weighted_total(_scores(0, 0, 0, 0, 0, 0)) == Decimal("0.00")
    assert weighted_total(_scores(1, 0, 0, 0, 0, 0)) == Decimal("0.25")
    assert weighted_total(_scores(1, 1, 1, 1, 1, 1)) == Decimal("1.00")


def test_validate_week() -> None:
    for good in ("2026-W01", "2026-W47", "2026-W53"):
        validate_week(good)
    for bad in ("2026-47", "2026-W54", "2026-W00", "2026-W1", "", "W47"):
        with pytest.raises(ValueError):
            validate_week(bad)


# ── DB 集成 ─────────────────────────────────────────────────────────────

TEST_DSN = os.environ.get("TEST_PG_DSN")

needs_db = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set")
DB_TESTS = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


@pytest.fixture
async def sup_pool():
    from obase.persistence.pool import PgPool

    assert TEST_DSN is not None
    pool = await PgPool.create(name="hemall_supplier_test", dsn=TEST_DSN, min_size=1, max_size=5)
    await ensure_supplier_schema(pool)
    try:
        yield pool
    finally:
        await pool.close()


class Seed:
    """收集本测试种的行，teardown 时按 FK 安全顺序清掉。"""

    def __init__(self) -> None:
        self.customers: list[str] = []
        self.products: list[str] = []
        self.suppliers: list[str] = []

    def tag(self) -> str:
        return uuid.uuid4().hex[:8]


@pytest.fixture
async def seed(sup_pool):
    s = Seed()
    yield s
    async with sup_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM procurement_supplier_evaluation WHERE supplier_id = ANY($1)", s.suppliers
        )
        await conn.execute(
            "DELETE FROM procurement_supplier_price WHERE supplier_id = ANY($1)",
            s.suppliers,
        )
        await conn.execute("DELETE FROM procurement_supplier WHERE id = ANY($1)", s.suppliers)
        await conn.execute("DELETE FROM product WHERE id = ANY($1)", s.products)
        await conn.execute('DELETE FROM "customer" WHERE id = ANY($1)', s.customers)


async def make_customer(pool, seed: Seed, tag: str, *, is_merchant: bool = False) -> str:
    cid = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO "customer" (id, email, status, is_merchant, created_at, updated_at)'
            " VALUES ($1, $2, 'active', $3, NOW(), NOW())",
            cid,
            f"sup-{tag}@test.dev",
            is_merchant,
        )
    seed.customers.append(cid)
    return cid


async def make_product(pool, seed: Seed, tag: str) -> str:
    pid = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO product (id, title, slug, status, voucher_excluded,"
            " created_at, updated_at) VALUES ($1, $2, $3, 'published', false, NOW(), NOW())",
            pid,
            f"sup product {tag}",
            f"sup-slug-{tag}",
        )
    seed.products.append(pid)
    return pid


async def make_supplier(pool, seed: Seed, tag: str, **kwargs) -> dict:
    params = {"name": f"sup-{tag}", "category": "veg"}
    params.update(kwargs)
    row = await create_supplier(pool, **params)
    seed.suppliers.append(row["id"])
    return row


@needs_db
async def test_create_and_lookup_primary_backup(sup_pool, seed) -> None:
    tag = seed.tag()
    primary = await make_supplier(sup_pool, seed, f"p-{tag}", category="veg", is_primary=True)
    backup = await make_supplier(sup_pool, seed, f"b-{tag}", category="veg", is_backup=True)
    other = await make_supplier(sup_pool, seed, f"o-{tag}", category="fruit", is_primary=True)
    got = await primary_backup_for_category(sup_pool, "veg")
    assert got["category"] == "veg"
    assert got["primary"]["id"] == primary["id"]
    assert got["backup"]["id"] == backup["id"]
    assert (await primary_backup_for_category(sup_pool, "fruit"))["primary"]["id"] == other["id"]
    assert (await primary_backup_for_category(sup_pool, "none"))["primary"] is None
    veg = await list_suppliers(sup_pool, category="veg")
    assert {r["id"] for r in veg} == {primary["id"], backup["id"]}
    assert (await get_supplier(sup_pool, primary["id"]))["name"] == f"sup-p-{tag}"


@needs_db
async def test_second_primary_and_backup_rejected(sup_pool, seed) -> None:
    tag = seed.tag()
    await make_supplier(sup_pool, seed, f"p-{tag}", category="veg", is_primary=True)
    with pytest.raises(SupplierConflictError):
        await make_supplier(sup_pool, seed, f"p2-{tag}", category="veg", is_primary=True)
    await make_supplier(sup_pool, seed, f"b-{tag}", category="veg", is_backup=True)
    with pytest.raises(SupplierConflictError):
        await make_supplier(sup_pool, seed, f"b2-{tag}", category="veg", is_backup=True)
    # 不同品类不受影响
    await make_supplier(sup_pool, seed, f"p3-{tag}", category="fruit", is_primary=True)


@needs_db
async def test_supplier_cannot_be_both_primary_and_backup(sup_pool, seed) -> None:
    with pytest.raises(ValueError):
        await create_supplier(
            sup_pool, name="both", category="veg", is_primary=True, is_backup=True
        )


@needs_db
async def test_update_primary_conflict_then_ok_after_deactivate(sup_pool, seed) -> None:
    tag = seed.tag()
    first = await make_supplier(sup_pool, seed, f"a-{tag}", category="veg", is_primary=True)
    second = await make_supplier(sup_pool, seed, f"b-{tag}", category="veg")
    with pytest.raises(SupplierConflictError):
        await update_supplier(sup_pool, second["id"], is_primary=True)
    await update_supplier(sup_pool, first["id"], status="inactive")
    updated = await update_supplier(sup_pool, second["id"], is_primary=True)
    assert updated["is_primary"] is True
    got = await primary_backup_for_category(sup_pool, "veg")
    assert got["primary"]["id"] == second["id"]


@needs_db
async def test_record_price_requires_existing_product(sup_pool, seed) -> None:
    tag = seed.tag()
    supplier = await make_supplier(sup_pool, seed, f"s-{tag}", category="veg")
    with pytest.raises(SupplierNotFoundError):
        await record_price(
            sup_pool,
            supplier["id"],
            product_id=str(uuid.uuid4()),
            price_cents=500,
            quoted_date="2026-11-20",
        )
    product = await make_product(sup_pool, seed, f"p-{tag}")
    row = await record_price(
        sup_pool,
        supplier["id"],
        product_id=product,
        price_cents=500,
        quoted_date="2026-11-20",
        note="first quote",
    )
    assert row["price_cents"] == 500
    assert row["quoted_date"] == "2026-11-20"
    assert row["product_id"] == product
    prices = await list_prices(sup_pool, supplier["id"])
    assert len(prices) == 1
    with pytest.raises(ValueError):
        await record_price(
            sup_pool,
            supplier["id"],
            product_id=product,
            price_cents=0,
            quoted_date="2026-11-20",
        )
    with pytest.raises(ValueError):
        await record_price(
            sup_pool,
            supplier["id"],
            product_id=product,
            price_cents=500,
            quoted_date="20 Nov 2026",
        )


@needs_db
async def test_evaluation_total_computed_and_duplicate_week_rejected(sup_pool, seed) -> None:
    tag = seed.tag()
    supplier = await make_supplier(sup_pool, seed, f"s-{tag}", category="veg")
    row = await record_evaluation(sup_pool, supplier["id"], week="2026-W47", **_scores())
    assert row["total_score"] == 73.5
    assert row["week"] == "2026-W47"
    with pytest.raises(SupplierConflictError):
        await record_evaluation(sup_pool, supplier["id"], week="2026-W47", **_scores())
    assert (await get_evaluation(sup_pool, supplier["id"], "2026-W47"))["total_score"] == 73.5
    updated = await update_evaluation(
        sup_pool, supplier["id"], "2026-W47", **_scores(100, 100, 100, 100, 100, 100)
    )
    assert updated["total_score"] == 100.0
    assert len(await list_evaluations(sup_pool, supplier["id"])) == 1
    with pytest.raises(ValueError):
        await record_evaluation(sup_pool, supplier["id"], week="2026-W48", **_scores(price=101))
    with pytest.raises(ValueError):
        await record_evaluation(sup_pool, supplier["id"], week="2026-48", **_scores())


@needs_db
async def test_delete_blocked_with_dependents_and_clean_delete(sup_pool, seed) -> None:
    tag = seed.tag()
    supplier = await make_supplier(sup_pool, seed, f"s-{tag}", category="veg")
    product = await make_product(sup_pool, seed, f"p-{tag}")
    await record_price(
        sup_pool,
        supplier["id"],
        product_id=product,
        price_cents=500,
        quoted_date="2026-11-20",
    )
    with pytest.raises(SupplierConflictError):
        await delete_supplier(sup_pool, supplier["id"])
    clean = await make_supplier(sup_pool, seed, f"c-{tag}", category="veg")
    assert (await delete_supplier(sup_pool, clean["id"]))["deleted"] is True
    with pytest.raises(SupplierNotFoundError):
        await get_supplier(sup_pool, clean["id"])
    with pytest.raises(SupplierNotFoundError):
        await get_supplier(sup_pool, str(uuid.uuid4()))


# ── HTTP 鉴权 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("get", "/store/suppliers"),
        ("get", "/store/suppliers/by-category/veg"),
        ("post", "/store/suppliers"),
    ],
)
def test_missing_token_401(client, method, url):
    if method == "get":
        r = client.get(url)
    else:
        r = getattr(client, method)(url, json={})
    assert r.status_code == 401


@pytest.fixture
async def sup_client(monkeypatch, tmp_path):
    """真实 DB + 可切换顾客主体的 TestClient；商家门禁走真实查库。"""

    from obase.persistence.pool import PgPool

    from app.deps import get_current_customer, get_settings
    from app.p0_schema import ensure_p0_schema

    pool = await PgPool.create(
        name="hemall_supplier_http_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await ensure_p0_schema(pool)
    await ensure_supplier_schema(pool)

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
    state: dict = {"customers": [], "products": [], "suppliers": []}
    merchant_id = str(uuid.uuid4())
    plain_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        for cid, merchant in ((merchant_id, True), (plain_id, False)):
            await conn.execute(
                'INSERT INTO "customer" (id, email, status, is_merchant, created_at, updated_at)'
                " VALUES ($1, $2, 'active', $3, NOW(), NOW())",
                cid,
                f"sup-http-{uuid.uuid4().hex[:8]}@test.dev",
                merchant,
            )
    state["customers"].extend([merchant_id, plain_id])
    http_seed = SimpleNamespace(customers=[], products=[], suppliers=[])
    product = await make_product(pool, http_seed, f"http-{uuid.uuid4().hex[:8]}")
    state["products"].extend(http_seed.products)

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
            product_id=product,
            as_principal=_as,
        )

    app.dependency_overrides.clear()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM procurement_supplier WHERE name LIKE 'sup-http-%'")
        await conn.execute("DELETE FROM product WHERE id = ANY($1)", state["products"])
        await conn.execute('DELETE FROM "customer" WHERE id = ANY($1)', state["customers"])
    await pool.close()


@DB_TESTS
class TestSupplierHttp:
    def test_non_merchant_403(self, sup_client):
        sup_client.as_principal(sup_client.plain_id)
        c = sup_client.client
        r = c.post(
            "/store/suppliers",
            json={"name": "sup-http-x", "category": "veg"},
        )
        assert r.status_code == 403, r.text
        r = c.get("/store/suppliers")
        assert r.status_code == 403, r.text

    def test_merchant_crud_flow(self, sup_client):
        c = sup_client.client
        r = c.post(
            "/store/suppliers",
            json={
                "name": "sup-http-primary",
                "contact_name": "Li",
                "phone": "13800000000",
                "category": "veg",
                "license_note": "license A",
                "is_primary": True,
            },
        )
        assert r.status_code == 200, r.text
        primary_id = r.json()["id"]
        assert r.json()["status"] == "active"
        r = c.post(
            "/store/suppliers",
            json={"name": "sup-http-primary2", "category": "veg", "is_primary": True},
        )
        assert r.status_code == 409, r.text
        r = c.post(
            "/store/suppliers",
            json={"name": "sup-http-backup", "category": "veg", "is_backup": True},
        )
        assert r.status_code == 200, r.text
        backup_id = r.json()["id"]
        r = c.get("/store/suppliers/by-category/veg")
        assert r.status_code == 200, r.text
        assert r.json()["primary"]["id"] == primary_id
        assert r.json()["backup"]["id"] == backup_id
        r = c.get(f"/store/suppliers/{primary_id}")
        assert r.status_code == 200, r.text
        assert r.json()["contact_name"] == "Li"
        r = c.patch(f"/store/suppliers/{primary_id}", json={"phone": "13900000000"})
        assert r.status_code == 200, r.text
        assert r.json()["phone"] == "13900000000"
        r = c.get("/store/suppliers?category=veg")
        assert r.status_code == 200, r.text
        assert len(r.json()["items"]) >= 2
        r = c.get(f"/store/suppliers/{uuid.uuid4()}")
        assert r.status_code == 404, r.text

    def test_price_and_evaluation_flow(self, sup_client):
        c = sup_client.client
        r = c.post(
            "/store/suppliers",
            json={"name": "sup-http-price", "category": "fruit"},
        )
        assert r.status_code == 200, r.text
        supplier_id = r.json()["id"]
        r = c.post(
            f"/store/suppliers/{supplier_id}/prices",
            json={
                "product_id": str(uuid.uuid4()),
                "price_cents": 500,
                "quoted_date": "2026-11-20",
            },
        )
        assert r.status_code == 404, r.text
        r = c.post(
            f"/store/suppliers/{supplier_id}/prices",
            json={
                "product_id": sup_client.product_id,
                "price_cents": 500,
                "quoted_date": "2026-11-20",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["price_cents"] == 500
        r = c.get(f"/store/suppliers/{supplier_id}/prices")
        assert r.status_code == 200, r.text
        assert len(r.json()["items"]) == 1
        scores = {
            "price_score": 80,
            "quality_score": 90,
            "punctuality_score": 70,
            "stability_score": 60,
            "aftersales_score": 50,
            "cooperation_score": 40,
        }
        r = c.post(
            f"/store/suppliers/{supplier_id}/evaluations",
            json={"week": "2026-W47", **scores},
        )
        assert r.status_code == 200, r.text
        assert r.json()["total_score"] == 73.5
        r = c.post(
            f"/store/suppliers/{supplier_id}/evaluations",
            json={"week": "2026-W47", **scores},
        )
        assert r.status_code == 409, r.text
        r = c.get(f"/store/suppliers/{supplier_id}/evaluations/2026-W47")
        assert r.status_code == 200, r.text
        assert r.json()["total_score"] == 73.5
        r = c.put(
            f"/store/suppliers/{supplier_id}/evaluations/2026-W47",
            json={
                **scores,
                "price_score": 100,
                "quality_score": 100,
                "punctuality_score": 100,
                "stability_score": 100,
                "aftersales_score": 100,
                "cooperation_score": 100,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["total_score"] == 100.0
        r = c.delete(f"/store/suppliers/{supplier_id}")
        assert r.status_code == 409, r.text
