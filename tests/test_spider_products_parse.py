"""parse_suning_products / parse_openfoodfacts_products / fetch_products 单测。

fixture 按苏宁搜索页内嵌 JSON 的真实结构造: 商品对象与 partnumber /
productPath / dynamicImg / auxdescription / shopName 同段, 且段内带嵌套
花括号 ("keyPoint":{}) —— 旧 parse_suning_search 的段回扫正则正是断在
这里 (prdid 全空); 新解析器用配平扫描, 嵌套不得影响提取。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.ext.spider_targets import (
    LayeredSpiderProvider,
    parse_openfoodfacts_products,
    parse_suning_products,
)

_SUNING_HTML = """
<html><head><title>草莓 - 苏宁易购</title></head><body>
<script>
var searchResult = {"list":[
  {"partnumber":"12451761730","catentryId":"12451761730",
   "productName":"丹东99红颜草莓 新鲜水果 2斤装",
   "productPath":"/product/0000000000/12451761730.html",
   "dynamicImg":"//imgservice3.suning.cn/uimg1/b2c/image/AbC123.jpg",
   "auxdescription":"<p>丹东产地直发, 个大味甜</p>",
   "shopName":"苏宁自营",
   "keyPoint":{"fresh":"产地直发","tags":["冷链","包邮"]},
   "pricePoint":{}},
  {"partnumber":"12451761731",
   "productName":"云南高山香蕉 5斤装",
   "productPath":"/m.suning.com/product/0000000000/12451761731.html",
   "dynamicImg":"//imgservice3.suning.cn/uimg1/b2c/image/XyZ.jpg",
   "catentdesc":"介绍写在 catentdesc 里",
   "keyPoint":{}},
  {"catentryId":"999888777",
   "productName":"只有 catentryId 的商品",
   "dynamicImg":"https://img.example.com/x.jpg",
   "auxdescription":"买{两}斤送一斤",
   "shopName":"第三方旗舰店"},
  {"productName":"没有 ID 的商品", "dynamicImg":"//img.example.com/y.jpg"},
  {"partnumber":"12451761730",
   "productName":"重复 ID 商品 (应被去重)",
   "productPath":"/product/0000000000/12451761730.html"}
],"pageInfo":{"totalCount":5}};
</script>
</body></html>
"""


def test_parse_suning_products_full_extraction() -> None:
    items = parse_suning_products(_SUNING_HTML)

    # 5 条原始数据: 无 ID 跳过 1 条, 重复 ID 去重 1 条 → 3 条
    assert len(items) == 3
    first = items[0]
    assert first["item"] == "丹东99红颜草莓 新鲜水果 2斤装"
    assert first["source_id"] == "12451761730"
    assert first["source"] == "suning"
    assert first["store"] == "苏宁自营"
    # productPath 是站内相对路径 → 拼移动端详情页绝对地址
    assert first["detail_url"] == "https://m.suning.com/product/0000000000/12451761730.html"
    # dynamicImg 协议相对地址补 https:
    assert first["image_url"] == "https://imgservice3.suning.cn/uimg1/b2c/image/AbC123.jpg"
    # 介绍去 HTML 标签
    assert first["description"] == "丹东产地直发, 个大味甜"


def test_parse_suning_products_path_and_desc_variants() -> None:
    items = parse_suning_products(_SUNING_HTML)
    banana = items[1]
    # productPath 自带 /m.suning.com 前缀时不重复拼
    assert banana["detail_url"] == "https://m.suning.com/product/0000000000/12451761731.html"
    # auxdescription 缺失时用 catentdesc; shopName 缺失回退苏宁易购
    assert banana["description"] == "介绍写在 catentdesc 里"
    assert banana["store"] == "苏宁易购"

    third = items[2]
    # 只有 catentryId 也能当 source_id; 无 productPath 按 ID 拼详情页
    assert third["source_id"] == "999888777"
    assert third["detail_url"] == "https://m.suning.com/product/0000000000/999888777.html"
    # 介绍里有花括号字符 (买{两}斤) 不得把对象配平扫断
    assert third["description"] == "买{两}斤送一斤"


def test_parse_suning_products_empty_and_garbage() -> None:
    assert parse_suning_products("") == []
    assert parse_suning_products("<html><body>验证码</body></html>") == []
    assert parse_suning_products('{"productName":"孤儿字段"}') == []  # 无 ID 不收


def test_parse_openfoodfacts_products() -> None:
    data = {
        "products": [
            {
                "product_name": "Nutella 400g",
                "generic_name": "Hazelnut cocoa spread",
                "image_url": "https://images.openfoodfacts.org/nutella.jpg",
                "url": "https://world.openfoodfacts.org/product/3017620422003",
                "code": "3017620422003",
            },
            {
                "product_name": "只有 URL 没条码",
                "url": "https://world.openfoodfacts.org/product/xyz",
            },
            {"product_name": "无 ID 无 URL", "generic_name": "x"},
            {"product_name": "   ", "code": "123"},
        ]
    }
    items = parse_openfoodfacts_products(data)
    assert len(items) == 2
    assert items[0] == {
        "item": "Nutella 400g",
        "image_url": "https://images.openfoodfacts.org/nutella.jpg",
        "description": "Hazelnut cocoa spread",
        "detail_url": "https://world.openfoodfacts.org/product/3017620422003",
        "source_id": "3017620422003",
        "store": "OpenFoodFacts",
        "source": "off",
    }
    # 缺条码时以详情页 URL 兜底作 source_id
    assert items[1]["source_id"] == "https://world.openfoodfacts.org/product/xyz"


async def test_fetch_products_merges_sources_and_dedupes(monkeypatch) -> None:
    provider = LayeredSpiderProvider()
    suning_items = [
        {
            "item": "丹东草莓",
            "image_url": "https://img.example.com/s.jpg",
            "description": "",
            "detail_url": "https://m.suning.com/product/0000000000/111.html",
            "source_id": "111",
            "store": "苏宁自营",
            "source": "suning",
        }
    ]
    off_items = [
        {
            "item": "进口奶",
            "image_url": "",
            "description": "",
            "detail_url": "https://off.org/p/222",
            "source_id": "222",
            "store": "OpenFoodFacts",
            "source": "off",
        }
    ]
    monkeypatch.setattr(provider, "_fetch_suning_products", AsyncMock(return_value=suning_items))
    monkeypatch.setattr(
        provider, "_fetch_openfoodfacts_products", AsyncMock(return_value=off_items)
    )

    # 同一关键词重复给两次: (source, source_id) 去重, 不重复返回
    items = await provider.fetch_products(keywords=["草莓", "草莓", "  "])
    assert len(items) == 2
    assert {it["source"] for it in items} == {"suning", "off"}
    for it in items:
        assert set(it) >= {
            "item",
            "image_url",
            "description",
            "detail_url",
            "source_id",
            "store",
        }
