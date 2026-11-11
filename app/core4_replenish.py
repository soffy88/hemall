"""Hemall Core4 智能补货 — 补货建议算法、采购单草稿、CSV 导出。

算法口径（BRIEF / INVENTORY.md 对齐）：
- 销量窗口：近 7 天 / 近 14 天日均销量（按件或克折件）
- 在库：active 批次可用库存 (stock_qty - reserved_qty)
- 在途：已下单未到货的采购单行汇总
- 保质期：shelf_life_days，建议补货天数不超过保质期的 60%（留足销售周期）
- 星期效应：近 8 周同星期销量均值 / 整体日均销量，得到 weekday_factor
- 目标库存天数：可配（默认 7 天），建议补货量 = max(0, 目标库存 - 在库 - 在途)
- 成本参考：优先批次 cost_price_cents，其次 variant.reference_price_cents * 0.6 估算
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .deps import assert_staff_role, get_current_user
from .store_ops import _csv_response

router = APIRouter(prefix="/admin/replenish", tags=["replenish"])

TZ = "Asia/Shanghai"

# 可配参数键（存 runtime_setting）
REPLENISH_SETTING_KEY = "replenish.settings.v1"

DEFAULT_SETTINGS = {
    "target_days": 7,           # 目标库存天数
    "min_order_qty": 1,         # 最小起订量（件）
    "max_shelf_life_pct": 0.6,  # 补货不超过保质期的比例
    "weekday_lookback_weeks": 8,  # 星期效应回望周数
    "sales_windows": [7, 14],   # 销量窗口（天）
    "cost_fallback_pct": 0.6,   # 无成本时用参考价 * 此比例估算成本
}


def _require_admin(request: Request, principal: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return assert_staff_role(principal, {"admin", "support"})


async def _get_settings(pool: Any) -> dict[str, Any]:
    """读取补货配置（runtime_setting 表）。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM runtime_setting WHERE key = $1",
            REPLENISH_SETTING_KEY,
        )
    if row and row["value"]:
        try:
            return {**DEFAULT_SETTINGS, **row["value"]}
        except Exception:
            pass
    return DEFAULT_SETTINGS


async def _save_settings(pool: Any, settings: dict[str, Any], operator_id: str) -> None:
    """保存补货配置。"""
    import json
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO runtime_setting (key, value, updated_by, updated_at)
            VALUES ($1, $2::jsonb, $3, NOW())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = NOW()
            """,
            REPLENISH_SETTING_KEY,
            json.dumps(settings, ensure_ascii=False),
            operator_id,
        )


def _num(v: Any) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


async def _compute_replenish_suggestions(pool: Any, settings: dict[str, Any]) -> list[dict[str, Any]]:
    """核心算法：计算每个变体的补货建议。"""
    target_days = settings.get("target_days", 7)
    max_shelf_life_pct = settings.get("max_shelf_life_pct", 0.6)
    weekday_lookback_weeks = settings.get("weekday_lookback_weeks", 8)
    sales_windows = settings.get("sales_windows", [7, 14])
    cost_fallback_pct = settings.get("cost_fallback_pct", 0.6)
    min_order_qty = settings.get("min_order_qty", 1)

    async with pool.acquire() as conn:
        # 1. 取所有在售变体（有批次、非测试、有库存或有销量）
        variants = await conn.fetch(
            f"""
            SELECT pv.id AS variant_id, pv.sku_code, pv.pricing_mode, pv.reference_price_cents,
                   pv.price_basis_grams, pv.weight_options, pv.stock_unit,
                   p.title AS product_name, p.id AS product_id,
                   COALESCE((
                       SELECT jsonb_agg(jsonb_build_object(
                           'id', ib.id, 'stock_qty', ib.stock_qty, 'reserved_qty', ib.reserved_qty,
                           'cost_price_cents', ib.cost_price_cents, 'shelf_life_days', ib.shelf_life_days
                       ))
                       FROM inventory_batch ib
                       WHERE ib.variant_id = pv.id AND ib.status = 'active' AND ib.deleted_at IS NULL
                   ), '[]'::jsonb) AS batches
            FROM product_variant pv
            JOIN product p ON p.id = pv.product_id
            WHERE pv.deleted_at IS NULL AND p.deleted_at IS NULL AND pv.status = 'active'
            """
        )

        # 2. 近 7/14 天销量（按件/克聚合）
        sales_7d: dict[str, dict] = defaultdict(lambda: {"qty": 0, "orders": 0})
        sales_14d: dict[str, dict] = defaultdict(lambda: {"qty": 0, "orders": 0})
        # 近 8 周同星期销量（用于 weekday_factor）
        weekday_sales: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))

        for window in sales_windows:
            cutoff = f"NOW() - INTERVAL '{window} days'"
            rows = await conn.fetch(
                f"""
                SELECT oli.variant_id,
                       SUM(CASE WHEN oli.pricing_mode = 'weight'
                            THEN COALESCE(oli.actual_weight_grams, oli.estimated_weight_grams, 0)
                            ELSE oli.quantity END)::bigint AS total_qty,
                       COUNT(DISTINCT oli.order_id)::int AS order_count
                FROM order_line_item oli
                JOIN customer_order o ON o.id = oli.order_id
                WHERE oli.preorder_qty = 0
                  AND o.status NOT IN ('cancelled', 'failed', 'draft')
                  AND o.created_at >= {cutoff}
                  AND NOT EXISTS (
                      SELECT 1 FROM customer c WHERE c.id = o.customer_id AND c.excluded_from_reports
                  )
                GROUP BY oli.variant_id
                """
            )
            target = sales_7d if window == 7 else sales_14d
            for r in rows:
                vid = str(r["variant_id"])
                target[vid]["qty"] = _num(r["total_qty"])
                target[vid]["orders"] = _num(r["order_count"])

        # 3. 近 8 周同星期销量（按 ISO 周一=1...周日=7）
        for w in range(weekday_lookback_weeks):
            start = f"NOW() - INTERVAL '{w * 7 + 7} days'"
            end = f"NOW() - INTERVAL '{w * 7} days'"
            rows = await conn.fetch(
                f"""
                SELECT oli.variant_id,
                       EXTRACT(ISODOW FROM o.created_at AT TIME ZONE '{TZ}')::int AS dow,
                       SUM(CASE WHEN oli.pricing_mode = 'weight'
                            THEN COALESCE(oli.actual_weight_grams, oli.estimated_weight_grams, 0)
                            ELSE oli.quantity END)::bigint AS total_qty
                FROM order_line_item oli
                JOIN customer_order o ON o.id = oli.order_id
                WHERE oli.preorder_qty = 0
                  AND o.status NOT IN ('cancelled', 'failed', 'draft')
                  AND o.created_at >= {start} AND o.created_at < {end}
                  AND NOT EXISTS (
                      SELECT 1 FROM customer c WHERE c.id = o.customer_id AND c.excluded_from_reports
                  )
                GROUP BY oli.variant_id, dow
                """
            )
            for r in rows:
                vid = str(r["variant_id"])
                dow = _num(r["dow"])
                weekday_sales[vid][dow].append(_num(r["total_qty"]))

        # 4. 在途采购单（已确认未收货）
        in_transit = await conn.fetch(
            """
            SELECT variant_id, SUM(suggested_qty)::int AS qty
            FROM core4_purchase_order_line pol
            JOIN core4_purchase_order po ON po.id = pol.po_id
            WHERE po.status IN ('confirmed')
            GROUP BY variant_id
            """
        )
        in_transit_map = {str(r["variant_id"]): _num(r["qty"]) for r in in_transit}

    # 5. 计算每个变体的建议
    suggestions = []
    today = datetime.now().date()
    # 当前星期（1=周一...7=周日）
    current_dow = today.isoweekday()

    for v in variants:
        vid = str(v["variant_id"])
        batches = v["batches"] or []

        # 在库可用量
        stock_qty = sum(_num(b.get("stock_qty")) - _num(b.get("reserved_qty")) for b in batches)
        stock_qty = max(0, stock_qty)

        # 在途
        transit_qty = in_transit_map.get(vid, 0)

        # 成本参考：取最新批次成本，无则用参考价 * fallback
        cost_price = None
        for b in sorted(batches, key=lambda x: x.get("id", ""), reverse=True):
            if b.get("cost_price_cents"):
                cost_price = _num(b["cost_price_cents"])
                break
        if cost_price is None or cost_price == 0:
            cost_price = int(_num(v["reference_price_cents"]) * cost_fallback_pct)

        # 保质期：取最小 shelf_life_days（最严格）
        shelf_life_days = None
        for b in batches:
            sl = b.get("shelf_life_days")
            if sl and _num(sl) > 0:
                if shelf_life_days is None or _num(sl) < shelf_life_days:
                    shelf_life_days = _num(sl)

        # 7/14 天日均销量
        avg_7d = sales_7d[vid]["qty"] / 7.0 if sales_7d[vid]["qty"] > 0 else 0
        avg_14d = sales_14d[vid]["qty"] / 14.0 if sales_14d[vid]["qty"] > 0 else 0
        # 取较大者作为基准日均（应对新品/促销）
        base_daily = max(avg_7d, avg_14d)

        # 星期效应因子：当前星期的历史均值 / 整体日均
        weekday_factor = 1.0
        if vid in weekday_sales and current_dow in weekday_sales[vid]:
            dow_vals = weekday_sales[vid][current_dow]
            if dow_vals:
                dow_avg = sum(dow_vals) / len(dow_vals)
                if base_daily > 0:
                    weekday_factor = dow_avg / base_daily
                    # 限幅 [0.5, 2.0] 防异常
                    weekday_factor = max(0.5, min(2.0, weekday_factor))

        # 目标库存 = 日均 * 星期因子 * 目标天数
        target_stock = base_daily * weekday_factor * target_days

        # 保质期约束：补货量不应导致库存超过保质期 * max_shelf_life_pct 的销量
        if shelf_life_days and shelf_life_days > 0:
            max_stock_by_shelf = base_daily * shelf_life_days * max_shelf_life_pct
            target_stock = min(target_stock, max_stock_by_shelf)

        # 建议补货量
        needed = max(0, int(target_stock + 0.5) - stock_qty - transit_qty)
        # 单位
        unit = "g" if (v["pricing_mode"] == "weight" or v.get("stock_unit") == "gram") else "件"
        # 最小起订量对齐
        if needed > 0 and needed < min_order_qty:
            needed = min_order_qty

        if needed > 0:
            suggestions.append({
                "variant_id": vid,
                "sku_code": v["sku_code"] or "",
                "product_name": v["product_name"] or "",
                "unit": unit,
                "stock_qty": stock_qty,
                "transit_qty": transit_qty,
                "avg_daily_7d": round(avg_7d, 2),
                "avg_daily_14d": round(avg_14d, 2),
                "base_daily": round(base_daily, 2),
                "weekday_factor": round(weekday_factor, 2),
                "target_days": target_days,
                "target_stock": int(target_stock + 0.5),
                "suggested_qty": needed,
                "cost_price_cents": cost_price,
                "estimated_amount_cents": needed * cost_price,
                "shelf_life_days": shelf_life_days,
            })

    # 按估算金额降序
    suggestions.sort(key=lambda x: x["estimated_amount_cents"], reverse=True)
    return suggestions


# ============ API 端点 ============


class ReplenishSettings(BaseModel):
    target_days: int = Field(default=7, ge=1, le=30)
    min_order_qty: int = Field(default=1, ge=1)
    max_shelf_life_pct: float = Field(default=0.6, ge=0.1, le=1.0)
    weekday_lookback_weeks: int = Field(default=8, ge=1, le=26)
    sales_windows: list[int] = Field(default=[7, 14])
    cost_fallback_pct: float = Field(default=0.6, ge=0.1, le=1.0)


@router.get("/settings")
async def get_replenish_settings(request: Request, _=Depends(_require_admin)):
    """获取补货配置。"""
    pool = request.app.state.pool
    settings = await _get_settings(pool)
    return {"settings": settings}


@router.put("/settings")
async def update_replenish_settings(
    request: Request,
    body: ReplenishSettings,
    principal: dict[str, Any] = Depends(_require_admin),
):
    """更新补货配置。"""
    pool = request.app.state.pool
    settings = body.model_dump()
    await _save_settings(pool, settings, str(principal.get("user_id") or ""))
    return {"ok": True, "settings": settings}


@router.get("/suggestions")
async def get_replenish_suggestions(
    request: Request,
    _=Depends(_require_admin),
):
    """获取补货建议列表。"""
    pool = request.app.state.pool
    settings = await _get_settings(pool)
    suggestions = await _compute_replenish_suggestions(pool, settings)
    return {"generated_at": datetime.now().isoformat(), "count": len(suggestions), "suggestions": suggestions}


@router.get("/suggestions.csv")
async def export_replenish_suggestions_csv(
    request: Request,
    _=Depends(_require_admin),
):
    """导出补货建议 CSV。"""
    pool = request.app.state.pool
    settings = await _get_settings(pool)
    suggestions = await _compute_replenish_suggestions(pool, settings)

    header = [
        "SKU",
        "商品名称",
        "单位",
        "在库可用量",
        "在途量",
        "7日日均",
        "14日日均",
        "基准日均",
        "星期因子",
        "目标天数",
        "目标库存",
        "建议补货量",
        "成本价(分)",
        "预估金额(分)",
        "保质期(天)",
    ]
    rows = [
        [
            s["sku_code"],
            s["product_name"],
            s["unit"],
            s["stock_qty"],
            s["transit_qty"],
            s["avg_daily_7d"],
            s["avg_daily_14d"],
            s["base_daily"],
            s["weekday_factor"],
            s["target_days"],
            s["target_stock"],
            s["suggested_qty"],
            s["cost_price_cents"],
            s["estimated_amount_cents"],
            s["shelf_life_days"] if s["shelf_life_days"] else "",
        ]
        for s in suggestions
    ]
    filename = f"replenish-suggestions-{date.today().isoformat()}.csv"
    return _csv_response(filename, header, rows)


class CreatePORequest(BaseModel):
    """从建议创建采购单草稿。"""
    supplier_name: str = Field(min_length=1)
    supplier_contact: str = ""
    expected_arrival: str | None = None  # YYYY-MM-DD
    note: str = ""
    variant_ids: list[str] = Field(default_factory=list)  # 为空则全选


@router.post("/purchase-orders")
async def create_purchase_order(
    request: Request,
    body: CreatePORequest,
    principal: dict[str, Any] = Depends(_require_admin),
):
    """根据补货建议创建采购单草稿。"""
    pool = request.app.state.pool
    settings = await _get_settings(pool)
    suggestions = await _compute_replenish_suggestions(pool, settings)

    # 筛选
    if body.variant_ids:
        variant_set = set(body.variant_ids)
        suggestions = [s for s in suggestions if s["variant_id"] in variant_set]

    if not suggestions:
        raise HTTPException(400, "no suggestions to create PO")

    expected_arrival = None
    if body.expected_arrival:
        try:
            expected_arrival = date.fromisoformat(body.expected_arrival)
        except ValueError:
            raise HTTPException(400, "expected_arrival must be YYYY-MM-DD")

    operator_id = str(principal.get("user_id") or "")

    async with pool.acquire() as conn:
        async with conn.transaction():
            # 创建采购单主表
            total_amount = sum(s["estimated_amount_cents"] for s in suggestions)
            po_id = await conn.fetchval(
                """
                INSERT INTO core4_purchase_order
                    (supplier_name, supplier_contact, expected_arrival, total_amount_cents, note, created_by)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                body.supplier_name,
                body.supplier_contact,
                expected_arrival,
                total_amount,
                body.note,
                operator_id,
            )

            # 创建行
            for s in suggestions:
                await conn.execute(
                    """
                    INSERT INTO core4_purchase_order_line
                        (po_id, variant_id, sku_code, product_name, suggested_qty, unit, unit_cost_cents, note)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    po_id,
                    s["variant_id"],
                    s["sku_code"],
                    s["product_name"],
                    s["suggested_qty"],
                    s["unit"],
                    s["cost_price_cents"],
                    f"base_daily={s['base_daily']}, weekday_factor={s['weekday_factor']}",
                )

    return {
        "po_id": str(po_id),
        "supplier_name": body.supplier_name,
        "line_count": len(suggestions),
        "total_amount_cents": total_amount,
        "status": "draft",
    }


@router.get("/purchase-orders")
async def list_purchase_orders(
    request: Request,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _=Depends(_require_admin),
):
    """采购单列表。"""
    pool = request.app.state.pool
    where = ""
    params: list[Any] = []
    if status:
        where = "WHERE po.status = $1"
        params.append(status)
    params.extend([limit, offset])

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT po.id, po.supplier_name, po.supplier_contact, po.status,
                   po.expected_arrival, po.total_amount_cents, po.note,
                   po.created_by, po.created_at, po.updated_at,
                   COUNT(pol.id)::int AS line_count
            FROM core4_purchase_order po
            LEFT JOIN core4_purchase_order_line pol ON pol.po_id = po.id
            {where}
            GROUP BY po.id
            ORDER BY po.created_at DESC
            LIMIT ${len(params) - 1} OFFSET ${len(params)}
            """,
            *params,
        )
    return [_row_to_dict(r) for r in rows]


@router.get("/purchase-orders/{po_id}")
async def get_purchase_order(
    po_id: str,
    request: Request,
    _=Depends(_require_admin),
):
    """采购单详情（含行）。"""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        po = await conn.fetchrow("SELECT * FROM core4_purchase_order WHERE id = $1", po_id)
        if not po:
            raise HTTPException(404, "purchase order not found")
        lines = await conn.fetch(
            "SELECT * FROM core4_purchase_order_line WHERE po_id = $1 ORDER BY created_at",
            po_id,
        )
    return {
        **_row_to_dict(po),
        "lines": [_row_to_dict(l) for l in lines],
    }


class UpdatePOStatusRequest(BaseModel):
    status: str = Field(pattern="^(confirmed|received|cancelled)$")
    note: str = ""


@router.patch("/purchase-orders/{po_id}/status")
async def update_purchase_order_status(
    po_id: str,
    request: Request,
    body: UpdatePOStatusRequest,
    principal: dict[str, Any] = Depends(_require_admin),
):
    """更新采购单状态（确认/收货/取消）。收货时回写入库（生成批次 + stock_movement + store_inventory_entry）。"""
    pool = request.app.state.pool
    operator_id = str(principal.get("user_id") or "")

    async with pool.acquire() as conn:
        async with conn.transaction():
            po = await conn.fetchrow(
                "SELECT * FROM core4_purchase_order WHERE id = $1 FOR UPDATE", po_id
            )
            if not po:
                raise HTTPException(404, "purchase order not found")

            if po["status"] == body.status:
                return {"po_id": po_id, "status": po["status"], "changed": False}

            # 状态流转校验
            valid_transitions = {
                "draft": ["confirmed", "cancelled"],
                "confirmed": ["received", "cancelled"],
                "received": [],
                "cancelled": [],
            }
            if body.status not in valid_transitions.get(po["status"], []):
                raise HTTPException(400, f"invalid transition from {po['status']} to {body.status}")

            # 收货：生成批次 + 入库记录
            if body.status == "received":
                lines = await conn.fetch(
                    "SELECT * FROM core4_purchase_order_line WHERE po_id = $1", po_id
                )
                for line in lines:
                    # 找或建批次
                    batch_id = await conn.fetchval(
                        """
                        SELECT id FROM inventory_batch
                        WHERE variant_id = $1 AND status = 'active' AND deleted_at IS NULL
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        line["variant_id"],
                    )
                    if batch_id is None:
                        # 需要 location_id
                        loc_id = await conn.fetchval(
                            "SELECT id FROM stock_location WHERE status = 'active' ORDER BY created_at LIMIT 1"
                        )
                        if loc_id is None:
                            raise HTTPException(500, "no active stock location")
                        batch_id = await conn.fetchval(
                            """
                            INSERT INTO inventory_batch
                                (id, batch_no, variant_id, location_id, cost_price_cents, retail_price_cents,
                                 stock_qty, inspection_status, status)
                            VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, 0, 'pending', 'active')
                            RETURNING id
                            """,
                            f"PO-{po_id}-{line['sku_code']}-{datetime.now():%Y%m%d%H%M%S}",
                            line["variant_id"],
                            loc_id,
                            line["unit_cost_cents"],
                            line["unit_cost_cents"],  # 暂用成本价作零售价，后台再改
                        )

                    # 入库
                    await conn.execute(
                        "UPDATE inventory_batch SET stock_qty = stock_qty + $1, updated_at = NOW() WHERE id = $2",
                        line["suggested_qty"],
                        batch_id,
                    )
                    # stock_movement
                    await conn.execute(
                        """
                        INSERT INTO stock_movement
                            (product_id, variant_id, location_id, batch_id, movement_type,
                             quantity, unit_cost, reference_type, reference_id, reason, operator_id)
                        SELECT p.id, $1, ib.location_id, $2, 'receipt', $3, $4, 'purchase_order', $5, $6, $7
                        FROM product_variant pv
                        JOIN product p ON p.id = pv.product_id
                        JOIN inventory_batch ib ON ib.id = $2
                        WHERE pv.id = $1
                        """,
                        line["variant_id"],
                        batch_id,
                        line["suggested_qty"],
                        line["unit_cost_cents"] / 100.0,
                        po_id,
                        f"采购单收货 {po['supplier_name']}",
                        operator_id,
                    )
                    # store_inventory_entry
                    unit = line["unit"]
                    await conn.execute(
                        """
                        INSERT INTO store_inventory_entry
                            (variant_id, product_id, sku_code, product_name, entry_type,
                             qty, unit, unit_cost_cents, supplier, reason, note, operator_id)
                        SELECT pv.id, p.id, pv.sku_code, p.title, 'in', $2, $3, $4, $5, $6, $7, $8
                        FROM product_variant pv JOIN product p ON p.id = pv.product_id
                        WHERE pv.id = $1
                        """,
                        line["variant_id"],
                        line["suggested_qty"],
                        unit,
                        line["unit_cost_cents"],
                        po["supplier_name"],
                        f"采购单收货 {po_id}",
                        body.note,
                        operator_id,
                    )

            await conn.execute(
                "UPDATE core4_purchase_order SET status = $1, note = COALESCE($2, note), updated_at = NOW() WHERE id = $3",
                body.status,
                body.note or None,
                po_id,
            )

    return {"po_id": po_id, "status": body.status, "changed": True}


@router.get("/purchase-orders/{po_id}.csv")
async def export_purchase_order_csv(
    po_id: str,
    request: Request,
    _=Depends(_require_admin),
):
    """导出采购单 CSV。"""
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        po = await conn.fetchrow("SELECT * FROM core4_purchase_order WHERE id = $1", po_id)
        if not po:
            raise HTTPException(404, "purchase order not found")
        lines = await conn.fetch(
            "SELECT * FROM core4_purchase_order_line WHERE po_id = $1 ORDER BY created_at", po_id
        )

    header = ["SKU", "商品名称", "单位", "建议数量", "成本价(分)", "行金额(分)", "备注"]
    rows = [
        [
            l["sku_code"],
            l["product_name"],
            l["unit"],
            l["suggested_qty"],
            l["unit_cost_cents"],
            l["suggested_qty"] * l["unit_cost_cents"],
            l["note"],
        ]
        for l in lines
    ]
    # 头部加采购单基本信息
    info_rows = [
        ["采购单号", str(po["id"])],
        ["供应商", po["supplier_name"]],
        ["联系方式", po["supplier_contact"]],
        ["状态", po["status"]],
        ["预计到货", po["expected_arrival"].isoformat() if po["expected_arrival"] else ""],
        ["总金额(分)", po["total_amount_cents"]],
        ["备注", po["note"]],
        ["创建人", po["created_by"]],
        ["创建时间", po["created_at"].isoformat()],
        ["", ""],  # 空行分隔
    ]
    all_rows = info_rows + [header] + rows
    filename = f"PO-{po_id}-{date.today().isoformat()}.csv"
    return _csv_response(filename, ["字段", "值"], all_rows)


def _row_to_dict(row: Any) -> dict:
    if row is None:
        return {}
    d = dict(row)
    for k, v in d.items():
        if hasattr(v, "hex"):
            d[k] = str(v)
        elif hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


from fastapi import HTTPException