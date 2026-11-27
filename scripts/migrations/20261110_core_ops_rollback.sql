-- 回滚 20261110 core-ops。地址列已从“JSON 字符串”规范化为对象，
-- 旧版本代码同时接受对象与字符串，故无需也不会重新双编码。
BEGIN;
ALTER TABLE customer_order DROP COLUMN IF EXISTS shipping_method_code;
ALTER TABLE customer_order DROP COLUMN IF EXISTS weigh_status;
ALTER TABLE cart DROP COLUMN IF EXISTS shipping_method_code;
ALTER TABLE weigh_settlement DROP CONSTRAINT IF EXISTS weigh_settlement_status_check;
ALTER TABLE weigh_settlement ADD CONSTRAINT weigh_settlement_status_check
    CHECK (status = ANY (ARRAY['recorded', 'auto_refund', 'manual_refund_pending',
        'charge_pending', 'needs_customer_confirmation', 'customer_accepted',
        'customer_rejected', 'refunded', 'void']));
DROP TABLE IF EXISTS store_inventory_entry;
COMMIT;
