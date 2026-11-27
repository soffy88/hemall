"""分拣交付表 + 损耗表（V1.0 落地任务 C）测试：纯计算 + DB 集成 + HTTP 鉴权。

覆盖：状态机合法/非法流转（自提只能进待提货、配送只能进待配送）、
一单一表头唯一、行明细复核（改实配须留复核人）、损耗原因闭合词表、
每日汇总（总金额 + 按品类统计）、无鉴权 401、越权 403；
另覆盖损耗只记账不扣库存（记损耗前后库存行数不变）。
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

from app.sorting import (
    SortingConflictError,
    SortingNotFoundError,
    _allowed_next,
    add_line,
    create_sorting,
    ensure_sorting_schema,
    get_sorting,
    get_spoilage,
    list_lines,
    list_sortings,
    list_spoilage,
    record_spoilage,
    review_line,
    spoilage_daily_summary,
    update_sorting_note,
    update_sorting_status,
)

# ── 纯计算 ──────────────────────────────────────────────────────────────


def test_allowed_next_pickup_vs_delivery() -> None:
    assert _allowed_next("pending", "pickup") == ("sorting", "exception")
    assert _allowed_next("sorting", "pickup") == ("ready_pickup", "exception")
    assert _allowed_next("sorting", "delivery") == ("out_for_delivery", "exception")
    assert _allowed_next("ready_pickup", "pickup") == ("completed", "exception")
    assert _allowed_next("out_for_delivery", "delivery") == ("completed", "exception")
    assert _allowed_next("exception", "pickup") == ("sorting",)
    assert _allowed_next("completed", "pickup") == ()


# ── DB 集成 ─────────────────────────────────────────────────────────────

TEST_DSN = os.environ.get("TEST_PG_DSN")

needs_db = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set")
DB_TESTS = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


@pytest.fixture
async def sort_pool():
    from obase.persistence.pool import PgPool

    assert TEST_DSN is not None
    pool = await PgPool.create(name="hemall_sorting_test", dsn=TEST_DSN, min_size=1, max_size=5)
    await ensure_sorting_schema(pool)
    try:
        yield pool
    finally:
        await pool.close()


class Seed:
    """收集本测试种的行，teardown 时按 FK 安全顺序清掉。"""

    def __init__(self) -> None:
        self.customers: list[str] = []
        self.products: list[str] = []
        self.categories: list[str] = []
        self.orders: list[str] = []
        self.sortings: list[str] = []
        self.spoilages: list[str] = []

    def tag(self) -> str:
        return uuid.uuid4().hex[:8]


@pytest.fixture
async def seed(sort_pool):
    s = Seed()
    yield s
    async with sort_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM sorting_delivery_line WHERE sorting_id = ANY($1)", s.sortings
        )
        await conn.execute("DELETE FROM sorting_delivery WHERE id = ANY($1)", s.sortings)
        await conn.execute("DELETE FROM spoilage_record WHERE id = ANY($1)", s.spoilages)
        await conn.execute("DELETE FROM order_line_item WHERE order_id = ANY($1)", s.orders)
        await conn.execute("DELETE FROM customer_order WHERE id = ANY($1)", s.orders)
        await conn.execute("DELETE FROM product WHERE id = ANY($1)", s.products)
        await conn.execute("DELETE FROM product_category WHERE id = ANY($1)", s.categories)
        await conn.execute('DELETE FROM "customer" WHERE id = ANY($1)', s.customers)


async def make_customer(pool, seed: Seed, tag: str) -> str:
    cid = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO "customer" (id, email, status, is_merchant, created_at, updated_at)'
            " VALUES ($1, $2, 'active', false, NOW(), NOW())",
            cid,
            f"sort-{tag}@test.dev",
        )
    seed.customers.append(cid)
    return cid


async def make_category(pool, seed: Seed, tag: str) -> str:
    cat_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO product_category (id, name, slug) VALUES ($1, $2, $3)",
            cat_id,
            f"cat {tag}",
            f"sort-cat-{tag}",
        )
    seed.categories.append(cat_id)
    return cat_id


async def make_product(pool, seed: Seed, tag: str, *, category_id: str | None = None) -> str:
    pid = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO product (id, title, slug, status, voucher_excluded,"
            " category_id, created_at, updated_at)"
            " VALUES ($1, $2, $3, 'published', false, $4, NOW(), NOW())",
            pid,
            f"sort product {tag}",
            f"sort-slug-{tag}",
            category_id,
        )
    seed.products.append(pid)
    return pid


async def make_order(pool, seed: Seed, customer_id: str) -> str:
    oid = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO customer_order (id, customer_id, currency, status,"
            " subtotal_cents, discount_cents, tax_cents, shipping_cents,"
            " grand_total_cents, refunded_cents, created_at, updated_at, version,"
            " voucher_redeemed_cents)"
            " VALUES ($1, $2, 'CNY', 'paid', 1000, 0, 0, 0, 1000, 0, NOW(), NOW(), 0, 0)",
            oid,
            customer_id,
        )
    seed.orders.append(oid)
    return oid


async def make_sorting(pool, seed: Seed, tag: str, **kwargs) -> dict:
    customer = await make_customer(pool, seed, f"c-{tag}")
    order = await make_order(pool, seed, customer)
    params = {"order_id": order, "delivery_method": "pickup", "operator_id": customer}
    params.update(kwargs)
    row = await create_sorting(pool, **params)
    seed.sortings.append(row["id"])
    return row


@needs_db
async def test_create_sorting_one_per_order(sort_pool, seed) -> None:
    tag = seed.tag()
    sheet = await make_sorting(sort_pool, seed, tag)
    assert sheet["status"] == "pending"
    assert sheet["status_label"] == "待分拣"
    assert sheet["delivery_method_label"] == "自提"
    with pytest.raises(SortingConflictError):
        await create_sorting(
            sort_pool,
            order_id=sheet["order_id"],
            delivery_method="delivery",
        )
    with pytest.raises(SortingNotFoundError):
        await create_sorting(sort_pool, order_id=str(uuid.uuid4()), delivery_method="pickup")
    with pytest.raises(ValueError):
        customer = await make_customer(sort_pool, seed, f"c2-{tag}")
        order = await make_order(sort_pool, seed, customer)
        await create_sorting(sort_pool, order_id=order, delivery_method="drone")
    got = await get_sorting(sort_pool, sheet["id"])
    assert got["order_id"] == sheet["order_id"]
    assert got["lines"] == []
    pending = await list_sortings(sort_pool, status="pending")
    assert any(r["id"] == sheet["id"] for r in pending)
    completed = await list_sortings(sort_pool, status="completed")
    assert all(r["status"] == "completed" for r in completed)


@needs_db
async def test_status_machine_pickup_and_delivery(sort_pool, seed) -> None:
    tag = seed.tag()
    pickup = await make_sorting(sort_pool, seed, f"p-{tag}", delivery_method="pickup")
    delivery = await make_sorting(sort_pool, seed, f"d-{tag}", delivery_method="delivery")
    # 待分拣不能直跳待提货 / 已完成
    with pytest.raises(SortingConflictError):
        await update_sorting_status(sort_pool, pickup["id"], status="ready_pickup")
    with pytest.raises(SortingConflictError):
        await update_sorting_status(sort_pool, pickup["id"], status="completed")
    # 分拣中：自提只能进待提货，配送只能进待配送
    await update_sorting_status(sort_pool, pickup["id"], status="sorting")
    await update_sorting_status(sort_pool, delivery["id"], status="sorting")
    with pytest.raises(SortingConflictError):
        await update_sorting_status(sort_pool, pickup["id"], status="out_for_delivery")
    with pytest.raises(SortingConflictError):
        await update_sorting_status(sort_pool, delivery["id"], status="ready_pickup")
    await update_sorting_status(sort_pool, pickup["id"], status="ready_pickup")
    await update_sorting_status(sort_pool, delivery["id"], status="out_for_delivery")
    # 异常 -> 返工 -> 走完；已完成为终态
    await update_sorting_status(sort_pool, pickup["id"], status="exception")
    await update_sorting_status(sort_pool, pickup["id"], status="sorting")
    await update_sorting_status(sort_pool, pickup["id"], status="ready_pickup")
    await update_sorting_status(sort_pool, pickup["id"], status="completed")
    with pytest.raises(SortingConflictError):
        await update_sorting_status(sort_pool, pickup["id"], status="sorting")
    with pytest.raises(SortingConflictError):
        await update_sorting_status(sort_pool, pickup["id"], status="exception")
    # 备注可改，不碰状态
    noted = await update_sorting_note(sort_pool, delivery["id"], note="leave at door")
    assert noted["note"] == "leave at door"
    assert noted["status"] == "out_for_delivery"


@needs_db
async def test_line_review_requires_reviewer(sort_pool, seed) -> None:
    tag = seed.tag()
    sheet = await make_sorting(sort_pool, seed, tag)
    product = await make_product(sort_pool, seed, f"p-{tag}")
    line = await add_line(sort_pool, sheet["id"], product_id=product, expected_qty=5)
    assert line["actual_qty"] == 0
    assert line["reviewer"] == ""
    # 同一品重复加行 409；应配数量须 > 0
    with pytest.raises(SortingConflictError):
        await add_line(sort_pool, sheet["id"], product_id=product, expected_qty=1)
    with pytest.raises(ValueError):
        await add_line(
            sort_pool,
            sheet["id"],
            product_id=await make_product(sort_pool, seed, f"q-{tag}"),
            expected_qty=0,
        )
    # 改实配不留复核人 400；留了复核人即过；只改备注不用复核人
    with pytest.raises(ValueError):
        await review_line(sort_pool, sheet["id"], line["id"], actual_qty=4)
    reviewed = await review_line(sort_pool, sheet["id"], line["id"], actual_qty=4, reviewer="op-1")
    assert reviewed["actual_qty"] == 4
    assert reviewed["reviewer"] == "op-1"
    again = await review_line(sort_pool, sheet["id"], line["id"], actual_qty=5)
    assert again["actual_qty"] == 5  # 行上已有复核人，本次可不传
    noted = await review_line(sort_pool, sheet["id"], line["id"], note="one short")
    assert noted["note"] == "one short"
    assert noted["actual_qty"] == 5
    assert len(await list_lines(sort_pool, sheet["id"])) == 1
    with pytest.raises(ValueError):
        await review_line(sort_pool, sheet["id"], line["id"], actual_qty=-1, reviewer="op-1")
    # 出库后不可再加行 / 再复核
    await update_sorting_status(sort_pool, sheet["id"], status="sorting")
    await update_sorting_status(sort_pool, sheet["id"], status="ready_pickup")
    with pytest.raises(SortingConflictError):
        await add_line(
            sort_pool,
            sheet["id"],
            product_id=await make_product(sort_pool, seed, f"r-{tag}"),
            expected_qty=1,
        )
    with pytest.raises(SortingConflictError):
        await review_line(sort_pool, sheet["id"], line["id"], actual_qty=5, reviewer="op-1")


@needs_db
async def test_spoilage_closed_reasons_and_daily_summary(sort_pool, seed) -> None:
    tag = seed.tag()
    cat = await make_category(sort_pool, seed, tag)
    veg = await make_product(sort_pool, seed, f"v-{tag}", category_id=cat)
    plain = await make_product(sort_pool, seed, f"n-{tag}")
    day = "2026-11-27"
    row = await record_spoilage(
        sort_pool,
        product_id=veg,
        sku=f"SKU-{tag}",
        batch_no="B-001",
        quantity=3,
        cost_cents=1500,
        reason="expired",
        handling="报废",
        recorded_by="op-1",
        recorded_date=day,
    )
    seed.spoilages.append(row["id"])
    assert row["reason_label"] == "过期"
    assert row["recorded_date"] == day
    assert (await get_spoilage(sort_pool, row["id"]))["cost_cents"] == 1500
    # 原因不在闭合词表内 400；数量 / 金额非法 400；商品不存在 404
    with pytest.raises(ValueError):
        await record_spoilage(
            sort_pool, product_id=veg, sku="X", quantity=1, cost_cents=100, reason="虫蛀"
        )
    with pytest.raises(ValueError):
        await record_spoilage(
            sort_pool, product_id=veg, sku="X", quantity=0, cost_cents=100, reason="expired"
        )
    with pytest.raises(ValueError):
        await record_spoilage(
            sort_pool, product_id=veg, sku="X", quantity=1, cost_cents=-1, reason="expired"
        )
    with pytest.raises(SortingNotFoundError):
        await record_spoilage(
            sort_pool,
            product_id=str(uuid.uuid4()),
            sku="X",
            quantity=1,
            cost_cents=100,
            reason="expired",
        )
    second = await record_spoilage(
        sort_pool,
        product_id=plain,
        sku=f"SKU2-{tag}",
        quantity=2,
        cost_cents=700,
        reason="damaged",
        recorded_date=day,
    )
    seed.spoilages.append(second["id"])
    # 另一天的一笔不应计入当日汇总
    other_day = await record_spoilage(
        sort_pool,
        product_id=veg,
        sku=f"SKU-{tag}",
        quantity=10,
        cost_cents=9999,
        reason="spoiled",
        recorded_date="2026-11-26",
    )
    seed.spoilages.append(other_day["id"])
    summary = await spoilage_daily_summary(sort_pool, day)
    assert summary["record_count"] == 2
    assert summary["total_quantity"] == 5
    assert summary["total_cost_cents"] == 2200
    by_cat = {r["category"]: r for r in summary["by_category"]}
    assert by_cat[f"cat {tag}"]["total_cost_cents"] == 1500
    assert by_cat[f"cat {tag}"]["record_count"] == 1
    assert by_cat["未分类"]["total_cost_cents"] == 700
    assert len(await list_spoilage(sort_pool, recorded_date=day, product_id=veg)) == 1
    assert len(await list_spoilage(sort_pool, recorded_date=day, product_id=plain)) == 1
    empty = await spoilage_daily_summary(sort_pool, "2026-11-01")
    assert empty["record_count"] == 0
    assert empty["total_cost_cents"] == 0
    assert empty["by_category"] == []


@needs_db
async def test_spoilage_does_not_touch_inventory(sort_pool, seed) -> None:
    """损耗只记账：记前后 inventory_batch / stock_movement 行数不变。"""
    tag = seed.tag()
    product = await make_product(sort_pool, seed, f"p-{tag}")
    async with sort_pool.acquire() as conn:
        before_batch = await conn.fetchval("SELECT COUNT(*) FROM inventory_batch")
        before_move = await conn.fetchval("SELECT COUNT(*) FROM stock_movement")
    row = await record_spoilage(
        sort_pool,
        product_id=product,
        sku=f"SKU-{tag}",
        quantity=1,
        cost_cents=100,
        reason="weigh_error",
        recorded_date="2026-11-27",
    )
    seed.spoilages.append(row["id"])
    async with sort_pool.acquire() as conn:
        after_batch = await conn.fetchval("SELECT COUNT(*) FROM inventory_batch")
        after_move = await conn.fetchval("SELECT COUNT(*) FROM stock_movement")
    assert after_batch == before_batch
    assert after_move == before_move


# ── HTTP 鉴权 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("get", "/store/sorting-deliveries"),
        ("post", "/store/sorting-deliveries"),
        ("get", "/store/sorting-deliveries/00000000-0000-0000-0000-000000000000"),
        ("get", "/store/spoilage"),
        ("post", "/store/spoilage"),
        ("get", "/store/spoilage/daily-summary"),
    ],
)
def test_missing_token_401(client, method, url):
    if method == "get":
        r = client.get(url)
    else:
        r = getattr(client, method)(url, json={})
    assert r.status_code == 401


@pytest.fixture
async def sort_client(monkeypatch, tmp_path):
    """真实 DB + 可切换顾客主体的 TestClient；商家门禁走真实查库。"""

    from obase.persistence.pool import PgPool

    from app.deps import get_current_customer, get_settings
    from app.p0_schema import ensure_p0_schema
    from app.sorting import ensure_sorting_schema

    pool = await PgPool.create(
        name="hemall_sorting_http_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await ensure_p0_schema(pool)
    await ensure_sorting_schema(pool)

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
    state: dict = {"customers": [], "products": [], "orders": [], "sortings": [], "spoilages": []}
    merchant_id = str(uuid.uuid4())
    plain_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        for cid, merchant in ((merchant_id, True), (plain_id, False)):
            await conn.execute(
                'INSERT INTO "customer" (id, email, status, is_merchant, created_at, updated_at)'
                " VALUES ($1, $2, 'active', $3, NOW(), NOW())",
                cid,
                f"sort-http-{uuid.uuid4().hex[:8]}@test.dev",
                merchant,
            )
    state["customers"].extend([merchant_id, plain_id])
    http_seed = Seed()
    product = await make_product(pool, http_seed, f"http-{uuid.uuid4().hex[:8]}")
    state["products"].extend(http_seed.products)
    customer = await make_customer(pool, http_seed, f"buy-{uuid.uuid4().hex[:8]}")
    state["customers"].extend(http_seed.customers)
    order = await make_order(pool, http_seed, customer)
    state["orders"].extend(http_seed.orders)

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
            order_id=order,
            as_principal=_as,
        )

    app.dependency_overrides.clear()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM sorting_delivery_line WHERE sorting_id = ANY($1)", state["sortings"]
        )
        await conn.execute("DELETE FROM sorting_delivery WHERE id = ANY($1)", state["sortings"])
        await conn.execute("DELETE FROM spoilage_record WHERE id = ANY($1)", state["spoilages"])
        await conn.execute("DELETE FROM order_line_item WHERE order_id = ANY($1)", state["orders"])
        await conn.execute("DELETE FROM customer_order WHERE id = ANY($1)", state["orders"])
        await conn.execute("DELETE FROM product WHERE id = ANY($1)", state["products"])
        await conn.execute('DELETE FROM "customer" WHERE id = ANY($1)', state["customers"])
    await pool.close()


@DB_TESTS
class TestSortingSpoilageHttp:
    def test_non_merchant_403(self, sort_client):
        sort_client.as_principal(sort_client.plain_id)
        c = sort_client.client
        r = c.post(
            "/store/sorting-deliveries",
            json={"order_id": sort_client.order_id, "delivery_method": "pickup"},
        )
        assert r.status_code == 403, r.text
        r = c.get("/store/sorting-deliveries")
        assert r.status_code == 403, r.text
        r = c.post(
            "/store/spoilage",
            json={
                "product_id": sort_client.product_id,
                "sku": "SKU-X",
                "quantity": 1,
                "cost_cents": 100,
                "reason": "expired",
            },
        )
        assert r.status_code == 403, r.text

    def test_sorting_flow(self, sort_client):
        c = sort_client.client
        r = c.post(
            "/store/sorting-deliveries",
            json={"order_id": sort_client.order_id, "delivery_method": "pickup"},
        )
        assert r.status_code == 200, r.text
        sorting_id = r.json()["id"]
        sort_client.state["sortings"].append(sorting_id)
        assert r.json()["status"] == "pending"
        # 同一订单再建 409
        r = c.post(
            "/store/sorting-deliveries",
            json={"order_id": sort_client.order_id, "delivery_method": "delivery"},
        )
        assert r.status_code == 409, r.text
        # 加行 + 复核
        r = c.post(
            f"/store/sorting-deliveries/{sorting_id}/lines",
            json={"product_id": sort_client.product_id, "expected_qty": 5},
        )
        assert r.status_code == 200, r.text
        line_id = r.json()["id"]
        assert r.json()["actual_qty"] == 0
        r = c.patch(
            f"/store/sorting-deliveries/{sorting_id}/lines/{line_id}",
            json={"actual_qty": 4},
        )
        assert r.status_code == 400, r.text  # 复核须留复核人
        r = c.patch(
            f"/store/sorting-deliveries/{sorting_id}/lines/{line_id}",
            json={"actual_qty": 4, "reviewer": "op-1"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["actual_qty"] == 4
        assert r.json()["reviewer"] == "op-1"
        # 非法流转 409，再走完正常流转
        r = c.patch(f"/store/sorting-deliveries/{sorting_id}", json={"status": "completed"})
        assert r.status_code == 409, r.text
        for status in ("sorting", "ready_pickup", "completed"):
            r = c.patch(f"/store/sorting-deliveries/{sorting_id}", json={"status": status})
            assert r.status_code == 200, r.text
            assert r.json()["status"] == status
        r = c.get(f"/store/sorting-deliveries/{sorting_id}")
        assert r.status_code == 200, r.text
        assert len(r.json()["lines"]) == 1
        assert r.json()["lines"][0]["actual_qty"] == 4
        r = c.get("/store/sorting-deliveries?status=completed")
        assert r.status_code == 200, r.text
        assert any(item["id"] == sorting_id for item in r.json()["items"])
        r = c.get(f"/store/sorting-deliveries/{uuid.uuid4()}")
        assert r.status_code == 404, r.text

    def test_spoilage_flow_and_daily_summary(self, sort_client):
        c = sort_client.client
        r = c.post(
            "/store/spoilage",
            json={
                "product_id": sort_client.product_id,
                "sku": "SKU-HTTP",
                "batch_no": "B-HTTP",
                "quantity": 2,
                "cost_cents": 800,
                "reason": "damaged",
                "handling": "报废",
                "recorded_date": "2026-11-28",
            },
        )
        assert r.status_code == 200, r.text
        spoilage_id = r.json()["id"]
        sort_client.state["spoilages"].append(spoilage_id)
        assert r.json()["reason_label"] == "破损"
        r = c.post(
            "/store/spoilage",
            json={
                "product_id": sort_client.product_id,
                "sku": "SKU-HTTP",
                "quantity": 1,
                "cost_cents": 100,
                "reason": "虫蛀",
            },
        )
        assert r.status_code == 400, r.text
        r = c.get("/store/spoilage/daily-summary?date=2026-11-28")
        assert r.status_code == 200, r.text
        assert r.json()["record_count"] == 1
        assert r.json()["total_cost_cents"] == 800
        assert r.json()["by_category"][0]["category"] == "未分类"
        r = c.get(f"/store/spoilage/{spoilage_id}")
        assert r.status_code == 200, r.text
        assert r.json()["batch_no"] == "B-HTTP"
        r = c.get("/store/spoilage?recorded_date=2026-11-28")
        assert r.status_code == 200, r.text
        assert any(item["id"] == spoilage_id for item in r.json()["items"])
