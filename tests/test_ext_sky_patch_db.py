"""补天计划 (Operation Sky-Patch) DB 集成测试 — 需真实 Postgres (TEST_PG_DSN)。

覆盖：
  - Task 2.1 get_nearby_feed: 只返回最近节点内 有货 + 安全货架期 的批次
  - Task 2.2 scrap_batch_inventory + inventory_decay_engine: 过期批次报损清零
  - Task 3.1 trigger_initial_probe_workflow: 零号探针落库 + 幂等
  - Task 3.2 competitor_spider_engine: 爬虫结果归一化入库 price_benchmark
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
    from app.ext.spider_provider import ManualSpiderProvider

    reg = ProviderRegistry.get()
    reg.register_generic("payment", "manual", ManualPaymentProvider(), replace=True)
    reg.register_generic("payout", "manual", ManualPayoutProvider(), replace=True)
    reg.register_generic("notification", "log", LogNotificationProvider(), replace=True)
    spider = ManualSpiderProvider()
    reg.register_generic("spider", "manual", spider, replace=True)

    pool = await PgPool.create(
        name="hemall_skypatch_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await ensure_ext_schema(pool)
    pool._test_spider = spider  # type: ignore[attr-defined]
    yield pool
    await pool.close()


async def _make_variant(
    pool, *, lat: float = 31.0, lon: float = 121.0, title: str = "土鸡蛋"
) -> tuple[str, str, str]:
    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, lat, lng, status) "
            "VALUES ($1,'skypatch_loc','cn-east','host_skypatch','测试车库',$2,$3,'active') RETURNING id",
            uuid7(),
            lat,
            lon,
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            title,
            f"skypatch-slug-{uuid7()}",
        )
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"skypatch-sku-{uuid7()}",
        )
    return str(loc_id), str(var_id), title


async def _make_batch(
    pool,
    *,
    var_id: str,
    loc_id: str,
    stock_qty: int,
    retail_price: int = 3900,
    expiration_time: datetime | None = None,
) -> str:
    from obase.uuid7 import uuid7

    batch_id = uuid7()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inventory_batch "
            "(id, batch_no, variant_id, location_id, video_url, stock_qty, reserved_qty, "
            "cost_price_cents, retail_price_cents, inspection_status, expiration_time) "
            "VALUES ($1,$2,$3,$4,$5,$6,0,$7,$8,'passed',$9)",
            batch_id,
            f"skypatch-batch-{batch_id}",
            var_id,
            loc_id,
            "https://video.example/v.mp4",
            stock_qty,
            2000,
            retail_price,
            expiration_time,
        )
    return str(batch_id)


async def _nearby_feed(pool, *, lat: float, lon: float, limit: int = 50) -> dict:
    """直接驱动路由处理函数 (不依赖 HTTP 层，避免 lifespan/DB 降级干扰)。"""
    from app.routers import get_nearby_feed

    from fastapi import Request

    class _FakeRequest:
        def __init__(self, pool):
            self.app = type("App", (), {"state": type("S", (), {"pool": pool})()})()

    return await get_nearby_feed(_FakeRequest(pool), lat=lat, lon=lon, limit=limit)


# ── Task 2.1: 位置 Feed ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nearby_feed_returns_only_safe_active_batches(cn_pool):
    lat, lon = round(random.uniform(10, 50), 6), round(random.uniform(100, 130), 6)
    loc_id, var_id, _ = await _make_variant(cn_pool, lat=lat, lon=lon)

    safe_batch = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        stock_qty=10,
        expiration_time=datetime.now(UTC) + timedelta(hours=12),
    )
    # 过期批次 + 临期批次 (安全余量内) 必须被过滤掉。
    await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        stock_qty=10,
        expiration_time=datetime.now(UTC) - timedelta(minutes=5),
    )
    await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        stock_qty=10,
        expiration_time=datetime.now(UTC) + timedelta(hours=1),
    )

    result = await _nearby_feed(cn_pool, lat=lat, lon=lon)
    assert result["nearest_location"]["id"] == loc_id
    assert result["nearest_location"]["distance_km"] <= 1.0
    batch_ids = [b["batch_id"] for b in result["batches"]]
    assert safe_batch in batch_ids
    assert len(batch_ids) == 1  # 过期 + 临期都被挡在门外


# ── Task 2.2: 报损清零 + 衰减引擎 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_scrap_batch_inventory_zeroes_stock(cn_pool):
    from pathlib import Path

    from app.ext.omodul.scrap_batch_inventory import (
        ScrapBatchInventoryConfig,
        ScrapBatchInventoryInput,
        scrap_batch_inventory,
    )

    lat, lon = round(random.uniform(10, 50), 6), round(random.uniform(100, 130), 6)
    loc_id, var_id, _ = await _make_variant(cn_pool, lat=lat, lon=lon)
    batch_id = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        stock_qty=8,
        expiration_time=datetime.now(UTC) - timedelta(minutes=1),
    )

    result = await scrap_batch_inventory(
        ScrapBatchInventoryConfig(),
        ScrapBatchInventoryInput(batch_id=batch_id, reason="expired"),
        Path("/tmp/skypatch-trail"),
        pool=cn_pool,
    )
    assert result["status"] == "completed"
    assert result["scrapped_qty"] == 8

    async with cn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, stock_qty, reserved_qty FROM inventory_batch WHERE id = $1",
            batch_id,
        )
    assert row["status"] == "disposed"
    assert row["stock_qty"] == 0
    assert row["reserved_qty"] == 0


@pytest.mark.asyncio
async def test_scrap_batch_inventory_idempotent(cn_pool):
    from pathlib import Path

    from app.ext.omodul.scrap_batch_inventory import (
        ScrapBatchInventoryConfig,
        ScrapBatchInventoryInput,
        scrap_batch_inventory,
    )

    lat, lon = round(random.uniform(10, 50), 6), round(random.uniform(100, 130), 6)
    loc_id, var_id, _ = await _make_variant(cn_pool, lat=lat, lon=lon)
    batch_id = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        stock_qty=3,
        expiration_time=datetime.now(UTC) - timedelta(minutes=1),
    )
    first = await scrap_batch_inventory(
        ScrapBatchInventoryConfig(),
        ScrapBatchInventoryInput(batch_id=batch_id),
        Path("/tmp/skypatch-trail"),
        pool=cn_pool,
    )
    second = await scrap_batch_inventory(
        ScrapBatchInventoryConfig(),
        ScrapBatchInventoryInput(batch_id=batch_id),
        Path("/tmp/skypatch-trail"),
        pool=cn_pool,
    )
    assert first["status"] == "completed"
    assert second["status"] == "failed"  # 已 disposed，重复报损被拒


@pytest.mark.asyncio
async def test_inventory_decay_engine_scraps_expired(cn_pool):
    from app.config import Settings
    from app.ext.oservi import build_inventory_decay_engine

    lat, lon = round(random.uniform(10, 50), 6), round(random.uniform(100, 130), 6)
    loc_id, var_id, _ = await _make_variant(cn_pool, lat=lat, lon=lon)
    batch_id = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        stock_qty=5,
        expiration_time=datetime.now(UTC) - timedelta(minutes=2),
    )

    engine = build_inventory_decay_engine(cn_pool, settings=Settings())
    result = await engine.run_once()
    assert any(b["batch_id"] == batch_id for b in result["scrapped"])

    async with cn_pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM inventory_batch WHERE id = $1", batch_id
        )
    assert status == "disposed"


# ── Task 3.1: 零号探针 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trigger_initial_probe_workflow(cn_pool):
    from pathlib import Path

    from app.ext.omodul.trigger_initial_probe_workflow import (
        TriggerInitialProbeWorkflowConfig,
        TriggerInitialProbeWorkflowInput,
        trigger_initial_probe_workflow,
    )

    lat, lon = round(random.uniform(10, 50), 6), round(random.uniform(100, 130), 6)
    loc_id, var_id, _ = await _make_variant(cn_pool, lat=lat, lon=lon)
    batch_id = await _make_batch(cn_pool, var_id=var_id, loc_id=loc_id, stock_qty=10)

    result = await trigger_initial_probe_workflow(
        TriggerInitialProbeWorkflowConfig(),
        TriggerInitialProbeWorkflowInput(batch_id=batch_id, initial_price=3500),
        Path("/tmp/skypatch-trail"),
        pool=cn_pool,
    )
    assert result["status"] == "completed"
    assert result["probe_status"] == "testing"

    async with cn_pool.acquire() as conn:
        probe = await conn.fetchrow(
            "SELECT status, probe_price_cents FROM probe_order_log WHERE batch_id = $1",
            batch_id,
        )
        price = await conn.fetchval(
            "SELECT retail_price_cents FROM inventory_batch WHERE id = $1", batch_id
        )
    assert probe["status"] == "testing"
    assert probe["probe_price_cents"] == 3500
    assert price == 3500  # 顾客可见价同步为试探起始价

    # 幂等：同批次已有活跃探针，重复触发被拒。
    again = await trigger_initial_probe_workflow(
        TriggerInitialProbeWorkflowConfig(),
        TriggerInitialProbeWorkflowInput(batch_id=batch_id, initial_price=3000),
        Path("/tmp/skypatch-trail"),
        pool=cn_pool,
    )
    assert again["status"] == "failed"


# ── Task 3.2: 爬虫入库引擎 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_competitor_spider_engine_writes_price_benchmark(cn_pool):
    from app.ext.oservi import build_competitor_spider_engine

    lat, lon = round(random.uniform(10, 50), 6), round(random.uniform(100, 130), 6)
    loc_id, var_id, title = await _make_variant(cn_pool, lat=lat, lon=lon)

    cn_pool._test_spider.set_results(
        lat=lat,
        lon=lon,
        radius_km=5,
        results=[
            {"item": title, "price": 12.8, "unit": "500g", "store": "永辉超市"},
            {"item": "不相关商品", "price": 3.5, "unit": "1kg", "store": "沃尔玛"},
        ],
    )

    engine = build_competitor_spider_engine(cn_pool, radius_km=5)
    result = await engine.run_once()
    assert result["written"] == 2

    async with cn_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT variant_id, source_type, raw_price_cents, raw_unit, "
            "normalized_price_per_unit FROM price_benchmark WHERE source_type = 'spider' "
            "ORDER BY captured_at DESC LIMIT 2"
        )
    matched = {str(r["variant_id"]) == var_id for r in rows}
    assert any(matched)  # 标题匹配的条目带上了真 variant FK
    for row in rows:
        assert row["source_type"] == "spider"
        assert row["normalized_price_per_unit"] is not None  # 500g/1kg 都能归一化
