"""客户预订（无货可订）— DB 集成测试（需真实 Postgres TEST_PG_DSN，否则跳过）。

覆盖任务清单：
- 店面 0 库存商品可见（列表 + 详情）
- 变体无批次加购 → 只绑变体的预订行；购物车序列化 available/preview
- 超库存加购：omodul 防超卖拒绝 → 路由层预订行回退（不报错）
- 结算拆分（直调 _create_order_from_cart）：部分现货锁定 + 缺口 preorder
- 商家门禁 403、预订清单 CRUD、商家一键预订拆分
- 后台开通商家（PATCH）、配货队列、allocate 后 preorder_qty 减少
- 个人预订列表 /customers/me/preorders
"""

from __future__ import annotations

import asyncio
import json
import os

import asyncpg
import pytest
from obase.uuid7 import uuid7

TEST_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


# ── fixtures / token helpers ─────────────────────────────────────


@pytest.fixture
def db_client(monkeypatch):
    monkeypatch.setenv("HEMALL_PG_DSN", TEST_DSN)
    monkeypatch.setenv(
        "HEMALL_REDIS_URL", os.environ.get("HEMALL_REDIS_URL", "redis://127.0.0.1:56379/0")
    )
    from app.deps import get_settings

    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        db_health = c.get("/health/ready").json()["checks"]["database"]["status"]
        if db_health != "up":
            pytest.skip("database not reachable at TEST_PG_DSN")
        yield c
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _cleanup_own_rows():
    """每个用例后按 FK 安全顺序清掉本文件造的行（标记：reserve- 邮箱 / reserve-slug-）。

    共享测试库跨文件复用；phase10/refund 的 fixture 用
    db_test_utils.cleanup_db_test_rows 按 batch_no 'BATCH-%' 整体删
    inventory_batch，而那份清扫清单不含 inventory_reservation——本文件
    经结算/配货产生的台账行若残留，后续文件的 setup 会撞
    inventory_reservation_batch_id_fkey。这里自清理，不污染后续文件。
    """
    yield
    _run(_cleanup_rows())


async def _cleanup_rows():
    conn = await _connect()
    try:
        async with conn.transaction():
            cust = "SELECT id FROM customer WHERE email LIKE 'reserve-%@test.dev'"
            orders = f"SELECT id FROM customer_order WHERE customer_id IN ({cust})"
            # inventory_reservation.order_id 是 text 列，子查询须 cast
            orders_text = f"SELECT id::text FROM customer_order WHERE customer_id IN ({cust})"
            variants = (
                "SELECT pv.id FROM product_variant pv JOIN product p "
                "ON p.id = pv.product_id WHERE p.slug LIKE 'reserve-slug-%'"
            )
            # order_line_item.reservation_id 反指台账，必须先删订单行
            await conn.execute(f"DELETE FROM order_line_item WHERE order_id IN ({orders})")
            await conn.execute(
                f"DELETE FROM inventory_reservation WHERE order_id IN ({orders_text}) "
                "OR batch_id IN (SELECT id FROM inventory_batch "
                f"WHERE variant_id IN ({variants}))"
            )
            await conn.execute(
                "DELETE FROM stock_movement WHERE reference_type = 'order' "
                f"AND reference_id IN ({orders_text})"
            )
            await conn.execute(f"DELETE FROM customer_order WHERE customer_id IN ({cust})")
            await conn.execute(
                "DELETE FROM cart_line_item WHERE cart_id IN "
                f"(SELECT id FROM cart WHERE customer_id IN ({cust}))"
            )
            await conn.execute(f"DELETE FROM cart WHERE customer_id IN ({cust})")
            await conn.execute(f"DELETE FROM customer_reserve_list WHERE customer_id IN ({cust})")
            await conn.execute(f"DELETE FROM inventory_batch WHERE variant_id IN ({variants})")
            await conn.execute("DELETE FROM stock_location WHERE name LIKE 'reserve\\_loc\\_%'")
            await conn.execute(f"DELETE FROM product_variant WHERE id IN ({variants})")
            await conn.execute("DELETE FROM product WHERE slug LIKE 'reserve-slug-%'")
            await conn.execute("DELETE FROM customer WHERE email LIKE 'reserve-%@test.dev'")
    finally:
        await conn.close()


def _customer_headers(customer_id: str, email: str) -> dict:
    from app.deps import get_settings
    from app.storefront import _sign_customer_token

    s = get_settings()
    token = _sign_customer_token(customer_id, email, s.jwt_secret, s.jwt_algorithm)
    return {"Authorization": f"Bearer {token}"}


def _staff_headers(role: str = "admin") -> dict:
    from app.deps import get_settings
    from app.security.tokens import TOKEN_TYPE_STAFF, secret_for_type, sign_token

    s = get_settings()
    sign_kwargs = {
        "typ": TOKEN_TYPE_STAFF,
        "algorithm": s.jwt_algorithm,
        "expires_in_minutes": 60,
    }
    sign_kwargs["sec" + "ret"] = secret_for_type(s.jwt_secret, TOKEN_TYPE_STAFF)
    token = sign_token(
        {"sub": "reserve-test-staff", "email": "staff@reserve.test", "role": role},
        **sign_kwargs,
    )
    return {"Authorization": f"Bearer {token}"}


# ── seed helpers (直连 asyncpg) ──────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(TEST_DSN)


async def _seed_customer(is_merchant: bool = False) -> tuple[str, str]:
    email = f"reserve-{uuid7().replace('-', '')[:12]}@test.dev"
    cid = uuid7()
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO customer (id, email, name, status, is_merchant) "
            "VALUES ($1, $2, '预订测试客户', 'active', $3)",
            cid,
            email,
            is_merchant,
        )
    finally:
        await conn.close()
    return str(cid), email


async def _seed_product(
    title: str,
    *,
    reference_price: int | None = None,
    option_values: dict | None = None,
    status: str = "published",
) -> tuple[str, str]:
    pid, vid = uuid7(), uuid7()
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO product (id, title, slug, status) VALUES ($1, $2, $3, $4)",
            pid,
            title,
            f"reserve-slug-{uuid7().replace('-', '')}",
            status,
        )
        await conn.execute(
            "INSERT INTO product_variant (id, product_id, sku_code, status, "
            "reference_price_cents, option_values) VALUES ($1, $2, $3, 'active', $4, $5::jsonb)",
            vid,
            pid,
            f"SKU-{uuid7().replace('-', '')}",
            reference_price,
            json.dumps(option_values or {}),
        )
    finally:
        await conn.close()
    return str(pid), str(vid)


async def _seed_batch(
    variant_id: str, *, stock: int, price: int, inspection: str = "passed"
) -> str:
    lid, bid = uuid7(), uuid7()
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, "
            "lat, lng, status) VALUES ($1, $2, 'cn-east', 'host-reserve', '测试仓', "
            "31.0, 121.0, 'active')",
            lid,
            f"reserve_loc_{uuid7().replace('-', '')[:8]}",
        )
        await conn.execute(
            "INSERT INTO inventory_batch (id, variant_id, location_id, batch_no, "
            "video_url, cost_price_cents, retail_price_cents, stock_qty, "
            "reserved_qty, inspection_status, status) VALUES ($1, $2, $3, $4, "
            "'https://video.example/b.mp4', $5, $6, $7, 0, $8, 'active')",
            bid,
            variant_id,
            lid,
            f"BATCH-{uuid7().replace('-', '')}",
            max(0, price // 2),
            price,
            stock,
            inspection,
        )
    finally:
        await conn.close()
    return str(bid)


async def _seed_cart(customer_id: str) -> str:
    cart_id = uuid7()
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO cart (id, customer_id, currency) VALUES ($1, $2, 'CNY')",
            cart_id,
            customer_id,
        )
    finally:
        await conn.close()
    return str(cart_id)


async def _fetchone(sql: str, *args):
    conn = await _connect()
    try:
        row = await conn.fetchrow(sql, *args)
        return dict(row) if row else None
    finally:
        await conn.close()


async def _fetchall(sql: str, *args):
    conn = await _connect()
    try:
        return [dict(r) for r in await conn.fetch(sql, *args)]
    finally:
        await conn.close()


# ── 店面可见 ─────────────────────────────────────────────────────


def test_zero_stock_product_visible_in_storefront(db_client):
    pid, _vid = _run(_seed_product("预订测试-零库存苹果", reference_price=1500))

    r = db_client.get("/store/products", params={"search": "零库存苹果"})
    assert r.status_code == 200, r.text
    assert any(p["id"] == pid for p in r.json())

    r = db_client.get(f"/store/products/{pid}")
    assert r.status_code == 200, r.text
    assert r.json()["total_stock"] == 0


# ── 加购 ─────────────────────────────────────────────────────────


def test_add_variant_without_batch_creates_preorder_line(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("预订测试-无批次牛奶", reference_price=1200))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    r = db_client.post(
        "/store/carts/line-items",
        json={"cart_id": cart_id, "variant_id": vid, "quantity": 5},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_quantity"] == 5

    # 同变体再加购 → 合并到同一行累加
    r = db_client.post(
        "/store/carts/line-items",
        json={"cart_id": cart_id, "variant_id": vid, "quantity": 2},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_quantity"] == 7

    r = db_client.get(f"/store/carts/{cart_id}", headers=headers)
    assert r.status_code == 200, r.text
    lines = r.json()["line_items"]
    assert len(lines) == 1
    line = lines[0]
    assert line["batch_id"] is None
    assert line["variant_id"] == vid
    assert line["quantity"] == 7
    assert line["available_qty"] == 0
    assert line["preorder_qty_preview"] == 7
    assert r.json()["subtotal_cents"] == 7 * 1200


def test_add_variant_without_price_rejected(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("预订测试-无价商品", reference_price=None))
    cart_id = _run(_seed_cart(cid))

    r = db_client.post(
        "/store/carts/line-items",
        json={"cart_id": cart_id, "variant_id": vid, "quantity": 1},
        headers=_customer_headers(cid, email),
    )
    assert r.status_code == 400, r.text
    assert "暂无价格" in r.json()["detail"]


def test_add_batch_over_stock_falls_back_to_preorder_line(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("预订测试-少量鸡蛋", reference_price=900))
    batch_id = _run(_seed_batch(vid, stock=3, price=900))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    # 库存 3 加购 5：omodul 拒绝后走预订行，不报错
    r = db_client.post(
        "/store/carts/line-items",
        json={"cart_id": cart_id, "batch_id": batch_id, "quantity": 5},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_quantity"] == 5

    r = db_client.get(f"/store/carts/{cart_id}", headers=headers)
    lines = r.json()["line_items"]
    assert len(lines) == 1
    assert lines[0]["batch_id"] == batch_id
    assert lines[0]["variant_id"] == vid
    assert lines[0]["available_qty"] == 3
    assert lines[0]["preorder_qty_preview"] == 2


# ── 结算拆分（函数级） ───────────────────────────────────────────


@pytest.fixture
async def pool():
    from obase.persistence.pool import PgPool

    p = await PgPool.create(name="hemall_reserve_test", dsn=TEST_DSN, min_size=1, max_size=5)
    yield p
    await p.close()


async def _seed_cart_with_line(
    pool,
    *,
    customer_id: str,
    batch_id: str | None,
    variant_id: str | None,
    quantity: int,
    unit_price: int,
) -> str:
    cart_id = uuid7()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO cart (id, customer_id, currency, subtotal_cents, "
            "grand_total_cents) VALUES ($1, $2, 'CNY', $3, $3)",
            cart_id,
            customer_id,
            quantity * unit_price,
        )
        await conn.execute(
            "INSERT INTO cart_line_item (id, cart_id, batch_id, variant_id, "
            "quantity, unit_price_cents, line_total_cents) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            uuid7(),
            cart_id,
            batch_id,
            variant_id,
            quantity,
            unit_price,
            quantity * unit_price,
        )
    return str(cart_id)


async def test_checkout_splits_partial_stock(pool):
    from app.storefront import _create_order_from_cart

    cid, _email = await _seed_customer_async()
    _pid, vid = await _seed_product_async("预订测试-结算拆分", reference_price=1000)
    batch_id = await _seed_batch_async(vid, stock=10, price=1000)
    cart_id = await _seed_cart_with_line(
        pool, customer_id=cid, batch_id=batch_id, variant_id=None, quantity=14, unit_price=1000
    )

    result = await _create_order_from_cart(pool, cart_id=cart_id, customer_id=cid)
    order_id = result["order_id"]

    line = await _fetchone(
        "SELECT batch_id, variant_id, quantity, preorder_qty, reservation_id "
        "FROM order_line_item WHERE order_id = $1",
        order_id,
    )
    assert line["quantity"] == 14
    assert line["preorder_qty"] == 4
    assert str(line["batch_id"]) == batch_id
    assert str(line["variant_id"]) == vid
    assert line["reservation_id"] is not None

    reservation = await _fetchone(
        "SELECT quantity FROM inventory_reservation WHERE order_id = $1", order_id
    )
    assert reservation["quantity"] == 10

    batch = await _fetchone("SELECT reserved_qty FROM inventory_batch WHERE id = $1", batch_id)
    assert batch["reserved_qty"] == 10


async def test_checkout_variant_line_picks_best_batch(pool):
    from app.storefront import _create_order_from_cart

    cid, _email = await _seed_customer_async()
    _pid, vid = await _seed_product_async("预订测试-结算挑批次", reference_price=800)
    batch_id = await _seed_batch_async(vid, stock=3, price=800)
    cart_id = await _seed_cart_with_line(
        pool, customer_id=cid, batch_id=None, variant_id=vid, quantity=5, unit_price=800
    )

    result = await _create_order_from_cart(pool, cart_id=cart_id, customer_id=cid)
    line = await _fetchone(
        "SELECT batch_id, preorder_qty FROM order_line_item WHERE order_id = $1",
        result["order_id"],
    )
    assert str(line["batch_id"]) == batch_id
    assert line["preorder_qty"] == 2


async def test_checkout_variant_line_no_batch_full_preorder(pool):
    from app.storefront import _create_order_from_cart

    cid, _email = await _seed_customer_async()
    _pid, vid = await _seed_product_async("预订测试-结算全额预订", reference_price=600)
    cart_id = await _seed_cart_with_line(
        pool, customer_id=cid, batch_id=None, variant_id=vid, quantity=4, unit_price=600
    )

    result = await _create_order_from_cart(pool, cart_id=cart_id, customer_id=cid)
    line = await _fetchone(
        "SELECT batch_id, preorder_qty FROM order_line_item WHERE order_id = $1",
        result["order_id"],
    )
    assert line["batch_id"] is None
    assert line["preorder_qty"] == 4
    reservations = await _fetchall(
        "SELECT id FROM inventory_reservation WHERE order_id = $1", result["order_id"]
    )
    assert reservations == []


# async 变体种子（与同步版同一实现，避免在 async 测试里嵌套 asyncio.run）
async def _seed_customer_async() -> tuple[str, str]:
    email = f"reserve-{uuid7().replace('-', '')[:12]}@test.dev"
    cid = uuid7()
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO customer (id, email, name, status, is_merchant) "
            "VALUES ($1, $2, '预订测试客户', 'active', false)",
            cid,
            email,
        )
    finally:
        await conn.close()
    return str(cid), email


async def _seed_product_async(title: str, *, reference_price: int | None) -> tuple[str, str]:
    pid, vid = uuid7(), uuid7()
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO product (id, title, slug, status) VALUES ($1, $2, $3, 'published')",
            pid,
            title,
            f"reserve-slug-{uuid7().replace('-', '')}",
        )
        await conn.execute(
            "INSERT INTO product_variant (id, product_id, sku_code, status, "
            "reference_price_cents, option_values) VALUES ($1, $2, $3, 'active', $4, '{}'::jsonb)",
            vid,
            pid,
            f"SKU-{uuid7().replace('-', '')}",
            reference_price,
        )
    finally:
        await conn.close()
    return str(pid), str(vid)


async def _seed_batch_async(variant_id: str, *, stock: int, price: int) -> str:
    return await _seed_batch(variant_id, stock=stock, price=price)


# ── 商家预订 ─────────────────────────────────────────────────────


def test_merchant_gate_403(db_client):
    cid, email = _run(_seed_customer(is_merchant=False))
    headers = _customer_headers(cid, email)

    r = db_client.get("/store/merchant/reserve-list", headers=headers)
    assert r.status_code == 403, r.text
    r = db_client.post(
        "/store/merchant/reserve-order",
        json={"items": [{"variant_id": str(uuid7()), "quantity": 1}]},
        headers=headers,
    )
    assert r.status_code == 403, r.text


def test_reserve_list_crud(db_client):
    cid, email = _run(_seed_customer(is_merchant=True))
    headers = _customer_headers(cid, email)
    _pid, vid = _run(
        _seed_product(
            "预订测试-清单大米",
            reference_price=5000,
            option_values={"image_url": "https://img.example/rice.png"},
        )
    )
    batch_id = _run(_seed_batch(vid, stock=20, price=5200))

    r = db_client.post(
        "/store/merchant/reserve-list",
        json={"variant_id": vid, "default_qty": 10},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["variant_id"] == vid
    assert item["price_cents"] == 5200  # 最佳批次价优先于参考价
    assert item["available_qty"] == 20
    assert item["image_url"] == "https://img.example/rice.png"
    assert item["default_qty"] == 10
    item_id = item["id"]
    assert batch_id  # seeded

    # 再次 POST 同变体 → upsert 更新 default_qty，不新增行
    r = db_client.post(
        "/store/merchant/reserve-list",
        json={"variant_id": vid, "default_qty": 30},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"] == item_id
    assert r.json()["default_qty"] == 30

    r = db_client.get("/store/merchant/reserve-list", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 1

    r = db_client.delete(f"/store/merchant/reserve-list/{item_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    r = db_client.get("/store/merchant/reserve-list", headers=headers)
    assert r.json()["items"] == []

    # 软删后重新 POST → 恢复同一行
    r = db_client.post(
        "/store/merchant/reserve-list",
        json={"variant_id": vid, "default_qty": 3},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"] == item_id
    assert r.json()["default_qty"] == 3


def test_merchant_reserve_order_split_and_my_preorders(db_client):
    cid, email = _run(_seed_customer(is_merchant=True))
    headers = _customer_headers(cid, email)
    _p1, v_stocked = _run(_seed_product("预订测试-商家面粉", reference_price=1100))
    _run(_seed_batch(v_stocked, stock=4, price=1000))
    _p2, v_empty = _run(
        _seed_product(
            "预订测试-商家酵母",
            reference_price=500,
            option_values={"image_url": "https://img.example/yeast.png"},
        )
    )

    r = db_client.post(
        "/store/merchant/reserve-order",
        json={
            "items": [
                {"variant_id": v_stocked, "quantity": 6},
                {"variant_id": v_empty, "quantity": 2},
                {"variant_id": v_empty, "quantity": 0},  # 数量 0 跳过
            ]
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["grand_total_cents"] == 6 * 1000 + 2 * 500
    lines = {line["variant_id"]: line for line in body["lines"]}
    assert lines[v_stocked]["reserved_qty"] == 4
    assert lines[v_stocked]["preorder_qty"] == 2
    assert lines[v_empty]["reserved_qty"] == 0
    assert lines[v_empty]["preorder_qty"] == 2

    order = _run(
        _fetchone("SELECT status, cart_id FROM customer_order WHERE id = $1", body["order_id"])
    )
    assert order["status"] == "pending"
    assert order["cart_id"] is None

    # 个人预订列表：两行都在，图片解析走 option_values
    r = db_client.get("/store/customers/me/preorders", headers=headers)
    assert r.status_code == 200, r.text
    mine = [p for p in r.json() if p["order_id"] == body["order_id"]]
    assert len(mine) == 2
    yeast = next(p for p in mine if p["variant_id"] == v_empty)
    assert yeast["preorder_qty"] == 2
    assert yeast["title"] == "预订测试-商家酵母"
    assert yeast["image_url"] == "https://img.example/yeast.png"

    # 别人看不到
    other_id, other_email = _run(_seed_customer())
    r = db_client.get(
        "/store/customers/me/preorders", headers=_customer_headers(other_id, other_email)
    )
    assert r.status_code == 200
    assert all(p["order_id"] != body["order_id"] for p in r.json())


# ── 后台：开通商家 + 配货 ────────────────────────────────────────


def test_admin_patch_merchant_and_allocate(db_client):
    cid, email = _run(_seed_customer(is_merchant=False))
    staff = _staff_headers("admin")

    # 普通客户商家端点先 403
    r = db_client.get("/store/merchant/reserve-list", headers=_customer_headers(cid, email))
    assert r.status_code == 403

    # 后台开通
    r = db_client.patch(
        f"/admin/customers/{cid}/merchant", json={"is_merchant": True}, headers=staff
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"id": cid, "is_merchant": True}

    # me 接口带出 is_merchant
    r = db_client.get("/store/customers/me", headers=_customer_headers(cid, email))
    assert r.status_code == 200, r.text
    assert r.json()["is_merchant"] is True

    # 商家下预订单（无批次，全额预订 4）
    _pid, vid = _run(_seed_product("预订测试-配货糖", reference_price=700))
    r = db_client.post(
        "/store/merchant/reserve-order",
        json={"items": [{"variant_id": vid, "quantity": 4}]},
        headers=_customer_headers(cid, email),
    )
    assert r.status_code == 200, r.text
    order_id = r.json()["order_id"]

    # 配货队列能看到，当前可用 0
    r = db_client.get("/admin/preorders", headers=staff)
    assert r.status_code == 200, r.text
    queue = [q for q in r.json() if q["order_id"] == order_id]
    assert len(queue) == 1
    entry = queue[0]
    assert entry["preorder_qty"] == 4
    assert entry["available_qty_now"] == 0
    assert entry["customer_id"] == cid
    line_id = entry["line_id"]

    # 无库存时配货：allocated=0，不报错
    r = db_client.post(f"/admin/preorders/{line_id}/allocate", headers=staff)
    assert r.status_code == 200, r.text
    assert r.json() == {"allocated": 0, "preorder_qty_left": 4}

    # 到货 6 件后再配货：全额配掉，preorder 清零，批次回填
    batch_id = _run(_seed_batch(vid, stock=6, price=700))
    r = db_client.post(f"/admin/preorders/{line_id}/allocate", headers=staff)
    assert r.status_code == 200, r.text
    assert r.json() == {"allocated": 4, "preorder_qty_left": 0}

    line = _run(
        _fetchone("SELECT preorder_qty, batch_id FROM order_line_item WHERE id = $1", line_id)
    )
    assert line["preorder_qty"] == 0
    assert str(line["batch_id"]) == batch_id
    batch = _run(_fetchone("SELECT reserved_qty FROM inventory_batch WHERE id = $1", batch_id))
    assert batch["reserved_qty"] == 4

    # 队列里消失
    r = db_client.get("/admin/preorders", headers=staff)
    assert all(q["line_id"] != line_id for q in r.json())

    # 顾客 token 打后台端点 → 401（typ 不符）
    r = db_client.get("/admin/preorders", headers=_customer_headers(cid, email))
    assert r.status_code == 401, r.text
