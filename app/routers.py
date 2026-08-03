"""hemall 路由层 — 从 registry 声明式生成全部商务端点 + 挂鉴权路由 + 读层 + 商城。

每个 omodul → 一个 ``POST /<domain>/<omodul_name>`` 端点, 请求体即该 omodul 的
Input pydantic 模型 (FastAPI 自动生成 OpenAPI schema)。样板收敛在 respond 的
omodul_endpoint 工厂; 本文件只做"登记 + 个别端点的旁路接线"。

读层 (queries) 和商城编排层 (storefront) 分别在独立模块中定义，此处挂载。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import queries
from .auth import router as auth_router
from .deps import get_current_user
from .events import fire_event
from .ext.registry import all_endpoint_specs as ext_endpoint_specs
from .ext.feed_quant import build_feed_item, normalize_velocity
from .ext.agent_gateway import (
    discover_tools,
    execute_tool,
    ingest_media,
)
from .ext.oskill import (
    compute_batch_affinity_scores,
    find_nearest_location,
    rerank_feed_by_affinity,
)
from .registry import all_endpoint_specs
from .respond import omodul_endpoint
from .storefront import router as storefront_router


async def _fire_order_placed(result: dict[str, Any], request: Request) -> None:
    """complete_checkout 成功后的旁路: 派发 order.placed (SPEC §6 并发拉起履约/记账)。"""
    engine = getattr(request.app.state, "events", None)
    await fire_event(
        engine,
        "order.placed",
        {
            "order_id": result.get("order_id"),
            "grand_total_cents": result.get("grand_total_cents"),
        },
    )


# 需要旁路接线的端点 (omodul 名 → post_hook)。其余端点无旁路。
_POST_HOOKS: dict[str, Callable[[dict, Request], Any]] = {
    "complete_checkout": _fire_order_placed,
}


async def _fire_demand_aggregator_signal(
    result: dict[str, Any], request: Request
) -> None:
    """create_crowd_intent 达到阈值后的旁路: 拉起 demand_aggregator_engine 的
    信号扇出 (SPEC §5.1: "触发向下游产地的规模化采购 PO 通知")。
    """
    if not result.get("threshold_triggered"):
        return
    oservi = getattr(request.app.state, "ext_oservi", None)
    if oservi is None:
        return
    await oservi.demand_aggregator.dispatch(
        "crowd_intent.threshold_reached",
        {
            "variant_id": result.get("variant_id"),
            "pending_count": result.get("pending_count"),
        },
    )


async def _fire_batch_broadcast_signal(
    result: dict[str, Any], request: Request
) -> None:
    """create_inventory_batch 入库成功后的旁路: 拉起 batch_broadcast_engine
    推送底价实录 (SPEC §5.2: "批次视频上架"触发广播)。
    """
    oservi = getattr(request.app.state, "ext_oservi", None)
    if oservi is None:
        return
    await oservi.batch_broadcast.dispatch(
        "batch.listed",
        {
            "batch_id": result.get("batch_id"),
            "variant_id": result.get("variant_id"),
            "retail_price": result.get("retail_price"),
        },
    )


# app.ext 自己的旁路接线，跟 _POST_HOOKS 分开一张表——两套 omodul 系统里都有
# 同名的 "complete_checkout"，共用一张 post_hook 表会把 app.ext 的结账事件
# 意外接进原有商城域完全无关的 events 派发器 (纯属同名巧合，不是故意复用)。
_EXT_POST_HOOKS: dict[str, Callable[[dict, Request], Any]] = {
    "create_crowd_intent": _fire_demand_aggregator_signal,
    "create_inventory_batch": _fire_batch_broadcast_signal,
}


def _require_admin(request: Request) -> None:
    """轻量鉴权：要求 Authorization: Bearer <token> 头。复用 obase JWT 校验。"""
    from obase.crypto.util import CryptoUtil

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, "missing token")
    token = auth_header.removeprefix("Bearer ")
    cfg = request.app.state.config
    try:
        CryptoUtil.jwt_decode(
            token=token, secret=cfg.jwt_secret, algorithm=cfg.jwt_algorithm
        )
    except Exception:
        raise HTTPException(401, "invalid token")


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database not ready")
    return pool


# ── Admin 读端点 ─────────────────────────────────────────────────────────

admin_read_router = APIRouter(prefix="/admin", tags=["admin-read"])


@admin_read_router.get("/products")
async def admin_list_products(
    request: Request,
    _=Depends(_require_admin),
    status: str | None = None,
    search: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return await queries.list_products(
        _pool(request), status=status, search=search, limit=limit, offset=offset
    )


@admin_read_router.get("/products/{product_id}")
async def admin_get_product(
    product_id: str, request: Request, _=Depends(_require_admin)
):
    product = await queries.get_product(_pool(request), product_id)
    if product is None:
        raise HTTPException(404, "product not found")
    return product


@admin_read_router.get("/orders")
async def admin_list_orders(
    request: Request,
    _=Depends(_require_admin),
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return await queries.list_orders(
        _pool(request), status=status, limit=limit, offset=offset
    )


@admin_read_router.get("/orders/{order_id}")
async def admin_get_order(order_id: str, request: Request, _=Depends(_require_admin)):
    order = await queries.get_order(_pool(request), order_id)
    if order is None:
        raise HTTPException(404, "order not found")
    return order


@admin_read_router.get("/customers")
async def admin_list_customers(
    request: Request,
    _=Depends(_require_admin),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return await queries.list_customers(_pool(request), limit=limit, offset=offset)


@admin_read_router.get("/dashboard/kpis")
async def admin_dashboard_kpis(request: Request, _=Depends(_require_admin)):
    return await queries.get_dashboard_kpis(_pool(request))


@admin_read_router.get("/orders/{order_id}/fulfillments")
async def admin_list_fulfillments(
    order_id: str, request: Request, _=Depends(_require_admin)
):
    return await queries.list_fulfillments(_pool(request), order_id)


@admin_read_router.get("/discounts")
async def admin_list_discounts(request: Request, _=Depends(_require_admin)):
    return await queries.list_discounts(_pool(request))


@admin_read_router.get("/gift-cards")
async def admin_list_gift_cards(request: Request, _=Depends(_require_admin)):
    return await queries.list_gift_cards(_pool(request))


@admin_read_router.get("/returns")
async def admin_list_returns(
    request: Request, _=Depends(_require_admin), order_id: str | None = None
):
    return await queries.list_return_requests(_pool(request), order_id=order_id)


@admin_read_router.get("/swaps")
async def admin_list_swaps(
    request: Request, _=Depends(_require_admin), order_id: str | None = None
):
    return await queries.list_swaps(_pool(request), order_id=order_id)


@admin_read_router.get("/claims")
async def admin_list_claims(
    request: Request, _=Depends(_require_admin), order_id: str | None = None
):
    return await queries.list_claims(_pool(request), order_id=order_id)


@admin_read_router.get("/price-lists")
async def admin_list_price_lists(request: Request, _=Depends(_require_admin)):
    return await queries.list_price_lists(_pool(request))


@admin_read_router.get("/stock-locations")
async def admin_list_stock_locations(request: Request, _=Depends(_require_admin)):
    return await queries.list_stock_locations(_pool(request))


@admin_read_router.get("/sales-channels")
async def admin_list_sales_channels(request: Request, _=Depends(_require_admin)):
    return await queries.list_sales_channels(_pool(request))


@admin_read_router.get("/regions")
async def admin_list_regions(request: Request, _=Depends(_require_admin)):
    return await queries.list_regions_admin(_pool(request))


@admin_read_router.get("/tax-rates")
async def admin_list_tax_rates(
    request: Request, _=Depends(_require_admin), region_code: str | None = None
):
    return await queries.list_tax_rates(_pool(request), region_code=region_code)


@admin_read_router.get("/categories")
async def admin_list_categories(request: Request, _=Depends(_require_admin)):
    return await queries.list_product_categories(_pool(request))


@admin_read_router.get("/collections")
async def admin_list_collections(request: Request, _=Depends(_require_admin)):
    return await queries.list_product_collections(_pool(request))


@admin_read_router.get("/customer-groups")
async def admin_list_customer_groups(request: Request, _=Depends(_require_admin)):
    return await queries.list_customer_groups(_pool(request))


@admin_read_router.get("/customers/{customer_id}/addresses")
async def admin_list_customer_addresses(
    customer_id: str, request: Request, _=Depends(_require_admin)
):
    return await queries.list_customer_addresses(_pool(request), customer_id)


@admin_read_router.get("/users")
async def admin_list_users(request: Request, _=Depends(_require_admin)):
    return await queries.list_app_users(_pool(request))


@admin_read_router.get("/batch-jobs")
async def admin_list_batch_jobs(request: Request, _=Depends(_require_admin)):
    return await queries.list_batch_jobs(_pool(request))


@admin_read_router.get("/supply-chain/suppliers")
async def admin_list_ext_suppliers(request: Request, _=Depends(_require_admin)):
    return await queries.list_ext_suppliers(_pool(request))


@admin_read_router.get("/aftersales/rma-claims")
async def admin_list_ext_rma_claims(request: Request, _=Depends(_require_admin)):
    return await queries.list_ext_rma_claims(_pool(request))


@admin_read_router.get("/marketing/broadcast-logs")
async def admin_list_ext_broadcast_logs(request: Request, _=Depends(_require_admin)):
    return await queries.list_ext_broadcast_logs(_pool(request))


@admin_read_router.get("/marketing/price-benchmarks")
async def admin_list_ext_price_benchmarks(request: Request, _=Depends(_require_admin)):
    return await queries.list_ext_price_benchmarks(_pool(request))


@admin_read_router.get("/marketing/probe-logs")
async def admin_list_ext_probe_logs(request: Request, _=Depends(_require_admin)):
    return await queries.list_ext_probe_order_logs(_pool(request))


@admin_read_router.get("/growth/affiliate-contracts")
async def admin_list_ext_affiliate_contracts(
    request: Request, _=Depends(_require_admin)
):
    return await queries.list_ext_affiliate_contracts(_pool(request))


@admin_read_router.get("/growth/conversion-logs")
async def admin_list_ext_conversion_logs(request: Request, _=Depends(_require_admin)):
    return await queries.list_ext_douyin_conversion_logs(_pool(request))


# ── app.ext 手写端点 (不走声明式 registry) ──────────────────────────────────
# execute_ambient_intake_workflow 的 Input 有一个 bytes 字段，硬塞进
# "Input pydantic 模型直接当请求体 schema" 那套通用机制会有 JSON body 里
# bytes 语义不清的问题；submit_rma_claim 根本不是 omodul (是拉起
# oservi.autonomous_triage_engine 的信号入口)，两者都手写路由。统一改造收尾
# 后不再有共同的扩展前缀——三个端点分别落进各自域名 (supply-chain/
# marketing/aftersales)，不设 router 级别的公共 prefix。

ext_bespoke_router = APIRouter(tags=["ext-bespoke"])


def _ext_bespoke_output_dir(request: Request, op: str) -> Path:
    """跟 app/storefront._output_dir 同一个套路，就近建目录方便排障。"""
    base = Path(request.app.state.config.output_root)
    out = base / "ext_bespoke" / op
    out.mkdir(parents=True, exist_ok=True)
    return out


class _AmbientIntakeRequest(BaseModel):
    # 顶棚摄像头原始视频流在真实场景是二进制，这里的 HTTP 契约简化成纯文本
    # (ManualCVProvider 本来就是内存态占位实现，不真的解析视频内容)——
    # 编码成 bytes 只是为了满足 omodul.execute_ambient_intake_workflow 的
    # 签名，不代表这是生产环境真实视频流的传输协议 (那大概率是走对象存储
    # 预签名 URL 或 multipart 上传，不是塞进一个 JSON 字符串字段)。
    video_stream_text: str


@ext_bespoke_router.post("/supply-chain/execute_ambient_intake_workflow")
async def ext_execute_ambient_intake(
    body: _AmbientIntakeRequest,
    request: Request,
    _=Depends(_require_admin),
) -> JSONResponse:
    from .ext.omodul.execute_ambient_intake_workflow import (
        ExecuteAmbientIntakeWorkflowConfig,
        ExecuteAmbientIntakeWorkflowInput,
        execute_ambient_intake_workflow,
    )

    result = await execute_ambient_intake_workflow(
        ExecuteAmbientIntakeWorkflowConfig(),
        ExecuteAmbientIntakeWorkflowInput(video_stream=body.video_stream_text.encode()),
        _ext_bespoke_output_dir(request, "execute_ambient_intake_workflow"),
        pool=_pool(request),
    )
    status_code = 200 if result.get("status") == "completed" else 422
    return JSONResponse(status_code=status_code, content=jsonable_encoder(result))


class _RewardCrowdsourcedBenchmarkRequest(BaseModel):
    # receipt_image 在 omodul 契约里是 bytes (小票原始图片字节流)，理由跟
    # _AmbientIntakeRequest 一样：JSON body 里 bytes 语义不清，这里简化成
    # 纯文本再 .encode()，不代表生产环境真实图片上传走这个协议。
    customer_id: str
    receipt_image_text: str


@ext_bespoke_router.post("/marketing/reward_crowdsourced_benchmark_workflow")
async def ext_reward_crowdsourced_benchmark(
    body: _RewardCrowdsourcedBenchmarkRequest, request: Request
) -> JSONResponse:
    """公开端点 (顾客自助上传小票换算力金)：跟顾客侧其他自助操作同类，
    无需 admin token。"""
    from .ext.omodul.reward_crowdsourced_benchmark_workflow import (
        RewardCrowdsourcedBenchmarkWorkflowConfig,
        RewardCrowdsourcedBenchmarkWorkflowInput,
        reward_crowdsourced_benchmark_workflow,
    )

    result = await reward_crowdsourced_benchmark_workflow(
        RewardCrowdsourcedBenchmarkWorkflowConfig(),
        RewardCrowdsourcedBenchmarkWorkflowInput(
            customer_id=body.customer_id,
            receipt_image=body.receipt_image_text.encode(),
        ),
        _ext_bespoke_output_dir(request, "reward_crowdsourced_benchmark_workflow"),
        pool=_pool(request),
    )
    status_code = 200 if result.get("status") == "completed" else 422
    return JSONResponse(status_code=status_code, content=jsonable_encoder(result))


class _SubmitRmaClaimRequest(BaseModel):
    order_id: str
    batch_id: str
    user_id: str
    evidence_image_url: str
    user_trust_score: int
    route_risk: float = 0.0


@ext_bespoke_router.post("/aftersales/submit_rma_claim")
async def ext_submit_rma_claim(
    body: _SubmitRmaClaimRequest, request: Request
) -> JSONResponse:
    """公开端点 (顾客自助报案)：拉起 autonomous_triage_engine 的 on_signal
    分发，SPEC §5 描述的"注入 vlm_assess_damage → evaluate_claim_credibility
    → execute_liability_routing_workflow"全自动仲裁链路的 HTTP 入口。
    """
    oservi = getattr(request.app.state, "ext_oservi", None)
    if oservi is None:
        raise HTTPException(503, "ext background engines not ready")

    dispatch_result = await oservi.autonomous_triage.dispatch(
        "rma.claim_submitted", body.model_dump()
    )
    if dispatch_result["errors"]:
        return JSONResponse(status_code=422, content=jsonable_encoder(dispatch_result))
    inner = dispatch_result["results"][0] if dispatch_result["results"] else {}
    status_code = 200 if inner.get("status") == "completed" else 422
    return JSONResponse(status_code=status_code, content=jsonable_encoder(inner))


@ext_bespoke_router.get("/supply-chain/suppliers/lookup")
async def ext_lookup_supplier(wallet_account: str, request: Request):
    """公开端点 (供应商自助查询自己的入驻状态)：供应商目前没有真正的登录
    体系 (wallet_account 只是注册时自己填的自由字符串，不是账号凭据)，这是
    小型供应商门户"查询我的状态"页面的查询入口——只返回这一个 wallet_account
    自己的信誉分/质押余额/状态，不是 /admin/supply-chain/suppliers 那种需要
    admin token 的全量列表。
    """
    supplier = await queries.get_supplier_by_wallet(_pool(request), wallet_account)
    if supplier is None:
        raise HTTPException(404, "supplier not found")
    return supplier


# ── 补天计划 Task 2.1: 地理位置找货 Feed (Voronoi 网格前台化) ────────────────

#: get_nearby_feed 的安全货架期余量 (小时)。比过期提前 2 小时就不上架——即使
#: inventory_decay_engine 还没到下一个 tick，临期商品也不会流到顾客面前。
_NEARBY_FEED_SAFETY_MARGIN_HOURS = 2.0

#: 行为序列窗口 (天)：设备购买轨迹只取最近这些天的，旧购买不再影响 Feed。
_NEARBY_FEED_HISTORY_DAYS = 90
#: 行为序列最多取的商品数 (防单设备历史爆炸)。
_NEARBY_FEED_HISTORY_MAX_PRODUCTS = 20
#: 全局共现矩阵构建上限 (订单数)。全量订单可能很大，取最近窗口的样本就够
#: 算关联度 (冷启动阶段数据量小，上限够用；量大时按窗口截断)。
_NEARBY_FEED_COOCCURRENCE_ORDER_CAP = 2000


@ext_bespoke_router.get("/store/nearby-feed")
async def get_nearby_feed(
    request: Request,
    lat: float,
    lon: float,
    limit: int = Query(50, ge=1, le=100),
    customer_id: str | None = Query(None, description="已登录顾客 id (零登录可省略，省略时 user_system_balance=0)"),
):
    """位置 Feed 流 v9.0 (BFF 做市量化契约)：“人找货”到“地理位置找货”的彻底反转。

    Phase 9 升级：响应切到 ``location_context + feed_items`` 扁平数组——前端绝不
    拉取多余的富文本详情，后端直接把做市属性 (tag_type / benchmark_price /
    observed_velocity / affinity_boosted) 量化成标量字段，前端按 tag_type 在
    零层级大卡 (HeroCard) 和高密网格 (GridItem) 之间自动切换渲染引擎。

    tag_type 派生规则 (feed_quant.classify_feed_tag)：
      clearance: 现价比基准价直降 ≥ 40% 或 距过期 ≤ 12h (暴降大卡)
      fresh:     入库 ≤ 48h 且降幅 < 20% (溯源大卡)
      standard:  其余刚需生鲜 (基础网格)

    其余行为与旧版一致：最近 active 微仓 + 安全货架期过滤 + X-Device-Id
    行为序列加权 (协同过滤插队)。兼容旧契约字段 (nearest_location/batches)。

    公开端点 (零登录扫码即买)，纳入限流防刷保护。
    """
    pool = _pool(request)

    async with pool.acquire() as conn:
        locations = await conn.fetch(
            'SELECT id, name, lat, lng FROM "stock_location" '
            "WHERE status = 'active' AND lat IS NOT NULL AND lng IS NOT NULL"
        )
    if not locations:
        return {
            "location_context": {"node_id": None, "node_name": None, "distance_meters": None, "user_system_balance": 0},
            "feed_items": [],
            "nearest_location": None,
            "batches": [],
        }

    loc_list = [
        (str(row["id"]), float(row["lat"]), float(row["lng"])) for row in locations
    ]
    nearest_id, distance_km = find_nearest_location(loc_list, lat=lat, lon=lon)
    nearest_row = next((r for r in locations if str(r["id"]) == nearest_id), None)

    # ── 顾客资产外显 (v9.0): 已登录顾客的系统余额用于渲染顶部资产 ──
    user_system_balance = 0
    if customer_id:
        async with pool.acquire() as conn:
            bal_row = await conn.fetchval(
                'SELECT system_balance FROM "customer" WHERE id = $1 AND deleted_at IS NULL',
                customer_id,
            )
            user_system_balance = int(bal_row or 0)

    safety_cutoff = datetime.now(UTC) + timedelta(hours=_NEARBY_FEED_SAFETY_MARGIN_HOURS)
    async with pool.acquire() as conn:
        batches = await conn.fetch(
            "SELECT b.id, b.variant_id, b.retail_price_cents, b.stock_qty, "
            "b.reserved_qty, b.expiration_time, b.video_url, b.created_at, "
            "v.sku_code, v.reference_price_cents, p.title, p.id AS product_id, "
            "s.name AS location_name "
            'FROM "inventory_batch" b '
            'JOIN "product_variant" v ON v.id = b.variant_id '
            'JOIN "product" p ON p.id = v.product_id '
            'JOIN "stock_location" s ON s.id = b.location_id '
            "WHERE b.location_id = $1 AND b.status = 'active' "
            "AND b.stock_qty - b.reserved_qty > 0 "
            "AND (b.expiration_time IS NULL OR b.expiration_time > $2) "
            "ORDER BY b.expiration_time NULLS LAST "
            "LIMIT $3",
            nearest_id,
            safety_cutoff,
            limit,
        )

    batch_rows = [dict(row) for row in batches]
    batch_ids = [str(r["id"]) for r in batch_rows]

    # ── 实况流速 (v9.0): 过去 1 小时每个批次的成交笔数 ──
    velocity_by_batch: dict[str, int] = {}
    if batch_ids:
        async with pool.acquire() as conn:
            vel_rows = await conn.fetch(
                "SELECT ib.id AS batch_id, COUNT(*) AS n "
                'FROM "order_line_item" oli '
                'JOIN "inventory_batch" ib ON ib.id = oli.batch_id '
                'JOIN "customer_order" o ON o.id = oli.order_id '
                "WHERE ib.id = ANY($1::uuid[]) AND o.created_at > NOW() - INTERVAL '1 hour' "
                "GROUP BY ib.id",
                batch_ids,
            )
            velocity_by_batch = {str(r["batch_id"]): int(r["n"]) for r in vel_rows}

    # ── Phase 7 Task 3: 行为序列加权 (极简协同过滤) ──
    device_id = (request.headers.get("x-device-id") or "").strip()
    affinity_scores: dict[str, float] = {}
    history_cutoff = datetime.now(UTC) - timedelta(days=_NEARBY_FEED_HISTORY_DAYS)
    if device_id:
        async with pool.acquire() as conn:
            user_products = await conn.fetch(
                "SELECT DISTINCT product_id FROM device_purchase_log "
                "WHERE device_id = $1 AND purchased_at > $2 "
                "LIMIT $3",
                device_id[:64],
                history_cutoff,
                _NEARBY_FEED_HISTORY_MAX_PRODUCTS,
            )
            if user_products:
                cooccurrence_rows = await conn.fetch(
                    "WITH recent AS ( "
                    "  SELECT oli.order_id, ib.product_id "
                    '  FROM "order_line_item" oli '
                    '  JOIN "inventory_batch" ib ON ib.id = oli.batch_id '
                    '  JOIN "customer_order" o ON o.id = oli.order_id '
                    "  WHERE o.created_at > $1 "
                    "  GROUP BY oli.order_id, ib.product_id "
                    ") "
                    "SELECT a.product_id AS left_id, b.product_id AS right_id, COUNT(*) AS n "
                    "FROM recent a JOIN recent b ON a.order_id = b.order_id "
                    "AND a.product_id < b.product_id "
                    "GROUP BY 1, 2 ORDER BY n DESC LIMIT $2",
                    history_cutoff,
                    _NEARBY_FEED_COOCCURRENCE_ORDER_CAP,
                )
                cooccurrence = {
                    (str(r["left_id"]), str(r["right_id"])): r["n"]
                    for r in cooccurrence_rows
                }
                user_ids = [str(r["product_id"]) for r in user_products]
                candidate_ids = [str(r["product_id"]) for r in batch_rows]
                affinity_scores = compute_batch_affinity_scores(
                    user_ids, candidate_ids, cooccurrence
                )

    reranked = rerank_feed_by_affinity(batch_rows, affinity_scores)

    # ── BFF v9.0: 量化做市扁平数组 ──
    feed_items = []
    for row in reranked:
        enriched = dict(row)
        # 基准价: price_benchmark 缺失 (v9 时点表为空) → 回退商品参考价
        benchmark = int(enriched.get("reference_price_cents") or 0)
        if benchmark <= 0:
            benchmark = int(enriched.get("retail_price_cents") or 0)
        enriched["benchmark_price_cents"] = benchmark
        enriched["observed_velocity"] = normalize_velocity(
            velocity_by_batch.get(str(enriched["id"]), 0)
        )
        # 可用量 = 物理库存 - 结算期硬锁
        available = int(enriched.get("stock_qty") or 0) - int(enriched.get("reserved_qty") or 0)
        enriched["stock_qty"] = max(0, available)
        feed_items.append(build_feed_item(enriched))

    return {
        "location_context": {
            "node_id": nearest_id,
            "node_name": nearest_row["name"] if nearest_row else None,
            "distance_meters": round(distance_km * 1000),
            "user_system_balance": user_system_balance,
        },
        "feed_items": feed_items,
        # 旧契约兼容字段 (Phase 7 前端迁移期仍引用)
        "nearest_location": {"id": nearest_id, "distance_km": round(distance_km, 3)},
        "safety_margin_hours": _NEARBY_FEED_SAFETY_MARGIN_HOURS,
        "behavior_boosted": bool(affinity_scores),
        "batches": [
            {
                "batch_id": str(row["id"]),
                "variant_id": str(row["variant_id"]),
                "product_id": str(row["product_id"]),
                "title": row["title"],
                "sku_code": row["sku_code"],
                "retail_price_cents": row["retail_price_cents"],
                "stock_qty": max(0, int(row["stock_qty"]) - int(row.get("reserved_qty") or 0)),
                "expiration_time": (
                    row["expiration_time"].isoformat() if row["expiration_time"] else None
                ),
                "video_url": row["video_url"],
                "location_name": row["location_name"],
                "affinity": row["affinity"],
                "boosted": row["boosted"],
            }
            for row in reranked
        ],
    }


# ── Phase 9: 薛定谔购物车 TTL 锁 (乐观 UI 锁的后端基座) ──────────────────

#: 锁单 TTL 秒数 (5 分钟)。到期未结算自动释放，库存还给网络。
CART_LOCK_TTL_SECONDS = 300


class _CartLockRequest(BaseModel):
    batch_id: str
    device_id: str = ""
    quantity: int = 1


@ext_bespoke_router.post("/store/cart/lock")
async def lock_cart_item(body: _CartLockRequest, request: Request):
    """乐观锁单 (Phase 9: 薛定谔购物车状态机后端基座)。

    前端点击"抢！"的瞬间调用，后端原子写入 ``cart_lock`` TTL 锁行，返回
    ``locked_until`` (now + 5 分钟) 供前端倒计时；到期未结算自动释放。

    可用量口径: 物理库存 - 结算期硬锁 (reserved_qty) - 未过期软锁 (cart_lock)。
    每次调用先清扫本批次的过期锁 (防僵尸锁堆积)。

    原子性: 用 ``SELECT ... FOR UPDATE`` 锁住批次行，串行化同批次的并发
    抢锁，杜绝超卖 (与 checkout 的硬锁同一把互斥)。

    公开端点 (零登录扫码即买)，纳入限流防刷。
    """
    pool = _pool(request)
    qty = max(1, body.quantity)

    # 清扫过期软锁 (本批次) + 原子锁行，同一事务内完成
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                'DELETE FROM "cart_lock" WHERE batch_id = $1 AND locked_until <= NOW()',
                body.batch_id,
            )
            # 锁批次行，串行化并发抢锁
            batch = await conn.fetchrow(
                'SELECT id, stock_qty, reserved_qty FROM "inventory_batch" '
                "WHERE id = $1 AND status = 'active' "
                "FOR UPDATE",
                body.batch_id,
            )
            if batch is None:
                raise HTTPException(404, "batch not found or not active")

            locked_row = await conn.fetchrow(
                'SELECT COALESCE(SUM(qty), 0)::int AS n FROM "cart_lock" '
                "WHERE batch_id = $1 AND locked_until > NOW()",
                body.batch_id,
            )
            active_locks = int(locked_row["n"])

            stock_qty = int(batch["stock_qty"])
            reserved_qty = int(batch["reserved_qty"] or 0)
            available = stock_qty - reserved_qty - active_locks

            if qty > available:
                return {
                    "status": "failed",
                    "batch_id": body.batch_id,
                    "reason": "oversold" if available <= 0 else "insufficient",
                    "available_qty": max(0, available),
                }

            locked_until = datetime.now(UTC) + timedelta(seconds=CART_LOCK_TTL_SECONDS)
            await conn.execute(
                'INSERT INTO "cart_lock" (batch_id, device_id, qty, locked_until) '
                "VALUES ($1, $2, $3, $4)",
                body.batch_id,
                body.device_id[:64],
                qty,
                locked_until,
            )

    return {
        "status": "locked",
        "batch_id": body.batch_id,
        "qty": qty,
        "locked_until": locked_until.isoformat(),
        "ttl_seconds": CART_LOCK_TTL_SECONDS,
    }


# ── Phase 9: 弱网离线核销提货码 (PWA 凭证签发) ─────────────────────

#: 提货码有效期 (小时)。24 小时内凭码取货。
PICKUP_TICKET_TTL_HOURS = 24


class _PickupTicketRequest(BaseModel):
    order_id: str


@ext_bespoke_router.post("/store/pickup-ticket")
async def issue_pickup_ticket(body: _PickupTicketRequest, request: Request):
    """签发加密 JWT 提货码 (Phase 9 SPEC §5 弱网离线核销凭证)。

    支付成功回调后调用：后端校验订单已支付，签发带过期时间的 JWT，
    前端 Service Worker 将其硬缓存至 localStorage——大妈在地下车库断网
    也能用全屏最高亮度渲染该提货码，微仓扫码枪可反向读取。

    JWT payload: { order_id, node_name, typ: "pickup" }
    有效期: 24 小时。

    公开端点 (持提货码即可核销，无需登录)，纳入限流防刷。
    """
    from obase.crypto.util import CryptoUtil

    pool = _pool(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT o.id, o.status, o.grand_total_cents '
            'FROM "customer_order" o '
            'WHERE o.id = $1',
            body.order_id,
        )
    if row is None:
        raise HTTPException(404, "order not found")

    # 只对已支付订单签发提货码
    paid_statuses = {"confirmed", "paid", "fulfilled", "completed"}
    if str(row["status"]) not in paid_statuses:
        raise HTTPException(
            409, f"order not paid (status={row['status']}), cannot issue pickup ticket"
        )

    # 取货点: 优先从订单 shipping_address 的 node_name 取，缺失回退默认
    node_name = "附近节点"
    if row.get("node_name"):
        node_name = row["node_name"]

    cfg_store = request.app.state.config
    pickup_code = CryptoUtil.jwt_sign(
        payload={
            "order_id": body.order_id,
            "node_name": node_name,
            "typ": "pickup",
        },
        secret=cfg_store.jwt_secret,
        expires_in_minutes=PICKUP_TICKET_TTL_HOURS * 60,
        algorithm=cfg_store.jwt_algorithm,
    )
    expires_at = datetime.now(UTC) + timedelta(hours=PICKUP_TICKET_TTL_HOURS)

    return {
        "order_id": body.order_id,
        "pickup_code": pickup_code,
        "node_name": node_name,
        "grand_total_cents": int(row["grand_total_cents"] or 0),
        "expires_at": expires_at.isoformat(),
    }


# ── 智能体网关 (Agent Gateway): Hermes/Cindy/任意 Agent 接管系统 ───────
# 鉴权: /agent/* 全部要求 ADMIN OPS JWT (get_current_user)，手机指挥台先登录。
# 工具发现/执行/命令/视频传货 —— 见 app/ext/agent_gateway.py。


class _AgentExecuteRequest(BaseModel):
    tool: str
    args: dict[str, Any] = {}


class _AgentCommandRequest(BaseModel):
    text: str


class _AgentIngestRequest(BaseModel):
    location_id: str = ""
    retail_price_cents: int | None = None
    stock_qty: int = 30
    category_slug: str = "daily"


@ext_bespoke_router.get("/agent/tools")
async def agent_list_tools(
    principal: dict[str, Any] = Depends(get_current_user),
):
    """工具发现: 返回全部 omodul 工具清单 (含参数 schema)。

    这是 Hermes/Cindy 等智能体接管系统的标准入口 —— 拿到清单即可调用
    /agent/execute 执行任意业务能力 (上架/调价/结算/退款/广播)。
    """
    return {"count": len(discover_tools()), "tools": discover_tools()}


@ext_bespoke_router.post("/agent/execute")
async def agent_execute(
    body: _AgentExecuteRequest,
    request: Request,
    principal: dict[str, Any] = Depends(get_current_user),
):
    """工具执行 (JSON-RPC 风格): {tool, args} → omodul 结果。

    Hermes/Cindy 等 agent 的 function-calling 后端直接指到这里。
    """

    result = await execute_tool(
        body.tool,
        body.args,
        pool=_pool(request),
        out_root=_ext_bespoke_output_dir(request, "agent_execute"),
        principal=principal,
    )
    return result


@ext_bespoke_router.post("/agent/command")
async def agent_command(
    body: _AgentCommandRequest,
    request: Request,
    principal: dict[str, Any] = Depends(get_current_user),
):
    """自然语言命令: 手机发指令 (如"上架 西红柿 19.9 元 30 件")。

    规则意图匹配 → 自动提取参数 → 执行工具；返回路由 + 执行结果。
    LLM 就绪后可替换为 tool-calling 编排 (与 /admin/agent-chat 同构)。
    """
    from .ext.agent_gateway import execute_command

    return await execute_command(
        body.text,
        pool=_pool(request),
        out_root=_ext_bespoke_output_dir(request, "agent_command"),
        principal=principal,
    )


@ext_bespoke_router.post("/agent/ingest")
async def agent_ingest(
    request: Request,
    file: UploadFile,
    location_id: str = Form(""),
    retail_price_cents: int | None = Form(None),
    stock_qty: int = Form(30),
    category_slug: str = Form("daily"),
    principal: dict[str, Any] = Depends(get_current_user),
):
    """视频/图片传货: 手机实拍 → 自动上架。

    multipart/form-data: file=视频文件, 可选 location_id/retail_price_cents/
    stock_qty/category_slug。文件名清洗成商品名，媒体文件即商品图/视频。
    """

    return await ingest_media(
        file,
        location_id=location_id,
        pool=_pool(request),
        out_root=_ext_bespoke_output_dir(request, "agent_ingest"),
        principal=principal,
        retail_price_cents=retail_price_cents,
        stock_qty=stock_qty,
        category_slug=category_slug,
    )


# ── 补天计划 Task 1.1: 抖音转化回调 (HMAC-SHA256 验签入口) ─────────────────


class _DouyinCallbackRequest(BaseModel):
    order_id: str
    douyin_uid: str | None = None


@ext_bespoke_router.post("/growth/douyin_callback")
async def ext_douyin_callback(
    body: _DouyinCallbackRequest, request: Request
) -> JSONResponse:
    """抖音开放平台转化回调入口 (补天计划 Task 1.1)。

    签名校验由 WebhookSignatureMiddleware 在 ASGI 层强制完成 (非法请求 403
    丢弃，根本到不了这里)；能走到这个 handler 的都是验签通过的请求，handler
    直接把归因参数透传给 record_douyin_conversion_workflow 落账——不需要再
    挂 admin token (HMAC 就是这道端点的鉴权)。

    公开端点 (仅对携带合法 X-Hemall-Signature 的请求开放)，纳入限流防刷。
    """
    from .ext.omodul.record_douyin_conversion_workflow import (
        RecordDouyinConversionWorkflowConfig,
        RecordDouyinConversionWorkflowInput,
        record_douyin_conversion_workflow,
    )

    result = await record_douyin_conversion_workflow(
        RecordDouyinConversionWorkflowConfig(),
        RecordDouyinConversionWorkflowInput(
            order_id=body.order_id, douyin_uid=body.douyin_uid
        ),
        _ext_bespoke_output_dir(request, "record_douyin_conversion_workflow"),
        pool=_pool(request),
    )
    status_code = 200 if result.get("status") == "completed" else 422
    return JSONResponse(status_code=status_code, content=jsonable_encoder(result))


#: 补天计划 Task 1.2: ext 手写公开端点 (供限流中间件推导路径集合)。
_EXT_BESPOKE_PUBLIC_PATHS = {
    "/marketing/reward_crowdsourced_benchmark_workflow",
    "/aftersales/submit_rma_claim",
    "/supply-chain/suppliers/lookup",
    "/store/nearby-feed",
    "/store/cart/lock",
    "/store/pickup-ticket",
    "/growth/douyin_callback",
}


def public_zero_login_paths() -> set[str]:
    """补天计划 Task 1.2: 零登录自助公开端点的完整精确路径集合。

    来源：ext registry 里 require_auth=False 的 omodul 端点 (顾客/供应商/
    邻居自助) + ext 手写公开端点。storefront 共享商城公开面 (整个 /store/*
    前缀) 由调用方在 main.py 用 path_prefixes 传入，这里是精确路径集合。
    """
    paths = set(_EXT_BESPOKE_PUBLIC_PATHS)
    paths.update(spec.path for spec in ext_endpoint_specs() if not spec.require_auth)
    # 共享 registry 的公开端点 (create_customer/create_user 等自助注册) 同样防刷。
    paths.update(spec.path for spec in all_endpoint_specs() if not spec.require_auth)
    return paths


def build_router() -> APIRouter:
    """生成聚合路由: 鉴权 + 全部商务 omodul 端点 + 读层 + 商城。"""
    router = APIRouter()
    router.include_router(auth_router)
    router.include_router(storefront_router)
    router.include_router(admin_read_router)
    router.include_router(ext_bespoke_router)

    for spec in all_endpoint_specs():
        router.add_api_route(
            spec.path,
            omodul_endpoint(
                spec.fn,
                spec.config_cls,
                spec.input_cls,
                success_status=spec.success_status,
                require_auth=spec.require_auth,
                post_hook=_POST_HOOKS.get(spec.name),
            ),
            methods=["POST"],
            name=spec.name,
            tags=[spec.domain],
        )

    for spec in ext_endpoint_specs():
        router.add_api_route(
            spec.path,
            omodul_endpoint(
                spec.fn,
                spec.config_cls,
                spec.input_cls,
                success_status=spec.success_status,
                require_auth=spec.require_auth,
                post_hook=_EXT_POST_HOOKS.get(spec.name),
            ),
            methods=["POST"],
            name=f"ext_{spec.name}",
            tags=[spec.domain],
        )
    return router
