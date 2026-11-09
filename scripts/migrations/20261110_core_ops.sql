-- 20261110 core-ops：门店运营（进销存/报表/称重确认/自提）增量迁移。
-- 幂等；可由 app.p0_schema 启动时重复执行。上线前单独跑一次并记录。
BEGIN;

CREATE TABLE IF NOT EXISTS store_inventory_entry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    biz_date DATE NOT NULL DEFAULT (now() AT TIME ZONE 'Asia/Shanghai')::date,
    variant_id UUID REFERENCES product_variant(id),
    product_id UUID,
    sku_code TEXT NOT NULL DEFAULT '',
    product_name TEXT NOT NULL,
    entry_type TEXT NOT NULL CHECK (entry_type IN ('in', 'loss')),
    qty INTEGER NOT NULL CHECK (qty > 0),
    unit TEXT NOT NULL DEFAULT 'g',
    unit_cost_cents INTEGER,
    supplier TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    operator_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_store_inventory_entry_date
    ON store_inventory_entry(biz_date DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_store_inventory_entry_variant
    ON store_inventory_entry(variant_id, biz_date DESC);

ALTER TABLE customer_order ADD COLUMN IF NOT EXISTS shipping_method_code TEXT;
ALTER TABLE customer_order ADD COLUMN IF NOT EXISTS weigh_status TEXT;
ALTER TABLE cart ADD COLUMN IF NOT EXISTS shipping_method_code TEXT;

ALTER TABLE weigh_settlement DROP CONSTRAINT IF EXISTS weigh_settlement_status_check;
ALTER TABLE weigh_settlement ADD CONSTRAINT weigh_settlement_status_check
    CHECK (status = ANY (ARRAY['recorded', 'auto_refund', 'manual_refund_pending',
        'charge_pending', 'needs_customer_confirmation', 'customer_accepted',
        'customer_rejected', 'refunded', 'void', 'estimate_only']));

-- 修复 2026-11-10 前下单写入的双编码地址（jsonb 字符串）。
UPDATE customer_order
SET billing_address = (billing_address #>> '{}')::jsonb
WHERE jsonb_typeof(billing_address) = 'string'
  AND (billing_address #>> '{}') LIKE '{%';
UPDATE customer_order
SET shipping_address = (shipping_address #>> '{}')::jsonb
WHERE jsonb_typeof(shipping_address) = 'string'
  AND (shipping_address #>> '{}') LIKE '{%';

COMMIT;
