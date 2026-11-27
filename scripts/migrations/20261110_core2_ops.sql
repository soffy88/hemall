-- 20261110 core2：第二轮 Hemall 本体（搜索别名/测试账号排除/自提到店/批次改价留痕）。
-- 幂等；应用启动时也会执行同一份 DDL（app/core2_schema.py）。上线前单独跑一次并记录。
BEGIN;

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

COMMIT;
