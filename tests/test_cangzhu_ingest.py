"""仓主传货 upsert 集成测试 — 需真实 Postgres (TEST_PG_DSN)。

覆盖（deterministic_match + update_in_place，VLM mock 为不可用）：
- 首传新建：product + variant + batch，默认价/库存口径
- 同名再传：不建新品，原地更新最新批次（图/价/库存），batch 数不变
- 未提供价格/库存时原地更新只换图，价/库存保持不变
- 重复上传不建新品（product 数不变）
- 视频同名再传：命中同一商品，video_url 更新，batch 数不变
- 不同名：新建
"""

from __future__ import annotations

import base64

import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import UploadFile

TEST_DSN = os.environ.get("TEST_PG_DSN")


_TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAIAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCGiiivhz84P//Z"
)


@pytest.fixture
async def cn_pool():
    from obase.notification_providers import LogNotificationProvider
    from obase.payment_providers import ManualPaymentProvider
    from obase.persistence.pool import PgPool
    from obase.provider_registry import ProviderRegistry

    from app.ext.payout_provider import ManualPayoutProvider
    from app.ext.schema import ensure_ext_schema

    reg = ProviderRegistry.get()
    reg.register_generic("payment", "manual", ManualPaymentProvider(), replace=True)
    reg.register_generic("payout", "manual", ManualPayoutProvider(), replace=True)
    log_notify = LogNotificationProvider()
    reg.register_generic("notification", "log", log_notify, replace=True)

    pool = await PgPool.create(
        name="hemall_cangzhu_ingest_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await ensure_ext_schema(pool)
    yield pool
    await pool.close()


@pytest.fixture
async def location_id(cn_pool):
    from obase.uuid7 import uuid7

    async with cn_pool.acquire() as conn:
        loc_id = await conn.fetchval(
            (
                "INSERT INTO stock_location (id, name, region_code, host_id, address, "
                "lat, lng, status) "
                "VALUES ($1,'test_loc','cn-east','host_test','测试仓库', "
                "31.0,121.0,'active') "
                "RETURNING id"
            ),
            uuid7(),
        )
    return str(loc_id)


@pytest.fixture(autouse=True)
def _no_vlm(monkeypatch: pytest.MonkeyPatch):
    """VLM 识别固定不可用，隔离确定性匹配路径。"""

    async def _unavailable(image_bytes: bytes):
        return None, "ollama"

    monkeypatch.setattr(
        "app.ext.agent_gateway._identify_uploaded_image",
        _unavailable,
    )


def _upload(name: str, content: bytes, content_type: str):
    file = MagicMock(spec=UploadFile)
    file.filename = name
    file.content_type = content_type
    file.read = AsyncMock(return_value=content)
    return file


async def _ingest(cn_pool, out_root, location_id, name, content, content_type, **kwargs):
    from app.ext.agent_gateway import ingest_media

    return await ingest_media(
        _upload(name, content, content_type),
        location_id=location_id,
        pool=cn_pool,
        out_root=out_root,
        principal={"user_id": "cangzhu-test"},
        deterministic_match=True,
        update_in_place=True,
        actor_ref="c_test-actor",
        **kwargs,
    )


async def _product_count(cn_pool, title: str) -> int:
    async with cn_pool.acquire() as conn:
        return await conn.fetchval(
            'SELECT COUNT(*) FROM "product" WHERE title = $1 AND deleted_at IS NULL',
            title,
        )


async def _batch_of(cn_pool, product_id: str):
    async with cn_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT ib.id, ib.retail_price_cents, ib.cost_price_cents, ib.stock_qty, "
            'ib.shelf_image_url, ib.video_url FROM "inventory_batch" ib '
            'JOIN "product_variant" pv ON pv.id = ib.variant_id '
            "WHERE pv.product_id = $1 AND ib.deleted_at IS NULL "
            "ORDER BY ib.created_at",
            product_id,
        )


@pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")
class TestCangzhuIngestUpsert:
    async def test_create_then_update_in_place(self, cn_pool, location_id, tmp_path: Path) -> None:
        token = uuid.uuid4().hex[:8]
        title = f"仓主测试苹果{token}号"
        out_root = tmp_path / "output"

        first = await _ingest(
            cn_pool,
            out_root,
            location_id,
            f"仓主测试苹果{token}号-1.jpg",
            _TINY_JPEG,
            "image/jpeg",
            retail_price_cents=1999,
            stock_qty=50,
        )
        assert first["status"] == "created"
        assert first["title"] == title
        assert first["retail_price_cents"] == 1999
        assert first["stock_qty"] == 50
        pid = first["product_id"]

        batches = await _batch_of(cn_pool, pid)
        assert len(batches) == 1
        first_image = batches[0]["shelf_image_url"]

        second = await _ingest(
            cn_pool,
            out_root,
            location_id,
            f"仓主测试苹果{token}号-2.jpg",
            _TINY_JPEG,
            "image/jpeg",
            retail_price_cents=2500,
            stock_qty=40,
        )
        assert second["status"] == "updated"
        assert second["product_id"] == pid
        assert second["batch_action"] == "updated"
        assert set(second["updated_fields"]) >= {
            "shelf_image_url",
            "media_assets",
            "retail_price_cents",
            "cost_price_cents",
            "stock_qty",
        }
        assert second["retail_price_cents"] == 2500
        assert second["cost_price_cents"] == 1500
        assert second["stock_qty"] == 40
        assert await _product_count(cn_pool, title) == 1

        batches = await _batch_of(cn_pool, pid)
        assert len(batches) == 1
        assert batches[0]["retail_price_cents"] == 2500
        assert batches[0]["stock_qty"] == 40
        assert batches[0]["shelf_image_url"] != first_image

    async def test_update_keeps_price_and_stock_when_not_provided(
        self, cn_pool, location_id, tmp_path: Path
    ) -> None:
        token = uuid.uuid4().hex[:8]
        out_root = tmp_path / "output"

        first = await _ingest(
            cn_pool,
            out_root,
            location_id,
            f"仓主测试梨{token}号-1.jpg",
            _TINY_JPEG,
            "image/jpeg",
            retail_price_cents=1800,
            stock_qty=25,
        )
        assert first["status"] == "created"
        pid = first["product_id"]

        second = await _ingest(
            cn_pool,
            out_root,
            location_id,
            f"仓主测试梨{token}号-2.jpg",
            _TINY_JPEG,
            "image/jpeg",
        )
        assert second["status"] == "updated"
        assert second["batch_action"] == "updated"
        assert second["retail_price_cents"] == 1800
        assert second["stock_qty"] == 25
        assert set(second["updated_fields"]) == {"shelf_image_url", "media_assets"}

        batches = await _batch_of(cn_pool, pid)
        assert len(batches) == 1
        assert batches[0]["retail_price_cents"] == 1800
        assert batches[0]["stock_qty"] == 25

    async def test_duplicate_upload_creates_no_new_product(
        self, cn_pool, location_id, tmp_path: Path
    ) -> None:
        token = uuid.uuid4().hex[:8]
        title = f"仓主测试橙{token}号"
        out_root = tmp_path / "output"

        for i in range(3):
            result = await _ingest(
                cn_pool,
                out_root,
                location_id,
                f"仓主测试橙{token}号-{i + 1}.jpg",
                _TINY_JPEG,
                "image/jpeg",
            )
            assert result["status"] in ("created", "updated")
        assert await _product_count(cn_pool, title) == 1

    async def test_video_update_sets_video_url(self, cn_pool, location_id, tmp_path: Path) -> None:
        token = uuid.uuid4().hex[:8]
        out_root = tmp_path / "output"

        first = await _ingest(
            cn_pool,
            out_root,
            location_id,
            f"仓主测试瓜{token}号-1.jpg",
            _TINY_JPEG,
            "image/jpeg",
            retail_price_cents=1200,
            stock_qty=10,
        )
        assert first["status"] == "created"
        pid = first["product_id"]

        second = await _ingest(
            cn_pool,
            out_root,
            location_id,
            f"仓主测试瓜{token}号-1.mp4",
            b"melon-video",
            "video/mp4",
        )
        assert second["status"] == "updated"
        assert second["product_id"] == pid
        assert "video_url" in second["updated_fields"]

        batches = await _batch_of(cn_pool, pid)
        assert len(batches) == 1
        assert batches[0]["video_url"]
        assert batches[0]["retail_price_cents"] == 1200

    async def test_different_title_creates_new_product(
        self, cn_pool, location_id, tmp_path: Path
    ) -> None:
        token = uuid.uuid4().hex[:8]
        out_root = tmp_path / "output"
        first = await _ingest(
            cn_pool,
            out_root,
            location_id,
            f"仓主测试桃{token}号-1.jpg",
            _TINY_JPEG,
            "image/jpeg",
        )
        second = await _ingest(
            cn_pool,
            out_root,
            location_id,
            f"仓主测试杏{token}号-1.jpg",
            _TINY_JPEG,
            "image/jpeg",
        )
        assert first["status"] == "created"
        assert second["status"] == "created"
        assert first["product_id"] != second["product_id"]
