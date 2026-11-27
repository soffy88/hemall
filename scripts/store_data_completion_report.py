#!/usr/bin/env python3
"""导出“待补资料”清单：缺保质期/产地/储存条件/图片/价格的商品与批次。

默认只读，不隐藏任何商品（前台可见性由 HEMALL_CATALOG_STRICT_DATA_GATE 控制，
默认关）。输出 CSV 供门店逐项补全。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os

import asyncpg


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("HEMALL_PG_DSN", ""))
    parser.add_argument("--out", default="store-data-completion.csv")
    args = parser.parse_args()
    if not args.dsn:
        raise SystemExit("需要 --dsn 或 HEMALL_PG_DSN")
    conn = await asyncpg.connect(args.dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT p.title, pv.sku_code, ib.batch_no,
                   pv.pricing_mode,
                   CASE WHEN ib.expiration_time IS NULL THEN '缺保质期' END AS missing_expiry,
                   CASE WHEN BTRIM(COALESCE(ib.origin_region, '')) = ''
                        THEN '缺产地' END AS missing_origin,
                   CASE WHEN BTRIM(COALESCE(ib.storage_condition, '')) = ''
                        THEN '缺储存条件' END AS missing_storage,
                   CASE WHEN ib.shelf_life_days IS NULL OR ib.shelf_life_days <= 0
                        THEN '缺保质天数' END AS missing_shelf_life,
                   CASE WHEN COALESCE(pv.reference_price_cents, pv.unit_price_cents) IS NULL
                        THEN '缺售价' END AS missing_price,
                   CASE WHEN COALESCE(ib.shelf_image_url, '') = ''
                         AND COALESCE(jsonb_array_length(ib.media_assets), 0) = 0
                        THEN '缺实拍图' END AS missing_image
            FROM product p
            JOIN product_variant pv ON pv.product_id = p.id AND pv.deleted_at IS NULL
            LEFT JOIN inventory_batch ib ON ib.variant_id = pv.id AND ib.deleted_at IS NULL
            WHERE p.deleted_at IS NULL
              AND (
                ib.id IS NULL OR ib.expiration_time IS NULL
                OR BTRIM(COALESCE(ib.origin_region, '')) = ''
                OR BTRIM(COALESCE(ib.storage_condition, '')) = ''
                OR ib.shelf_life_days IS NULL OR ib.shelf_life_days <= 0
                OR COALESCE(pv.reference_price_cents, pv.unit_price_cents) IS NULL
                OR (COALESCE(ib.shelf_image_url, '') = ''
                    AND COALESCE(jsonb_array_length(ib.media_assets), 0) = 0)
              )
            ORDER BY p.title, pv.sku_code
            """
        )
    finally:
        await conn.close()
    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "商品",
                "SKU",
                "批次",
                "计价方式",
                "缺项",
            ]
        )
        for row in rows:
            missing = [
                value
                for key in (
                    "missing_expiry",
                    "missing_origin",
                    "missing_storage",
                    "missing_shelf_life",
                    "missing_price",
                    "missing_image",
                )
                if (value := row[key])
            ]
            writer.writerow(
                [
                    row["title"],
                    row["sku_code"],
                    row["batch_no"] or "",
                    row["pricing_mode"],
                    "；".join(missing),
                ]
            )
    print(f"写出 {len(rows)} 行 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
