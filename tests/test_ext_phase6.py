"""hemall 扩展域 v5.0 集成测试 — 需真实 Postgres (TEST_PG_DSN)。

覆盖全局定价与冷启动预言机：omodul.reward_crowdsourced_benchmark_workflow
(众包小票核销/归一化/算力金奖励) 、omodul.submit_supplier_reverse_auction_
workflow (果农反向竞标毛利红线核验) 和 oservi.market_maker_probe_engine
(试探单出清/降价)。

统一改造后：奖励余额并入共享 customer.system_balance (不再是 扩展域自己
的 user_profiles 表)，价格基线表改名 price_benchmark 且 sku_id 改成真
variant_id FK，批次/门店走共享表。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

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

    from app.ext.cv_provider import ManualCVProvider
    from app.ext.schema import ensure_ext_schema
    from app.ext.spider_provider import ManualSpiderProvider

    reg = ProviderRegistry.get()
    reg.register_generic("payment", "manual", ManualPaymentProvider(), replace=True)
    cv = ManualCVProvider()
    reg.register_generic("cv", "manual", cv, replace=True)
    reg.register_generic("spider", "manual", ManualSpiderProvider(), replace=True)

    pool = await PgPool.create(
        name="hemall_phase6_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await ensure_ext_schema(pool)
    pool._test_cv = cv  # type: ignore[attr-defined]
    yield pool
    await pool.close()


async def _make_variant(pool, *, product_name: str) -> tuple[str, str, str]:
    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, lat, lng, status) "
            "VALUES ($1,'p6_loc','cn-east','host_p6','测试车库',31.0,121.0,'active') RETURNING id",
            uuid7(),
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            product_name,
            f"p6-slug-{uuid7()}",
        )
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"p6-sku-{uuid7()}",
        )
    return str(loc_id), str(prod_id), str(var_id)


async def _make_batch(
    pool,
    *,
    var_id: str,
    loc_id: str,
    stock_qty: int = 10,
    retail_price: int = 3900,
    cost_price: int = 2000,
    status: str = "active",
) -> str:
    from obase.uuid7 import uuid7

    batch_id = uuid7()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inventory_batch "
            "(id, batch_no, variant_id, location_id, video_url, stock_qty, reserved_qty, "
            "cost_price_cents, retail_price_cents, status, inspection_status) "
            "VALUES ($1,$2,$3,$4,$5,$6,0,$7,$8,$9,'passed')",
            batch_id,
            f"p6-batch-{batch_id}",
            var_id,
            loc_id,
            "https://video.example/v.mp4",
            stock_qty,
            cost_price,
            retail_price,
            status,
        )
    return str(batch_id)


async def _make_customer(pool) -> str:
    from obase.uuid7 import uuid7

    customer_id = uuid7()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO customer (id, email, status) VALUES ($1,$2,'active')",
            customer_id,
            f"p6-{customer_id}@example.com",
        )
    return str(customer_id)


async def _make_order(pool, *, batch_id: str, qty: int = 1) -> str:
    from omodul.add_line_item_to_cart import (
        AddLineItemConfig,
        AddLineItemInput,
        add_line_item_to_cart,
    )
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
    from omodul.create_cart import CreateCartConfig, CreateCartInput, create_cart
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

    cart_result = await create_cart(
        CreateCartConfig(), CreateCartInput(), Path("/tmp"), pool=pool
    )
    assert cart_result["status"] == "completed", cart_result
    cart_id = cart_result["cart_id"]

    add_result = await add_line_item_to_cart(
        AddLineItemConfig(),
        AddLineItemInput(cart_id=cart_id, batch_id=batch_id, quantity=qty),
        Path("/tmp"),
        pool=pool,
    )
    assert add_result["status"] == "completed", add_result

    sessions_result = await create_payment_sessions(
        CreatePaymentSessionsConfig(),
        CreatePaymentSessionsInput(cart_id=cart_id, provider_names=["manual"]),
        Path("/tmp"),
        pool=pool,
    )
    assert sessions_result["status"] == "completed", sessions_result

    set_result = await set_payment_session(
        SetPaymentSessionConfig(),
        SetPaymentSessionInput(cart_id=cart_id, provider_name="manual"),
        Path("/tmp"),
        pool=pool,
    )
    assert set_result["status"] == "completed", set_result

    authorize_result = await authorize_payment_for_cart(
        AuthorizePaymentForCartConfig(),
        AuthorizePaymentForCartInput(cart_id=cart_id),
        Path("/tmp"),
        pool=pool,
    )
    assert authorize_result["status"] == "completed", authorize_result

    checkout_result = await complete_checkout(
        CompleteCheckoutConfig(),
        CompleteCheckoutInput(cart_id=cart_id),
        Path("/tmp"),
        pool=pool,
    )
    assert checkout_result["status"] == "completed", checkout_result
    return str(checkout_result["order_id"])


# ── reward_crowdsourced_benchmark_workflow ───────────────────────────────────


@pytest.mark.asyncio
async def test_reward_crowdsourced_benchmark_rejects_unrecognized_receipt(
    cn_pool, tmp_path
):
    from app.ext.omodul.reward_crowdsourced_benchmark_workflow import (
        RewardCrowdsourcedBenchmarkWorkflowConfig,
        RewardCrowdsourcedBenchmarkWorkflowInput,
        reward_crowdsourced_benchmark_workflow,
    )

    customer_id = await _make_customer(cn_pool)
    result = await reward_crowdsourced_benchmark_workflow(
        RewardCrowdsourcedBenchmarkWorkflowConfig(),
        RewardCrowdsourcedBenchmarkWorkflowInput(
            customer_id=customer_id, receipt_image=b"blank"
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "failed"
    assert "unrecognized_receipt" in result["error"]["message"]


@pytest.mark.asyncio
async def test_reward_crowdsourced_benchmark_matches_variant_and_rewards_balance(
    cn_pool, tmp_path
):
    from obase.uuid7 import uuid7

    from app.ext.omodul.reward_crowdsourced_benchmark_workflow import (
        RewardCrowdsourcedBenchmarkWorkflowConfig,
        RewardCrowdsourcedBenchmarkWorkflowInput,
        reward_crowdsourced_benchmark_workflow,
    )

    # 商品名带唯一后缀——避免跟共享真实 DB 里历史测试残留的同名商品撞车
    # (子串匹配会命中所有同名商品，选中的可能是别的测试留下的旧 variant)。
    product_name = f"车厘子p6_{uuid7()}"
    loc_id, prod_id, var_id = await _make_variant(cn_pool, product_name=product_name)
    customer_id = await _make_customer(cn_pool)

    cn_pool._test_cv.set_receipt_result(
        image_bytes=b"receipt-1",
        items=[
            {"item": product_name, "price": 1280, "unit": "500g", "store": "Hema"},
            {
                "item": "完全不存在的水果p6",
                "price": 800,
                "unit": "1盒",
                "store": "NTUC",
            },
        ],
    )

    result = await reward_crowdsourced_benchmark_workflow(
        RewardCrowdsourcedBenchmarkWorkflowConfig(),
        RewardCrowdsourcedBenchmarkWorkflowInput(
            customer_id=customer_id, receipt_image=b"receipt-1"
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["rewarded_cents"] == 500
    assert result["benchmarks_added"] == 2

    async with cn_pool.acquire() as conn:
        balance = await conn.fetchval(
            "SELECT system_balance FROM customer WHERE id=$1", customer_id
        )
        rows = await conn.fetch(
            "SELECT variant_id, raw_item_name, raw_price_cents, normalized_price_per_unit "
            "FROM price_benchmark WHERE id = ANY($1)",
            result["benchmark_ids"],
        )
    assert balance == 500
    matched = next(r for r in rows if r["raw_price_cents"] == 1280)
    assert str(matched["variant_id"]) == var_id
    assert float(matched["normalized_price_per_unit"]) == 1280 / 500
    unmatched = next(r for r in rows if r["raw_price_cents"] == 800)
    assert unmatched["variant_id"] is None
    assert unmatched["raw_item_name"] == "完全不存在的水果p6"
    assert (
        unmatched["normalized_price_per_unit"] is None
    )  # "1盒" 无法归一化，NULL 不污染均值


@pytest.mark.asyncio
async def test_reward_crowdsourced_benchmark_accumulates_balance_across_calls(
    cn_pool, tmp_path
):
    from app.ext.omodul.reward_crowdsourced_benchmark_workflow import (
        RewardCrowdsourcedBenchmarkWorkflowConfig,
        RewardCrowdsourcedBenchmarkWorkflowInput,
        reward_crowdsourced_benchmark_workflow,
    )

    customer_id = await _make_customer(cn_pool)
    for i in range(2):
        cn_pool._test_cv.set_receipt_result(
            image_bytes=f"receipt-acc-{i}".encode(),
            items=[{"item": f"任意商品{i}", "price": 100, "unit": "1件", "store": "x"}],
        )
        result = await reward_crowdsourced_benchmark_workflow(
            RewardCrowdsourcedBenchmarkWorkflowConfig(),
            RewardCrowdsourcedBenchmarkWorkflowInput(
                customer_id=customer_id, receipt_image=f"receipt-acc-{i}".encode()
            ),
            tmp_path,
            pool=cn_pool,
        )
        assert result["status"] == "completed", result

    async with cn_pool.acquire() as conn:
        balance = await conn.fetchval(
            "SELECT system_balance FROM customer WHERE id=$1", customer_id
        )
    assert balance == 1000  # 500 * 2，靠 upsert 累加而不是覆盖


# ── submit_supplier_reverse_auction_workflow ─────────────────────────────────


@pytest.mark.asyncio
async def test_reverse_auction_fails_without_benchmark_data(cn_pool, tmp_path):
    from app.ext.omodul.submit_supplier_reverse_auction_workflow import (
        SubmitSupplierReverseAuctionWorkflowConfig,
        SubmitSupplierReverseAuctionWorkflowInput,
        submit_supplier_reverse_auction_workflow,
    )

    loc_id, _, var_id = await _make_variant(cn_pool, product_name="无基线商品p6")
    batch_id = await _make_batch(cn_pool, var_id=var_id, loc_id=loc_id)

    result = await submit_supplier_reverse_auction_workflow(
        SubmitSupplierReverseAuctionWorkflowConfig(),
        SubmitSupplierReverseAuctionWorkflowInput(
            batch_id=batch_id, supplier_bid_price_per_gram=1.0, batch_cost_price=100000
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "failed"
    assert "no price benchmark data" in result["error"]["message"]


async def _seed_benchmark(pool, *, variant_id: str, normalized_price: float) -> None:
    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO price_benchmark "
            "(id, variant_id, source_type, raw_price_cents, raw_unit, normalized_price_per_unit) "
            "VALUES ($1,$2,'spider',$3,$4,$5)",
            uuid7(),
            variant_id,
            int(normalized_price * 500),
            "500g",
            normalized_price,
        )


@pytest.mark.asyncio
async def test_reverse_auction_approves_bid_clearing_margin(cn_pool, tmp_path):
    from app.ext.omodul.submit_supplier_reverse_auction_workflow import (
        SubmitSupplierReverseAuctionWorkflowConfig,
        SubmitSupplierReverseAuctionWorkflowInput,
        submit_supplier_reverse_auction_workflow,
    )

    loc_id, _, var_id = await _make_variant(cn_pool, product_name="毛利达标商品p6")
    batch_id = await _make_batch(cn_pool, var_id=var_id, loc_id=loc_id)
    await _seed_benchmark(cn_pool, variant_id=var_id, normalized_price=2.56)

    result = await submit_supplier_reverse_auction_workflow(
        SubmitSupplierReverseAuctionWorkflowConfig(),
        SubmitSupplierReverseAuctionWorkflowInput(
            batch_id=batch_id, supplier_bid_price_per_gram=1.0, batch_cost_price=150000
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["action"] == "batch_approved"

    async with cn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT cost_price_cents, status FROM inventory_batch WHERE id=$1", batch_id
        )
    assert row["cost_price_cents"] == 150000
    assert row["status"] == "in_transit"


@pytest.mark.asyncio
async def test_reverse_auction_rejects_bid_below_margin(cn_pool, tmp_path):
    from app.ext.omodul.submit_supplier_reverse_auction_workflow import (
        SubmitSupplierReverseAuctionWorkflowConfig,
        SubmitSupplierReverseAuctionWorkflowInput,
        submit_supplier_reverse_auction_workflow,
    )

    loc_id, _, var_id = await _make_variant(cn_pool, product_name="毛利不达标商品p6")
    batch_id = await _make_batch(cn_pool, var_id=var_id, loc_id=loc_id)
    await _seed_benchmark(cn_pool, variant_id=var_id, normalized_price=2.0)

    result = await submit_supplier_reverse_auction_workflow(
        SubmitSupplierReverseAuctionWorkflowConfig(),
        SubmitSupplierReverseAuctionWorkflowInput(
            batch_id=batch_id, supplier_bid_price_per_gram=1.5, batch_cost_price=150000
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "failed"
    assert "margin_too_low" in result["error"]["message"]

    async with cn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT cost_price_cents, status FROM inventory_batch WHERE id=$1", batch_id
        )
    assert row["cost_price_cents"] == 2000  # 未被改动
    assert row["status"] == "active"


# ── market_maker_probe_engine ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_market_maker_probe_engine_clears_and_reprices(cn_pool, tmp_path):
    from obase.uuid7 import uuid7

    from app.ext.oservi import build_market_maker_probe_engine

    loc_id, _, var_id = await _make_variant(cn_pool, product_name="试探单商品p6")
    now = datetime.now(UTC)

    # 会成交的批次: 1 单真实成交
    cleared_batch = await _make_batch(
        cn_pool, var_id=var_id, loc_id=loc_id, retail_price=3900
    )
    await _make_order(cn_pool, batch_id=cleared_batch, qty=1)
    cleared_probe = uuid7()
    async with cn_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO probe_order_log (id, batch_id, probe_price_cents, created_at) "
            "VALUES ($1,$2,$3,$4)",
            cleared_probe,
            cleared_batch,
            3900,
            now - timedelta(hours=2),
        )

    # 卖不动的批次: 零成交
    slow_batch = await _make_batch(
        cn_pool, var_id=var_id, loc_id=loc_id, retail_price=4200
    )
    slow_probe = uuid7()
    async with cn_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO probe_order_log (id, batch_id, probe_price_cents, created_at) "
            "VALUES ($1,$2,$3,$4)",
            slow_probe,
            slow_batch,
            4200,
            now - timedelta(hours=2),
        )

    # 刚创建、还没到 1 小时冷却期的探针，这一 tick 不该被处理
    fresh_batch = await _make_batch(
        cn_pool, var_id=var_id, loc_id=loc_id, retail_price=3500
    )
    fresh_probe = uuid7()
    async with cn_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO probe_order_log (id, batch_id, probe_price_cents) VALUES ($1,$2,$3)",
            fresh_probe,
            fresh_batch,
            3500,
        )

    engine = build_market_maker_probe_engine(cn_pool, target_velocity=0.01)
    results = await engine.run_once()
    summary = results[0]

    cleared_ids = {c["batch_id"] for c in summary["cleared"]}
    repriced_ids = {r["batch_id"] for r in summary["repriced"]}
    assert cleared_batch in cleared_ids
    assert slow_batch in repriced_ids
    assert fresh_batch not in cleared_ids and fresh_batch not in repriced_ids

    async with cn_pool.acquire() as conn:
        cleared_probe_row = await conn.fetchrow(
            "SELECT status FROM probe_order_log WHERE id=$1", cleared_probe
        )
        slow_probe_row = await conn.fetchrow(
            "SELECT status FROM probe_order_log WHERE id=$1", slow_probe
        )
        cleared_batch_row = await conn.fetchrow(
            "SELECT retail_price_cents FROM inventory_batch WHERE id=$1", cleared_batch
        )
        slow_batch_row = await conn.fetchrow(
            "SELECT retail_price_cents FROM inventory_batch WHERE id=$1", slow_batch
        )
        slow_new_probes = await conn.fetch(
            "SELECT status, probe_price_cents FROM probe_order_log WHERE batch_id=$1",
            slow_batch,
        )

    assert cleared_probe_row["status"] == "cleared"
    assert slow_probe_row["status"] == "failed"
    assert cleared_batch_row["retail_price_cents"] == 3900
    assert slow_batch_row["retail_price_cents"] == int(4200 * 0.95)
    assert len(slow_new_probes) == 2
    assert any(
        p["status"] == "testing" and p["probe_price_cents"] == int(4200 * 0.95)
        for p in slow_new_probes
    )
