-- P0-08 食品资料门禁迁移（幂等；只加列/约束，不改商品数据）
-- 执行前必须先做 pg_dump 备份。应用启动时也会通过 app/p0_schema.py 自动执行。

BEGIN;

ALTER TABLE product
    ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE inventory_batch
    ADD COLUMN IF NOT EXISTS origin_region TEXT;
ALTER TABLE inventory_batch
    ADD COLUMN IF NOT EXISTS storage_condition TEXT;
ALTER TABLE inventory_batch
    ADD COLUMN IF NOT EXISTS shelf_life_days INTEGER;
ALTER TABLE inventory_batch
    ADD COLUMN IF NOT EXISTS quality_certificate_url TEXT;
ALTER TABLE inventory_batch
    ADD COLUMN IF NOT EXISTS is_test_batch BOOLEAN NOT NULL DEFAULT FALSE;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'inventory_batch_shelf_life_days_check'
    ) THEN
        ALTER TABLE inventory_batch
            ADD CONSTRAINT inventory_batch_shelf_life_days_check
            CHECK (shelf_life_days IS NULL OR shelf_life_days > 0);
    END IF;
END $$;

COMMIT;
