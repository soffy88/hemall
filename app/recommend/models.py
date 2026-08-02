"""推荐系统数据模型 — 评分矩阵、用户画像、推荐结果结构。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── 用户行为类型枚举 ────────────────────────────────────────────


class UserBehaviorType(Enum):
    """用户行为类型及权重。"""

    VIEW = "view"         # 浏览 (权重 1)
    CART_ADD = "cart_add" # 加购 (权重 3)
    FAVORITE = "favorite" # 收藏 (权重 2)
    PURCHASE = "purchase" # 购买 (权重 5)
    SHARE = "share"       # 分享 (权重 4)


# ── 用户行为事件 ────────────────────────────────────────────────


@dataclass
class UserBehaviorEvent:
    """用户行为日志事件。

    Attributes:
        user_id: 用户 ID
        product_id: 商品 ID
        behavior_type: 行为类型
        timestamp: 发生时间
        context: 上下文信息 (source/position/referer)
    """

    user_id: str
    product_id: str
    behavior_type: UserBehaviorType
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    context: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "product_id": self.product_id,
            "behavior_type": self.behavior_type.value,
            "timestamp": self.timestamp.isoformat(),
            "context": self.context,
        }


# ── 评分矩阵 ─────────────────────────────────────────────────────


class RatingMatrix:
    """隐式反馈评分矩阵 (稀疏矩阵优化)。

    存储格式: Dict[user_id] -> Dict[product_id] -> score
    支持高效查询用户的 TopN 未评分商品
    """

    def __init__(self) -> None:
        self._matrix: dict[str, dict[str, float]] = {}

    def add_rating(self, user_id: str, product_id: str, score: float) -> None:
        if user_id not in self._matrix:
            self._matrix[user_id] = {}
        self._matrix[user_id][product_id] = score

    def get_rating(self, user_id: str, product_id: str) -> float | None:
        users_ratings = self._matrix.get(user_id, {})
        return users_ratings.get(product_id)

    def get_user_ratings(self, user_id: str) -> dict[str, float]:
        return self._matrix.get(user_id, {}).copy()

    def get_all_users(self) -> list[str]:
        return list(self._matrix.keys())


# ── 推荐结果模型 ────────────────────────────────────────────────


class RecommendedProduct(BaseModel):
    """推荐商品详情。"""

    product_id: str
    name: str
    main_image_url: str | None
    selling_price_cents: int | None
    rating_avg: float
    sold_count: int
    reason: str | None = None  # 推荐理由 (如 "与你之前购买的相似")


class RecommendationResult(BaseModel):
    """完整推荐响应。"""

    products: list[RecommendedProduct]
    algorithm: str = "hybrid"  # 使用的算法 (collaborative/content/hybrid)
    total: int = 0
    personalized: bool = True
    refresh_time: datetime = field(default_factory=datetime.now)


class SimilarityScore(BaseModel):
    """相似度得分。"""

    user_id_a: str
    user_id_b: str
    similarity: float  # Pearson correlation [-1, 1] or Cosine [0, 1]


# ── 用户画像 ─────────────────────────────────────────────────────


class UserProfile(BaseModel):
    """用户画像数据模型。"""

    user_id: str
    tags: list[str] = field(default_factory=list)  # 用户标签 (premium/new/intolerant)
    preferred_categories: list[str] = field(default_factory=list)  # 偏好品类
    preferred_brands: list[str] = field(default_factory=list)  # 偏好品牌
    avg_order_value_cents: int = 0  # 客单价
    purchase_frequency_days: int = 30  # 购买频次 (平均间隔天数)
    last_purchase_at: datetime | None = None
    created_at: datetime = field(default_factory=datetime.now)


# ── 商品特征 ─────────────────────────────────────────────────────


class ProductFeatures(BaseModel):
    """商品特征向量 (用于内容-based 推荐)。"""

    product_id: str
    category_id: str
    brand_id: str | None
    price_range: str  # budget/mid/high/premium
    tags: list[str]  # hot/new/sale/promo
    min_price_cents: int | None
    max_price_cents: int | None
    avg_rating: float
    review_count: int
    sold_count: int
    updated_at: datetime = field(default_factory=datetime.now)


# ── 推荐配置 ─────────────────────────────────────────────────────


class RecommendConfig(BaseModel):
    """推荐策略配置。"""

    # 算法权重 (0-1 之间，总和为 1)
    collaborative_weight: float = 0.6
    content_weight: float = 0.3
    trending_weight: float = 0.1  # 热门商品兜底

    # TopK 参数
    top_k_similar: int = 20      # 相似商品数量
    top_k_for_you: int = 50      # 个性化推荐数量
    top_k_hot: int = 10          # 热门兜底数量

    # 冷启动策略
    cold_start_mode: str = "trending"  # trending/popular/newest
    min_common_items_threshold: int = 5  # 最小共同物品数 (用于协同过滤)

    # 实时性控制
    update_interval_seconds: int = 3600  # 评分矩阵刷新间隔
