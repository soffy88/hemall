"""家庭套餐（V1.0 落地任务 D）测试：纯计算 + DB 集成 + HTTP 鉴权。

覆盖：适用人数闭合词表、明细校验（商品须存在、数量 > 0、不重复）、
上下架、顾客端只读已上架的、删除级联明细、无鉴权 401、越权 403。
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

from app.meal_package import (
    SERVES_LABELS,
    MealPackageNotFoundError,
    create_meal_package,
    delete_meal_package,
    ensure_meal_package_schema,
    get_meal_package,
    list_meal_packages,
    set_listed,
    update_meal_package,
)

# ── 纯计算 ──────────────────────────────────────────────────────────────


def test_serves_labels() -> None:
    assert SERVES_LABELS == {"single": "1人", "couple": "双人", "family": "家庭"}


# ── DB 集成 ─────────────────────────────────────────────────────────────

TEST_DSN = os.environ.get("TEST_PG_DSN")

needs_db = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set")
DB_TESTS = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


@pytest.fixture
async def meal_pool():
    from obase.persistence.pool import PgPool

    assert TEST_DSN is not None
    pool = await PgPool.create(name="hemall_meal_test", dsn=TEST_DSN, min_size=1, max_size=5)
    await ensure_meal_package_schema(pool)
    try:
        yield pool
    finally:
        await pool.close()


class Seed:
    """收集本测试种的行，teardown 时按 FK 安全顺序清掉。"""

    def __init__(self) -> None:
        self.products: list[str] = []
        self.packages: list[str] = []

    def tag(self) -> str:
        return uuid.uuid4().hex[:8]


@pytest.fixture
async def seed(meal_pool):
    s = Seed()
    yield s
    async with meal_pool.acquire() as conn:
        await conn.execute("DELETE FROM meal_package_line WHERE package_id = ANY($1)", s.packages)
        await conn.execute("DELETE FROM meal_package WHERE id = ANY($1)", s.packages)
        await conn.execute("DELETE FROM product WHERE id = ANY($1)", s.products)


async def make_product(pool, seed: Seed, tag: str) -> str:
    pid = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO product (id, title, slug, status, voucher_excluded,"
            " category_id, created_at, updated_at)"
            " VALUES ($1, $2, $3, 'published', false, NULL, NOW(), NOW())",
            pid,
            f"meal product {tag}",
            f"meal-slug-{tag}",
        )
    seed.products.append(pid)
    return pid


async def make_package(pool, seed: Seed, tag: str, **kwargs) -> dict:
    p1 = await make_product(pool, seed, f"a-{tag}")
    p2 = await make_product(pool, seed, f"b-{tag}")
    params = {
        "name": f"家庭套餐 {tag}",
        "serves": "family",
        "price_cents": 9900,
        "items": [
            {"product_id": p1, "quantity": 2},
            {"product_id": p2, "quantity": 1},
        ],
        "net_weight_note": "净重约 1.2kg",
        "allergen_note": "含花生",
    }
    params.update(kwargs)
    row = await create_meal_package(pool, **params)
    seed.packages.append(row["id"])
    return row


@needs_db
async def test_create_get_list_filters(meal_pool, seed) -> None:
    tag = seed.tag()
    pkg = await make_package(meal_pool, seed, tag)
    assert pkg["is_listed"] is False  # 缺省不上架
    assert pkg["serves_label"] == "家庭"
    assert pkg["net_weight_note"] == "净重约 1.2kg"
    assert pkg["allergen_note"] == "含花生"
    assert len(pkg["items"]) == 2
    assert pkg["items"][0]["quantity"] == 2
    assert pkg["items"][0]["product_title"] == f"meal product a-{tag}"
    got = await get_meal_package(meal_pool, pkg["id"])
    assert got["name"] == pkg["name"]
    assert len(got["items"]) == 2
    # 顾客端视角：未上架不可见
    with pytest.raises(MealPackageNotFoundError):
        await get_meal_package(meal_pool, pkg["id"], listed_only=True)
    assert all(r["is_listed"] for r in await list_meal_packages(meal_pool, listed_only=True))
    assert all(r["id"] != pkg["id"] for r in await list_meal_packages(meal_pool, listed_only=True))
    family = await list_meal_packages(meal_pool, serves="family")
    assert any(r["id"] == pkg["id"] for r in family)
    single = await list_meal_packages(meal_pool, serves="single")
    assert all(r["serves"] == "single" for r in single)
    assert all(r["id"] != pkg["id"] for r in single)


@needs_db
async def test_item_validation(meal_pool, seed) -> None:
    tag = seed.tag()
    good = await make_product(meal_pool, seed, f"g-{tag}")
    # 适用人数不在闭合词表 / 套餐价须 > 0 / 明细须非空
    with pytest.raises(ValueError):
        await create_meal_package(
            meal_pool,
            name="x",
            serves="party",
            price_cents=100,
            items=[{"product_id": good, "quantity": 1}],
        )
    with pytest.raises(ValueError):
        await create_meal_package(
            meal_pool,
            name="x",
            serves="single",
            price_cents=0,
            items=[{"product_id": good, "quantity": 1}],
        )
    with pytest.raises(ValueError):
        await create_meal_package(meal_pool, name="x", serves="single", price_cents=100, items=[])
    # 数量须 > 0；同一商品重复 400；商品不存在 404
    with pytest.raises(ValueError):
        await create_meal_package(
            meal_pool,
            name="x",
            serves="single",
            price_cents=100,
            items=[{"product_id": good, "quantity": 0}],
        )
    with pytest.raises(ValueError):
        await create_meal_package(
            meal_pool,
            name="x",
            serves="single",
            price_cents=100,
            items=[
                {"product_id": good, "quantity": 1},
                {"product_id": good, "quantity": 2},
            ],
        )
    with pytest.raises(MealPackageNotFoundError):
        await create_meal_package(
            meal_pool,
            name="x",
            serves="single",
            price_cents=100,
            items=[{"product_id": str(uuid.uuid4()), "quantity": 1}],
        )
    with pytest.raises(MealPackageNotFoundError):
        await get_meal_package(meal_pool, str(uuid.uuid4()))


@needs_db
async def test_update_replace_items_and_listing(meal_pool, seed) -> None:
    tag = seed.tag()
    pkg = await make_package(meal_pool, seed, tag)
    new_product = await make_product(meal_pool, seed, f"n-{tag}")
    # 改名 + 改价 + 整单替换明细
    updated = await update_meal_package(
        meal_pool,
        pkg["id"],
        name="双人套餐",
        serves="couple",
        price_cents=5900,
        items=[{"product_id": new_product, "quantity": 3}],
    )
    assert updated["name"] == "双人套餐"
    assert updated["serves"] == "couple"
    assert updated["serves_label"] == "双人"
    assert updated["price_cents"] == 5900
    assert len(updated["items"]) == 1
    assert updated["items"][0]["product_id"] == new_product
    assert updated["items"][0]["quantity"] == 3
    # 只改净菜属性，不碰明细
    noted = await update_meal_package(
        meal_pool, pkg["id"], storage_note="冷藏 0-4°C，24 小时内食用"
    )
    assert noted["storage_note"] == "冷藏 0-4°C，24 小时内食用"
    assert len(noted["items"]) == 1
    # 什么都不改 400；替换明细含不存在商品 404；替换为空 400
    with pytest.raises(ValueError):
        await update_meal_package(meal_pool, pkg["id"])
    with pytest.raises(MealPackageNotFoundError):
        await update_meal_package(
            meal_pool,
            pkg["id"],
            items=[{"product_id": str(uuid.uuid4()), "quantity": 1}],
        )
    with pytest.raises(ValueError):
        await update_meal_package(meal_pool, pkg["id"], items=[])
    # 上架 / 下架
    listed = await set_listed(meal_pool, pkg["id"], listed=True)
    assert listed["is_listed"] is True
    assert len(await list_meal_packages(meal_pool, listed_only=True)) >= 1
    assert any(r["id"] == pkg["id"] for r in await list_meal_packages(meal_pool, listed_only=True))
    unlisted = await set_listed(meal_pool, pkg["id"], listed=False)
    assert unlisted["is_listed"] is False
    assert all(r["id"] != pkg["id"] for r in await list_meal_packages(meal_pool, listed_only=True))
    with pytest.raises(MealPackageNotFoundError):
        await set_listed(meal_pool, str(uuid.uuid4()), listed=True)


@needs_db
async def test_delete_cascades_lines(meal_pool, seed) -> None:
    tag = seed.tag()
    pkg = await make_package(meal_pool, seed, tag)
    result = await delete_meal_package(meal_pool, pkg["id"])
    assert result == {"deleted": True, "package_id": pkg["id"]}
    seed.packages.remove(pkg["id"])
    with pytest.raises(MealPackageNotFoundError):
        await get_meal_package(meal_pool, pkg["id"])
    async with meal_pool.acquire() as conn:
        left = await conn.fetchval(
            "SELECT COUNT(*) FROM meal_package_line WHERE package_id = $1", pkg["id"]
        )
    assert left == 0
    with pytest.raises(MealPackageNotFoundError):
        await delete_meal_package(meal_pool, pkg["id"])


# ── HTTP 鉴权 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("post", "/store/meal-packages"),
        ("get", "/store/meal-packages/manage"),
        ("get", "/store/meal-packages/manage/00000000-0000-0000-0000-000000000000"),
        ("patch", "/store/meal-packages/00000000-0000-0000-0000-000000000000"),
        ("delete", "/store/meal-packages/00000000-0000-0000-0000-000000000000"),
        ("post", "/store/meal-packages/00000000-0000-0000-0000-000000000000/list"),
        ("post", "/store/meal-packages/00000000-0000-0000-0000-000000000000/unlist"),
    ],
)
def test_missing_token_401(client, method, url):
    if method == "get":
        r = client.get(url)
    elif method == "delete":
        r = client.delete(url)
    else:
        r = getattr(client, method)(url, json={})
    assert r.status_code == 401


@pytest.fixture
async def meal_client(monkeypatch, tmp_path):
    """真实 DB + 可切换顾客主体的 TestClient；商家门禁走真实查库。"""

    from obase.persistence.pool import PgPool

    from app.deps import get_current_customer, get_settings
    from app.meal_package import ensure_meal_package_schema
    from app.p0_schema import ensure_p0_schema

    pool = await PgPool.create(name="hemall_meal_http_test", dsn=TEST_DSN, min_size=1, max_size=5)
    await ensure_p0_schema(pool)
    await ensure_meal_package_schema(pool)

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
    state: dict = {"customers": [], "products": [], "packages": []}
    merchant_id = str(uuid.uuid4())
    plain_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        for cid, merchant in ((merchant_id, True), (plain_id, False)):
            await conn.execute(
                'INSERT INTO "customer" (id, email, status, is_merchant, created_at, updated_at)'
                " VALUES ($1, $2, 'active', $3, NOW(), NOW())",
                cid,
                f"meal-http-{uuid.uuid4().hex[:8]}@test.dev",
                merchant,
            )
    state["customers"].extend([merchant_id, plain_id])
    http_seed = Seed()
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
        await conn.execute(
            "DELETE FROM meal_package_line WHERE package_id = ANY($1)", state["packages"]
        )
        await conn.execute("DELETE FROM meal_package WHERE id = ANY($1)", state["packages"])
        await conn.execute("DELETE FROM product WHERE id = ANY($1)", state["products"])
        await conn.execute('DELETE FROM "customer" WHERE id = ANY($1)', state["customers"])
    await pool.close()


@DB_TESTS
class TestMealPackageHttp:
    def test_non_merchant_403(self, meal_client):
        meal_client.as_principal(meal_client.plain_id)
        c = meal_client.client
        r = c.post(
            "/store/meal-packages",
            json={
                "name": "x",
                "serves": "single",
                "price_cents": 100,
                "items": [{"product_id": meal_client.product_id, "quantity": 1}],
            },
        )
        assert r.status_code == 403, r.text
        r = c.get("/store/meal-packages/manage")
        assert r.status_code == 403, r.text
        r = c.get("/store/meal-packages/manage/00000000-0000-0000-0000-000000000000")
        assert r.status_code == 403, r.text

    def test_public_list_only_shows_listed(self, meal_client):
        # 顾客端无需登录即可看列表 / 详情，但只能看到已上架的。
        c = meal_client.client
        r = c.post(
            "/store/meal-packages",
            json={
                "name": "家庭套餐",
                "serves": "family",
                "price_cents": 9900,
                "items": [{"product_id": meal_client.product_id, "quantity": 2}],
            },
        )
        assert r.status_code == 200, r.text
        pkg_id = r.json()["id"]
        meal_client.state["packages"].append(pkg_id)
        assert r.json()["is_listed"] is False
        r = c.get("/store/meal-packages")
        assert r.status_code == 200, r.text
        assert all(item["id"] != pkg_id for item in r.json()["items"])
        r = c.get(f"/store/meal-packages/{pkg_id}")
        assert r.status_code == 404, r.text
        r = c.post(f"/store/meal-packages/{pkg_id}/list")
        assert r.status_code == 200, r.text
        assert r.json()["is_listed"] is True
        r = c.get("/store/meal-packages")
        assert any(item["id"] == pkg_id for item in r.json()["items"])
        r = c.get(f"/store/meal-packages/{pkg_id}")
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "家庭套餐"
        assert len(r.json()["items"]) == 1
        r = c.get("/store/meal-packages", params={"serves": "single"})
        assert r.status_code == 200, r.text
        assert all(item["id"] != pkg_id for item in r.json()["items"])
        # 下架后顾客端再次不可见
        r = c.post(f"/store/meal-packages/{pkg_id}/unlist")
        assert r.status_code == 200, r.text
        assert r.json()["is_listed"] is False
        r = c.get("/store/meal-packages")
        assert all(item["id"] != pkg_id for item in r.json()["items"])

    def test_merchant_crud_flow(self, meal_client):
        c = meal_client.client
        # 明细校验走 HTTP：商品不存在 404、数量 0 报 400、重复商品 400
        r = c.post(
            "/store/meal-packages",
            json={
                "name": "x",
                "serves": "single",
                "price_cents": 100,
                "items": [{"product_id": str(uuid.uuid4()), "quantity": 1}],
            },
        )
        assert r.status_code == 404, r.text
        r = c.post(
            "/store/meal-packages",
            json={
                "name": "x",
                "serves": "single",
                "price_cents": 100,
                "items": [{"product_id": meal_client.product_id, "quantity": 0}],
            },
        )
        assert r.status_code == 400, r.text
        r = c.post(
            "/store/meal-packages",
            json={
                "name": "x",
                "serves": "single",
                "price_cents": 100,
                "items": [
                    {"product_id": meal_client.product_id, "quantity": 1},
                    {"product_id": meal_client.product_id, "quantity": 2},
                ],
            },
        )
        assert r.status_code == 400, r.text
        # 建 → 商家管理列表可见未上架 → 改 → 删
        r = c.post(
            "/store/meal-packages",
            json={
                "name": "双人套餐",
                "serves": "couple",
                "price_cents": 5900,
                "items": [{"product_id": meal_client.product_id, "quantity": 2}],
                "storage_note": "冷藏",
            },
        )
        assert r.status_code == 200, r.text
        pkg_id = r.json()["id"]
        meal_client.state["packages"].append(pkg_id)
        r = c.get("/store/meal-packages/manage")
        assert r.status_code == 200, r.text
        assert any(item["id"] == pkg_id for item in r.json()["items"])
        r = c.get(f"/store/meal-packages/manage/{pkg_id}")
        assert r.status_code == 200, r.text
        assert r.json()["storage_note"] == "冷藏"
        r = c.patch(
            f"/store/meal-packages/{pkg_id}",
            json={"name": "双人套餐·改", "price_cents": 6900},
        )
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "双人套餐·改"
        assert r.json()["price_cents"] == 6900
        assert len(r.json()["items"]) == 1  # 没传 items 则明细不动
        r = c.patch(f"/store/meal-packages/{pkg_id}", json={})
        assert r.status_code == 400, r.text  # 什么都不改
        r = c.delete(f"/store/meal-packages/{pkg_id}")
        assert r.status_code == 200, r.text
        assert r.json() == {"deleted": True, "package_id": pkg_id}
        meal_client.state["packages"].remove(pkg_id)
        r = c.get(f"/store/meal-packages/manage/{pkg_id}")
        assert r.status_code == 404, r.text
