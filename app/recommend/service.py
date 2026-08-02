"""推荐系统服务层 — 行为收集、模型训练、推荐生成。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from .engine import (
    CollaborativeFilteringEngine,
    ContentBasedEngine,
    HybridRecommender,
    TrendingEngine,
)
from .models import (
    ProductFeatures,
    RecommendConfig,
    RecommendedProduct,
    RecommendationResult,
    UserBehaviorEvent,
    UserBehaviorType,
    UserProfile,
)

logger = logging.getLogger("hemall.recommend.service")


class RecommendationService:
    """推荐系统服务 — 统一入口。

    功能:
        - 收集用户行为日志
        - 训练/更新推荐模型
        - 生成个性化推荐
        - 查询相似商品
        - 热门推荐 (冷启动兜底)
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self._config = RecommendConfig()
        self._hybrid = HybridRecommender(self._config)
        self._behaviors: list[UserBehaviorEvent] = []
        self._user_profiles: dict[str, UserProfile] = {}
        self._product_features: dict[str, ProductFeatures] = {}
        self._redis_url = redis_url
        self._trained = False
        self._background_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动后台训练任务。"""
        self._background_task = asyncio.create_task(self._periodic_training())
        logger.info("RecommendationService started")

    async def stop(self) -> None:
        """停止后台任务。"""
        if self._background_task:
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass
        logger.info("RecommendationService stopped")

    async def _periodic_training(self) -> None:
        """定期训练推荐模型 (每小时)。"""
        while True:
            await asyncio.sleep(self._config.update_interval_seconds)
            if self._behaviors:
                await self.train()
                logger.info(
                    "Recommendation model retrained (%d behaviors)",
                    len(self._behaviors),
                )

    # ── 行为收集 ────────────────────────────────────────────────

    async def log_behavior(self, event: UserBehaviorEvent) -> None:
        """记录用户行为到内存 + 数据库。"""
        self._behaviors.append(event)
        logger.debug(
            "Behavior logged: user=%s product=%s type=%s",
            event.user_id,
            event.product_id,
            event.behavior_type.value,
        )

    async def log_behaviors_batch(self, events: list[UserBehaviorEvent]) -> None:
        """批量记录用户行为。"""
        self._behaviors.extend(events)
        logger.info("Batch logged %d behaviors", len(events))

    # ── 模型训练 ────────────────────────────────────────────────

    async def train(self) -> None:
        """训练/更新推荐模型。

        从已有行为数据重建评分矩阵，更新热门商品列表。内容/热门推荐不依赖行为
        数据 (本来就是冷启动兜底)，即使没有任何行为记录也要用已注册的商品特征
        更新一遍，否则新部署在第一条用户行为出现前 /recommend/hot 会一直空着。
        """
        # 1. 协同过滤模型训练 (需要行为数据)
        if self._behaviors:
            await self._hybrid.collaborative.init_from_behaviors(self._behaviors)
        else:
            logger.info(
                "No behavior data yet; skipping collaborative filtering training"
            )

        # 2. 内容推荐模型训练
        features = list(self._product_features.values())
        await self._hybrid.content.register_products(features)

        # 3. 热门推荐更新
        await self._hybrid.trending.update_trending(features)

        self._trained = True
        logger.info(
            "Recommendation model trained: %d behaviors, %d products",
            len(self._behaviors),
            len(features),
        )

    # ── 推荐生成 ────────────────────────────────────────────────

    async def recommend_for_user(
        self, user_id: str, top_k: int = 20
    ) -> RecommendationResult:
        """为用户生成个性化推荐。

        Args:
            user_id: 用户 ID
            top_k: 推荐数量

        Returns:
            RecommendationResult 包含推荐商品列表
        """
        if not self._trained:
            logger.info("Model not trained yet, using trending")
            recs = await self._hybrid.trending.get_trending(top_k)
            algorithm = "trending"
        else:
            user_profile = self._user_profiles.get(user_id)
            recs = await self._hybrid.recommend_for_user(user_id, user_profile, top_k)
            algorithm = "hybrid"

        # 映射为 RecommendedProduct
        products = []
        for pid, score in recs:
            feat = self._product_features.get(pid)
            products.append(
                RecommendedProduct(
                    product_id=pid,
                    name=feat.product_id if feat else pid,
                    main_image_url=None,
                    selling_price_cents=feat.min_price_cents if feat else None,
                    rating_avg=feat.avg_rating if feat else 0.0,
                    sold_count=feat.sold_count if feat else 0,
                    reason=self._generate_reason(),
                )
            )

        return RecommendationResult(
            products=products,
            algorithm=algorithm,
            total=len(products),
            personalized=True,
            refresh_time=datetime.now(timezone.utc),
        )

    async def find_similar_products(
        self, product_id: str, top_k: int = 10
    ) -> RecommendationResult:
        """查找相似商品 (内容-based)。"""
        recs = await self._hybrid.find_similar_products(product_id, top_k)
        products = []
        for pid, score in recs:
            feat = self._product_features.get(pid)
            products.append(
                RecommendedProduct(
                    product_id=pid,
                    name=feat.product_id if feat else pid,
                    main_image_url=None,
                    selling_price_cents=feat.min_price_cents if feat else None,
                    rating_avg=feat.avg_rating if feat else 0.0,
                    sold_count=feat.sold_count if feat else 0,
                    reason="与您浏览的商品相似",
                )
            )

        return RecommendationResult(
            products=products,
            algorithm="content_based",
            total=len(products),
            personalized=False,
            refresh_time=datetime.now(timezone.utc),
        )

    async def get_trending(self, top_k: int = 20) -> RecommendationResult:
        """获取热门推荐 (冷启动兜底)。"""
        recs = await self._hybrid.trending.get_trending(top_k)
        products = []
        for pid, score in recs:
            feat = self._product_features.get(pid)
            products.append(
                RecommendedProduct(
                    product_id=pid,
                    name=feat.product_id if feat else pid,
                    main_image_url=None,
                    selling_price_cents=feat.min_price_cents if feat else None,
                    rating_avg=feat.avg_rating if feat else 0.0,
                    sold_count=feat.sold_count if feat else 0,
                    reason="当前热门推荐",
                )
            )

        return RecommendationResult(
            products=products,
            algorithm="trending",
            total=len(products),
            personalized=False,
            refresh_time=datetime.now(timezone.utc),
        )

    # ── 数据管理 ────────────────────────────────────────────────

    async def register_product_features(self, features: list[ProductFeatures]) -> None:
        """注册/更新商品特征。"""
        for f in features:
            self._product_features[f.product_id] = f
        logger.info("Registered %d product features", len(features))

    async def register_user_profile(self, profile: UserProfile) -> None:
        """注册用户画像。"""
        self._user_profiles[profile.user_id] = profile

    async def get_stats(self) -> dict[str, Any]:
        """推荐系统统计信息。"""
        return {
            "trained": self._trained,
            "behaviors_count": len(self._behaviors),
            "products_count": len(self._product_features),
            "users_count": len(self._user_profiles),
            "config": self._config.model_dump(mode="json"),
        }

    def _generate_reason(self) -> str:
        """生成推荐理由 (随机选择)。"""
        import random

        reasons = [
            "基于您的浏览历史推荐",
            "与您购买过的商品相似",
            "您可能也喜欢",
            "同类商品热销推荐",
            "为您精选",
        ]
        return random.choice(reasons)
