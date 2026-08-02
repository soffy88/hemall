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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import queries
from .auth import router as auth_router
from .events import fire_event
from .ext.registry import all_endpoint_specs as ext_endpoint_specs
from .ext.oskill import find_nearest_location
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


@ext_bespoke_router.get("/store/nearby-feed")
async def get_nearby_feed(
    request: Request,
    lat: float,
    lon: float,
    limit: int = Query(50, ge=1, le=100),
):
    """位置 Feed 流："人找货"到"地理位置找货"的彻底反转 (补天计划 Task 2.1)。

    根据传入坐标，通过底层距离算法 (oskill.find_nearest_location，纯 Python
    haversine，不依赖 PostGIS) 找出最近的 active 微仓，只返回该节点内
    ``stock_qty > 0`` 且 ``expiration_time`` 安全的批次列表 (安全货架期余量
    2 小时，过期/临期批次在 SQL 层直接过滤掉，是比 decay 引擎更靠前的一道
    防线)。

    公开端点 (零登录扫码即买)，纳入限流防刷保护 (见 public_zero_login_paths)。

    Args (query params):
        lat/lon: 顾客当前位置 (十进制坐标)。
        limit: 返回批次上限 (默认 50，最多 100)。
    """
    pool = _pool(request)

    async with pool.acquire() as conn:
        locations = await conn.fetch(
            'SELECT id, lat, lng FROM "stock_location" '
            "WHERE status = 'active' AND lat IS NOT NULL AND lng IS NOT NULL"
        )
    if not locations:
        return {"nearest_location": None, "batches": []}

    loc_list = [
        (str(row["id"]), float(row["lat"]), float(row["lng"])) for row in locations
    ]
    nearest_id, distance_km = find_nearest_location(loc_list, lat=lat, lon=lon)

    safety_cutoff = datetime.now(UTC) + timedelta(hours=_NEARBY_FEED_SAFETY_MARGIN_HOURS)
    async with pool.acquire() as conn:
        batches = await conn.fetch(
            "SELECT b.id, b.variant_id, b.retail_price_cents, b.stock_qty, "
            "b.expiration_time, b.video_url, v.sku_code, p.title, s.name AS location_name "
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

    return {
        "nearest_location": {"id": nearest_id, "distance_km": round(distance_km, 3)},
        "safety_margin_hours": _NEARBY_FEED_SAFETY_MARGIN_HOURS,
        "batches": [
            {
                "batch_id": str(row["id"]),
                "variant_id": str(row["variant_id"]),
                "title": row["title"],
                "sku_code": row["sku_code"],
                "retail_price_cents": row["retail_price_cents"],
                "stock_qty": row["stock_qty"],
                "expiration_time": (
                    row["expiration_time"].isoformat()
                    if row["expiration_time"]
                    else None
                ),
                "video_url": row["video_url"],
                "location_name": row["location_name"],
            }
            for row in batches
        ],
    }


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
