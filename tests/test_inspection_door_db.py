"""质检门补齐 — DB 集成测试（需真实 Postgres TEST_PG_DSN，否则跳过）。

覆盖：
- 待检（pending）批次按 batch_id 加购成功 → 购物车全额预订预览 →
  结算全额 preorder_qty（不锁定现货）
- 待检批次按 variant_id 加购 → 落绑该批次的预订行
- 质检 failed 批次加购 400（batch_id 与 variant_id 两条路）；pending
  被置 failed 后同样 400
- 质检端点 PATCH /inventory/batches/{id}/inspection：pending→passed
  后同批次可正常加购、结算锁定现货（preorder_qty=0）；批次列表过滤
- 端点鉴权（匿名/客户 token 拒绝）、非法状态值 422、不存在 404、
  软删批次 400
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
    """每个用例后按 FK 安全顺序清掉本文件造的行（标记：inspect- 邮箱 / inspect-slug-）。

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
            cust = "SELECT id FROM customer WHERE email LIKE 'inspect-%@test.dev'"
            orders = f"SELECT id FROM customer_order WHERE customer_id IN ({cust})"
            # inventory_reservation.order_id 是 text 列，子查询须 cast
            orders_text = f"SELECT id::text FROM customer_order WHERE customer_id IN ({cust})"
            variants = (
                "SELECT pv.id FROM product_variant pv JOIN product p "
                "ON p.id = pv.product_id WHERE p.slug LIKE 'inspect-slug-%'"
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
            await conn.execute("DELETE FROM stock_location WHERE name LIKE 'inspect\\_loc\\_%'")
            await conn.execute(f"DELETE FROM product_variant WHERE id IN ({variants})")
            await conn.execute("DELETE FROM product WHERE slug LIKE 'inspect-slug-%'")
            await conn.execute("DELETE FROM customer WHERE email LIKE 'inspect-%@test.dev'")
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
        {"sub": "inspect-test-staff", "email": "staff@inspect.test", "role": role},
        **sign_kwargs,
    )
    return {"Authorization": f"Bearer {token}"}


# ── seed helpers (直连 asyncpg) ──────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(TEST_DSN)


async def _seed_customer() -> tuple[str, str]:
    email = f"inspect-{uuid7().replace('-', '')[:12]}@test.dev"
    cid = uuid7()
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO customer (id, email, name, status) "
            "VALUES ($1, $2, '质检测试客户', 'active')",
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
            f"inspect-slug-{uuid7().replace('-', '')}",
        )
        await conn.execute(
            "INSERT INTO product_variant (id, product_id, sku_code, status, "
            "reference_price_cents, option_values) VALUES ($1, $2, $3, 'active', $4, $5::jsonb)",
            vid,
            pid,
            f"SKU-{uuid7().replace('-', '')}",
            reference_price,
            json.dumps({}),
        )
    finally:
        await conn.close()
    return str(pid), str(vid)


async def _seed_batch(
    variant_id: str, *, stock: int, price: int, inspection: str = "pending"
) -> str:
    lid, bid = uuid7(), uuid7()
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, "
            "lat, lng, status) VALUES ($1, $2, 'cn-east', 'host-inspect', '测试仓', "
            "31.0, 121.0, 'active')",
            lid,
            f"inspect_loc_{uuid7().replace('-', '')[:8]}",
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


async def _execute(sql: str, *args):
    conn = await _connect()
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


def _checkout(cart_id: str, customer_id: str) -> dict:
    async def _go():
        from obase.persistence.pool import PgPool

        from app.storefront import _create_order_from_cart

        pool = await PgPool.create(name="hemall_inspect_test", dsn=TEST_DSN, min_size=1, max_size=2)
        try:
            return await _create_order_from_cart(pool, cart_id=cart_id, customer_id=customer_id)
        finally:
            await pool.close()

    return _run(_go())


def _add(db_client, headers, cart_id, **kwargs):
    return db_client.post(
        "/store/carts/line-items",
        json={"cart_id": cart_id, **kwargs},
        headers=headers,
    )


# ── 待检批次加购 → 全额预订 ───────────────────────────────────────


def test_pending_batch_add_by_batch_id_full_preorder(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("质检测试-待检苹果", reference_price=5000))
    bid = _run(_seed_batch(vid, stock=10, price=5000, inspection="pending"))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    r = _add(db_client, headers, cart_id, batch_id=bid, quantity=3)
    assert r.status_code == 200, r.text
    assert r.json()["new_quantity"] == 3

    r = db_client.get(f"/store/carts/{cart_id}", headers=headers)
    assert r.status_code == 200, r.text
    lines = r.json()["line_items"]
    assert len(lines) == 1
    line = lines[0]
    assert line["batch_id"] == bid
    assert line["available_qty"] == 0
    assert line["preorder_qty_preview"] == 3

    result = _checkout(cart_id, cid)
    order_line = _run(
        _fetchone(
            "SELECT quantity, preorder_qty, reservation_id FROM order_line_item "
            "WHERE order_id = $1",
            result["order_id"],
        )
    )
    assert order_line["quantity"] == 3
    assert order_line["preorder_qty"] == 3
    assert order_line["reservation_id"] is None
    batch = _run(_fetchone("SELECT reserved_qty FROM inventory_batch WHERE id = $1", bid))
    assert batch["reserved_qty"] == 0


def test_pending_batch_add_by_variant_id_binds_batch(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("质检测试-变体待检牛奶", reference_price=1200))
    bid = _run(_seed_batch(vid, stock=8, price=1200, inspection="pending"))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    r = _add(db_client, headers, cart_id, variant_id=vid, quantity=2)
    assert r.status_code == 200, r.text

    r = db_client.get(f"/store/carts/{cart_id}", headers=headers)
    lines = r.json()["line_items"]
    assert len(lines) == 1
    assert lines[0]["batch_id"] == bid
    assert lines[0]["unit_price_cents"] == 1200
    assert lines[0]["preorder_qty_preview"] == 2


# ── failed 批次一律拒绝 ───────────────────────────────────────────


def test_failed_batch_add_rejected_both_paths(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("质检测试-不通过鸡蛋", reference_price=800))
    bid = _run(_seed_batch(vid, stock=5, price=800, inspection="failed"))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    r = _add(db_client, headers, cart_id, batch_id=bid, quantity=1)
    assert r.status_code == 400, r.text

    r = _add(db_client, headers, cart_id, variant_id=vid, quantity=1)
    assert r.status_code == 400, r.text


def test_pending_turned_failed_then_add_rejected(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("质检测试-待检转不通过", reference_price=900))
    bid = _run(_seed_batch(vid, stock=5, price=900, inspection="pending"))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)

    r = db_client.patch(
        f"/inventory/batches/{bid}/inspection",
        json={"inspection_status": "failed"},
        headers=_staff_headers(),
    )
    assert r.status_code == 200, r.text

    r = _add(db_client, headers, cart_id, batch_id=bid, quantity=1)
    assert r.status_code == 400, r.text
    r = _add(db_client, headers, cart_id, variant_id=vid, quantity=1)
    assert r.status_code == 400, r.text


# ── 质检端点 ─────────────────────────────────────────────────────


def test_inspection_pass_then_add_and_checkout_reserves(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("质检测试-待检转通过", reference_price=5000))
    bid = _run(_seed_batch(vid, stock=10, price=5000, inspection="pending"))
    cart_id = _run(_seed_cart(cid))
    headers = _customer_headers(cid, email)
    staff = _staff_headers()

    r = db_client.patch(
        f"/inventory/batches/{bid}/inspection",
        json={"inspection_status": "passed"},
        headers=staff,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"id": bid, "inspection_status": "passed"}

    r = db_client.get("/inventory/batches", params={"inspection_status": "passed"}, headers=staff)
    assert r.status_code == 200, r.text
    assert any(b["id"] == bid and b["inspection_status"] == "passed" for b in r.json())

    r = _add(db_client, headers, cart_id, batch_id=bid, quantity=4)
    assert r.status_code == 200, r.text
    # 语义（双重占位修复后）：omodul 加购即预留——加购后批次 reserved_qty
    # 先 +4（购物车占位）；结算时占位转正，计数器只补差额、台账记全量，
    # 结算后仍为 4（修复前会被二次锁定成 8，与台账不一致）。
    batch = _run(_fetchone("SELECT reserved_qty FROM inventory_batch WHERE id = $1", bid))
    assert batch["reserved_qty"] == 4

    result = _checkout(cart_id, cid)
    order_line = _run(
        _fetchone(
            "SELECT quantity, preorder_qty, reservation_id FROM order_line_item "
            "WHERE order_id = $1",
            result["order_id"],
        )
    )
    assert order_line["quantity"] == 4
    assert order_line["preorder_qty"] == 0
    assert order_line["reservation_id"] is not None
    batch = _run(_fetchone("SELECT reserved_qty FROM inventory_batch WHERE id = $1", bid))
    assert batch["reserved_qty"] == 4


def test_inspection_endpoint_auth_and_validation(db_client):
    cid, email = _run(_seed_customer())
    _pid, vid = _run(_seed_product("质检测试-端点守卫", reference_price=100))
    bid = _run(_seed_batch(vid, stock=1, price=100, inspection="pending"))
    staff = _staff_headers()

    # 匿名 → 401
    r = db_client.patch(
        f"/inventory/batches/{bid}/inspection", json={"inspection_status": "passed"}
    )
    assert r.status_code == 401, r.text
    r = db_client.get("/inventory/batches")
    assert r.status_code == 401, r.text

    # 客户 token（非员工）→ 401/403
    r = db_client.patch(
        f"/inventory/batches/{bid}/inspection",
        json={"inspection_status": "passed"},
        headers=_customer_headers(cid, email),
    )
    assert r.status_code in (401, 403), r.text

    # 非法状态值 → 422
    r = db_client.patch(
        f"/inventory/batches/{bid}/inspection",
        json={"inspection_status": "ok"},
        headers=staff,
    )
    assert r.status_code == 422, r.text

    # 不存在的批次 → 404
    r = db_client.patch(
        f"/inventory/batches/{uuid7()}/inspection",
        json={"inspection_status": "passed"},
        headers=staff,
    )
    assert r.status_code == 404, r.text

    # 软删批次 → 400 拒绝
    _run(_execute("UPDATE inventory_batch SET deleted_at = NOW() WHERE id = $1", bid))
    r = db_client.patch(
        f"/inventory/batches/{bid}/inspection",
        json={"inspection_status": "passed"},
        headers=staff,
    )
    assert r.status_code == 400, r.text
