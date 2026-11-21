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
from collections.abc import Iterator
from html import unescape as _html_unescape
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
    return items[:_MAX_ITEMS_PER_KEYWORD]


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


def _iter_object_spans(text: str) -> Iterator[str]:
    """字符串感知的花括号配平扫描: 产出文本中每个平衡闭合的 {...} 片段。

    旧 parse_suning_search 的段回扫用 ``{[^{}]*?}`` 正则, 段内一出现嵌套
    对象 (如 ``"keyPoint":{}``) 就断裂——实测新版搜索页里 prdid 因此全空。
    本扫描逐字跟踪字符串状态与转义, 用栈记录每个 ``{`` 的位置, 每个 ``}``
    闭合时产出对应片段, 嵌套多深都不断裂。引号外的花括号 (HTML 正文等)
    产出的片段过不了后续 JSON 解析/字段校验, 会被自然过滤。
    """
    stack: list[int] = []
    in_str = False
    escaped = False
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            yield text[start : i + 1]


def _clean_description(raw: str) -> str:
    """清洗源站介绍文本: 去 HTML 标签/实体, 折叠空白, 限长。"""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = _html_unescape(text)
    return " ".join(text.split())[:500]


def _normalize_suning_image_url(raw: str) -> str:
    # 苏宁搜索图默认是 200w 移动端缩略图；同链接换 800w 即为真 800 大图
    # （2026-10-28 实测 imgservice 800x800 可用），归一化时直接升档，
    # 避免前台再出现小图放大发糊。
    img = (raw or "").strip()
    if not img:
        return ""
    if img.startswith("//"):
        img = f"https:{img}"
    if "suning.cn" in img:
        img = re.sub(r"_\d+w_\d+h", "_800w_800h", img)
    return img


def _normalize_suning_detail_url(raw_path: str, source_id: str) -> str:
    """productPath 归一化为详情页绝对 URL; 缺失时按源站 URL 规则用 ID 拼。"""
    p = (raw_path or "").strip()
    if p.startswith(("http://", "https://")):
        return p
    if p.startswith("//"):
        return f"https:{p}"
    if p.startswith("/m.suning.com/"):
        return f"https://{p[1:]}"
    if p.startswith("/"):
        return f"https://m.suning.com{p}"
    if p:
        return f"https://m.suning.com/{p}"
    if source_id:
        # 源站移动端详情页 URL 规则: /product/<店铺占位 0000000000>/<商品ID>.html
        return f"https://m.suning.com/product/0000000000/{source_id}.html"
    return ""


def _suning_product_from_obj(obj: dict[str, Any]) -> dict[str, Any] | None:
    """从一个含 productName 的 JSON 对象抽完整商品本体; 不是商品对象返回 None。"""
    title = str(obj.get("productName") or "").strip()
    if not title:
        return None
    source_id = str(obj.get("partnumber") or obj.get("catentryId") or "").strip()
    detail_url = _normalize_suning_detail_url(str(obj.get("productPath") or ""), source_id)
    if not source_id:
        # 无稳定 ID 无法做幂等键/detail 兜底, 不上架 (诚实跳过)
        return None
    description = _clean_description(str(obj.get("auxdescription") or obj.get("catentdesc") or ""))
    return {
        "item": title[:120],
        "image_url": _normalize_suning_image_url(str(obj.get("dynamicImg") or "")),
        "description": description,
        "detail_url": detail_url,
        "source_id": source_id,
        "store": str(obj.get("shopName") or "").strip() or "苏宁易购",
        "source": "suning",
    }


_SUNING_FIELD_RES = {
    field: re.compile(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"')
    for field in (
        "productName",
        "partnumber",
        "catentryId",
        "productPath",
        "dynamicImg",
        "auxdescription",
        "catentdesc",
        "shopName",
    )
}


def _suning_product_from_span(span: str) -> dict[str, Any] | None:
    """JSON 解析失败的片段兜底: 在已配平的单商品片段内逐字段正则提取。

    只接受片段自身带 partnumber/catentryId/productPath 之一的片段——父级
    容器片段 (混多个商品) 字段会串味, 宁可跳过不串味上架。
    """
    fields: dict[str, str] = {}
    for field, pattern in _SUNING_FIELD_RES.items():
        m = pattern.search(span)
        if m:
            try:
                fields[field] = json.loads(f'"{m.group(1)}"')
            except (json.JSONDecodeError, ValueError):
                fields[field] = m.group(1)
    if not fields.get("productName"):
        return None
    if not (fields.get("partnumber") or fields.get("catentryId") or fields.get("productPath")):
        return None
    return _suning_product_from_obj(fields)


def _collect_suning_products(node: Any, out: list[dict[str, Any]], seen: set[str]) -> None:
    """递归遍历已解析 JSON, 收集全部商品对象 (容器嵌套多深都收)。"""
    if len(out) >= _MAX_ITEMS_PER_KEYWORD:
        return
    if isinstance(node, dict):
        product = _suning_product_from_obj(node)
        if product is not None and product["source_id"] not in seen:
            seen.add(product["source_id"])
            out.append(product)
        for value in node.values():
            _collect_suning_products(value, out, seen)
    elif isinstance(node, list):
        for value in node:
            _collect_suning_products(value, out, seen)


def parse_suning_products(html: str) -> list[dict[str, Any]]:
    """解析苏宁移动端搜索页, 返回完整商品本体 (不含价格)。

    返回 [{"item","image_url","description","detail_url","source_id",
    "store","source"}, ...]。字段来源 (实测内嵌 JSON): productName=标题、
    partnumber/catentryId=商品 ID、productPath=详情页路径、dynamicImg=
    商品图 (// 协议相对地址补 https:)、auxdescription/catentdesc=介绍、
    shopName=店铺。价格不在这里拿——源站价格接口无价是既成事实, 价格改走
    详情页截图 + 本地 VLM 读价 (spider_listing 引擎填价段)。
    """
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for span in _iter_object_spans(html or ""):
        if '"productName"' not in span or len(span) > 60_000:
            continue
        try:
            parsed = json.loads(span)
        except (json.JSONDecodeError, ValueError):
            product = _suning_product_from_span(span)
            if product is not None and product["source_id"] not in seen:
                seen.add(product["source_id"])
                items.append(product)
            continue
        _collect_suning_products(parsed, items, seen)
        if len(items) >= _MAX_ITEMS_PER_KEYWORD:
            break
    return items[:_MAX_ITEMS_PER_KEYWORD]


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
        :_MAX_ITEMS_PER_KEYWORD
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


def parse_openfoodfacts_products(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 OFP 搜索 JSON 为完整商品本体 (不含价格)。

    字段: product_name=标题、generic_name=介绍、image_url=商品图、
    url=详情页、code=条码 (作 source_id 幂等键; 缺条码时用详情页 URL 兜底)。
    两者都缺的条目没有稳定身份, 跳过。
    """
    items: list[dict[str, Any]] = []
    for p in data.get("products", []):
        name = str(p.get("product_name") or "").strip()
        if not name:
            continue
        detail_url = str(p.get("url") or "").strip()
        source_id = str(p.get("code") or "").strip() or detail_url
        if not source_id:
            continue
        items.append(
            {
                "item": name[:120],
                "image_url": str(p.get("image_url") or "").strip(),
                "description": _clean_description(str(p.get("generic_name") or "")),
                "detail_url": detail_url,
                "source_id": source_id,
                "store": "OpenFoodFacts",
                "source": "off",
            }
        )
    return items[:_MAX_ITEMS_PER_KEYWORD]


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
                resp = await client.get(f"https://m.suning.com/search/{quote(keyword)}/")
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
                resp = await client.get(f"https://www.jddj.com/search?keyword={quote(keyword)}")
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

    async def _fetch_suning_products(self, keyword: str) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT, headers={"User-Agent": _USER_AGENT}, follow_redirects=True
            ) as client:
                resp = await client.get(f"https://m.suning.com/search/{quote(keyword)}/")
                if resp.status_code >= 400:
                    logger.warning("suning products %s -> %d", keyword, resp.status_code)
                    return []
                return parse_suning_products(resp.text)
        except Exception as exc:  # noqa: BLE001 - 目标不可达诚实留痕
            logger.debug("suning products failed %s: %s", keyword, exc)
            return []

    async def _fetch_openfoodfacts_products(self, keyword: str) -> list[dict[str, Any]]:
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
                        "page_size": "12",
                        "fields": "product_name,generic_name,image_url,url,code",
                    },
                )
            if resp.status_code >= 400:
                return []
            return parse_openfoodfacts_products(resp.json())
        except Exception as exc:  # noqa: BLE001 - 数据源不可达诚实留痕
            logger.debug("openfoodfacts products failed %s: %s", keyword, exc)
            return []

    async def fetch_products(self, *, keywords: list[str]) -> list[dict[str, Any]]:
        """抓商品本体 (标题/图片/介绍/详情页) 供自动上架——刻意不带价格。

        价格不走源站接口 (苏宁价格 JSONP 无价、OFP 价格字段覆盖率近零是
        实测结论): 商品先上架, 价格由 spider_listing 引擎截图详情页 +
        本地 VLM 读价补齐。两源 (苏宁 + OpenFoodFacts) 按 (source,
        source_id) 去重, 保持源站顺序。
        """
        results: dict[str, dict[str, Any]] = {}
        for keyword in keywords:
            if not keyword or not keyword.strip():
                continue
            batches = await asyncio.gather(
                self._fetch_suning_products(keyword),
                self._fetch_openfoodfacts_products(keyword),
            )
            for batch in batches:
                for item in batch:
                    title = str(item.get("item", "") or "").strip()
                    source_id = str(item.get("source_id", "") or "").strip()
                    if not title or not source_id:
                        continue
                    key = f"{item.get('source', 'web')}:{source_id}"
                    if key in results:
                        continue
                    results[key] = {
                        "item": title,
                        "image_url": str(item.get("image_url") or ""),
                        "description": str(item.get("description") or ""),
                        "detail_url": str(item.get("detail_url") or ""),
                        "source_id": source_id,
                        "store": str(item.get("store") or "web"),
                        "source": str(item.get("source") or "web"),
                    }
        return list(results.values())

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
                batches = await asyncio.gather(*fetchers[:_MAX_TARGETS_PER_KEYWORD])
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
        return list(results.values())[:_MAX_ITEMS_PER_KEYWORD]
