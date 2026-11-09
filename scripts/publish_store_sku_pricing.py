#!/usr/bin/env python3
"""按公开市场参考价上架门店 99 SKU（可 dry-run / apply / 生成回滚脚本）。

规则（BRIEF 2026-11-09）：
- 有“建议售价”的 SKU 上架；售价 = 建议售价。
- 若同时有“参考进价”且 建议售价 < 进价 × 1.15，则售价 = 进价 × 1.15 向上取到 0.1 元。
- 称重商品按元/斤（计价基准 500g）；按件商品按件。
- 参考进价写入批次成本字段；成本来源标注“公开市场参考价”。
- 无售价的 SKU 保持草稿（新商品 product=draft；合并商品只把新变体置 draft）。
- 不编造库存：每个上架 SKU 只建一个 0 库存“占位批次”（inspection pending），
  前台按现有缺货预订门禁显示“缺货可预订”。
- 图片：新商品用 assets/store-sku/<SKU>.jpg；复用商品优先沿用原批次实拍图，
  不覆盖原批次/原变体。
- 幂等：批次按 batch_no=STORE-<SKU> upsert；变体/商品按 sku_code 更新。
- 回滚：--rollback-out 写 SQL（恢复商品/变体原状态与价格、删除本次占位批次）。

用法：
  python scripts/publish_store_sku_pricing.py --dsn postgresql://... --dry-run
  HEMALL_IMPORT_ALLOW_PROD=1 python scripts/publish_store_sku_pricing.py --dsn ... --apply \
      --rollback-out /data/soffy/wt/hemal-publish-run/rollback_20261109.sql
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import shutil
import uuid
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "store-sku-pricing.csv"
DEFAULT_MEDIA = ROOT / "assets" / "store-sku"
DEFAULT_SPIDER = Path("/data/docker/volumes/hemal_hemall-spider-media/_data")

LOCATION_ID = "00000000-0000-4000-8000-000000000003"  # 广州仓
WEIGHT_OPTIONS = (
    '[{"label":"约0.5斤","grams":250},{"label":"约1斤","grams":500},'
    '{"label":"约2斤","grams":1000}]'
)
COST_NOTE_COST = "成本=公开市场参考价（2026-11-08，非供应商当日报价）"
COST_NOTE_UNKNOWN = "进价未找到（成本暂按售价占位，勿用于毛利核算）"
PRICE_SUFFIX = "（价格为公开市场参考价 2026-11-08，以门店当日标价为准）"
LIVE_NOTE = "到店现捞/暂养条件就绪后供应；当前缺货可预订，按实际称重结算。"
LIVE_SKUS = {"AQU-001", "AQU-002", "AQU-004", "AQU-005", "AQU-006", "AQU-007", "AQU-008"}


def dec(raw: str | None) -> Decimal | None:
    text = (raw or "").strip()
    if not text or text in ("待报价", "待填", "-"):
        return None
    try:
        value = Decimal(text)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"价格不是数字: {raw!r}") from exc
    return value if value > 0 else None


def effective_price(cost: Decimal | None, suggested: Decimal | None) -> tuple[Decimal | None, bool]:
    """返回 (上架价, 是否触发进价×1.15 保护)。"""
    if suggested is None:
        return None, False
    if cost is not None and suggested < cost * Decimal("1.15"):
        raw = cost * Decimal("1.15")
        tenths = (raw * 10).to_integral_value(rounding=ROUND_CEILING)
        return (tenths / 10).quantize(Decimal("0.1")), True
    return suggested, False


def load_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) != 99:
        raise SystemExit(f"期望 99 行，实际 {len(rows)}")
    planned = []
    for row in rows:
        sku = (row.get("SKU") or "").strip()
        pricing = (row.get("计价方式") or "").strip()
        if pricing not in ("weight", "piece"):
            raise SystemExit(f"{sku}: 计价方式非法 {pricing!r}")
        unit = (row.get("计价单位") or ("斤" if pricing == "weight" else "件")).strip()
        if pricing == "weight" and unit != "斤":
            raise SystemExit(f"{sku}: 称重商品单位必须是斤，实际 {unit!r}")
        if pricing == "piece" and unit == "斤":
            raise SystemExit(f"{sku}: 按件商品单位不能是斤")
        cost = dec(row.get("参考进价"))
        suggested = dec(row.get("建议售价"))
        sale, adjusted = effective_price(cost, suggested)
        planned.append(
            {
                "sku": sku,
                "name": (row.get("商品名称") or "").strip(),
                "pricing": pricing,
                "unit": unit,
                "basis": 500 if pricing == "weight" else 1,
                "cost": cost,
                "suggested": suggested,
                "sale": sale,
                "adjusted": adjusted,
                "image": (row.get("图片") or "").split("；")[0].strip(),
                "note": (row.get("备注") or "").strip(),
            }
        )
    return planned


def yuan_to_cents(value: Decimal) -> int:
    return int((value * 100).to_integral_value())


def check_dsn(dsn: str) -> None:
    dbname = dsn.rsplit("/", 1)[-1].split("?")[0].lower()
    if "prod" in dbname and os.environ.get("HEMALL_IMPORT_ALLOW_PROD") != "1":
        raise SystemExit(
            f"拒绝写入生产库 {dbname!r}：确认后显式 HEMALL_IMPORT_ALLOW_PROD=1"
        )


def copy_images(planned: list[dict], media_root: Path, spider_root: Path) -> dict[str, str]:
    """把参考图复制到 /media/spider/store-sku/，返回 sku -> url。"""
    target_dir = spider_root / "store-sku"
    target_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    for row in planned:
        if row["sale"] is None:
            continue
        src = ROOT / row["image"] if row["image"] else None
        if src is None or not src.is_file():
            continue
        dst = target_dir / f"{row['sku']}.jpg"
        if not dst.is_file() or dst.stat().st_size != src.stat().st_size:
            shutil.copyfile(src, dst)
        mapping[row["sku"]] = f"/media/spider/store-sku/{row['sku']}.jpg"
    return mapping


def sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def build_rollback(records: list[dict]) -> str:
    lines = [
        "-- 回滚：恢复 99 SKU 上架前的商品/变体状态与价格，删除本次占位批次",
        "-- 生成：hemal-publish-run scripts/publish_store_sku_pricing.py",
        "BEGIN;",
    ]
    for rec in records:
        v = rec["before_variant"]
        p = rec["before_product"]
        lines.append(
            f"UPDATE product SET status = {sql_literal(p['status'])}, "
            f"description = {sql_literal(p['description'])}, updated_at = NOW() "
            f"WHERE id = {sql_literal(rec['product_id'])};"
        )
        lines.append(
            f"UPDATE product_variant SET status = {sql_literal(v['status'])}, "
            f"pricing_mode = {sql_literal(v['pricing_mode'])}, "
            f"reference_price_cents = {sql_literal(v['reference_price_cents'])}, "
            f"unit_price_cents = {sql_literal(v['unit_price_cents'])}, "
            f"price_basis_grams = {sql_literal(v['price_basis_grams'])}, "
            f"updated_at = NOW() WHERE sku_code = {sql_literal(rec['sku'])};"
        )
        if not rec["batch_existed"]:
            lines.append(
                "DELETE FROM inventory_batch WHERE batch_no = "
                f"{sql_literal('STORE-' + rec['sku'])};"
            )
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


async def publish(
    dsn: str,
    planned: list[dict],
    image_urls: dict[str, str],
    *,
    apply: bool,
    rollback_out: Path | None,
    report_out: Path | None,
) -> dict:
    conn = await asyncpg.connect(dsn)
    records: list[dict] = []
    stats = {
        "total": len(planned),
        "priced": 0,
        "unpriced": 0,
        "price_adjusted": 0,
        "products_published": 0,
        "variants_activated": 0,
        "batches_created": 0,
        "batches_updated": 0,
        "images": 0,
        "reused_image": 0,
        "skipped_not_found": 0,
    }
    try:
        async with conn.transaction():
            for row in planned:
                sku = row["sku"]
                vrow = await conn.fetchrow(
                    """
                    SELECT pv.id AS variant_id, pv.product_id, pv.sku_code, pv.status,
                           pv.pricing_mode, pv.reference_price_cents, pv.unit_price_cents,
                           pv.price_basis_grams, p.id AS pid, p.status AS product_status,
                           p.slug, p.description, p.title
                    FROM product_variant pv
                    JOIN product p ON p.id = pv.product_id AND p.deleted_at IS NULL
                    WHERE pv.sku_code = $1 AND pv.deleted_at IS NULL
                    """,
                    sku,
                )
                if vrow is None:
                    stats["skipped_not_found"] += 1
                    continue
                product_id = str(vrow["product_id"])
                variant_id = str(vrow["variant_id"])
                reused = not str(vrow["slug"]).startswith("store-")
                batch_no = f"STORE-{sku}"
                batch = await conn.fetchrow(
                    "SELECT id, status FROM inventory_batch WHERE batch_no = $1",
                    batch_no,
                )

                # 复用商品：沿用原批次实拍图（不覆盖原批次）。
                image_url = image_urls.get(sku)
                if reused:
                    orig = await conn.fetchrow(
                        """
                        SELECT ib.media_assets, ib.shelf_image_url
                        FROM inventory_batch ib
                        JOIN product_variant pv ON pv.id = ib.variant_id
                        WHERE pv.product_id = $1 AND ib.deleted_at IS NULL
                          AND ib.batch_no <> $2
                        ORDER BY ib.created_at ASC
                        LIMIT 1
                        """,
                        product_id,
                        batch_no,
                    )
                    if orig is not None:
                        media = orig["media_assets"]
                        if isinstance(media, str):
                            try:
                                media = json.loads(media)
                            except Exception:  # noqa: BLE001
                                media = []
                        if media and isinstance(media, list):
                            first = media[0]
                            if isinstance(first, dict):
                                image_url = first.get("url") or first.get("src")
                            else:
                                image_url = str(first)
                        elif orig["shelf_image_url"]:
                            image_url = orig["shelf_image_url"]
                        if image_url:
                            stats["reused_image"] += 1

                rec = {
                    "sku": sku,
                    "title": row["name"],
                    "product_id": product_id,
                    "variant_id": variant_id,
                    "reused_product": reused,
                    "suggested_yuan": str(row["suggested"]),
                    "cost_yuan": (
                        str(row["cost"]) if row["cost"] is not None else None
                    ),
                    "sale_yuan": str(row["sale"]) if row["sale"] is not None else None,
                    "adjusted": row["adjusted"],
                    "batch_existed": batch is not None,
                    "before_variant": {
                        "status": vrow["status"],
                        "pricing_mode": vrow["pricing_mode"],
                        "reference_price_cents": vrow["reference_price_cents"],
                        "unit_price_cents": vrow["unit_price_cents"],
                        "price_basis_grams": vrow["price_basis_grams"],
                    },
                    "before_product": {
                        "status": vrow["product_status"],
                        "description": vrow["description"],
                    },
                }

                if row["sale"] is None:
                    stats["unpriced"] += 1
                    rec["action"] = "draft"
                    if apply:
                        await conn.execute(
                            "UPDATE product_variant SET status = 'draft', updated_at = NOW() "
                            "WHERE id = $1",
                            variant_id,
                        )
                        if not reused:
                            await conn.execute(
                                "UPDATE product SET status = 'draft', updated_at = NOW() "
                                "WHERE id = $1 AND status <> 'draft'",
                                product_id,
                            )
                        if batch is not None:
                            await conn.execute(
                                "UPDATE inventory_batch SET status = 'inactive', "
                                "updated_at = NOW() WHERE id = $1",
                                batch["id"],
                            )
                    records.append(rec)
                    continue

                stats["priced"] += 1
                if row["adjusted"]:
                    stats["price_adjusted"] += 1
                sale_cents = yuan_to_cents(row["sale"])
                cost_cents = yuan_to_cents(row["cost"]) if row["cost"] is not None else sale_cents
                cost_note = COST_NOTE_COST if row["cost"] is not None else COST_NOTE_UNKNOWN
                media_json = json.dumps([image_url] if image_url else [], ensure_ascii=False)
                rec["action"] = "publish"
                rec["image_url"] = image_url
                if image_url:
                    stats["images"] += 1

                if apply:
                    status_before = str(vrow["product_status"])
                    await conn.execute(
                        "UPDATE product SET status = 'published', updated_at = NOW() WHERE id = $1",
                        product_id,
                    )
                    if status_before != "published":
                        stats["products_published"] += 1
                    await conn.execute(
                        """
                        UPDATE product_variant
                        SET status = 'active', pricing_mode = $1, reference_price_cents = $2,
                            unit_price_cents = $3, price_basis_grams = $4,
                            weight_options = $5::jsonb, stock_unit = $6, updated_at = NOW()
                        WHERE id = $7
                        """,
                        row["pricing"],
                        sale_cents,
                        sale_cents,
                        row["basis"],
                        WEIGHT_OPTIONS if row["pricing"] == "weight" else "[]",
                        "gram" if row["pricing"] == "weight" else "piece",
                        variant_id,
                    )
                    if str(vrow["status"]) != "active":
                        stats["variants_activated"] += 1
                    if batch is None:
                        stats["batches_created"] += 1
                        await conn.execute(
                            """
                            INSERT INTO inventory_batch
                                (id, batch_no, variant_id, location_id, video_url,
                                 media_assets, cost_price_cents, retail_price_cents,
                                 currency, stock_qty, reserved_qty, inspection_status,
                                 status, is_test_batch, is_ghost_batch, delivery_type,
                                 shelf_image_url, inspection_note)
                            VALUES ($1, $2, $3, $4, '', $5::jsonb, $6, $7, 'CNY', 0, 0,
                                    'pending', 'active', FALSE, FALSE, 'spot', $8, $9)
                            """,
                            str(uuid.uuid4()),
                            batch_no,
                            variant_id,
                            LOCATION_ID,
                            media_json,
                            cost_cents,
                            sale_cents,
                            image_url,
                            f"上架占位批次：未收货、库存0，缺货可预订（不编造库存）；{cost_note}",
                        )
                    else:
                        stats["batches_updated"] += 1
                        await conn.execute(
                            """
                            UPDATE inventory_batch
                            SET variant_id = $1, media_assets = $2::jsonb,
                                cost_price_cents = $3, retail_price_cents = $4,
                                inspection_status = 'pending', status = 'active',
                                shelf_image_url = $5, inspection_note = $6, updated_at = NOW()
                            WHERE id = $7
                            """,
                            variant_id,
                            media_json,
                            cost_cents,
                            sale_cents,
                            image_url,
                            f"上架占位批次：未收货、库存0，缺货可预订（不编造库存）；{cost_note}",
                            batch["id"],
                        )

                    # 描述：去掉旧的“待补”，追加公开市场参考价说明；活鲜追加供应说明。
                    desc = str(vrow["description"] or "")
                    new_desc = desc.replace("价格/图片待补。", f"{PRICE_SUFFIX}。")
                    if PRICE_SUFFIX not in new_desc:
                        new_desc = (new_desc.rstrip() + " " + PRICE_SUFFIX).strip()
                    if sku in LIVE_SKUS and LIVE_NOTE not in new_desc:
                        new_desc = (new_desc.rstrip() + " " + LIVE_NOTE).strip()
                    if new_desc != desc:
                        await conn.execute(
                            "UPDATE product SET description = $1, updated_at = NOW() WHERE id = $2",
                            new_desc,
                            product_id,
                        )
                records.append(rec)
        if rollback_out is not None:
            rollback_out.parent.mkdir(parents=True, exist_ok=True)
            rollback_out.write_text(build_rollback(records), encoding="utf-8")
        if report_out is not None:
            report_out.parent.mkdir(parents=True, exist_ok=True)
            report_out.write_text(
                json.dumps({"stats": stats, "records": records}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return {"stats": stats, "records": records}
    finally:
        await conn.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--dsn", default=os.environ.get("HEMALL_PG_DSN", ""))
    parser.add_argument("--media", type=Path, default=DEFAULT_MEDIA)
    parser.add_argument("--spider", type=Path, default=DEFAULT_SPIDER)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只读预演（默认行为，显式给出便于审计）",
    )
    parser.add_argument(
        "--skip-image-copy",
        action="store_true",
        help="图片已由 docker cp 放入容器卷，跳过宿主机复制",
    )
    parser.add_argument("--rollback-out", type=Path)
    parser.add_argument("--report-json", type=Path)
    args = parser.parse_args()

    planned = load_rows(args.csv)
    priced = [r for r in planned if r["sale"] is not None]
    print(
        f"CSV {len(planned)} 行：有售价 {len(priced)}，无售价 {len(planned) - len(priced)}；"
        f"触发进价×1.15 {sum(1 for r in planned if r['adjusted'])}"
    )
    for r in planned:
        if r["adjusted"]:
            print(
                f"  调价 {r['sku']} {r['name']}: 建议 {r['suggested']} -> "
                f"{r['sale']} (进价 {r['cost']})"
            )

    if not args.dsn:
        raise SystemExit("需要 --dsn 或 HEMALL_PG_DSN")
    if args.apply:
        check_dsn(args.dsn)

    if args.apply and not args.skip_image_copy:
        image_urls = copy_images(planned, args.media, args.spider)
        print(f"参考图复制到 {args.spider / 'store-sku'}：{len(image_urls)} 张")
    else:
        image_urls = {
            r["sku"]: f"/media/spider/store-sku/{r['sku']}.jpg"
            for r in priced
            if r["image"] and (ROOT / r["image"]).is_file()
        }
        print(f"可用参考图 {len(image_urls)} 张（未复制）")

    result = await publish(
        args.dsn,
        planned,
        image_urls,
        apply=args.apply,
        rollback_out=args.rollback_out,
        report_out=args.report_json,
    )
    print(json.dumps(result["stats"], ensure_ascii=False))
    if args.rollback_out:
        print(f"回滚脚本：{args.rollback_out}")
    if args.report_json:
        print(f"明细：{args.report_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
