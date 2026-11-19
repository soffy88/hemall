"""宋鲜生小程序：社区接龙团购（拼单）API。

复用 night_pintuan 核心逻辑，暴露给小程序的顾客端接口：
- GET  /store/pintuan/groups         团购列表（可按状态筛选）
- GET  /store/pintuan/groups/{id}    团详情（含参团名单、脱敏昵称）
- POST /store/pintuan/groups         发起拼单（需登录）
- POST /store/pintuan/groups/{id}/join  参团（需登录）

后台管理端点（/admin/pintuan）：
- GET  /admin/pintuan/groups         团列表（含拣货汇总）
- GET  /admin/pintuan/groups/{id}    团详情
- POST /admin/pintuan/groups/sweep-expired  批量过期
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .deps import get_current_customer, get_current_user
from .night_pintuan import (
    create_pintuan_group,
    get_pintuan_group,
    join_pintuan_group,
    list_pintuan_groups,
    sweep_expired_pintuan_groups,
)
from .security.tokens import TOKEN_TYPE_PICKUP, sign_token

router = APIRouter(prefix="/store/pintuan", tags=["pintuan"])
admin_router = APIRouter(prefix="/admin/pintuan", tags=["admin-pintuan"])


# ── 顾客端模型 ────────────────────────────────────────────────────────────

class CreatePintuanGroupRequest(BaseModel):
    variant_id: str
    pintuan_price_cents: int = Field(gt=0)
    target_size: int = Field(ge=2, le=1000)
    duration_minutes: int = Field(gt=0, default=1440)
    quantity: int = Field(ge=1, default=1)


class JoinPintuanGroupRequest(BaseModel):
    quantity: int = Field(ge=1, default=1)


def _mask_nickname(nickname: str | None) -> str:
    """昵称脱敏：张**、李** 等。"""
    if not nickname:
        return "用户"
    if len(nickname) == 1:
        return nickname + "*"
    if len(nickname) == 2:
        return nickname[0] + "*"
    return nickname[0] + "*" * (len(nickname) - 2) + nickname[-1]


async def _enrich_group_with_variant(pool: Any, group: dict[str, Any]) -> dict[str, Any]:
    """为团信息补充商品详情（标题、图片、规格等）。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT pv.id, pv.sku_code, pv.option_values,
                   p.title, p.slug,
                   (
                       SELECT COALESCE(NULLIF(ib.shelf_image_url, ''), ib.media_assets->>0)
                       FROM inventory_batch ib
                       WHERE ib.variant_id = pv.id AND ib.deleted_at IS NULL
                         AND ib.status = 'active' AND ib.inspection_status <> 'failed'
                       ORDER BY GREATEST(ib.stock_qty - ib.reserved_qty, 0) DESC, ib.created_at ASC
                       LIMIT 1
                   ) AS image_url
            FROM product_variant pv
            LEFT JOIN product p ON p.id = pv.product_id
            WHERE pv.id = $1 AND pv.deleted_at IS NULL
            """,
            group["variant_id"],
        )
    if row:
        group["variant"] = dict(row)
    return group


async def _enrich_participants_with_nickname(pool: Any, participants: list[dict]) -> list[dict]:
    """为参与者补充脱敏昵称。"""
    if not participants:
        return participants
    customer_ids = [p["customer_id"] for p in participants]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name FROM customer WHERE id = ANY($1)",
            customer_ids,
        )
    name_map = {str(r["id"]): r["name"] for r in rows}
    for p in participants:
        p["nickname_masked"] = _mask_nickname(name_map.get(p["customer_id"]))
    return participants


# ── 顾客端端点 ────────────────────────────────────────────────────────────

@router.get("/groups")
async def list_groups(
    request: Request,
    status: str | None = Query(None, description="筛选状态：open/formed/expired"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """团购列表：默认只看 open/formed，支持分页。"""
    pool = request.app.state.pool
    groups = await list_pintuan_groups(pool, status=status, limit=limit, offset=offset)
    # 补充商品信息
    for g in groups:
        await _enrich_group_with_variant(pool, g)
    return {"items": groups, "limit": limit, "offset": offset}


@router.get("/groups/{group_id}")
async def get_group(
    request: Request,
    group_id: str,
):
    """团详情：含参团名单（脱敏昵称）、商品信息。"""
    pool = request.app.state.pool
    group = await get_pintuan_group(pool, group_id)
    if not group:
        raise HTTPException(404, "团购不存在或已过期")
    await _enrich_group_with_variant(pool, group)
    group["participants"] = await _enrich_participants_with_nickname(pool, group.get("participants", []))
    return group


@router.post("/groups")
async def create_group(
    request: Request,
    body: CreatePintuanGroupRequest,
    principal: dict[str, Any] = Depends(get_current_customer),
):
    """发起拼单：需登录，发起人自动成为第1名参与者。"""
    pool = request.app.state.pool
    customer_id = principal["customer_id"]
    try:
        group = await create_pintuan_group(
            pool,
            creator_id=customer_id,
            variant_id=body.variant_id,
            pintuan_price_cents=body.pintuan_price_cents,
            target_size=body.target_size,
            duration_minutes=body.duration_minutes,
            quantity=body.quantity,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _enrich_group_with_variant(pool, group)
    return group


@router.post("/groups/{group_id}/join")
async def join_group(
    request: Request,
    group_id: str,
    body: JoinPintuanGroupRequest,
    principal: dict[str, Any] = Depends(get_current_customer),
):
    """参团：需登录，一人一团仅能参与一次。"""
    pool = request.app.state.pool
    customer_id = principal["customer_id"]
    try:
        group = await join_pintuan_group(
            pool,
            group_id=group_id,
            customer_id=customer_id,
            quantity=body.quantity,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _enrich_group_with_variant(pool, group)
    group["participants"] = await _enrich_participants_with_nickname(pool, group.get("participants", []))
    return group


# ── 后台管理端点 ────────────────────────────────────────────────────────────

@admin_router.get("/groups")
async def admin_list_groups(
    request: Request,
    _=Depends(get_current_user),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """后台团购列表（含拣货汇总）。"""
    pool = request.app.state.pool
    groups = await list_pintuan_groups(pool, status=status, limit=limit, offset=offset)
    for g in groups:
        await _enrich_group_with_variant(pool, g)
        # 计算需拣货总量
        total_qty = sum(p["quantity"] for p in g.get("participants", []))
        g["total_pick_qty"] = total_qty
    return {"items": groups, "limit": limit, "offset": offset}


@admin_router.get("/groups/{group_id}")
async def admin_get_group(
    request: Request,
    group_id: str,
    _=Depends(get_current_user),
):
    """后台团详情：含完整参团名单、拣货清单。"""
    pool = request.app.state.pool
    group = await get_pintuan_group(pool, group_id)
    if not group:
        raise HTTPException(404, "团购不存在")
    await _enrich_group_with_variant(pool, group)
    # 后台显示真实姓名/手机，不脱敏
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id AS customer_id, c.name, c.phone, pp.quantity, pp.created_at
            FROM pintuan_participant pp
            JOIN customer c ON c.id = pp.customer_id
            WHERE pp.group_id = $1
            ORDER BY pp.created_at ASC
            """,
            group_id,
        )
    group["participants"] = [dict(r) for r in rows]
    group["total_pick_qty"] = sum(p["quantity"] for p in group["participants"])
    return group


@admin_router.post("/groups/sweep-expired")
async def admin_sweep_expired(
    request: Request,
    _=Depends(get_current_user),
):
    """批量过期：将所有已过期的 open 团转为 expired。"""
    pool = request.app.state.pool
    count = await sweep_expired_pintuan_groups(pool)
    return {"expired_count": count}


@admin_router.get("/groups/{group_id}/pick-list")
async def admin_get_pick_list(
    request: Request,
    group_id: str,
    _=Depends(get_current_user),
):
    """拣货单：按商品汇总需拣货数量，供门店打印。"""
    pool = request.app.state.pool
    group = await get_pintuan_group(pool, group_id)
    if not group:
        raise HTTPException(404, "团购不存在")
    if group["status"] != "formed":
        raise HTTPException(400, "仅成团后可生成拣货单")

    await _enrich_group_with_variant(pool, group)

    # 汇总：按 variant 聚合
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pv.id AS variant_id, pv.sku_code, pv.option_values,
                   p.title, SUM(pp.quantity) AS total_qty
            FROM pintuan_participant pp
            JOIN product_variant pv ON pv.id = $1
            JOIN product p ON p.id = pv.product_id
            WHERE pp.group_id = $1
            GROUP BY pv.id, pv.sku_code, pv.option_values, p.title
            """,
            group_id,
        )

    pick_items = [dict(r) for r in rows]
    return {
        "group_id": group_id,
        "variant_id": group["variant_id"],
        "pintuan_price_cents": group["pintuan_price_cents"],
        "target_size": group["target_size"],
        "participant_count": group["participant_count"],
        "items": pick_items,
        "created_at": group["created_at"],
    }