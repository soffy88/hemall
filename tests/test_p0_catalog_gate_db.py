"""P0-08 商品前台门禁：质检、效期、测试数据与食品资料。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from obase.uuid7 import uuid7

from app import queries

TEST_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


@pytest.fixture
async def catalog_pool():
    from obase.persistence.pool import PgPool

    pool = await PgPool.create(name="hemal_p0_catalog_gate", dsn=TEST_DSN, min_size=1, max_size=3)
    token = uuid7().replace("-", "")
    ids: dict[str, str] = {"token": token}
    try:
        async with pool.acquire() as conn:
            ids["location"] = str(
                await conn.fetchval(
                    """
                    INSERT INTO stock_location (id, name, region_code, status)
                    VALUES ($1, $2, 'cn-east', 'active') RETURNING id
                    """,
                    uuid7(),
                    f"p0gate-loc-{token}",
                )
            )
            for slug in ("fresh", "daily", "drinks"):
                ids[slug] = str(
                    await conn.fetchval(
                        """
                        INSERT INTO product_category (id, name, slug, status)
                        VALUES ($1, $2, $3, 'active') RETURNING id
                        """,
                        uuid7(),
                        f"P0 门禁 {slug} {token}",
                        slug,
                    )
                )
        yield pool, ids
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM inventory_batch
                WHERE variant_id IN (
                    SELECT pv.id FROM product_variant pv
                    JOIN product p ON p.id = pv.product_id
                    WHERE p.slug LIKE $1
                )
                """,
                f"p0gate-%{token}",
            )
            await conn.execute(
                """
                DELETE FROM product_variant
                WHERE product_id IN (SELECT id FROM product WHERE slug LIKE $1)
                """,
                f"p0gate-%{token}",
            )
            await conn.execute("DELETE FROM product WHERE slug LIKE $1", f"p0gate-%{token}%")
            await conn.execute(
                "DELETE FROM product_category WHERE id = ANY($1::uuid[])",
                [ids["fresh"], ids["daily"], ids["drinks"]],
            )
            await conn.execute("DELETE FROM stock_location WHERE name = $1", f"p0gate-loc-{token}")
        await pool.close()


async def _make_product(
    pool,
    ids: dict[str, str],
    *,
    tag: str,
    category_slug: str = "fresh",
    title_prefix: str = "商品",
    is_test: bool = False,
    batch: dict | None = None,
) -> str:
    token = ids["token"]
    async with pool.acquire() as conn:
        product_id = await conn.fetchval(
            """
            INSERT INTO product (id, title, slug, category_id, status, is_test)
            VALUES ($1, $2, $3, $4, 'published', $5)
            RETURNING id
            """,
            uuid7(),
            f"p0gate-{token} {title_prefix} {tag}",
            f"p0gate-{tag}-{token}",
            ids[category_slug],
            is_test,
        )
        variant_id = await conn.fetchval(
            """
            INSERT INTO product_variant (id, product_id, sku_code, status)
            VALUES ($1, $2, $3, 'active') RETURNING id
            """,
            uuid7(),
            product_id,
            f"p0gate-sku-{tag}-{token}",
        )
        if batch is not None:
            now = datetime.now(UTC)
            await conn.execute(
                """
                INSERT INTO inventory_batch (
                    id, batch_no, variant_id, location_id, video_url,
                    cost_price_cents, retail_price_cents, stock_qty,
                    inspection_status, status, expiration_time, origin_region,
                    storage_condition, shelf_life_days, is_test_batch
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'active',$10,$11,$12,$13,$14)
                """,
                uuid7(),
                f"p0gate-batch-{tag}-{token}",
                variant_id,
                ids["location"],
                "https://video.example/p0gate.mp4",
                100,
                200,
                batch.get("stock", 10),
                batch.get("inspection", "passed"),
                batch.get("expiration", now + timedelta(days=7)),
                batch.get("origin", "上海"),
                batch.get("storage", "0-4℃冷藏"),
                batch.get("shelf_life", 7),
                batch.get("is_test_batch", False),
            )
    return str(product_id)


@pytest.mark.asyncio
async def test_storefront_catalog_gate(catalog_pool):
    pool, ids = catalog_pool
    token = ids["token"]

    valid = await _make_product(pool, ids, tag="valid", batch={})
    missing = await _make_product(pool, ids, tag="missing", batch={"origin": None})
    failed = await _make_product(pool, ids, tag="failed", batch={"inspection": "failed"})
    expired = await _make_product(
        pool,
        ids,
        tag="expired",
        batch={"expiration": datetime.now(UTC) - timedelta(hours=1)},
    )
    test_product = await _make_product(pool, ids, tag="test", is_test=True, batch={})
    cmd_product = await _make_product(pool, ids, tag="cmd", title_prefix="CMD-081550", batch={})
    daily = await _make_product(pool, ids, tag="daily", category_slug="daily", batch={"origin": None})
    alcohol = await _make_product(pool, ids, tag="alcohol", category_slug="drinks", batch={})
    zero_stock = await _make_product(pool, ids, tag="preorder", batch={"stock": 0})

    listed = await queries.list_storefront_products(pool, search=token, limit=100)
    listed_ids = {row["id"] for row in listed}

    assert valid in listed_ids
    assert daily in listed_ids
    assert zero_stock in listed_ids
    assert missing not in listed_ids
    assert failed not in listed_ids
    assert expired not in listed_ids
    assert test_product not in listed_ids
    assert cmd_product not in listed_ids
    assert alcohol not in listed_ids

    assert await queries.get_storefront_product(pool, valid) is not None
    assert await queries.get_storefront_product(pool, daily) is not None
    assert await queries.get_storefront_product(pool, missing) is None
    assert await queries.get_storefront_product(pool, failed) is None
    assert await queries.get_storefront_product(pool, expired) is None
    assert await queries.get_storefront_product(pool, test_product) is None
    assert await queries.get_storefront_product(pool, cmd_product) is None
    assert await queries.get_storefront_product(pool, alcohol) is None

    alcohol_listed = await queries.list_storefront_products(
        pool, search=token, include_alcohol=True, limit=100
    )
    assert alcohol in {row["id"] for row in alcohol_listed}
    assert await queries.get_storefront_product(pool, alcohol, include_alcohol=True) is not None
