-- 回滚 20261110 core2。旧代码不读这些列/表，回滚后可安全保留数据；
-- 如需彻底移除再执行本脚本（会丢失改价记录与别名/标记）。
BEGIN;
DROP TABLE IF EXISTS batch_price_change;
ALTER TABLE customer_order DROP COLUMN IF EXISTS pickup_arrived_at;
ALTER TABLE customer DROP COLUMN IF EXISTS excluded_from_reports;
DROP INDEX IF EXISTS idx_product_search_aliases;
ALTER TABLE product DROP COLUMN IF EXISTS search_aliases;
COMMIT;
