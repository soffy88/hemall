"""Hemall Core4 个性化 — 再来一单、我的常买、共购推荐、首页个性化排序、菜谱。

功能清单（BRIEF / INVENTORY.md 对齐）：
1. 再来一单：一键复制上次订单到购物车
2. 我的常买：基于历史订单频次的个性化常购清单
3. 共购推荐：买了它的人也买了（基于订单共现矩阵）
4. 首页个性化排序：有历史→个性化推荐，无历史→热销兜底
5. 菜谱：数据结构 + 3 个在售示例 + 一键加购
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .deps import get_current_customer
from .store_ops import _csv_response

router = APIRouter(prefix="/store/personalization", tags=["personalization"])

logger = logging.getLogger("hemall.core4.personalization")

# ── 配置键 ────────────────────────────────────────────────────────────
PERSONALIZATION_SETTING_KEY = "personalization.settings.v1"

DEFAULT_PERSONALIZATION_SETTINGS = {
    "reorder_lookback_days": 90,      # 再来一单：回看天数
    "frequent_lookback_days": 180,    # 我的常买：回看天数
    "min_purchase_count": 2,          # 我的常买：最小购买次数
    "copurchase_lookback_days": 180,  # 共购：回看天数
    "copurchase_min_cooccurrence": 2, # 共购：最小共现次数
    "copurchase_max_recommendations": 10,
    "homepage_top_k": 20,             # 首页推荐数量
}

# ── 工具函数 ───────────────────────────────────────────────────────────

def _num(v: Any) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


async def _get_settings(pool: Any) -> dict[str, Any]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM runtime_setting WHERE key = $1",
            PERSONALIZATION_SETTING_KEY,
        )
    if row and row["value"]:
        try:
            return {**DEFAULT_PERSONALIZATION_SETTINGS, **row["value"]}
        except Exception:
            pass
    return DEFAULT_PERSONALIZATION_SETTINGS


async def _save_settings(pool: Any, settings: dict[str, Any], operator_id: str) -> None:
    import json
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO runtime_setting (key, value, updated_by, updated_at)
            VALUES ($1, $2::jsonb, $3, NOW())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = NOW()
            """,
            PERSONALIZATION_SETTING_KEY,
            json.dumps(settings, ensure_ascii=False),
            operator_id,
        )


async def _get_customer_last_order(pool: Any, customer_id: str) -> dict | None:
    """获取顾客最近一笔已完成订单（用于再来一单）。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT o.id, o.status, o.created_at,
                   jsonb_agg(jsonb_build_object(
                       'variant_id', oli.variant_id,
                       'batch_id', oli.batch_id,
                       'quantity', oli.quantity,
                       'pricing_mode', COALESCE(oli.pricing_mode, 'piece'),
                       'unit_price_cents', oli.unit_price_cents,
                       'unit_price_cents_per_jin', oli.unit_price_cents_per_jin,
                       'price_basis_grams', COALESCE(oli.price_basis_grams, 500),
                       'weight_option_label', oli.weight_option_label
                   )) AS line_items
            FROM customer_order o
            LEFT JOIN order_line_item oli ON oli.order_id = o.id
            WHERE o.customer_id = $1
              AND o.status IN ('completed', 'delivered', 'shipped', 'confirmed', 'paid')
              AND oli.deleted_at IS NULL
            GROUP BY o.id
            ORDER BY o.created_at DESC
            LIMIT 1
            """,
            customer_id,
        )
    if row:
        return dict(row)
    return None


# ── 1. 再来一单 ─────────────────────────────────────────────────────────

@router.get("/reorder/last-order")
async def get_last_order_for_reorder(
    request: Request,
    principal: dict[str, Any] = Depends(get_current_customer),
):
    """获取上一单详情（用于"再来一单"预览）。"""
    pool = request.app.state.pool
    customer_id = principal["customer_id"]
    order = await _get_customer_last_order(pool, customer_id)
    if not order:
        raise HTTPException(404, "no eligible previous order found")
    return {"order": order}


class ReorderRequest(BaseModel):
    """再来一单请求：可选覆盖数量、跳过某些行。"""
    order_id: str
    variant_qty_overrides: dict[str, int] = Field(default_factory=dict)  # variant_id -> quantity
    skip_variant_ids: list[str] = Field(default_factory=list)


@router.post("/reorder/apply")
async def apply_reorder_to_cart(
    request: Request,
    body: ReorderRequest,
    principal: dict[str, Any] = Depends(get_current_customer),
):
    """将上一单商品加入当前购物车（创建或复用购物车）。"""
    pool = request.app.state.pool
    customer_id = principal["customer_id"]

    # 校验订单归属
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT customer_id FROM customer_order WHERE id = $1", body.order_id
        )
        if str(owner) != str(customer_id):
            raise HTTPException(403, "order not owned by customer")

        # 获取订单行
        lines = await conn.fetch(
            """
            SELECT oli.variant_id, oli.batch_id, oli.quantity,
                   oli.pricing_mode, oli.unit_price_cents,
                   oli.unit_price_cents_per_jin, oli.price_basis_grams,
                   oli.weight_option_label, pv.product_id, p.title
            FROM order_line_item oli
            LEFT JOIN product_variant pv ON pv.id = oli.variant_id
            LEFT JOIN product p ON p.id = pv.product_id
            WHERE oli.order_id = $1 AND oli.deleted_at IS NULL
            """,
            body.order_id,
        )

    if not lines:
        raise HTTPException(404, "order has no items")

    # 获取或创建购物车
    async with pool.acquire() as conn:
        cart = await conn.fetchrow(
            "SELECT id FROM cart WHERE customer_id = $1 AND status = 'active' AND deleted_at IS NULL",
            customer_id,
        )
        if cart:
            cart_id = str(cart["id"])
        else:
            cart_id = await conn.fetchval(
                """
                INSERT INTO cart (customer_id, status, currency, subtotal_cents, grand_total_cents)
                VALUES ($1, 'active', 'CNY', 0, 0) RETURNING id
                """,
                customer_id,
            )

    # 逐行加入购物车（复用现有 add_to_cart 逻辑或直接写入）
    added = 0
    skipped = 0
    async with pool.acquire() as conn:
        for line in lines:
            vid = str(line["variant_id"])
            if vid in body.skip_variant_ids:
                skipped += 1
                continue

            qty = body.variant_qty_overrides.get(vid, _num(line["quantity"]))
            if qty <= 0:
                skipped += 1
                continue

            # 检查是否已有该变体在购物车中
            existing = await conn.fetchrow(
                "SELECT id, quantity FROM cart_line_item WHERE cart_id = $1 AND variant_id = $2 AND deleted_at IS NULL",
                cart_id, vid,
            )
            if existing:
                new_qty = _num(existing["quantity"]) + qty
                await conn.execute(
                    "UPDATE cart_line_item SET quantity = $1, updated_at = NOW() WHERE id = $2",
                    new_qty, existing["id"],
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO cart_line_item
                        (cart_id, variant_id, batch_id, quantity, unit_price_cents,
                         line_total_cents, pricing_mode, unit_price_cents_per_jin,
                         price_basis_grams, weight_option_label)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    cart_id,
                    vid,
                    line["batch_id"],
                    qty,
                    _num(line["unit_price_cents"]),
                    qty * _num(line["unit_price_cents"]),
                    line["pricing_mode"] or "piece",
                    line["unit_price_cents_per_jin"],
                    _num(line["price_basis_grams"]),
                    line["weight_option_label"],
                )
            added += 1

        # 重算购物车汇总
        await conn.execute(
            """
            UPDATE cart SET
                subtotal_cents = (
                    SELECT COALESCE(SUM(line_total_cents), 0)
                    FROM cart_line_item
                    WHERE cart_id = $1 AND deleted_at IS NULL
                ),
                grand_total_cents = (
                    SELECT COALESCE(SUM(line_total_cents), 0)
                    FROM cart_line_item
                    WHERE cart_id = $1 AND deleted_at IS NULL
                ),
                updated_at = NOW()
            WHERE id = $1
            """,
            cart_id,
        )

    return {
        "cart_id": cart_id,
        "added_lines": added,
        "skipped_lines": skipped,
        "message": f"已将 {added} 个商品加入购物车" + (f"，跳过 {skipped} 个" if skipped else ""),
    }


# ── 2. 我的常买 ─────────────────────────────────────────────────────────

async def _compute_frequent_purchases(pool: Any, customer_id: str, settings: dict[str, Any]) -> list[dict]:
    """计算我的常买：近 N 天内购买频次最高的商品。"""
    lookback_days = settings.get("frequent_lookback_days", 180)
    min_count = settings.get("min_purchase_count", 2)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT pv.id AS variant_id, pv.sku_code, p.title AS product_name,
                   SUM(CASE WHEN oli.pricing_mode = 'weight'
                        THEN COALESCE(oli.actual_weight_grams, oli.estimated_weight_grams, 0)
                        ELSE oli.quantity END)::bigint AS total_qty,
                   COUNT(DISTINCT o.id)::int AS purchase_count,
                   AVG(CASE WHEN oli.pricing_mode = 'weight'
                        THEN COALESCE(oli.actual_amount_cents, oli.line_total_cents, 0)
                        ELSE oli.line_total_cents END)::numeric AS avg_price_cents,
                   MAX(o.created_at) AS last_purchase_at,
                   (
                       SELECT COALESCE(NULLIF(ib.shelf_image_url, ''), ib.media_assets->>0)
                       FROM inventory_batch ib
                       WHERE ib.variant_id = pv.id AND ib.deleted_at IS NULL
                         AND ib.status = 'active' AND ib.inspection_status <> 'failed'
                       ORDER BY GREATEST(ib.stock_qty - ib.reserved_qty, 0) DESC, ib.created_at ASC
                       LIMIT 1
                   ) AS image_url
            FROM order_line_item oli
            JOIN customer_order o ON o.id = oli.order_id
            LEFT JOIN product_variant pv ON pv.id = oli.variant_id
            LEFT JOIN product p ON p.id = pv.product_id
            WHERE o.customer_id = $1
              AND o.status IN ('completed', 'delivered', 'shipped', 'confirmed', 'paid')
              AND o.created_at >= NOW() - INTERVAL '{lookback_days} days'
              AND oli.preorder_qty = 0
              AND oli.deleted_at IS NULL
            GROUP BY pv.id, pv.sku_code, p.title
            HAVING COUNT(DISTINCT o.id) >= $2
            ORDER BY purchase_count DESC, last_purchase_at DESC
            LIMIT 50
            """,
            customer_id,
            min_count,
        )

    return [dict(r) for r in rows]


@router.get("/frequent")
async def get_frequent_purchases(
    request: Request,
    principal: dict[str, Any] = Depends(get_current_customer),
):
    """获取我的常买清单。"""
    pool = request.app.state.pool
    customer_id = principal["customer_id"]
    settings = await _get_settings(pool)
    frequent = await _compute_frequent_purchases(pool, customer_id, settings)
    return {"items": frequent, "count": len(frequent)}


@router.post("/frequent/add-to-cart")
async def add_frequent_to_cart(
    request: Request,
    variant_ids: list[str] = Query(..., description="要加入购物车的 variant_id 列表"),
    quantities: list[int] = Query(default=[], description="对应的数量，默认各 1"),
    principal: dict[str, Any] = Depends(get_current_customer),
):
    """一键将常购商品加入购物车。"""
    pool = request.app.state.pool
    customer_id = principal["customer_id"]

    if not variant_ids:
        raise HTTPException(400, "variant_ids cannot be empty")

    if quantities and len(quantities) != len(variant_ids):
        raise HTTPException(400, "quantities length must match variant_ids")

    # 获取或创建购物车
    async with pool.acquire() as conn:
        cart = await conn.fetchrow(
            "SELECT id FROM cart WHERE customer_id = $1 AND status = 'active' AND deleted_at IS NULL",
            customer_id,
        )
        if cart:
            cart_id = str(cart["id"])
        else:
            cart_id = await conn.fetchval(
                """
                INSERT INTO cart (customer_id, status, currency, subtotal_cents, grand_total_cents)
                VALUES ($1, 'active', 'CNY', 0, 0) RETURNING id
                """,
                customer_id,
            )

        added = 0
        for i, vid in enumerate(variant_ids):
            qty = quantities[i] if quantities else 1
            if qty <= 0:
                continue

            # 获取变体价格
            variant = await conn.fetchrow(
                "SELECT reference_price_cents FROM product_variant WHERE id = $1 AND deleted_at IS NULL",
                vid,
            )
            if not variant:
                continue

            existing = await conn.fetchrow(
                "SELECT id, quantity FROM cart_line_item WHERE cart_id = $1 AND variant_id = $2 AND deleted_at IS NULL",
                cart_id, vid,
            )
            price = _num(variant["reference_price_cents"])
            if existing:
                new_qty = _num(existing["quantity"]) + qty
                await conn.execute(
                    "UPDATE cart_line_item SET quantity = $1, updated_at = NOW() WHERE id = $2",
                    new_qty, existing["id"],
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO cart_line_item
                        (cart_id, variant_id, quantity, unit_price_cents,
                         line_total_cents, pricing_mode)
                    VALUES ($1, $2, $3, $4, $5, 'piece')
                    """,
                    cart_id, vid, qty, price, qty * price,
                )
            added += 1

        await conn.execute(
            """
            UPDATE cart SET
                subtotal_cents = (SELECT COALESCE(SUM(line_total_cents), 0) FROM cart_line_item WHERE cart_id = $1 AND deleted_at IS NULL),
                grand_total_cents = (SELECT COALESCE(SUM(line_total_cents), 0) FROM cart_line_item WHERE cart_id = $1 AND deleted_at IS NULL),
                updated_at = NOW()
            WHERE id = $1
            """,
            cart_id,
        )

    return {"cart_id": cart_id, "added_lines": added}


# ── 3. 共购推荐：买了它的人也买了 ────────────────────────────────────────

async def _compute_copurchase_recommendations(
    pool: Any,
    product_id: str,
    settings: dict[str, Any],
    limit: int = 10,
) -> list[dict]:
    """计算共购推荐：买了该商品的人还买了什么（基于订单共现矩阵）。"""
    lookback_days = settings.get("copurchase_lookback_days", 180)
    min_cooccurrence = settings.get("copurchase_min_cooccurrence", 2)
    max_recs = settings.get("copurchase_max_recommendations", 10)
    limit = min(limit, max_recs)

    async with pool.acquire() as conn:
        # 先找到包含该商品的订单
        rows = await conn.fetch(
            f"""
            WITH target_orders AS (
                SELECT DISTINCT o.id
                FROM customer_order o
                JOIN order_line_item oli ON oli.order_id = o.id
                LEFT JOIN product_variant pv ON pv.id = oli.variant_id
                LEFT JOIN product p ON p.id = pv.product_id
                WHERE p.id = $1
                  AND o.status IN ('completed', 'delivered', 'shipped', 'confirmed', 'paid')
                  AND o.created_at >= NOW() - INTERVAL '{lookback_days} days'
                  AND oli.deleted_at IS NULL
            ),
            copurchase AS (
                SELECT p2.id AS product_id, p2.title, p2.slug,
                       COUNT(DISTINCT o.id)::int AS cooccurrence_count
                FROM target_orders to
                JOIN customer_order o ON o.id = to.id
                JOIN order_line_item oli2 ON oli2.order_id = o.id
                LEFT JOIN product_variant pv2 ON pv2.id = oli2.variant_id
                LEFT JOIN product p2 ON p2.id = pv2.product_id
                WHERE p2.id <> $1
                  AND o.status IN ('completed', 'delivered', 'shipped', 'confirmed', 'paid')
                  AND oli2.deleted_at IS NULL
                GROUP BY p2.id, p2.title, p2.slug
                HAVING COUNT(DISTINCT o.id) >= $2
            )
            SELECT c.*,
                   (
                       SELECT COALESCE(NULLIF(ib.shelf_image_url, ''), ib.media_assets->>0)
                       FROM inventory_batch ib
                       JOIN product_variant pv ON pv.product_id = c.product_id
                       WHERE ib.variant_id = pv.id AND ib.deleted_at IS NULL
                         AND ib.status = 'active' AND ib.inspection_status <> 'failed'
                       ORDER BY GREATEST(ib.stock_qty - ib.reserved_qty, 0) DESC, ib.created_at ASC
                       LIMIT 1
                   ) AS image_url,
                   (
                       SELECT MIN(ib.retail_price_cents)
                       FROM inventory_batch ib
                       JOIN product_variant pv ON pv.product_id = c.product_id
                       WHERE ib.variant_id = pv.id AND ib.deleted_at IS NULL
                         AND ib.status = 'active' AND ib.inspection_status <> 'failed'
                         AND ib.retail_price_cents IS NOT NULL
                   ) AS min_price_cents
            FROM copurchase c
            ORDER BY c.cooccurrence_count DESC, c.product_id
            LIMIT $3
            """,
            product_id,
            min_cooccurrence,
            limit,
        )

    return [dict(r) for r in rows]


@router.get("/copurchase/{product_id}")
async def get_copurchase_recommendations(
    product_id: str,
    request: Request,
    limit: int = Query(10, ge=1, le=20),
):
    """获取"买了它的人也买了"推荐。"""
    pool = request.app.state.pool
    settings = await _get_settings(pool)
    recs = await _compute_copurchase_recommendations(pool, product_id, settings, limit)
    return {"product_id": product_id, "recommendations": recs, "count": len(recs)}


# ── 4. 首页个性化排序 ───────────────────────────────────────────────────

async def _get_personalized_homepage(
    pool: Any,
    customer_id: str | None,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """首页个性化排序：有历史→个性化推荐，无历史→热销兜底。"""
    top_k = settings.get("homepage_top_k", 20)

    if not customer_id:
        # 匿名用户：返回热销
        return await _get_hot_products(pool, top_k, "anonymous")

    # 检查用户是否有购买历史
    async with pool.acquire() as conn:
        has_history = await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM customer_order o
                JOIN order_line_item oli ON oli.order_id = o.id
                WHERE o.customer_id = $1
                  AND o.status IN ('completed', 'delivered', 'shipped', 'confirmed', 'paid')
                  AND oli.preorder_qty = 0
                  AND oli.deleted_at IS NULL
            )
            """,
            customer_id,
        )

    if not has_history:
        # 新用户：返回热销 + 推荐标记
        return await _get_hot_products(pool, top_k, "new_user")

    # 有历史用户：使用推荐引擎的个性化推荐
    # 这里复用现有的 recommend 服务
    from app.recommend.service import RecommendationService
    from app.main import get_app_state

    app_state = get_app_state()
    svc = getattr(app_state, "recommend_service", None)
    if svc and svc._trained:
        result = await svc.recommend_for_user(customer_id, top_k)
        return {
            "products": [p.model_dump(mode="json") for p in result.products],
            "algorithm": result.algorithm,
            "personalized": True,
            "reason": "基于您的购买历史为您推荐",
        }

    # 推荐服务未就绪：回落到热销
    return await _get_hot_products(pool, top_k, "fallback")


async def _get_hot_products(pool: Any, top_k: int, reason: str) -> dict[str, Any]:
    """获取热销商品（兜底）。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT p.id, p.title, p.slug, p.description,
                   MIN(ib.retail_price_cents) AS min_price_cents,
                   SUM(GREATEST(ib.stock_qty - ib.reserved_qty, 0))::int AS total_stock,
                   (
                       SELECT COALESCE(NULLIF(ib2.shelf_image_url, ''), ib2.media_assets->>0)
                       FROM inventory_batch ib2
                       JOIN product_variant pv2 ON pv2.id = ib2.variant_id
                       WHERE pv2.product_id = p.id AND ib2.deleted_at IS NULL
                         AND ib2.status = 'active' AND ib2.inspection_status <> 'failed'
                       ORDER BY GREATEST(ib2.stock_qty - ib2.reserved_qty, 0) DESC, ib2.created_at ASC
                       LIMIT 1
                   ) AS image_url
            FROM product p
            JOIN product_variant pv ON pv.product_id = p.id
            JOIN inventory_batch ib ON ib.variant_id = pv.id
            WHERE p.deleted_at IS NULL AND p.status = 'published'
              AND pv.deleted_at IS NULL AND pv.status = 'active'
              AND ib.deleted_at IS NULL AND ib.status = 'active'
              AND ib.inspection_status <> 'failed'
              AND (ib.expiration_time IS NULL OR ib.expiration_time > NOW())
            GROUP BY p.id, p.title, p.slug, p.description
            ORDER BY total_stock DESC, min_price_cents ASC
            LIMIT $1
            """,
            top_k,
        )

    return {
        "products": [dict(r) for r in rows],
        "algorithm": "hot",
        "personalized": False,
        "reason": reason,
    }


@router.get("/homepage")
async def get_personalized_homepage(
    request: Request,
    principal: dict[str, Any] | None = Depends(get_current_customer),
):
    """首页个性化商品流。"""
    pool = request.app.state.pool
    customer_id = principal["customer_id"] if principal else None
    settings = await _get_settings(pool)
    result = await _get_personalized_homepage(pool, customer_id, settings)
    return result


# ── 5. 菜谱 ─────────────────────────────────────────────────────────────

class RecipeIngredient(BaseModel):
    variant_id: str
    quantity: float
    unit: str
    note: str = ""


class RecipeStep(BaseModel):
    step_number: int
    instruction: str
    image_url: str | None = None
    duration_minutes: int | None = None


class RecipeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    description: str = ""
    cover_image_url: str = ""
    servings: int = Field(default=2, ge=1)
    prep_time_minutes: int = Field(default=10, ge=0)
    cook_time_minutes: int = Field(default=20, ge=0)
    difficulty: str = Field(default="easy", pattern="^(easy|medium|hard)$")
    tags: list[str] = Field(default_factory=list)
    ingredients: list[RecipeIngredient] = Field(min_length=1)
    steps: list[RecipeStep] = Field(min_length=1)
    is_published: bool = True


class RecipeUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    cover_image_url: str | None = None
    servings: int | None = Field(default=None, ge=1)
    prep_time_minutes: int | None = Field(default=None, ge=0)
    cook_time_minutes: int | None = Field(default=None, ge=0)
    difficulty: str | None = Field(default=None, pattern="^(easy|medium|hard)$")
    tags: list[str] | None = None
    ingredients: list[RecipeIngredient] | None = None
    steps: list[RecipeStep] | None = None
    is_published: bool | None = None


# 菜谱 DDL（在 core4_schema.py 中已定义 core4_recipe 表，这里只做运行时检查）
RECIPE_DDL = """
CREATE TABLE IF NOT EXISTS core4_recipe (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    cover_image_url TEXT NOT NULL DEFAULT '',
    servings INTEGER NOT NULL DEFAULT 2 CHECK (servings > 0),
    prep_time_minutes INTEGER NOT NULL DEFAULT 10 CHECK (prep_time_minutes >= 0),
    cook_time_minutes INTEGER NOT NULL DEFAULT 20 CHECK (cook_time_minutes >= 0),
    difficulty TEXT NOT NULL DEFAULT 'easy' CHECK (difficulty IN ('easy', 'medium', 'hard')),
    tags TEXT[] NOT NULL DEFAULT '{}',
    ingredients JSONB NOT NULL DEFAULT '[]'::jsonb,
    steps JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_published BOOLEAN NOT NULL DEFAULT TRUE,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_core4_recipe_published ON core4_recipe(is_published, created_at DESC);
"""

async def _ensure_recipe_table(pool: Any) -> None:
    async with pool.acquire() as conn:
        await conn.execute(RECIPE_DDL)


@router.post("/recipes")
async def create_recipe(
    request: Request,
    body: RecipeCreate,
    principal: dict[str, Any] = Depends(get_current_customer),
):
    """创建菜谱（后台/商家用）。"""
    pool = request.app.state.pool
    await _ensure_recipe_table(pool)

    # 验证所有 variant_id 存在且在售
    for ing in body.ingredients:
        async with pool.acquire() as conn:
            variant = await conn.fetchrow(
                """
                SELECT pv.id, pv.product_id, p.title
                FROM product_variant pv
                JOIN product p ON p.id = pv.product_id
                WHERE pv.id = $1 AND pv.deleted_at IS NULL AND p.deleted_at IS NULL
                """,
                ing.variant_id,
            )
            if not variant:
                raise HTTPException(400, f"variant {ing.variant_id} not found")

    operator_id = str(principal.get("user_id") or principal.get("customer_id") or "")

    async with pool.acquire() as conn:
        recipe_id = await conn.fetchval(
            """
            INSERT INTO core4_recipe
                (title, description, cover_image_url, servings, prep_time_minutes,
                 cook_time_minutes, difficulty, tags, ingredients, steps, is_published, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            RETURNING id
            """,
            body.title,
            body.description,
            body.cover_image_url,
            body.servings,
            body.prep_time_minutes,
            body.cook_time_minutes,
            body.difficulty,
            body.tags,
            json.dumps([i.model_dump() for i in body.ingredients]),
            json.dumps([s.model_dump() for s in body.steps]),
            body.is_published,
            operator_id,
        )

    return {"recipe_id": str(recipe_id), "message": "recipe created"}


@router.get("/recipes")
async def list_recipes(
    request: Request,
    published_only: bool = True,
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
):
    """获取菜谱列表。"""
    pool = request.app.state.pool
    await _ensure_recipe_table(pool)

    where = "WHERE is_published = TRUE" if published_only else ""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, title, description, cover_image_url, servings,
                   prep_time_minutes, cook_time_minutes, difficulty, tags,
                   ingredients, steps, is_published, created_at
            FROM core4_recipe
            {where}
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )

    return {
        "recipes": [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "description": r["description"],
                "cover_image_url": r["cover_image_url"],
                "servings": r["servings"],
                "prep_time_minutes": r["prep_time_minutes"],
                "cook_time_minutes": r["cook_time_minutes"],
                "difficulty": r["difficulty"],
                "tags": r["tags"],
                "ingredients": r["ingredients"],
                "steps": r["steps"],
                "is_published": r["is_published"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    }


@router.get("/recipes/{recipe_id}")
async def get_recipe(
    recipe_id: str,
    request: Request,
):
    """获取菜谱详情。"""
    pool = request.app.state.pool
    await _ensure_recipe_table(pool)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM core4_recipe WHERE id = $1", recipe_id
        )
        if not row:
            raise HTTPException(404, "recipe not found")

    return {
        "id": str(row["id"]),
        "title": row["title"],
        "description": row["description"],
        "cover_image_url": row["cover_image_url"],
        "servings": row["servings"],
        "prep_time_minutes": row["prep_time_minutes"],
        "cook_time_minutes": row["cook_time_minutes"],
        "difficulty": row["difficulty"],
        "tags": row["tags"],
        "ingredients": row["ingredients"],
        "steps": row["steps"],
        "is_published": row["is_published"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.patch("/recipes/{recipe_id}")
async def update_recipe(
    recipe_id: str,
    request: Request,
    body: RecipeUpdate,
    principal: dict[str, Any] = Depends(get_current_customer),
):
    """更新菜谱。"""
    pool = request.app.state.pool
    await _ensure_recipe_table(pool)

    updates = []
    params = [recipe_id]
    param_idx = 2

    fields = {
        "title": body.title,
        "description": body.description,
        "cover_image_url": body.cover_image_url,
        "servings": body.servings,
        "prep_time_minutes": body.prep_time_minutes,
        "cook_time_minutes": body.cook_time_minutes,
        "difficulty": body.difficulty,
        "tags": body.tags,
        "is_published": body.is_published,
    }

    for field, value in fields.items():
        if value is not None:
            updates.append(f"{field} = ${param_idx}")
            params.append(value)
            param_idx += 1

    if body.ingredients is not None:
        updates.append(f"ingredients = ${param_idx}")
        params.append(json.dumps([i.model_dump() for i in body.ingredients]))
        param_idx += 1

    if body.steps is not None:
        updates.append(f"steps = ${param_idx}")
        params.append(json.dumps([s.model_dump() for s in body.steps]))
        param_idx += 1

    if not updates:
        return {"message": "no changes"}

    updates.append("updated_at = NOW()")
    query = f"UPDATE core4_recipe SET {', '.join(updates)} WHERE id = $1"
    async with pool.acquire() as conn:
        await conn.execute(query, *params)

    return {"message": "recipe updated"}


@router.delete("/recipes/{recipe_id}")
async def delete_recipe(
    recipe_id: str,
    request: Request,
    principal: dict[str, Any] = Depends(get_current_customer),
):
    """删除菜谱（软删除：设为 unpublished）。"""
    pool = request.app.state.pool
    await _ensure_recipe_table(pool)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE core4_recipe SET is_published = FALSE, updated_at = NOW() WHERE id = $1",
            recipe_id,
        )

    return {"message": "recipe deleted"}


@router.post("/recipes/{recipe_id}/add-to-cart")
async def add_recipe_to_cart(
    recipe_id: str,
    request: Request,
    servings: int = Query(1, ge=1, description="按份数缩放食材数量"),
    principal: dict[str, Any] = Depends(get_current_customer),
):
    """一键将菜谱食材加入购物车。"""
    pool = request.app.state.pool
    customer_id = principal["customer_id"]
    await _ensure_recipe_table(pool)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ingredients FROM core4_recipe WHERE id = $1 AND is_published = TRUE",
            recipe_id,
        )
        if not row:
            raise HTTPException(404, "recipe not found")

        ingredients = row["ingredients"] or []

    # 获取或创建购物车
    async with pool.acquire() as conn:
        cart = await conn.fetchrow(
            "SELECT id FROM cart WHERE customer_id = $1 AND status = 'active' AND deleted_at IS NULL",
            customer_id,
        )
        if cart:
            cart_id = str(cart["id"])
        else:
            cart_id = await conn.fetchval(
                """
                INSERT INTO cart (customer_id, status, currency, subtotal_cents, grand_total_cents)
                VALUES ($1, 'active', 'CNY', 0, 0) RETURNING id
                """,
                customer_id,
            )

        added = 0
        for ing in ingredients:
            vid = ing.get("variant_id")
            if not vid:
                continue

            base_qty = float(ing.get("quantity", 1))
            qty = max(1, int(base_qty * servings + 0.5))

            variant = await conn.fetchrow(
                "SELECT reference_price_cents FROM product_variant WHERE id = $1 AND deleted_at IS NULL",
                vid,
            )
            if not variant:
                continue

            existing = await conn.fetchrow(
                "SELECT id, quantity FROM cart_line_item WHERE cart_id = $1 AND variant_id = $2 AND deleted_at IS NULL",
                cart_id, vid,
            )
            price = _num(variant["reference_price_cents"])
            if existing:
                new_qty = _num(existing["quantity"]) + qty
                await conn.execute(
                    "UPDATE cart_line_item SET quantity = $1, updated_at = NOW() WHERE id = $2",
                    new_qty, existing["id"],
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO cart_line_item
                        (cart_id, variant_id, quantity, unit_price_cents,
                         line_total_cents, pricing_mode)
                    VALUES ($1, $2, $3, $4, $5, 'piece')
                    """,
                    cart_id, vid, qty, price, qty * price,
                )
            added += 1

        await conn.execute(
            """
            UPDATE cart SET
                subtotal_cents = (SELECT COALESCE(SUM(line_total_cents), 0) FROM cart_line_item WHERE cart_id = $1 AND deleted_at IS NULL),
                grand_total_cents = (SELECT COALESCE(SUM(line_total_cents), 0) FROM cart_line_item WHERE cart_id = $1 AND deleted_at IS NULL),
                updated_at = NOW()
            WHERE id = $1
            """,
            cart_id,
        )

    return {
        "cart_id": cart_id,
        "added_lines": added,
        "message": f"已将菜谱 {servings} 人份食材加入购物车（{added} 种食材）",
    }


# ── 管理端配置 ───────────────────────────────────────────────────────────

class PersonalizationSettings(BaseModel):
    reorder_lookback_days: int = Field(default=90, ge=1, le=365)
    frequent_lookback_days: int = Field(default=180, ge=1, le=365)
    min_purchase_count: int = Field(default=2, ge=1, le=20)
    copurchase_lookback_days: int = Field(default=180, ge=1, le=365)
    copurchase_min_cooccurrence: int = Field(default=2, ge=1, le=10)
    copurchase_max_recommendations: int = Field(default=10, ge=1, le=50)
    homepage_top_k: int = Field(default=20, ge=5, le=100)


def _require_admin(request: Request, principal: dict[str, Any] = Depends(get_current_customer)) -> dict[str, Any]:
    # 复用现有的 admin 鉴权逻辑
    from .deps import assert_staff_role
    return assert_staff_role(principal, {"admin", "support"})


@router.get("/admin/settings")
async def get_personalization_settings(
    request: Request,
    _=Depends(_require_admin),
):
    """获取个性化配置。"""
    pool = request.app.state.pool
    settings = await _get_settings(pool)
    return {"settings": settings}


@router.put("/admin/settings")
async def update_personalization_settings(
    request: Request,
    body: PersonalizationSettings,
    principal: dict[str, Any] = Depends(_require_admin),
):
    """更新个性化配置。"""
    pool = request.app.state.pool
    settings = body.model_dump()
    await _save_settings(pool, settings, str(principal.get("user_id") or ""))
    return {"ok": True, "settings": settings}