"""蜘蛛上架数据卫生: 标题/介绍去品牌与外链, 商品图下载本地化.

Soffy 要求: 前台只展示产品图, 不要其他公司的标识与连接。
- 标题: 去掉苏宁系品牌词 (苏宁自营/苏鲜生/苏宁宜品/...) 与顺丰快递。
- 介绍: 截掉末尾 "来源:<店铺> <详情页 URL>" 尾巴, 并清掉残留 URL。
- 商品图: 下载到 <output_root>/ext_bespoke/spider_media/, 前台经
  /media/spider/<hash>.<ext> 访问 (main.py 挂载, 与 agent_ingest 同机制);
  下载失败返回 None, 调用方保留原 URL 并由 tick 回填重试。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

SPIDER_MEDIA_DIRNAME = "spider_media"
SPIDER_MEDIA_ROUTE = "/media/spider"

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 品牌词: 长串优先, 避免短词先吃掉长词的一部分。
_BRAND_TOKENS = (
    "苏宁超市自营",
    "苏宁自有品牌",
    "苏鲜生推荐",
    "苏宁易购",
    "苏宁自营",
    "苏宁甄选",
    "苏鲜生",
    "苏宁宜品",
    "顺丰快递",
    "苏宁",
    "易购",
    "云商",
)
_BRAND_BRACKET_RE = re.compile(r"\[[^\]]*(?:苏宁|易购|云商|苏鲜生|顺丰快递)[^\]]*\]")
_EMPTY_BRACKETS_RE = re.compile(r"\[\s*\]")
_FW_OPEN = chr(0x3010)
_FW_CLOSE = chr(0x3011)
_URL_RE = re.compile(r"https?://\S+")
_SOURCE_TRAILER_RE = re.compile(r"\n来源[:：]")
_SOURCE_TRAILER_URL_RE = re.compile(r"来源[:：][^\n]*https?://")


# OCR 品牌门: 蜘蛛图片入库时筛像素内印的品牌字 (词表与人工质检一致)。
# 只对新下载的图片跑; 命中 -> failed, 干净 -> passed, OCR 不可用/超时/报错 -> pending。
OCR_BRAND_TERMS = ("苏宁", "易购", "苏鲜生", "suning", "顺丰", "云商")

# OCR 品牌门 DDL: 质检原因留痕列 (ensure_p0_schema 幂等执行)。
OCR_GATE_DDL = """
ALTER TABLE IF EXISTS inventory_batch
    ADD COLUMN IF NOT EXISTS inspection_note TEXT;
"""

BRAND_OCR_TIMEOUT_SECONDS = 15.0

BrandScreenStatus = Literal["clean", "hit", "skipped"]


@dataclass(frozen=True)
class BrandScreenResult:
    """单图 OCR 品牌筛查结果。status: clean/hit/skipped(未筛出结论)。"""

    status: BrandScreenStatus
    matched_term: str | None = None
    detail: str | None = None


def match_brand_terms(ocr_text: str) -> str | None:
    """OCR 文本命中品牌词即返回该词 (去空白后子串匹配, 容忍 CJK 识别断词)。"""
    compact = re.sub(r"\s+", "", ocr_text or "").lower()
    for term in OCR_BRAND_TERMS:
        if term.lower() in compact:
            return term
    return None


def brand_screen_to_inspection(result: BrandScreenResult) -> tuple[str, str | None]:
    """筛查结果 -> (inspection_status, inspection_note); skipped 留 pending 不拦单。"""
    if result.status == "hit":
        return "failed", f"ocr_brand:{result.matched_term}"
    if result.status == "clean":
        return "passed", None
    return "pending", None


_OCR_MISSING_WARNED = False


async def _run_tesseract(image_path: Path, *, timeout: float) -> str:
    """tesseract chi_sim+eng --psm 11 读图取文本; 超时杀进程并抛 TimeoutError。"""
    proc = await asyncio.create_subprocess_exec(
        "tesseract",
        str(image_path),
        "stdout",
        "-l",
        "chi_sim+eng",
        "--psm",
        "11",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return out.decode("utf-8", errors="replace")


async def screen_spider_image(
    image_path: str | Path,
    *,
    timeout: float = BRAND_OCR_TIMEOUT_SECONDS,
    ocr_text_runner: Callable[[Path], Awaitable[str]] | None = None,
) -> BrandScreenResult:
    """对一张本地图片跑 OCR 品牌筛查; OCR 不可用/超时/报错时返回 skipped, 绝不拦单。

    ocr_text_runner 仅供测试注入 (fake OCR 文本); 生产走真实 tesseract。
    """
    global _OCR_MISSING_WARNED
    path = Path(image_path)
    try:
        if ocr_text_runner is not None:
            text = await ocr_text_runner(path)
        else:
            if shutil.which("tesseract") is None:
                if not _OCR_MISSING_WARNED:
                    _OCR_MISSING_WARNED = True
                    logger.warning("brand ocr skipped: tesseract binary not found")
                return BrandScreenResult("skipped", detail="ocr_unavailable")
            text = await _run_tesseract(path, timeout=timeout)
    except TimeoutError:
        logger.warning("brand ocr timeout: %s", path.name)
        return BrandScreenResult("skipped", detail="ocr_timeout")
    except FileNotFoundError:
        if not _OCR_MISSING_WARNED:
            _OCR_MISSING_WARNED = True
            logger.warning("brand ocr skipped: tesseract binary not found")
        return BrandScreenResult("skipped", detail="ocr_unavailable")
    except Exception as exc:  # noqa: BLE001 - OCR 故障绝不拖垮 spider tick
        logger.warning("brand ocr error: %s: %s", path.name, exc)
        return BrandScreenResult("skipped", detail=f"ocr_error: {exc}")
    matched = match_brand_terms(text)
    if matched is not None:
        return BrandScreenResult("hit", matched_term=matched, detail=f"ocr_brand:{matched}")
    return BrandScreenResult("clean")


def _find_cached_spider_image(image_url: str, output_root: str | Path) -> Path | None:
    """同 localize 的落盘复用判定 (镜像逻辑): 已落盘返回路径, 否则 None。"""
    if not image_url or not image_url.startswith(("http://", "https://")):
        return None
    digest = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:20]
    root = spider_media_root(output_root)
    for ext in (".jpg", ".png", ".webp"):
        candidate = root / f"{digest}{ext}"
        if candidate.exists() and candidate.stat().st_size > 4096:
            return candidate
    return None


async def localize_and_screen_spider_image(
    image_url: str,
    *,
    output_root: str | Path,
    timeout: float = 20.0,
    ocr_text_runner: Callable[[Path], Awaitable[str]] | None = None,
) -> tuple[str | None, BrandScreenResult]:
    """本地化 + 仅对新下载图片跑 OCR 品牌筛查。

    缓存命中 (同 URL 已落盘) 不重复筛查, verdict 为 skipped(cached);
    已上架批次的回填路径不走本函数, 其 inspection_status 不受影响。
    """
    cached = _find_cached_spider_image(image_url, output_root)
    local = await localize_spider_image(image_url, output_root=output_root, timeout=timeout)
    if local is None:
        return None, BrandScreenResult("skipped", detail="download_failed")
    if cached is not None:
        return local, BrandScreenResult("skipped", detail="cached")
    dest = spider_media_root(output_root) / local.rsplit("/", 1)[1]
    result = await screen_spider_image(dest, ocr_text_runner=ocr_text_runner)
    return local, result


def clean_spider_title(title: str) -> str:
    """去掉标题里的其他公司品牌词, 只留产品名本身."""
    text = (title or "").replace(_FW_OPEN, "[").replace(_FW_CLOSE, "]")
    text = _BRAND_BRACKET_RE.sub("", text)
    for token in _BRAND_TOKENS:
        text = text.replace(token, "")
    text = _EMPTY_BRACKETS_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" ,，、")
    if "/" in text:
        parts = [p.strip() for p in text.split("/")]
        deduped: list[str] = []
        for part in parts:
            if not part:
                continue
            if deduped and deduped[-1] == part:
                continue
            deduped.append(part)
        text = "/".join(deduped)
    return text.strip()


def clean_spider_description(description: str | None) -> str:
    """截掉介绍末尾的来源行 (含外链), 只留产品介绍本身."""
    if not description:
        return ""
    text = description
    m = _SOURCE_TRAILER_RE.search(text)
    if m:
        text = text[: m.start()]
    else:
        m2 = _SOURCE_TRAILER_URL_RE.search(text)
        if m2:
            text = text[: m2.start()]
    text = _URL_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def spider_media_root(output_root: str | Path) -> Path:
    return Path(output_root) / "ext_bespoke" / SPIDER_MEDIA_DIRNAME


def _sniff_extension(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


async def localize_spider_image(
    image_url: str,
    *,
    output_root: str | Path,
    timeout: float = 20.0,
) -> str | None:
    """把外链商品图下载到本地媒体目录, 返回 /media/spider/... 路径.

    已落盘 (同 URL 哈希) 直接复用; 非 http(s) 链接、下载失败、
    内容过小或不是图片时返回 None (调用方保留原 URL)。
    """
    if not image_url or not image_url.startswith(("http://", "https://")):
        return None
    digest = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:20]
    root = spider_media_root(output_root)
    for ext in (".jpg", ".png", ".webp"):
        candidate = root / f"{digest}{ext}"
        if candidate.exists() and candidate.stat().st_size > 4096:
            return f"{SPIDER_MEDIA_ROUTE}/{candidate.name}"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(
                image_url,
                headers={"User-Agent": _BROWSER_UA, "Referer": "https://www.suning.com/"},
            )
            resp.raise_for_status()
            data = resp.content
    except Exception:
        return None
    if len(data) < 4096:
        return None
    ext = _sniff_extension(data)
    if ext is None:
        return None
    root.mkdir(parents=True, exist_ok=True)
    dest = root / f"{digest}{ext}"
    tmp = root / f".{digest}{ext}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return f"{SPIDER_MEDIA_ROUTE}/{dest.name}"
