"""app.store_ops — 门店运营必需：每日报表、进销存日记、拣货/小票与自提核销。

界面全部在后台 /admin 下，鉴权沿用员工 JWT（admin/support 白名单）。金额一律
整数分，称重一律克；按件商品沿用“件”。本模块只做门店运营需要的薄层：
- 入库/损耗写库存批次与 stock_movement，随后前台立即反映库存；
- 销售出库从订单行自动带出（不重复记账）；
- 日末结存按批次当前库存倒推期初；
- CSV 导出与页面共用同一份 SQL 结果。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .deps import assert_staff_role, get_current_user

TZ = ZoneInfo("Asia/Shanghai")
router = APIRouter(prefix="/admin/store-ops", tags=["store-ops"])

OPS_DDL = """
CREATE TABLE IF NOT EXISTS store_inventory_entry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    biz_date DATE NOT NULL DEFAULT (now() AT TIME ZONE 'Asia/Shanghai')::date,
    variant_id UUID REFERENCES product_variant(id),
    product_id UUID,
    sku_code TEXT NOT NULL DEFAULT '',
    product_name TEXT NOT NULL,
    entry_type TEXT NOT NULL CHECK (entry_type IN ('in', 'loss')),
    qty INTEGER NOT NULL CHECK (qty > 0),
    unit TEXT NOT NULL DEFAULT 'g',
    unit_cost_cents INTEGER,
    supplier TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    operator_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_store_inventory_entry_date
    ON store_inventory_entry(biz_date DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_store_inventory_entry_variant
    ON store_inventory_entry(variant_id, biz_date DESC);

ALTER TABLE customer_order ADD COLUMN IF NOT EXISTS shipping_method_code TEXT;
ALTER TABLE customer_order ADD COLUMN IF NOT EXISTS weigh_status TEXT;
ALTER TABLE cart ADD COLUMN IF NOT EXISTS shipping_method_code TEXT;

-- 顾客拒绝补差时按预估重量出货：结算状态新增 estimate_only。
ALTER TABLE weigh_settlement DROP CONSTRAINT IF EXISTS weigh_settlement_status_check;
ALTER TABLE weigh_settlement ADD CONSTRAINT weigh_settlement_status_check
    CHECK (status = ANY (ARRAY['recorded', 'auto_refund', 'manual_refund_pending',
        'charge_pending', 'needs_customer_confirmation', 'customer_accepted',
        'customer_rejected', 'refunded', 'void', 'estimate_only']));
"""

#: 计入销售出库的订单状态（预订单在配货前不算销售）。
SALE_STATUSES = (
    "paid",
    "confirmed",
    "processing",
    "shipped",
    "delivered",
    "completed",
)


def _ops_admin(principal: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return assert_staff_role(principal, {"admin", "support"})


def _today() -> date:
    return datetime.now(TZ).date()


def _parse_day(value: str | None) -> date:
    if not value:
        return _today()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, "date must be YYYY-MM-DD") from exc


def _csv_response(filename: str, header: list[str], rows: list[list[Any]]) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    # UTF-8 BOM 让 Excel 直接识别中文。
    body = "\ufeff" + buf.getvalue()
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _num(value: Any) -> int:
    return int(value or 0)


async def _daily_rows(pool: Any, day: date) -> dict[str, Any]:
    async with pool.acquire() as conn:
        orders = await conn.fetch(
            """
            SELECT id, status, subtotal_cents, discount_cents, tax_cents,
                   shipping_cents, grand_total_cents, created_at
            FROM customer_order o
            WHERE (o.created_at AT TIME ZONE 'Asia/Shanghai')::date = $1
              AND o.status NOT IN ('cancelled', 'draft', 'failed')
              AND NOT EXISTS (
                  SELECT 1 FROM customer c
                  WHERE c.id = o.customer_id AND c.excluded_from_reports
              )
            """,
            day,
        )
        order_ids = [str(row["id"]) for row in orders]
        sales: list[dict[str, Any]] = []
        if order_ids:
            rows = await conn.fetch(
                """
                SELECT oli.variant_id, oli.pricing_mode, oli.quantity,
                       oli.preorder_qty, oli.estimated_weight_grams,
                       oli.actual_weight_grams, oli.line_total_cents,
                       oli.estimated_amount_cents, oli.actual_amount_cents,
                       pv.sku_code, p.title
                FROM order_line_item oli
                JOIN customer_order o ON o.id = oli.order_id
                LEFT JOIN product_variant pv ON pv.id = oli.variant_id
                LEFT JOIN product p ON p.id = pv.product_id
                WHERE oli.order_id = ANY($1::uuid[]) AND oli.preorder_qty = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM customer c
                      WHERE c.id = o.customer_id AND c.excluded_from_reports
                  )
                """,
                order_ids,
            )
            for r in rows:
                if r["pricing_mode"] == "weight":
                    qty = _num(r["actual_weight_grams"]) or _num(r["estimated_weight_grams"]) or 0
                    unit = "g"
                    amount = (
                        _num(r["actual_amount_cents"])
                        or _num(r["estimated_amount_cents"])
                        or _num(r["line_total_cents"])
                    )
                else:
                    qty = _num(r["quantity"])
                    unit = "件"
                    amount = _num(r["line_total_cents"])
                sales.append(
                    {
                        "variant_id": str(r["variant_id"]) if r["variant_id"] else "",
                        "sku": r["sku_code"] or "",
                        "product": r["title"] or "",
                        "qty": qty,
                        "unit": unit,
                        "amount_cents": amount,
                    }
                )

        entries = await conn.fetch(
            """
            SELECT variant_id, sku_code, product_name, entry_type, qty, unit, unit_cost_cents,
                   loss_category
            FROM store_inventory_entry WHERE biz_date = $1
            """,
            day,
        )
        stock = await conn.fetch(
            """
            SELECT ib.variant_id, COALESCE(SUM(ib.stock_qty), 0)::int AS stock_qty
            FROM inventory_batch ib
            WHERE ib.status = 'active' AND ib.deleted_at IS NULL
            GROUP BY ib.variant_id
            """
        )
        settlements = await conn.fetch(
            """
            SELECT COALESCE(SUM(CASE WHEN diff_cents < 0 THEN -diff_cents ELSE 0 END), 0)::int
                       AS refund_cents,
                   COALESCE(SUM(CASE WHEN diff_cents > 0 THEN diff_cents ELSE 0 END), 0)::int
                       AS charge_cents
            FROM weigh_settlement ws
            JOIN customer_order o ON o.id = ws.order_id
            WHERE (ws.created_at AT TIME ZONE 'Asia/Shanghai')::date = $1
              AND NOT EXISTS (
                  SELECT 1 FROM customer c
                  WHERE c.id = o.customer_id AND c.excluded_from_reports
              )
            """,
            day,
        )
        refunds = await conn.fetchval(
            """
            SELECT COALESCE(SUM(CASE WHEN actual_refunded_amount > 0
                                     THEN actual_refunded_amount ELSE approved_amount END), 0)::int
            FROM refund_request rr
            JOIN customer_order o ON o.id = rr.order_id
            WHERE (rr.created_at AT TIME ZONE 'Asia/Shanghai')::date = $1
              AND rr.status NOT IN ('rejected', 'cancelled')
              AND NOT EXISTS (
                  SELECT 1 FROM customer c
                  WHERE c.id = o.customer_id AND c.excluded_from_reports
              )
            """,
            day,
        )
        preorders = await conn.fetchval(
            """
            SELECT COALESCE(SUM(oli.preorder_qty), 0)::int
            FROM order_line_item oli
            JOIN customer_order o ON o.id = oli.order_id
            WHERE (o.created_at AT TIME ZONE 'Asia/Shanghai')::date = $1
              AND o.status NOT IN ('cancelled', 'failed')
              AND NOT EXISTS (
                  SELECT 1 FROM customer c
                  WHERE c.id = o.customer_id AND c.excluded_from_reports
              )
            """,
            day,
        )
        top = await conn.fetch(
            """
            SELECT COALESCE(p.title, '未知商品') AS title,
                   COALESCE(SUM(CASE WHEN oli.pricing_mode = 'weight'
                        THEN COALESCE(oli.actual_weight_grams, oli.estimated_weight_grams, 0)
                        ELSE oli.quantity END), 0)::bigint AS qty,
                   COALESCE(SUM(COALESCE(oli.actual_amount_cents, oli.line_total_cents)), 0)::bigint
                       AS amount_cents,
                   MAX(CASE WHEN oli.pricing_mode = 'weight' THEN 'g' ELSE '件' END) AS unit
            FROM order_line_item oli
            JOIN customer_order o ON o.id = oli.order_id
            LEFT JOIN product_variant pv ON pv.id = oli.variant_id
            LEFT JOIN product p ON p.id = pv.product_id
            WHERE (o.created_at AT TIME ZONE 'Asia/Shanghai')::date = $1
              AND o.status NOT IN ('cancelled', 'failed', 'draft')
              AND oli.preorder_qty = 0
              AND NOT EXISTS (
                  SELECT 1 FROM customer c
                  WHERE c.id = o.customer_id AND c.excluded_from_reports
              )
            GROUP BY 1
            ORDER BY amount_cents DESC, qty DESC
            LIMIT 20
            """,
            day,
        )

    by_variant: dict[str, dict[str, Any]] = {}
    for s in sales:
        key = s["variant_id"] or f"name:{s['product']}"
        row = by_variant.setdefault(
            key,
            {
                "sku": s["sku"],
                "product": s["product"],
                "unit": s["unit"],
                "in_qty": 0,
                "sales_qty": 0,
                "loss_qty": 0,
                "amount_cents": 0,
                "stock_qty": 0,
                "unit_cost_cents": None,
            },
        )
        row["sales_qty"] += s["qty"]
        row["amount_cents"] += s["amount_cents"]
    for e in entries:
        key = str(e["variant_id"]) if e["variant_id"] else "unknown"
        row = by_variant.setdefault(
            key,
            {
                "sku": "",
                "product": "未知商品",
                "unit": e["unit"],
                "in_qty": 0,
                "sales_qty": 0,
                "loss_qty": 0,
                "amount_cents": 0,
                "stock_qty": 0,
                "unit_cost_cents": None,
            },
        )
        if e["entry_type"] == "in":
            row["in_qty"] += _num(e["qty"])
            if e["unit_cost_cents"] is not None:
                row["unit_cost_cents"] = _num(e["unit_cost_cents"])
        else:
            row["loss_qty"] += _num(e["qty"])
    for r in stock:
        key = str(r["variant_id"]) if r["variant_id"] else "unknown"
        if key in by_variant:
            by_variant[key]["stock_qty"] = _num(r["stock_qty"])
    journal = []
    for row in by_variant.values():
        opening = row["stock_qty"] - row["in_qty"] + row["sales_qty"] + row["loss_qty"]
        journal.append({**row, "opening_qty": max(0, opening)})
    journal.sort(key=lambda r: (r["sales_qty"] + r["in_qty"] + r["loss_qty"]) * -1)
    total_sales = sum(_num(o["grand_total_cents"]) for o in orders)
    aov = int(total_sales / len(orders)) if orders else 0
    loss_by_category: dict[str, dict[str, int]] = {}
    loss_value_cents = 0
    for e in entries:
        if e["entry_type"] != "loss":
            continue
        category = e["loss_category"] or "other"
        qty = _num(e["qty"])
        value = qty * _num(e["unit_cost_cents"]) if e["unit_cost_cents"] is not None else 0
        loss_value_cents += value
        bucket = loss_by_category.setdefault(category, {"qty": 0, "value_cents": 0, "entries": 0})
        bucket["qty"] += qty
        bucket["value_cents"] += value
        bucket["entries"] += 1
    loss_denominator = total_sales + loss_value_cents
    loss_rate_pct = (
        round(loss_value_cents * 100 / loss_denominator, 2) if loss_denominator > 0 else None
    )
    return {
        "date": day.isoformat(),
        "kpis": {
            "sales_cents": total_sales,
            "order_count": len(orders),
            "aov_cents": aov,
            "weigh_refund_cents": _num(settlements[0]["refund_cents"]) if settlements else 0,
            "weigh_charge_cents": _num(settlements[0]["charge_cents"]) if settlements else 0,
            "refund_cents": _num(refunds),
            "preorder_qty": _num(preorders),
            "loss_value_cents": loss_value_cents,
            "loss_rate_pct": loss_rate_pct,
            "loss_by_category": [
                {
                    "category": category,
                    "label": LOSS_CATEGORY_LABELS.get(category, category),
                    **values,
                }
                for category, values in sorted(loss_by_category.items())
            ],
        },
        "top_skus": [
            {
                "title": r["title"],
                "qty": _num(r["qty"]),
                "unit": r["unit"],
                "amount_cents": _num(r["amount_cents"]),
            }
            for r in top
        ],
        "journal": journal,
    }


@router.get("/daily-report")
async def daily_report(
    request: Request,
    day: str | None = Query(None, alias="date"),
    _=Depends(_ops_admin),
) -> dict[str, Any]:
    return await _daily_rows(request.app.state.pool, _parse_day(day))


@router.get("/daily-report.csv")
async def daily_report_csv(
    request: Request,
    day: str | None = Query(None, alias="date"),
    _=Depends(_ops_admin),
) -> Response:
    data = await _daily_rows(request.app.state.pool, _parse_day(day))
    k = data["kpis"]
    rows = [
        ["销售额(分)", k["sales_cents"]],
        ["订单数", k["order_count"]],
        ["客单价(分)", k["aov_cents"]],
        ["称重退款(分)", k["weigh_refund_cents"]],
        ["称重补收(分)", k["weigh_charge_cents"]],
        ["退款(分)", k["refund_cents"]],
        ["缺货预订数量", k["preorder_qty"]],
        ["损耗金额(分)", k["loss_value_cents"]],
        ["损耗率(%)", k["loss_rate_pct"] if k["loss_rate_pct"] is not None else "无销售/损耗"],
    ]
    for item in k["loss_by_category"]:
        rows.append(
            [
                f"损耗-{item['label']}",
                f"数量 {item['qty']} / 金额 {item['value_cents']}分 / {item['entries']} 笔",
            ]
        )
    for s in data["top_skus"]:
        rows.append([f"Top: {s['title']}", f"{s['qty']}{s['unit']} / {s['amount_cents']}分"])
    return _csv_response(f"daily-report-{data['date']}.csv", ["指标", "值"], rows)


@router.get("/journal")
async def journal(
    request: Request,
    day: str | None = Query(None, alias="date"),
    _=Depends(_ops_admin),
) -> dict[str, Any]:
    data = await _daily_rows(request.app.state.pool, _parse_day(day))
    async with request.app.state.pool.acquire() as conn:
        entries = await conn.fetch(
            """
            SELECT id, biz_date, variant_id, sku_code, product_name, entry_type,
                   qty, unit, unit_cost_cents, supplier, reason, note, created_at,
                   loss_category
            FROM store_inventory_entry
            WHERE biz_date = $1 ORDER BY created_at DESC
            """,
            _parse_day(day),
        )
    return {
        "date": data["date"],
        "rows": data["journal"],
        "entries": [
            {
                "id": str(e["id"]),
                "date": e["biz_date"].isoformat(),
                "variant_id": str(e["variant_id"]) if e["variant_id"] else None,
                "sku": e["sku_code"],
                "product": e["product_name"],
                "entry_type": e["entry_type"],
                "qty": _num(e["qty"]),
                "unit": e["unit"],
                "unit_cost_cents": e["unit_cost_cents"],
                "supplier": e["supplier"],
                "reason": e["reason"],
                "loss_category": e["loss_category"],
                "loss_category_label": LOSS_CATEGORY_LABELS.get(
                    e["loss_category"] or "other", "其他"
                ),
                "note": e["note"],
                "created_at": e["created_at"].isoformat(),
            }
            for e in entries
        ],
    }


@router.get("/journal.csv")
async def journal_csv(
    request: Request,
    day: str | None = Query(None, alias="date"),
    _=Depends(_ops_admin),
) -> Response:
    day_value = _parse_day(day)
    pool = request.app.state.pool
    base = await _daily_rows(pool, day_value)
    data = {"date": base["date"], "rows": base["journal"]}
    rows = [
        [
            r["sku"],
            r["product"],
            r["unit"],
            r["opening_qty"],
            r["in_qty"],
            r["sales_qty"],
            r["loss_qty"],
            r["stock_qty"],
            r["unit_cost_cents"] if r["unit_cost_cents"] is not None else "",
            r["amount_cents"],
        ]
        for r in data["rows"]
    ]
    return _csv_response(
        f"journal-{data['date']}.csv",
        [
            "SKU",
            "商品",
            "单位",
            "期初",
            "入库",
            "销售出库",
            "损耗",
            "日末结存",
            "进价(分)",
            "销售额(分)",
        ],
        rows,
    )


class InboundInput(BaseModel):
    variant_id: str
    qty: int = Field(gt=0)
    unit_cost_cents: int | None = Field(default=None, ge=0)
    supplier: str = ""
    note: str = ""


LOSS_CATEGORIES = ("expired", "damaged", "tasting", "other")
LOSS_CATEGORY_LABELS = {
    "expired": "过期",
    "damaged": "破损",
    "tasting": "试吃",
    "other": "其他",
}


class LossInput(BaseModel):
    variant_id: str
    qty: int = Field(gt=0)
    reason: str = Field(min_length=1)
    note: str = ""
    loss_category: str = "other"


async def _variant(conn: Any, variant_id: str) -> Any:
    row = await conn.fetchrow(
        """
        SELECT pv.id, pv.product_id, pv.sku_code, pv.pricing_mode, pv.stock_unit,
               pv.unit_price_cents, p.title,
               (SELECT ib.id FROM inventory_batch ib
                WHERE ib.variant_id = pv.id AND ib.status = 'active' AND ib.deleted_at IS NULL
                ORDER BY ib.created_at DESC LIMIT 1) AS batch_id,
               (SELECT ib.location_id FROM inventory_batch ib
                WHERE ib.variant_id = pv.id AND ib.status = 'active' AND ib.deleted_at IS NULL
                ORDER BY ib.created_at DESC LIMIT 1) AS location_id,
               (SELECT ib.retail_price_cents FROM inventory_batch ib
                WHERE ib.variant_id = pv.id AND ib.status = 'active' AND ib.deleted_at IS NULL
                ORDER BY ib.created_at DESC LIMIT 1) AS retail_price_cents
        FROM product_variant pv JOIN product p ON p.id = pv.product_id
        WHERE pv.id = $1 AND p.deleted_at IS NULL
        """,
        variant_id,
    )
    if row is None:
        raise HTTPException(404, "variant not found")
    return row


@router.post("/journal/in")
async def journal_in(
    request: Request,
    body: InboundInput,
    principal: dict[str, Any] = Depends(_ops_admin),
) -> dict[str, Any]:
    pool = request.app.state.pool
    if body.loss_category not in LOSS_CATEGORIES:
        raise HTTPException(400, "loss_category must be expired/damaged/tasting/other")
    async with pool.acquire() as conn:
        async with conn.transaction():
            v = await _variant(conn, body.variant_id)
            unit = "g" if (v["pricing_mode"] == "weight" or v["stock_unit"] == "gram") else "件"
            location_id = v["location_id"]
            if location_id is None:
                location_id = await conn.fetchval(
                    "SELECT id FROM stock_location WHERE status = 'active' "
                    "ORDER BY created_at LIMIT 1"
                )
            batch_id = v["batch_id"]
            retail = _num(v["retail_price_cents"]) or _num(v["unit_price_cents"])
            if batch_id is None:
                batch_id = await conn.fetchval(
                    """
                    INSERT INTO inventory_batch
                        (id, batch_no, variant_id, location_id, video_url,
                         cost_price_cents, retail_price_cents, stock_qty,
                         inspection_status, status)
                    VALUES (gen_random_uuid(), $1, $2, $3, '', $4, $5, 0, 'pending', 'active')
                    RETURNING id
                    """,
                    f"STORE-{v['sku_code'] or 'SKU'}-{datetime.now(TZ):%Y%m%d%H%M%S}",
                    v["id"],
                    location_id,
                    _num(body.unit_cost_cents) if body.unit_cost_cents is not None else 0,
                    retail,
                )
            await conn.execute(
                """
                UPDATE inventory_batch
                SET stock_qty = stock_qty + $1,
                    cost_price_cents = COALESCE($2, cost_price_cents),
                    updated_at = NOW()
                WHERE id = $3
                """,
                body.qty,
                body.unit_cost_cents,
                batch_id,
            )
            await conn.execute(
                """
                INSERT INTO stock_movement
                    (product_id, variant_id, location_id, batch_id, movement_type,
                     quantity, unit_cost, reference_type, reason, operator_id)
                VALUES ($1, $2, $3, $4, 'receipt', $5, $6, 'store_journal', $7, $8)
                """,
                str(v["product_id"]),
                str(v["id"]),
                location_id,
                batch_id,
                body.qty,
                (body.unit_cost_cents or 0) / 100,
                body.supplier or body.note or "门店入库",
                str(principal.get("user_id") or ""),
            )
            entry_id = await conn.fetchval(
                """
                INSERT INTO store_inventory_entry
                    (variant_id, product_id, sku_code, product_name, entry_type,
                     qty, unit, unit_cost_cents, supplier, note, operator_id)
                VALUES ($1, $2, $3, $4, 'in', $5, $6, $7, $8, $9, $10)
                RETURNING id
                """,
                v["id"],
                v["product_id"],
                v["sku_code"] or "",
                v["title"],
                body.qty,
                unit,
                body.unit_cost_cents,
                body.supplier,
                body.note,
                str(principal.get("user_id") or ""),
            )
            stock = await conn.fetchval(
                "SELECT stock_qty FROM inventory_batch WHERE id = $1", batch_id
            )
    return {
        "status": "recorded",
        "entry_id": str(entry_id),
        "batch_id": str(batch_id),
        "stock_qty": _num(stock),
        "unit": unit,
    }


@router.post("/journal/loss")
async def journal_loss(
    request: Request,
    body: LossInput,
    principal: dict[str, Any] = Depends(_ops_admin),
) -> dict[str, Any]:
    pool = request.app.state.pool
    if body.loss_category not in LOSS_CATEGORIES:
        raise HTTPException(400, "loss_category must be expired/damaged/tasting/other")
    async with pool.acquire() as conn:
        async with conn.transaction():
            v = await _variant(conn, body.variant_id)
            unit = "g" if (v["pricing_mode"] == "weight" or v["stock_unit"] == "gram") else "件"
            batch = await conn.fetchrow(
                """
                SELECT id, location_id, stock_qty, reserved_qty, cost_price_cents
                FROM inventory_batch
                WHERE variant_id = $1 AND status = 'active' AND deleted_at IS NULL
                  AND stock_qty - reserved_qty >= $2
                ORDER BY created_at DESC LIMIT 1
                FOR UPDATE
                """,
                v["id"],
                body.qty,
            )
            if batch is None:
                raise HTTPException(409, "可用库存不足，无法报损")
            await conn.execute(
                "UPDATE inventory_batch SET stock_qty = stock_qty - $1, "
                "updated_at = NOW() WHERE id = $2",
                body.qty,
                batch["id"],
            )
            await conn.execute(
                """
                INSERT INTO stock_movement
                    (product_id, variant_id, location_id, batch_id, movement_type,
                     quantity, reference_type, reason, operator_id)
                VALUES ($1, $2, $3, $4, 'adjustment_down', $5, 'store_journal', $6, $7)
                """,
                str(v["product_id"]),
                str(v["id"]),
                batch["location_id"],
                batch["id"],
                body.qty,
                body.reason,
                str(principal.get("user_id") or ""),
            )
            entry_id = await conn.fetchval(
                """
                INSERT INTO store_inventory_entry
                    (variant_id, product_id, sku_code, product_name, entry_type,
                     qty, unit, unit_cost_cents, reason, note, operator_id, loss_category)
                VALUES ($1, $2, $3, $4, 'loss', $5, $6, $7, $8, $9, $10, $11)
                RETURNING id
                """,
                v["id"],
                v["product_id"],
                v["sku_code"] or "",
                v["title"],
                body.qty,
                unit,
                batch["cost_price_cents"],
                body.reason,
                body.note,
                str(principal.get("user_id") or ""),
                body.loss_category,
            )
            stock = await conn.fetchval(
                "SELECT stock_qty FROM inventory_batch WHERE id = $1", batch["id"]
            )
    return {"status": "recorded", "entry_id": str(entry_id), "stock_qty": _num(stock), "unit": unit}


@router.get("/orders/{order_id}/summary")
async def order_summary(request: Request, order_id: str, _=Depends(_ops_admin)) -> dict[str, Any]:
    """打印/拣货用订单摘要：含称重预估与实称栏位。"""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        order = await conn.fetchrow(
            """
            SELECT id, status, currency, subtotal_cents, discount_cents, tax_cents,
                   shipping_cents, grand_total_cents, shipping_address,
                   shipping_method_code, weigh_status, created_at
            FROM customer_order WHERE id = $1
            """,
            order_id,
        )
        if order is None:
            raise HTTPException(404, "order not found")
        lines = await conn.fetch(
            """
            SELECT oli.id, oli.quantity, oli.unit_price_cents, oli.line_total_cents,
                   oli.pricing_mode, oli.unit_price_cents_per_jin, oli.price_basis_grams,
                   oli.estimated_weight_grams, oli.estimated_amount_cents,
                   oli.actual_weight_grams, oli.actual_amount_cents,
                   oli.settlement_status, pv.sku_code, p.title
            FROM order_line_item oli
            LEFT JOIN product_variant pv ON pv.id = oli.variant_id
            LEFT JOIN product p ON p.id = pv.product_id
            WHERE oli.order_id = $1 ORDER BY oli.created_at
            """,
            order_id,
        )
        settlements = await conn.fetch(
            "SELECT * FROM weigh_settlement WHERE order_id = $1 ORDER BY created_at",
            order_id,
        )
    addr = order["shipping_address"]
    if isinstance(addr, str):
        try:
            addr = json.loads(addr)
            if isinstance(addr, str):
                addr = json.loads(addr)
        except (TypeError, ValueError):
            addr = {}
    return {
        "order": {
            "id": str(order["id"]),
            "status": order["status"],
            "currency": order["currency"],
            "subtotal_cents": _num(order["subtotal_cents"]),
            "discount_cents": _num(order["discount_cents"]),
            "tax_cents": _num(order["tax_cents"]),
            "shipping_cents": _num(order["shipping_cents"]),
            "grand_total_cents": _num(order["grand_total_cents"]),
            "shipping_address": addr or {},
            "shipping_method_code": order["shipping_method_code"],
            "weigh_status": order["weigh_status"],
            "created_at": order["created_at"].isoformat(),
        },
        "lines": [
            {
                "id": str(li["id"]),
                "title": li["title"] or "",
                "sku": li["sku_code"] or "",
                "quantity": _num(li["quantity"]),
                "unit_price_cents": _num(li["unit_price_cents"]),
                "line_total_cents": _num(li["line_total_cents"]),
                "pricing_mode": li["pricing_mode"],
                "unit_price_cents_per_jin": li["unit_price_cents_per_jin"],
                "price_basis_grams": _num(li["price_basis_grams"] or 500),
                "estimated_weight_grams": li["estimated_weight_grams"],
                "estimated_amount_cents": li["estimated_amount_cents"],
                "actual_weight_grams": li["actual_weight_grams"],
                "actual_amount_cents": li["actual_amount_cents"],
                "settlement_status": li["settlement_status"],
            }
            for li in lines
        ],
        "settlements": [
            {
                "id": str(s["id"]),
                "order_line_item_id": str(s["order_line_item_id"]),
                "weight_grams": _num(s["weight_grams"]),
                "actual_amount_cents": _num(s["actual_amount_cents"]),
                "diff_cents": _num(s["diff_cents"]),
                "status": s["status"],
            }
            for s in settlements
        ],
    }


class StaffWeighInput(BaseModel):
    order_line_item_id: str
    weight_grams: int = Field(gt=0)
    note: str = ""


@router.post("/orders/{order_id}/weigh")
async def staff_record_weigh(
    request: Request,
    order_id: str,
    body: StaffWeighInput,
    principal: dict[str, Any] = Depends(_ops_admin),
) -> dict[str, Any]:
    from .weigh_settle import WeighSettlementConflict, WeighSettlementError, record_actual_weight

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT order_id FROM order_line_item WHERE id = $1", body.order_line_item_id
        )
    if owner is None or str(owner) != str(order_id):
        raise HTTPException(404, "order line not found")
    cfg = request.app.state.config
    try:
        settlement = await record_actual_weight(
            pool,
            order_line_item_id=body.order_line_item_id,
            weight_grams=body.weight_grams,
            tolerance_percent=int(cfg.weigh_diff_tolerance_percent),
            created_by=str(principal.get("user_id") or ""),
            note=body.note,
        )
    except WeighSettlementConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except WeighSettlementError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"settlement": settlement}


@router.post("/orders/{order_id}/verify-pickup")
async def verify_pickup(
    request: Request,
    order_id: str,
    principal: dict[str, Any] = Depends(_ops_admin),
) -> dict[str, Any]:
    """自提核销：仅自提单，且必须已收款；核销后订单完成。"""
    from .orders.models import OrderStatus
    from .orders.service import OrderService

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, shipping_method_code FROM customer_order WHERE id = $1",
            order_id,
        )
    if row is None:
        raise HTTPException(404, "order not found")
    if row["shipping_method_code"] != "pickup":
        raise HTTPException(400, "该订单不是到店自提单")
    if row["status"] not in (
        "paid",
        "confirmed",
        "processing",
        "packed",
        "shipped",
        "delivered",
    ):
        raise HTTPException(400, f"订单状态 {row['status']} 不可核销")
    svc = OrderService(pool)
    chain = [
        "paid",
        "confirmed",
        "processing",
        "packed",
        "shipped",
        "delivered",
        "completed",
    ]
    current = row["status"]
    start = chain.index(current) if current in chain else 0
    order = None
    for nxt in chain[start + 1 :]:
        order = await svc.transition_status(
            order_id,
            OrderStatus(nxt),
            reason="门店自提核销",
            operator_id=str(principal.get("user_id") or ""),
        )
    assert order is not None
    return {"status": "verified", "order_id": order_id, "order_status": order.status.value}


@router.get("/orders/{order_id}/print")
async def order_print(
    request: Request,
    order_id: str,
    paper: str = Query("a4", pattern="^(58|a4)$"),
    _=Depends(_ops_admin),
) -> dict[str, Any]:
    data = await order_summary(request, order_id, _)
    data["paper"] = paper
    return data


# ── 批次直接改价（留痕）────────────────────────────────────────────────


class BatchPriceInput(BaseModel):
    retail_price_cents: int = Field(ge=0, le=100_000_000)
    reason: str = Field(default="", max_length=200)


@router.patch("/batches/{batch_id}/price")
async def update_batch_price(
    request: Request,
    batch_id: str,
    body: BatchPriceInput,
    principal: dict[str, Any] = Depends(_ops_admin),
) -> dict[str, Any]:
    """已有批次直接改价：同一事务写批次新价 + 改价记录（旧价/新价/操作人/时间）。"""
    pool = request.app.state.pool
    operator_id = str(principal.get("user_id") or "")
    operator_name = str(principal.get("email") or principal.get("user_id") or "")
    async with pool.acquire() as conn:
        async with conn.transaction():
            batch = await conn.fetchrow(
                "SELECT id, batch_no, retail_price_cents, currency FROM inventory_batch "
                "WHERE id = $1 AND deleted_at IS NULL FOR UPDATE",
                batch_id,
            )
            if batch is None:
                raise HTTPException(404, "batch not found")
            old_price = int(batch["retail_price_cents"])
            new_price = int(body.retail_price_cents)
            if old_price == new_price:
                return {
                    "batch_id": batch_id,
                    "batch_no": batch["batch_no"],
                    "old_price_cents": old_price,
                    "new_price_cents": new_price,
                    "changed": False,
                }
            await conn.execute(
                "UPDATE inventory_batch SET retail_price_cents = $1, updated_at = NOW() "
                "WHERE id = $2",
                new_price,
                batch_id,
            )
            log = await conn.fetchrow(
                """
                INSERT INTO batch_price_change
                    (batch_id, old_price_cents, new_price_cents,
                     operator_id, operator_name, reason)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, created_at
                """,
                batch_id,
                old_price,
                new_price,
                operator_id,
                operator_name,
                body.reason,
            )
    return {
        "batch_id": batch_id,
        "batch_no": batch["batch_no"],
        "currency": batch["currency"],
        "old_price_cents": old_price,
        "new_price_cents": new_price,
        "changed": True,
        "change_id": str(log["id"]),
        "created_at": log["created_at"].isoformat(),
        "operator": operator_name,
    }


@router.get("/batches/{batch_id}/price-history")
async def batch_price_history(
    request: Request,
    batch_id: str,
    limit: int = Query(50, ge=1, le=200),
    _=Depends(_ops_admin),
) -> dict[str, Any]:
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, old_price_cents, new_price_cents, operator_id, operator_name,
                   reason, created_at
            FROM batch_price_change
            WHERE batch_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            batch_id,
            limit,
        )
    return {
        "batch_id": batch_id,
        "changes": [
            {
                "id": str(r["id"]),
                "old_price_cents": int(r["old_price_cents"]),
                "new_price_cents": int(r["new_price_cents"]),
                "operator_id": r["operator_id"],
                "operator_name": r["operator_name"],
                "reason": r["reason"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }


# ── 今日待办 / 新订单提醒 ─────────────────────────────────────────────


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


@router.get("/today")
async def today_board(
    request: Request,
    _=Depends(_ops_admin),
) -> dict[str, Any]:
    """今日待办看板：按截止时间排序的六类门店任务。"""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT o.id, o.status, o.shipping_method_code, o.weigh_status,
                   o.created_at, o.promised_delivery_at, o.pickup_arrived_at,
                   o.grand_total_cents, o.currency,
                   COALESCE(NULLIF(c.name, ''), c.email, '') AS customer,
                   EXISTS (
                       SELECT 1 FROM order_line_item li
                       WHERE li.order_id = o.id AND li.pricing_mode = 'weight'
                         AND li.actual_weight_grams IS NULL
                   ) AS needs_weigh,
                   EXISTS (
                       SELECT 1 FROM order_line_item li
                       WHERE li.order_id = o.id AND li.preorder_qty > 0
                   ) AS has_preorder,
                   (SELECT COUNT(*) FROM order_line_item li WHERE li.order_id = o.id)::int
                       AS line_count
            FROM customer_order o
            LEFT JOIN customer c ON c.id = o.customer_id
            WHERE o.status NOT IN ('cancelled', 'failed', 'draft', 'completed', 'refunded')
              AND COALESCE(c.excluded_from_reports, FALSE) = FALSE
            ORDER BY COALESCE(o.promised_delivery_at, o.created_at) ASC
            """
        )

    groups: dict[str, list[dict[str, Any]]] = {
        "pick": [],
        "weigh": [],
        "weigh_confirm": [],
        "delivery": [],
        "pickup": [],
        "preorder": [],
    }
    for r in rows:
        task = {
            "order_id": str(r["id"]),
            "status": r["status"],
            "customer": r["customer"],
            "grand_total_cents": int(r["grand_total_cents"] or 0),
            "currency": r["currency"],
            "shipping_method_code": r["shipping_method_code"],
            "weigh_status": r["weigh_status"],
            "line_count": int(r["line_count"] or 0),
            "created_at": _iso(r["created_at"]),
            "deadline": _iso(r["promised_delivery_at"] or r["created_at"]),
            "pickup_arrived_at": _iso(r["pickup_arrived_at"]),
        }
        if r["status"] in ("paid", "confirmed", "processing"):
            groups["pick"].append(task)
        if r["needs_weigh"] and r["status"] in (
            "paid",
            "confirmed",
            "processing",
            "packed",
        ):
            groups["weigh"].append(task)
        if r["weigh_status"] in ("pending_customer", "disputed"):
            groups["weigh_confirm"].append(task)
        if r["shipping_method_code"] != "pickup" and r["status"] in (
            "paid",
            "confirmed",
            "processing",
            "packed",
        ):
            groups["delivery"].append(task)
        if r["shipping_method_code"] == "pickup" and r["status"] in (
            "paid",
            "confirmed",
            "processing",
            "packed",
            "shipped",
            "delivered",
        ):
            groups["pickup"].append(task)
        if r["has_preorder"]:
            groups["preorder"].append(task)

    labels = {
        "pick": "待拣货",
        "weigh": "待称重",
        "weigh_confirm": "待补差确认",
        "delivery": "待配送",
        "pickup": "待自提",
        "preorder": "缺货预订",
    }
    # 自提到店的排到最前（顾客已经在店里等）。
    groups["pickup"].sort(key=lambda t: (t["pickup_arrived_at"] is None, t["deadline"] or ""))
    return {
        "generated_at": datetime.now(TZ).isoformat(),
        "groups": [
            {"key": key, "label": labels[key], "count": len(groups[key]), "tasks": groups[key]}
            for key in labels
        ],
        "total": sum(len(v) for v in groups.values()),
    }


def _parse_after(value: str | None) -> datetime:
    if not value:
        return datetime.now(TZ)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, "after must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed


@router.get("/alerts")
async def order_alerts(
    request: Request,
    after: str | None = Query(None, description="只返回该时间之后的新事件（ISO-8601）"),
    limit: int = Query(50, ge=1, le=200),
    _=Depends(_ops_admin),
) -> dict[str, Any]:
    """门店提醒轮询：新待处理订单、顾客补差确认/拒绝、顾客到店自提。"""
    since = _parse_after(after)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        new_orders = await conn.fetch(
            """
            SELECT o.id, o.status, o.grand_total_cents, o.currency, o.created_at,
                   COALESCE(NULLIF(c.name, ''), c.email, '') AS customer
            FROM customer_order o
            LEFT JOIN customer c ON c.id = o.customer_id
            WHERE o.created_at > $1
              AND o.status IN ('pending', 'paid', 'confirmed')
              AND COALESCE(c.excluded_from_reports, FALSE) = FALSE
            ORDER BY o.created_at DESC
            LIMIT $2
            """,
            since,
            limit,
        )
        weigh = await conn.fetch(
            """
            SELECT ws.id, ws.order_id, ws.status, ws.diff_cents, ws.updated_at,
                   COALESCE(NULLIF(c.name, ''), c.email, '') AS customer
            FROM weigh_settlement ws
            JOIN customer_order o ON o.id = ws.order_id
            LEFT JOIN customer c ON c.id = o.customer_id
            WHERE ws.updated_at > $1
              AND ws.status IN ('customer_accepted', 'customer_rejected',
                                'estimate_only', 'charge_pending')
              AND COALESCE(c.excluded_from_reports, FALSE) = FALSE
            ORDER BY ws.updated_at DESC
            LIMIT $2
            """,
            since,
            limit,
        )
        arrivals = await conn.fetch(
            """
            SELECT o.id, o.pickup_arrived_at, o.grand_total_cents, o.currency,
                   COALESCE(NULLIF(c.name, ''), c.email, '') AS customer
            FROM customer_order o
            LEFT JOIN customer c ON c.id = o.customer_id
            WHERE o.pickup_arrived_at > $1
              AND COALESCE(c.excluded_from_reports, FALSE) = FALSE
            ORDER BY o.pickup_arrived_at DESC
            LIMIT $2
            """,
            since,
            limit,
        )

    events: list[dict[str, Any]] = []
    for r in new_orders:
        events.append(
            {
                "type": "new_order",
                "at": _iso(r["created_at"]),
                "order_id": str(r["id"]),
                "title": f"新订单 ¥{int(r['grand_total_cents'] or 0) / 100:.2f}",
                "detail": r["customer"] or "",
                "status": r["status"],
            }
        )
    for r in weigh:
        labels = {
            "customer_accepted": "顾客已确认补差",
            "customer_rejected": "顾客拒绝补差",
            "estimate_only": "顾客拒绝补差（按预估出货）",
            "charge_pending": "补差待线下收款",
        }
        events.append(
            {
                "type": "weigh_decision",
                "at": _iso(r["updated_at"]),
                "order_id": str(r["order_id"]),
                "title": labels.get(r["status"], f"称重结算 {r['status']}"),
                "detail": f"{r['customer'] or ''} 差额 ¥{int(r['diff_cents'] or 0) / 100:.2f}",
                "status": r["status"],
            }
        )
    for r in arrivals:
        events.append(
            {
                "type": "pickup_arrived",
                "at": _iso(r["pickup_arrived_at"]),
                "order_id": str(r["id"]),
                "title": "顾客已到店自提",
                "detail": r["customer"] or "",
                "status": "pickup_arrived",
            }
        )
    events.sort(key=lambda e: e["at"] or "", reverse=True)
    return {
        "server_time": datetime.now(TZ).isoformat(),
        "after": since.isoformat(),
        "events": events[:limit],
    }
