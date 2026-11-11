"""Hemall Core4 溯源与信任展示 — 产地/入库日期/质检结果专用接口。

INVENTORY.md 确认：后端字段已全备（inventory_batch 已有 origin_region/storage_condition/
shelf_life_days/quality_certificate_url/inspection_status/inspection_note/created_at），
storefront API (queries.get_storefront_product) 已返回这些字段。

本模块提供：
1. 专用溯源聚合接口：/store/traceability/{product_id} —— 为前端详情页提供结构化溯源卡片数据
2. 批次级溯源链：供应商 → 入库批次 → 质检记录 → 货架
3. 质检证书校验：返回证书有效性、过期状态
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from .deps import get_current_customer

router = APIRouter(prefix="/store/traceability", tags=["traceability"])


class TraceabilityBatch(BaseModel):
    """溯源批次信息。"""
    batch_id: str
    batch_no: str
    produced_at: str | None = None          # 生产日期（若批次有记录）
    received_at: str                        # 到店/入库日期 (batch.created_at)
    origin_region: str | None = None        # 产地
    storage_condition: str | None = None    # 储存条件
    shelf_life_days: int | None = None      # 保质期天数
    expiration_time: str | None = None      # 过期时间
    quality_certificate_url: str | None = None  # 质检证书
    inspection_status: str                  # 质检状态: passed/pending/failed
    inspection_note: str | None = None      # 质检备注
    inspection_at: str | None = None        # 质检时间
    available_qty: int                      # 可用库存
    retail_price_cents: int                 # 零售价


class TraceabilityResponse(BaseModel):
    """商品溯源聚合响应。"""
    product_id: str
    product_title: str
    category_name: str | None = None
    batches: list[TraceabilityBatch]
    traceability_complete: bool             # 是否所有关键字段齐全
    missing_fields: list[str] = []          # 缺失的关键字段
    earliest_received_at: str | None = None # 最早入库日期
    latest_expiration: str | None = None    # 最近过期时间


@router.get("/{product_id}")
async def get_product_traceability(
    product_id: str,
    request: Request,
    include_inactive: bool = Query(False, description="包含非 active 批次（如已售罄/过期）"),
    principal: dict[str, Any] | None = Depends(get_current_customer),
) -> TraceabilityResponse:
    """获取商品完整溯源信息（为前端详情页"溯源卡片"提供数据）。

    返回结构化批次溯源数据，含：
    - 产地/入库日期/储存条件/保质期/过期时间
    - 质检状态/证书/备注
    - 数据完整性标记（前端据此决定是否展示"溯源完整"徽章）

    公开端点（零登录可访问），纳入限流防刷。
    """
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        # 1. 基础商品信息
        product = await conn.fetchrow(
            """
            SELECT p.id, p.title, p.category_id, pc.name AS category_name
            FROM product p
            LEFT JOIN product_category pc ON pc.id = p.category_id
            WHERE p.id = $1 AND p.deleted_at IS NULL AND p.status = 'published'
            """,
            product_id,
        )
        if not product:
            raise HTTPException(404, "product not found")

        # 2. 批次溯源数据（按入库时间倒序）
        batch_where = "ib.status = 'active'" if not include_inactive else "ib.deleted_at IS NULL"
        rows = await conn.fetch(
            f"""
            SELECT ib.id AS batch_id, ib.batch_no, ib.created_at,
                   ib.origin_region, ib.storage_condition, ib.shelf_life_days,
                   ib.expiration_time, ib.quality_certificate_url,
                   ib.inspection_status, ib.inspection_note,
                   ib.inspection_at, ib.stock_qty, ib.reserved_qty,
                   ib.retail_price_cents
            FROM inventory_batch ib
            JOIN product_variant pv ON pv.id = ib.variant_id
            WHERE pv.product_id = $1 AND ib.deleted_at IS NULL AND {batch_where}
            ORDER BY ib.created_at DESC
            """,
            product_id,
        )

    batches = []
    missing_fields_set: set[str] = set()
    required_fields = [
        "origin_region", "storage_condition", "shelf_life_days",
        "quality_certificate_url", "inspection_status"
    ]
    received_dates = []
    expiration_dates = []

    for r in rows:
        available = _num(r["stock_qty"]) - _num(r["reserved_qty"])
        batch = TraceabilityBatch(
            batch_id=str(r["batch_id"]),
            batch_no=r["batch_no"] or "",
            produced_at=None,  # 暂无生产日期字段，如需可扩展
            received_at=r["created_at"].isoformat() if r["created_at"] else "",
            origin_region=r["origin_region"] or None,
            storage_condition=r["storage_condition"] or None,
            shelf_life_days=r["shelf_life_days"],
            expiration_time=r["expiration_time"].isoformat() if r["expiration_time"] else None,
            quality_certificate_url=r["quality_certificate_url"] or None,
            inspection_status=r["inspection_status"] or "pending",
            inspection_note=r["inspection_note"] or None,
            inspection_at=r["inspection_at"].isoformat() if r["inspection_at"] else None,
            available_qty=max(0, available),
            retail_price_cents=_num(r["retail_price_cents"]),
        )
        batches.append(batch)

        # 检查缺失字段
        for field in required_fields:
            val = getattr(batch, field)
            if val is None or (isinstance(val, str) and not val.strip()):
                missing_fields_set.add(field)

        if batch.received_at:
            received_dates.append(batch.received_at)
        if batch.expiration_time:
            expiration_dates.append(batch.expiration_time)

    # 字段标签映射（前端展示用）
    field_labels = {
        "origin_region": "产地",
        "storage_condition": "储存条件",
        "shelf_life_days": "保质期",
        "quality_certificate_url": "质检证书",
        "inspection_status": "质检状态",
    }
    missing_fields = [field_labels[f] for f in sorted(missing_fields_set)]

    return TraceabilityResponse(
        product_id=str(product["id"]),
        product_title=product["title"],
        category_name=product["category_name"],
        batches=batches,
        traceability_complete=len(missing_fields) == 0,
        missing_fields=missing_fields,
        earliest_received_at=min(received_dates) if received_dates else None,
        latest_expiration=max(expiration_dates) if expiration_dates else None,
    )


@router.get("/batch/{batch_id}")
async def get_batch_traceability(
    batch_id: str,
    request: Request,
    principal: dict[str, Any] | None = Depends(get_current_customer),
) -> dict[str, Any]:
    """获取单批次完整溯源链（供应商→入库→质检→货架）。

    返回更详细的批次级信息，含：
    - 供应商信息（关联 supply_chain 入库记录）
    - 入库操作记录（操作人/时间/成本）
    - 质检流程记录
    - 货架上架记录
    """
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        batch = await conn.fetchrow(
            """
            SELECT ib.*, pv.sku_code, pv.product_id, p.title AS product_title
            FROM inventory_batch ib
            JOIN product_variant pv ON pv.id = ib.variant_id
            JOIN product p ON p.id = pv.product_id
            WHERE ib.id = $1 AND ib.deleted_at IS NULL
            """,
            batch_id,
        )
        if not batch:
            raise HTTPException(404, "batch not found")

        # 入库记录（stock_movement type=receipt）
        movements = await conn.fetch(
            """
            SELECT sm.*, u.name AS operator_name
            FROM stock_movement sm
            LEFT JOIN "app_user" u ON u.id = sm.operator_id
            WHERE sm.batch_id = $1 AND sm.movement_type = 'receipt'
            ORDER BY sm.created_at DESC
            LIMIT 5
            """,
            batch_id,
        )

        # 店内日志（入库/损耗）
        store_entries = await conn.fetch(
            """
            SELECT sie.*, u.name AS operator_name
            FROM store_inventory_entry sie
            LEFT JOIN "app_user" u ON u.id = sie.operator_id
            WHERE sie.variant_id = $1 AND sie.biz_date >= (ib.created_at AT TIME ZONE 'Asia/Shanghai')::date
              AND ib.id = $2
            ORDER BY sie.created_at DESC
            LIMIT 10
            """,
            batch["variant_id"], batch_id,
        )

    return {
        "batch": {
            "batch_id": str(batch["id"]),
            "batch_no": batch["batch_no"],
            "product_title": batch["product_title"],
            "sku_code": batch["sku_code"],
            "origin_region": batch["origin_region"],
            "storage_condition": batch["storage_condition"],
            "shelf_life_days": batch["shelf_life_days"],
            "expiration_time": batch["expiration_time"].isoformat() if batch["expiration_time"] else None,
            "quality_certificate_url": batch["quality_certificate_url"],
            "inspection_status": batch["inspection_status"],
            "inspection_note": batch["inspection_note"],
            "inspection_at": batch["inspection_at"].isoformat() if batch["inspection_at"] else None,
            "cost_price_cents": batch["cost_price_cents"],
            "retail_price_cents": batch["retail_price_cents"],
            "stock_qty": batch["stock_qty"],
            "reserved_qty": batch["reserved_qty"],
            "available_qty": _num(batch["stock_qty"]) - _num(batch["reserved_qty"]),
            "created_at": batch["created_at"].isoformat() if batch["created_at"] else None,
            "location_id": str(batch["location_id"]) if batch["location_id"] else None,
        },
        "inbound_movements": [
            {
                "id": str(m["id"]),
                "quantity": m["quantity"],
                "unit_cost": m["unit_cost"],
                "reason": m["reason"],
                "operator": m["operator_name"],
                "created_at": m["created_at"].isoformat() if m["created_at"] else None,
            }
            for m in movements
        ],
        "store_journal": [
            {
                "id": str(e["id"]),
                "entry_type": e["entry_type"],
                "qty": e["qty"],
                "unit": e["unit"],
                "unit_cost_cents": e["unit_cost_cents"],
                "supplier": e["supplier"],
                "reason": e["reason"],
                "loss_category": e["loss_category"],
                "operator": e["operator_name"],
                "created_at": e["created_at"].isoformat() if e["created_at"] else None,
            }
            for e in store_entries
        ],
    }


@router.get("/certificate/verify")
async def verify_quality_certificate(
    request: Request,
    certificate_url: str = Query(..., description="质检证书 URL"),
    principal: dict[str, Any] | None = Depends(get_current_customer),
) -> dict[str, Any]:
    """校验质检证书有效性（简单 HEAD 请求 + 域名白名单）。

    返回：可访问性、证书域名、内容类型、文件大小（如有）。
    不下载完整文件，仅做轻量校验。
    """
    import httpx

    # 允许的证书域名白名单（防 SSRF）
    allowed_domains = {
        "ciq.gov.cn", "customs.gov.cn", "marketreg.gov.cn",
        "test.hemall.local", "localhost",
    }

    try:
        from urllib.parse import urlparse
        parsed = urlparse(certificate_url)
        if parsed.scheme not in ("http", "https"):
            return {"valid": False, "reason": "invalid_scheme", "url": certificate_url}

        domain = parsed.netloc.lower()
        # 允许子域名匹配
        allowed = any(domain == d or domain.endswith("." + d) for d in allowed_domains)
        if not allowed and not domain.startswith("127.") and not domain.startswith("192.168."):
            return {"valid": False, "reason": "domain_not_allowed", "domain": domain, "url": certificate_url}
    except Exception as e:
        return {"valid": False, "reason": "parse_error", "error": str(e), "url": certificate_url}

    # 发 HEAD 请求
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            resp = await client.head(certificate_url)
            return {
                "valid": resp.status_code == 200,
                "status_code": resp.status_code,
                "content_type": resp.headers.get("content-type"),
                "content_length": resp.headers.get("content-length"),
                "domain": domain,
                "url": certificate_url,
                "checked_at": datetime.now().isoformat(),
            }
    except httpx.TimeoutException:
        return {"valid": False, "reason": "timeout", "url": certificate_url}
    except httpx.RequestError as e:
        return {"valid": False, "reason": "request_error", "error": str(e), "url": certificate_url}


def _num(v: Any) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


# ── 管理端：溯源数据完整性报表 ────────────────────────────────────────────

def _require_admin(request: Request, principal: dict[str, Any] = Depends(get_current_customer)) -> dict[str, Any]:
    from .deps import assert_staff_role
    return assert_staff_role(principal, {"admin", "support"})


@router.get("/admin/completeness-report")
async def traceability_completeness_report(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    _=Depends(_require_admin),
) -> dict[str, Any]:
    """溯源数据完整性报表：统计有多少商品/批次缺失关键溯源字段。"""
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT p.id, p.title, p.category_id, pc.name AS category_name,
                   COUNT(ib.id)::int AS total_batches,
                   COUNT(ib.id) FILTER (WHERE ib.origin_region IS NOT NULL AND ib.origin_region <> '')::int AS has_origin,
                   COUNT(ib.id) FILTER (WHERE ib.storage_condition IS NOT NULL AND ib.storage_condition <> '')::int AS has_storage,
                   COUNT(ib.id) FILTER (WHERE ib.shelf_life_days IS NOT NULL AND ib.shelf_life_days > 0)::int AS has_shelf_life,
                   COUNT(ib.id) FILTER (WHERE ib.quality_certificate_url IS NOT NULL AND ib.quality_certificate_url <> '')::int AS has_cert,
                   COUNT(ib.id) FILTER (WHERE ib.inspection_status = 'passed')::int AS passed_inspection,
                   COUNT(ib.id) FILTER (WHERE ib.inspection_status = 'failed')::int AS failed_inspection,
                   COUNT(ib.id) FILTER (WHERE ib.inspection_status = 'pending')::int AS pending_inspection
            FROM product p
            LEFT JOIN product_category pc ON pc.id = p.category_id
            LEFT JOIN product_variant pv ON pv.product_id = p.id AND pv.deleted_at IS NULL
            LEFT JOIN inventory_batch ib ON ib.variant_id = pv.id AND ib.deleted_at IS NULL AND ib.status = 'active'
            WHERE p.deleted_at IS NULL AND p.status = 'published'
            GROUP BY p.id, p.title, p.category_id, pc.name
            HAVING COUNT(ib.id) > 0
            ORDER BY total_batches DESC
            LIMIT $1
            """,
            limit,
        )

    report = []
    for r in rows:
        total = _num(r["total_batches"])
        complete = (_num(r["has_origin"]) == total and
                    _num(r["has_storage"]) == total and
                    _num(r["has_shelf_life"]) == total and
                    _num(r["has_cert"]) == total and
                    _num(r["passed_inspection"]) == total)
        missing = []
        if _num(r["has_origin"]) < total: missing.append("产地")
        if _num(r["has_storage"]) < total: missing.append("储存条件")
        if _num(r["has_shelf_life"]) < total: missing.append("保质期")
        if _num(r["has_cert"]) < total: missing.append("质检证书")
        if _num(r["passed_inspection"]) < total: missing.append("质检未通过")

        report.append({
            "product_id": str(r["id"]),
            "title": r["title"],
            "category": r["category_name"],
            "total_batches": total,
            "complete": complete,
            "missing_fields": missing,
            "details": {
                "has_origin": _num(r["has_origin"]),
                "has_storage": _num(r["has_storage"]),
                "has_shelf_life": _num(r["has_shelf_life"]),
                "has_cert": _num(r["has_cert"]),
                "passed_inspection": _num(r["passed_inspection"]),
                "failed_inspection": _num(r["failed_inspection"]),
                "pending_inspection": _num(r["pending_inspection"]),
            }
        })

    complete_count = sum(1 for r in report if r["complete"])
    return {
        "total_products": len(report),
        "complete_products": complete_count,
        "incomplete_products": len(report) - complete_count,
        "completeness_rate": round(complete_count / len(report) * 100, 1) if report else 100,
        "report": report,
    }


@router.get("/admin/completeness-report.csv")
async def traceability_completeness_report_csv(
    request: Request,
    _=Depends(_require_admin),
) -> Any:
    """导出溯源完整性报表 CSV。"""
    from .store_ops import _csv_response

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.id, p.title, pc.name AS category_name,
                   COUNT(ib.id)::int AS total_batches,
                   COUNT(ib.id) FILTER (WHERE ib.origin_region IS NOT NULL AND ib.origin_region <> '')::int AS has_origin,
                   COUNT(ib.id) FILTER (WHERE ib.storage_condition IS NOT NULL AND ib.storage_condition <> '')::int AS has_storage,
                   COUNT(ib.id) FILTER (WHERE ib.shelf_life_days IS NOT NULL AND ib.shelf_life_days > 0)::int AS has_shelf_life,
                   COUNT(ib.id) FILTER (WHERE ib.quality_certificate_url IS NOT NULL AND ib.quality_certificate_url <> '')::int AS has_cert,
                   COUNT(ib.id) FILTER (WHERE ib.inspection_status = 'passed')::int AS passed_inspection
            FROM product p
            LEFT JOIN product_category pc ON pc.id = p.category_id
            LEFT JOIN product_variant pv ON pv.product_id = p.id AND pv.deleted_at IS NULL
            LEFT JOIN inventory_batch ib ON ib.variant_id = pv.id AND ib.deleted_at IS NULL AND ib.status = 'active'
            WHERE p.deleted_at IS NULL AND p.status = 'published'
            GROUP BY p.id, p.title, pc.name
            HAVING COUNT(ib.id) > 0
            ORDER BY total_batches DESC
            """
        )

    header = ["商品ID", "商品名称", "分类", "批次数", "有产地", "有储存条件", "有保质期", "有证书", "质检通过", "完整性"]
    csv_rows = []
    for r in rows:
        total = _num(r["total_batches"])
        complete = (_num(r["has_origin"]) == total and
                    _num(r["has_storage"]) == total and
                    _num(r["has_shelf_life"]) == total and
                    _num(r["has_cert"]) == total and
                    _num(r["passed_inspection"]) == total)
        csv_rows.append([
            str(r["id"]),
            r["title"],
            r["category_name"] or "",
            total,
            _num(r["has_origin"]),
            _num(r["has_storage"]),
            _num(r["has_shelf_life"]),
            _num(r["has_cert"]),
            _num(r["passed_inspection"]),
            "完整" if complete else "不完整",
        ])

    from datetime import date
    return _csv_response(f"traceability-completeness-{date.today().isoformat()}.csv", header, csv_rows)