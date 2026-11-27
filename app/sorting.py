"""分拣交付表 + 损耗表（V1.0 落地任务 C）。

设计口径（写死在这里，改动须经 Soffy 拍板）：
- sorting_delivery：一单一表头（order_id 唯一，外键 customer_order(id)）；
  交付方式 pickup（自提）/ delivery（配送）；状态机：
  pending（待分拣）-> sorting（分拣中）-> ready_pickup（待提货，限自提）
  或 out_for_delivery（待配送，限配送）-> completed（已完成）；
  任何进行中状态可进 exception（异常），异常只能回 sorting 返工；
  completed 为终态。非法流转报 SortingConflictError（HTTP 409）。
- sorting_delivery_line：一单一品一行（UNIQUE(sorting_id, product_id)），
  应配 expected_qty、实配 actual_qty、复核人 reviewer；复核（改 actual_qty）
  须留复核人（本次传或行上已有），且只能在 pending / sorting 状态下做。
- spoilage_record：只记账，不碰库存——损耗不自动扣库存、不写任何
  stock/inventory 表，现有库存口径不动。原因闭合词表：
  expired（过期）/ spoiled（变质）/ damaged（破损）/
  weigh_error（称重误差）/ other（其他）；成本金额 cost_cents 以分为单位；
  recorded_date 为损耗发生日期（缺省当天），recorded_by 为记录人（商家 id）。
- 每日汇总 spoilage_daily_summary：按日统计损耗金额与按品类统计
  （品类取 product.category_id -> product_category.name，无品类记“未分类”）。

本模块不 import 任何 app 内模块，避免循环导入。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date
from typing import Any

from asyncpg import UniqueViolationError

logger = logging.getLogger("hemall.sorting")

# ── 取值（改动须经 Soffy 拍板）───────────────────────────────────────────
DELIVERY_METHODS = ("pickup", "delivery")
DELIVERY_METHOD_LABELS = {"pickup": "自提", "delivery": "配送"}
SORTING_STATUSES = (
    "pending",
    "sorting",
    "ready_pickup",
    "out_for_delivery",
    "completed",
    "exception",
)
SORTING_STATUS_LABELS = {
    "pending": "待分拣",
    "sorting": "分拣中",
    "ready_pickup": "待提货",
    "out_for_delivery": "待配送",
    "completed": "已完成",
    "exception": "异常",
}
# 不依赖交付方式的合法流转；sorting -> ready/out 由交付方式再分流。
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("sorting", "exception"),
    "sorting": ("ready_pickup", "out_for_delivery", "exception"),
    "ready_pickup": ("completed", "exception"),
    "out_for_delivery": ("completed", "exception"),
    "exception": ("sorting",),
    "completed": (),
}
SPOILAGE_REASONS = ("expired", "spoiled", "damaged", "weigh_error", "other")
SPOILAGE_REASON_LABELS = {
    "expired": "过期",
    "spoiled": "变质",
    "damaged": "破损",
    "weigh_error": "称重误差",
    "other": "其他",
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_TEXT_CHARS = 500
MAX_SKU_CHARS = 100


class SortingError(ValueError):
    """分拣 / 损耗模块业务错误基类（端点按子类映射 HTTP 状态码）。"""


class SortingNotFoundError(SortingError):
    """分拣单 / 明细 / 损耗记录 / 订单 / 商品不存在（HTTP 404）。"""


class SortingConflictError(SortingError):
    """一单一表头重复、非法状态流转、重复品行等冲突（HTTP 409）。"""


SORTING_DDL = """
CREATE TABLE IF NOT EXISTS sorting_delivery (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL UNIQUE REFERENCES customer_order(id),
    delivery_method TEXT NOT NULL
        CHECK (delivery_method IN ('pickup', 'delivery')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sorting', 'ready_pickup',
                          'out_for_delivery', 'completed', 'exception')),
    operator_id TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sorting_delivery_status_created
    ON sorting_delivery(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sorting_delivery_method
    ON sorting_delivery(delivery_method, created_at DESC);

CREATE TABLE IF NOT EXISTS sorting_delivery_line (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sorting_id UUID NOT NULL REFERENCES sorting_delivery(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES product(id),
    expected_qty INT NOT NULL CHECK (expected_qty > 0),
    actual_qty INT NOT NULL DEFAULT 0 CHECK (actual_qty >= 0),
    reviewer TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sorting_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_sorting_delivery_line_sorting
    ON sorting_delivery_line(sorting_id, created_at);

CREATE TABLE IF NOT EXISTS spoilage_record (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID NOT NULL REFERENCES product(id),
    sku TEXT NOT NULL CHECK (sku <> ''),
    batch_no TEXT NOT NULL DEFAULT '',
    quantity INT NOT NULL CHECK (quantity > 0),
    cost_cents BIGINT NOT NULL CHECK (cost_cents >= 0),
    reason TEXT NOT NULL
        CHECK (reason IN ('expired', 'spoiled', 'damaged', 'weigh_error', 'other')),
    handling TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL DEFAULT '',
    recorded_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_spoilage_record_date
    ON spoilage_record(recorded_date, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_spoilage_record_product_date
    ON spoilage_record(product_id, recorded_date DESC);
"""


async def ensure_sorting_schema(pool: Any) -> None:
    """幂等创建分拣交付表与损耗表。"""

    async with pool.acquire() as conn:
        await conn.execute(SORTING_DDL)


# ── 行序列化 ────────────────────────────────────────────────────────────


def _sorting_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "order_id": str(row["order_id"]),
        "delivery_method": row["delivery_method"],
        "delivery_method_label": DELIVERY_METHOD_LABELS[row["delivery_method"]],
        "status": row["status"],
        "status_label": SORTING_STATUS_LABELS[row["status"]],
        "operator_id": row["operator_id"],
        "note": row["note"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _line_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "sorting_id": str(row["sorting_id"]),
        "product_id": str(row["product_id"]),
        "expected_qty": int(row["expected_qty"]),
        "actual_qty": int(row["actual_qty"]),
        "reviewer": row["reviewer"],
        "note": row["note"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _spoilage_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "product_id": str(row["product_id"]),
        "sku": row["sku"],
        "batch_no": row["batch_no"],
        "quantity": int(row["quantity"]),
        "cost_cents": int(row["cost_cents"]),
        "reason": row["reason"],
        "reason_label": SPOILAGE_REASON_LABELS[row["reason"]],
        "handling": row["handling"],
        "recorded_by": row["recorded_by"],
        "recorded_date": row["recorded_date"].isoformat(),
        "created_at": row["created_at"].isoformat(),
    }


# ── 内部校验 ────────────────────────────────────────────────────────────


def _as_uuid(value: str, what: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise SortingNotFoundError(f"{what} not found") from None


def _check_int(value: Any, field: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    return value


def _check_text(value: str, field: str, *, required: bool = False) -> str:
    text = (value or "").strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"{field} is too long")
    return text


def _validate_date(value: str | None) -> date:
    """损耗日期须为 YYYY-MM-DD；缺省当天。返回 date 对象（asyncpg 的
    DATE 列不接受字符串参数）。"""
    if value is None:
        return date.today()
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ValueError("recorded_date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("recorded_date must be YYYY-MM-DD") from None


def _allowed_next(status: str, delivery_method: str) -> tuple[str, ...]:
    """给定当前状态与交付方式，返回合法的下一状态。"""
    allowed = ALLOWED_TRANSITIONS[status]
    if status == "sorting":
        ready = "ready_pickup" if delivery_method == "pickup" else "out_for_delivery"
        allowed = tuple(s for s in allowed if s in ("exception", ready))
    return allowed


async def _require_sorting(conn: Any, sorting_id: str) -> Any:
    row = await conn.fetchrow(
        "SELECT * FROM sorting_delivery WHERE id = $1",
        _as_uuid(sorting_id, "sorting delivery"),
    )
    if row is None:
        raise SortingNotFoundError("sorting delivery not found")
    return row


async def _require_product(conn: Any, product_id: str) -> None:
    exists = await conn.fetchval(
        "SELECT id FROM product WHERE id = $1", _as_uuid(product_id, "product")
    )
    if exists is None:
        raise SortingNotFoundError("product not found")


# ── 分拣交付：表头 ──────────────────────────────────────────────────────


async def create_sorting(
    pool: Any,
    *,
    order_id: str,
    delivery_method: str,
    operator_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    """按订单建分拣单（一单一表头）；订单须存在。"""
    order_uuid = _as_uuid(order_id, "order")
    if delivery_method not in DELIVERY_METHODS:
        raise ValueError("delivery_method must be pickup or delivery")
    clean_note = _check_text(note, "note")
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM customer_order WHERE id = $1", order_uuid)
        if exists is None:
            raise SortingNotFoundError("order not found")
        try:
            row = await conn.fetchrow(
                "INSERT INTO sorting_delivery"
                " (order_id, delivery_method, operator_id, note)"
                " VALUES ($1, $2, $3, $4) RETURNING *",
                order_uuid,
                delivery_method,
                operator_id,
                clean_note,
            )
        except UniqueViolationError as exc:
            raise SortingConflictError("order already has a sorting delivery") from exc
    return _sorting_row(row)


async def get_sorting(pool: Any, sorting_id: str, *, with_lines: bool = True) -> dict[str, Any]:
    """取分拣单（缺省含行明细）。"""
    async with pool.acquire() as conn:
        row = await _require_sorting(conn, sorting_id)
        result = _sorting_row(row)
        if with_lines:
            lines = await conn.fetch(
                "SELECT * FROM sorting_delivery_line WHERE sorting_id = $1 ORDER BY created_at",
                row["id"],
            )
            result["lines"] = [_line_row(r) for r in lines]
        return result


async def list_sortings(
    pool: Any,
    *,
    status: str | None = None,
    delivery_method: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """分拣单列表（可按状态 / 交付方式过滤）。"""
    if status is not None and status not in SORTING_STATUSES:
        raise ValueError("status must be a valid sorting status")
    if delivery_method is not None and delivery_method not in DELIVERY_METHODS:
        raise ValueError("delivery_method must be pickup or delivery")
    conditions: list[str] = []
    params: list[Any] = []
    if status is not None:
        params.append(status)
        conditions.append(f"status = ${len(params)}")
    if delivery_method is not None:
        params.append(delivery_method)
        conditions.append(f"delivery_method = ${len(params)}")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM sorting_delivery"
            f" {where} ORDER BY created_at DESC"
            f" LIMIT ${len(params) - 1} OFFSET ${len(params)}",
            *params,
        )
    return [_sorting_row(r) for r in rows]


async def update_sorting_status(pool: Any, sorting_id: str, *, status: str) -> dict[str, Any]:
    """分拣单状态流转；非法流转报 409。"""
    if status not in SORTING_STATUSES:
        raise ValueError("status must be a valid sorting status")
    async with pool.acquire() as conn:
        row = await _require_sorting(conn, sorting_id)
        allowed = _allowed_next(row["status"], row["delivery_method"])
        if status not in allowed:
            raise SortingConflictError(
                f"cannot move sorting delivery from {row['status']}"
                f" to {status}; allowed: {', '.join(allowed) or 'none'}"
            )
        updated = await conn.fetchrow(
            "UPDATE sorting_delivery SET status = $1, updated_at = NOW() WHERE id = $2 RETURNING *",
            status,
            row["id"],
        )
    return _sorting_row(updated)


async def update_sorting_note(pool: Any, sorting_id: str, *, note: str) -> dict[str, Any]:
    """只改分拣单备注（不碰状态）。"""
    clean_note = _check_text(note, "note")
    async with pool.acquire() as conn:
        row = await _require_sorting(conn, sorting_id)
        updated = await conn.fetchrow(
            "UPDATE sorting_delivery SET note = $1, updated_at = NOW() WHERE id = $2 RETURNING *",
            clean_note,
            row["id"],
        )
    return _sorting_row(updated)


# ── 分拣交付：行明细 ────────────────────────────────────────────────────


async def add_line(
    pool: Any,
    sorting_id: str,
    *,
    product_id: str,
    expected_qty: int,
    note: str = "",
) -> dict[str, Any]:
    """给分拣单加一行（一单一品一行；表头须在待分拣/分拣中）。"""
    qty = _check_int(expected_qty, "expected_qty", minimum=1)
    clean_note = _check_text(note, "note")
    async with pool.acquire() as conn:
        sheet = await _require_sorting(conn, sorting_id)
        if sheet["status"] not in ("pending", "sorting"):
            raise SortingConflictError(
                f"cannot add lines while sorting delivery is {sheet['status']}"
            )
        await _require_product(conn, product_id)
        try:
            row = await conn.fetchrow(
                "INSERT INTO sorting_delivery_line"
                " (sorting_id, product_id, expected_qty, note)"
                " VALUES ($1, $2, $3, $4) RETURNING *",
                sheet["id"],
                _as_uuid(product_id, "product"),
                qty,
                clean_note,
            )
        except UniqueViolationError as exc:
            raise SortingConflictError(
                "product already has a line on this sorting delivery"
            ) from exc
    return _line_row(row)


async def list_lines(pool: Any, sorting_id: str) -> list[dict[str, Any]]:
    """某分拣单的全部行明细。"""
    async with pool.acquire() as conn:
        sheet = await _require_sorting(conn, sorting_id)
        rows = await conn.fetch(
            "SELECT * FROM sorting_delivery_line WHERE sorting_id = $1 ORDER BY created_at",
            sheet["id"],
        )
    return [_line_row(r) for r in rows]


async def review_line(
    pool: Any,
    sorting_id: str,
    line_id: str,
    *,
    actual_qty: int | None = None,
    reviewer: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """行明细复核：改实配数量须留复核人；只允许在待分拣/分拣中做。"""
    if actual_qty is None and reviewer is None and note is None:
        raise ValueError("nothing to review")
    if actual_qty is not None:
        _check_int(actual_qty, "actual_qty", minimum=0)
    clean_reviewer = _check_text(reviewer, "reviewer") if reviewer is not None else None
    clean_note = _check_text(note, "note") if note is not None else None
    async with pool.acquire() as conn:
        sheet = await _require_sorting(conn, sorting_id)
        if sheet["status"] not in ("pending", "sorting"):
            raise SortingConflictError(
                f"cannot review lines while sorting delivery is {sheet['status']}"
            )
        row = await conn.fetchrow(
            "SELECT * FROM sorting_delivery_line WHERE id = $1 AND sorting_id = $2",
            _as_uuid(line_id, "sorting line"),
            sheet["id"],
        )
        if row is None:
            raise SortingNotFoundError("sorting line not found")
        if actual_qty is not None:
            effective_reviewer = clean_reviewer if clean_reviewer is not None else row["reviewer"]
            if not effective_reviewer.strip():
                raise ValueError("reviewer is required when setting actual_qty")
        sets: list[str] = []
        params: list[Any] = []
        if actual_qty is not None:
            params.append(actual_qty)
            sets.append(f"actual_qty = ${len(params)}")
        if clean_reviewer is not None:
            params.append(clean_reviewer)
            sets.append(f"reviewer = ${len(params)}")
        if clean_note is not None:
            params.append(clean_note)
            sets.append(f"note = ${len(params)}")
        params.append(row["id"])
        updated = await conn.fetchrow(
            "UPDATE sorting_delivery_line"
            f" SET {', '.join(sets)}, updated_at = NOW()"
            f" WHERE id = ${len(params)} RETURNING *",
            *params,
        )
    return _line_row(updated)


# ── 损耗记录 ────────────────────────────────────────────────────────────


async def record_spoilage(
    pool: Any,
    *,
    product_id: str,
    sku: str,
    quantity: int,
    cost_cents: int,
    reason: str,
    batch_no: str = "",
    handling: str = "",
    recorded_by: str = "",
    recorded_date: str | None = None,
) -> dict[str, Any]:
    """记一笔损耗（只记账，不扣库存）。商品须存在，原因须在闭合词表内。"""
    qty = _check_int(quantity, "quantity", minimum=1)
    cost = _check_int(cost_cents, "cost_cents", minimum=0)
    if reason not in SPOILAGE_REASONS:
        raise ValueError("reason must be one of " + ", ".join(SPOILAGE_REASONS))
    clean_sku = _check_text(sku, "sku", required=True)
    if len(clean_sku) > MAX_SKU_CHARS:
        raise ValueError("sku is too long")
    clean_batch = _check_text(batch_no, "batch_no")
    if len(clean_batch) > MAX_SKU_CHARS:
        raise ValueError("batch_no is too long")
    clean_handling = _check_text(handling, "handling")
    day = _validate_date(recorded_date)
    async with pool.acquire() as conn:
        await _require_product(conn, product_id)
        row = await conn.fetchrow(
            "INSERT INTO spoilage_record"
            " (product_id, sku, batch_no, quantity, cost_cents, reason,"
            " handling, recorded_by, recorded_date)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING *",
            _as_uuid(product_id, "product"),
            clean_sku,
            clean_batch,
            qty,
            cost,
            reason,
            clean_handling,
            recorded_by,
            day,
        )
    return _spoilage_row(row)


async def get_spoilage(pool: Any, spoilage_id: str) -> dict[str, Any]:
    """取一笔损耗记录。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM spoilage_record WHERE id = $1",
            _as_uuid(spoilage_id, "spoilage record"),
        )
    if row is None:
        raise SortingNotFoundError("spoilage record not found")
    return _spoilage_row(row)


async def list_spoilage(
    pool: Any,
    *,
    recorded_date: str | None = None,
    reason: str | None = None,
    product_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """损耗记录列表（可按日期 / 原因 / 商品过滤）。"""
    if reason is not None and reason not in SPOILAGE_REASONS:
        raise ValueError("reason must be one of " + ", ".join(SPOILAGE_REASONS))
    conditions: list[str] = []
    params: list[Any] = []
    if recorded_date is not None:
        params.append(_validate_date(recorded_date))
        conditions.append(f"recorded_date = ${len(params)}")
    if reason is not None:
        params.append(reason)
        conditions.append(f"reason = ${len(params)}")
    if product_id is not None:
        params.append(_as_uuid(product_id, "product"))
        conditions.append(f"product_id = ${len(params)}")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM spoilage_record"
            f" {where} ORDER BY recorded_date DESC, created_at DESC"
            f" LIMIT ${len(params) - 1} OFFSET ${len(params)}",
            *params,
        )
    return [_spoilage_row(r) for r in rows]


async def spoilage_daily_summary(pool: Any, day: str) -> dict[str, Any]:
    """每日损耗汇总：当日总笔数 / 总数量 / 总成本金额 + 按品类统计。"""
    clean_day = _validate_date(day)
    async with pool.acquire() as conn:
        total = await conn.fetchrow(
            "SELECT COUNT(*) AS n, COALESCE(SUM(quantity), 0) AS qty,"
            " COALESCE(SUM(cost_cents), 0) AS cost FROM spoilage_record"
            " WHERE recorded_date = $1",
            clean_day,
        )
        by_category = await conn.fetch(
            "SELECT COALESCE(pc.name, '未分类') AS category,"
            " COUNT(*) AS n, COALESCE(SUM(s.quantity), 0) AS qty,"
            " COALESCE(SUM(s.cost_cents), 0) AS cost"
            " FROM spoilage_record s"
            " LEFT JOIN product p ON p.id = s.product_id"
            " LEFT JOIN product_category pc"
            " ON pc.id = p.category_id AND pc.deleted_at IS NULL"
            " WHERE s.recorded_date = $1"
            " GROUP BY category ORDER BY cost DESC, category",
            clean_day,
        )
    return {
        "date": clean_day.isoformat(),
        "record_count": int(total["n"]),
        "total_quantity": int(total["qty"]),
        "total_cost_cents": int(total["cost"]),
        "by_category": [
            {
                "category": r["category"],
                "record_count": int(r["n"]),
                "total_quantity": int(r["qty"]),
                "total_cost_cents": int(r["cost"]),
            }
            for r in by_category
        ],
    }
