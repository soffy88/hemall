"""缓存层数据模型 — Key 模板、TTL 策略、缓存区域枚举。

定义:
    - CacheKey: 结构化 key 生成器
    - CacheRegion: 不同数据的 TTL 分级
    - 缓存前缀规范
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class CacheRegion(Enum):
    """缓存区域 — 按数据时效性划分 TTL 策略。"""

    # 热点数据 (频繁读取，少量更新)
    PRODUCT_DETAIL = "product:detail"
    PRODUCT_LISTING = "product:listing"
    INVENTORY_STOCK = "inventory:stock"

    # 热数据 (定期更新)
    USER_PROFILE = "user:profile"
    CART_DATA = "cart:data"

    # 温数据 (不常访问)
    CATEGORY_TREE = "category:tree"
    BRAND_INFO = "brand:info"

    # 低频数据 (长 TTL)
    ORDER_HISTORY = "order:history"


@dataclass(frozen=True)
class CacheConfig:
    """缓存配置。"""

    ttl_seconds: int
    prefix: str
    max_memory_mb: int = 512
    evict_policy: str = "allkeys-lru"


# ── TTL 策略 ────────────────────────────────────────────────


CACHE_TTL_MAP: dict[CacheRegion, int] = {
    CacheRegion.PRODUCT_DETAIL: 300,     # 商品详情 5 min
    CacheRegion.PRODUCT_LISTING: 60,      # 列表缓存 1 min
    CacheRegion.INVENTORY_STOCK: 30,      # 库存余量 30s (高频变动)
    CacheRegion.USER_PROFILE: 3600,       # 用户信息 1 h
    CacheRegion.CART_DATA: 900,           # 购物车数据 15 min
    CacheRegion.CATEGORY_TREE: 7200,      # 分类树 2 h
    CacheRegion.BRAND_INFO: 3600,         # 品牌信息 1 h
    CacheRegion.ORDER_HISTORY: 86400,     # 订单历史 24 h
}

# ── Key 模板 ────────────────────────────────────────────────────


class CacheKey:
    """缓存 key 生成器。

    命名规范: hemall:{region}:{entity_id}:{extra}
    示例:
        hemall:product:detail:sku-123          → 商品详情
        hemall:inventory:stock:WH-SH/PROD-456  → 仓库库存
        hemall:user:profile:user-789           → 用户画像
    """

    PREFIX = "hemall"

    @classmethod
    def product_detail(cls, sku_id: str) -> str:
        return f"{cls.PREFIX}:product:detail:{sku_id}"

    @classmethod
    def inventory_stock(cls, warehouse_code: str, product_code: str) -> str:
        return f"{cls.PREFIX}:inventory:stock:{warehouse_code}/{product_code}"

    @classmethod
    def category_tree(cls) -> str:
        return f"{cls.PREFIX}:category:tree:root"

    @classmethod
    def brand_info(cls, brand_id: str) -> str:
        return f"{cls.PREFIX}:brand:info:{brand_id}"

    @classmethod
    def user_profile(cls, user_id: str) -> str:
        return f"{cls.PREFIX}:user:profile:{user_id}"

    @classmethod
    def cart_data(cls, user_id: str) -> str:
        return f"{cls.PREFIX}:cart:data:{user_id}"
