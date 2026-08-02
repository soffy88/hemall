"""hemall 扩展域 §5 oservi 引擎集成测试 — 需真实 Postgres (TEST_PG_DSN)。

每个引擎都通过 CronSchedulerEngine.run_once() / EventWebhookDispatcherEngine
.dispatch() 驱动单次执行 (骨架自带的测试入口)，不跑真正的常驻循环/定时器。

统一改造后：物理批次/门店/订单全部走共享表 (product/product_variant/
inventory_batch/stock_location/customer_order/order_line_item)，不再是
扩展域自己的平行表。
"""

from __future__ import annotations

import os
import random
from datetime import UTC, datetime, timedelta

import pytest

TEST_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration"
)


@pytest.fixture
async def cn_pool():
    from obase.notification_providers import LogNotificationProvider
    from obase.payment_providers import ManualPaymentProvider
    from obase.persistence.pool import PgPool
    from obase.provider_registry import ProviderRegistry

    from app.ext.payout_provider import ManualPayoutProvider
    from app.ext.schema import ensure_ext_schema
    from app.ext.weather_provider import ManualWeatherProvider

    reg = ProviderRegistry.get()
    reg.register_generic("payment", "manual", ManualPaymentProvider(), replace=True)
    reg.register_generic("payout", "manual", ManualPayoutProvider(), replace=True)
    reg.register_generic("notification", "log", LogNotificationProvider(), replace=True)
    weather = ManualWeatherProvider()
    reg.register_generic("weather", "manual", weather, replace=True)

    pool = await PgPool.create(
        name="hemall_oservi_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await ensure_ext_schema(pool)
    pool._test_weather = weather  # type: ignore[attr-defined]
    yield pool
    await pool.close()


async def _make_variant(
    pool, *, lat: float = 31.0, lon: float = 121.0
) -> tuple[str, str]:
    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, lat, lng, status) "
            "VALUES ($1,'oservi_loc','cn-east','host_oservi','测试车库',$2,$3,'active') RETURNING id",
            uuid7(),
            lat,
            lon,
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            "土鸡蛋",
            f"oservi-slug-{uuid7()}",
        )
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"oservi-sku-{uuid7()}",
        )
    return str(loc_id), str(var_id)


async def _make_batch(
    pool,
    *,
    var_id: str,
    loc_id: str,
    stock_qty: int,
    reserved_qty: int = 0,
    retail_price: int = 3900,
    cost_price: int = 2000,
    intake_time: datetime | None = None,
    expiration_time: datetime | None = None,
) -> str:
    from obase.uuid7 import uuid7

    batch_id = uuid7()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inventory_batch "
            "(id, batch_no, variant_id, location_id, video_url, stock_qty, reserved_qty, "
            "cost_price_cents, retail_price_cents, inspection_status, created_at, expiration_time) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'passed',COALESCE($10, NOW()),$11)",
            batch_id,
            f"oservi-batch-{batch_id}",
            var_id,
            loc_id,
            "https://video.example/v.mp4",
            stock_qty,
            reserved_qty,
            cost_price,
            retail_price,
            intake_time,
            expiration_time,
        )
    return str(batch_id)


# ── weather_arbitrage_engine ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_weather_arbitrage_engine_reprices_on_storm(cn_pool):
    from app.ext.oservi import (
        build_batch_broadcast_engine,
        build_weather_arbitrage_engine,
    )

    # 共享真实 DB，不回滚——用随机坐标避免跟历史测试残留的 location 撞在
    # 同一个 (lat, lon) 上，导致 set_forecast 覆写命中别的批次，count 断言失真。
    lat, lon = round(random.uniform(10, 50), 6), round(random.uniform(100, 130), 6)
    loc_id, var_id = await _make_variant(cn_pool, lat=lat, lon=lon)
    batch_id = await _make_batch(
        cn_pool, var_id=var_id, loc_id=loc_id, stock_qty=50, retail_price=3900
    )
    cn_pool._test_weather.set_forecast(
        lat=lat, lon=lon, rain_probability=0.9, rain_intensity=3
    )

    broadcast = build_batch_broadcast_engine()
    engine = build_weather_arbitrage_engine(cn_pool, broadcast_engine=broadcast)
    results = await engine.run_once()

    assert len(results) == 1
    summary = results[0]
    repriced_ids = {r["batch_id"] for r in summary["repriced"]}
    assert batch_id in repriced_ids

    async with cn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT retail_price_cents FROM inventory_batch WHERE id=$1", batch_id
        )
    assert row["retail_price_cents"] < 3900


@pytest.mark.asyncio
async def test_weather_arbitrage_engine_calm_weather_no_change(cn_pool):
    from app.ext.oservi import (
        build_batch_broadcast_engine,
        build_weather_arbitrage_engine,
    )

    loc_id, var_id = await _make_variant(cn_pool, lat=32.5, lon=122.5)
    await _make_batch(
        cn_pool, var_id=var_id, loc_id=loc_id, stock_qty=50, retail_price=3900
    )

    broadcast = build_batch_broadcast_engine()
    engine = build_weather_arbitrage_engine(cn_pool, broadcast_engine=broadcast)
    results = await engine.run_once()

    assert results[0]["batches_repriced"] == 0


# ── inventory_reaper_engine ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inventory_reaper_engine_disposes_expired_and_marks_down_near_expiry(
    cn_pool,
):
    from app.ext.oservi import build_inventory_reaper_engine
    from app.config import Settings

    loc_id, var_id = await _make_variant(cn_pool)
    now = datetime.now(UTC)

    expired_batch = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        stock_qty=10,
        retail_price=3900,
        cost_price=2000,
        intake_time=now - timedelta(days=10),
        expiration_time=now - timedelta(hours=1),
    )
    near_expiry_batch = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        stock_qty=10,
        retail_price=3900,  # 1.5x cost=2000 时的入库价
        cost_price=2000,
        intake_time=now - timedelta(hours=23),
        expiration_time=now + timedelta(hours=1),  # 24h 保质期，已过 23/24
    )

    engine = build_inventory_reaper_engine(
        cn_pool, settings=Settings(output_root="/tmp")
    )
    results = await engine.run_once()

    summary = results[0]
    assert expired_batch in summary["disposed"]
    assert any(r["batch_id"] == near_expiry_batch for r in summary["repriced"])

    async with cn_pool.acquire() as conn:
        expired_row = await conn.fetchrow(
            "SELECT status FROM inventory_batch WHERE id=$1", expired_batch
        )
        near_row = await conn.fetchrow(
            "SELECT retail_price_cents FROM inventory_batch WHERE id=$1",
            near_expiry_batch,
        )
    assert expired_row["status"] == "disposed"
    assert near_row["retail_price_cents"] < 3900


# ── demand_aggregator_engine ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_demand_aggregator_engine_dispatches_po_notification(cn_pool):
    from app.ext.oservi import build_demand_aggregator_engine

    engine = build_demand_aggregator_engine()
    result = await engine.dispatch(
        "crowd_intent.threshold_reached", {"variant_id": "var_x", "count": 500}
    )

    assert result["status"] == "completed"
    assert result["errors"] == []
    assert result["results"][0]["notified"] is True


# ── delivery_wave_engine ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delivery_wave_engine_dispatches_wave_orders_once(cn_pool):
    from obase.uuid7 import uuid7

    from app.ext.oservi import build_delivery_wave_engine
    from app.ext.oskill import WAVE_SHIPPING_PRICE_CENTS

    loc_id, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(cn_pool, var_id=var_id, loc_id=loc_id, stock_qty=10)

    order_id = uuid7()
    oli_id = uuid7()
    async with cn_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO customer_order (id, currency, status, subtotal_cents, "
            "discount_cents, tax_cents, shipping_cents, grand_total_cents) "
            "VALUES ($1,'CNY','paid',$2,0,0,$3,$4)",
            order_id,
            3900,
            WAVE_SHIPPING_PRICE_CENTS,
            3900 + WAVE_SHIPPING_PRICE_CENTS,
        )
        await conn.execute(
            "INSERT INTO order_line_item (id, order_id, batch_id, quantity, "
            "unit_price_cents, line_total_cents) VALUES ($1,$2,$3,$4,$5,$5)",
            oli_id,
            order_id,
            batch_id,
            1,
            3900,
        )

    engine = build_delivery_wave_engine(cn_pool)
    results = await engine.run_once()
    summary = results[0]
    assert summary["wave_orders_dispatched"] == 1
    assert summary["by_location"] == {loc_id: 1}

    async with cn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT dispatch_status, dispatched_at FROM customer_order WHERE id=$1",
            order_id,
        )
    assert row["dispatch_status"] == "dispatched"
    assert row["dispatched_at"] is not None

    # 再跑一次 tick，不应该重复打包同一个订单
    again = await engine.run_once()
    assert again[0]["wave_orders_dispatched"] == 0


# ── node_host_settlement_engine ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_node_host_settlement_engine_settles_pending_labor(cn_pool):
    from app.ext.oprim import generate_id_v7
    from app.ext.oservi import build_node_host_settlement_engine
    from app.config import Settings

    worker_id = generate_id_v7("worker")
    async with cn_pool.acquire() as conn:
        for amount in (500, 700):
            await conn.execute(
                "INSERT INTO labor_ledger (id, worker_id, wage_amount, status) VALUES ($1,$2,$3,'pending')",
                generate_id_v7("labor"),
                worker_id,
                amount,
            )

    engine = build_node_host_settlement_engine(
        cn_pool, settings=Settings(output_root="/tmp")
    )
    results = await engine.run_once()
    summary = results[0]

    settled = {e["worker_id"]: e["total_amount"] for e in summary["labor_settled"]}
    assert settled.get(worker_id) == 1200
    assert "host_dividends_skipped_reason" in summary

    async with cn_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status FROM labor_ledger WHERE worker_id=$1", worker_id
        )
    assert all(r["status"] == "paid" for r in rows)


# ── batch_broadcast_engine ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_broadcast_engine_pushes_signal(cn_pool):
    from app.ext.oservi import build_batch_broadcast_engine

    engine = build_batch_broadcast_engine()
    result = await engine.dispatch("batch.listed", {"batch_id": "batch_x"})

    assert result["status"] == "completed"
    assert result["errors"] == []
    assert result["results"][0]["pushed"] is True
