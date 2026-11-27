"""core2 门店运营：搜索别名、批次改价留痕、购物车折扣预览、提醒/待办、到店提醒。

DB 集成用例需要 TEST_PG_DSN；无 DSN 时整文件跳过（与既有 DB 测试一致）。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from obase.uuid7 import uuid7

from app import queries
from app.store_ops import (
    BatchPriceInput,
    batch_price_history,
    order_alerts,
    today_board,
    update_batch_price,
)
from app.storefront import _cart_discount_preview, customer_pickup_arrived

TEST_DSN = os.environ.get("TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


class _Req:
    def __init__(self, pool):
        self.app = SimpleNamespace(state=SimpleNamespace(pool=pool))


@pytest.fixture
async def core2_pool():
    from obase.persistence.pool import PgPool

    token = uuid7().replace("-", "")
    pool = await PgPool.create(
        name=f"he_mall_core2_{token[-12:]}", dsn=TEST_DSN, min_size=1, max_size=3
    )
    try:
        yield pool, token
    finally:
        await pool.close()


async def _seed_product(pool, token: str, *, title: str, aliases: list[str], price: int = 300):
    async with pool.acquire() as conn:
        category = await conn.fetchval(
            "INSERT INTO product_category (id, name, slug, status) "
            "VALUES ($1, $2, $3, 'active') RETURNING id",
            uuid7(),
            f"core2类目{token}",
            f"core2-cat-{token}",
        )
        product = await conn.fetchval(
            "INSERT INTO product (id, title, slug, category_id, status, search_aliases) "
            "VALUES ($1, $2, $3, $4, 'published', $5) RETURNING id",
            uuid7(),
            title,
            f"core2-{token}",
            category,
            aliases,
        )
        variant = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, option_values, status, "
            "pricing_mode, unit_price_cents, price_basis_grams, weight_options, stock_unit) "
            "VALUES ($1, $2, $3, '{}'::jsonb, 'active', 'weight', $4, 500, "
            "'[{\"label\":\"约1斤\",\"grams\":500}]'::jsonb, 'gram') RETURNING id",
            uuid7(),
            product,
            f"CORE2-{token}",
            price,
        )
        location = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, status) "
            "VALUES ($1, $2, 'cn-east', 'active') RETURNING id",
            uuid7(),
            f"core2-loc-{token}",
        )
        batch = await conn.fetchval(
            "INSERT INTO inventory_batch (id, batch_no, variant_id, location_id, video_url, "
            "cost_price_cents, retail_price_cents, currency, stock_qty, inspection_status, status) "
            "VALUES ($1, $2, $3, $4, '', $5, $5, 'CNY', 10, 'passed', 'active') RETURNING id",
            uuid7(),
            f"CORE2-BATCH-{token}",
            variant,
            location,
            price,
        )
    return {"product": str(product), "variant": str(variant), "batch": str(batch)}


async def test_search_matches_chinese_substring_and_alias(core2_pool):
    pool, token = core2_pool
    seeded = await _seed_product(pool, token, title=f"上海青{token}", aliases=["青菜", "小油菜"])

    by_title = await queries.list_storefront_products(pool, search="上海青")
    by_alias = await queries.list_storefront_products(pool, search="青菜")
    by_multi = await queries.list_storefront_products(pool, search="上海青 青菜")

    assert any(p["id"] == seeded["product"] for p in by_title)
    assert any(p["id"] == seeded["product"] for p in by_alias)
    assert any(p["id"] == seeded["product"] for p in by_multi)


async def test_batch_price_change_writes_audit(core2_pool):
    pool, token = core2_pool
    seeded = await _seed_product(pool, token, title=f"改价商品{token}", aliases=[])
    req = _Req(pool)
    principal = {"user_id": "staff-1", "email": "staff@example.invalid", "claims": {}}

    result = await update_batch_price(
        req,
        seeded["batch"],
        BatchPriceInput(retail_price_cents=456, reason="晚市调价"),
        principal,
    )
    assert result["changed"] is True
    assert result["old_price_cents"] == 300
    assert result["new_price_cents"] == 456
    assert result["operator"] == "staff@example.invalid"

    history = await batch_price_history(req, seeded["batch"], 50, principal)
    assert history["changes"][0]["new_price_cents"] == 456
    assert history["changes"][0]["reason"] == "晚市调价"

    async with pool.acquire() as conn:
        price = await conn.fetchval(
            "SELECT retail_price_cents FROM inventory_batch WHERE id = $1", seeded["batch"]
        )
    assert price == 456


async def test_cart_preview_matches_member_discount(core2_pool):
    pool, token = core2_pool
    seeded = await _seed_product(pool, token, title=f"折扣商品{token}", aliases=[], price=1000)
    async with pool.acquire() as conn:
        customer = await conn.fetchval(
            "INSERT INTO customer (id, email, name) VALUES ($1, $2, '预览顾客') RETURNING id",
            uuid7(),
            f"core2-{token}@example.invalid",
        )
    cart = {
        "customer_id": str(customer),
        "subtotal_cents": 2000,
        "discount_cents": 0,
        "tax_cents": 0,
        "shipping_cents": 500,
        "grand_total_cents": 2500,
        "line_items": [
            {"variant_id": seeded["variant"], "line_total_cents": 2000},
        ],
    }
    preview = await _cart_discount_preview(pool, cart)
    assert preview["member_discount_cents"] == 200
    assert preview["preview_discount_cents"] == 200
    assert preview["preview_grand_total_cents"] == 2300


async def test_alerts_and_today_board(core2_pool):
    pool, token = core2_pool
    async with pool.acquire() as conn:
        customer = await conn.fetchval(
            "INSERT INTO customer (id, email, name) VALUES ($1, $2, '提醒顾客') RETURNING id",
            uuid7(),
            f"alert-{token}@example.invalid",
        )
        order = await conn.fetchval(
            "INSERT INTO customer_order (id, customer_id, status, subtotal_cents, "
            "grand_total_cents, currency, shipping_method_code) "
            "VALUES ($1, $2, 'paid', 1000, 1000, 'CNY', 'standard') RETURNING id",
            uuid7(),
            customer,
        )
    req = _Req(pool)
    principal = {"user_id": "staff-1", "email": "staff@example.invalid", "claims": {}}

    board = await today_board(req, principal)
    assert any(g["key"] == "pick" and g["count"] >= 1 for g in board["groups"])
    assert any(t["order_id"] == str(order) for g in board["groups"] for t in g["tasks"])

    alerts = await order_alerts(req, after="2000-01-01T00:00:00+08:00", limit=50, _=principal)
    assert any(e["type"] == "new_order" and e["order_id"] == str(order) for e in alerts["events"])


async def test_customer_pickup_arrived(core2_pool):
    pool, token = core2_pool
    async with pool.acquire() as conn:
        customer = await conn.fetchval(
            "INSERT INTO customer (id, email, name) VALUES ($1, $2, '自提顾客') RETURNING id",
            uuid7(),
            f"pickup-{token}@example.invalid",
        )
        order = await conn.fetchval(
            "INSERT INTO customer_order (id, customer_id, status, subtotal_cents, "
            "grand_total_cents, currency, shipping_method_code) "
            "VALUES ($1, $2, 'processing', 1000, 1000, 'CNY', 'pickup') RETURNING id",
            uuid7(),
            customer,
        )
    result = await customer_pickup_arrived(
        str(order), _Req(pool), {"customer_id": str(customer)}
    )
    assert result["status"] == "arrived"
    assert result["pickup_arrived_at"]

    async with pool.acquire() as conn:
        arrived = await conn.fetchval(
            "SELECT pickup_arrived_at FROM customer_order WHERE id = $1", order
        )
    assert arrived is not None
