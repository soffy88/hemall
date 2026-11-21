"""tests/test_counter_vision.py — 柜台视觉识别接线：纯函数 + 端点 + 评估账本。

覆盖：
- 纯函数：图像魔数判定、SKU 白名单、候选校验、空识别器/超时/抛错转空候选、
  manifest 追加与损坏重建。
- 主干 HTTP：无 token 401（四端点）。
- DB 集成 (TEST_PG_DSN)：识别空候选写账本、帧采集 multipart/base64/幂等/关联
  校验、数据集上传与 manifest、确认口子、非商家 403。

状态口径：模型未训——识别端点只验证“空候选 + 账本”行为，绝不伪造识别结果。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.counter_vision import (
    append_manifest_entry,
    build_manifest_entry,
    detect_image_format,
    run_recognition,
    sanitize_sku,
    set_recognizer,
    validate_candidates,
    validate_image_bytes,
)

TEST_DSN = os.environ.get("TEST_PG_DSN")

DB_TESTS = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")

# 1x1 PNG（魔数 + 合法结构），与一张最小 JPEG 头（仅魔数判定用）。
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
JPEG_MIN = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


# ── 纯函数 ─────────────────────────────────────────────────────────────────


class TestDetectImageFormat:
    def test_jpeg_png_webp(self):
        assert detect_image_format(JPEG_MIN) == ("jpeg", "jpg")
        assert detect_image_format(PNG_1X1) == ("png", "png")
        assert detect_image_format(b"RIFF\x24\x00\x00\x00WEBPVP8 ") == ("webp", "webp")

    def test_unknown_is_none(self):
        assert detect_image_format(b"GIF89a") is None
        assert detect_image_format(b"hello world") is None
        assert detect_image_format(b"") is None


class TestValidateImageBytes:
    def test_valid(self):
        assert validate_image_bytes(PNG_1X1) == ("png", "png")
        assert validate_image_bytes(JPEG_MIN) == ("jpeg", "jpg")

    def test_empty_and_oversized(self):
        with pytest.raises(ValueError):
            validate_image_bytes(b"")
        with pytest.raises(ValueError):
            validate_image_bytes(b"\xff\xd8\xff" + b"\x00" * 10, max_bytes=5)

    def test_unsupported(self):
        with pytest.raises(ValueError):
            validate_image_bytes(b"GIF89a" + b"\x00" * 10)


class TestSanitizeSku:
    def test_valid(self):
        assert sanitize_sku("qingcai-01") == "qingcai-01"
        assert sanitize_sku("  SKU_2.5 ") == "SKU_2.5"

    def test_rejects_traversal_and_empty(self):
        for bad in ["../evil", "a/b", "a\\b", "", "  ", ".hidden", "-lead"]:
            with pytest.raises(ValueError):
                sanitize_sku(bad)


class TestValidateCandidates:
    def test_sorts_caps_and_dedupes(self):
        v1, v2, v3, v4 = (str(uuid.uuid4()) for _ in range(4))
        raw = [
            {"variant_id": v2, "score": 0.5},
            {"variant_id": v1, "score": 0.9},
            {"variant_id": v2, "score": 0.7},
            {"variant_id": v3, "score": 0.1},
            {"variant_id": v4, "score": 0.8},
        ]
        got = validate_candidates(raw)
        assert [c["variant_id"] for c in got] == [v1, v4, v2]
        assert got[0]["score"] == 0.9

    def test_drops_invalid(self):
        raw = [
            {"variant_id": "not-a-uuid", "score": 0.9},
            {"variant_id": str(uuid.uuid4()), "score": 1.5},
            {"variant_id": str(uuid.uuid4()), "score": float("nan")},
            {"variant_id": str(uuid.uuid4()), "score": "high"},
            {"score": 0.5},
            "junk",
        ]
        assert validate_candidates(raw) == []
        assert validate_candidates("junk") == []
        assert validate_candidates(None) == []


class TestRunRecognition:
    async def test_null_recognizer_returns_empty_unconfigured(self):
        set_recognizer(None)
        from app.counter_vision import get_recognizer

        candidates, configured = await run_recognition(get_recognizer(), PNG_1X1, 500)
        assert candidates == []
        assert configured is False

    async def test_slow_recognizer_times_out_to_empty(self):
        class Slow:
            name = "slow"
            configured = True

            async def recognize(self, image_bytes, weight_grams):
                await asyncio.sleep(0.2)
                return [{"variant_id": str(uuid.uuid4()), "score": 0.9}]

        candidates, configured = await run_recognition(Slow(), PNG_1X1, 500, timeout_seconds=0.05)
        assert candidates == []
        assert configured is True

    async def test_raising_recognizer_to_empty(self):
        class Boom:
            name = "boom"
            configured = True

            async def recognize(self, image_bytes, weight_grams):
                raise RuntimeError("model down")

        candidates, configured = await run_recognition(Boom(), PNG_1X1, 500)
        assert candidates == []
        assert configured is True


class TestManifest:
    def test_append_and_roundtrip(self, tmp_path):
        manifest = tmp_path / "manifest.json"
        entry = build_manifest_entry(
            filename="f1.jpg",
            sku="qingcai",
            variant_id=None,
            captured_at="2026-11-03T08:00:00+08:00",
            photographer="dian",
            note="",
            uploaded_by="u1",
            sha256="abc",
            byte_size=12,
        )
        entries = append_manifest_entry(manifest, entry)
        assert len(entries) == 1
        assert json.loads(manifest.read_text(encoding="utf-8"))[0]["sku"] == "qingcai"
        entries = append_manifest_entry(manifest, {**entry, "file": "f2.jpg"})
        assert len(entries) == 2

    def test_corrupt_manifest_backed_up_and_rebuilt(self, tmp_path):
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{broken", encoding="utf-8")
        entries = append_manifest_entry(manifest, {"file": "f1.jpg"})
        assert len(entries) == 1
        assert list(tmp_path.glob("manifest.corrupt.*.json"))


# ── 主干 HTTP：无 token 401（无需 DB）──────────────────────────────────────


class TestVisionAuth:
    @pytest.mark.parametrize(
        "method,url",
        [
            ("post", "/store/counter/recognize"),
            ("post", "/store/counter/frames"),
            ("post", "/store/counter/dataset-photos"),
            ("post", "/store/counter/recognitions/00000000-0000-0000-0000-000000000000/confirm"),
        ],
    )
    def test_missing_token_401(self, client, method, url):
        r = getattr(client, method)(url, json={})
        assert r.status_code == 401


# ── DB 集成 ────────────────────────────────────────────────────────────────


@pytest.fixture
async def vision_client(monkeypatch, tmp_path):
    """真实 DB + 商家主体的 TestClient；文件写进 tmp_path（HEMALL_OUTPUT_ROOT）。

    用 dependency_overrides 注入顾客主体（JWT 签发细节不属于本测试范围），
    商家门禁（is_merchant 查库）仍走真实逻辑。直连池与 TestClient 各用各的
    连接（同一库），不跨 loop 共用连接池。
    """
    from db_test_utils import cleanup_db_test_rows
    from obase.persistence.pool import PgPool

    from app.deps import get_current_customer, get_settings
    from app.ext.schema import ensure_ext_schema
    from app.p0_schema import ensure_p0_schema

    pool = await PgPool.create(name="hemall_vision_test", dsn=TEST_DSN, min_size=1, max_size=5)
    await cleanup_db_test_rows(pool)
    await ensure_p0_schema(pool)
    await ensure_ext_schema(pool)

    monkeypatch.setenv("HEMALL_PG_DSN", TEST_DSN)
    monkeypatch.setenv("HEMALL_OUTPUT_ROOT", str(tmp_path))
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    state: dict = {
        "customers": [],
        "products": [],
        "variants": [],
        "batches": [],
        "locations": [],
        "tickets": [],
        "frames": [],
        "recognitions": [],
    }
    merchant_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO "customer" (id, email, status, is_merchant, created_at, updated_at)'
            " VALUES ($1, $2, 'active', true, NOW(), NOW())",
            merchant_id,
            f"cv-merchant-{uuid.uuid4().hex[:8]}@test.dev",
        )
    state["customers"].append(merchant_id)

    with TestClient(app) as c:
        db_health = c.get("/health/ready").json()["checks"]["database"]["status"]
        if db_health != "up":
            pytest.skip("database not reachable at TEST_PG_DSN")
        app.dependency_overrides[get_current_customer] = lambda: {
            "customer_id": merchant_id,
            "email": "merchant@test.dev",
        }
        yield SimpleNamespace(client=c, state=state, pool=pool)

    app.dependency_overrides.clear()
    async with pool.acquire() as conn:
        if state["recognitions"]:
            await conn.execute(
                'DELETE FROM "counter_recognition_event" WHERE id = ANY($1::uuid[])',
                state["recognitions"],
            )
        if state["frames"]:
            await conn.execute(
                'DELETE FROM "counter_frame" WHERE id = ANY($1::uuid[])', state["frames"]
            )
        if state["tickets"]:
            await conn.execute(
                'DELETE FROM "hardware_event" WHERE ticket_id = ANY($1::uuid[])',
                state["tickets"],
            )
            await conn.execute(
                'DELETE FROM "counter_ticket" WHERE id = ANY($1::uuid[])', state["tickets"]
            )
        await conn.execute("DELETE FROM \"hardware_event\" WHERE message_id LIKE 'cvtest-%'")
        if state["batches"]:
            await conn.execute(
                "DELETE FROM inventory_batch WHERE id = ANY($1::uuid[])", state["batches"]
            )
        if state["variants"]:
            await conn.execute(
                "DELETE FROM product_variant WHERE id = ANY($1::uuid[])", state["variants"]
            )
        if state["products"]:
            await conn.execute("DELETE FROM product WHERE id = ANY($1::uuid[])", state["products"])
        if state["locations"]:
            await conn.execute(
                "DELETE FROM stock_location WHERE id = ANY($1::uuid[])", state["locations"]
            )
        if state["customers"]:
            await conn.execute(
                'DELETE FROM "customer" WHERE id = ANY($1::uuid[])', state["customers"]
            )
    await pool.close()
    get_settings.cache_clear()


async def _seed_variant(pool, state) -> str:
    """种一个最小变体（确认口子的 variant 存在性校验用），返回 variant_id。"""
    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, "
            "lat, lng, status) VALUES ($1, 'cv_loc', 'cn-east', 'host_cv', '柜台', "
            "31.23, 121.47, 'active') RETURNING id",
            uuid7(),
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,$2,$3,'published') "
            "RETURNING id",
            uuid7(),
            f"识别测试品{uuid.uuid4().hex[:6]}",
            f"cv-slug-{uuid.uuid4().hex[:8]}",
        )
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, option_values, "
            "reference_price_cents, status) VALUES ($1,$2,$3,$4,$5,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"CV-{uuid.uuid4().hex[:6]}",
            '{"规格": "500g/份"}',
            419,
        )
    state["locations"].append(str(loc_id))
    state["products"].append(str(prod_id))
    state["variants"].append(str(var_id))
    return str(var_id)


@DB_TESTS
class TestRecognizeDb:
    def test_recognize_empty_candidates_writes_ledger(self, vision_client, tmp_path):
        c = vision_client.client
        r = c.post(
            "/store/counter/recognize",
            files={"file": ("frame.png", PNG_1X1, "image/png")},
            data={"weight_grams": "500"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["candidates"] == []
        assert body["model_configured"] is False
        assert body["weight_grams"] == 500
        vision_client.state["recognitions"].append(body["recognition_id"])
        vision_client.state["frames"].append(body["frame_id"])
        assert len(list(Path(tmp_path).rglob("*.png"))) == 1

    def test_recognize_json_base64(self, vision_client):
        r = vision_client.client.post(
            "/store/counter/recognize",
            json={
                "image_base64": base64.b64encode(JPEG_MIN).decode(),
                "weight_grams": 350,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["candidates"] == []
        vision_client.state["recognitions"].append(body["recognition_id"])
        vision_client.state["frames"].append(body["frame_id"])

    def test_recognize_rejects_non_image(self, vision_client):
        r = vision_client.client.post(
            "/store/counter/recognize",
            files={"file": ("x.txt", b"not an image", "text/plain")},
            data={"weight_grams": "500"},
        )
        assert r.status_code == 400

    def test_recognize_requires_weight(self, vision_client):
        c = vision_client.client
        files = {"file": ("frame.png", PNG_1X1, "image/png")}
        assert c.post("/store/counter/recognize", files=files, data={}).status_code == 400
        assert (
            c.post("/store/counter/recognize", files=files, data={"weight_grams": "0"}).status_code
            == 400
        )


@DB_TESTS
class TestFramesDb:
    def test_frames_multipart_and_idempotent_replay(self, vision_client):
        c = vision_client.client
        frame_id = f"cam-{uuid.uuid4().hex}"
        files = {"file": ("frame.png", PNG_1X1, "image/png")}
        data = {"camera_id": "cam-01", "client_frame_id": frame_id}
        r = c.post("/store/counter/frames", files=files, data=data)
        assert r.status_code == 200, r.text
        first = r.json()
        assert first["duplicate"] is False
        vision_client.state["frames"].append(first["frame_id"])
        r2 = c.post("/store/counter/frames", files=files, data=data)
        assert r2.status_code == 200, r2.text
        second = r2.json()
        assert second["duplicate"] is True
        assert second["frame_id"] == first["frame_id"]

    def test_frames_unknown_ticket_404(self, vision_client):
        r = vision_client.client.post(
            "/store/counter/frames",
            files={"file": ("frame.png", PNG_1X1, "image/png")},
            data={"ticket_id": str(uuid.uuid4())},
        )
        assert r.status_code == 404

    def test_frames_unknown_weigh_message_404(self, vision_client):
        r = vision_client.client.post(
            "/store/counter/frames",
            json={
                "image_base64": base64.b64encode(PNG_1X1).decode(),
                "weigh_message_id": "cvtest-no-such-event",
            },
        )
        assert r.status_code == 404


@DB_TESTS
class TestDatasetDb:
    def test_dataset_upload_writes_manifest(self, vision_client, tmp_path):
        r = vision_client.client.post(
            "/store/counter/dataset-photos",
            files={"file": ("leaf.jpg", JPEG_MIN, "image/jpeg")},
            data={"sku": "qingcai", "photographer": "dian", "note": "摊开"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["stored"] is True
        assert body["sku"] == "qingcai"
        assert body["manifest"]["photographer"] == "dian"
        assert (tmp_path / body["path"]).exists()
        manifest = json.loads(
            (tmp_path / "counter_dataset" / "qingcai" / "manifest.json").read_text()
        )
        assert manifest[0]["file"] == body["file"]

    def test_dataset_rejects_non_image_and_traversal(self, vision_client):
        c = vision_client.client
        r = c.post(
            "/store/counter/dataset-photos",
            files={"file": ("x.txt", b"junk", "text/plain")},
            data={"sku": "qingcai"},
        )
        assert r.status_code == 400
        r = c.post(
            "/store/counter/dataset-photos",
            files={"file": ("leaf.jpg", JPEG_MIN, "image/jpeg")},
            data={"sku": "../evil"},
        )
        assert r.status_code == 400


@DB_TESTS
class TestConfirmDb:
    async def test_confirm_and_none_correct(self, vision_client):
        c = vision_client.client
        r = c.post(
            "/store/counter/recognize",
            files={"file": ("frame.png", PNG_1X1, "image/png")},
            data={"weight_grams": "500"},
        )
        assert r.status_code == 200, r.text
        rec_id = r.json()["recognition_id"]
        vision_client.state["recognitions"].append(rec_id)
        vision_client.state["frames"].append(r.json()["frame_id"])

        var_id = await _seed_variant(vision_client.pool, vision_client.state)
        r = c.post(
            f"/store/counter/recognitions/{rec_id}/confirm",
            json={"variant_id": var_id},
        )
        assert r.status_code == 200, r.text
        assert r.json()["confirmed_variant_id"] == var_id
        assert r.json()["confirmed_none"] is False

        r = c.post(
            f"/store/counter/recognitions/{rec_id}/confirm",
            json={"none_correct": True},
        )
        assert r.status_code == 200, r.text
        assert r.json()["confirmed_none"] is True
        assert r.json()["confirmed_variant_id"] is None

    def test_confirm_unknown_recognition_404(self, vision_client):
        r = vision_client.client.post(
            f"/store/counter/recognitions/{uuid.uuid4()}/confirm",
            json={"none_correct": True},
        )
        assert r.status_code == 404


@DB_TESTS
class TestMerchantGateDb:
    async def test_non_merchant_403(self, vision_client):
        """is_merchant=false 的顾客调门禁 → 403（门禁走真实查库）。"""
        from app.storefront import _require_merchant

        pool = vision_client.pool
        plain_id = str(uuid.uuid4())
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO "customer" (id, email, status, is_merchant, created_at, updated_at)'
                " VALUES ($1, $2, 'active', false, NOW(), NOW())",
                plain_id,
                f"cv-plain-{uuid.uuid4().hex[:8]}@test.dev",
            )
        vision_client.state["customers"].append(plain_id)
        fake_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
        with pytest.raises(Exception) as excinfo:
            await _require_merchant(fake_request, {"customer_id": plain_id})
        assert getattr(excinfo.value, "status_code", None) == 403
