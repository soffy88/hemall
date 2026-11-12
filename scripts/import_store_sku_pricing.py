#!/usr/bin/env python3
"""导入门店合并 SKU 清单（99 项）并按称重/按件建立商品与规格。

安全边界（BRIEF 明确）：
- 只写显式传入的临时/预发数据库；DSN 库名含 ``prod`` 时默认拒绝（除非
  显式 HEMALL_IMPORT_ALLOW_PROD=1，正常流程不要设）。
- 没有真实报价就不编价：售价/进价留空即 NULL，商品保持 draft 未上架；
  只有 CSV 填了售价且显式 --publish 才发布。
- 不生成库存批次、不下载/上传网络图片；CSV 图片列仅记录本地参考图路径。
- 按 slug / sku_code 幂等；优先按 merge-decisions.csv 复用现有商品
  （保留实拍图与批次），其次同名同品。

用法：
  python scripts/import_store_sku_pricing.py            # dry-run
  HEMALL_PG_DSN=postgresql://... python scripts/import_store_sku_pricing.py --apply
  ... --apply --publish   # 只发布已填售价的行
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import unicodedata
from pathlib import Path

import asyncpg

try:
    from obase.uuid7 import uuid7
except Exception:  # pragma: no cover - uv 环境可用
    import uuid

    def uuid7() -> str:  # type: ignore[misc]
        return str(uuid.uuid4())


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "store-sku-pricing.csv"
DEFAULT_MERGE = ROOT / "merge-decisions.csv"

CATEGORY_NAMES = {
    "vegetables": "蔬菜",
    "fruits": "水果",
    "pork": "猪肉",
    "poultry": "禽肉",
    "seafood": "水产",
    "eggs-tofu": "蛋及豆制品",
    "grain-oil": "粮油米面调味",
}

# 同品异名归一（左侧为清单名/别名，右侧为规范名）；只用于匹配已有商品。
ALIASES = {
    "青菜": "上海青",
    "小青菜": "上海青",
    "蒜头": "大蒜",
    "鲜鸡": "整鸡",
    "鸭腿": "鸭翅",
    "面条": "鲜湿面条",
    "千张": "千张豆皮",
    "豆皮": "千张豆皮",
    "西红柿": "番茄",
    "马铃薯": "土豆",
    "卷心菜": "包菜",
    "蛤蜊": "花甲",
}

WEIGHT_DEFAULT_OPTIONS = (
    '[{"label":"约0.5斤","grams":250},{"label":"约1斤","grams":500},{"label":"约2斤","grams":1000}]'
)


def norm(name: str) -> str:
    text = unicodedata.normalize("NFKC", name or "").strip().lower()
    text = "".join(ch for ch in text if not ch.isspace())
    return ALIASES.get(text, text)


def load_merge_decisions(path: Path) -> dict[str, str]:
    """人工确认的同名/同品合并表：SKU -> 现有商品 ID（决定=合并）。"""
    if not path.is_file():
        return {}
    mapping: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("决定") or "").strip() != "合并":
                continue
            sku = (row.get("SKU") or "").strip()
            pid = (row.get("现有商品ID") or "").strip()
            if not sku or not pid:
                raise SystemExit(f"merge-decisions.csv 合并行缺少 SKU 或现有商品ID: {sku!r}")
            mapping[sku] = pid
    return mapping


def slugify(sku: str) -> str:
    return "store-" + sku.strip().lower().replace("_", "-")


def parse_price_yuan(raw: str) -> int | None:
    text = (raw or "").strip()
    if not text or text in ("待报价", "待填", "-"):
        return None
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"售价不是数字: {raw!r}") from exc
    if value <= 0:
        return None
    return int(round(value * 100))


def per_jin_cents(price_cents: int | None, unit: str, pricing: str) -> int | None:
    if price_cents is None or pricing != "weight":
        return price_cents
    unit = (unit or "斤").strip().lower()
    if unit in ("斤", "jin", "500g"):
        return price_cents
    if unit in ("kg", "公斤", "千克", "1000g"):
        return int(round(price_cents / 2))
    raise ValueError(f"称重商品单位必须是斤或kg: {unit!r}")


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit("CSV 为空")
    required = {"SKU", "品类", "类目slug", "商品名称", "计价方式", "计价单位", "首批建议数量"}
    missing = required - set(rows[0])
    if missing:
        raise SystemExit(f"CSV 缺少列: {sorted(missing)}")
    seen: set[str] = set()
    for index, row in enumerate(rows, start=2):
        sku = (row.get("SKU") or "").strip()
        if not sku:
            raise SystemExit(f"第 {index} 行 SKU 为空")
        if sku in seen:
            raise SystemExit(f"第 {index} 行 SKU 重复: {sku}")
        seen.add(sku)
        pricing = (row.get("计价方式") or "").strip()
        if pricing not in ("weight", "piece"):
            raise SystemExit(f"第 {index} 行计价方式非法: {pricing!r}")
        slug = (row.get("类目slug") or "").strip()
        if not slug:
            raise SystemExit(f"第 {index} 行类目slug为空")
        if pricing == "weight":
            try:
                basis = int((row.get("计价基准克") or "500").strip())
            except ValueError as exc:
                raise SystemExit(f"第 {index} 行计价基准克非法") from exc
            if basis <= 0:
                raise SystemExit(f"第 {index} 行计价基准克必须 > 0")
    return rows


async def import_rows(
    dsn: str,
    rows: list[dict[str, str]],
    *,
    apply: bool,
    publish: bool,
    reuse: bool,
    merge_map: dict[str, str] | None = None,
) -> dict[str, int]:
    stats = {"created": 0, "reused": 0, "updated": 0, "skipped": 0, "no_price": 0}
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            # 1) 分类：只新增缺失类目，不删旧类目。
            category_ids: dict[str, str] = {}
            for slug, name in CATEGORY_NAMES.items():
                row = await conn.fetchrow(
                    "SELECT id FROM product_category WHERE slug = $1 AND deleted_at IS NULL",
                    slug,
                )
                if row is None:
                    if apply:
                        category_ids[slug] = str(
                            await conn.fetchval(
                                "INSERT INTO product_category (id, name, slug, status) "
                                "VALUES ($1, $2, $3, 'active') RETURNING id",
                                uuid7(),
                                name,
                                slug,
                            )
                        )
                    else:
                        category_ids[slug] = f"<new:{slug}>"
                else:
                    category_ids[slug] = str(row["id"])

            for row in rows:
                sku = row["SKU"].strip()
                pricing = row["计价方式"].strip()
                slug = row["类目slug"].strip()
                if slug not in CATEGORY_NAMES:
                    raise SystemExit(f"{sku}: 未知类目slug {slug}")
                name = row["商品名称"].strip()
                unit = (row.get("计价单位") or ("斤" if pricing == "weight" else "件")).strip()
                basis = int(row.get("计价基准克") or ("500" if pricing == "weight" else "1"))
                sale_cents = parse_price_yuan(row.get("待填售价(元/单位)", ""))
                parse_price_yuan(row.get("待填进价(元/单位)", ""))  # 只校验格式
                if sale_cents is None:
                    stats["no_price"] += 1
                jin_price = per_jin_cents(sale_cents, unit, pricing)
                category_id = category_ids[slug]
                status = "published" if (publish and sale_cents is not None) else "draft"

                # 2) 优先按人工确认的合并表复用（保留实拍图/批次），
                #    其次同名同品，最后按 slug 幂等新建。
                existing = None
                merge_pid = (merge_map or {}).get(sku)
                if merge_pid:
                    existing = await conn.fetchrow(
                        "SELECT id, title, slug FROM product "
                        "WHERE id = $1 AND deleted_at IS NULL",
                        merge_pid,
                    )
                    if existing is None:
                        raise SystemExit(f"{sku}: 合并目标商品不存在或已删除: {merge_pid}")
                if existing is None and reuse and not category_id.startswith("<new:"):
                    candidates = await conn.fetch(
                        "SELECT id, title, slug FROM product "
                        "WHERE category_id = $1 AND deleted_at IS NULL",
                        category_id,
                    )
                    target = norm(name)
                    for cand in candidates:
                        if norm(cand["title"]) == target:
                            existing = cand
                            break
                if existing is None:
                    existing = await conn.fetchrow(
                        "SELECT id, title, slug FROM product "
                        "WHERE slug = $1 AND deleted_at IS NULL",
                        slugify(sku),
                    )
                if existing is None:
                    stats["created"] += 1
                    if apply:
                        product_id = await conn.fetchval(
                            """
                            INSERT INTO product
                                (id, title, slug, description, category_id, status)
                            VALUES ($1, $2, $3, $4, $5, $6)
                            RETURNING id
                            """,
                            uuid7(),
                            name,
                            slugify(sku),
                            (
                                f"门店第一阶段 SKU（{row.get('来源') or '99'}）："
                                f"{name}。价格/图片待补。"
                            ),
                            category_id,
                            status,
                        )
                    else:
                        product_id = None
                else:
                    stats["reused"] += 1
                    product_id = str(existing["id"])
                    if apply and sale_cents is not None and publish:
                        await conn.execute(
                            "UPDATE product SET status = 'published', updated_at = NOW() "
                            "WHERE id = $1 AND status = 'draft'",
                            product_id,
                        )

                variant = await conn.fetchrow(
                    "SELECT id FROM product_variant WHERE sku_code = $1 AND deleted_at IS NULL",
                    sku,
                )
                if variant is None:
                    stats["created" if existing is None else "updated"] += 1
                    if apply and product_id is not None:
                        options = (
                            WEIGHT_DEFAULT_OPTIONS
                            if pricing == "weight"
                            else json.dumps({"规格": unit}, ensure_ascii=False)
                        )
                        await conn.execute(
                            """
                            INSERT INTO product_variant
                                (id, product_id, sku_code, option_values,
                                 reference_price_cents, status, pricing_mode,
                                 unit_price_cents, price_basis_grams,
                                 weight_options, stock_unit)
                            VALUES ($1, $2, $3, $4::jsonb, $5, 'active', $6, $7, $8,
                                    $9::jsonb, $10)
                            """,
                            uuid7(),
                            product_id,
                            sku,
                            options,
                            sale_cents,
                            pricing,
                            jin_price,
                            basis,
                            WEIGHT_DEFAULT_OPTIONS if pricing == "weight" else "[]",
                            "gram" if pricing == "weight" else "piece",
                        )
                else:
                    stats["updated"] += 1
                    if apply:
                        await conn.execute(
                            """
                            UPDATE product_variant
                            SET pricing_mode = $1, unit_price_cents = $2,
                                price_basis_grams = $3, reference_price_cents = $4,
                                weight_options = $5::jsonb, stock_unit = $6,
                                updated_at = NOW()
                            WHERE id = $7
                            """,
                            pricing,
                            jin_price,
                            basis,
                            sale_cents,
                            WEIGHT_DEFAULT_OPTIONS if pricing == "weight" else "[]",
                            "gram" if pricing == "weight" else "piece",
                            variant["id"],
                        )
        return stats
    finally:
        await conn.close()


def check_dsn(dsn: str) -> None:
    dbname = dsn.rsplit("/", 1)[-1].split("?")[0].lower()
    if "prod" in dbname and os.environ.get("HEMALL_IMPORT_ALLOW_PROD") != "1":
        raise SystemExit(
            f"拒绝写入生产库 {dbname!r}：本脚本只允许临时/预发库；"
            "确需写入生产必须人工评审并显式 HEMALL_IMPORT_ALLOW_PROD=1"
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--dsn", default=os.environ.get("HEMALL_PG_DSN", ""))
    parser.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    parser.add_argument("--publish", action="store_true", help="仅发布已填售价的行")
    parser.add_argument("--no-reuse", action="store_true", help="不与同名现有商品合并")
    parser.add_argument(
        "--merge-csv", type=Path, default=DEFAULT_MERGE, help="人工确认的同品合并表"
    )
    args = parser.parse_args()

    rows = read_rows(args.csv)
    if not args.apply:
        print(f"[dry-run] CSV {len(rows)} 行；不连接数据库。加 --apply 且提供 --dsn 执行。")
        no_price = sum(1 for r in rows if not (r.get("待填售价(元/单位)") or "").strip())
        print(f"[dry-run] 无售价行: {no_price}")
        return 0
    if not args.dsn:
        raise SystemExit("--apply 需要 --dsn 或 HEMALL_PG_DSN")
    check_dsn(args.dsn)
    stats = await import_rows(
        args.dsn,
        rows,
        apply=True,
        publish=args.publish,
        reuse=not args.no_reuse,
        merge_map=load_merge_decisions(args.merge_csv),
    )
    print(
        f"导入完成: 新建 {stats['created']} / 复用商品 {stats['reused']} / "
        f"更新 {stats['updated']} / 无售价(保持草稿) {stats['no_price']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
