"""hemall 读层 — 直接 SQL 查询，绕过 omodul 写引擎。

所有函数接收 obase.persistence.pool.PgPool，用 asyncpg 原生 fetch/fetchrow。
表名列名严格对齐 obase.commerce_batch_schema。
"""

from __future__ import annotations

import json
from typing import Any


async def list_products(
    pool: Any,
    *,
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """商品列表：SPU + 变体 + 批次价格/库存聚合。"""
    async with pool.acquire() as conn:
        where_clauses = ["p.deleted_at IS NULL"]
        params: list[Any] = []

        if status:
            params.append(status)
            where_clauses.append(f"p.status = ${len(params)}")
        if search:
            params.append(f"%{search}%")
            where_clauses.append(
                f"(p.title ILIKE ${len(params)} OR p.description ILIKE ${len(params)})"
            )

        where = " AND ".join(where_clauses)

        rows = await conn.fetch(
            f"""
            SELECT p.id, p.title, p.slug, p.description, p.category_id, p.status,
                   p.created_at, p.updated_at,
                   COALESCE(options.options, '[]'::jsonb) AS options,
                   COALESCE(variants.variants, '[]'::jsonb) AS variants,
                   COALESCE(variants.total_stock, 0) AS total_stock,
                   COALESCE(variants.min_price, NULL) AS min_price_cents
            FROM product p
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object('id', po.id, 'name', po.name)) AS options
                FROM product_option po
                WHERE po.product_id = p.id AND po.deleted_at IS NULL
            ) options ON true
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'id', pv.id,
                    'sku_code', pv.sku_code,
                    'option_values', pv.option_values,
                    'reference_price_cents', pv.reference_price_cents,
                    'status', pv.status,
                    'batches', COALESCE(batches.batch_list, '[]'::jsonb),
                    'total_stock', COALESCE(batches.total_stock, 0),
                    'min_price_cents', COALESCE(batches.min_price, pv.reference_price_cents)
                )) AS variants,
                SUM(COALESCE(batches.total_stock, 0))::int AS total_stock,
                MIN(COALESCE(batches.min_price, pv.reference_price_cents))::int AS min_price
                FROM product_variant pv
                LEFT JOIN LATERAL (
                    SELECT jsonb_agg(jsonb_build_object(
                        'id', ib.id,
                        'batch_no', ib.batch_no,
                        'stock_qty', ib.stock_qty,
                        'reserved_qty', ib.reserved_qty,
                        'available_qty', ib.stock_qty - ib.reserved_qty,
                        'retail_price_cents', ib.retail_price_cents,
                        'cost_price_cents', ib.cost_price_cents,
                        'currency', ib.currency,
                        'location_id', ib.location_id,
                        'status', ib.status
                    )) AS batch_list,
                    SUM(ib.stock_qty - ib.reserved_qty)::int AS total_stock,
                    MIN(ib.retail_price_cents)::int AS min_price
                    FROM inventory_batch ib
                    WHERE ib.variant_id = pv.id AND ib.deleted_at IS NULL AND ib.status = 'active'
                ) batches ON true
                WHERE pv.product_id = p.id AND pv.deleted_at IS NULL
            ) variants ON true
            WHERE {where}
            ORDER BY p.created_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params,
            limit,
            offset,
        )
        return [_row_to_dict(r) for r in rows]


async def get_product(pool: Any, product_id: str) -> dict | None:
    """单个商品详情：含变体、批次、价格、库存。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.id, p.title, p.slug, p.description, p.category_id, p.status,
                   p.created_at, p.updated_at,
                   COALESCE(options.options, '[]'::jsonb) AS options,
                   COALESCE(variants.variants, '[]'::jsonb) AS variants,
                   COALESCE(variants.total_stock, 0) AS total_stock,
                   COALESCE(variants.min_price, NULL) AS min_price_cents
            FROM product p
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object('id', po.id, 'name', po.name)) AS options
                FROM product_option po
                WHERE po.product_id = p.id AND po.deleted_at IS NULL
            ) options ON true
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'id', pv.id,
                    'sku_code', pv.sku_code,
                    'option_values', pv.option_values,
                    'reference_price_cents', pv.reference_price_cents,
                    'status', pv.status,
                    'batches', COALESCE(batches.batch_list, '[]'::jsonb),
                    'total_stock', COALESCE(batches.total_stock, 0),
                    'min_price_cents', COALESCE(batches.min_price, pv.reference_price_cents)
                )) AS variants,
                SUM(COALESCE(batches.total_stock, 0))::int AS total_stock,
                MIN(COALESCE(batches.min_price, pv.reference_price_cents))::int AS min_price
                FROM product_variant pv
                LEFT JOIN LATERAL (
                    SELECT jsonb_agg(jsonb_build_object(
                        'id', ib.id,
                        'batch_no', ib.batch_no,
                        'stock_qty', ib.stock_qty,
                        'reserved_qty', ib.reserved_qty,
                        'available_qty', ib.stock_qty - ib.reserved_qty,
                        'retail_price_cents', ib.retail_price_cents,
                        'cost_price_cents', ib.cost_price_cents,
                        'currency', ib.currency,
                        'location_id', ib.location_id,
                        'status', ib.status
                    )) AS batch_list,
                    SUM(ib.stock_qty - ib.reserved_qty)::int AS total_stock,
                    MIN(ib.retail_price_cents)::int AS min_price
                    FROM inventory_batch ib
                    WHERE ib.variant_id = pv.id AND ib.deleted_at IS NULL AND ib.status = 'active'
                ) batches ON true
                WHERE pv.product_id = p.id AND pv.deleted_at IS NULL
            ) variants ON true
            WHERE p.id = $1 AND p.deleted_at IS NULL
            """,
            product_id,
        )
        return _row_to_dict(row) if row else None


async def list_storefront_products(
    pool: Any,
    *,
    search: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """商城公开商品列表：仅返回有库存的活跃商品。"""
    async with pool.acquire() as conn:
        where_clauses = [
            "p.deleted_at IS NULL",
            "p.status = 'published'",
            "COALESCE(variants.total_stock, 0) > 0",
        ]
        params: list[Any] = []

        if search:
            params.append(f"%{search}%")
            where_clauses.append(
                f"(p.title ILIKE ${len(params)} OR p.description ILIKE ${len(params)})"
            )

        where = " AND ".join(where_clauses)

        # 先查列表，再过滤价格
        rows = await conn.fetch(
            f"""
            SELECT p.id, p.title, p.slug, p.description, p.category_id,
                   COALESCE(variants.variants, '[]'::jsonb) AS variants,
                   COALESCE(variants.total_stock, 0) AS total_stock,
                   COALESCE(variants.min_price, NULL) AS min_price_cents
            FROM product p
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'id', pv.id,
                    'sku_code', pv.sku_code,
                    'option_values', pv.option_values,
                    'reference_price_cents', pv.reference_price_cents,
                    'batches', COALESCE(batches.batch_list, '[]'::jsonb),
                    'total_stock', COALESCE(batches.total_stock, 0),
                    'min_price_cents', COALESCE(batches.min_price, pv.reference_price_cents)
                )) AS variants,
                SUM(COALESCE(batches.total_stock, 0))::int AS total_stock,
                MIN(COALESCE(batches.min_price, pv.reference_price_cents))::int AS min_price
                FROM product_variant pv
                LEFT JOIN LATERAL (
                    SELECT jsonb_agg(jsonb_build_object(
                        'id', ib.id,
                        'batch_no', ib.batch_no,
                        'available_qty', ib.stock_qty - ib.reserved_qty,
                        'retail_price_cents', ib.retail_price_cents,
                        'currency', ib.currency
                    )) AS batch_list,
                    SUM(ib.stock_qty - ib.reserved_qty)::int AS total_stock,
                    MIN(ib.retail_price_cents)::int AS min_price
                    FROM inventory_batch ib
                    WHERE ib.variant_id = pv.id AND ib.deleted_at IS NULL
                      AND ib.status = 'active' AND (ib.stock_qty - ib.reserved_qty) > 0
                ) batches ON true
                WHERE pv.product_id = p.id AND pv.deleted_at IS NULL
            ) variants ON true
            WHERE {where}
            ORDER BY p.created_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params,
            limit,
            offset,
        )
        results = [_row_to_dict(r) for r in rows]

        # 价格过滤（SQL 后过滤，因为 JSONB 聚合不便在 WHERE 中过滤）
        if min_price is not None:
            results = [
                r
                for r in results
                if r.get("min_price_cents") and r["min_price_cents"] >= min_price
            ]
        if max_price is not None:
            results = [
                r
                for r in results
                if r.get("min_price_cents") and r["min_price_cents"] <= max_price
            ]

        return results


async def get_storefront_product(pool: Any, product_id: str) -> dict | None:
    """商城商品详情：仅返回有库存的活跃商品。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.id, p.title, p.slug, p.description, p.category_id,
                   COALESCE(variants.variants, '[]'::jsonb) AS variants,
                   COALESCE(variants.total_stock, 0) AS total_stock,
                   COALESCE(variants.min_price, NULL) AS min_price_cents
            FROM product p
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'id', pv.id,
                    'sku_code', pv.sku_code,
                    'option_values', pv.option_values,
                    'reference_price_cents', pv.reference_price_cents,
                    'batches', COALESCE(batches.batch_list, '[]'::jsonb),
                    'total_stock', COALESCE(batches.total_stock, 0),
                    'min_price_cents', COALESCE(batches.min_price, pv.reference_price_cents)
                )) AS variants,
                SUM(COALESCE(batches.total_stock, 0))::int AS total_stock,
                MIN(COALESCE(batches.min_price, pv.reference_price_cents))::int AS min_price
                FROM product_variant pv
                LEFT JOIN LATERAL (
                    SELECT jsonb_agg(jsonb_build_object(
                        'id', ib.id,
                        'batch_no', ib.batch_no,
                        'available_qty', ib.stock_qty - ib.reserved_qty,
                        'retail_price_cents', ib.retail_price_cents,
                        'currency', ib.currency
                    )) AS batch_list,
                    SUM(ib.stock_qty - ib.reserved_qty)::int AS total_stock,
                    MIN(ib.retail_price_cents)::int AS min_price
                    FROM inventory_batch ib
                    WHERE ib.variant_id = pv.id AND ib.deleted_at IS NULL
                      AND ib.status = 'active' AND (ib.stock_qty - ib.reserved_qty) > 0
                ) batches ON true
                WHERE pv.product_id = p.id AND pv.deleted_at IS NULL
            ) variants ON true
            WHERE p.id = $1 AND p.deleted_at IS NULL AND p.status = 'published'
            """,
            product_id,
        )
        if row is None:
            return None
        result = _row_to_dict(row)
        if result.get("total_stock", 0) <= 0:
            return None
        return result


async def list_orders(
    pool: Any,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """订单列表：含行项摘要。"""
    async with pool.acquire() as conn:
        where_clauses = ["1=1"]
        params: list[Any] = []

        if status:
            params.append(status)
            where_clauses.append(f"o.status = ${len(params)}")

        where = " AND ".join(where_clauses)

        rows = await conn.fetch(
            f"""
            SELECT o.id, o.cart_id, o.customer_id, o.region_code, o.currency,
                   o.status, o.subtotal_cents, o.discount_cents, o.tax_cents,
                   o.shipping_cents, o.grand_total_cents,
                   o.payment_provider_name, o.billing_address, o.shipping_address,
                   o.created_at, o.updated_at,
                   COALESCE(items.line_items, '[]'::jsonb) AS line_items
            FROM customer_order o
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'id', oli.id,
                    'quantity', oli.quantity,
                    'unit_price_cents', oli.unit_price_cents,
                    'line_total_cents', oli.line_total_cents,
                    'product_title', prod.title,
                    'variant_sku', pv.sku_code,
                    'option_values', pv.option_values
                )) AS line_items
                FROM order_line_item oli
                LEFT JOIN inventory_batch ib ON oli.batch_id = ib.id
                LEFT JOIN product_variant pv ON ib.variant_id = pv.id
                LEFT JOIN product prod ON pv.product_id = prod.id
                WHERE oli.order_id = o.id
            ) items ON true
            WHERE {where}
            ORDER BY o.created_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params,
            limit,
            offset,
        )
        return [_row_to_dict(r) for r in rows]


async def get_order(pool: Any, order_id: str) -> dict | None:
    """单个订单详情。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT o.id, o.cart_id, o.customer_id, o.region_code, o.currency,
                   o.status, o.subtotal_cents, o.discount_cents, o.tax_cents,
                   o.shipping_cents, o.grand_total_cents,
                   o.payment_provider_name, o.payment_intent_id,
                   o.billing_address, o.shipping_address,
                   o.created_at, o.updated_at,
                   COALESCE(items.line_items, '[]'::jsonb) AS line_items
            FROM customer_order o
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'id', oli.id,
                    'quantity', oli.quantity,
                    'unit_price_cents', oli.unit_price_cents,
                    'line_total_cents', oli.line_total_cents,
                    'product_title', prod.title,
                    'variant_sku', pv.sku_code,
                    'option_values', pv.option_values
                )) AS line_items
                FROM order_line_item oli
                LEFT JOIN inventory_batch ib ON oli.batch_id = ib.id
                LEFT JOIN product_variant pv ON ib.variant_id = pv.id
                LEFT JOIN product prod ON pv.product_id = prod.id
                WHERE oli.order_id = o.id
            ) items ON true
            WHERE o.id = $1
            """,
            order_id,
        )
        return _row_to_dict(row) if row else None


async def get_order_by_receipt_token(
    pool: Any, token: str, jwt_secret: str, jwt_algorithm: str
) -> dict | None:
    """凭收据 token 查单。token 是签名的 JWT，payload 含 order_id。"""
    from obase.crypto.util import CryptoUtil

    try:
        payload = CryptoUtil.jwt_decode(
            token=token, secret=jwt_secret, algorithm=jwt_algorithm
        )
    except Exception:
        return None

    order_id = payload.get("order_id")
    if not order_id:
        return None
    return await get_order(pool, order_id)


async def list_customers(
    pool: Any,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """客户列表。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.email, c.phone, c.name, c.customer_group_id,
                   c.status, c.created_at, c.updated_at,
                   COUNT(co.id)::int AS order_count,
                   COALESCE(SUM(co.grand_total_cents), 0)::int AS total_spent_cents
            FROM customer c
            LEFT JOIN customer_order co ON co.customer_id = c.id
            WHERE c.deleted_at IS NULL
            GROUP BY c.id
            ORDER BY c.created_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )
        return [_row_to_dict(r) for r in rows]


async def get_customer(pool: Any, customer_id: str) -> dict | None:
    """单个客户详情（供顾客自己的账号页 / 管理端复用）。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT c.id, c.email, c.phone, c.name, c.customer_group_id,
                   c.status, c.created_at, c.updated_at,
                   COUNT(co.id)::int AS order_count,
                   COALESCE(SUM(co.grand_total_cents), 0)::int AS total_spent_cents
            FROM customer c
            LEFT JOIN customer_order co ON co.customer_id = c.id
            WHERE c.id = $1 AND c.deleted_at IS NULL
            GROUP BY c.id
            """,
            customer_id,
        )
        return _row_to_dict(row) if row else None


async def list_customer_orders(pool: Any, customer_id: str) -> list[dict]:
    """某顾客自己的订单列表（顾客账号页用；跟 list_orders 同构，按 customer_id 过滤）。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT o.id, o.cart_id, o.customer_id, o.region_code, o.currency,
                   o.status, o.subtotal_cents, o.discount_cents, o.tax_cents,
                   o.shipping_cents, o.grand_total_cents,
                   o.payment_provider_name, o.billing_address, o.shipping_address,
                   o.created_at, o.updated_at,
                   COALESCE(items.line_items, '[]'::jsonb) AS line_items
            FROM customer_order o
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'id', oli.id,
                    'batch_id', oli.batch_id,
                    'quantity', oli.quantity,
                    'unit_price_cents', oli.unit_price_cents,
                    'line_total_cents', oli.line_total_cents,
                    'product_title', prod.title,
                    'variant_sku', pv.sku_code,
                    'option_values', pv.option_values
                )) AS line_items
                FROM order_line_item oli
                LEFT JOIN inventory_batch ib ON oli.batch_id = ib.id
                LEFT JOIN product_variant pv ON ib.variant_id = pv.id
                LEFT JOIN product prod ON pv.product_id = prod.id
                WHERE oli.order_id = o.id
            ) items ON true
            WHERE o.customer_id = $1
            ORDER BY o.created_at DESC
            """,
            customer_id,
        )
        return [_row_to_dict(r) for r in rows]


async def list_customer_claims(pool: Any, customer_id: str) -> list[dict]:
    """某顾客自己提交过的 RMA 客诉记录（顾客账号页"我的售后"用）。

    只筛 user_id = customer_id 这一批——claim 表本身是共享表，同时也承载
    普通退货/换货那条链路的记录 (claim_type 不是 'refund'/user_id 为 NULL 的
    那些)，这里只关心透仓 RMA 全自动仲裁产生的记录 (decision 列非空即代表
    走过这条链路)。
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, order_id, batch_id, evidence_image_url, vlm_damage_type,
                   vlm_severity, decision, liable_party, refund_amount_cents,
                   created_at
            FROM claim
            WHERE user_id = $1 AND decision IS NOT NULL
            ORDER BY created_at DESC
            """,
            customer_id,
        )
        return [_row_to_dict(r) for r in rows]


async def get_cart(pool: Any, cart_id: str) -> dict | None:
    """购物车详情：含行项 + 关联商品信息。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT c.id, c.customer_id, c.region_code, c.currency, c.status,
                   c.subtotal_cents, c.discount_cents, c.tax_cents,
                   c.shipping_cents, c.grand_total_cents,
                   c.billing_address, c.shipping_address,
                   c.created_at, c.updated_at,
                   COALESCE(items.line_items, '[]'::jsonb) AS line_items,
                   COALESCE(discounts.discounts, '[]'::jsonb) AS discounts,
                   COALESCE(gift_cards.gift_cards, '[]'::jsonb) AS gift_cards
            FROM cart c
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'id', cli.id,
                    'batch_id', cli.batch_id,
                    'quantity', cli.quantity,
                    'unit_price_cents', cli.unit_price_cents,
                    'line_total_cents', cli.line_total_cents,
                    'product_title', prod.title,
                    'variant_sku', pv.sku_code,
                    'option_values', pv.option_values,
                    'available_qty', ib.stock_qty - ib.reserved_qty + cli.quantity
                )) AS line_items
                FROM cart_line_item cli
                LEFT JOIN inventory_batch ib ON cli.batch_id = ib.id
                LEFT JOIN product_variant pv ON ib.variant_id = pv.id
                LEFT JOIN product prod ON pv.product_id = prod.id
                WHERE cli.cart_id = c.id AND cli.deleted_at IS NULL
            ) items ON true
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'id', cd.id, 'discount_id', cd.discount_id, 'code', d.code,
                    'applied_amount_cents', cd.applied_amount_cents
                )) AS discounts
                FROM cart_discount cd
                JOIN discount d ON d.id = cd.discount_id
                WHERE cd.cart_id = c.id AND cd.deleted_at IS NULL
            ) discounts ON true
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'id', cgc.id, 'gift_card_id', cgc.gift_card_id, 'code', gc.code,
                    'applied_amount_cents', cgc.applied_amount_cents
                )) AS gift_cards
                FROM cart_gift_card cgc
                JOIN gift_card gc ON gc.id = cgc.gift_card_id
                WHERE cgc.cart_id = c.id AND cgc.deleted_at IS NULL
            ) gift_cards ON true
            WHERE c.id = $1 AND c.deleted_at IS NULL
            """,
            cart_id,
        )
        return _row_to_dict(row) if row else None


async def get_dashboard_kpis(pool: Any) -> dict:
    """仪表盘 KPI 指标。"""
    async with pool.acquire() as conn:
        revenue = await conn.fetchrow(
            """
            SELECT COUNT(*)::int AS total_orders,
                   COALESCE(SUM(grand_total_cents), 0)::int AS total_revenue_cents,
                   COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0)::int AS pending_orders,
                   COALESCE(SUM(CASE WHEN created_at >= NOW() - INTERVAL '30 days'
                      THEN grand_total_cents ELSE 0 END), 0)::int AS revenue_30d_cents
            FROM customer_order
            """
        )
        products = await conn.fetchrow(
            """
            SELECT COUNT(*)::int AS total_products,
                   COUNT(CASE WHEN status = 'published' THEN 1 END)::int AS published_products
            FROM product WHERE deleted_at IS NULL
            """
        )
        customers = await conn.fetchrow(
            "SELECT COUNT(*)::int AS total_customers FROM customer WHERE deleted_at IS NULL"
        )
        return {
            "total_orders": revenue["total_orders"],
            "total_revenue_cents": revenue["total_revenue_cents"],
            "pending_orders": revenue["pending_orders"],
            "revenue_30d_cents": revenue["revenue_30d_cents"],
            "total_products": products["total_products"],
            "published_products": products["published_products"],
            "total_customers": customers["total_customers"],
        }


async def list_discounts(pool: Any) -> list[dict]:
    """折扣列表：含规则 + 限制条件。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.id, d.code, d.status, d.created_at, d.updated_at,
                   rule.rule,
                   COALESCE(conditions.conditions, '[]'::jsonb) AS conditions
            FROM discount d
            LEFT JOIN LATERAL (
                SELECT jsonb_build_object(
                    'id', dr.id, 'rule_type', dr.rule_type, 'amount_cents', dr.amount_cents,
                    'percent', dr.percent, 'min_subtotal_cents', dr.min_subtotal_cents,
                    'region_codes', dr.region_codes, 'valid_from', dr.valid_from,
                    'valid_until', dr.valid_until, 'max_uses', dr.max_uses, 'uses_count', dr.uses_count
                ) AS rule
                FROM discount_rule dr WHERE dr.discount_id = d.id
            ) rule ON true
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'id', dc.id, 'condition_type', dc.condition_type, 'target_id', dc.target_id
                )) AS conditions
                FROM discount_condition dc WHERE dc.discount_id = d.id
            ) conditions ON true
            WHERE d.deleted_at IS NULL
            ORDER BY d.created_at DESC
            """
        )
        return [_row_to_dict(r) for r in rows]


async def list_gift_cards(pool: Any) -> list[dict]:
    """礼品卡列表。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, code, initial_balance_cents, balance_cents, currency, status,
                   expires_at, created_at, updated_at
            FROM gift_card WHERE deleted_at IS NULL ORDER BY created_at DESC
            """
        )
        return [_row_to_dict(r) for r in rows]


async def list_return_requests(pool: Any, *, order_id: str | None = None) -> list[dict]:
    """退货申请列表；order_id 可选过滤。"""
    async with pool.acquire() as conn:
        where = "WHERE order_id = $1" if order_id else ""
        params = [order_id] if order_id else []
        rows = await conn.fetch(
            f"""
            SELECT id, order_id, status, items, refund_amount_cents, created_at, updated_at
            FROM return_request {where} ORDER BY created_at DESC
            """,
            *params,
        )
        return [_row_to_dict(r) for r in rows]


async def list_swaps(pool: Any, *, order_id: str | None = None) -> list[dict]:
    """换货单列表；order_id 可选过滤。"""
    async with pool.acquire() as conn:
        where = "WHERE order_id = $1" if order_id else ""
        params = [order_id] if order_id else []
        rows = await conn.fetch(
            f"""
            SELECT id, order_id, status, return_items, new_items, price_difference_cents,
                   payment_status, fulfillment_id, created_at, updated_at
            FROM swap {where} ORDER BY created_at DESC
            """,
            *params,
        )
        return [_row_to_dict(r) for r in rows]


async def list_claims(pool: Any, *, order_id: str | None = None) -> list[dict]:
    """客诉索赔列表；order_id 可选过滤。"""
    async with pool.acquire() as conn:
        where = "WHERE order_id = $1" if order_id else ""
        params = [order_id] if order_id else []
        rows = await conn.fetch(
            f"""
            SELECT id, order_id, status, claim_type, items, refund_amount_cents, new_items,
                   fulfillment_id, created_at, updated_at
            FROM claim {where} ORDER BY created_at DESC
            """,
            *params,
        )
        return [_row_to_dict(r) for r in rows]


async def list_price_lists(pool: Any) -> list[dict]:
    """价格表列表：含条目（变体 + 价格）。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pl.id, pl.name, pl.currency, pl.starts_at, pl.ends_at, pl.status, pl.created_at,
                   COALESCE(items.items, '[]'::jsonb) AS items
            FROM price_list pl
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'id', pli.id, 'variant_id', pli.variant_id, 'price_cents', pli.price_cents,
                    'sku_code', pv.sku_code
                )) AS items
                FROM price_list_item pli
                LEFT JOIN product_variant pv ON pli.variant_id = pv.id
                WHERE pli.price_list_id = pl.id
            ) items ON true
            WHERE pl.deleted_at IS NULL ORDER BY pl.created_at DESC
            """
        )
        return [_row_to_dict(r) for r in rows]


async def list_stock_locations(pool: Any) -> list[dict]:
    """仓位/门店列表。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, region_code, lat, lng, channel_tags, status, created_at
            FROM stock_location WHERE deleted_at IS NULL ORDER BY created_at DESC
            """
        )
        return [_row_to_dict(r) for r in rows]


async def list_sales_channels(pool: Any) -> list[dict]:
    """销售渠道列表：含已发布商品数。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT sc.id, sc.name, sc.status, sc.created_at,
                   COUNT(scp.product_id)::int AS product_count
            FROM sales_channel sc
            LEFT JOIN sales_channel_product scp ON scp.channel_id = sc.id
            WHERE sc.deleted_at IS NULL
            GROUP BY sc.id
            ORDER BY sc.created_at DESC
            """
        )
        return [_row_to_dict(r) for r in rows]


async def list_regions_admin(pool: Any) -> list[dict]:
    """区域列表（管理端全字段：含 status / payment_provider_names）。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT code, name, currency, payment_provider_names, status, created_at
            FROM region WHERE deleted_at IS NULL ORDER BY name
            """
        )
        return [_row_to_dict(r) for r in rows]


async def list_tax_rates(pool: Any, *, region_code: str | None = None) -> list[dict]:
    """税率列表；region_code 可选过滤。"""
    async with pool.acquire() as conn:
        where = (
            "region_code = $1 AND deleted_at IS NULL"
            if region_code
            else "deleted_at IS NULL"
        )
        params = [region_code] if region_code else []
        rows = await conn.fetch(
            f"""
            SELECT id, region_code, name, rate_percent, status, created_at
            FROM tax_rate WHERE {where} ORDER BY created_at DESC
            """,
            *params,
        )
        return [_row_to_dict(r) for r in rows]


async def list_fulfillments(pool: Any, order_id: str) -> list[dict]:
    """某订单的履约单据列表。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, order_id, status, items, provider_name, tracking_number, carrier,
                   created_at, updated_at
            FROM fulfillment WHERE order_id = $1 ORDER BY created_at DESC
            """,
            order_id,
        )
        return [_row_to_dict(r) for r in rows]


async def list_product_categories(pool: Any) -> list[dict]:
    """商品分类列表：按 DFS 序 (lft) 返回，附带父分类名便于展示层级。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.name, c.slug, c.parent_id, p.name AS parent_name,
                   c.status, c.created_at
            FROM product_category c
            LEFT JOIN product_category p ON c.parent_id = p.id
            WHERE c.deleted_at IS NULL
            ORDER BY c.lft
            """
        )
        return [_row_to_dict(r) for r in rows]


async def list_product_collections(pool: Any) -> list[dict]:
    """商品合集列表：附带已收录商品数。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pc.id, pc.name, pc.slug, pc.status, pc.created_at,
                   COUNT(pci.product_id)::int AS product_count
            FROM product_collection pc
            LEFT JOIN product_collection_item pci ON pci.collection_id = pc.id
            WHERE pc.deleted_at IS NULL
            GROUP BY pc.id
            ORDER BY pc.created_at DESC
            """
        )
        return [_row_to_dict(r) for r in rows]


async def list_customer_groups(pool: Any) -> list[dict]:
    """买家分组列表：附带成员数。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT cg.id, cg.name, cg.created_at,
                   COUNT(c.id)::int AS member_count
            FROM customer_group cg
            LEFT JOIN customer c ON c.customer_group_id = cg.id AND c.deleted_at IS NULL
            WHERE cg.deleted_at IS NULL
            GROUP BY cg.id
            ORDER BY cg.created_at DESC
            """
        )
        return [_row_to_dict(r) for r in rows]


async def list_customer_addresses(pool: Any, customer_id: str) -> list[dict]:
    """某买家的收货地址列表。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, customer_id, recipient_name, phone, address_line1, address_line2,
                   city, region_code, postal_code, is_default, created_at
            FROM customer_address
            WHERE customer_id = $1 AND deleted_at IS NULL
            ORDER BY is_default DESC, created_at DESC
            """,
            customer_id,
        )
        return [_row_to_dict(r) for r in rows]


async def list_app_users(pool: Any) -> list[dict]:
    """管理员 (app_user) 账号列表；不返回密码哈希 / 重置令牌。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, email, name, status, created_at, updated_at
            FROM "app_user" WHERE deleted_at IS NULL ORDER BY created_at DESC
            """
        )
        return [_row_to_dict(r) for r in rows]


async def list_batch_jobs(pool: Any) -> list[dict]:
    """长时任务列表。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, job_type, status, payload, result, created_at, updated_at
            FROM batch_job ORDER BY created_at DESC LIMIT 200
            """
        )
        return [_row_to_dict(r) for r in rows]


# ── app.ext (v2.0 供应商治理 / 全自动仲裁) ──────────────────────────────────
# 表结构在 app/ext/schema.py，不是 obase.commerce_batch_schema，故意独立成
# 一段，跟上面原有商城域的读查询分开摆放。统一改造后这些查询指向的是新的
# 本地表 (supplier/claim/channel_broadcast_log/price_benchmark/
# probe_order_log/affiliate_contract/douyin_conversion_log)，不是已经退休
# 不再写入的旧 ClearNode 平行表 (suppliers/rma_claims/channel_broadcast_logs/
# price_benchmarks/probe_order_logs/affiliate_contracts/
# douyin_conversion_logs)——之前这里一直读的是旧表，统一之后 omodul 早就
# 全部改写新表了，旧表数据不会再更新，这几个 admin 只读端点之前一直在读
# 死数据，这次一并修正。


async def list_ext_suppliers(pool: Any) -> list[dict]:
    """供应商列表：信誉分 + escrow 质押余额 + 状态，运营巡查用。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, wallet_account, spatial_polygon, trust_score,
                   escrow_balance, status
            FROM supplier ORDER BY trust_score ASC LIMIT 200
            """
        )
        return [_row_to_dict(r) for r in rows]


async def get_supplier_by_wallet(pool: Any, wallet_account: str) -> dict | None:
    """按 wallet_account 查单个供应商自己的状态（小型供应商门户"查询我的状态"用）。

    供应商目前没有真正的登录体系 (wallet_account 只是自由字符串，不是账号
    凭据)——这是唯一现有的"准身份"标识，先用它做查询入口；wallet_account
    也没有唯一约束 (claim_origin_workflow 允许重复注册)，取最近一次注册的
    那条记录。真正的供应商账号体系是后续工作，不在这轮范围内。
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, wallet_account, polygon_name, trust_score,
                   escrow_balance, status, created_at
            FROM supplier
            WHERE wallet_account = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            wallet_account,
        )
        return _row_to_dict(row) if row else None


async def list_ext_rma_claims(pool: Any) -> list[dict]:
    """客诉仲裁记录：VLM 判损 + 裁决结果，运营核查用。仲裁字段并入了共享
    claim 表 (见 app/ext/schema.py._ensure_shared_table_local_columns)，
    这里只筛出真正走过仲裁流程的行 (decision IS NOT NULL)。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, order_id, batch_id, user_id, evidence_image_url,
                   vlm_damage_type, vlm_severity, decision, liable_party, created_at
            FROM claim WHERE decision IS NOT NULL
            ORDER BY created_at DESC LIMIT 200
            """
        )
        return [_row_to_dict(r) for r in rows]


async def list_ext_broadcast_logs(pool: Any) -> list[dict]:
    """微信视频号播报记录：文案 + 发布状态，运营核查用。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, batch_id, broadcast_type, llm_copywriting, wechat_feed_id,
                   mini_program_path, status, error_message, created_at, published_at
            FROM channel_broadcast_log ORDER BY created_at DESC LIMIT 200
            """
        )
        return [_row_to_dict(r) for r in rows]


async def list_ext_price_benchmarks(pool: Any) -> list[dict]:
    """价格基线记录：爬虫/众包小票来源的归一化单价，运营核查定价依据用。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, variant_id, raw_item_name, source_type, competitor_name,
                   raw_price_cents, raw_unit, normalized_price_per_unit, captured_at
            FROM price_benchmark ORDER BY captured_at DESC LIMIT 200
            """
        )
        return [_row_to_dict(r) for r in rows]


async def list_ext_probe_order_logs(pool: Any) -> list[dict]:
    """试探单做市日志：试探价 + 观测销售速度 + 状态，运营核查定价引擎用。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, batch_id, probe_price_cents, traffic_exposure,
                   observed_sales_velocity, status, created_at, resolved_at
            FROM probe_order_log ORDER BY created_at DESC LIMIT 200
            """
        )
        return [_row_to_dict(r) for r in rows]


async def list_ext_affiliate_contracts(pool: Any) -> list[dict]:
    """抖音达人智能分润契约：领主税/雇佣兵悬赏的绑定关系，运营核查用。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, douyin_uid, contract_type, bound_entity_id, commission_logic,
                   status, created_at
            FROM affiliate_contract ORDER BY created_at DESC LIMIT 200
            """
        )
        return [_row_to_dict(r) for r in rows]


async def list_ext_douyin_conversion_logs(pool: Any) -> list[dict]:
    """抖音引流转化流水：每笔订单产生的分润明细 + 结算状态，运营核查用。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, order_id, douyin_uid, contract_id, dividend_amount_cents,
                   settlement_status, created_at
            FROM douyin_conversion_log ORDER BY created_at DESC LIMIT 200
            """
        )
        return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: Any) -> dict:
    """将 asyncpg Record 转为 dict，处理 UUID 和 JSONB 序列化。"""
    if row is None:
        return {}
    d = dict(row)
    for k, v in d.items():
        if hasattr(v, "hex"):  # UUID
            d[k] = str(v)
        elif hasattr(v, "isoformat"):  # datetime
            d[k] = v.isoformat()
        elif isinstance(v, str) and k in (
            "option_values",
            "billing_address",
            "shipping_address",
            "variants",
            "line_items",
            "items",
            "return_items",
            "new_items",
            "rule",
            "conditions",
            "options",
            "payload",
            "result",
            "discounts",
            "gift_cards",
            "spatial_polygon",
            "commission_logic",
        ):
            try:
                d[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                pass
    return d
