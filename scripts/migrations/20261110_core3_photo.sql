-- 20261110 core3：店员手机拍照换商品图 / 拍照上新（product_photo）。
-- 幂等；应用启动时也会执行同一份 DDL（app/core3_schema.py）。上线前单独跑一次并记录。
BEGIN;

CREATE TABLE IF NOT EXISTS product_photo (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID NOT NULL REFERENCES product(id),
    variant_id UUID REFERENCES product_variant(id),
    url TEXT NOT NULL,
    thumb_url TEXT,
    sha256 TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    bytes INTEGER,
    original_filename TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'staff_upload',
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    position INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    replaced_by UUID,
    previous_images JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_product_photo_product
    ON product_photo(product_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_product_photo_active
    ON product_photo(product_id, position)
    WHERE deleted_at IS NULL AND status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS uq_product_photo_primary
    ON product_photo(product_id)
    WHERE is_primary AND deleted_at IS NULL AND status = 'active';

COMMIT;
