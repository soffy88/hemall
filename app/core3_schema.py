"""Hemall core3 增量 schema（第三轮：店员手机拍照换商品图 / 拍照上新）。

幂等 DDL，启动期与迁移脚本共用。只加表，不动已有数据：
- product_photo：商品实拍图相册。每次上传一行；换主图时旧行标记 replaced
  并保留文件（不物理删除），可恢复。
  previous_images 记录被覆盖前每个批次的 shelf_image_url，删除/恢复时按原值还原。
"""

from __future__ import annotations

from typing import Any

CORE3_DDL = """
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
"""


async def ensure_core3_schema(pool: Any) -> None:
    async with pool.acquire() as conn:
        await conn.execute(CORE3_DDL)
