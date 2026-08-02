"""推荐系统 API 路由 — 个性化推荐、相似商品、热门推荐。

Phase 3 Priority 1: 为用户推荐个性化商品，提升转化率。

API:
    GET /recommend/home          # 首页个性化推荐
    GET /recommend/hot           # 热门推荐 (冷启动)
    GET /recommend/{product_id}/similar  # 相似商品
    GET /recommend/stats         # 推荐系统统计
    POST /recommend/behavior     # 记录用户行为
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import Settings
from ..deps import get_settings
from .models import UserBehaviorEvent, UserBehaviorType
from .service import RecommendationService

router = APIRouter(prefix="/recommend", tags=["recommend"])
logger = logging.getLogger("hemall.recommend.router")


def get_recommend_service(
    settings: Settings = Depends(get_settings),
) -> RecommendationService:
    """获取推荐服务实例。"""
    from app.main import get_app_state

    app_state = get_app_state()
    if not hasattr(app_state, "recommend_service") or app_state.recommend_service is None:
        raise HTTPException(status_code=503, detail="RecommendationService not initialized")
    return app_state.recommend_service


@router.get("/home")
async def home_recommendations(
    user_id: str = Query(..., description="用户 ID"),
    top_k: int = Query(20, ge=1, le=50, description="推荐数量"),
    svc: RecommendationService = Depends(get_recommend_service),
) -> dict[str, Any]:
    """首页个性化推荐。"""
    result = await svc.recommend_for_user(user_id, top_k)
    return {"data": result.model_dump(mode="json")}


@router.get("/hot")
async def hot_recommendations(
    top_k: int = Query(20, ge=1, le=50, description="数量"),
    svc: RecommendationService = Depends(get_recommend_service),
) -> dict[str, Any]:
    """热门推荐 (冷启动兜底)。"""
    result = await svc.get_trending(top_k)
    return {"data": result.model_dump(mode="json")}


@router.get("/{product_id}/similar")
async def similar_products(
    product_id: str,
    top_k: int = Query(10, ge=1, le=30, description="数量"),
    svc: RecommendationService = Depends(get_recommend_service),
) -> dict[str, Any]:
    """查找相似商品 (内容-based)。"""
    result = await svc.find_similar_products(product_id, top_k)
    return {"data": result.model_dump(mode="json")}


@router.post("/behavior")
async def log_behavior(
    user_id: str = Query(..., description="用户 ID"),
    product_id: str = Query(..., description="商品 ID"),
    behavior_type: str = Query(..., description="行为类型: view/cart_add/favorite/purchase/share"),
    svc: RecommendationService = Depends(get_recommend_service),
) -> dict[str, Any]:
    """记录用户行为 (用于训练推荐模型)。"""
    try:
        btype = UserBehaviorType(behavior_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid behavior_type: {behavior_type}. Must be one of: view/cart_add/favorite/purchase/share",
        )

    event = UserBehaviorEvent(
        user_id=user_id,
        product_id=product_id,
        behavior_type=btype,
    )
    await svc.log_behavior(event)
    return {"status": "logged", "user_id": user_id, "product_id": product_id, "behavior_type": behavior_type}


@router.get("/stats")
async def recommend_stats(
    svc: RecommendationService = Depends(get_recommend_service),
) -> dict[str, Any]:
    """推荐系统统计信息。"""
    stats = await svc.get_stats()
    return {"data": stats}