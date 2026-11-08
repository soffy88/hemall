"""称重下单 → 实称 → 差额退款 端到端（需 TEST_PG_DSN 临时库）。

覆盖：称重行结算按克预留、订单行预估字段、实称差额在容差内自动发起
退款（manual 模式停在 refunding 等人工确认）、重复录入幂等。
"""

from __future__ import annotations

import os

import pytest
from obase.uuid7 import uuid7

from app.storefront import _create_order_from_cart
from app.weigh_settle import record_actual_weight

TEST_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


@pytest.fixture
async def weigh_pool():
    from obase.persistence.pool import PgPool

    token = uuid7().replace("-", "")
    pool = await PgPool.create(
        name=f"he_mall_weigh_{token[-12:]}", dsn=TEST_DSN, min_size=1, max_size=3
    )
    ids: dict[str, str] = {"token": token}
    try:
        async with pool.acquire() as conn:
            ids["customer"] = str(
                await conn.fetchval(
                    "INSERT INTO customer (id, email, name) VALUES ($1, $2, $3) RETURNING id",
                    uuid7(),
                    f"weigh-{token}@example.invalid",
                    "weigh tester",
                )
            )
            ids["location"] = str(
                await conn.fetchval(
                    "INSERT INTO stock_location (id, name, region_code, status) "
                    "VALUES ($1, $2, 'cn-east', 'active') RETURNING id",
                    uuid7(),
                    f"weigh-loc-{token}",
                )
            )
            ids["category"] = str(
                await conn.fetchval(
                    "INSERT INTO product_category (id, name, slug, status) "
                    "VALUES ($1, $2, $3, 'active') RETURNING id",
                    uuid7(),
                    f"称重测试类目 {token}",
                    f"weigh-cat-{token}",
                )
            )
            ids["product"] = str(
                await conn.fetchval(
                    "INSERT INTO product (id, title, slug, category_id, status) "
                    "VALUES ($1, $2, $3, $4, 'published') RETURNING id",
                    uuid7(),
                    f"称重测试菜心 {token}",
                    f"weigh-product-{token}",
                    ids["category"],
                )
            )
            ids["variant"] = str(
                await conn.fetchval(
                    """
                    INSERT INTO product_variant
                        (id, product_id, sku_code, option_values, status,
                         pricing_mode, unit_price_cents, price_basis_grams,
                         weight_options, stock_unit)
                    VALUES ($1, $2, $3, '{"规格":"散装"}'::jsonb, 'active',
                            'weight', 4000, 500,
                            '[{"label":"约1斤","grams":500}]'::jsonb, 'gram')
                    RETURNING id
                    """,
                    uuid7(),
                    ids["product"],
                    f"WEIGH-{token}",
                )
            )
            ids["batch"] = str(
                await conn.fetchval(
                    """
                    INSERT INTO inventory_batch
                        (id, batch_no, variant_id, location_id, video_url,
                         cost_price_cents, retail_price_cents, stock_qty,
                         inspection_status, status, expiration_time,
                         origin_region, storage_condition, shelf_life_days)
                    VALUES ($1, $2, $3, $4, '', 2500, 4000, 1000, 'passed',
                            'active', NOW() + INTERVAL '5 days', '惠州',
                            '0-4℃冷藏', 5)
                    RETURNING id
                    """,
                    uuid7(),
                    f"WEIGH-BATCH-{token}",
                    ids["variant"],
                    ids["location"],
                )
            )
            ids["cart"] = str(
                await conn.fetchval(
                    """
                    INSERT INTO cart (id, customer_id, region_code, currency, status,
                                      subtotal_cents, grand_total_cents)
                    VALUES ($1, $2, 'cn-east', 'CNY', 'active', 4000, 4000)
                    RETURNING id
                    """,
                    uuid7(),
                    ids["customer"],
                )
            )
            await conn.execute(
                """
                INSERT INTO cart_line_item
                    (id, cart_id, batch_id, variant_id, quantity, unit_price_cents,
                     line_total_cents, pricing_mode, unit_price_cents_per_jin,
                     price_basis_grams, estimated_weight_grams, weight_step_grams,
                     weight_option_label)
                VALUES ($1, $2, $3, $4, 1, 4000, 4000, 'weight', 4000, 500,
                        500, 500, '约1斤')
                """,
                uuid7(),
                ids["cart"],
                ids["batch"],
                ids["variant"],
            )
        yield pool, ids
    finally:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM refund_audit_log WHERE refund_id IN "
                    "(SELECT id FROM refund_request WHERE order_id IN "
                    "(SELECT id FROM customer_order WHERE cart_id = $1))",
                    ids.get("cart"),
                )
                await conn.execute(
                    "DELETE FROM refund_request WHERE order_id IN "
                    "(SELECT id FROM customer_order WHERE cart_id = $1)",
                    ids.get("cart"),
                )
                await conn.execute(
                    "DELETE FROM weigh_settlement WHERE order_id IN "
                    "(SELECT id FROM customer_order WHERE cart_id = $1)",
                    ids.get("cart"),
                )
                await conn.execute(
                    "DELETE FROM payment_intent WHERE order_id IN "
                    "(SELECT id::text FROM customer_order WHERE cart_id = $1)",
                    ids.get("cart"),
                )
                await conn.execute(
                    "DELETE FROM stock_movement WHERE reference_id IN "
                    "(SELECT id::text FROM customer_order WHERE cart_id = $1)",
                    ids.get("cart"),
                )
                await conn.execute(
                    "DELETE FROM order_status_history WHERE order_id IN "
                    "(SELECT id FROM customer_order WHERE cart_id = $1)",
                    ids.get("cart"),
                )
                await conn.execute(
                    "DELETE FROM order_line_item WHERE order_id IN "
                    "(SELECT id FROM customer_order WHERE cart_id = $1)",
                    ids.get("cart"),
                )
                await conn.execute(
                    "DELETE FROM inventory_reservation WHERE order_id IN "
                    "(SELECT id::text FROM customer_order WHERE cart_id = $1)",
                    ids.get("cart"),
                )
                await conn.execute("DELETE FROM customer_order WHERE cart_id = $1", ids.get("cart"))
                await conn.execute("DELETE FROM cart_line_item WHERE cart_id = $1", ids.get("cart"))
                await conn.execute("DELETE FROM cart WHERE id = $1", ids.get("cart"))
                await conn.execute("DELETE FROM inventory_batch WHERE id = $1", ids.get("batch"))
                await conn.execute("DELETE FROM product_variant WHERE id = $1", ids.get("variant"))
                await conn.execute("DELETE FROM product WHERE id = $1", ids.get("product"))
                await conn.execute(
                    "DELETE FROM product_category WHERE id = $1", ids.get("category")
                )
                await conn.execute("DELETE FROM stock_location WHERE id = $1", ids.get("location"))
                await conn.execute("DELETE FROM customer WHERE id = $1", ids.get("customer"))
        await pool.close()


@pytest.mark.asyncio
async def test_weight_order_and_diff_refund(weigh_pool):
    pool, ids = weigh_pool
    order = await _create_order_from_cart(pool, cart_id=ids["cart"], customer_id=ids["customer"])
    # 会员 9 折会自动作用于称重行，订单实付为 3600；预估行金额仍是 4000。
    assert order["grand_total_cents"] == 3600
    assert order["order_id"]

    async with pool.acquire() as conn:
        line = await conn.fetchrow(
            "SELECT * FROM order_line_item WHERE order_id = $1", order["order_id"]
        )
        batch = await conn.fetchrow("SELECT * FROM inventory_batch WHERE id = $1", ids["batch"])
        await conn.execute(
            "INSERT INTO payment_intent (id, order_id, amount_cents, currency, provider, status) "
            "VALUES ($1, $2, 3600, 'CNY', 'manual', 'paid')",
            uuid7(),
            order["order_id"],
        )
    assert line["pricing_mode"] == "weight"
    assert line["estimated_weight_grams"] == 500
    assert line["estimated_amount_cents"] == 4000
    assert line["settlement_status"] == "pending"
    # 库存按克预留：500 克。
    assert batch["reserved_qty"] == 500
    assert batch["stock_qty"] == 1000

    settlement = await record_actual_weight(
        pool,
        order_line_item_id=str(line["id"]),
        weight_grams=450,
        tolerance_percent=10,
        created_by="tester",
    )
    assert settlement["actual_amount_cents"] == 3600
    assert settlement["diff_cents"] == -400
    assert settlement["within_tolerance"] is True
    assert settlement["status"] == "manual_refund_pending"

    async with pool.acquire() as conn:
        refund = await conn.fetchrow(
            "SELECT * FROM refund_request WHERE order_id = $1", order["order_id"]
        )
        updated = await conn.fetchrow("SELECT * FROM order_line_item WHERE id = $1", line["id"])
    assert refund is not None
    assert int(refund["requested_amount"]) == 400
    assert refund["status"] == "refunding"  # manual：线下确认后才 succeeded
    assert updated["actual_weight_grams"] == 450
    assert updated["actual_amount_cents"] == 3600
    assert updated["weight_diff_cents"] == -400
    assert updated["settlement_status"] == "auto_settled"

    # 幂等：同重量重复提交返回同一条结算，不再产生第二笔退款。
    again = await record_actual_weight(
        pool,
        order_line_item_id=str(line["id"]),
        weight_grams=450,
        tolerance_percent=10,
        created_by="tester",
    )
    assert again["id"] == settlement["id"]
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM refund_request WHERE order_id = $1", order["order_id"]
        )
    assert count == 1


@pytest.mark.asyncio
async def test_weight_order_diff_beyond_tolerance_needs_customer(weigh_pool):
    pool, ids = weigh_pool
    order = await _create_order_from_cart(pool, cart_id=ids["cart"], customer_id=ids["customer"])
    async with pool.acquire() as conn:
        line = await conn.fetchrow(
            "SELECT * FROM order_line_item WHERE order_id = $1", order["order_id"]
        )
    settlement = await record_actual_weight(
        pool,
        order_line_item_id=str(line["id"]),
        weight_grams=600,  # 600g × 40元/斤 = 4800，超出 10% 容差
        tolerance_percent=10,
        created_by="tester",
    )
    assert settlement["diff_cents"] == 800
    assert settlement["within_tolerance"] is False
    assert settlement["status"] == "needs_customer_confirmation"
    async with pool.acquire() as conn:
        refunds = await conn.fetchval(
            "SELECT COUNT(*) FROM refund_request WHERE order_id = $1", order["order_id"]
        )
    assert refunds == 0
