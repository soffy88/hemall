"""spider_hygiene 单测: 标题/介绍去品牌与外链, 商品图下载本地化 (Fake HTTP)。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.ext.spider_hygiene import (
    clean_spider_description,
    clean_spider_title,
    localize_spider_image,
)

_JPEG = b"\xff\xd8\xff" + b"\x00" * 5000
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 5000


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("苏宁自营酥田东北盘锦蟹稻共生大米2.5KG 小包装", "酥田东北盘锦蟹稻共生大米2.5KG 小包装"),
        (
            "苏鲜生 东北大米5kg 东北米粳米寿司米10斤[苏宁自有品牌]",
            "东北大米5kg 东北米粳米寿司米10斤",
        ),
        (
            "苏宁超市自营 晋龙 新鲜鸡蛋10枚红心蛋非农家散养鸡蛋",
            "晋龙 新鲜鸡蛋10枚红心蛋非农家散养鸡蛋",
        ),
        ("苏鲜生 [苏宁自有品牌]可生食鲜鸡蛋1.8kg(30枚)", "可生食鲜鸡蛋1.8kg(30枚)"),
        (
            "苏鲜生推荐东北大米500g 东北五常米圆粒粳米珍珠寿司米1斤[苏宁自有品牌]",
            "东北大米500g 东北五常米圆粒粳米珍珠寿司米1斤",
        ),
        (
            "苏宁易购 蟹灵阁 阳澄湖 大闸蟹 螃蟹卡 提货券 公6.0两 母4.5两 4对8只装",
            "蟹灵阁 阳澄湖 大闸蟹 螃蟹卡 提货券 公6.0两 母4.5两 4对8只装",
        ),
        ("[电子提货券]苏鲜生 苏宁甄选大闸蟹提货券", "[电子提货券] 大闸蟹提货券"),
        (
            "苏宁宜品 手撕豆皮20包网红辣片辣味宿舍囤货休闲解馋小零食独立小包",
            "手撕豆皮20包网红辣片辣味宿舍囤货休闲解馋小零食独立小包",
        ),
        (
            "顺丰快递黄天鹅可生食鸡蛋 20枚/2.12斤礼盒装 生鲜鸡蛋苏宁自营 无抗生素",
            "黄天鹅可生食鸡蛋 20枚/2.12斤礼盒装 生鲜鸡蛋 无抗生素",
        ),
        ("丹东草莓 新鲜水果 2斤装", "丹东草莓 新鲜水果 2斤装"),
    ],
)
def test_clean_spider_title(raw: str, want: str) -> None:
    assert clean_spider_title(raw) == want


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (
            "产地直发\n来源：苏宁自营 https://m.suning.com/product/0000000000/12417909119.html",
            "产地直发",
        ),
        (
            "正品保障\n来源：苏宁自营 https://m.suning.com/product/0000000000/12417909119.html",
            "正品保障",
        ),
        ("来源：OpenFoodFacts https://world.openfoodfacts.org/product/4710063312168", ""),
        (
            "四段蕴香控火技术 14大养生菜单\n来源：美的小家电苏宁自营旗舰店 https://m.suning.com/x",
            "四段蕴香控火技术 14大养生菜单",
        ),
        ("产地直发", "产地直发"),
        ("", ""),
        (None, ""),
    ],
)
def test_clean_spider_description(raw: Any, want: str) -> None:
    assert clean_spider_description(raw) == want


class _FakeImgResp:
    def __init__(self, content: bytes, *, error: Exception | None = None) -> None:
        self.content = content
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error


class _FakeImgClient:
    next_content: bytes = _JPEG
    next_error: Exception | None = None
    calls: int = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeImgClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def get(self, url: str, params: Any = None, headers: Any = None) -> _FakeImgResp:
        type(self).calls += 1
        if type(self).next_error is not None:
            raise type(self).next_error
        return _FakeImgResp(type(self).next_content)


@pytest.fixture(autouse=True)
def _patch_img_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeImgClient.next_content = _JPEG
    _FakeImgClient.next_error = None
    _FakeImgClient.calls = 0
    monkeypatch.setattr(httpx, "AsyncClient", _FakeImgClient)


async def test_localize_downloads_and_reuses(tmp_path: Path) -> None:
    url = "https://imgservice3.suning.cn/uimg1/b2c/image/abc.jpg"
    first = await localize_spider_image(url, output_root=tmp_path)
    assert first is not None
    assert first.startswith("/media/spider/")
    assert first.endswith(".jpg")
    assert _FakeImgClient.calls == 1
    assert (tmp_path / "ext_bespoke" / "spider_media" / first.rsplit("/", 1)[1]).exists()

    second = await localize_spider_image(url, output_root=tmp_path)
    assert second == first
    assert _FakeImgClient.calls == 1  # 已落盘不再下载


async def test_localize_png_extension(tmp_path: Path) -> None:
    _FakeImgClient.next_content = _PNG
    got = await localize_spider_image("https://example.com/x.png", output_root=tmp_path)
    assert got is not None and got.endswith(".png")


@pytest.mark.parametrize("content", [b"hello" * 2000, b"\xff\xd8\xff" + b"\x00" * 10])
async def test_localize_rejects_non_image_or_tiny(tmp_path: Path, content: bytes) -> None:
    _FakeImgClient.next_content = content
    assert await localize_spider_image("https://example.com/x", output_root=tmp_path) is None


async def test_localize_http_error_returns_none(tmp_path: Path) -> None:
    _FakeImgClient.next_error = httpx.HTTPError("boom")
    assert await localize_spider_image("https://example.com/x", output_root=tmp_path) is None


async def test_localize_non_http_returns_none_without_client(tmp_path: Path) -> None:
    assert await localize_spider_image("/products/a.jpg", output_root=tmp_path) is None
    assert await localize_spider_image("", output_root=tmp_path) is None
    assert _FakeImgClient.calls == 0
