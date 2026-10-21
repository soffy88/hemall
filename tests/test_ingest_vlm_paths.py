"""ingest_media 的 VLM 三条路径单测 — FakePool/FakeConn + mock oprim.vlm_*。

覆盖 (不依赖真实 PG / 真实模型):
- 识别成功且无候选 → 识别新建 (识别名/品类/描述落库, 批次带 vwap 曲线)
- 识别成功 + 候选有图 + VLM 判同一 → 以图更新 (只补批次, 不建商品)
- 识别成功 + 候选无图 + 标题归一化全等 → title_exact 更新
- VLM 异常 → 降级旧逻辑 (文件名推导, recognition="unavailable")
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import UploadFile

from app.ext import agent_gateway, oprim
from tests.service_fakes import FakeConn, FakePool

_IDENTIFY_OK = {
    "title": "丹东草莓",
    "category_hint": "水果",
    "description": "丹东产鲜草莓",
    "keywords": ["草莓"],
    "confidence": 0.9,
}


@pytest.fixture(autouse=True)
def _settings_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.deps.get_settings",
        lambda: SimpleNamespace(default_vlm_provider="ollama"),
    )


def _image_upload(name: str = "随手拍.jpg", content: bytes = b"new-image-bytes"):
    file = MagicMock(spec=UploadFile)
    file.filename = name
    file.content_type = "image/jpeg"
    file.read = AsyncMock(return_value=content)
    return file


def _base_conn() -> FakeConn:
    """三条路径共用的落库脚本: 分类全量 + 按 slug 查 + 三张表插入。"""
    conn = FakeConn()
    conn.on(r'SELECT slug, name FROM "product_category"', [{"slug": "fruit", "name": "水果"}])
    conn.on(r'SELECT id FROM "product_category" WHERE slug', "cat-1")
    conn.on(r'INSERT INTO "product" \(', "prod-new")
    conn.on(r'INSERT INTO "product_variant" \(', "var-new")
    conn.on(r'INSERT INTO "inventory_batch" \(', "batch-new")
    return conn


async def _ingest(conn: FakeConn, out_root: Path, file, **kwargs):
    return await agent_gateway.ingest_media(
        file,
        location_id="loc-1",
        pool=FakePool(conn),
        out_root=out_root,
        principal={"user_id": "u1"},
        **kwargs,
    )


def _batch_insert_args(conn: FakeConn) -> tuple:
    issued = conn.issued(r'INSERT INTO "inventory_batch" \(')
    assert len(issued) == 1
    return issued[0][1]


class TestIdentifyCreatePath:
    async def test_recognized_create_uses_identified_fields_and_curve(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            oprim, "vlm_identify_product", AsyncMock(return_value=dict(_IDENTIFY_OK))
        )
        same_mock = AsyncMock()
        monkeypatch.setattr(oprim, "vlm_same_product", same_mock)
        conn = _base_conn()
        conn.on(r'SELECT id, title FROM "product"', [])  # 无候选

        result = await _ingest(conn, tmp_path, _image_upload(), stock_qty=48)

        assert result["status"] == "created"
        assert result["recognition"] == "ollama"
        assert result["identified_title"] == "丹东草莓"
        assert result["title"] == "丹东草莓"
        assert result["matched"] is False
        same_mock.assert_not_called()

        # 商品名/描述/分类都用识别结果
        product_args = conn.issued(r'INSERT INTO "product" \(')[0][1]
        assert "丹东草莓" in product_args
        assert "丹东产鲜草莓" in product_args
        cat_args = conn.issued(r'SELECT id FROM "product_category" WHERE slug')[0][1]
        assert cat_args == ("fruit",)

        # 批次带 vwap 做市曲线: 72 点, 末点 = stock_qty
        curve = json.loads(_batch_insert_args(conn)[-1])
        assert len(curve["expected_cumulative"]) == 72
        assert curve["expected_cumulative"][-1] == 48
        assert curve["stddev"] == pytest.approx(48 * 0.15)


class TestSameProductUpdatePath:
    async def test_image_match_updates_existing_product(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            oprim, "vlm_identify_product", AsyncMock(return_value=dict(_IDENTIFY_OK))
        )
        same_mock = AsyncMock(
            return_value={"same": True, "confidence": 0.88, "reason": "包装一致"}
        )
        monkeypatch.setattr(oprim, "vlm_same_product", same_mock)

        # 候选旧图先落盘 (映射: out_root/agent_ingest/<user>/<file>)
        old_img = tmp_path / "agent_ingest" / "u1" / "old.jpg"
        old_img.parent.mkdir(parents=True)
        old_img.write_bytes(b"old-image-bytes")

        conn = _base_conn()
        conn.on(r'SELECT id, title FROM "product"', [{"id": "prod-9", "title": "丹东草莓"}])
        conn.on(r'SELECT id FROM "product_variant"', "var-9")
        conn.on(
            r'SELECT shelf_image_url, media_assets FROM "inventory_batch"',
            {
                "shelf_image_url": '["/media/agent_ingest/u1/old.jpg"]',
                "media_assets": '["/media/agent_ingest/u1/old.jpg"]',
            },
        )
        conn.on(r'INSERT INTO "inventory_batch" \(', "batch-upd")

        result = await _ingest(conn, tmp_path, _image_upload(), stock_qty=20)

        assert result["status"] == "updated"
        assert result["matched"] is True
        assert result["matched_product_id"] == "prod-9"
        assert result["product_id"] == "prod-9"
        assert result["variant_id"] == "var-9"
        assert result["match_via"] == "vlm_same_product"
        assert result["match_confidence"] == pytest.approx(0.88)
        same_mock.assert_awaited_once()

        # 不新建商品/规格, 只给旧 variant 补批次 (且带做市曲线)
        assert not conn.did_issue(r'INSERT INTO "product" \(')
        assert not conn.did_issue(r'INSERT INTO "product_variant" \(')
        batch_args = _batch_insert_args(conn)
        assert batch_args[0] == "var-9"
        curve = json.loads(batch_args[-1])
        assert curve["expected_cumulative"][-1] == 20


class TestTitleExactUpdatePath:
    async def test_candidate_without_image_falls_back_to_title_exact(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            oprim, "vlm_identify_product", AsyncMock(return_value=dict(_IDENTIFY_OK))
        )
        same_mock = AsyncMock()
        monkeypatch.setattr(oprim, "vlm_same_product", same_mock)

        conn = _base_conn()
        conn.on(
            r'SELECT id, title FROM "product"',
            [{"id": "prod-7", "title": "丹东 草莓!"}],  # 归一化后与识别名全等
        )
        conn.on(r'SELECT id FROM "product_variant"', "var-7")
        conn.on(r'SELECT shelf_image_url, media_assets FROM "inventory_batch"', None)
        conn.on(r'INSERT INTO "inventory_batch" \(', "batch-exact")

        result = await _ingest(conn, tmp_path, _image_upload())

        assert result["status"] == "updated"
        assert result["matched_product_id"] == "prod-7"
        assert result["match_via"] == "title_exact"
        same_mock.assert_not_called()
        assert not conn.did_issue(r'INSERT INTO "product" \(')


class TestIdentifyUnavailableDegrades:
    async def test_vlm_failure_falls_back_to_filename_logic(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            oprim,
            "vlm_identify_product",
            AsyncMock(side_effect=RuntimeError("ollama unreachable")),
        )
        conn = _base_conn()

        result = await _ingest(
            conn, tmp_path, _image_upload(name="黄瓜实拍.jpg", content=b"cucumber")
        )

        assert result["status"] == "created"
        assert result["recognition"] == "unavailable"
        assert result["identified_title"] is None
        assert result["matched"] is False
        assert result["title"] == "黄瓜实拍"  # 旧逻辑: 文件名推导
        # 降级路径不查候选, 分类用入参默认 slug
        assert not conn.did_issue(r'SELECT id, title FROM "product"')
        cat_args = conn.issued(r'SELECT id FROM "product_category" WHERE slug')[0][1]
        assert cat_args == ("daily",)
        # 降级新建的批次同样带做市曲线
        curve = json.loads(_batch_insert_args(conn)[-1])
        assert len(curve["expected_cumulative"]) == 72
