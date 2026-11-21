"""供应商管理（B 部分）：供应商 + 报价 + 周评分。

设计口径（写死在这里，改动须经 Soffy 拍板）：
- supplier.category 为核心品类；每品类 1 家主要 + 1 家备用，用
  is_primary / is_backup 标志。唯一性由部分唯一索引保证（仅 active
  行），应用层创建/更新前同样校验；冲突报 SupplierConflictError
  （HTTP 409）。同一供应商不能同时是主要和备用。
- procurement_supplier_price 用 product_id（UUID，外键 product(id)），不用 sku；
  报价以分为单位，quoted_date 为报价日期；商品须存在。
- procurement_supplier_evaluation 的 week 用 "YYYY-Www" 字符串（如 2026-W47）；
  六项评分各 0-100，权重：价格 25、质量 30、准时 15、稳定 15、
  售后 10、配合 5；total_score 由服务端算（两位小数），请求里没有
  total_score 字段，客户端传了也进不了库。
- 同一供应商每周只允许一条评分（UNIQUE(supplier_id, week)），重复
  录入报 409，改分走 PUT。

本模块不 import 任何 app 内模块，避免循环导入。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from asyncpg import UniqueViolationError

logger = logging.getLogger("hemall.supplier")

# ── 权重与取值（改动须经 Soffy 拍板）───────────────────────────────────
WEIGHTS: dict[str, int] = {
    "price_score": 25,  # 价格竞争力
    "quality_score": 30,  # 商品质量
    "punctuality_score": 15,  # 到货准时率
    "stability_score": 15,  # 供货稳定性
    "aftersales_score": 10,  # 售后处理
    "cooperation_score": 5,  # 配合程度
}
SCORE_FIELDS: tuple[str, ...] = tuple(WEIGHTS)
WEEK_RE = re.compile(r"^\d{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$")
SUPPLIER_STATUSES = ("active", "inactive")
MAX_NAME_CHARS = 100
MAX_TEXT_CHARS = 500


class SupplierError(ValueError):
    """供应商模块业务错误基类（端点按子类映射 HTTP 状态码）。"""


class SupplierNotFoundError(SupplierError):
    """供应商 / 商品 / 评分不存在（HTTP 404）。"""


class SupplierConflictError(SupplierError):
    """主备唯一性、重复周评分、带依赖删除等冲突（HTTP 409）。"""


SUPPLIER_DDL = """
CREATE TABLE IF NOT EXISTS procurement_supplier (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL CHECK (name <> ''),
    contact_name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL CHECK (category <> ''),
    license_note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'inactive')),
    is_primary BOOLEAN NOT NULL DEFAULT false,
    is_backup BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (NOT (is_primary AND is_backup))
);
-- 同一品类的主要 / 备用各唯一（仅 active 行；停用后可另立主备）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_procurement_supplier_primary_category
    ON procurement_supplier(category) WHERE is_primary AND status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS uq_procurement_supplier_backup_category
    ON procurement_supplier(category) WHERE is_backup AND status = 'active';
CREATE INDEX IF NOT EXISTS idx_procurement_supplier_category_status
    ON procurement_supplier(category, status);

CREATE TABLE IF NOT EXISTS procurement_supplier_price (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id UUID NOT NULL REFERENCES procurement_supplier(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES product(id),
    price_cents BIGINT NOT NULL CHECK (price_cents > 0),
    quoted_date DATE NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_procurement_supplier_price_supplier
    ON procurement_supplier_price(supplier_id, quoted_date DESC);
CREATE INDEX IF NOT EXISTS idx_procurement_supplier_price_product
    ON procurement_supplier_price(product_id, quoted_date DESC);

CREATE TABLE IF NOT EXISTS procurement_supplier_evaluation (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id UUID NOT NULL REFERENCES procurement_supplier(id) ON DELETE CASCADE,
    week TEXT NOT NULL
        CHECK (week ~ '^[0-9]{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$'),
    price_score INT NOT NULL CHECK (price_score BETWEEN 0 AND 100),
    quality_score INT NOT NULL CHECK (quality_score BETWEEN 0 AND 100),
    punctuality_score INT NOT NULL CHECK (punctuality_score BETWEEN 0 AND 100),
    stability_score INT NOT NULL CHECK (stability_score BETWEEN 0 AND 100),
    aftersales_score INT NOT NULL CHECK (aftersales_score BETWEEN 0 AND 100),
    cooperation_score INT NOT NULL CHECK (cooperation_score BETWEEN 0 AND 100),
    total_score NUMERIC(5, 2) NOT NULL CHECK (total_score BETWEEN 0 AND 100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (supplier_id, week)
);
CREATE INDEX IF NOT EXISTS idx_procurement_supplier_evaluation_supplier
    ON procurement_supplier_evaluation(supplier_id, week DESC);
"""


async def ensure_supplier_schema(pool: Any) -> None:
    """幂等创建供应商管理所需表与索引。"""

    async with pool.acquire() as conn:
        await conn.execute(SUPPLIER_DDL)


# ── 纯计算（无 DB，可单元测试）─────────────────────────────────────────


def validate_week(week: str) -> None:
    """周次须为 YYYY-Www 格式，如 2026-W47。"""

    if not isinstance(week, str) or not WEEK_RE.match(week):
        raise ValueError("week must look like 2026-W47")


def weighted_total(scores: dict[str, int]) -> Decimal:
    """六项评分加权总分（两位小数，四舍五入）。"""

    total = sum(scores[field] * weight for field, weight in WEIGHTS.items())
    return (Decimal(total) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _validate_scores(scores: dict[str, Any]) -> dict[str, int]:
    cleaned: dict[str, int] = {}
    for field in SCORE_FIELDS:
        value = scores.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field} must be an integer")
        if not 0 <= value <= 100:
            raise ValueError(f"{field} must be between 0 and 100")
        cleaned[field] = value
    return cleaned


# ── 行序列化 ────────────────────────────────────────────────────────────


def _supplier_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "contact_name": row["contact_name"],
        "phone": row["phone"],
        "category": row["category"],
        "license_note": row["license_note"],
        "status": row["status"],
        "is_primary": bool(row["is_primary"]),
        "is_backup": bool(row["is_backup"]),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _price_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "supplier_id": str(row["supplier_id"]),
        "product_id": str(row["product_id"]),
        "price_cents": int(row["price_cents"]),
        "quoted_date": row["quoted_date"].isoformat(),
        "note": row["note"],
        "created_at": row["created_at"].isoformat(),
    }


def _evaluation_row(row: Any) -> dict[str, Any]:
    result = {
        "id": str(row["id"]),
        "supplier_id": str(row["supplier_id"]),
        "week": row["week"],
        "total_score": float(row["total_score"]),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }
    for field in SCORE_FIELDS:
        result[field] = int(row[field])
    return result


# ── 内部校验 ────────────────────────────────────────────────────────────


def _as_uuid(value: str, what: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise SupplierNotFoundError(f"{what} not found") from None


async def _require_supplier(conn: Any, supplier_id: str) -> Any:
    row = await conn.fetchrow(
        "SELECT * FROM procurement_supplier WHERE id = $1", _as_uuid(supplier_id, "supplier")
    )
    if row is None:
        raise SupplierNotFoundError("supplier not found")
    return row


async def _check_primary_backup_unique(
    conn: Any,
    *,
    category: str,
    is_primary: bool,
    is_backup: bool,
    status: str,
    exclude_id: str | None = None,
) -> None:
    """应用层主备唯一校验（数据库部分唯一索引是最后一道防线）。"""

    if status != "active":
        return
    for flag, column, label in (
        (is_primary, "is_primary", "primary"),
        (is_backup, "is_backup", "backup"),
    ):
        if not flag:
            continue
        clash = await conn.fetchval(
            f"SELECT id FROM procurement_supplier WHERE category = $1"
            f" AND status = 'active' AND {column}"
            " AND id IS DISTINCT FROM $2::uuid",
            category,
            exclude_id,
        )
        if clash is not None:
            raise SupplierConflictError(
                f"category {category} already has an active {label} supplier"
            )


def _check_text(value: str, field: str, *, required: bool = False) -> str:
    text = (value or "").strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"{field} is too long")
    return text


# ── 供应商 CRUD ─────────────────────────────────────────────────────────


async def create_supplier(
    pool: Any,
    *,
    name: str,
    category: str,
    contact_name: str = "",
    phone: str = "",
    license_note: str = "",
    status: str = "active",
    is_primary: bool = False,
    is_backup: bool = False,
) -> dict[str, Any]:
    """新建供应商；同一品类第二家主要 / 备用报 409。"""

    name = _check_text(name, "name", required=True)
    category = _check_text(category, "category", required=True)
    contact_name = _check_text(contact_name, "contact_name")
    phone = _check_text(phone, "phone")
    license_note = _check_text(license_note, "license_note")
    if status not in SUPPLIER_STATUSES:
        raise ValueError("status must be active or inactive")
    if is_primary and is_backup:
        raise ValueError("a supplier cannot be both primary and backup")
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _check_primary_backup_unique(
                conn,
                category=category,
                is_primary=is_primary,
                is_backup=is_backup,
                status=status,
            )
            try:
                row = await conn.fetchrow(
                    "INSERT INTO procurement_supplier (name, contact_name, phone, category,"
                    " license_note, status, is_primary, is_backup)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
                    name,
                    contact_name,
                    phone,
                    category,
                    license_note,
                    status,
                    is_primary,
                    is_backup,
                )
            except UniqueViolationError as exc:
                raise SupplierConflictError(
                    f"category {category} already has an active"
                    f" {'primary' if is_primary else 'backup'} supplier"
                ) from exc
    assert row is not None
    return _supplier_row(row)


async def get_supplier(pool: Any, supplier_id: str) -> dict[str, Any]:
    """取一家供应商；不存在报 404。"""

    async with pool.acquire() as conn:
        return _supplier_row(await _require_supplier(conn, supplier_id))


async def list_suppliers(
    pool: Any,
    *,
    category: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """按品类 / 状态列供应商（创建时间倒序）。"""

    if status is not None and status not in SUPPLIER_STATUSES:
        raise ValueError("status must be active or inactive")
    conditions: list[str] = []
    params: list[Any] = []
    if category is not None:
        params.append(category)
        conditions.append(f"category = ${len(params)}")
    if status is not None:
        params.append(status)
        conditions.append(f"status = ${len(params)}")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM procurement_supplier {where}"
            f" ORDER BY created_at DESC LIMIT ${len(params) - 1}"
            f" OFFSET ${len(params)}",
            *params,
        )
    return [_supplier_row(row) for row in rows]


async def primary_backup_for_category(pool: Any, category: str) -> dict[str, Any]:
    """按品类查主备供应商（仅 active 行；缺位为 null）。"""

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM procurement_supplier WHERE category = $1 AND status = 'active'"
            " AND (is_primary OR is_backup) ORDER BY created_at",
            category,
        )
    primary = next((r for r in rows if r["is_primary"]), None)
    backup = next((r for r in rows if r["is_backup"]), None)
    return {
        "category": category,
        "primary": _supplier_row(primary) if primary is not None else None,
        "backup": _supplier_row(backup) if backup is not None else None,
    }


async def update_supplier(pool: Any, supplier_id: str, **fields: Any) -> dict[str, Any]:
    """改供应商；改主备 / 改品类 / 重新激活都要过唯一校验。"""

    allowed = {
        "name",
        "contact_name",
        "phone",
        "category",
        "license_note",
        "status",
        "is_primary",
        "is_backup",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        raise ValueError("no fields to update")
    if "name" in updates:
        updates["name"] = _check_text(updates["name"], "name", required=True)
    if "category" in updates:
        updates["category"] = _check_text(updates["category"], "category", required=True)
    for field in ("contact_name", "phone", "license_note"):
        if field in updates:
            updates[field] = _check_text(updates[field], field)
    if "status" in updates and updates["status"] not in SUPPLIER_STATUSES:
        raise ValueError("status must be active or inactive")
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await _require_supplier(conn, supplier_id)
            merged = {**dict(current), **updates}
            if merged["is_primary"] and merged["is_backup"]:
                raise ValueError("a supplier cannot be both primary and backup")
            await _check_primary_backup_unique(
                conn,
                category=str(merged["category"]),
                is_primary=bool(merged["is_primary"]),
                is_backup=bool(merged["is_backup"]),
                status=str(merged["status"]),
                exclude_id=str(current["id"]),
            )
            assignments = ", ".join(f"{key} = ${i + 1}" for i, key in enumerate(updates))
            values = list(updates.values())
            values.append(str(current["id"]))
            try:
                row = await conn.fetchrow(
                    f"UPDATE procurement_supplier SET {assignments}, updated_at = NOW()"
                    f" WHERE id = ${len(values)} RETURNING *",
                    *values,
                )
            except UniqueViolationError as exc:
                raise SupplierConflictError(
                    "category already has an active primary or backup supplier"
                ) from exc
    assert row is not None
    return _supplier_row(row)


async def delete_supplier(pool: Any, supplier_id: str) -> dict[str, Any]:
    """删供应商；有报价或评分记录时报 409（先停用，不要硬删历史）。"""

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _require_supplier(conn, supplier_id)
            supplier_uuid = str(row["id"])
            price_count = await conn.fetchval(
                "SELECT COUNT(*) FROM procurement_supplier_price WHERE supplier_id = $1",
                supplier_uuid,
            )
            eval_count = await conn.fetchval(
                "SELECT COUNT(*) FROM procurement_supplier_evaluation WHERE supplier_id = $1",
                supplier_uuid,
            )
            if price_count or eval_count:
                raise SupplierConflictError(
                    "supplier has price or evaluation records; deactivate it instead of deleting"
                )
            await conn.execute("DELETE FROM procurement_supplier WHERE id = $1", supplier_uuid)
    return {"deleted": True, "supplier_id": supplier_uuid}


# ── 报价 ────────────────────────────────────────────────────────────────


async def record_price(
    pool: Any,
    supplier_id: str,
    *,
    product_id: str,
    price_cents: int,
    quoted_date: str,
    note: str = "",
) -> dict[str, Any]:
    """录一条报价；商品须存在（product_id 外键 + 应用层 404）。"""

    if isinstance(price_cents, bool) or not isinstance(price_cents, int):
        raise ValueError("price_cents must be an integer")
    if price_cents <= 0:
        raise ValueError("price_cents must be positive")
    try:
        quoted = date.fromisoformat(str(quoted_date))
    except ValueError:
        raise ValueError("quoted_date must be YYYY-MM-DD") from None
    note = _check_text(note, "note")
    async with pool.acquire() as conn:
        await _require_supplier(conn, supplier_id)
        product_uuid = _as_uuid(product_id, "product")
        exists = await conn.fetchval("SELECT id FROM product WHERE id = $1", product_uuid)
        if exists is None:
            raise SupplierNotFoundError("product not found")
        row = await conn.fetchrow(
            "INSERT INTO procurement_supplier_price (supplier_id, product_id, price_cents,"
            " quoted_date, note) VALUES ($1, $2, $3, $4, $5) RETURNING *",
            str(supplier_id),
            product_uuid,
            price_cents,
            quoted,
            note,
        )
    assert row is not None
    return _price_row(row)


async def list_prices(
    pool: Any,
    supplier_id: str,
    *,
    product_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """列某供应商的报价（报价日期倒序，可按商品过滤）。"""

    async with pool.acquire() as conn:
        row = await _require_supplier(conn, supplier_id)
        params: list[Any] = [str(row["id"])]
        extra = ""
        if product_id is not None:
            params.append(_as_uuid(product_id, "product"))
            extra = f" AND product_id = ${len(params)}"
        params.extend([limit, offset])
        rows = await conn.fetch(
            "SELECT * FROM procurement_supplier_price WHERE supplier_id = $1"
            f"{extra} ORDER BY quoted_date DESC, created_at DESC"
            f" LIMIT ${len(params) - 1} OFFSET ${len(params)}",
            *params,
        )
    return [_price_row(r) for r in rows]


# ── 周评分 ──────────────────────────────────────────────────────────────


async def record_evaluation(
    pool: Any, supplier_id: str, *, week: str, **scores: Any
) -> dict[str, Any]:
    """录一周评分；总分由服务端算，同一周重复录入报 409。"""

    validate_week(week)
    cleaned = _validate_scores(scores)
    total = weighted_total(cleaned)
    async with pool.acquire() as conn:
        await _require_supplier(conn, supplier_id)
        columns = ", ".join(SCORE_FIELDS)
        placeholders = ", ".join(f"${i + 3}" for i in range(len(SCORE_FIELDS)))
        try:
            row = await conn.fetchrow(
                f"INSERT INTO procurement_supplier_evaluation (supplier_id, week, {columns},"
                f" total_score) VALUES ($1, $2, {placeholders},"
                f" ${len(SCORE_FIELDS) + 3}) RETURNING *",
                str(supplier_id),
                week,
                *[cleaned[field] for field in SCORE_FIELDS],
                total,
            )
        except UniqueViolationError as exc:
            raise SupplierConflictError(f"evaluation for {week} already exists") from exc
    assert row is not None
    return _evaluation_row(row)


async def get_evaluation(pool: Any, supplier_id: str, week: str) -> dict[str, Any]:
    """取某供应商某一周的评分；不存在报 404。"""

    validate_week(week)
    async with pool.acquire() as conn:
        await _require_supplier(conn, supplier_id)
        row = await conn.fetchrow(
            "SELECT * FROM procurement_supplier_evaluation WHERE supplier_id = $1 AND week = $2",
            str(supplier_id),
            week,
        )
    if row is None:
        raise SupplierNotFoundError("evaluation not found")
    return _evaluation_row(row)


async def list_evaluations(
    pool: Any,
    supplier_id: str,
    *,
    week: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """列某供应商的周评分（周次倒序，可按周过滤）。"""

    if week is not None:
        validate_week(week)
    async with pool.acquire() as conn:
        row = await _require_supplier(conn, supplier_id)
        params: list[Any] = [str(row["id"])]
        extra = ""
        if week is not None:
            params.append(week)
            extra = f" AND week = ${len(params)}"
        params.extend([limit, offset])
        rows = await conn.fetch(
            "SELECT * FROM procurement_supplier_evaluation WHERE supplier_id = $1"
            f"{extra} ORDER BY week DESC"
            f" LIMIT ${len(params) - 1} OFFSET ${len(params)}",
            *params,
        )
    return [_evaluation_row(r) for r in rows]


async def update_evaluation(
    pool: Any, supplier_id: str, week: str, **scores: Any
) -> dict[str, Any]:
    """改某一周的评分；总分按新分重算。"""

    validate_week(week)
    cleaned = _validate_scores(scores)
    total = weighted_total(cleaned)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _require_supplier(conn, supplier_id)
            current = await conn.fetchrow(
                "SELECT * FROM procurement_supplier_evaluation"
                " WHERE supplier_id = $1 AND week = $2",
                str(supplier_id),
                week,
            )
            if current is None:
                raise SupplierNotFoundError("evaluation not found")
            merged = {**{f: current[f] for f in SCORE_FIELDS}, **cleaned}
            total = weighted_total(merged)
            assignments = ", ".join(f"{field} = ${i + 3}" for i, field in enumerate(SCORE_FIELDS))
            row = await conn.fetchrow(
                f"UPDATE procurement_supplier_evaluation SET {assignments},"
                f" total_score = ${len(SCORE_FIELDS) + 3}, updated_at = NOW()"
                " WHERE supplier_id = $1 AND week = $2 RETURNING *",
                str(supplier_id),
                week,
                *[merged[field] for field in SCORE_FIELDS],
                total,
            )
    assert row is not None
    return _evaluation_row(row)
