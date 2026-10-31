"""蜘蛛上架数据卫生: 标题/介绍去品牌与外链, 商品图下载本地化.

Soffy 要求: 前台只展示产品图, 不要其他公司的标识与连接。
- 标题: 去掉苏宁系品牌词 (苏宁自营/苏鲜生/苏宁宜品/...) 与顺丰快递。
- 介绍: 截掉末尾 "来源:<店铺> <详情页 URL>" 尾巴, 并清掉残留 URL。
- 商品图: 下载到 <output_root>/ext_bespoke/spider_media/, 前台经
  /media/spider/<hash>.<ext> 访问 (main.py 挂载, 与 agent_ingest 同机制);
  下载失败返回 None, 调用方保留原 URL 并由 tick 回填重试。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import httpx

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
_BRAND_BRACKET_RE = re.compile(r"\[[^\]]*(?:苏宁|易购|云商)[^\]]*\]")
_URL_RE = re.compile(r"https?://\S+")
_SOURCE_TRAILER_RE = re.compile(r"\n来源[:：]")
_SOURCE_TRAILER_URL_RE = re.compile(r"来源[:：][^\n]*https?://")


def clean_spider_title(title: str) -> str:
    """去掉标题里的其他公司品牌词, 只留产品名本身."""
    text = _BRAND_BRACKET_RE.sub("", title or "")
    for token in _BRAND_TOKENS:
        text = text.replace(token, "")
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
