"""OCR 品牌门单测: fake OCR 覆盖命中/干净/超时/缺二进制; 真实 fixture 验真命中与放行。"""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.ext.spider_hygiene import (
    BrandScreenResult,
    brand_screen_to_inspection,
    localize_and_screen_spider_image,
    match_brand_terms,
    screen_spider_image,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_JPEG = b"\xff\xd8\xff" + b"\x00" * 5000


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("顺丰速运 免费送货上门", "顺丰"),
        ("苏 宁 易 购", "苏宁"),
        ("SuFresh 苏鲜生", "苏鲜生"),
        ("SUNING.COM", "suning"),
        ("新鲜鸡蛋 20 枚", None),
        ("", None),
    ],
)
def test_match_brand_terms(text: str, want: str | None) -> None:
    assert match_brand_terms(text) == want


@pytest.mark.parametrize(
    ("result", "want"),
    [
        (
            BrandScreenResult("hit", matched_term="顺丰", detail="ocr_brand:顺丰"),
            ("failed", "ocr_brand:顺丰"),
        ),
        (BrandScreenResult("clean"), ("passed", None)),
        (BrandScreenResult("skipped", detail="ocr_timeout"), ("pending", None)),
        (BrandScreenResult("skipped", detail="cached"), ("pending", None)),
    ],
)
def test_brand_screen_to_inspection(
    result: BrandScreenResult, want: tuple[str, str | None]
) -> None:
    assert brand_screen_to_inspection(result) == want


async def _fake_hit(_path: Path) -> str:
    return "顺丰速运"


async def _fake_clean(_path: Path) -> str:
    return "新鲜鸡蛋 20 枚"


async def _fake_timeout(_path: Path) -> str:
    raise TimeoutError


async def _fake_boom(_path: Path) -> str:
    raise RuntimeError("ocr exploded")


async def test_screen_hit_clean_timeout_error(tmp_path: Path) -> None:
    img = tmp_path / "x.jpg"
    img.write_bytes(_JPEG)
    got = await screen_spider_image(img, ocr_text_runner=_fake_hit)
    assert got.status == "hit" and got.matched_term == "顺丰"
    got = await screen_spider_image(img, ocr_text_runner=_fake_clean)
    assert got.status == "clean"
    got = await screen_spider_image(img, ocr_text_runner=_fake_timeout)
    assert got.status == "skipped" and got.detail == "ocr_timeout"
    got = await screen_spider_image(img, ocr_text_runner=_fake_boom)
    assert got.status == "skipped"
    assert got.detail is not None and got.detail.startswith("ocr_error")


async def test_screen_missing_tesseract_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    img = tmp_path / "x.jpg"
    img.write_bytes(_JPEG)
    got = await screen_spider_image(img)
    assert got.status == "skipped" and got.detail == "ocr_unavailable"


class _FakeImgResp:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _FakeImgClient:
    next_content: bytes = _JPEG
    calls: int = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeImgClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def get(self, url: str, params: Any = None, headers: Any = None) -> _FakeImgResp:
        type(self).calls += 1
        return _FakeImgResp(type(self).next_content)


@pytest.fixture(autouse=True)
def _patch_img_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeImgClient.next_content = _JPEG
    _FakeImgClient.calls = 0
    monkeypatch.setattr(httpx, "AsyncClient", _FakeImgClient)


async def test_localize_and_screen_fresh_hit_then_cached(tmp_path: Path) -> None:
    calls: list[Path] = []

    async def _runner(path: Path) -> str:
        calls.append(path)
        return "顺丰速运"

    url = "https://imgservice3.suning.cn/uimg1/b2c/image/abc.jpg"
    first, verdict = await localize_and_screen_spider_image(
        url, output_root=tmp_path, ocr_text_runner=_runner
    )
    assert first is not None and first.startswith("/media/spider/")
    assert verdict.status == "hit" and verdict.matched_term == "顺丰"
    assert len(calls) == 1
    second, verdict2 = await localize_and_screen_spider_image(
        url, output_root=tmp_path, ocr_text_runner=_runner
    )
    assert second == first
    assert verdict2.status == "skipped" and verdict2.detail == "cached"
    assert len(calls) == 1  # 缓存命中不再跑 OCR


async def test_localize_and_screen_non_http(tmp_path: Path) -> None:
    got, verdict = await localize_and_screen_spider_image("/products/a.jpg", output_root=tmp_path)
    assert got is None
    assert verdict.status == "skipped"


@lru_cache(maxsize=1)
def _tesseract_has_chi_sim() -> bool:
    if shutil.which("tesseract") is None:
        return False
    proc = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=15)
    return "chi_sim" in proc.stdout


@pytest.mark.skipif(not _tesseract_has_chi_sim(), reason="tesseract chi_sim 不可用")
async def test_real_fixture_brand_image_hits() -> None:
    img = _FIXTURES / "ocr-brand-shunfeng.jpg"
    assert img.exists()
    got = await screen_spider_image(img)
    assert got.status == "hit" and got.matched_term == "顺丰"


@pytest.mark.skipif(not _tesseract_has_chi_sim(), reason="tesseract chi_sim 不可用")
async def test_real_fixture_clean_seed_passes() -> None:
    img = _FIXTURES / "ocr-clean-seed.jpg"
    assert img.exists()
    got = await screen_spider_image(img)
    assert got.status == "clean"
