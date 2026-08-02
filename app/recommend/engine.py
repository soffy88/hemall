"""推荐算法引擎 — 协同过滤 + 内容推荐 + 混合推荐。

Phase 3 Priority 1: 实现多种推荐算法，通过加权融合提供最佳结果。

算法选择:
    - 协同过滤: 用户-商品评分矩阵 + Pearson 相似度
    - 内容推荐: 商品特征向量 + Cosine 相似度
    - 混合推荐: 加权融合 + 热门兜底
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict
from typing import Any

from .models import (
    ProductFeatures,
    RatingMatrix,
    RecommendConfig,
    RecommendedProduct,
    RecommendationResult,
    UserBehaviorEvent,
    UserBehaviorType,
    UserProfile,
)

logger = logging.getLogger("hemall.recommend.engine")


class CollaborativeFilteringEngine:
    """基于用户的协同过滤推荐引擎。

    核心思路:
        1. 构建用户-商品评分矩阵 (隐式反馈)
        2. 计算用户间的 Pearson 相似度
        3. 为目标用户找到最相似的 K 个用户
        4. 预测目标用户对未互动商品的评分
        5. 按预测评分排序推荐 TopN
    """

    def __init__(self, min_common_items: int = 5):
        self._rating_matrix = RatingMatrix()
        self._user_similarity_cache: dict[str, list[tuple[str, float]]] = {}
        self._min_common_items = min_common_items

    async def init_from_behaviors(self, behaviors: list[UserBehaviorEvent]) -> None:
        """从用户行为日志初始化评分矩阵。"""
        self._rating_matrix = RatingMatrix()
        for b in behaviors:
            score = self._behavior_to_score(b.behavior_type)
            self._rating_matrix.add_rating(b.user_id, b.product_id, score)
        logger.info("Rating matrix built: %d users, %d items",
                     len(self._rating_matrix.get_all_users()),
                     sum(len(ratings) for ratings in self._rating_matrix._matrix.values()))

    def _behavior_to_score(self, behavior_type: UserBehaviorType) -> float:
        """将用户行为类型映射为评分权重。

        隐式反馈权重设计:
            - 浏览: 1 (低意图)
            - 收藏: 2 (中期意图)
            - 加购: 3 (高意图)
            - 分享: 4 (高社交价值)
            - 购买: 5 (最高意图)
        """
        mapping = {
            UserBehaviorType.VIEW: 1.0,
            UserBehaviorType.FAVORITE: 2.0,
            UserBehaviorType.CART_ADD: 3.0,
            UserBehaviorType.SHARE: 4.0,
            UserBehaviorType.PURCHASE: 5.0,
        }
        return mapping.get(behavior_type, 1.0)

    def _pearson_similarity(
        self, ratings_a: dict[str, float], ratings_b: dict[str, float]
    ) -> float:
        """计算两个用户间的 Pearson 相关系数。

        Pearson 公式:
            r = Σ((x_i - x̄)(y_i - ȳ)) / sqrt(Σ(x_i - x̄)² * Σ(y_i - ȳ)²)
        """
        common_items = set(ratings_a.keys()) & set(ratings_b.keys())
        if len(common_items) < self._min_common_items:
            return 0.0

        n = len(common_items)
        # 计算均值
        sum_a = sum(ratings_a[item] for item in common_items)
        sum_b = sum(ratings_b[item] for item in common_items)
        mean_a = sum_a / n
        mean_b = sum_b / n

        # 计算 Pearson 分子分母
        numerator = 0.0
        denom_a = 0.0
        denom_b = 0.0

        for item in common_items:
            diff_a = ratings_a[item] - mean_a
            diff_b = ratings_b[item] - mean_b
            numerator += diff_a * diff_b
            denom_a += diff_a ** 2
            denom_b += diff_b ** 2

        denominator = math.sqrt(denom_a * denom_b)
        if denominator == 0:
            return 0.0

        return numerator / denominator

    async def find_similar_users(
        self, target_user_id: str, top_k: int = 20
    ) -> list[tuple[str, float]]:
        """找到与目标用户最相似的 K 个用户。

        Returns:
            [(similar_user_id, similarity_score), ...]
        """
        # 优先使用缓存
        cache_key = f"{target_user_id}:{top_k}"
        if cache_key in self._user_similarity_cache:
            return self._user_similarity_cache[cache_key]

        target_ratings = self._rating_matrix.get_user_ratings(target_user_id)
        if not target_ratings:
            return []

        similarities: list[tuple[str, float]] = []
        for other_user in self._rating_matrix.get_all_users():
            if other_user == target_user_id:
                continue
            other_ratings = self._rating_matrix.get_user_ratings(other_user)
            sim = self._pearson_similarity(target_ratings, other_ratings)
            if sim > 0:
                similarities.append((other_user, sim))

        # 排序取 TopK
        similarities.sort(key=lambda x: x[1], reverse=True)
        result = similarities[:top_k]

        # 缓存结果
        self._user_similarity_cache[cache_key] = result
        return result

    async def predict_score(
        self, target_user_id: str, product_id: str, similar_users: list[tuple[str, float]]
    ) -> float:
        """预测目标用户对商品的评分。

        加权平均公式:
            pred = avg_rating + Σ(sim * (rating - avg_rating)) / Σ(|sim|)
        """
        target_ratings = self._rating_matrix.get_user_ratings(target_user_id)
        target_avg = sum(target_ratings.values()) / len(target_ratings) if target_ratings else 3.0

        numerator = 0.0
        denominator = 0.0

        for other_user, similarity in similar_users:
            other_ratings = self._rating_matrix.get_user_ratings(other_user)
            if product_id in other_ratings:
                other_avg = sum(other_ratings.values()) / len(other_ratings)
                numerator += similarity * (other_ratings[product_id] - other_avg)
                denominator += abs(similarity)

        if denominator == 0:
            return target_avg

        return target_avg + (numerator / denominator)

    async def recommend_for_user(
        self, target_user_id: str, top_k: int = 20
    ) -> list[tuple[str, float]]:
        """为用户推荐 TopK 商品 (协同过滤)。

        Returns:
            [(product_id, predicted_score), ...]
        """
        target_ratings = self._rating_matrix.get_user_ratings(target_user_id)
        similar_users = await self.find_similar_users(target_user_id)

        if not similar_users:
            return []

        # 找出所有候选商品 (相似用户购买过但目标用户未购买的)
        candidate_products: dict[str, float] = {}
        for other_user, _ in similar_users:
            other_ratings = self._rating_matrix.get_user_ratings(other_user)
            for product_id, score in other_ratings.items():
                if product_id not in target_ratings:
                    if product_id not in candidate_products:
                        candidate_products[product_id] = 0.0

        # 预测评分并排序
        scored_products = []
        for product_id in candidate_products:
            pred_score = await self.predict_score(target_user_id, product_id, similar_users)
            scored_products.append((product_id, pred_score))

        scored_products.sort(key=lambda x: x[1], reverse=True)
        return scored_products[:top_k]


class ContentBasedEngine:
    """基于内容的推荐引擎。

    核心思路:
        1. 构建商品特征向量 (品类/品牌/价格区间/标签)
        2. 计算商品间的 Cosine 相似度
        3. 根据用户已购/喜好的商品特征，推荐相似商品
    """

    def __init__(self) -> None:
        self._product_features: dict[str, ProductFeatures] = {}
        self._item_similarity_cache: dict[str, list[tuple[str, float]]] = {}

    async def register_products(self, features: list[ProductFeatures]) -> None:
        """注册商品特征到引擎。"""
        for f in features:
            self._product_features[f.product_id] = f
        logger.info("Content engine: %d products registered", len(self._product_features))

    def _extract_feature_vector(self, product: ProductFeatures) -> dict[str, int]:
        """将商品特征映射为稀疏向量。

        特征维度:
            - category_id (one-hot)
            - brand_id (one-hot)
            - price_range (one-hot)
            - tags (multi-hot)
            - rating (binned)
        """
        vector: dict[str, int] = {}
        vector[f"cat:{product.category_id}"] = 1
        if product.brand_id:
            vector[f"brand:{product.brand_id}"] = 1
        vector[f"price:{product.price_range}"] = 1
        for tag in product.tags:
            vector[f"tag:{tag}"] = 1
        # 评分分箱
        rating_bin = int(product.avg_rating)
        vector[f"rating:{rating_bin}"] = 1
        return vector

    def _cosine_similarity(
        self, vec_a: dict[str, int], vec_b: dict[str, int]
    ) -> float:
        """计算两个特征向量的 Cosine 相似度。

        Cosine 公式:
            sim = (A · B) / (||A|| * ||B||)
        """
        common_keys = set(vec_a.keys()) & set(vec_b.keys())
        if not common_keys:
            return 0.0

        dot_product = sum(vec_a[k] * vec_b[k] for k in common_keys)
        norm_a = math.sqrt(sum(v ** 2 for v in vec_a.values()))
        norm_b = math.sqrt(sum(v ** 2 for v in vec_b.values()))

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot_product / (norm_a * norm_b)

    async def find_similar_products(
        self, product_id: str, top_k: int = 20
    ) -> list[tuple[str, float]]:
        """找到与目标商品最相似的 K 个商品。

        Returns:
            [(similar_product_id, similarity_score), ...]
        """
        # 缓存检查
        cache_key = f"{product_id}:{top_k}"
        if cache_key in self._item_similarity_cache:
            return self._item_similarity_cache[cache_key]

        target = self._product_features.get(product_id)
        if not target:
            return []

        target_vec = self._extract_feature_vector(target)
        similarities: list[tuple[str, float]] = []

        for other_id, other_features in self._product_features.items():
            if other_id == product_id:
                continue
            other_vec = self._extract_feature_vector(other_features)
            sim = self._cosine_similarity(target_vec, other_vec)
            if sim > 0:
                similarities.append((other_id, sim))

        similarities.sort(key=lambda x: x[1], reverse=True)
        result = similarities[:top_k]

        # 缓存
        self._item_similarity_cache[cache_key] = result
        return result

    async def recommend_for_user(
        self, liked_products: list[str], top_k: int = 20
    ) -> list[tuple[str, float]]:
        """根据用户喜好的商品列表推荐相似商品。

        Args:
            liked_products: 用户已购/喜好的商品 ID 列表
            top_k: 推荐数量

        Returns:
            [(product_id, avg_similarity), ...]
        """
        if not liked_products:
            return []

        # 聚合所有相似商品
        score_map: dict[str, list[float]] = defaultdict(list)
        for liked_id in liked_products:
            similar = await self.find_similar_products(liked_id, top_k=top_k)
            for other_id, sim in similar:
                if other_id not in liked_products:
                    score_map[other_id].append(sim)

        # 计算平均相似度
        avg_scores = [(pid, sum(scores) / len(scores)) for pid, scores in score_map.items()]
        avg_scores.sort(key=lambda x: x[1], reverse=True)
        return avg_scores[:top_k]


class TrendingEngine:
    """热门商品推荐引擎 (冷启动兜底)。

    基于全局销售数据 + 评分数据，推荐当前最热门的商品。
    适用于新用户/无历史行为用户。
    """

    def __init__(self) -> None:
        self._trending_cache: list[tuple[str, float]] = []

    async def update_trending(self, products: list[ProductFeatures]) -> None:
        """更新热门商品列表 (按销量+评分综合排序)。"""
        scored = []
        for p in products:
            # 综合热度评分: 销量 * 0.7 + 评分 * 0.3
            hot_score = p.sold_count * 0.7 + p.avg_rating * p.review_count * 0.3
            scored.append((p.product_id, hot_score))

        scored.sort(key=lambda x: x[1], reverse=True)
        self._trending_cache = scored[:100]
        logger.info("Trending engine updated: %d products", len(self._trending_cache))

    async def get_trending(self, top_k: int = 20) -> list[tuple[str, float]]:
        """获取热门推荐。"""
        return self._trending_cache[:top_k]


class HybridRecommender:
    """混合推荐引擎 — 加权融合多种算法。

    推荐策略:
        1. 有历史行为用户: 协同过滤 60% + 内容推荐 30% + 热门兜底 10%
        2. 新用户 (冷启动): 热门推荐 100%
        3. 商品详情页: 内容推荐 100% (相似商品)
    """

    def __init__(self, config: RecommendConfig | None = None) -> None:
        self._collaborative = CollaborativeFilteringEngine()
        self._content = ContentBasedEngine()
        self._trending = TrendingEngine()
        self._config = config or RecommendConfig()

    async def recommend_for_user(
        self,
        user_id: str,
        user_profile: UserProfile | None = None,
        top_k: int = 20,
    ) -> list[tuple[str, float]]:
        """为用户生成个性化推荐。

        Args:
            user_id: 目标用户 ID
            user_profile: 用户画像 (可选)
            top_k: 推荐数量

        Returns:
            [(product_id, composite_score), ...]
        """
        ratings = self._collaborative._rating_matrix.get_user_ratings(user_id)

        # 冷启动: 无行为数据时返回热门推荐
        if not ratings:
            logger.info("Cold start for user %s, using trending", user_id)
            return await self._trending.get_trending(top_k)

        # 协同过滤推荐
        collab_recs = await self._collaborative.recommend_for_user(user_id, top_k)
        collab_map = {pid: score for pid, score in collab_recs}

        # 内容推荐 (基于已购商品)
        liked_products = sorted(ratings, key=ratings.get, reverse=True)[:10]
        content_recs = await self._content.recommend_for_user(liked_products, top_k)
        content_map = {pid: score for pid, score in content_recs}

        # 热门兜底
        trending_recs = await self._trending.get_trending(top_k)
        trending_map = {pid: score for pid, score in trending_recs}

        # 加权融合
        hybrid_scores: dict[str, float] = defaultdict(float)
        all_products = set(list(collab_map.keys()) + list(content_map.keys()) + list(trending_map.keys()))

        for pid in all_products:
            score = 0.0
            score += collab_map.get(pid, 0) * self._config.collaborative_weight
            score += content_map.get(pid, 0) * self._config.content_weight
            score += trending_map.get(pid, 0) * self._config.trending_weight
            hybrid_scores[pid] = score

        # 排序
        sorted_recs = sorted(hybrid_scores.items(), key=lambda x: x[1], reverse=True)
        return sorted_recs[:top_k]

    async def find_similar_products(
        self, product_id: str, top_k: int = 10
    ) -> list[tuple[str, float]]:
        """查找相似商品 (内容-based)。"""
        return await self._content.find_similar_products(product_id, top_k)

    @property
    def collaborative(self) -> CollaborativeFilteringEngine:
        return self._collaborative

    @property
    def content(self) -> ContentBasedEngine:
        return self._content

    @property
    def trending(self) -> TrendingEngine:
        return self._trending