"""商品搜索引擎 API 路由 — RESTful 搜索 + 聚合分析 + 索引管理。

Phase 2: 暴露 Elasticsearch 检索能力给前端和其他服务。

API:
    GET /search/products          # 商品搜索 (关键词 + 筛选)
    GET /search/autocomplete      # 搜索建议
    POST /search/sync/product     # 手动同步单个商品
    DELETE /search/index/{product_id}  # 删除商品索引
    GET /search/stats             # 统计信息
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import Settings
from ..deps import get_current_user, get_settings
from .client import ElasticsearchClient
from .models import SearchQueryParams
from .service import SearchService

# 后台搜索运维 API：重建索引 (sync)、删索引、看内部统计。顾客商城搜索走
# /store/products，不经此处，故一律要求员工 JWT。
router = APIRouter(
    prefix="/search", tags=["search"], dependencies=[Depends(get_current_user)]
)
logger = logging.getLogger("hemall.search.router")


def get_search_service(
    settings: Settings = Depends(get_settings),
) -> SearchService:
    """获取搜索服务实例 (懒初始化)。"""
    from app.main import get_app_state  # 避免循环导入

    # 从应用状态获取已初始化的客户端
    app_state = get_app_state()
    if not hasattr(app_state, "search_client") or app_state.search_client is None:
        raise HTTPException(status_code=503, detail="Elasticsearch not initialized")

    if not hasattr(app_state, "search_service") or app_state.search_service is None:
        app_state.search_service = SearchService.create(
            app_state.search_client, settings
        )

    return app_state.search_service


# ── 商品搜索 ────────────────────────────────────────────────────────


@router.get("/products")
async def search_products(
    keyword: str | None = Query(None, description="搜索关键词"),
    category_id: str | None = Query(None, description="分类 ID"),
    brand_id: str | None = Query(None, description="品牌 ID"),
    price_min: int | None = Query(None, ge=0, description="最低价格 (分)"),
    price_max: int | None = Query(None, ge=0, description="最高价格 (分)"),
    min_rating: float | None = Query(None, ge=0, le=5, description="最低评分"),
    in_stock_only: bool = Query(False, description="仅显示有货"),
    sort_by: str = Query(
        "relevance", pattern="^(relevance|price_newest|sold_desc|rating)$"
    ),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(20, ge=1, le=100, description="每页条数"),
    facets: str = Query("category,brand,price_range", description="聚合维度，逗号分隔"),
    svc: SearchService = Depends(get_search_service),
) -> dict[str, Any]:
    """商品搜索 API。"""
    facet_list = [f.strip() for f in facets.split(",")]
    params = SearchQueryParams(
        keyword=keyword,
        category_id=category_id,
        brand_id=brand_id,
        price_min_cents=price_min,
        price_max_cents=price_max,
        min_rating=min_rating,
        in_stock_only=in_stock_only,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        size=size,
        facets=facet_list,
    )

    result = await svc.search(params)
    return {
        "data": result.model_dump(mode="json"),
    }


# ── 自动补全 ────────────────────────────────────────────────────────


@router.get("/autocomplete")
async def autocomplete(
    prefix: str = Query(..., min_length=1, max_length=50, description="输入前缀"),
    limit: int = Query(10, ge=1, le=50, description="返回条数"),
    svc: SearchService = Depends(get_search_service),
) -> dict[str, Any]:
    """搜索建议 / 自动补全。"""
    request = type("AutoCompleteRequest", (), {"prefix": prefix, "limit": limit})()
    result = await svc.autocomplete(request)
    return {"data": result.model_dump(mode="json")}


# ── 索引管理 (管理员接口) ───────────────────────────────────────────


@router.post("/sync/product")
async def sync_product_index(
    product_id: str = Query(..., description="商品 ID"),
    svc: SearchService = Depends(get_search_service),
) -> dict[str, Any]:
    """手动触发单个商品同步到 ES。"""
    success = await svc.sync_product(product_id)
    return {"status": "synced" if success else "failed", "product_id": product_id}


@router.delete("/index/{product_id}")
async def delete_product_index(
    product_id: str,
    svc: SearchService = Depends(get_search_service),
) -> dict[str, Any]:
    """删除商品 ES 索引。"""
    success = await svc.remove_product(product_id)
    return {"status": "deleted" if success else "not_found", "product_id": product_id}


@router.get("/stats")
async def search_stats(
    svc: SearchService = Depends(get_search_service),
) -> dict[str, Any]:
    """搜索服务健康检查与统计。"""
    if not svc._client._es:
        return {"status": "degraded", "message": "Elasticsearch not available"}

    try:
        health = await svc._client._es.cluster.health()
        return {
            "status": "healthy",
            "cluster": health.get("cluster_name"),
            "nodes": health.get("number_of_nodes"),
            "active_shards": health.get("active_shards"),
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
