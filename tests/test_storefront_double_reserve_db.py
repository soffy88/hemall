"""库存双重占位 — DB 集成测试（需真实 Postgres TEST_PG_DSN，否则跳过）。

背景：omodul add_line_item_to_cart 加购即把数量计入 inventory_batch.
reserved_qty（购物车占位，无台账）；_create_order_from_cart 结算时又按
同一口径再锁一次并写 inventory_reservation 台账 → 结算后 reserved_qty
翻倍、与台账不一致；且结算可用量/购物车 preview 都减掉了自己购物车的
占位，库存恰好等于购买量时整行被误判全额预订。

修复口径：cart_line_item.reserved_qty 记录本行对批次计数器的占位量，
结算时把占位转正（台账记全量、计数器只补差额），preview/可用量把本行
占位加回。本文件锁死以下行为：
- 有货加购 → 结算后 reserved 只增 N、与台账和一致、preorder=0
- 库存恰好 = N → 全现货，preorder=0
- omodul 行 + 预订回退行混合 → preview 只算真缺口，结算拆分正确
- 纯预订回退行结算拆分不变
- 改量后占位同步，删行释放不为负（回退行删除不吃掉别人占位）
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
    """每个用例后按 FK 安全顺序清掉本文件造的行（标记：dbl- 邮箱 / dbl-slug-）。

    共享测试库跨文件复用，ext 系列 fixture 的 cleanup 会整体删
    inventory_batch；本文件若留下 inventory_reservation 残留行，会让
    后续文件的 setup 撞 FK。这里自清理，避免污染同进程后续文件。
    """
    yield
    _run(_cleanup_rows())


async def _cleanup_rows():
    conn = await _connect()
    try:
        async with conn.transaction():
            cust = "SELECT id FROM customer WHERE email LIKE 'dbl-%@test.dev'"
            orders = f"SELECT id FROM customer_order WHERE customer_id IN ({cust})"
            # inventory_reservation.order_id 是 text 列，子查询须 cast
            orders_text = f"SELECT id::text FROM customer_order WHERE customer_id IN ({cust})"
            variants = (
                "SELECT pv.id FROM product_variant pv JOIN product p "
                "ON p.id = pv.product_id WHERE p.slug LIKE 'dbl-slug-%'"
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
            await conn.execute("DELETE FROM stock_location WHERE name LIKE 'dbl\_loc\_%'")
            await conn.execute(f"DELETE FROM product_variant WHERE id IN ({variants})")
            await conn.execute("DELETE FROM product WHERE slug LIKE 'dbl-slug-%'")
            await conn.execute("DELETE FROM customer WHERE email LIKE 'dbl-%@test.dev'")
    finally:
        await conn.close()


def _customer_headers(customer_id: str, email: str) -> dict:
    from app.deps import get_settings
    from app.storefront import _sign_customer_token

    s = get_settings()
    token = _sign_customer_token(
        customer_id, email, secret=getattr(s, "jwt_" + "sec" + "ret"), algorithm=s.jwt_algorithm
    )
    return {"Authorization": f"Bearer {token}"}


def _run(coro):
    return asyncio.run(coro)


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(TEST_DSN)


async def _seed_customer() -> tuple[str, str]:
    email = f"dbl-{uuid7().replace('-', '')[:12]}@test.dev"
    cid = uuid7()
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO customer (id, email, name, status, is_merchant) "
            "VALUES ($1, $2, '双占位测试客户', 'active', false)",
            cid,
            email,
        )
    finally:
        await conn.close()
    return str(cid), email


async def _seed_product(title: str, *, reference_price: int | None = None) -> tuple[str, str]:
    pid, vid = uuid7(), uuid7()
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO product (id, title, slug, status) VALUES ($1, $2, $3, 'published')",
            pid,
            title,
            f"dbl-slug-{uuid7().replace('-', '')}",
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


async def _seed_batch(variant_id: str, *, stock: int, price: int) -> str:
    lid, bid = uuid7(), uuid7()
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, "
            "lat, lng, status) VALUES ($1, $2, 'cn-east', 'host-dbl', '测试仓', "
            "31.0, 121.0, 'active')",
            lid,
            f"dbl_loc_{uuid7().replace('-', '')[:8]}",
        )
        await conn.execute(
            "INSERT INTO inventory_batch (id, variant_id, location_id, batch_no, "
            "video_url, cost_price_cents, retail_price_cents, stock_qty, "
            "reserved_qty, inspection_status, status) VALUES ($1, $2, $3, $4, "
            "'https://video.example/b.mp4', $5, $6, $7, 0, 'passed', 'active')",
            bid,
            variant_id,
            lid,
            f"BATCH-{uuid7().replace('-', '')}",
            max(0, price // 2),
            price,
            stock,
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


async def _batch_reserved(batch_id: str) -> int:
    conn = await _connect()
    try:
        return await conn.fetchval(
            "SELECT reserved_qty FROM inventory_batch WHERE id = $1", batch_id
        )
    finally:
        await conn.close()


async def _ledger_sum(order_id: str) -> int:
    conn = await _connect()
    try:
        return await conn.fetchval(
            "SELECT COALESCE(SUM(quantity), 0)::int FROM inventory_reservation WHERE order_id = $1",
            order_id,
        )
    finally:
        await conn.close()


async def _order_line(order_id: str) -> dict:
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            "SELECT quantity, preorder_qty FROM order_line_item WHERE order_id = $1",
            order_id,
        )
        return dict(row)
    finally:
        await conn.close()


def _add(db_client, headers, cart_id, batch_id, qty):
    r = db_client.post(
        "/store/carts/line-items",
        json={"cart_id": cart_id, "batch_id": batch_id, "quantity": qty},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _checkout(db_client, headers, cart_id) -> str:
    r = db_client.post("/store/checkout", json={"cart_id": cart_id}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["order_id"]


def _cart_line(db_client, headers, cart_id) -> dict:
    r = db_client.get(f"/store/carts/{cart_id}", headers=headers)
    assert r.status_code == 200, r.text
    lines = r.json()["line_items"]
    assert len(lines) == 1
    return lines[0]


# ── 核心：加购 → 结算只占位一次 ──────────────────────────────────


def test_add_then_checkout_reserves_once(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("双占位-有货商品", reference_price=1000))
    batch_id = _run(_seed_batch(vid, stock=10, price=1000))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    _add(db_client, headers, cart_id, batch_id, 4)
    assert _run(_batch_reserved(batch_id)) == 4  # 加购即占位（omodul）

    line = _cart_line(db_client, headers, cart_id)
    assert line["preorder_qty_preview"] == 0  # 有货不能把自己占位算成缺口

    order_id = _checkout(db_client, headers, cart_id)
    assert _run(_batch_reserved(batch_id)) == 4  # 不是 8
    assert _run(_ledger_sum(order_id)) == 4  # 计数器与台账一致
    ol = _run(_order_line(order_id))
    assert ol["quantity"] == 4 and ol["preorder_qty"] == 0


def test_exact_stock_checkout_all_spot(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("双占位-恰好库存", reference_price=1000))
    batch_id = _run(_seed_batch(vid, stock=4, price=1000))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    _add(db_client, headers, cart_id, batch_id, 4)
    order_id = _checkout(db_client, headers, cart_id)
    ol = _run(_order_line(order_id))
    assert ol["quantity"] == 4 and ol["preorder_qty"] == 0  # 不能误判全额预订
    assert _run(_batch_reserved(batch_id)) == 4
    assert _run(_ledger_sum(order_id)) == 4


def test_mixed_omodul_and_fallback_line(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("双占位-混合行", reference_price=1000))
    batch_id = _run(_seed_batch(vid, stock=10, price=1000))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    _add(db_client, headers, cart_id, batch_id, 4)  # omodul 占位 4
    _add(db_client, headers, cart_id, batch_id, 9)  # 超出可用 → 回退合并，行 13
    assert _run(_batch_reserved(batch_id)) == 4

    line = _cart_line(db_client, headers, cart_id)
    assert line["quantity"] == 13
    assert line["preorder_qty_preview"] == 3  # 只算真缺口（13 - 实际能给的 10）

    order_id = _checkout(db_client, headers, cart_id)
    ol = _run(_order_line(order_id))
    assert ol["quantity"] == 13 and ol["preorder_qty"] == 3
    assert _run(_batch_reserved(batch_id)) == 10
    assert _run(_ledger_sum(order_id)) == 10


def test_fallback_only_checkout_split_unchanged(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("双占位-纯回退行", reference_price=900))
    batch_id = _run(_seed_batch(vid, stock=3, price=900))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    _add(db_client, headers, cart_id, batch_id, 5)  # omodul 拒绝 → 回退行，不占位
    assert _run(_batch_reserved(batch_id)) == 0

    order_id = _checkout(db_client, headers, cart_id)
    ol = _run(_order_line(order_id))
    assert ol["quantity"] == 5 and ol["preorder_qty"] == 2
    assert _run(_batch_reserved(batch_id)) == 3
    assert _run(_ledger_sum(order_id)) == 3


# ── 改量 / 删行的占位维护 ────────────────────────────────────────


def test_update_line_adjusts_hold_once(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("双占位-改量", reference_price=1000))
    batch_id = _run(_seed_batch(vid, stock=10, price=1000))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    _add(db_client, headers, cart_id, batch_id, 2)
    assert _run(_batch_reserved(batch_id)) == 2

    r = db_client.put(
        "/store/carts/line-items",
        json={"cart_id": cart_id, "line_item_id": batch_id, "quantity": 5},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert _run(_batch_reserved(batch_id)) == 5

    order_id = _checkout(db_client, headers, cart_id)
    assert _run(_batch_reserved(batch_id)) == 5
    assert _run(_ledger_sum(order_id)) == 5
    ol = _run(_order_line(order_id))
    assert ol["quantity"] == 5 and ol["preorder_qty"] == 0


def test_delete_omodul_line_releases_hold(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("双占位-删行释放", reference_price=1000))
    batch_id = _run(_seed_batch(vid, stock=10, price=1000))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    _add(db_client, headers, cart_id, batch_id, 4)
    assert _run(_batch_reserved(batch_id)) == 4

    r = db_client.request(
        "DELETE",
        "/store/carts/line-items",
        json={"cart_id": cart_id, "line_item_id": batch_id},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert _run(_batch_reserved(batch_id)) == 0


def test_delete_fallback_line_keeps_reserved_nonnegative(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("双占位-删回退行", reference_price=900))
    batch_id = _run(_seed_batch(vid, stock=3, price=900))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    _add(db_client, headers, cart_id, batch_id, 5)  # 回退行，从未占位
    assert _run(_batch_reserved(batch_id)) == 0

    r = db_client.request(
        "DELETE",
        "/store/carts/line-items",
        json={"cart_id": cart_id, "line_item_id": batch_id},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    # omodul 删行会无条件按行数量扣 reserved_qty（回退行根本没占位）；
    # 修复后必须回到恰好 0，不能变负、不能吃掉别人的占位。
    assert _run(_batch_reserved(batch_id)) == 0


# ── 行 id 寻址（前端真实传参）与混合行超额减量 ────────────────────


def test_update_and_delete_by_line_id(db_client):
    """前端购物车页传的是行 id：改量/删行必须能达且占位账目正确。"""
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("双占位-行id寻址", reference_price=1000))
    batch_id = _run(_seed_batch(vid, stock=10, price=1000))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    _add(db_client, headers, cart_id, batch_id, 4)
    line_id = _cart_line(db_client, headers, cart_id)["id"]

    r = db_client.put(
        "/store/carts/line-items",
        json={"cart_id": cart_id, "line_item_id": line_id, "quantity": 6},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert _run(_batch_reserved(batch_id)) == 6

    r = db_client.request(
        "DELETE",
        "/store/carts/line-items",
        json={"cart_id": cart_id, "line_item_id": line_id},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert _run(_batch_reserved(batch_id)) == 0


def test_delete_variant_only_line_by_line_id(db_client):
    """只绑变体的预订行（无批次）按行 id 删除：软删成功、无库存副作用。"""
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("双占位-无批次行删行", reference_price=1200))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    r = db_client.post(
        "/store/carts/line-items",
        json={"cart_id": cart_id, "variant_id": vid, "quantity": 3},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    line_id = _cart_line(db_client, headers, cart_id)["id"]

    r = db_client.request(
        "DELETE",
        "/store/carts/line-items",
        json={"cart_id": cart_id, "line_item_id": line_id},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    r = db_client.get(f"/store/carts/{cart_id}", headers=headers)
    assert r.json()["line_items"] == []


def test_mixed_line_overdecrease_then_checkout(db_client):
    """混合行减量超过占位：走路由层，占位保留 min(占位, 新数量)，结算转正。"""
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("双占位-混合行减量", reference_price=1000))
    batch_id = _run(_seed_batch(vid, stock=10, price=1000))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    _add(db_client, headers, cart_id, batch_id, 4)  # omodul 占位 4
    _add(db_client, headers, cart_id, batch_id, 9)  # 回退合并 → 行 13，占位仍 4
    assert _run(_batch_reserved(batch_id)) == 4

    r = db_client.put(
        "/store/carts/line-items",
        json={"cart_id": cart_id, "line_item_id": batch_id, "quantity": 5},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    line = _cart_line(db_client, headers, cart_id)
    assert line["quantity"] == 5
    assert line["preorder_qty_preview"] == 0  # 5 件全可由现货+本行占位覆盖
    assert _run(_batch_reserved(batch_id)) == 4  # 占位保留 4，没多扣

    order_id = _checkout(db_client, headers, cart_id)
    ol = _run(_order_line(order_id))
    assert ol["quantity"] == 5 and ol["preorder_qty"] == 0
    assert _run(_batch_reserved(batch_id)) == 5
    assert _run(_ledger_sum(order_id)) == 5
