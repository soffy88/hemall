"""core3 店员拍照换商品图 / 拍照上新。

纯函数部分（压缩、EXIF、类型、大小、相机文件名）不依赖数据库；
需要 TEST_PG_DSN 的用例验证相册、换主图、删除恢复与目标换图。
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from starlette.datastructures import Headers

from app import product_photo as pp
from app.ext import agent_gateway

TEST_DSN = os.environ.get("TEST_PG_DSN")


def _jpeg(*, size=(2400, 1600), exif: bytes | None = None) -> bytes:
    im = Image.new("RGB", size, (30, 120, 60))
    buf = io.BytesIO()
    kwargs = {"format": "JPEG", "quality": 90}
    if exif:
        kwargs["exif"] = exif
    im.save(buf, **kwargs)
    return buf.getvalue()


def _upload(data: bytes, name: str = "IMG_1234.jpg", ctype: str = "image/jpeg") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data), filename=name, headers=Headers({"content-type": ctype})
    )


class TestProcessImage:
    def test_resizes_and_strips_exif(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # Orientation
        exif[0x8825] = {1: "N"}  # GPSInfo
        raw = _jpeg(exif=exif.tobytes())
        out = pp.process_image(raw)
        assert max(out.width, out.height) <= pp.MAX_EDGE
        # EXIF orientation=6 会把 2400x1600 摆正成 1600x2400, 长边压到 1200
        assert max(out.width, out.height) == pp.MAX_EDGE
        with Image.open(io.BytesIO(out.main)) as im:
            assert im.format == "JPEG"
            assert dict(im.getexif()) == {}
        with Image.open(io.BytesIO(out.thumb)) as th:
            assert max(th.size) <= pp.THUMB_EDGE

    def test_rejects_non_image(self):
        with pytest.raises(ValueError):
            pp.process_image(b"not an image")

    def test_rejects_gif(self):
        buf = io.BytesIO()
        Image.new("RGB", (10, 10)).save(buf, format="GIF")
        with pytest.raises(ValueError, match="unsupported"):
            pp.process_image(buf.getvalue())

    def test_rejects_over_10mb(self):
        with pytest.raises(ValueError, match="too large"):
            pp.process_image(b"x" * (pp.MAX_UPLOAD_BYTES + 1))

    def test_hash_is_content_addressed(self):
        raw = _jpeg()
        a = pp.process_image(raw)
        b = pp.process_image(raw)
        assert a.sha256 == b.sha256


class TestCameraFilenames:
    @pytest.mark.parametrize(
        "name",
        [
            "IMG_1234.HEIC",
            "DSC_0001.jpg",
            "PXL_20261110.jpg",
            "微信图片_2026.jpg",
            "Screenshot_1.png",
        ],
    )
    def test_camera_like(self, name):
        assert agent_gateway._CAMERA_FILENAME_RE.match(Path(name).stem)

    @pytest.mark.parametrize("name", ["丹东草莓实拍.jpg", "上海青.png", "有机土豆.jpg"])
    def test_product_like(self, name):
        assert not agent_gateway._CAMERA_FILENAME_RE.match(Path(name).stem)

    def test_heic_extensions_allowed(self):
        for name in ("IMG_1.heic", "IMG_2.heif", "IMG_3.avif"):
            assert agent_gateway._sanitize_upload_filename(name)


@pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")
class TestPhotoStore:
    @pytest.fixture
    async def pool(self):
        from obase.persistence.pool import PgPool
        from obase.uuid7 import uuid7

        token = uuid7().replace("-", "")
        p = await PgPool.create(
            name=f"he_photo_{token[-12:]}", dsn=TEST_DSN, min_size=1, max_size=3
        )
        try:
            yield p, token
        finally:
            await p.close()

    async def _seed(self, pool, token):
        from obase.uuid7 import uuid7

        async with pool.acquire() as conn:
            cat = await conn.fetchval(
                "INSERT INTO product_category (id, name, slug, status) "
                "VALUES ($1,$2,$3,'active') RETURNING id",
                uuid7(),
                f"拍照类目{token}",
                f"photo-cat-{token}",
            )
            product = await conn.fetchval(
                "INSERT INTO product (id,title,slug,category_id,status) "
                "VALUES ($1,$2,$3,$4,'published') RETURNING id",
                uuid7(),
                f"拍照商品{token}",
                f"photo-{token}",
                cat,
            )
            variant = await conn.fetchval(
                "INSERT INTO product_variant (id,product_id,sku_code,option_values,"
                "reference_price_cents,status) VALUES ($1,$2,$3,'{}'::jsonb,300,'active') "
                "RETURNING id",
                uuid7(),
                product,
                f"PH-{uuid7().replace('-', '')[-12:]}",
            )
            loc = await conn.fetchval(
                "SELECT id FROM stock_location WHERE status='active' ORDER BY created_at LIMIT 1"
            )
            if loc is None:
                loc = await conn.fetchval(
                    "INSERT INTO stock_location (id,name,status) "
                    "VALUES (gen_random_uuid(),$1,'active') RETURNING id",
                    f"拍照库位{token}",
                )
            batch = await conn.fetchval(
                "INSERT INTO inventory_batch (id,batch_no,variant_id,location_id,video_url,"
                "media_assets,cost_price_cents,retail_price_cents,stock_qty,shelf_image_url) "
                "VALUES (gen_random_uuid(),$1,$2,$3,'','[]'::jsonb,100,300,10,$4) RETURNING id",
                f"STORE-{uuid7().replace('-', '')[-12:]}",
                variant,
                loc,
                "/media/spider/ref.jpg",
            )
        return SimpleNamespace(product=str(product), variant=str(variant), batch=str(batch))

    async def test_upload_replaces_and_restores(self, pool, tmp_path):
        p, token = pool
        seeded = await self._seed(p, token)
        photo = await pp.add_photo(
            p,
            output_root=tmp_path,
            product_id=seeded.product,
            variant_id=seeded.variant,
            file=_upload(_jpeg()),
            make_primary=True,
            actor="staff@test",
        )
        assert photo["is_primary"] is True
        assert photo["url"].startswith("/media/product_photo/")
        async with p.acquire() as conn:
            shelf = await conn.fetchval(
                "SELECT shelf_image_url FROM inventory_batch WHERE id=$1", seeded.batch
            )
        assert shelf == photo["url"]
        assert photo["previous_images"][seeded.batch] == "/media/spider/ref.jpg"
        rel = photo["url"].removeprefix("/media/product_photo/")
        assert (tmp_path / "product_photo" / rel).is_file()

        second = await pp.add_photo(
            p,
            output_root=tmp_path,
            product_id=seeded.product,
            variant_id=seeded.variant,
            file=_upload(_jpeg(size=(800, 600))),
            make_primary=True,
            actor="staff@test",
        )
        assert second["is_primary"] is True
        gone = await pp.delete_photo(p, product_id=seeded.product, photo_id=second["id"])
        assert gone["status"] == "deleted"
        async with p.acquire() as conn:
            shelf = await conn.fetchval(
                "SELECT shelf_image_url FROM inventory_batch WHERE id=$1", seeded.batch
            )
        # 删主图时下一张顶上来
        assert shelf == photo["url"]
        # 再删最后一张, 封面还原到换图前的参考图 (旧文件一直保留)
        await pp.delete_photo(p, product_id=seeded.product, photo_id=photo["id"])
        async with p.acquire() as conn:
            shelf = await conn.fetchval(
                "SELECT shelf_image_url FROM inventory_batch WHERE id=$1", seeded.batch
            )
        assert shelf == "/media/spider/ref.jpg"
        restored = await pp.restore_photo(p, product_id=seeded.product, photo_id=second["id"])
        assert restored["deleted_at"] is None

    async def test_upload_rejects_bad_type_and_size(self, pool, tmp_path):
        p, token = pool
        seeded = await self._seed(p, token)
        with pytest.raises(HTTPException) as exc:
            await pp.add_photo(
                p,
                output_root=tmp_path,
                product_id=seeded.product,
                variant_id=None,
                file=_upload(b"x" * (pp.MAX_UPLOAD_BYTES + 1)),
                make_primary=True,
                actor="staff@test",
            )
        assert exc.value.status_code == 413
        with pytest.raises(HTTPException) as exc:
            await pp.add_photo(
                p,
                output_root=tmp_path,
                product_id=seeded.product,
                variant_id=None,
                file=_upload(b"nope", name="a.png"),
                make_primary=True,
                actor="staff@test",
            )
        assert exc.value.status_code == 400

    async def test_reference_products_and_progress(self, pool, tmp_path):
        p, token = pool
        await self._seed(p, token)
        res = await pp.reference_products(p, scope="all", limit=5, offset=0)
        assert any(i["id"] for i in res["items"])
        assert res["progress"]["total"] >= res["progress"]["replaced"]


@pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")
class TestIngestGaps:
    @pytest.fixture
    async def pool(self):
        from obase.persistence.pool import PgPool
        from obase.uuid7 import uuid7

        token = uuid7().replace("-", "")
        p = await PgPool.create(
            name=f"he_ingest3_{token[-12:]}", dsn=TEST_DSN, min_size=1, max_size=3
        )
        try:
            yield p, token
        finally:
            await p.close()

    async def _seed(self, pool, token):
        from obase.uuid7 import uuid7

        async with pool.acquire() as conn:
            cat = await conn.fetchval(
                "INSERT INTO product_category (id, name, slug, status) "
                "VALUES ($1,$2,$3,'active') RETURNING id",
                uuid7(),
                f"传货类目{token}",
                f"ingest-cat-{token}",
            )
            product = await conn.fetchval(
                "INSERT INTO product (id,title,slug,category_id,status) "
                "VALUES ($1,$2,$3,$4,'published') RETURNING id",
                uuid7(),
                f"传货商品{token}",
                f"ingest-{token}",
                cat,
            )
            variant = await conn.fetchval(
                "INSERT INTO product_variant (id,product_id,sku_code,option_values,"
                "reference_price_cents,status) VALUES ($1,$2,$3,'{}'::jsonb,300,'active') "
                "RETURNING id",
                uuid7(),
                product,
                f"IG-{uuid7().replace('-', '')[-12:]}",
            )
            loc = await conn.fetchval(
                "SELECT id FROM stock_location WHERE status='active' ORDER BY created_at LIMIT 1"
            )
            if loc is None:
                loc = await conn.fetchval(
                    "INSERT INTO stock_location (id,name,status) "
                    "VALUES (gen_random_uuid(),$1,'active') RETURNING id",
                    f"传货库位{token}",
                )
            batch = await conn.fetchval(
                "INSERT INTO inventory_batch (id,batch_no,variant_id,location_id,video_url,"
                "media_assets,cost_price_cents,retail_price_cents,stock_qty,shelf_image_url) "
                "VALUES (gen_random_uuid(),$1,$2,$3,'','[]'::jsonb,100,300,10,$4) RETURNING id",
                f"STORE-{uuid7().replace('-', '')[-12:]}",
                variant,
                loc,
                "/media/spider/ref.jpg",
            )
        return SimpleNamespace(
            product=str(product), variant=str(variant), batch=str(batch), loc=str(loc)
        )

    async def test_target_updates_image_only(self, pool, tmp_path, monkeypatch):
        p, token = pool
        seeded = await self._seed(p, token)
        monkeypatch.setattr(
            agent_gateway,
            "_identify_uploaded_image",
            lambda *_a, **_k: _none_async(),
        )
        before = await _count(p, "product")
        res = await agent_gateway.ingest_media(
            _upload(_jpeg(), name="IMG_9999.jpg"),
            location_id="",
            pool=p,
            out_root=tmp_path,
            principal={"user_id": "staff-1"},
            target_product_id=seeded.product,
            can_edit_any=True,
            actor_ref="staff-1",
        )
        assert res["status"] == "image_updated"
        assert await _count(p, "product") == before
        async with p.acquire() as conn:
            shelf = await conn.fetchval(
                "SELECT shelf_image_url FROM inventory_batch WHERE id=$1", seeded.batch
            )
            photos = await conn.fetchval(
                "SELECT COUNT(*) FROM product_photo WHERE product_id=$1", seeded.product
            )
        assert shelf.startswith("/media/agent_ingest/staff-1/")
        assert photos == 1

    async def test_customer_cannot_overwrite_foreign_image(self, pool, tmp_path, monkeypatch):
        p, token = pool
        seeded = await self._seed(p, token)
        monkeypatch.setattr(
            agent_gateway,
            "_identify_uploaded_image",
            lambda *_a, **_k: _none_async(),
        )
        with pytest.raises(HTTPException) as exc:
            await agent_gateway.ingest_media(
                _upload(_jpeg(), name="IMG_8888.jpg"),
                location_id="",
                pool=p,
                out_root=tmp_path,
                principal={"user_id": "c_1"},
                target_product_id=seeded.product,
                can_edit_any=False,
                owner_ref="c_1",
                actor_ref="c_1",
            )
        assert exc.value.status_code == 403

    async def test_camera_filename_without_vlm_creates_draft(self, pool, tmp_path, monkeypatch):
        p, token = pool
        await self._seed(p, token)
        monkeypatch.setattr(
            agent_gateway,
            "_identify_uploaded_image",
            lambda *_a, **_k: _none_async(),
        )
        res = await agent_gateway.ingest_media(
            _upload(_jpeg(), name="IMG_7777.jpg"),
            location_id="",
            pool=p,
            out_root=tmp_path,
            principal={"user_id": "staff-2"},
            actor_ref="staff-2",
        )
        assert res["status"] == "draft"
        assert res["requires_name"] is True
        async with p.acquire() as conn:
            status = await conn.fetchval(
                "SELECT status FROM product WHERE id=$1", res["product_id"]
            )
        assert status == "draft"


async def _none_async():
    return None, "ollama"


async def _count(pool, table: str) -> int:
    async with pool.acquire() as conn:
        return int(await conn.fetchval(f"SELECT COUNT(*) FROM {table}"))
