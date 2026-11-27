"""家庭套餐（V1.0 落地任务 D）。

设计口径（写死在这里，改动须经 Soffy 拍板）：
- meal_package：套餐定义。适用人数 serves 闭合词表 single（1 人）/
  couple（双人）/ family（家庭）；套餐价 price_cents 以分为单位（> 0）；
  is_listed 上架开关，缺省不上架。
- meal_package_line：套餐包含商品明细，一套餐一品一行
  （UNIQUE(package_id, product_id)），数量 quantity > 0，商品须存在。
- 净菜属性（净重说明 net_weight_note / 分装说明 packing_note /
  加工日期要求 processing_date_note / 储存条件 storage_note /
  过敏原提示 allergen_note）均为可选填文本。
- 套餐只做定义与展示，不接下单结算：不碰购物车 / 库存 / 券逻辑。
- 删除为硬删（明细随套餐级联删除）；下架不是删除前置条件。

本模块不 import 任何 app 内模块，避免循环导入。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from asyncpg import UniqueViolationError

logger = logging.getLogger("hemall.meal_package")

# ── 取值（改动须经 Soffy 拍板）───────────────────────────────────────────
SERVES_OPTIONS = ("single", "couple", "family")
SERVES_LABELS = {"single": "1人", "couple": "双人", "family": "家庭"}
MAX_NAME_CHARS = 100
MAX_TEXT_CHARS = 500


class MealPackageError(ValueError):
    """家庭套餐模块业务错误基类（端点按子类映射 HTTP 状态码）。"""


class MealPackageNotFoundError(MealPackageError):
    """套餐 / 商品不存在（HTTP 404）。"""


class MealPackageConflictError(MealPackageError):
    """重复品行等冲突（HTTP 409）。"""


MEAL_PACKAGE_DDL = """
CREATE TABLE IF NOT EXISTS meal_package (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL CHECK (name <> ''),
    serves TEXT NOT NULL
        CHECK (serves IN ('single', 'couple', 'family')),
    price_cents BIGINT NOT NULL CHECK (price_cents > 0),
    is_listed BOOLEAN NOT NULL DEFAULT false,
    net_weight_note TEXT NOT NULL DEFAULT '',
    packing_note TEXT NOT NULL DEFAULT '',
    processing_date_note TEXT NOT NULL DEFAULT '',
    storage_note TEXT NOT NULL DEFAULT '',
    allergen_note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_meal_package_listed_created
    ON meal_package(is_listed, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_meal_package_serves_created
    ON meal_package(serves, created_at DESC);

CREATE TABLE IF NOT EXISTS meal_package_line (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    package_id UUID NOT NULL REFERENCES meal_package(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES product(id),
    quantity INT NOT NULL CHECK (quantity > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (package_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_meal_package_line_package
    ON meal_package_line(package_id, created_at);
"""


async def ensure_meal_package_schema(pool: Any) -> None:
    """幂等创建家庭套餐表与明细表。"""

    async with pool.acquire() as conn:
        await conn.execute(MEAL_PACKAGE_DDL)


# ── 行序列化 ────────────────────────────────────────────────────────────


def _package_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "serves": row["serves"],
        "serves_label": SERVES_LABELS[row["serves"]],
        "price_cents": int(row["price_cents"]),
        "is_listed": bool(row["is_listed"]),
        "net_weight_note": row["net_weight_note"],
        "packing_note": row["packing_note"],
        "processing_date_note": row["processing_date_note"],
        "storage_note": row["storage_note"],
        "allergen_note": row["allergen_note"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _line_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "package_id": str(row["package_id"]),
        "product_id": str(row["product_id"]),
        "product_title": row["product_title"],
        "quantity": int(row["quantity"]),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


# ── 内部校验 ────────────────────────────────────────────────────────────


def _as_uuid(value: str, what: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise MealPackageNotFoundError(f"{what} not found") from None


def _check_int(value: Any, field: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    return value


def _check_text(value: str, field: str, *, required: bool = False, max_chars: int = 500) -> str:
    text = (value or "").strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > max_chars:
        raise ValueError(f"{field} is too long")
    return text


def _validate_items(items: Any) -> list[dict[str, Any]]:
    """校验套餐明细：非空列表；每行商品 id 合法、数量 > 0、商品不重复。"""
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each item must be an object")
        product_id = _as_uuid(item.get("product_id"), "product")
        if product_id in seen:
            raise ValueError("duplicate product_id in items")
        seen.add(product_id)
        quantity = _check_int(item.get("quantity"), "quantity", minimum=1)
        cleaned.append({"product_id": product_id, "quantity": quantity})
    return cleaned


async def _require_products(conn: Any, product_ids: list[str]) -> None:
    """明细商品须全部存在；缺一个即 404。"""
    for product_id in product_ids:
        exists = await conn.fetchval("SELECT id FROM product WHERE id = $1", product_id)
        if exists is None:
            raise MealPackageNotFoundError("product not found")


async def _require_package(conn: Any, package_id: str) -> Any:
    row = await conn.fetchrow(
        "SELECT * FROM meal_package WHERE id = $1",
        _as_uuid(package_id, "meal package"),
    )
    if row is None:
        raise MealPackageNotFoundError("meal package not found")
    return row


async def _lines_of(conn: Any, package_id: Any) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT mpl.*, p.title AS product_title FROM meal_package_line mpl"
        " LEFT JOIN product p ON p.id = mpl.product_id"
        " WHERE mpl.package_id = $1 ORDER BY mpl.created_at",
        package_id,
    )
    return [_line_row(r) for r in rows]


async def _insert_lines(conn: Any, package_id: Any, items: list[dict[str, Any]]) -> None:
    for item in items:
        try:
            await conn.execute(
                "INSERT INTO meal_package_line (package_id, product_id, quantity)"
                " VALUES ($1, $2, $3)",
                package_id,
                item["product_id"],
                item["quantity"],
            )
        except UniqueViolationError as exc:
            raise MealPackageConflictError("duplicate product in meal package") from exc


# ── 套餐 CRUD ───────────────────────────────────────────────────────────


async def create_meal_package(
    pool: Any,
    *,
    name: str,
    serves: str,
    price_cents: int,
    items: list[dict[str, Any]],
    net_weight_note: str = "",
    packing_note: str = "",
    processing_date_note: str = "",
    storage_note: str = "",
    allergen_note: str = "",
) -> dict[str, Any]:
    """新建套餐（缺省不上架）；明细商品须存在、数量 > 0。"""
    clean_name = _check_text(name, "name", required=True, max_chars=MAX_NAME_CHARS)
    if serves not in SERVES_OPTIONS:
        raise ValueError("serves must be single, couple or family")
    price = _check_int(price_cents, "price_cents", minimum=1)
    cleaned_items = _validate_items(items)
    notes = {
        "net_weight_note": _check_text(net_weight_note, "net_weight_note"),
        "packing_note": _check_text(packing_note, "packing_note"),
        "processing_date_note": _check_text(processing_date_note, "processing_date_note"),
        "storage_note": _check_text(storage_note, "storage_note"),
        "allergen_note": _check_text(allergen_note, "allergen_note"),
    }
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _require_products(conn, [i["product_id"] for i in cleaned_items])
            row = await conn.fetchrow(
                "INSERT INTO meal_package"
                " (name, serves, price_cents, net_weight_note, packing_note,"
                "  processing_date_note, storage_note, allergen_note)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
                clean_name,
                serves,
                price,
                notes["net_weight_note"],
                notes["packing_note"],
                notes["processing_date_note"],
                notes["storage_note"],
                notes["allergen_note"],
            )
            await _insert_lines(conn, row["id"], cleaned_items)
            result = _package_row(row)
            result["items"] = await _lines_of(conn, row["id"])
    return result


async def get_meal_package(
    pool: Any, package_id: str, *, listed_only: bool = False
) -> dict[str, Any]:
    """取套餐（含明细）；listed_only 时未上架的报 404。"""
    async with pool.acquire() as conn:
        row = await _require_package(conn, package_id)
        if listed_only and not row["is_listed"]:
            raise MealPackageNotFoundError("meal package not found")
        result = _package_row(row)
        result["items"] = await _lines_of(conn, row["id"])
        return result


async def list_meal_packages(
    pool: Any,
    *,
    serves: str | None = None,
    listed_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """套餐列表（可按适用人数过滤；顾客端只看已上架的）。"""
    if serves is not None and serves not in SERVES_OPTIONS:
        raise ValueError("serves must be single, couple or family")
    conditions: list[str] = []
    params: list[Any] = []
    if serves is not None:
        params.append(serves)
        conditions.append(f"serves = ${len(params)}")
    if listed_only:
        conditions.append("is_listed = true")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM meal_package"
            f" {where} ORDER BY created_at DESC"
            f" LIMIT ${len(params) - 1} OFFSET ${len(params)}",
            *params,
        )
        return [_package_row(r) for r in rows]


async def update_meal_package(
    pool: Any,
    package_id: str,
    *,
    name: str | None = None,
    serves: str | None = None,
    price_cents: int | None = None,
    items: list[dict[str, Any]] | None = None,
    net_weight_note: str | None = None,
    packing_note: str | None = None,
    processing_date_note: str | None = None,
    storage_note: str | None = None,
    allergen_note: str | None = None,
) -> dict[str, Any]:
    """改套餐定义；传 items 即整单替换明细（须非空、商品须存在）。"""
    assignments: list[str] = []
    values: list[Any] = []
    if name is not None:
        values.append(_check_text(name, "name", required=True, max_chars=MAX_NAME_CHARS))
        assignments.append(f"name = ${len(values)}")
    if serves is not None:
        if serves not in SERVES_OPTIONS:
            raise ValueError("serves must be single, couple or family")
        values.append(serves)
        assignments.append(f"serves = ${len(values)}")
    if price_cents is not None:
        values.append(_check_int(price_cents, "price_cents", minimum=1))
        assignments.append(f"price_cents = ${len(values)}")
    note_fields = {
        "net_weight_note": net_weight_note,
        "packing_note": packing_note,
        "processing_date_note": processing_date_note,
        "storage_note": storage_note,
        "allergen_note": allergen_note,
    }
    for field, value in note_fields.items():
        if value is not None:
            values.append(_check_text(value, field))
            assignments.append(f"{field} = ${len(values)}")
    cleaned_items = _validate_items(items) if items is not None else None
    if not assignments and cleaned_items is None:
        raise ValueError("nothing to update")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _require_package(conn, package_id)
            package_uuid = row["id"]
            if cleaned_items is not None:
                await _require_products(conn, [i["product_id"] for i in cleaned_items])
                await conn.execute(
                    "DELETE FROM meal_package_line WHERE package_id = $1",
                    package_uuid,
                )
                await _insert_lines(conn, package_uuid, cleaned_items)
            if assignments:
                values.append(package_uuid)
                row = await conn.fetchrow(
                    f"UPDATE meal_package SET {', '.join(assignments)},"
                    f" updated_at = NOW() WHERE id = ${len(values)} RETURNING *",
                    *values,
                )
                assert row is not None
            result = _package_row(row)
            result["items"] = await _lines_of(conn, package_uuid)
    return result


async def set_listed(pool: Any, package_id: str, *, listed: bool) -> dict[str, Any]:
    """上架 / 下架套餐。"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _require_package(conn, package_id)
            updated = await conn.fetchrow(
                "UPDATE meal_package SET is_listed = $1, updated_at = NOW()"
                " WHERE id = $2 RETURNING *",
                listed,
                row["id"],
            )
            assert updated is not None
            result = _package_row(updated)
            result["items"] = await _lines_of(conn, row["id"])
    return result


async def delete_meal_package(pool: Any, package_id: str) -> dict[str, Any]:
    """硬删套餐；明细随套餐级联删除。"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _require_package(conn, package_id)
            package_uuid = str(row["id"])
            await conn.execute("DELETE FROM meal_package WHERE id = $1", package_uuid)
    return {"deleted": True, "package_id": package_uuid}
