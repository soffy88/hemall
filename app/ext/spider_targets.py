"""app.ext.spider_targets — 具象化竞对价格爬虫目标 (Phase 7 Task 2)。

把 competitor_spider_engine 从"ManualSpiderProvider 默认空转"升级为**真实
HTTP 请求 + 真实解析**的具象化爬虫。三个目标 (架构师指令 A/B + 免费结构化
数据 API 替代)：

1) SuningMobileSpider (目标 A — 主流传统电商·苏宁易购生鲜)
   - GET https://m.suning.com/search/{keyword}/ (移动端搜索页，服务端渲染)
   - BeautifulSoup 解析嵌入 JSON 的真实商品名 + prdid
   - 每商品再探测价格 (pas.suning.com 价格 JSONP)；今日 API 价格字段缺失时
     诚实返回 price=None，不伪造

2) JddjSearchSpider (目标 B — 同城 O2O·京东到家)
   - GET https://www.jddj.com/search?keyword=... (公开比价入口)
   - 当前页面为 JS 壳 (服务端只吐框架) → 诚实返回空；解析代码就位，页面
     一旦恢复服务端渲染即可出数据，无需改引擎

3) OpenFoodFactsSpider (目标 B 替代 — 免费结构化数据 API)
   - GET https://world.openfoodfacts.org/cgi/search.pl?...&json=1
   - 真实商品 + quantity 规格；price 字段各市场覆盖率不一，有即入库

LayeredSpiderProvider 按 (lat, lon, radius) 先查测试覆写 (set_results，对齐
ManualSpiderProvider 契约)，否则对每个关键词依次打真实目标，条目去重后返回。
单位归一化交给调用方 (oservi.spider_engine → oskill.normalize_sku_price)：
本模块在 title/quantity 里提取规格单位 (如 "800g"/"1.5kg") 填入 item["unit"]。

诚实的空白：中文零售价格的纯 HTTP 抓取受 JS 渲染/反爬限制，免费源的
价格字段覆盖率决定了 price_benchmark 的真实写入率；本模块保证"真实请求 +
真实解析 + 诚实留痕"，不伪造价格冒充真数据。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger("hemall.ext.spider_targets")

#: 与 oskill.normalize_sku_price 同源的规格单位提取 (g/kg/ml/l)；支持小数
#: 数量 (1.5kg)，与 normalize 侧的整数回退相比提取更完整。
_UNIT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(g|kg|ml|l)", re.IGNORECASE)
#: 抓取超时 (真实目标网络慢，放宽到 10s)
_HTTP_TIMEOUT = 10.0
#: 每关键词最多打的真实目标数
_MAX_TARGETS_PER_KEYWORD = 3
#: 每关键词最多返回条目
_MAX_ITEMS_PER_KEYWORD = 12
#: 每商品价格探测上限 (防单关键词请求爆炸)
_MAX_PRICE_PROBES = 5

_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)


def extract_unit_from_text(text: str) -> str:
    """从商品名/规格文本提取单位 (如 "整切眼肉牛排800g(5片装)" → "800g")。

    返回最后一个命中 (标题里往往前段是系列名、后段才是净含量)；无命中返回
    空串——normalize_sku_price 拿到空单位会返回 None，price_benchmark 的
    normalized_price_per_unit 存 NULL 不污染基准线，诚实留痕。
    """
    matches = _UNIT_PATTERN.findall(text or "")
    if not matches:
        return ""
    qty, unit = matches[-1]
    return f"{qty}{unit.lower()}"


# ── 目标 A: 苏宁易购 (移动端搜索，服务端渲染) ───────────────────────────


def parse_suning_search(html: str) -> list[dict[str, Any]]:
    """解析苏宁移动端搜索页，返回 [{"item","prdid","unit"}, ...]。

    页面把商品数据以 JSON 内嵌在 script 里 (productName 是真实商品名，实测
    可解析出"货出六盘 宁夏西吉县谷草饲喂养 六盘山牛肉 精品黄牛 牛腩4斤")。
    prdid 是详情页商品 id，价格探测用它拼 pas.suning.com 价格 API。
    """
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    # 优先走内嵌 JSON 的商品名 (productName 实测存在)
    for m in re.finditer(r'"productName"\s*:\s*"([^"]{2,120})"', html):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        items.append({"item": name, "prdid": "", "unit": extract_unit_from_text(name)})
    # prdid 与商品名在同一段商品数据里，回扫每段 JSON 补齐
    for seg in re.finditer(r'\{[^{}]*?"productName"[^{}]*?\}', html):
        seg_txt = seg.group(0)
        prdid_m = re.search(r'"prdid"\s*:\s*"(\d{8,})"', seg_txt)
        name_m = re.search(r'"productName"\s*:\s*"([^"]{2,120})"', seg_txt)
        if prdid_m and name_m:
            for it in items:
                if it["item"] == name_m.group(1):
                    it["prdid"] = prdid_m.group(1)
    # 解析失败但页面确实含关键词时留空 (不伪造)
    return items[: _MAX_ITEMS_PER_KEYWORD]


def parse_suning_price(jsonp_body: str) -> float | None:
    """解析 pas.suning.com 价格 JSONP。今日该 API 只回信封 (无价格字段) →
    返回 None，如实反映源侧无价。"""
    text = jsonp_body.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    for key in ("promotionPrice", "price", "salePrice", "refPrice"):
        val = data.get(key)
        if val not in (None, ""):
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


# ── 目标 B: 京东到家 (O2O 公开比价页) ──────────────────────────────────


def parse_jddj_search(html: str) -> list[dict[str, Any]]:
    """解析京东到家搜索页。当前页面是 JS 壳 (服务端只吐框架) → 返回空。

    解析器就位：若页面恢复服务端渲染 (商品卡带价格 DOM)，按 .item-card /
    .price 选择器即可出数据，引擎与 provider 无需改动。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []
    for card in soup.select(".item-card, .product-item, [class*='item-card']")[
        : _MAX_ITEMS_PER_KEYWORD
    ]:
        title_el = card.select_one(".item-title, .product-name, .title")
        price_el = card.select_one(".price, .price-num, .sale-price")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue
        price: float | None = None
        if price_el:
            m = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)", price_el.get_text())
            if m:
                price = float(m.group(1))
        items.append(
            {
                "item": title,
                "price": price,
                "unit": extract_unit_from_text(title),
                "store": "京东到家",
            }
        )
    return items


# ── 目标 B 替代: OpenFoodFacts (免费结构化数据 API) ────────────────────


def parse_openfoodfacts(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 OFP 搜索 JSON。price 字段各市场覆盖率不一，有则带价入库。"""
    items: list[dict[str, Any]] = []
    for p in data.get("products", []):
        name = (p.get("product_name") or "").strip()
        if not name:
            continue
        qty = p.get("quantity") or ""
        price: float | None = None
        raw_price = p.get("price")
        if raw_price not in (None, ""):
            try:
                price = float(raw_price)
            except (TypeError, ValueError):
                price = None
        items.append(
            {
                "item": name[:120],
                "price": price,
                "unit": qty or extract_unit_from_text(name),
                "store": "OpenFoodFacts",
                "source": "openfoodfacts",
            }
        )
    return items


# ── 分层 provider (测试覆写 → 真实目标) ─────────────────────────────────


class LayeredSpiderProvider:
    """竞对价格爬虫：测试覆写优先，否则对关键词打真实目标。

    契约与 ManualSpiderProvider 一致 (set_results / fetch_competitor_prices)，
    测试 (test_ext_sky_patch_db) 与演练的 set_results 覆写仍然生效；真实部署
    下关键词命中真实目标时发出真实 HTTP 请求、返回真实解析结果。
    """

    def __init__(self) -> None:
        self._overrides: dict[tuple[float, float, int], list[dict[str, Any]]] = {}

    def set_results(
        self,
        *,
        lat: float,
        lon: float,
        radius_km: int,
        results: list[dict[str, Any]],
    ) -> None:
        self._overrides[(lat, lon, radius_km)] = results

    async def _fetch_suning(self, keyword: str) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT, headers={"User-Agent": _USER_AGENT}, follow_redirects=True
            ) as client:
                resp = await client.get(
                    f"https://m.suning.com/search/{quote(keyword)}/"
                )
                if resp.status_code >= 400:
                    logger.warning("suning search %s -> %d", keyword, resp.status_code)
                    return []
                items = parse_suning_search(resp.text)
        except Exception as exc:  # noqa: BLE001 - 目标不可达诚实留痕
            logger.debug("suning search failed %s: %s", keyword, exc)
            return []
        # 价格探测 (有 prdid 才打；免费 API 价格字段缺失时 item 不带 price)
        probed = 0
        for it in items:
            if probed >= _MAX_PRICE_PROBES or not it.get("prdid"):
                continue
            try:
                async with httpx.AsyncClient(
                    timeout=_HTTP_TIMEOUT,
                    headers={"User-Agent": "Mozilla/5.0"},
                    follow_redirects=True,
                ) as client:
                    r = await client.get(
                        f"https://pas.suning.com/nspcsale_0_{it['prdid']}"
                        "_0000000000_10_0_7000113_1000257_1_30115_1_5053.html"
                    )
                price = parse_suning_price(r.text)
                if price is not None:
                    it["price"] = price
                    it["store"] = "苏宁易购"
                    it["source"] = "suning"
                probed += 1
            except Exception as exc:  # noqa: BLE001 - 单商品探测失败不中断
                logger.debug("suning price probe failed %s: %s", it["prdid"], exc)
        return items

    async def _fetch_jddj(self, keyword: str) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT,
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            ) as client:
                resp = await client.get(
                    f"https://www.jddj.com/search?keyword={quote(keyword)}"
                )
            if resp.status_code >= 400:
                return []
            return parse_jddj_search(resp.text)
        except Exception as exc:  # noqa: BLE001 - 目标不可达诚实留痕
            logger.debug("jddj search failed %s: %s", keyword, exc)
            return []

    async def _fetch_openfoodfacts(self, keyword: str) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT, headers={"User-Agent": "hemall-price-research/1.0"}
            ) as client:
                resp = await client.get(
                    "https://world.openfoodfacts.org/cgi/search.pl",
                    params={
                        "search_terms": keyword,
                        "search_simple": "1",
                        "action": "process",
                        "json": "1",
                        "page_size": "5",
                        "fields": "product_name,quantity,price",
                    },
                )
            if resp.status_code >= 400:
                return []
            return parse_openfoodfacts(resp.json())
        except Exception as exc:  # noqa: BLE001 - 数据源不可达诚实留痕
            logger.debug("openfoodfacts search failed %s: %s", keyword, exc)
            return []

    async def fetch_competitor_prices(
        self, *, lat: float, lon: float, radius_km: int, keywords: list[str]
    ) -> list[dict[str, Any]]:
        """抓取坐标周围的商超 O2O 价格。测试覆写优先；否则按关键词打真实目标。"""
        override = self._overrides.get((lat, lon, radius_km))
        if override is not None:
            return [dict(item) for item in override]

        results: dict[str, dict[str, Any]] = {}
        for keyword in keywords:
            if not keyword or not keyword.strip():
                continue
            fetchers = [
                self._fetch_suning(keyword),
                self._fetch_jddj(keyword),
                self._fetch_openfoodfacts(keyword),
            ]
            try:
                batches = await asyncio.gather(*fetchers[: _MAX_TARGETS_PER_KEYWORD])
            except Exception as exc:  # noqa: BLE001 - 单关键词抓取失败不中断整体
                logger.debug("spider targets failed %s: %s", keyword, exc)
                continue
            for batch in batches:
                for item in batch:
                    title = str(item.get("item", "") or "").strip()
                    if not title:
                        continue
                    key = f"{item.get('source', 'web')}:{title}"
                    if key in results:
                        continue
                    results[key] = {
                        "item": title,
                        "price": item.get("price"),
                        "unit": str(item.get("unit") or ""),
                        "store": str(item.get("store") or "web"),
                    }
        return list(results.values())[: _MAX_ITEMS_PER_KEYWORD]
