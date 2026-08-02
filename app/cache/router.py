"""缓存层 API 路由 — 健康检查 + 缓存管理 + 数据查询接口。

Phase 2 Priority 4: 暴露 Redis 缓存管理能力与业务数据缓存接口。

API:
    GET /cache/health                      # 缓存健康状态
    DELETE /cache/prefix/{prefix}          # 按前缀清除缓存
    POST /cache/warmup                     # 触发缓存预热
    GET /inventory/cached                  # 带缓存的库存查询
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import Settings
from ..deps import get_settings
from .service import CacheManager

router = APIRouter(prefix="/cache", tags=["cache"])
logger = logging.getLogger("hemall.cache.router")


def get_cache_manager(settings: Settings = Depends(get_settings)) -> CacheManager:
    """获取缓存管理器实例。"""
    from app.main import get_app_state

    app_state = get_app_state()
    if not hasattr(app_state, "cache_manager") or app_state.cache_manager is None:
        raise HTTPException(status_code=503, detail="CacheManager not initialized")
    return app_state.cache_manager


# ── 缓存管理 ────────────────────────────────────────────────────────


@router.get("/health")
async def cache_health(
    mgr: CacheManager = Depends(get_cache_manager),
) -> dict[str, Any]:
    """缓存服务健康检查。"""
    return await mgr.health_check()


@router.delete("/prefix/{prefix}")
async def flush_cache_prefix(
    prefix: str,
    mgr: CacheManager = Depends(get_cache_manager),
) -> dict[str, int]:
    """按前缀批量删除缓存。"""
    deleted = await mgr.flush_prefix(f"hemall:{prefix}")
    return {"deleted": deleted}


@router.post("/warmup")
async def trigger_warmup(
    warmup_type: str = Query("inventory", description="预热类型: inventory|categories"),
    mgr: CacheManager = Depends(get_cache_manager),
) -> dict[str, Any]:
    """触发缓存预热任务。"""
    if warmup_type == "inventory":
        result = await mgr.warmup_inventory_stock([("WH-SH", "PROD-001"), ("WH-BJ", "PROD-002")])
    elif warmup_type == "categories":
        mock_categories = [
            {
                "id": "cat1",
                "name": "电子产品",
                "children": [
                    {"id": "sub1", "name": "手机", "children": []},
                    {"id": "sub2", "name": "电脑", "children": []},
                ],
            }
        ]
        result = {"warmed": 1, "failed": 0} if await mgr.warmup_categories(mock_categories) else {"warmed": 0, "failed": 1}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown warmup type: {warmup_type}")
    return result


# ── 带缓存的查询接口 ────────────────────────────────────────────────


@router.get("/inventory/{warehouse_code}/{product_code}")
async def get_cached_inventory(
    warehouse_code: str,
    product_code: str,
    mgr: CacheManager = Depends(get_cache_manager),
) -> dict[str, Any]:
    """查询库存 (带缓存加速)。

    流程:
        1. 尝试从 Redis 获取缓存
        2. 缓存未命中 → 返回空响应 (实际应从 DB 查询并回填缓存)
    """
    from .models import CacheKey, CacheRegion

    key = CacheKey.inventory_stock(warehouse_code, product_code)
    cached = await mgr.get(key)
    if cached is not None:
        return {"data": cached, "source": "cache"}

    # 缓存未命中: 应在此处调用数据库查询
    # 为演示目的，返回模拟数据
    return {
        "data": {"available": 0, "reserved": 0, "in_warehouse": 0},
        "source": "empty",
    }
