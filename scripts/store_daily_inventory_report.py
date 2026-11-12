#!/usr/bin/env python3
"""门店每日进销存报表（进货/销售/剩余/报损/进价/售价/毛利）。

数据来源：
- stock_movement：收货(进货)、shipment(销售)、damage(报损)、调整；
- inventory_batch：当日结存 stock_qty（称重商品单位为克，按件商品为件）；
- product_variant：unit_price_cents（称重按分/斤）与 reference_price_cents。

输出 CSV：日期、SKU、商品、计价方式、单位、进货量、销售量、报损量、
剩余量、进货均价(元/单位)、售价(元/单位)、销售额(元)、毛利(元)。
称重行按克记录数量、按 500 克/斤换算金额；按件行按件。
毛利 = 销售额 - 销售数量 × 进货均价（进货均价缺失时留空，不猜测）。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
from datetime import date
from decimal import Decimal

import asyncpg


async def build_rows(dsn: str, biz_date: date) -> list[list[object]]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            WITH mv AS (
                SELECT variant_id,
                       SUM(quantity) FILTER (
                           WHERE movement_type IN
                               ('receipt','return_in','transfer_in','adjustment_up')
                       ) AS purchase_qty,
                       SUM(quantity) FILTER (WHERE movement_type = 'shipment') AS sales_qty,
                       SUM(quantity) FILTER (WHERE movement_type = 'damage') AS waste_qty,
                       AVG(unit_cost) FILTER (
                           WHERE movement_type IN
                               ('receipt','return_in','transfer_in','adjustment_up')
                             AND unit_cost IS NOT NULL
                       ) AS avg_cost_yuan
                FROM stock_movement
                WHERE created_at >= $1::date
                  AND created_at < ($1::date + INTERVAL '1 day')
                GROUP BY variant_id
            )
            SELECT pv.sku_code, p.title,
                   COALESCE(pv.pricing_mode, 'piece') AS pricing_mode,
                   CASE WHEN pv.stock_unit = 'gram' THEN '克' ELSE '件' END AS unit,
                   COALESCE(mv.purchase_qty, 0)::int AS purchase_qty,
                   COALESCE(mv.sales_qty, 0)::int AS sales_qty,
                   COALESCE(mv.waste_qty, 0)::int AS waste_qty,
                   COALESCE(SUM(ib.stock_qty) FILTER (WHERE ib.deleted_at IS NULL), 0)::int
                       AS remaining_qty,
                   mv.avg_cost_yuan,
                   COALESCE(pv.unit_price_cents, pv.reference_price_cents) AS price_cents
            FROM product_variant pv
            JOIN product p ON p.id = pv.product_id AND p.deleted_at IS NULL
            LEFT JOIN mv ON mv.variant_id = pv.id
            LEFT JOIN inventory_batch ib ON ib.variant_id = pv.id
            WHERE pv.deleted_at IS NULL
              AND (mv.variant_id IS NOT NULL OR ib.id IS NOT NULL)
            GROUP BY pv.id, p.title, pv.sku_code, pv.pricing_mode, pv.stock_unit,
                     pv.unit_price_cents, pv.reference_price_cents,
                     mv.purchase_qty, mv.sales_qty, mv.waste_qty, mv.avg_cost_yuan
            ORDER BY p.title, pv.sku_code
            """,
            biz_date,
        )
    finally:
        await conn.close()

    out: list[list[object]] = []
    for row in rows:
        pricing = row["pricing_mode"]
        unit = row["unit"]
        price_cents = row["price_cents"]
        cost_yuan = row["avg_cost_yuan"]
        if pricing == "weight" and price_cents is not None:
            price_yuan_per_jin = Decimal(price_cents) / 100
            revenue = Decimal(row["sales_qty"]) * price_yuan_per_jin / Decimal(500)
            price_per_unit = price_yuan_per_jin
        else:
            price_per_unit = Decimal(price_cents) / 100 if price_cents is not None else None
            revenue = (
                Decimal(row["sales_qty"]) * price_per_unit if price_per_unit is not None else None
            )
        if cost_yuan is not None and revenue is not None:
            cost_qty = Decimal(row["sales_qty"])
            cost = (
                cost_qty * Decimal(cost_yuan) / Decimal(500)
                if pricing == "weight"
                else cost_qty * Decimal(cost_yuan)
            )
            margin: Decimal | None = revenue - cost
        else:
            margin = None
        out.append(
            [
                biz_date.isoformat(),
                row["sku_code"],
                row["title"],
                "按重量" if pricing == "weight" else "按件",
                unit,
                row["purchase_qty"],
                row["sales_qty"],
                row["waste_qty"],
                row["remaining_qty"],
                f"{Decimal(cost_yuan):.2f}" if cost_yuan is not None else "",
                f"{price_per_unit:.2f}" if price_per_unit is not None else "",
                f"{revenue:.2f}" if revenue is not None else "",
                f"{margin:.2f}" if margin is not None else "",
            ]
        )
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--dsn", default=os.environ.get("HEMALL_PG_DSN", ""))
    parser.add_argument("--out", default="store-daily-inventory.csv")
    args = parser.parse_args()
    if not args.dsn:
        raise SystemExit("需要 --dsn 或 HEMALL_PG_DSN")
    biz = date.fromisoformat(args.date)
    rows = await build_rows(args.dsn, biz)
    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "日期",
                "SKU",
                "商品",
                "计价方式",
                "数量单位",
                "进货量",
                "销售量",
                "报损量",
                "剩余量",
                "进货均价(元/单位)",
                "售价(元/单位)",
                "销售额(元)",
                "毛利(元)",
            ]
        )
        writer.writerows(rows)
    print(f"写出 {len(rows)} 行 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
