"""首页轮播图/横幅管理 API。

功能：
- 后台管理轮播图（增删改查、排序、启用/禁用）
- 顾客端获取轮播图列表（仅返回启用的）
- 存储在 runtime_setting 表中，key: banner.settings.v1
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .deps import assert_staff_role, get_current_user

router = APIRouter(prefix="/store/banner", tags=["banner"])
admin_router = APIRouter(prefix="/admin/banner", tags=["admin-banner"])

SETTING_KEY = "banner.settings.v1"

STAFF_ROLES = frozenset({"admin", "support", "staff"})
ADMIN_ROLES = frozenset({"admin", "support"})


class BannerItem(BaseModel):
    """轮播图项"""

    id: str = Field(default_factory=lambda: f"banner_{secrets.token_hex(4)}")
    image_url: str = ""
    link_type: str = "none"  # none, product, category, pintuan, pickup, url
    link_target: str = ""  # 目标 ID 或 URL
    title: str = ""
    subtitle: str = ""
    sort_order: int = 0
    is_active: bool = True
    start_at: str | None = None  # ISO datetime
    end_at: str | None = None  # ISO datetime


def default_banner_settings() -> dict[str, Any]:
    """默认轮播图配置（占位值）。"""
    return {
        "banners": [
            {
                "id": "banner_default_1",
                "image_url": "/static/images/banner-placeholder-1.png",
                "link_type": "none",
                "link_target": "",
                "title": "宋鲜生 · 新鲜到家",
                "subtitle": "社区生鲜 30 分钟送达",
                "sort_order": 0,
                "is_active": True,
                "start_at": None,
                "end_at": None,
            },
            {
                "id": "banner_default_2",
                "image_url": "/static/images/banner-placeholder-2.png",
                "link_type": "none",
                "link_target": "",
                "title": "社区拼单 · 更优惠",
                "subtitle": "邻里凑单 享团购价",
                "sort_order": 1,
                "is_active": True,
                "start_at": None,
                "end_at": None,
            },
        ],
        "note": "占位配置，请在后台配置真实轮播图",
    }


def _clean_banners(raw: Any) -> dict[str, Any]:
    """清洗后台写入的轮播图配置。"""
    base = default_banner_settings()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    if not isinstance(raw, dict):
        return base

    banners = raw.get("banners")
    if isinstance(banners, list):
        cleaned = []
        for b in banners:
            if not isinstance(b, dict):
                continue
            cleaned.append(
                {
                    "id": str(b.get("id", "")) or f"banner_{secrets.token_hex(4)}",
                    "image_url": str(b.get("image_url", "")).strip(),
                    "link_type": str(b.get("link_type", "none")).strip(),
                    "link_target": str(b.get("link_target", "")).strip(),
                    "title": str(b.get("title", "")).strip(),
                    "subtitle": str(b.get("subtitle", "")).strip(),
                    "sort_order": int(b.get("sort_order", 0)),
                    "is_active": bool(b.get("is_active", True)),
                    "start_at": b.get("start_at") if b.get("start_at") else None,
                    "end_at": b.get("end_at") if b.get("end_at") else None,
                }
            )
        # 按 sort_order 排序
        cleaned.sort(key=lambda x: x["sort_order"])
        base["banners"] = cleaned

    if raw.get("note"):
        base["note"] = str(raw["note"])

    return base


async def load_banner_settings(pool: Any) -> dict[str, Any]:
    if pool is None:
        return default_banner_settings()
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT value FROM runtime_setting WHERE key = $1", SETTING_KEY
            )
    except Exception:
        return default_banner_settings()
    return _clean_banners(raw)


async def save_banner_settings(
    pool: Any, cfg: dict[str, Any], *, updated_by: str
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO runtime_setting (key, value, updated_by, updated_at)
            VALUES ($1, $2::jsonb, $3, NOW())
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = NOW()
            """,
            SETTING_KEY,
            json.dumps(cfg, ensure_ascii=False),
            updated_by,
        )
    return cfg


# 权限依赖
def _admin(principal: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return assert_staff_role(principal, ADMIN_ROLES)


def _staff(principal: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return assert_staff_role(principal, STAFF_ROLES)


# ── 后台管理端点 ──────────────────────────────────────────────────────


@admin_router.get("/settings")
async def get_banner_settings(request: Request, _=Depends(_admin)) -> dict[str, Any]:
    """获取轮播图配置（后台完整版，含未启用）。"""
    pool = getattr(request.app.state, "pool", None)
    cfg = await load_banner_settings(pool)
    return {"config": cfg}


@admin_router.put("/settings")
async def put_banner_settings(
    request: Request, body: dict[str, Any], principal=Depends(_admin)
) -> dict[str, Any]:
    """保存轮播图配置（整体替换）。"""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database not ready")
    cfg = _clean_banners(body)
    saved = await save_banner_settings(
        pool, cfg, updated_by=str(principal.get("user_id") or principal.get("sub") or "")
    )
    return {"ok": True, "config": saved}


@admin_router.post("/banners")
async def create_banner(
    request: Request, body: BannerItem, principal=Depends(_admin)
) -> dict[str, Any]:
    """新增轮播图（追加到列表末尾）。"""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database not ready")
    cfg = await load_banner_settings(pool)
    new_banner = body.model_dump()
    # 分配新的 sort_order（最大值 + 1）
    max_order = max((b["sort_order"] for b in cfg["banners"]), default=-1)
    new_banner["sort_order"] = max_order + 1
    cfg["banners"].append(new_banner)
    saved = await save_banner_settings(
        pool, cfg, updated_by=str(principal.get("user_id") or principal.get("sub") or "")
    )
    return {"ok": True, "banner": new_banner, "config": saved}


@admin_router.put("/banners/{banner_id}")
async def update_banner(
    request: Request, banner_id: str, body: BannerItem, principal=Depends(_admin)
) -> dict[str, Any]:
    """更新轮播图。"""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database not ready")
    cfg = await load_banner_settings(pool)
    idx = next((i for i, b in enumerate(cfg["banners"]) if b["id"] == banner_id), None)
    if idx is None:
        raise HTTPException(404, "轮播图不存在")
    updated = body.model_dump()
    updated["id"] = banner_id
    cfg["banners"][idx] = updated
    cfg["banners"].sort(key=lambda x: x["sort_order"])
    saved = await save_banner_settings(
        pool, cfg, updated_by=str(principal.get("user_id") or principal.get("sub") or "")
    )
    return {"ok": True, "banner": updated, "config": saved}


@admin_router.delete("/banners/{banner_id}")
async def delete_banner(
    request: Request, banner_id: str, principal=Depends(_admin)
) -> dict[str, Any]:
    """删除轮播图。"""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database not ready")
    cfg = await load_banner_settings(pool)
    cfg["banners"] = [b for b in cfg["banners"] if b["id"] != banner_id]
    saved = await save_banner_settings(
        pool, cfg, updated_by=str(principal.get("user_id") or principal.get("sub") or "")
    )
    return {"ok": True, "config": saved}


@admin_router.post("/banners/reorder")
async def reorder_banners(
    request: Request, body: list[dict[str, int]], principal=Depends(_admin)
) -> dict[str, Any]:
    """批量重排序：body = [{id, sort_order}, ...]"""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database not ready")
    cfg = await load_banner_settings(pool)
    order_map = {item["id"]: item["sort_order"] for item in body}
    for b in cfg["banners"]:
        if b["id"] in order_map:
            b["sort_order"] = order_map[b["id"]]
    cfg["banners"].sort(key=lambda x: x["sort_order"])
    saved = await save_banner_settings(
        pool, cfg, updated_by=str(principal.get("user_id") or principal.get("sub") or "")
    )
    return {"ok": True, "config": saved}


# ── 顾客端端点 ──────────────────────────────────────────────────────


@router.get("/list")
async def get_banners(request: Request) -> dict[str, Any]:
    """获取轮播图列表（仅返回启用且在有效期内的）。"""
    from datetime import UTC, datetime

    pool = getattr(request.app.state, "pool", None)
    cfg = await load_banner_settings(pool)
    now = datetime.now(UTC)
    banners = []
    for b in cfg["banners"]:
        if not b["is_active"]:
            continue
        if b.get("start_at"):
            try:
                if datetime.fromisoformat(b["start_at"].replace("Z", "+00:00")) > now:
                    continue
            except ValueError:
                pass
        if b.get("end_at"):
            try:
                if datetime.fromisoformat(b["end_at"].replace("Z", "+00:00")) < now:
                    continue
            except ValueError:
                pass
        banners.append(b)
    return {"items": banners}
