"""Hemall core4 增量 schema（第四轮：即时配送/临期/补货/个性化/监控）。

幂等 DDL，启动期与迁移脚本共用。只加表/列，不删数据：
- customer_order.delivery_status / delivery_slot：自营配送状态与所选时段；
- core4_delivery_task：自营配送任务（接单→取货→送达+拍照），一单一任务；
- inventory_batch.list_price_cents / discount_label 等：临期折扣的原价与标签
  （折扣价写回 retail_price_cents，旧镜像不读这些列，可安全回滚）；
- store_inventory_entry.loss_category：损耗分类（过期/破损/试吃/其他）；
- core4_purchase_order / core4_purchase_order_line：补货建议生成的采购单草稿；
- core4_alert：系统状态页的告警记录（不外发）。
"""

from __future__ import annotations

from typing import Any

CORE4_DELIVERY_DDL = """
ALTER TABLE customer_order ADD COLUMN IF NOT EXISTS delivery_status TEXT;
ALTER TABLE customer_order ADD COLUMN IF NOT EXISTS delivery_slot TEXT;

CREATE TABLE IF NOT EXISTS core4_delivery_task (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL UNIQUE REFERENCES customer_order(id),
    slot_date DATE NOT NULL,
    slot_start TEXT NOT NULL,
    slot_end TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'picked', 'delivered', 'cancelled')),
    assigned_to TEXT NOT NULL DEFAULT '',
    distance_km NUMERIC(8, 2),
    fee_cents INTEGER NOT NULL DEFAULT 0,
    proof_photo_url TEXT,
    note TEXT NOT NULL DEFAULT '',
    claimed_at TIMESTAMPTZ,
    picked_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_core4_delivery_task_status
    ON core4_delivery_task(status, slot_date, slot_start);
CREATE INDEX IF NOT EXISTS idx_core4_delivery_task_slot
    ON core4_delivery_task(slot_date, slot_start, status);
"""

CORE4_EXPIRY_DDL = """
ALTER TABLE inventory_batch ADD COLUMN IF NOT EXISTS list_price_cents INTEGER;
ALTER TABLE inventory_batch ADD COLUMN IF NOT EXISTS discount_label TEXT;
ALTER TABLE inventory_batch ADD COLUMN IF NOT EXISTS discount_reason TEXT;
ALTER TABLE inventory_batch ADD COLUMN IF NOT EXISTS discount_updated_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_inventory_batch_expiry_discount
    ON inventory_batch(expiration_time)
    WHERE status = 'active' AND discount_label IS NOT NULL;

ALTER TABLE store_inventory_entry ADD COLUMN IF NOT EXISTS loss_category TEXT;
CREATE INDEX IF NOT EXISTS idx_store_inventory_entry_loss
    ON store_inventory_entry(biz_date, loss_category)
    WHERE entry_type = 'loss';
"""

CORE4_REPLENISH_DDL = """
CREATE TABLE IF NOT EXISTS core4_purchase_order (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_name TEXT NOT NULL DEFAULT '',
    supplier_contact TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'confirmed', 'received', 'cancelled')),
    expected_arrival DATE,
    total_amount_cents INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_core4_purchase_order_status
    ON core4_purchase_order(status, created_at DESC);

CREATE TABLE IF NOT EXISTS core4_purchase_order_line (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    po_id UUID NOT NULL REFERENCES core4_purchase_order(id) ON DELETE CASCADE,
    variant_id UUID NOT NULL REFERENCES product_variant(id),
    sku_code TEXT NOT NULL DEFAULT '',
    product_name TEXT NOT NULL DEFAULT '',
    suggested_qty INTEGER NOT NULL CHECK (suggested_qty > 0),
    unit TEXT NOT NULL DEFAULT '件',
    unit_cost_cents INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_core4_purchase_order_line_po
    ON core4_purchase_order_line(po_id);
"""


async def ensure_core4_schema(pool: Any) -> None:
    async with pool.acquire() as conn:
        await conn.execute(CORE4_DELIVERY_DDL)
        await conn.execute(CORE4_EXPIRY_DDL)
        await conn.execute(CORE4_REPLENISH_DDL)
