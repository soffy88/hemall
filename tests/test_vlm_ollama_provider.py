"""OllamaVLMProvider 单测 — monkeypatch httpx.AsyncClient, 不打真模型。

覆盖: 正常 JSON 解析 / 带 markdown 围栏的 JSON / 坏 JSON 抛错 / 超时抛错 /
空标题抛错 / assess_damage 与 Manual 版同形状 + 本地图读不到抛错 /
oprim registry 路由。
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from app.ext import oprim
from app.ext.vlm_ollama_provider import OllamaVLMProvider


class _FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        content: bytes = b"",
    ) -> None:
        self._payload = payload or {}
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    """按类属性脚本化响应的 httpx.AsyncClient 替身。"""

    next_post_response: _FakeResponse | None = None
    next_get_response: _FakeResponse | None = None
    next_post_error: Exception | None = None
    last_post: dict[str, Any] | None = None
    last_timeout: Any = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).last_timeout = kwargs.get("timeout")

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def post(self, url: str, json: Any = None) -> _FakeResponse:
        type(self).last_post = {"url": url, "json": json}
        if type(self).next_post_error is not None:
            raise type(self).next_post_error
        assert type(self).next_post_response is not None
        return type(self).next_post_response

    async def get(self, url: str) -> _FakeResponse:
        assert type(self).next_get_response is not None
        return type(self).next_get_response


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.next_post_response = None
    _FakeAsyncClient.next_get_response = None
    _FakeAsyncClient.next_post_error = None
    _FakeAsyncClient.last_post = None
    _FakeAsyncClient.last_timeout = None
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)


def _provider(**kwargs: Any) -> OllamaVLMProvider:
    return OllamaVLMProvider(
        base_url="http://172.17.0.1:11434",
        model="minicpm-v47-1b-local:latest",
        timeout_s=5.0,
        **kwargs,
    )


def _chat(content: str) -> _FakeResponse:
    return _FakeResponse({"message": {"role": "assistant", "content": content}})


_IDENTIFY_JSON = {
    "title": "丹东草莓",
    "category_hint": "水果",
    "description": "丹东产鲜草莓, 果大味甜",
    "keywords": ["草莓", "丹东", "鲜果"],
    "confidence": 0.87,
}


class TestIdentifyProduct:
    async def test_parses_plain_json_and_sends_native_chat_payload(self) -> None:
        _FakeAsyncClient.next_post_response = _chat(json.dumps(_IDENTIFY_JSON))

        result = await _provider().identify_product(image_bytes=b"img-bytes")

        assert result == _IDENTIFY_JSON
        post = _FakeAsyncClient.last_post
        assert post is not None
        assert post["url"] == "http://172.17.0.1:11434/api/chat"
        payload = post["json"]
        assert payload["model"] == "minicpm-v47-1b-local:latest"
        assert payload["stream"] is False
        images = payload["messages"][0]["images"]
        assert images == [base64.b64encode(b"img-bytes").decode("ascii")]
        assert _FakeAsyncClient.last_timeout == 5.0

    async def test_parses_fenced_json(self) -> None:
        fenced = "```json\n" + json.dumps(_IDENTIFY_JSON) + "\n```"
        _FakeAsyncClient.next_post_response = _chat(fenced)

        result = await _provider().identify_product(image_bytes=b"x")

        assert result["title"] == "丹东草莓"
        assert result["confidence"] == pytest.approx(0.87)

    async def test_bad_json_raises(self) -> None:
        _FakeAsyncClient.next_post_response = _chat("这是一张红色水果的照片")

        with pytest.raises(ValueError):
            await _provider().identify_product(image_bytes=b"x")

    async def test_timeout_raises(self) -> None:
        _FakeAsyncClient.next_post_error = httpx.TimeoutException("too slow")

        with pytest.raises(httpx.TimeoutException):
            await _provider().identify_product(image_bytes=b"x")

    async def test_empty_title_raises(self) -> None:
        _FakeAsyncClient.next_post_response = _chat(json.dumps({"title": "", "confidence": 0.1}))

        with pytest.raises(ValueError):
            await _provider().identify_product(image_bytes=b"x")

    async def test_keywords_string_coerced_and_confidence_clamped(self) -> None:
        _FakeAsyncClient.next_post_response = _chat(
            json.dumps({"title": "黄瓜", "keywords": "黄瓜，蔬菜、鲜蔬", "confidence": 1.4})
        )

        result = await _provider().identify_product(image_bytes=b"x")

        assert result["keywords"] == ["黄瓜", "蔬菜", "鲜蔬"]
        assert result["confidence"] == 1.0


class TestSameProduct:
    async def test_parses_verdict_and_sends_two_images(self) -> None:
        _FakeAsyncClient.next_post_response = _chat(
            json.dumps({"same": True, "confidence": 0.92, "reason": "包装一致"})
        )

        result = await _provider().same_product(image_a=b"a", image_b=b"b")

        assert result == {"same": True, "confidence": 0.92, "reason": "包装一致"}
        images = _FakeAsyncClient.last_post["json"]["messages"][0]["images"]  # type: ignore[index]
        assert len(images) == 2


class TestAssessDamage:
    async def test_local_file_matches_manual_shape_and_clamps(self, tmp_path) -> None:
        img = tmp_path / "evidence.jpg"
        img.write_bytes(b"evidence-bytes")
        _FakeAsyncClient.next_post_response = _chat(
            json.dumps({"damage_type": "破损", "severity": 1.7, "fraud_probability": 0.05})
        )

        result = await _provider().assess_damage(
            evidence_img=str(img), original_batch_video="/media/batch.mp4"
        )

        assert result == {
            "evidence_img": str(img),
            "original_batch_video": "/media/batch.mp4",
            "damage_type": "破损",
            "severity": 1.0,
            "fraud_probability": 0.05,
        }

    async def test_missing_file_raises_not_fabricated(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError):
            await _provider().assess_damage(
                evidence_img=str(tmp_path / "nope.jpg"),
                original_batch_video="",
            )

    async def test_http_url_evidence_downloaded(self) -> None:
        _FakeAsyncClient.next_get_response = _FakeResponse(content=b"remote-img")
        _FakeAsyncClient.next_post_response = _chat(
            json.dumps({"damage_type": "none", "severity": 0.0, "fraud_probability": 0.0})
        )

        result = await _provider().assess_damage(
            evidence_img="https://example.com/e.jpg", original_batch_video=""
        )

        assert result["damage_type"] == "none"
        sent = _FakeAsyncClient.last_post["json"]["messages"][0]["images"]  # type: ignore[index]
        assert sent == [base64.b64encode(b"remote-img").decode("ascii")]

    async def test_media_path_maps_under_output_root(self, tmp_path) -> None:
        media_file = tmp_path / "ext_bespoke" / "agent_ingest" / "agent_ingest" / "u1" / "e.jpg"
        media_file.parent.mkdir(parents=True)
        media_file.write_bytes(b"media-img")
        _FakeAsyncClient.next_post_response = _chat(
            json.dumps({"damage_type": "挤压变形", "severity": 0.4, "fraud_probability": 0.1})
        )

        provider = _provider(output_root=tmp_path)
        result = await provider.assess_damage(
            evidence_img="/media/agent_ingest/u1/e.jpg", original_batch_video=""
        )

        assert result["damage_type"] == "挤压变形"
        assert result["severity"] == pytest.approx(0.4)


class TestExtractPrice:
    async def test_parses_numeric_price(self) -> None:
        _FakeAsyncClient.next_post_response = _chat(
            json.dumps({"price_yuan": 29.9, "price_text": "¥29.90", "confidence": 0.92})
        )

        result = await _provider().extract_price(image_bytes=b"shot")

        assert result == {
            "price_yuan": 29.9,
            "price_text": "¥29.90",
            "confidence": pytest.approx(0.92),
        }

    async def test_parses_string_price_with_symbols(self) -> None:
        _FakeAsyncClient.next_post_response = _chat(
            json.dumps({"price_yuan": "¥1,299.00", "price_text": "¥1299", "confidence": 0.8})
        )

        result = await _provider().extract_price(image_bytes=b"shot")
        assert result["price_yuan"] == 1299.0

    async def test_no_price_returns_none_without_raising(self) -> None:
        _FakeAsyncClient.next_post_response = _chat(
            json.dumps({"price_yuan": None, "price_text": "", "confidence": 0.15})
        )

        result = await _provider().extract_price(image_bytes=b"shot")
        assert result["price_yuan"] is None
        assert result["confidence"] == pytest.approx(0.15)

    async def test_garbage_confidence_degrades_to_zero(self) -> None:
        _FakeAsyncClient.next_post_response = _chat(
            json.dumps({"price_yuan": 10, "price_text": "¥10", "confidence": "高"})
        )

        result = await _provider().extract_price(image_bytes=b"shot")
        assert result["price_yuan"] == 10.0
        assert result["confidence"] == 0.0

    async def test_network_error_raises(self) -> None:
        _FakeAsyncClient.next_post_error = httpx.ConnectError("down")

        with pytest.raises(httpx.ConnectError):
            await _provider().extract_price(image_bytes=b"shot")

    async def test_empty_image_raises(self) -> None:
        with pytest.raises(ValueError):
            await _provider().extract_price(image_bytes=b"")


class TestOprimRouting:
    async def test_oprim_routes_through_registry(self) -> None:
        from obase.provider_registry import ProviderRegistry

        sentinel = {
            "title": "测试商品",
            "category_hint": "",
            "description": "",
            "keywords": [],
            "confidence": 0.5,
        }

        class _FakeVLM:
            async def identify_product(self, *, image_bytes: bytes) -> dict[str, Any]:
                return dict(sentinel)

            async def same_product(self, *, image_a: bytes, image_b: bytes) -> dict[str, Any]:
                return {"same": True, "confidence": 1.0, "reason": "fake"}

        ProviderRegistry.get().register_generic("vlm", "unit-test", _FakeVLM(), replace=True)

        assert await oprim.vlm_identify_product("unit-test", image_bytes=b"x") == sentinel
        verdict = await oprim.vlm_same_product("unit-test", image_a=b"a", image_b=b"b")
        assert verdict["same"] is True

    async def test_oprim_extract_price_routes_through_registry(self) -> None:
        from obase.provider_registry import ProviderRegistry

        class _FakeVLM:
            async def extract_price(self, *, image_bytes: bytes) -> dict[str, Any]:
                assert image_bytes == b"png"
                return {"price_yuan": 12.5, "price_text": "¥12.5", "confidence": 0.9}

        ProviderRegistry.get().register_generic("vlm", "unit-test-price", _FakeVLM(), replace=True)

        result = await oprim.vlm_extract_price("unit-test-price", image_bytes=b"png")
        assert result["price_yuan"] == 12.5

    async def test_oprim_spider_fetch_products_routes_through_registry(self) -> None:
        from obase.provider_registry import ProviderRegistry

        class _FakeSpider:
            async def fetch_products(self, *, keywords: list[str]) -> list[dict[str, Any]]:
                return [{"item": f"kw:{keywords[0]}", "source_id": "1"}]

        ProviderRegistry.get().register_generic("spider", "unit-test", _FakeSpider(), replace=True)

        items = await oprim.spider_fetch_products("unit-test", keywords=["草莓"])
        assert items[0]["item"] == "kw:草莓"
