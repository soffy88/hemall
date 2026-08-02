"""hemall 扩展域 入库→加车→结账全链路集成测试 — 需真实 Postgres (TEST_PG_DSN)。

统一改造后：扩展域自己重复实现的 add_line_item_to_cart/complete_checkout
已经退休，购物车/结账走的就是 hemall 原有 /store/* 背后那套共享 omodul
(omodul.create_cart / omodul.add_line_item_to_cart / omodul.
create_payment_sessions / omodul.set_payment_session / omodul.
authorize_payment_for_cart / omodul.complete_checkout)——这个文件验证
本地的 create_inventory_batch 薄封装 (打 expiration_time/supplier_id 补丁
列) 跟这条共享链路真的能串起来用，不是接不上的孤岛。
"""

from __future__ import annotations

import os

import pytest

TEST_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration"
)


@pytest.fixture
async def cn_pool():
    from obase.payment_providers import ManualPaymentProvider
    from obase.persistence.pool import PgPool
    from obase.provider_registry import ProviderRegistry

    from app.ext.schema import ensure_ext_schema

    ProviderRegistry.get().register_generic(
        "payment", "manual", ManualPaymentProvider(), replace=True
    )
    pool = await PgPool.create(
        name="hemall_e2e_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await ensure_ext_schema(pool)
    yield pool
    await pool.close()


async def _make_variant(pool) -> tuple[str, str]:
    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, lat, lng, status) "
            "VALUES ($1,'e2e_loc','cn-east','host_e2e','测试车库',31.0,121.0,'active') RETURNING id",
            uuid7(),
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,'土鸡蛋',$2,'active') RETURNING id",
            uuid7(),
            f"e2e-slug-{uuid7()}",
        )
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"e2e-sku-{uuid7()}",
        )
    return str(loc_id), str(var_id)


@pytest.mark.asyncio
async def test_intake_to_checkout_full_chain(cn_pool, tmp_path):
    from app.ext.omodul.create_inventory_batch import (
        CreateInventoryBatchConfig,
        CreateInventoryBatchInput,
        create_inventory_batch,
    )

    loc_id, var_id = await _make_variant(cn_pool)

    # 1. 入库 (本地薄封装：共享核心插入 + 打补丁列)
    intake = await create_inventory_batch(
        CreateInventoryBatchConfig(),
        CreateInventoryBatchInput(
            variant_id=var_id,
            location_id=loc_id,
            video_url="https://video.example/v.mp4",
            stock_qty=10,
            cost_price=2000,
            retail_price=3900,
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert intake["status"] == "completed", intake
    batch_id = intake["batch_id"]

    # 1b. video_url 为空必须拒绝
    rejected = await create_inventory_batch(
        CreateInventoryBatchConfig(),
        CreateInventoryBatchInput(
            variant_id=var_id,
            location_id=loc_id,
            video_url="",
            stock_qty=5,
            cost_price=1000,
            retail_price=2000,
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert rejected["status"] == "failed"
    assert "video_url" in rejected["error"]["message"]

    # 2. 建车 + 加车 (共享 omodul，不是 扩展域自己的版本)
    from omodul.add_line_item_to_cart import (
        AddLineItemConfig,
        AddLineItemInput,
        add_line_item_to_cart,
    )
    from omodul.create_cart import CreateCartConfig, CreateCartInput, create_cart

    cart_result = await create_cart(
        CreateCartConfig(), CreateCartInput(), tmp_path, pool=cn_pool
    )
    assert cart_result["status"] == "completed", cart_result
    cart_id = cart_result["cart_id"]

    add_result = await add_line_item_to_cart(
        AddLineItemConfig(),
        AddLineItemInput(cart_id=cart_id, batch_id=batch_id, quantity=3),
        tmp_path,
        pool=cn_pool,
    )
    assert add_result["status"] == "completed", add_result
    assert add_result["unit_price_cents"] == 3900
    assert add_result["subtotal_cents"] == 11700

    # 2b. 加车强绑定真实存在的 batch_id —— 假 batch_id 必须拒绝
    import uuid

    fake_add = await add_line_item_to_cart(
        AddLineItemConfig(),
        AddLineItemInput(cart_id=cart_id, batch_id=str(uuid.uuid4()), quantity=1),
        tmp_path,
        pool=cn_pool,
    )
    assert fake_add["status"] == "failed"
    assert "not found" in fake_add["error"]["message"]

    # 3. 授权支付 + 结账 (共享链路：create_payment_sessions -> set_payment_session
    # -> authorize_payment_for_cart -> complete_checkout，跟 app/storefront.py
    # 的 /store/checkout 端点内部走的是同一条链)
    from omodul.authorize_payment_for_cart import (
        AuthorizePaymentForCartConfig,
        AuthorizePaymentForCartInput,
        authorize_payment_for_cart,
    )
    from omodul.complete_checkout import (
        CompleteCheckoutConfig,
        CompleteCheckoutInput,
        complete_checkout,
    )
    from omodul.create_payment_sessions import (
        CreatePaymentSessionsConfig,
        CreatePaymentSessionsInput,
        create_payment_sessions,
    )
    from omodul.set_payment_session import (
        SetPaymentSessionConfig,
        SetPaymentSessionInput,
        set_payment_session,
    )

    sessions_result = await create_payment_sessions(
        CreatePaymentSessionsConfig(),
        CreatePaymentSessionsInput(cart_id=cart_id, provider_names=["manual"]),
        tmp_path,
        pool=cn_pool,
    )
    assert sessions_result["status"] == "completed", sessions_result

    set_result = await set_payment_session(
        SetPaymentSessionConfig(),
        SetPaymentSessionInput(cart_id=cart_id, provider_name="manual"),
        tmp_path,
        pool=cn_pool,
    )
    assert set_result["status"] == "completed", set_result

    authorize_result = await authorize_payment_for_cart(
        AuthorizePaymentForCartConfig(),
        AuthorizePaymentForCartInput(cart_id=cart_id),
        tmp_path,
        pool=cn_pool,
    )
    assert authorize_result["status"] == "completed", authorize_result

    checkout_result = await complete_checkout(
        CompleteCheckoutConfig(),
        CompleteCheckoutInput(cart_id=cart_id),
        tmp_path,
        pool=cn_pool,
    )
    assert checkout_result["status"] == "completed", checkout_result
    assert checkout_result["grand_total_cents"] == 11700

    async with cn_pool.acquire() as conn:
        batch_row = await conn.fetchrow(
            "SELECT stock_qty, reserved_qty FROM inventory_batch WHERE id=$1",
            batch_id,
        )
        order_row = await conn.fetchrow(
            "SELECT status, grand_total_cents FROM customer_order WHERE id=$1",
            checkout_result["order_id"],
        )
        oli_rows = await conn.fetch(
            "SELECT batch_id, quantity, unit_price_cents FROM order_line_item WHERE order_id=$1",
            checkout_result["order_id"],
        )

    # complete_checkout 把预留转成永久出库：stock_qty 和 reserved_qty 同时扣减
    assert batch_row["stock_qty"] == 7
    assert batch_row["reserved_qty"] == 0
    assert order_row["status"] == "pending"
    assert order_row["grand_total_cents"] == 11700
    assert len(oli_rows) == 1
    assert str(oli_rows[0]["batch_id"]) == batch_id
    assert oli_rows[0]["quantity"] == 3
