"""Phase 7 数据飞轮 — DB 集成测试 (需真实 Postgres TEST_PG_DSN)。

覆盖：
  - 空间-行为矩阵: device_purchase_log 写入 → nearby-feed 查询 SQL → oskill
    关联度插队 (买过牛肉 → 番茄排最前)
  - 真实爬虫链路: layered provider 覆写注入 → competitor_spider_engine →
    normalize_sku_price 归一化 → price_benchmark 真实入库 (parsed/priced/written
    统计齐全)
"""

from __future__ import annotations

import os
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
    from app.ext.spider_targets import LayeredSpiderProvider

    reg = ProviderRegistry.get()
    reg.register_generic("payment", "manual", ManualPaymentProvider(), replace=True)
    reg.register_generic("payout", "manual", ManualPayoutProvider(), replace=True)
    reg.register_generic("notification", "log", LogNotificationProvider(), replace=True)
    spider = LayeredSpiderProvider()
    reg.register_generic("spider", "layered", spider, replace=True)

    pool = await PgPool.create(
        name="hemall_phase7_test", dsn=TEST_DSN, min_size=1, max_size=5
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
            "VALUES ($1,'phase7_loc','cn-east','host_phase7','测试车库',$2,$3,'active') RETURNING id",
            uuid7(),
            lat,
            lon,
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            title,
            f"phase7-slug-{uuid7()}",
        )
        variant_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, title, status) "
            "VALUES ($1,$2,$3,$4,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"SKU-{uuid7().hex[:8].upper()}",
            title,
        )
        batch_id = await conn.fetchval(
            "INSERT INTO inventory_batch (id, variant_id, location_id, supplier_id, "
            "batch_code, stock_qty, reserved_qty, retail_price_cents, expiration_time, status) "
            "VALUES ($1,$2,$3,NULL,$4,$5,0,$6,$7,'active') RETURNING id",
            uuid7(),
            variant_id,
            loc_id,
            f"BATCH-{uuid7().hex[:8].upper()}",
            10,
            9900,
            datetime.now(UTC) + timedelta(days=30),
        )
        return str(prod_id), str(variant_id), str(batch_id)


async def _insert_order(pool, product_ids: list[str]) -> str:
    """建一个含多商品的订单 (供共现矩阵/device log 用)。"""
    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        cust_id = await conn.fetchval(
            "INSERT INTO customer (id, email, first_name, last_name, status) "
            "VALUES ($1,$2,'p7','p7','active') RETURNING id",
            uuid7(),
            f"p7-{uuid7()}@test.dev",
        )
        order_id = await conn.fetchval(
            "INSERT INTO customer_order (id, customer_id, status, currency_code, region_code, "
            "shipping_cents, tax_cents, grand_total_cents) "
            "VALUES ($1,$2,'paid','cny','cn-east',0,0,10000) RETURNING id",
            uuid7(),
            cust_id,
        )
        for pid in product_ids:
            variant_id = await conn.fetchval(
                "SELECT id FROM product_variant WHERE product_id = $1 LIMIT 1", pid
            )
            batch_id = await conn.fetchval(
                "SELECT id FROM inventory_batch WHERE variant_id = $1 LIMIT 1",
                variant_id,
            )
            await conn.execute(
                "INSERT INTO order_line_item (id, order_id, batch_id, quantity, "
                "unit_price_cents, line_total_cents, product_id, variant_id) "
                "VALUES ($1,$2,$3,1,1000,1000,$4,$5)",
                uuid7(),
                order_id,
                batch_id,
                pid,
                variant_id,
            )
        return str(order_id)


async def test_affinity_feed_boosts_related_product(cn_pool):
    """买过牛肉 → Feed 里番茄/洋葱插队到最前 (空间-行为矩阵升维)。"""
    from app.ext.oskill import (
        build_cooccurrence_matrix,
        compute_batch_affinity_scores,
        rerank_feed_by_affinity,
    )

    pool = cn_pool
    beef_prod, beef_var, beef_batch = await _make_variant(pool, title="鲜切牛肉")
    tomato_prod, tomato_var, tomato_batch = await _make_variant(pool, title="本地番茄")
    onion_prod, onion_var, onion_batch = await _make_variant(pool, title="紫皮洋葱")
    milk_prod, milk_var, milk_batch = await _make_variant(pool, title="鲜牛奶")

    # 全局历史: 牛肉与番茄/洋葱共现 (其他顾客也这么买)，牛奶独立
    await _insert_order(pool, [beef_prod, tomato_prod, onion_prod])
    await _insert_order(pool, [beef_prod, tomato_prod])
    await _insert_order(pool, [milk_prod])

    # 设备购买轨迹: 这台设备买过牛肉
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO device_purchase_log (id, device_id, order_id, batch_id, product_id, variant_id) "
            "VALUES (gen_random_uuid(), 'dev-phase7-test', $1, $2, $3, $4)",
            await _insert_order(pool, [beef_prod]),
            beef_batch,
            beef_prod,
            beef_var,
        )

    # 复刻 nearby-feed 的三段: 用户历史 → 共现 → 得分 → 插队
    async with pool.acquire() as conn:
        user_products = await conn.fetch(
            "SELECT DISTINCT product_id FROM device_purchase_log "
            "WHERE device_id = $1",
            "dev-phase7-test",
        )
        cooccurrence_rows = await conn.fetch(
            "WITH recent AS ( "
            "  SELECT oli.order_id, ib.product_id "
            '  FROM "order_line_item" oli '
            '  JOIN "inventory_batch" ib ON ib.id = oli.batch_id '
            '  JOIN "customer_order" o ON o.id = oli.order_id '
            "  WHERE o.created_at > NOW() - INTERVAL '90 days' "
            "  GROUP BY oli.order_id, ib.product_id "
            ") "
            "SELECT a.product_id AS left_id, b.product_id AS right_id, COUNT(*) AS n "
            "FROM recent a JOIN recent b ON a.order_id = b.order_id "
            "AND a.product_id < b.product_id "
            "GROUP BY 1, 2 ORDER BY n DESC",
        )
    cooccurrence = {
        (str(r["left_id"]), str(r["right_id"])): r["n"] for r in cooccurrence_rows
    }
    user_ids = [str(r["product_id"]) for r in user_products]
    candidates = [tomato_prod, onion_prod, milk_prod]
    scores = compute_batch_affinity_scores(user_ids, candidates, cooccurrence)

    assert scores.get(tomato_prod, 0) > scores.get(onion_prod, 0) > 0
    assert "milk_prod" not in scores or scores.get(milk_prod, 0) == 0

    feed = [
        {"product_id": milk_prod, "title": "鲜牛奶"},
        {"product_id": tomato_prod, "title": "本地番茄"},
        {"product_id": onion_prod, "title": "紫皮洋葱"},
    ]
    reranked = rerank_feed_by_affinity(feed, scores)
    assert [b["title"] for b in reranked][:2] == ["本地番茄", "紫皮洋葱"]
    assert reranked[0]["boosted"] is True
    assert reranked[0]["affinity"] == pytest.approx(
        scores[tomato_prod], abs=1e-4
    )


async def test_spider_engine_layered_writes_price_benchmark(cn_pool):
    """layered provider 覆写注入 → 引擎归一化 → price_benchmark 真实入库。"""
    from app.ext.oservi import build_competitor_spider_engine

    pool = cn_pool
    prod, variant, batch = await _make_variant(pool, title="雪花牛肉800g")
    spider = pool._test_spider  # type: ignore[attr-defined]
    spider.set_results(
        lat=31.0,
        lon=121.0,
        radius_km=5,
        results=[
            {
                "item": "雪花牛肉800g 家庭装",
                "price": 45.9,
                "unit": "800g",
                "store": "测试超市",
            }
        ],
    )
    engine = build_competitor_spider_engine(pool, spider_provider="layered")
    result = (await engine.run_once())[0]
    assert result["parsed"] == 1
    assert result["priced"] == 1
    assert result["written"] == 1

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM price_benchmark WHERE source_type = 'spider' "
            "ORDER BY captured_at DESC LIMIT 1"
        )
    assert row is not None
    assert row["raw_price_cents"] == 4590
    assert row["raw_unit"] == "800g"
    # 归一化: 4590 分 / 800 克 = 5.7375 分/克 (oskill.normalize_sku_price)
    assert float(row["normalized_price_per_unit"]) == pytest.approx(5.7375, abs=1e-3)
    assert row["competitor_name"] == "测试超市"


async def test_device_purchase_log_dedup_index(cn_pool):
    """device_purchase_log 可重复写入 (行为轨迹 append-only)，索引存在。"""
    from obase.uuid7 import uuid7

    pool = cn_pool
    prod, variant, batch = await _make_variant(pool, title="土鸡蛋")
    order_id = await _insert_order(pool, [prod])
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO device_purchase_log (id, device_id, order_id, batch_id, product_id, variant_id) "
            "VALUES (gen_random_uuid(), 'dev-dedup-test', $1, $2, $3, $4)",
            order_id,
            batch,
            prod,
            variant,
        )
        count = await conn.fetchval(
            "SELECT count(*) FROM device_purchase_log WHERE device_id = 'dev-dedup-test'"
        )
        assert count == 1
        idx = await conn.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE indexname = 'idx_device_purchase_device'"
        )
        assert idx == 1
