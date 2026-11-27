"""Hemall core2 增量 schema（第二轮门店运营）。

幂等 DDL，启动期与迁移脚本共用。只加列/表，不删数据：
- product.search_aliases：商品搜索别名（如 上海青/青菜、大蒜/蒜头）。
- customer.excluded_from_reports：测试/停用账号，从报表与顾客统计排除。
- customer_order.pickup_arrived_at：顾客点“我已到店”的时间（自提提醒）。
- batch_price_change：批次直接改价的操作留痕（旧价/新价/操作人/时间）。
"""

from __future__ import annotations

from typing import Any

CORE2_DDL = """
ALTER TABLE product ADD COLUMN IF NOT EXISTS search_aliases TEXT[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS idx_product_search_aliases
    ON product USING GIN (search_aliases);

ALTER TABLE customer ADD COLUMN IF NOT EXISTS excluded_from_reports BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE customer_order ADD COLUMN IF NOT EXISTS pickup_arrived_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS batch_price_change (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id UUID NOT NULL REFERENCES inventory_batch(id),
    old_price_cents INTEGER NOT NULL,
    new_price_cents INTEGER NOT NULL,
    operator_id TEXT NOT NULL DEFAULT '',
    operator_name TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_batch_price_change_batch
    ON batch_price_change(batch_id, created_at DESC);
"""


async def ensure_core2_schema(pool: Any) -> None:
    async with pool.acquire() as conn:
        await conn.execute(CORE2_DDL)
