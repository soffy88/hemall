"""分类映射单测: 最长命中优先、非食品标记、新增数码规则、未映射返回 None。"""

from __future__ import annotations

import pytest

from app.ext.oservi import _map_spider_listing_category

CATS = [
    {"id": "frozen", "name": "冷冻速食"},
    {"id": "drink", "name": "酒水饮料"},
    {"id": "dairy", "name": "乳品烘焙"},
    {"id": "seafood", "name": "海鲜水产"},
    {"id": "meat", "name": "肉禽蛋"},
    {"id": "fruit", "name": "时令水果"},
    {"id": "veg", "name": "蔬菜豆品"},
    {"id": "grain", "name": "粮油调味"},
    {"id": "snack", "name": "休闲零食"},
    {"id": "daily", "name": "日用百货"},
    {"id": "elec", "name": "数码家电"},
]


@pytest.mark.parametrize(
    ("title", "want"),
    [
        ("新鲜菠菜 500g", "veg"),
        ("酥田东北盘锦蟹稻共生大米2.5KG", "grain"),
        ("盘锦蟹田大米500克 当季新米", "grain"),
        ("大闸蟹提货券", "seafood"),
        ("哈铁亿农 猪五花肉 500g", "meat"),
        ("哈铁亿农 刀鱼 1kg", "seafood"),
        ("去骨鸡腿肉排 麻辣味", "meat"),
        ("云南大青枣 净重3斤", "fruit"),
        ("云南新鲜四季豆 净重3斤", "veg"),
        ("恒顺白醋 纯酿造 2L", "grain"),
        ("农夫山泉100%NFC橙汁", "drink"),
        ("佳果源 椰子水 350ml", "drink"),
        ("纯山羊奶 250ml", "dairy"),
        ("美的 养生壶 1.7L", "elec"),
        ("Apple iPhone 18 Pro Max 256G", "elec"),
        ("Apple iPad Air M4 平板电脑", "elec"),
        ("手剥巴旦木", "snack"),
        ("龙口粉丝", "grain"),
        ("速冻水饺", "frozen"),
        ("手抓饼", "frozen"),
        ("抽纸", "daily"),
        ("新鲜水果黄瓜 净重3斤", "veg"),
        ("青蛙王子宝宝益生菌牙膏草莓奶昔", "daily"),
        ("伊利臻浓牛奶 咖啡伴侣", "dairy"),
        ("统一茄皇方便面番茄鸡蛋面", "snack"),
        ("千味央厨饺子白菜猪肉馅", "frozen"),
        ("想念宝宝菠菜面挂面", "grain"),
        ("来伊份爆汁豆腐豆干零食", "snack"),
    ],
)
def test_map_spider_category(title: str, want: str) -> None:
    assert _map_spider_listing_category(title, CATS) == want


@pytest.mark.parametrize(
    "title",
    [
        "[M]大作家的语文课:胡萝卜先生的长胡子-9787531355014",
        "正版 西兰花菜心节本高效栽培 9787535956965",
        "元宵灯 中国节日绘本",
        "新全新 定制产品勿拍1",
        "FANTASTIC 翻篇儿",
        "元宵主题储值卡 礼品卡",
        "蔬菜种子四季播种香菜菠菜生菜",
    ],
)
def test_map_spider_category_unmapped_is_none(title: str) -> None:
    assert _map_spider_listing_category(title, CATS) is None
