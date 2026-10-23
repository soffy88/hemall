"""app.ext.oservi — hemall 扩展层元服务：无人工厂的调度引擎装配。

SPEC §5: "引擎不包含具体的电商逻辑，它只负责依据时间和信号，无情地拉起底层的
omodul"——机制 (定时循环/信号扇出) 由 platform/3O/oservi 的共享引擎骨架
(CronSchedulerEngine / EventWebhookDispatcherEngine) 提供；业务逻辑 (扫哪张表、
算什么价、调用哪个 omodul) 由本文件以"注入点 callable"的形式装配，风格对齐
hemall 自己已有的 app/events.py (那是 hemall 原有商城域接 oservi 的先例)。

engine.run()：EventWebhookDispatcherEngine 的 run() 是非阻塞的 (只是置位就绪)，
在装配期直接调用即可；CronSchedulerEngine 不用它自己的 run() (内部
`asyncio.run(...)`，不能在已经跑着的 event loop 里直接调)，而是在
oservi_lifecycle.py 里用一个跑在主 event loop 上的 asyncio.Task 反复调
run_once()——这里只负责装配引擎实例，常驻调度循环怎么接见 oservi_lifecycle.py。

SPEC §5 六个引擎 (v1.0) 的落地情况 (每个函数体内的 docstring 详述了具体每个
哪里是"如实做不到"的诚实空白，不是漏做)：
    §5.1 市场与动态定价引擎
        build_weather_arbitrage_engine — 微气候套利 (on_interval)
        build_inventory_reaper_engine  — 死神引擎 (on_cron)
        build_demand_aggregator_engine — 集单脉冲 (on_demand/on_signal)
    §5.2 物理网络流转引擎
        build_delivery_wave_engine        — 班车波次 (on_cron)
        build_node_host_settlement_engine — 去中心化分润 (on_cron)
        build_batch_broadcast_engine      — 零成本广播 (on_signal)

v2.0 §5 全部四个引擎：
    build_autonomous_triage_engine — 全自动纠纷仲裁 (on_signal)
    build_market_maker_engine      — 物理资产做市 (on_interval，5 分钟)
    build_tote_balancing_engine    — 潮汐载具调度 (on_cron，每日发车前)
    build_spatial_fomo_engine      — 幽灵节点围栏点火 (on_signal)

v4.0 §5 社交播报引擎：
    build_social_broadcast_engine — 微信视频号自动播报 (on_interval，15 分钟)

v5.0 §5 试探单做市引擎：
    build_market_maker_probe_engine — 冷启动试探单出清 (on_interval，1 小时)

v6.0 §5 抖音做市与清算守护进程：
    build_mercenary_routing_engine   — 嗜血雇佣兵路由 (on_interval，10 分钟)
    build_affiliate_settlement_engine — 夜间分润清算 (on_cron，每日凌晨 2 点)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg

from ..config import Settings
from .omodul.commission_new_location import (
    CommissionNewLocationConfig,
    CommissionNewLocationInput,
    commission_new_location,
)
from .omodul.dispatch_labor_payment import (
    DispatchLaborPaymentConfig,
    DispatchLaborPaymentInput,
    dispatch_labor_payment,
)
from .omodul.execute_channel_broadcast_workflow import (
    ExecuteChannelBroadcastWorkflowConfig,
    ExecuteChannelBroadcastWorkflowInput,
    execute_channel_broadcast_workflow,
)
from .omodul.execute_liability_routing_workflow import (
    ExecuteLiabilityRoutingWorkflowConfig,
    ExecuteLiabilityRoutingWorkflowInput,
    execute_liability_routing_workflow,
)
from .omodul.mark_batch_for_disposal import (
    MarkBatchForDisposalConfig,
    MarkBatchForDisposalInput,
    mark_batch_for_disposal,
)
from .omodul.scrap_batch_inventory import (
    ScrapBatchInventoryConfig,
    ScrapBatchInventoryInput,
    scrap_batch_inventory,
)
from .oprim import (
    db_query_many,
    db_query_one,
    ext_douyin_sync_inventory,
    ext_notify_send,
    ext_pay_transfer,
    ext_weather_forecast,
    spider_fetch_competitor_prices,
    spider_fetch_products,
    vlm_assess_damage,
    vlm_extract_price,
)
from .oservi_compat import CronSchedulerEngine, EventWebhookDispatcherEngine
from .oskill import (
    SLA_COMPENSATION_CENTS,
    WAVE_SHIPPING_PRICE_CENTS,
    adjust_demand_curve_by_weather,
    build_default_vwap_curve,
    calculate_broadcast_priority,
    calculate_decay_price,
    calculate_next_probe_price,
    calculate_vwap_deviation,
    compute_delivery_sla,
    compute_market_maker_price,
    compute_mercenary_bounty_rate,
    evaluate_claim_credibility,
    normalize_sku_price,
)
from .spider_targets import extract_unit_from_text

logger = logging.getLogger("hemall.ext.oservi")


def _output_dir(settings: Settings, engine_name: str, omodul_name: str) -> Path:
    """oservi 引擎内部调用 omodul 时用的 output_dir，风格对齐 app/storefront._output_dir。"""
    out = Path(settings.output_root) / "oservi" / engine_name / omodul_name
    out.mkdir(parents=True, exist_ok=True)
    return out


# ── §5.1 市场与动态定价引擎 ──────────────────────────────────────────────────


def build_weather_arbitrage_engine(
    pool: Any,
    *,
    broadcast_engine: EventWebhookDispatcherEngine,
    weather_provider: str = "manual",
) -> CronSchedulerEngine:
    """微气候套利引擎 (on_interval，每小时)。

    拉取每个 active 微仓坐标点的天气预报 → 对该仓全部 active 批次调
    oskill.adjust_demand_curve_by_weather 重算零售价 (阈值判断已经在 oskill
    内部：没到暴雨阻断自提的严重程度直接原价返回，这里不重复判断) → 价格
    真的变了才写库 → 有批次被改价，就拉起 batch_broadcast_engine 广播降价 Feed
    (SPEC: "再拉起广播下发降价 Feed，完成时空套利")。
    """

    async def reprice_for_weather(**_: Any) -> dict[str, Any]:
        locations = await db_query_many(
            pool,
            sql=(
                'SELECT id, lat, lng AS lon FROM "stock_location" '
                "WHERE status = 'active' AND lat IS NOT NULL AND lng IS NOT NULL"
            ),
        )
        repriced: list[dict[str, Any]] = []
        for loc in locations:
            forecast = await ext_weather_forecast(
                weather_provider, lat=float(loc["lat"]), lon=float(loc["lon"])
            )
            batches = await db_query_many(
                pool,
                sql=(
                    'SELECT id, retail_price_cents FROM "inventory_batch" '
                    "WHERE location_id = $1 AND status = 'active'"
                ),
                params=(loc["id"],),
            )
            for batch in batches:
                new_price = adjust_demand_curve_by_weather(
                    batch["retail_price_cents"],
                    rain_probability=forecast["rain_probability"],
                    rain_intensity=forecast["rain_intensity"],
                )
                if new_price == batch["retail_price_cents"]:
                    continue
                async with pool.acquire() as conn:
                    await conn.execute(
                        'UPDATE "inventory_batch" SET retail_price_cents = $1 WHERE id = $2',
                        new_price,
                        batch["id"],
                    )
                repriced.append(
                    {
                        "batch_id": str(batch["id"]),
                        "location_id": str(loc["id"]),
                        "old_price": batch["retail_price_cents"],
                        "new_price": new_price,
                    }
                )

        if repriced:
            await broadcast_engine.dispatch("batch.repriced", {"batches": repriced})

        return {
            "locations_checked": len(locations),
            "batches_repriced": len(repriced),
            "repriced": repriced,
        }

    reprice_for_weather.__name__ = "reprice_for_weather"

    return CronSchedulerEngine(
        tasks=[reprice_for_weather],
        trigger={"on_interval": 3600},
        config={"interval_seconds": 3600},
        name="ext-weather-arbitrage",
    )


#: 临期降价窗口 (小时)：批次距过期还有这么久时开始进入衰减降价梯度；
#: 超过过期时间直接走销毁分支，不再降价。
_MARKDOWN_WINDOW_HOURS = 48


def build_inventory_reaper_engine(pool: Any, *, settings: Settings) -> CronSchedulerEngine:
    """死神引擎 (on_cron，半夜 2:00)。

    扫描全仓 active 且有可用库存 (未被硬锁) 的批次：已过期的直接注入
    omodul.mark_batch_for_disposal 强制销毁；临期未过期的调
    oskill.calculate_decay_price 算清仓价，价格真的降了才写库 (SPEC:
    "分发降价任务或直接注入 mark_batch_for_disposal 强制销毁废弃")。
    单个批次处理失败不中断整个 tick，记录在 errors 里。
    """

    async def reap_expiring_batches(**_: Any) -> dict[str, Any]:
        now = datetime.now(UTC)
        candidates = await db_query_many(
            pool,
            sql=(
                "SELECT id, created_at AS intake_time, expiration_time, "
                "cost_price_cents, retail_price_cents "
                'FROM "inventory_batch" '
                "WHERE status = 'active' AND expiration_time IS NOT NULL "
                "AND stock_qty - reserved_qty > 0"
            ),
        )

        disposed: list[str] = []
        repriced: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for batch in candidates:
            batch_id = str(batch["id"])
            try:
                expiration_time = batch["expiration_time"]
                if expiration_time <= now:
                    result = await mark_batch_for_disposal(
                        MarkBatchForDisposalConfig(),
                        MarkBatchForDisposalInput(batch_id=batch_id, reason="expired"),
                        _output_dir(
                            settings,
                            "inventory_reaper_engine",
                            "mark_batch_for_disposal",
                        ),
                        pool=pool,
                    )
                    if result["status"] == "completed":
                        disposed.append(batch_id)
                    else:
                        errors.append({"batch_id": batch_id, "error": result["error"]})
                    continue

                hours_to_expiry = (expiration_time - now).total_seconds() / 3600
                if hours_to_expiry > _MARKDOWN_WINDOW_HOURS:
                    continue

                shelf_life_hours = (expiration_time - batch["intake_time"]).total_seconds() / 3600
                if shelf_life_hours <= 0:
                    continue  # 数据异常 (expiration<=intake)，跳过不崩整个 tick

                new_price = calculate_decay_price(
                    batch["intake_time"],
                    shelf_life_hours=shelf_life_hours,
                    base_cost=batch["cost_price_cents"],
                )
                if new_price >= batch["retail_price_cents"]:
                    continue
                async with pool.acquire() as conn:
                    await conn.execute(
                        'UPDATE "inventory_batch" SET retail_price_cents = $1 WHERE id = $2',
                        new_price,
                        batch["id"],
                    )
                repriced.append({"batch_id": batch_id, "new_price": new_price})
            except Exception as exc:  # noqa: BLE001 - 单批次失败不拖垮整个死神 tick
                errors.append({"batch_id": batch_id, "error": str(exc)})

        return {"disposed": disposed, "repriced": repriced, "errors": errors}

    reap_expiring_batches.__name__ = "reap_expiring_batches"

    return CronSchedulerEngine(
        tasks=[reap_expiring_batches],
        trigger={"on_cron": "0 2 * * *"},
        config={"interval_seconds": 86400},
        name="ext-inventory-reaper",
    )


def build_demand_aggregator_engine(
    *, notification_provider: str = "log"
) -> EventWebhookDispatcherEngine:
    """集单脉冲引擎 (on_demand，监听意向金水池)。

    omodul.create_crowd_intent 达到 500 单阈值时，本身已经会直接发一条通知——
    那是"监听意向金水池"最小可用的实现，不依赖这个引擎也能工作。这个引擎
    提供的是"信号 → 多订阅者并发扇出"这层机制本身：未来调用方 (路由/编排层)
    在 create_crowd_intent 返回 threshold_triggered=True 后改为调
    engine.dispatch("crowd_intent.threshold_reached", {...})，就能不改
    create_crowd_intent 本身，白送多订阅者能力 (同时通知采购 + 记审计 + 触发
    下游动作)。omodul 不允许反向依赖 oservi (红线 5：3O 四包禁 import
    oservi)，这根线必须由更外层的调用方接，不能塞进 create_crowd_intent 内部。
    """

    async def notify_procurement_po(*, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        ok = await ext_notify_send(
            notification_provider,
            channel="email",
            template=event,
            data={"to": "supply-chain@hemall.internal", **payload},
        )
        return {"event": event, "notified": ok}

    engine = EventWebhookDispatcherEngine(
        subscribers=[notify_procurement_po],
        trigger={"on_demand": True},
        config={},
        name="ext-demand-aggregator",
    )
    engine.run()
    return engine


# ── §5.2 物理网络流转引擎 ────────────────────────────────────────────────────


def build_delivery_wave_engine(pool: Any) -> CronSchedulerEngine:
    """班车波次引擎 (on_cron，定点 10:00/16:00)。

    打包所有 wave (慢送) 未发车订单：orders 表没有单独的 shipping_type 列，
    但 wave 档运费恒为 oskill.WAVE_SHIPPING_PRICE_CENTS (跟 express 的浮动价、
    pickup 恒 0 互斥)，可以拿运费金额反查配送方式，不需要额外加列。按订单
    对应批次的 location_id 分组，标记 dispatch_status='dispatched' 防止下次
    tick 重复打包。SPEC 要的"规划顺路的物流节点"是真正的路径优化问题 (VRP)，
    这里不做 (没有车辆/路网数据模型)，只按发车微仓分组，如实标注在返回值里。
    """

    async def dispatch_wave_orders(**_: Any) -> dict[str, Any]:
        rows = await db_query_many(
            pool,
            sql=(
                "SELECT o.id AS order_id, b.location_id AS location_id "
                'FROM "customer_order" o '
                'JOIN "order_line_item" oli ON oli.order_id = o.id '
                'JOIN "inventory_batch" b ON b.id = oli.batch_id '
                "WHERE o.shipping_cents = $1 AND o.status = 'paid' "
                "AND o.dispatch_status = 'pending'"
            ),
            params=(WAVE_SHIPPING_PRICE_CENTS,),
        )
        # 一个订单可能有多行 order_line_item (多个 batch)；只取第一次出现的
        # location_id 当发车点分组依据，不代表真实路径规划。
        order_location: dict[Any, Any] = {}
        for row in rows:
            order_location.setdefault(row["order_id"], row["location_id"])

        if not order_location:
            return {"wave_orders_dispatched": 0, "by_location": {}}

        order_ids = list(order_location.keys())
        async with pool.acquire() as conn:
            await conn.execute(
                (
                    "UPDATE \"customer_order\" SET dispatch_status = 'dispatched', "
                    "dispatched_at = NOW() WHERE id = ANY($1)"
                ),
                order_ids,
            )

        by_location: dict[str, int] = {}
        for loc_id in order_location.values():
            by_location[str(loc_id)] = by_location.get(str(loc_id), 0) + 1

        return {"wave_orders_dispatched": len(order_ids), "by_location": by_location}

    dispatch_wave_orders.__name__ = "dispatch_wave_orders"

    return CronSchedulerEngine(
        tasks=[dispatch_wave_orders],
        trigger={"on_cron": "0 10,16 * * *"},
        config={"interval_seconds": 21600},
        name="ext-delivery-wave",
    )


def build_node_host_settlement_engine(
    pool: Any, *, settings: Settings, payout_provider: str = "manual"
) -> CronSchedulerEngine:
    """去中心化分润引擎 (on_cron，每日 23:55)。

    遍历所有有 pending 计件工资的大妈，逐个调 omodul.dispatch_labor_payment
    聚合结清 (worker_id 本身兼做打款账户——SPEC 没给"工人档案"表，扫码/CV
    验证时录的 worker_id 本来就是外部支付渠道 ID 如微信 openid，不重复建一张
    目录表)。

    宿主场地分润 (dispatch_host_dividend) 需要 tote_count (中转量) 和 host
    的打款账户，两者都没有落地的日志/目录表 (SPEC 未给 tote 流转日志表)，
    这个引擎里没法自动推导出来——如实在返回值里说明跳过原因，不假装算出
    一个数字去调用它。
    """

    async def settle_daily_ledgers(**_: Any) -> dict[str, Any]:
        worker_rows = await db_query_many(
            pool,
            sql="SELECT DISTINCT worker_id FROM \"labor_ledger\" WHERE status = 'pending'",
        )

        labor_settled: list[dict[str, Any]] = []
        labor_failed: list[dict[str, Any]] = []
        for row in worker_rows:
            worker_id = row["worker_id"]
            result = await dispatch_labor_payment(
                DispatchLaborPaymentConfig(payout_provider=payout_provider),
                DispatchLaborPaymentInput(worker_id=worker_id, payout_account=worker_id),
                _output_dir(settings, "node_host_settlement_engine", "dispatch_labor_payment"),
                pool=pool,
            )
            if result["status"] == "completed":
                labor_settled.append(
                    {"worker_id": worker_id, "total_amount": result["total_amount"]}
                )
            else:
                labor_failed.append({"worker_id": worker_id, "error": result["error"]})

        return {
            "labor_settled": labor_settled,
            "labor_failed": labor_failed,
            "host_dividends_skipped_reason": (
                "dispatch_host_dividend 需要 tote_count 和宿主打款账户，两者都没有"
                "落地的日志/目录表 (SPEC 未给)，无法在此自动推导，留给人工/未来的"
                "tote 流转日志表补齐后再自动化。"
            ),
        }

    settle_daily_ledgers.__name__ = "settle_daily_ledgers"

    return CronSchedulerEngine(
        tasks=[settle_daily_ledgers],
        trigger={"on_cron": "55 23 * * *"},
        config={"interval_seconds": 86400},
        name="ext-node-host-settlement",
    )


def build_batch_broadcast_engine(
    *, notification_provider: str = "log"
) -> EventWebhookDispatcherEngine:
    """零成本广播引擎 (on_signal，批次视频上架/改价时触发)。

    取代竞价排名：直接把底价实录推给私域订阅用户的终端。hemall 扩展侧没有
    "订阅用户名单"这张表 (跟顾客身份一样，SPEC 没定义)，这里推给一个约定的
    广播频道 (notification provider="log" 时就是落日志)；真正接入私域推送
    (公众号模板消息/小程序订阅消息) 只需要换掉 provider 实现，上层不用改。

    触发方式：build_weather_arbitrage_engine 改价后会自动 dispatch 这个引擎；
    未来批次上架流程 (create_inventory_batch 之后) 也应该调
    engine.dispatch("batch.listed", {...})，同样出于红线 5 不能塞进 omodul
    内部，得由更外层的调用方接。
    """

    async def push_price_feed(*, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        ok = await ext_notify_send(
            notification_provider,
            channel="push",
            template=event,
            data=payload,
        )
        return {"event": event, "pushed": ok}

    engine = EventWebhookDispatcherEngine(
        subscribers=[push_price_feed],
        trigger={"on_signal": True},
        config={},
        name="ext-batch-broadcast",
    )
    engine.run()
    return engine


# ── v2.0 全自动纠纷仲裁引擎 ──────────────────────────────────────────────────


def build_autonomous_triage_engine(
    pool: Any,
    *,
    settings: Settings,
    vlm_provider: str = "manual",
    payment_provider: str = "manual",
) -> EventWebhookDispatcherEngine:
    """全自动纠纷仲裁引擎 (on_signal，监听用户提交客诉单)。

    SPEC v2.0 §5 原文流程："注入 oprim.vlm_assess_damage 获取损坏定性，结合
    oskill.evaluate_claim_credibility 给出判决，最后拉起
    omodul.execute_liability_routing_workflow 完成退款和责任方扣款"——三步里
    前两步 (vlm 判损 + 信誉裁决) 由这个引擎自己的订阅者直接做 (机制在
    platform/3O/oservi 的 EventWebhookDispatcherEngine 骨架，业务在这个订阅者，
    跟 weather_arbitrage_engine 直接调 oskill 是同一个道理)；omodul 本身只管
    "给定裁决结果，落地执行"，不重复判损/裁决——见
    execute_liability_routing_workflow 的 docstring。

    batch_anomaly_rate 从该批次历史 rma_claims 里的 instant_refund 占比真实
    统计出来 (不是拍的数)；route_risk 目前没有配送路由日志可推导，由调用方
    传入 (跟 dispatch_host_dividend 的 tote_count 同一个"诚实空白"处理方式)。

    触发方式：调用方 (未来的 RMA 提交入口/HTTP 路由) 在客诉单提交后调
    ``engine.dispatch("rma.claim_submitted", {order_id, batch_id, user_id,
    evidence_image_url, user_trust_score, route_risk})``。
    """

    async def handle_claim_submitted(*, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        order_id = payload["order_id"]
        batch_id = payload["batch_id"]
        user_id = payload["user_id"]
        evidence_image_url = payload["evidence_image_url"]
        user_trust_score = payload["user_trust_score"]
        route_risk = payload.get("route_risk", 0.0)

        batch = await db_query_one(
            pool,
            sql='SELECT video_url FROM "inventory_batch" WHERE id = $1',
            params=(batch_id,),
        )
        if batch is None:
            raise ValueError(f"batch {batch_id!r} not found")

        assessment = await vlm_assess_damage(
            vlm_provider,
            evidence_img=evidence_image_url,
            original_batch_video=batch["video_url"],
        )

        anomaly_row = await db_query_one(
            pool,
            sql=(
                "SELECT COUNT(*) FILTER (WHERE decision IS NOT NULL) AS total, "
                "COUNT(*) FILTER (WHERE decision = 'instant_refund') AS upheld "
                'FROM "claim" WHERE batch_id = $1'
            ),
            params=(batch_id,),
        )
        total = anomaly_row["total"] or 0
        upheld = anomaly_row["upheld"] or 0
        batch_anomaly_rate = (upheld / total) if total > 0 else 0.0

        credibility_decision = evaluate_claim_credibility(
            user_trust_score,
            batch_anomaly_rate=batch_anomaly_rate,
            route_risk=route_risk,
        )

        result = await execute_liability_routing_workflow(
            ExecuteLiabilityRoutingWorkflowConfig(payment_provider=payment_provider),
            ExecuteLiabilityRoutingWorkflowInput(
                order_id=order_id,
                batch_id=batch_id,
                user_id=user_id,
                evidence_image_url=evidence_image_url,
                vlm_damage_type=assessment["damage_type"],
                vlm_severity=assessment["severity"],
                fraud_probability=assessment["fraud_probability"],
                credibility_decision=credibility_decision,
            ),
            _output_dir(
                settings,
                "autonomous_triage_engine",
                "execute_liability_routing_workflow",
            ),
            pool=pool,
        )
        return result

    engine = EventWebhookDispatcherEngine(
        subscribers=[handle_claim_submitted],
        trigger={"on_signal": True},
        config={},
        name="ext-autonomous-triage",
    )
    engine.run()
    return engine


# ── v2.0 高频做市与宏观抄底 / 潮汐调度 / 围栏点火引擎 ────────────────────────


def build_market_maker_engine(
    pool: Any, *, broadcast_engine: EventWebhookDispatcherEngine
) -> CronSchedulerEngine:
    """物理资产做市引擎 (on_interval，每 5 分钟)。

    只对设了 vwap_target_curve 的批次 (运营刻意标记为"高净值生鲜、需要做市"
    的批次，不是全量批次) 做市——current_sales 从 order_line_items 里真实统计
    出来，elapsed_hours 从 intake_time 算，theta_decay 按距过期时间的比例算
    (没有 expiration_time 的批次 theta_decay=0，不衰减)。真的改价了才写库，
    并复用 batch_broadcast_engine 推送 (跟 weather_arbitrage_engine 同一个
    "改价后广播" 套路)。
    """

    async def make_market(**_: Any) -> dict[str, Any]:
        batches = await db_query_many(
            pool,
            sql=(
                "SELECT id, retail_price_cents, created_at AS intake_time, "
                "expiration_time, vwap_target_curve "
                'FROM "inventory_batch" '
                "WHERE status = 'active' AND vwap_target_curve IS NOT NULL"
            ),
        )
        repriced: list[dict[str, Any]] = []
        for batch in batches:
            current_sales = await db_query_one(
                pool,
                sql=(
                    "SELECT COALESCE(SUM(quantity), 0) AS total_sold "
                    'FROM "order_line_item" WHERE batch_id = $1'
                ),
                params=(batch["id"],),
            )
            now = datetime.now(UTC)
            elapsed_hours = int((now - batch["intake_time"]).total_seconds() // 3600)
            expiration_time = batch["expiration_time"]
            if expiration_time is not None:
                total_shelf_hours = max(
                    (expiration_time - batch["intake_time"]).total_seconds() / 3600, 1
                )
                theta_decay = min(max(elapsed_hours / total_shelf_hours, 0.0), 1.0)
            else:
                theta_decay = 0.0

            import json

            target_curve = batch["vwap_target_curve"]
            if isinstance(target_curve, str):
                target_curve = json.loads(target_curve)

            try:
                deviation = calculate_vwap_deviation(
                    current_sales["total_sold"],
                    target_curve=target_curve,
                    elapsed_hours=elapsed_hours,
                )
            except ValueError:
                continue  # target_curve 格式不对，跳过这一条不拖垮整个 tick

            new_price = compute_market_maker_price(
                batch["retail_price_cents"],
                vwap_deviation=deviation,
                theta_decay=theta_decay,
            )
            if new_price == batch["retail_price_cents"]:
                continue

            async with pool.acquire() as conn:
                await conn.execute(
                    'UPDATE "inventory_batch" SET retail_price_cents = $1 WHERE id = $2',
                    new_price,
                    batch["id"],
                )
            repriced.append(
                {
                    "batch_id": str(batch["id"]),
                    "old_price": batch["retail_price_cents"],
                    "new_price": new_price,
                    "vwap_deviation": deviation,
                }
            )

        if repriced:
            await broadcast_engine.dispatch("batch.repriced", {"batches": repriced})

        return {"batches_checked": len(batches), "repriced": repriced}

    make_market.__name__ = "make_market"

    return CronSchedulerEngine(
        tasks=[make_market],
        trigger={"on_interval": 300},
        config={"interval_seconds": 300},
        name="ext-market-maker",
    )


def build_tote_balancing_engine(
    pool: Any, *, notification_provider: str = "log"
) -> CronSchedulerEngine:
    """潮汐载具调度引擎 (on_cron，每日定点发车前)。

    统计全城各节点的空筐 (idle) 存量，用"全城平均值"当基线 (SPEC 没给每个
    节点该配多少个 tote 的编制数，用平均值是能跑起来的最小合理基线)：低于
    平均值的节点判定"缺筐"，下发"顺手回收 20 个空筐奖励 5 元"的任务通知——
    这里没有真实的司机调度/智能合约执行系统，"下发任务"落地成一条通知
    (log provider 就是落日志)，跟 mark_batch_for_disposal 的 dispose_task
    通知是同一个诚实程度。
    """

    _REWARD_PER_20_TOTES_CENTS = 500

    async def balance_totes(**_: Any) -> dict[str, Any]:
        rows = await db_query_many(
            pool,
            sql=(
                "SELECT current_location_id, "
                "COUNT(*) FILTER (WHERE status = 'idle') AS idle_count "
                'FROM "tote" WHERE current_location_id IS NOT NULL '
                "GROUP BY current_location_id"
            ),
        )
        if not rows:
            return {"locations_checked": 0, "shortage_locations": []}

        avg_idle = sum(r["idle_count"] for r in rows) / len(rows)
        shortage_locations: list[dict[str, Any]] = []
        for row in rows:
            if row["idle_count"] >= avg_idle:
                continue
            location_id = str(row["current_location_id"])
            await ext_notify_send(
                notification_provider,
                channel="email",
                template="tote_rebalance_task",
                data={
                    "to": f"location:{location_id}",
                    "location_id": location_id,
                    "idle_count": row["idle_count"],
                    "reward_cents": _REWARD_PER_20_TOTES_CENTS,
                },
            )
            shortage_locations.append(
                {
                    "location_id": location_id,
                    "idle_count": row["idle_count"],
                }
            )

        return {
            "locations_checked": len(rows),
            "average_idle": avg_idle,
            "shortage_locations": shortage_locations,
        }

    balance_totes.__name__ = "balance_totes"

    return CronSchedulerEngine(
        tasks=[balance_totes],
        trigger={"on_cron": "0 9 * * *"},
        config={"interval_seconds": 86400},
        name="ext-tote-balancing",
    )


def build_spatial_fomo_engine(
    pool: Any,
    *,
    settings: Settings,
    notification_provider: str = "log",
) -> EventWebhookDispatcherEngine:
    """幽灵节点与围栏点火引擎 (on_signal)。

    运营配置"幽灵打样批次" (is_ghost_batch=True) 后，两类信号都由这一个引擎
    处理 (根据 event 名分支)：
      - "spatial_fomo.promo_push"：向目标围栏区域定向推送极端底价——
        hemall 扩展侧没有"小区订阅名单"这张表 (跟 batch_broadcast_engine 的
        "没有订阅用户名单"是同一个诚实空白)，这里推给一个约定的广播频道。
      - "spatial_fomo.threshold_reached"：意向金集够了 (由调用方监听
        crowd_intents 判定，不在这个引擎内部重新判断阈值——那是
        create_crowd_intent 自己的职责，避免重复实现)，直接拉起
        omodul.commission_new_location 在目标坐标建仓，"零成本完成新网格的
        破冰"。
    """

    async def handle_fomo_signal(*, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        if event == "spatial_fomo.threshold_reached":
            result = await commission_new_location(
                CommissionNewLocationConfig(),
                CommissionNewLocationInput(
                    host_id=payload["host_id"],
                    address=payload["address"],
                    lat=payload["lat"],
                    lon=payload["lon"],
                ),
                _output_dir(settings, "spatial_fomo_engine", "commission_new_location"),
                pool=pool,
            )
            return result

        ok = await ext_notify_send(
            notification_provider,
            channel="push",
            template=event,
            data=payload,
        )
        return {"event": event, "pushed": ok}

    engine = EventWebhookDispatcherEngine(
        subscribers=[handle_fomo_signal],
        trigger={"on_signal": True},
        config={},
        name="ext-spatial-fomo",
    )
    engine.run()
    return engine


# ── v4.0 微信视频号社交播报引擎 ──────────────────────────────────────────────


#: 曾经硬编码在生产函数签名里的占位符。现在任何引擎都不许收下它。
_PLACEHOLDER_ACCESS_TOKENS = frozenset(
    {"MOCK_WECHAT_TOKEN_XXX", "MOCK_DY_TOKEN", "MOCK_TOKEN", "placeholder"}
)

#: 显式的"未接入"哨兵：调用方主动声明"这个引擎没有凭据"，与伪装成真凭据的
#: 占位符区分开。构造照常成功，播报在调用真接口前会因凭据无效而失败。
UNCONFIGURED_TOKEN = "UNCONFIGURED"


def _reject_mock_access_token(access_token: str, engine_name: str) -> None:
    """占位符 access_token 一律拒绝——带假 token 调真接口只会静默失败。

    :data:`UNCONFIGURED_TOKEN` 是唯一被接受的"没有凭据"表达：调用方显式声明
    未接入，引擎照常装配（开发/测试环境能跑），而不是伪装成有凭据。
    """
    token = str(access_token)
    if token == UNCONFIGURED_TOKEN:
        logger.warning(
            "%s: no access_token configured (UNCONFIGURED); real broadcasts will fail",
            engine_name,
        )
        return
    if not token.strip():
        raise ValueError(
            f"{engine_name}: access_token must not be empty; wire a real credential "
            f"or pass UNCONFIGURED_TOKEN to declare it intentionally unset"
        )
    if token in _PLACEHOLDER_ACCESS_TOKENS:
        raise ValueError(
            f"{engine_name}: refusing placeholder access_token {token!r}; "
            f"pass the real credential explicitly"
        )


def build_social_broadcast_engine(
    pool: Any,
    *,
    settings: Settings,
    llm_provider: str = "manual",
    wechat_provider: str = "manual",
    access_token: str,
    stagger_seconds: float = 60.0,
) -> CronSchedulerEngine:
    """社交播报引擎 (on_interval，每 15 分钟)。

    扫两类候选：入库 1 小时内、库存 >20 且没播报过 "fresh_arrival" 的批次
    (上新)；距过期 <12 小时、库存 >5 且没播报过 "clearance" 的批次 (清仓)——
    "没播报过"直接用 LEFT JOIN channel_broadcast_logs ... WHERE l.id IS NULL
    在 SQL 里判断，不需要额外查一遍 (SPEC 原文的写法)。

    clearance 候选按 oskill.calculate_broadcast_priority 降序排列——SPEC 自己
    定义了这个优先级算子 ("库存越大、距销毁时间越短，优先级越高，插队最先
    发")，但给出的引擎伪代码里从来没真的调用它，是一个孤立函数；这里按它
    自己文档说的用途接上，不是脱离规范新造逻辑。fresh_arrival 候选不参与
    优先级排序——"距销毁时间"这个概念对刚入库的批次没有意义，calculate_
    broadcast_priority 自己的语义就是给清仓场景用的。

    access_token 是微信视频号 access_token。**必填 kwarg，没有默认值**：
    以前这里硬编码着 "MOCK_WECHAT_TOKEN_XXX"，调用方漏传真实 token 也不会报错，
    引擎带着假 token 去调微信 API，失败还只是一条 warning。
    现在漏传在调用期就 TypeError；真实 token 由调用方从配置显式解析
    （app/ext/oservi_lifecycle.py 传 settings.wechat_channel_access_token）。
    真实的 OAuth access_token 刷新流程本引擎没有实现，是诚实的已知空白。

    stagger_seconds 对应 SPEC "错峰发布，防止触发微信风控封禁"的要求，默认
    60 秒；测试时会把它调成 0，否则 run_once() 在有多个候选批次时要跑几分钟。
    """
    _reject_mock_access_token(access_token, "wechat_channel")

    async def broadcast_tick(**_: Any) -> dict[str, Any]:
        fresh_batches = await db_query_many(
            pool,
            sql=(
                "SELECT b.id AS batch_id, b.stock_qty, b.retail_price_cents AS retail_price "
                'FROM "inventory_batch" b '
                'LEFT JOIN "channel_broadcast_log" l '
                "  ON b.id = l.batch_id AND l.broadcast_type = 'fresh_arrival' "
                "WHERE l.id IS NULL AND b.status = 'active' "
                "AND b.created_at > NOW() - INTERVAL '1 hour' "
                "AND b.stock_qty > 20"
            ),
        )
        clearance_rows = await db_query_many(
            pool,
            sql=(
                "SELECT b.id AS batch_id, b.stock_qty, b.retail_price_cents AS retail_price, "
                "b.expiration_time "
                'FROM "inventory_batch" b '
                'LEFT JOIN "channel_broadcast_log" l '
                "  ON b.id = l.batch_id AND l.broadcast_type = 'clearance' "
                "WHERE l.id IS NULL AND b.status = 'active' "
                "AND b.expiration_time < NOW() + INTERVAL '12 hours' "
                "AND b.stock_qty > 5"
            ),
        )

        now = datetime.now(UTC)
        prioritized: list[tuple[int, dict[str, Any]]] = []
        for row in clearance_rows:
            expiration_time = row["expiration_time"]
            # 精确到秒的小时数，不整数取整——"还剩 59 分钟"取整成 0 小时会跟
            # "已经过期"混为一谈，明明是最紧急的那一档反而被打成最低优先级。
            theta_decay_hours = (
                (expiration_time - now).total_seconds() / 3600
                if expiration_time is not None
                else 0.0
            )
            priority = calculate_broadcast_priority(
                row["stock_qty"], theta_decay_hours=theta_decay_hours
            )
            prioritized.append((priority, row))
        prioritized.sort(key=lambda pair: pair[0], reverse=True)
        clearance_sorted = [row for _, row in prioritized]

        published: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        async def _fire(batch_row: dict[str, Any], broadcast_type: str, multiplier: int) -> None:
            result = await execute_channel_broadcast_workflow(
                ExecuteChannelBroadcastWorkflowConfig(
                    llm_provider=llm_provider, wechat_provider=wechat_provider
                ),
                ExecuteChannelBroadcastWorkflowInput(
                    batch_id=str(batch_row["batch_id"]),
                    broadcast_type=broadcast_type,
                    market_price=batch_row["retail_price"] * multiplier,
                    access_token=access_token,
                ),
                _output_dir(
                    settings,
                    "social_broadcast_engine",
                    "execute_channel_broadcast_workflow",
                ),
                pool=pool,
            )
            entry = {
                "batch_id": str(batch_row["batch_id"]),
                "broadcast_type": broadcast_type,
            }
            if result["status"] == "completed":
                published.append(entry)
            else:
                failed.append({**entry, "error": result.get("error")})
            if stagger_seconds > 0:
                await asyncio.sleep(stagger_seconds)

        for fb in fresh_batches:
            await _fire(fb, "fresh_arrival", 2)  # 模拟商超均价 = hemall 价 2 倍
        for cb in clearance_sorted:
            await _fire(cb, "clearance", 3)  # 清仓价更低，对比锚点拉大到 3 倍

        return {
            "fresh_candidates": len(fresh_batches),
            "clearance_candidates": len(clearance_sorted),
            "published": published,
            "failed": failed,
        }

    broadcast_tick.__name__ = "broadcast_tick"

    return CronSchedulerEngine(
        tasks=[broadcast_tick],
        trigger={"on_interval": 900},
        config={"interval_seconds": 900},
        name="ext-social-broadcast",
    )


# ── v5.0 试探单做市引擎 ──────────────────────────────────────────────────────


def build_market_maker_probe_engine(
    pool: Any, *, target_velocity: float = 0.5
) -> CronSchedulerEngine:
    """冷启动试探单做市引擎 (on_interval，每 1 小时)。

    捞出所有处于 "testing" 状态、超过 1 小时没调整过价格的探针，按过去 1
    小时的真实成交量算出流速：达标就把这条探针标 "cleared"、把探针价定为
    最终零售价；不达标就调 oskill.calculate_next_probe_price 打 95 折，
    废弃旧探针、插一条新探针继续试探，同步把新价格写回 inventory_batches
    (跟 SPEC 原文一致——顾客端立刻看到新价，不是等 "cleared" 才更新)。

    两处跟 SPEC 原文不一样的地方：
      1. 用这个项目自己的 CronSchedulerEngine (on_interval 1 小时) 替掉 SPEC
         原文那个手写 while True + asyncio.sleep(3600) 的循环，风格对齐
         weather_arbitrage_engine 等其余引擎，机制不重复造轮子。
      2. 流速查询 SPEC 原文写的是 "SELECT COUNT(*) FROM orders WHERE
         batch_id = $1"——但 orders 表根本没有 batch_id 列 (批次归属信息在
         order_line_items 上，orders 只挂 cart_id)，这条 SQL 在这个项目的
         schema 下会直接报列不存在。改成 JOIN order_line_items 按 batch_id
         过滤，跟 execute_liability_routing_workflow 等函数查订单明细的方式
         一致。

    诚实的空白：SPEC 全篇没有任何地方创建"第一条" probe_order_logs 记录——
    这个引擎只处理已经存在的 'testing' 探针，谁来在批次刚上架时插入初始
    探针 (价格是多少、什么时候进入试探模式) 不在这轮 SPEC 范围内，需要外部
    先手动插入一条 probe_order_logs 才会被这个引擎捡到，跟
    execute_ambient_intake_workflow 需要批次已经预先存在是同一类"诚实空白"。
    定价直接在这个 task 里做 (oskill 调用 + 裸 SQL)，不额外包一层 omodul——
    跟 market_maker_engine (v2.0) 同一个先例：高频内部调价不是"核心业务
    事务"，不需要四大支柱的完整仪式。

    Args:
        pool: obase.persistence.PgPool。
        target_velocity: 目标流速 (单/分钟)，SPEC 原文写死 0.5，做成可配置
            但默认值一致。
    """

    async def probe_tick(**_: Any) -> dict[str, Any]:
        from obase.uuid7 import uuid7

        active_probes = await db_query_many(
            pool,
            sql=(
                "SELECT p.id AS probe_id, p.batch_id, p.probe_price_cents, b.stock_qty "
                'FROM "probe_order_log" p '
                'JOIN "inventory_batch" b ON p.batch_id = b.id '
                "WHERE p.status = 'testing' "
                "AND p.created_at < NOW() - INTERVAL '1 hour'"
            ),
        )

        cleared: list[dict[str, Any]] = []
        repriced: list[dict[str, Any]] = []

        for probe in active_probes:
            batch_id = probe["batch_id"]
            probe_id = probe["probe_id"]
            sales_row = await db_query_one(
                pool,
                sql=(
                    'SELECT COUNT(*) AS cnt FROM "order_line_item" oli '
                    'JOIN "customer_order" o ON o.id = oli.order_id '
                    "WHERE oli.batch_id = $1 "
                    "AND o.created_at > NOW() - INTERVAL '1 hour'"
                ),
                params=(batch_id,),
            )
            observed_velocity = (sales_row["cnt"] if sales_row else 0) / 60.0

            if observed_velocity >= target_velocity:
                async with pool.acquire() as conn:
                    await conn.execute(
                        'UPDATE "probe_order_log" '
                        "SET status = 'cleared', resolved_at = NOW() WHERE id = $1",
                        probe_id,
                    )
                    await conn.execute(
                        'UPDATE "inventory_batch" SET retail_price_cents = $1 WHERE id = $2',
                        probe["probe_price_cents"],
                        batch_id,
                    )
                cleared.append(
                    {
                        "batch_id": str(batch_id),
                        "probe_id": str(probe_id),
                        "final_price": probe["probe_price_cents"],
                        "observed_velocity": observed_velocity,
                    }
                )
            else:
                new_price = calculate_next_probe_price(
                    probe["probe_price_cents"],
                    observed_velocity=observed_velocity,
                    target_velocity=target_velocity,
                )
                new_probe_id = uuid7()
                async with pool.acquire() as conn:
                    await conn.execute(
                        'UPDATE "probe_order_log" '
                        "SET status = 'failed', resolved_at = NOW() WHERE id = $1",
                        probe_id,
                    )
                    await conn.execute(
                        'INSERT INTO "probe_order_log" (id, batch_id, probe_price_cents) '
                        "VALUES ($1, $2, $3)",
                        new_probe_id,
                        batch_id,
                        new_price,
                    )
                    await conn.execute(
                        'UPDATE "inventory_batch" SET retail_price_cents = $1 WHERE id = $2',
                        new_price,
                        batch_id,
                    )
                repriced.append(
                    {
                        "batch_id": str(batch_id),
                        "old_probe_id": str(probe_id),
                        "new_probe_id": new_probe_id,
                        "new_price": new_price,
                        "observed_velocity": observed_velocity,
                    }
                )

        return {
            "probes_checked": len(active_probes),
            "cleared": cleared,
            "repriced": repriced,
        }

    probe_tick.__name__ = "probe_tick"

    return CronSchedulerEngine(
        tasks=[probe_tick],
        trigger={"on_interval": 3600},
        config={"interval_seconds": 3600},
        name="ext-market-maker-probe",
    )


def build_mercenary_routing_engine(
    pool: Any, *, douyin_provider: str = "manual", access_token: str = UNCONFIGURED_TOKEN
) -> CronSchedulerEngine:
    """嗜血雇佣兵引擎 (on_interval，每 10 分钟)。

    扫全城 24 小时内到期的危险库存，按 oskill.compute_mercenary_bounty_rate
    算暴击佣金，强行覆写抖音橱窗的 SKU 数据。跟 SPEC 原文一致，这个引擎本身
    不写任何表——它只是持续把"当前应该给多少悬赏"同步给抖音端，真正的
    "谁买了、赚了多少钱"归因落账是 record_douyin_conversion_workflow 的职责
    (在订单确认后另外触发，见该 omodul 的 docstring)，这里不重复。

    SPEC 用手写 ``while True + asyncio.sleep(600)`` 循环，改用这个项目自己的
    CronSchedulerEngine (on_interval)，跟 weather_arbitrage_engine 等其余引擎
    风格一致，机制不重复造轮子。

    Args:
        pool: obase.persistence.PgPool。
        douyin_provider: 抖音网关 provider 名，默认 "manual"
            (ManualDouyinProvider，内存态占位实现)。
        access_token: 抖音开放平台 access_token。默认是显式的"未接入"哨兵
            (UNCONFIGURED_TOKEN)；传真实凭据时若给的是伪装成真凭据的占位符
            会被 :func:`_reject_mock_access_token` 挡下。
    """
    _reject_mock_access_token(access_token, "mercenary_routing")

    async def route_mercenaries(**_: Any) -> dict[str, Any]:
        danger_batches = await db_query_many(
            pool,
            sql=(
                "SELECT id, retail_price_cents, stock_qty, expiration_time "
                'FROM "inventory_batch" '
                "WHERE status = 'active' AND stock_qty > 0 "
                "AND expiration_time < NOW() + INTERVAL '24 hours'"
            ),
        )

        pushed: list[dict[str, Any]] = []
        for b in danger_batches:
            hours_to_expiry = (b["expiration_time"] - datetime.now(UTC)).total_seconds() / 3600.0
            bounty_rate = compute_mercenary_bounty_rate(hours_to_expiry)
            if bounty_rate <= 0:
                continue

            response = await ext_douyin_sync_inventory(
                douyin_provider,
                sku_id=str(b["id"]),
                price=b["retail_price_cents"],
                stock=b["stock_qty"],
                commission_rate=bounty_rate,
                access_token=access_token,
            )
            pushed.append(
                {
                    "batch_id": str(b["id"]),
                    "bounty_rate": bounty_rate,
                    "hours_to_expiry": hours_to_expiry,
                    "response": response,
                }
            )

        return {"scanned": len(danger_batches), "pushed": pushed}

    route_mercenaries.__name__ = "route_mercenaries"

    return CronSchedulerEngine(
        tasks=[route_mercenaries],
        trigger={"on_interval": 600},
        config={"interval_seconds": 600},
        name="ext-mercenary-routing",
    )


def build_affiliate_settlement_engine(
    pool: Any, *, payout_provider: str = "manual"
) -> CronSchedulerEngine:
    """夜间分润清算引擎 (on_cron，每日凌晨 2 点)。

    捞出所有 settlement_status='pending' 的分润记录，按 douyin_uid 聚合
    (满 1 元起结，跟 SPEC 原文一致)，逐个调 ext_pay_transfer 真实转账，成功
    后核销对应记录。转账失败的 douyin_uid 如实记进 failed 列表，不悄悄吞掉——
    留到下一次 tick 自然重试 (settlement_status 没被改成 settled，下次
    还会被同一条 SQL 捞到)。

    定价/做市类引擎 (market_maker_engine / market_maker_probe_engine) 高频
    内部调价不需要完整四支柱 omodul 仪式；这里是"钱真的要转出去"的结算动作，
    但 SPEC 本身描述的就是"批量聚合打款"而不是"单笔业务事务"，跟
    node_host_settlement_engine 遍历 worker 逐个调 dispatch_labor_payment
    (那是有专门 omodul 的) 不同——这里没有一个"聚合抖音分润并打款"的独立
    omodul，直接在 task 里做 (oprim 调用 + 裸 SQL)，理由同 market_maker_probe_
    engine：高频/批量的内部结算轮询，不是需要完整 decision_trail/fingerprint
    审计的核心业务事务 (每一笔分润本身已经在 record_douyin_conversion_
    workflow 落过账，有完整审计轨迹，这里只是把已经落账的记录批量兑现)。

    Args:
        pool: obase.persistence.PgPool。
        payout_provider: 打款 provider 名，默认 "manual" (ManualPayoutProvider)。
    """

    async def settle_affiliates(**_: Any) -> dict[str, Any]:
        payouts = await db_query_many(
            pool,
            sql=(
                "SELECT douyin_uid, SUM(dividend_amount_cents) AS total_payout "
                'FROM "douyin_conversion_log" '
                "WHERE settlement_status = 'pending' "
                "GROUP BY douyin_uid "
                "HAVING SUM(dividend_amount_cents) > 100"
            ),
        )

        settled: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for p in payouts:
            try:
                transfer_result = await ext_pay_transfer(
                    payout_provider,
                    account=p["douyin_uid"],
                    amount=p["total_payout"],
                )
                async with pool.acquire() as conn:
                    await conn.execute(
                        'UPDATE "douyin_conversion_log" '
                        "SET settlement_status = 'settled' "
                        "WHERE douyin_uid = $1 AND settlement_status = 'pending'",
                        p["douyin_uid"],
                    )
                settled.append(
                    {
                        "douyin_uid": p["douyin_uid"],
                        "amount": p["total_payout"],
                        "transfer_id": transfer_result["transfer_id"],
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 单个达人打款失败不该中断整批结算
                failed.append({"douyin_uid": p["douyin_uid"], "error": str(exc)})

        return {"settled": settled, "failed": failed}

    settle_affiliates.__name__ = "settle_affiliates"

    return CronSchedulerEngine(
        tasks=[settle_affiliates],
        trigger={"on_cron": "0 2 * * *"},
        config={"interval_seconds": 86400},
        name="ext-affiliate-settlement",
    )


def build_inventory_decay_engine(pool: Any, *, settings: Settings) -> CronSchedulerEngine:
    """Theta 衰减引信引擎 (on_interval，每 1 小时)。

    补天计划 Task 2.2：对过期时间强制轮询——一旦 NOW() > expiration_time，
    强制注入 omodul.scrap_batch_inventory 报损清零 (status + 库存同时归零)，
    防止腐坏商品流入前端。

    跟 inventory_reaper_engine (半夜 2:00 正式销毁仪式) 是两条互补防线：reaper
    走 mark_batch_for_disposal (只改 status，库存数字保留审计)，decay 引擎走
    scrap_batch_inventory (报损清零)，任何一条先命中后另一条的 SQL 都捞不到
    该批次 (status 已非 active)，天然幂等不重复处理。1 小时粒度保证过期后
    最多滞留一个 tick；真正把腐坏商品挡在顾客面前的第一道防线是
    get_nearby_feed 的安全货架期过滤 (oskill.is_shelf_life_safe，防御纵深，
    不依赖引擎及时性)。

    Args:
        pool: obase.persistence.PgPool。
        settings: Settings (output_root / notification provider)。
    """

    async def decay_tick(**_: Any) -> dict[str, Any]:
        now = datetime.now(UTC)
        expired = await db_query_many(
            pool,
            sql=(
                "SELECT id, stock_qty, reserved_qty, location_id "
                'FROM "inventory_batch" '
                "WHERE status = 'active' AND expiration_time IS NOT NULL "
                "AND expiration_time <= $1 AND stock_qty - reserved_qty > 0"
            ),
            params=(now,),
        )

        scrapped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for batch in expired:
            batch_id = str(batch["id"])
            try:
                result = await scrap_batch_inventory(
                    ScrapBatchInventoryConfig(),
                    ScrapBatchInventoryInput(batch_id=batch_id, reason="expired"),
                    _output_dir(settings, "inventory_decay_engine", "scrap_batch_inventory"),
                    pool=pool,
                )
                if result["status"] == "completed":
                    scrapped.append(
                        {
                            "batch_id": batch_id,
                            "scrapped_qty": result["scrapped_qty"],
                            "location_id": str(batch["location_id"]),
                        }
                    )
                else:
                    errors.append({"batch_id": batch_id, "error": result["error"]})
            except Exception as exc:  # noqa: BLE001 - 单个批次报损失败不中断整个 tick
                errors.append({"batch_id": batch_id, "error": str(exc)})

        return {"checked": len(expired), "scrapped": scrapped, "errors": errors}

    decay_tick.__name__ = "decay_tick"

    return CronSchedulerEngine(
        tasks=[decay_tick],
        trigger={"on_interval": 3600},
        config={"interval_seconds": 3600},
        name="ext-inventory-decay",
    )


def build_competitor_spider_engine(
    pool: Any,
    *,
    spider_provider: str = "layered",
    radius_km: int = 5,
    max_calls_per_tick: int = 200,
) -> CronSchedulerEngine:
    """竞对价格爬虫入库引擎 (on_cron，每日凌晨 3:00)。

    补天计划 Task 3.2：读取所有 active 的 SKU (product_variant JOIN product)，
    对每个 active 微仓坐标调用 oprim.spider_fetch_competitor_prices，把抓到的
    商超 O2O 价格经 oskill.normalize_sku_price 归一化 (分/克) 后写入
    price_benchmark (source_type='spider')，让做市预言机开始自动吐出数据——
    submit_supplier_reverse_auction_workflow 的基准线 / 众包 OCR 核销从此有
    了自动来源。

    Phase 7 Task 2 升级：默认 provider 从 "manual" (永远空转) 换成
    "layered" (app.ext.spider_targets)——对关键词发真实 HTTP 请求 (苏宁移动
    搜索 / 京东到家 / OpenFoodFacts)，真实解析后入库。诚实的空白依然成立：
    免费源的价格字段覆盖率决定真实写入率，抓不到价就如实记 0，不伪造。
    tick 返回新增 requests/parsed/priced 统计，方便看板核对真实抓取状态。

    SKU↔抓取条目匹配：抓取返回 {"item", "price", "unit", "store"}，item 是
    商品名自由文本，跟 SKU 没有稳定键——按 SKU 标题做大小写不敏感子串匹配，
    匹配不上就存 variant_id=NULL + raw_item_name 原文留痕 (跟 price_benchmark
    表设计一致)，不假装存在一个不存在的商品关联。

    Args:
        pool: obase.persistence.PgPool。
        spider_provider: 爬虫 provider 名，默认 "layered"。
        radius_km: 抓取半径 (公里)。
        max_calls_per_tick: 单 tick 抓取调用上限，防止 SKU×节点组合爆炸
            (真实爬虫有成本和速率限制，这里先做硬上限)。
    """

    async def spider_tick(**_: Any) -> dict[str, Any]:
        variants = await db_query_many(
            pool,
            sql=(
                "SELECT v.id, v.sku_code, p.title "
                'FROM "product_variant" v '
                'JOIN "product" p ON p.id = v.product_id '
                # 商品状态与商城口径一致 (published) 而非 active——生产库里
                # 商品全是 published (storefront 查询也用 published)，用
                # active 会永远扫 0 个 SKU。
                "WHERE v.status = 'active' AND p.status = 'published'"
            ),
        )
        locations = await db_query_many(
            pool,
            sql=(
                'SELECT id, lat, lng FROM "stock_location" '
                "WHERE status = 'active' AND lat IS NOT NULL AND lng IS NOT NULL"
            ),
        )
        if not variants or not locations:
            return {"scanned_variants": len(variants), "written": 0, "calls": 0}

        from obase.uuid7 import uuid7

        written = 0
        calls = 0
        parsed = 0
        priced = 0
        for loc in locations:
            for variant in variants:
                if calls >= max_calls_per_tick:
                    break
                calls += 1
                try:
                    items = await spider_fetch_competitor_prices(
                        spider_provider,
                        lat=float(loc["lat"]),
                        lon=float(loc["lng"]),
                        radius_km=radius_km,
                        keywords=[variant["title"], variant["sku_code"]],
                    )
                except Exception as exc:  # noqa: BLE001 - 单次抓取失败不中断整批
                    logger.warning(
                        "spider fetch failed: variant=%s loc=%s: %s",
                        variant["id"],
                        loc["id"],
                        exc,
                    )
                    continue

                for item in items:
                    parsed += 1
                    raw_price = float(item.get("price", 0) or 0)
                    if raw_price <= 0:
                        continue
                    priced += 1
                    raw_unit = str(item.get("unit", "") or "")
                    title = str(item.get("item", "") or "")
                    if not raw_unit:
                        raw_unit = extract_unit_from_text(title)
                    title = str(item.get("item", "") or "")
                    match = (
                        title.strip().lower() == variant["title"].strip().lower()
                        or variant["title"].strip().lower() in title.strip().lower()
                    )
                    normalized = normalize_sku_price(int(round(raw_price * 100)), raw_unit=raw_unit)
                    async with pool.acquire() as conn:
                        await conn.execute(
                            'INSERT INTO "price_benchmark" '
                            "(id, variant_id, raw_item_name, source_type, competitor_name, "
                            " raw_price_cents, raw_unit, normalized_price_per_unit) "
                            "VALUES ($1, $2, $3, 'spider', $4, $5, $6, $7)",
                            uuid7(),
                            variant["id"] if match else None,
                            title[:128],
                            str(item.get("store", "") or "")[:64],
                            int(round(raw_price * 100)),
                            raw_unit[:20],
                            normalized,
                        )
                    written += 1

        return {
            "scanned_variants": len(variants),
            "scanned_locations": len(locations),
            "calls": calls,
            "parsed": parsed,
            "priced": priced,
            "written": written,
        }

    spider_tick.__name__ = "spider_tick"

    # 每次 tick 落一行抓取状态 (Phase 7 Task 2 汇报口径: 真实请求数/解析数/
    # 有价数/入库数)，看板/日志直接核对爬虫是不是在空转。
    async def spider_tick_logged(**_: Any) -> dict[str, Any]:
        result = await spider_tick()
        logger.info(
            "spider engine tick: scanned_variants=%s scanned_locations=%s calls=%s "
            "parsed=%s priced=%s written=%s",
            result.get("scanned_variants"),
            result.get("scanned_locations"),
            result.get("calls"),
            result.get("parsed"),
            result.get("priced"),
            result.get("written"),
        )
        return result

    spider_tick_logged.__name__ = "spider_tick_logged"

    return CronSchedulerEngine(
        tasks=[spider_tick_logged],
        trigger={"on_cron": "0 3 * * *"},
        config={"interval_seconds": 86400},
        name="ext-competitor-spider",
    )


# ── 履约 SLA (P1 冲刺: 时效承诺 + 超时赔付) ─────────────────────────────


def build_sla_promise_engine(pool: Any, settings: Settings) -> CronSchedulerEngine:
    """时效承诺回填引擎 (on_interval，每小时)。

    对"已支付但还没有 promised_delivery_at"的订单按配送方式计算承诺：
        wave → 下一班车 (10:00/16:00) + 2h；express → 支付时刻 + 2h；
        pickup → 支付时刻 + 1h。
    不依赖结算链路改造 (共享 checkout omodul 不动)，回填式把承诺补进订单流；
    订单列表查询直接吐 promised_delivery_at (见 queries.list_customer_orders)。
    """

    async def backfill_promises(**_: Any) -> dict[str, Any]:
        from datetime import datetime

        rows = await db_query_many(
            pool,
            sql=(
                'SELECT id, shipping_cents, created_at FROM "customer_order" '
                "WHERE status = 'paid' AND promised_delivery_at IS NULL"
            ),
        )
        if not rows:
            return {"promises_backfilled": 0, "orders": []}

        now = datetime.now(UTC)
        updated: list[dict[str, Any]] = []
        async with pool.acquire() as conn:
            for row in rows:
                sla = compute_delivery_sla(
                    now,
                    shipping_cents=row["shipping_cents"],
                    paid_at=row["created_at"] or now,
                )
                await conn.execute(
                    'UPDATE "customer_order" SET promised_delivery_at = $1 WHERE id = $2',
                    sla["promised_at"],
                    row["id"],
                )
                updated.append(
                    {
                        "order_id": str(row["id"]),
                        "shipping_type": sla["shipping_type"],
                        "promised_at": sla["promised_at"].isoformat(),
                        "compensation_cents": sla["compensation_cents"],
                    }
                )
        return {"promises_backfilled": len(updated), "orders": updated}

    backfill_promises.__name__ = "backfill_promises"

    return CronSchedulerEngine(
        tasks=[backfill_promises],
        trigger={"on_interval": 3600},
        config={"interval_seconds": 3600},
        name="ext-sla-promise",
    )


def build_sla_compensation_engine(pool: Any, settings: Settings) -> CronSchedulerEngine:
    """履约超时赔付引擎 (on_interval，每小时)。

    扫描 promised_delivery_at 已过、尚未赔付的已支付订单，向顾客
    customer.system_balance 注入定额算力金 (SLA_COMPENSATION_CENTS = 400 分)，
    并置 sla_compensated_at 幂等标记——一单一赔，重复 tick 不重复赔付。
    赔付走 system_balance 1:1 (架构师禁区合规：无优惠券/折扣，只加余额)。
    """

    async def compensate_late_orders(**_: Any) -> dict[str, Any]:
        overdue = await db_query_many(
            pool,
            sql=(
                "SELECT o.id AS order_id, o.customer_id AS customer_id "
                'FROM "customer_order" o '
                "WHERE o.promised_delivery_at IS NOT NULL "
                "AND o.promised_delivery_at < NOW() "
                "AND o.status NOT IN ('canceled', 'refunded') "
                "AND o.sla_compensated_at IS NULL"
            ),
        )
        if not overdue:
            return {"compensated": 0, "total_cents": 0, "orders": []}

        compensated: list[str] = []
        async with pool.acquire() as conn:
            for row in overdue:
                await conn.execute(
                    'UPDATE "customer" SET system_balance = system_balance + $1 WHERE id = $2',
                    SLA_COMPENSATION_CENTS,
                    row["customer_id"],
                )
                await conn.execute(
                    'UPDATE "customer_order" SET sla_compensated_at = NOW() WHERE id = $1',
                    row["order_id"],
                )
                compensated.append(str(row["order_id"]))

        total = len(compensated) * SLA_COMPENSATION_CENTS
        logger.info(
            "sla compensation: %d orders, %d cents credited to system_balance",
            len(compensated),
            total,
        )
        return {
            "compensated": len(compensated),
            "total_cents": total,
            "orders": compensated,
            "per_order_cents": SLA_COMPENSATION_CENTS,
        }

    compensate_late_orders.__name__ = "compensate_late_orders"

    return CronSchedulerEngine(
        tasks=[compensate_late_orders],
        trigger={"on_interval": 3600},
        config={"interval_seconds": 3600},
        name="ext-sla-compensation",
    )


# ── 微信平台证书轮换 (Phase 7 Task 1: 消灭"到期运维手动更新") ─────────────


def build_wechat_cert_rotation_engine(pool: Any, settings: Settings) -> CronSchedulerEngine:
    """平台证书自轮换守护引擎 (on_cron，每 12 小时)。

    微信 v3 平台证书会过期，传统做法是运维到期手动拉新证书替换；本引擎把
    这条链路自动化：

        1. 从 ProviderRegistry 解析当前付款网关 (按 HEMALL_PAYMENT_GATEWAY_PROVIDER
           注册名)；
        2. 非 wechat 网关 / 密钥未配齐 → 诚实跳过并记录原因 (不假装轮换)；
        3. GET /v3/certificates (商户私钥 RSA-SHA256 签名) → 解密 encrypt_
           certificate (AEAD_AES_256_GCM, APIv3 密钥) → 解析 PEM 平台证书；
        4. 取 expire_time 最新的一张 → 热更新到内存网关 (rotate_platform_cert，
           无重启生效) + 原子写 settings.wechat_platform_cert_store 持久化
           (JSON: serial_no/expire_time/pem)；
        5. 返回轮换结果留痕 (fetched/rotated_to/expire_time/stored)。

    pool 参数保留给未来持久化轮换轨迹 (cert_rotation_log) 用，当前版本不做
    额外建表——轮换本身只是内存热换 + 文件持久化，不产生业务数据。
    """
    import json as _json
    import tempfile

    def _load_gateway() -> tuple[str, Any] | None:
        from obase.provider_registry import ProviderRegistry

        reg = ProviderRegistry.get()
        name = settings.payment_gateway_provider
        try:
            gw = reg.generic("payment_gateway", name)
        except Exception:  # noqa: BLE001 - 未注册时按未配置处理
            gw = None
        if gw is None or not getattr(gw, "is_configured", False):
            return None
        return name, gw

    async def cert_rotation_tick(**_: Any) -> dict[str, Any]:
        from .payment_gateways import (
            WechatPayNativeGateway,
            fetch_wechat_platform_certificates,
        )

        loaded = _load_gateway()
        if loaded is None:
            logger.info(
                "wechat cert rotation skipped: gateway '%s' not wechat or not configured",
                settings.payment_gateway_provider,
            )
            return {
                "rotated": False,
                "reason": "gateway not wechat or not configured",
                "provider": settings.payment_gateway_provider,
            }
        name, gateway = loaded
        if not isinstance(gateway, WechatPayNativeGateway):
            logger.info(
                "wechat cert rotation skipped: provider '%s' is %s",
                name,
                type(gateway).__name__,
            )
            return {"rotated": False, "reason": f"gateway type {type(gateway).__name__}"}

        certs = await fetch_wechat_platform_certificates(gateway)
        if not certs:
            logger.warning("wechat cert rotation: /v3/certificates returned no certs")
            return {"rotated": False, "reason": "no certificates returned", "fetched": 0}

        # 新旧并存窗口期响应多张证书：取 expire_time 最新的一张 (RFC3339 字符串
        # 字典序即时间序)。
        newest = max(certs, key=lambda c: c.expire_time)
        gateway.rotate_platform_cert(newest.pem, serial_no=newest.serial_no)

        # 原子持久化 (先写临时文件再 rename，避免中途崩溃留下半个 JSON)。
        store_path = Path(settings.wechat_platform_cert_store)
        store_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=store_path.parent, delete=False, encoding="utf-8"
        ) as tmp:
            _json.dump(newest.to_dict(), tmp, ensure_ascii=False)
            tmp_path = tmp.name
        import os

        os.replace(tmp_path, store_path)

        logger.info(
            "wechat cert rotated: serial=%s expire=%s stored=%s (fetched=%d)",
            newest.serial_no,
            newest.expire_time,
            store_path,
            len(certs),
        )
        return {
            "rotated": True,
            "fetched": len(certs),
            "rotated_to": newest.serial_no,
            "effective_time": newest.effective_time,
            "expire_time": newest.expire_time,
            "stored": str(store_path),
        }

    cert_rotation_tick.__name__ = "cert_rotation_tick"

    async def cert_rotation_tick_logged(**_: Any) -> dict[str, Any]:
        result = await cert_rotation_tick()
        logger.info("wechat cert rotation tick: %s", result)
        return result

    cert_rotation_tick_logged.__name__ = "cert_rotation_tick_logged"

    return CronSchedulerEngine(
        tasks=[cert_rotation_tick_logged],
        trigger={"on_cron": "0 */12 * * *"},
        config={"interval_seconds": 43200},
        name="ext-wechat-cert-rotation",
    )


# ── 蜘蛛自动上架 + VLM 截图填价 ──────────────────────────────────────

#: 自动上架来源短名 (进 product.slug / variant.sku_code 幂等键)。
_SPIDER_LISTING_SOURCES = ("suning", "off")

#: 蜘蛛填价 advisory lock 的 key1 (本管线固定常数, 与其它管线的锁命名
#: 空间隔离); key2 取 hashtext(variant_id::text), 按变体粒度串行化。
_SPIDER_PRICING_LOCK_KEY1 = 0x53505243  # "SPRC"

#: 标题关键词 → 分类名映射 (按顺序优先匹配; 分类名不在库里时整条跳过)。
#: 顺序按"具体品类先于泛品类": 蛋糕先归乳品烘焙而不是被"蛋"带去肉禽蛋,
#: 果汁先归酒水饮料而不是时令水果。库里实际分类以 product_category 为准
#: (种子: 蔬菜豆品/时令水果/肉禽蛋/海鲜水产/粮油调味/乳品烘焙/冷冻速食/
#: 休闲零食/酒水饮料/日用百货), 映射不到时归库里第一个分类。
_SPIDER_LISTING_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("冷冻速食", ("速冻", "冷冻", "冰淇淋", "冰激凌", "雪糕", "汤圆", "水饺", "馄饨")),
    (
        "酒水饮料",
        (
            "白酒",
            "啤酒",
            "红酒",
            "葡萄酒",
            "饮料",
            "可乐",
            "雪碧",
            "果汁",
            "矿泉水",
            "纯净水",
            "苏打水",
            "气泡水",
            "咖啡",
            "茶饮",
        ),
    ),
    (
        "乳品烘焙",
        (
            "牛奶",
            "酸奶",
            "奶酪",
            "芝士",
            "黄油",
            "奶油",
            "鲜奶",
            "豆奶",
            "豆浆",
            "面包",
            "蛋糕",
            "吐司",
            "烘焙",
        ),
    ),
    (
        "海鲜水产",
        (
            "海鲜",
            "虾",
            "蟹",
            "带鱼",
            "鲈鱼",
            "黄鱼",
            "三文鱼",
            "鳕鱼",
            "扇贝",
            "生蚝",
            "鲍鱼",
            "海参",
            "鱿鱼",
            "章鱼",
            "蛤蜊",
            "鲜鱼",
            "活鱼",
        ),
    ),
    (
        "肉禽蛋",
        (
            "牛肉",
            "猪肉",
            "羊肉",
            "鸡肉",
            "鸭肉",
            "牛排",
            "排骨",
            "鸡蛋",
            "鸭蛋",
            "鹅蛋",
            "皮蛋",
            "咸蛋",
            "火腿",
            "香肠",
            "腊肉",
        ),
    ),
    (
        "时令水果",
        (
            "苹果",
            "草莓",
            "香蕉",
            "橙子",
            "柑橘",
            "橘子",
            "梨",
            "桃子",
            "葡萄",
            "西瓜",
            "芒果",
            "榴莲",
            "蓝莓",
            "樱桃",
            "车厘子",
            "猕猴桃",
            "柠檬",
            "柚子",
            "水果",
            "鲜果",
        ),
    ),
    (
        "蔬菜豆品",
        (
            "西红柿",
            "番茄",
            "土豆",
            "黄瓜",
            "萝卜",
            "茄子",
            "辣椒",
            "豆腐",
            "豆芽",
            "豆皮",
            "菌菇",
            "蘑菇",
            "菠菜",
            "白菜",
            "南瓜",
            "蔬菜",
            "青菜",
            "玉米",
            "红薯",
            "山药",
            "莲藕",
        ),
    ),
    (
        "粮油调味",
        (
            "大米",
            "小米",
            "面粉",
            "食用油",
            "橄榄油",
            "花生油",
            "菜籽油",
            "酱油",
            "陈醋",
            "香醋",
            "调味",
            "五谷",
            "杂粮",
            "燕麦",
            "挂面",
            "糯米",
            "红豆",
            "绿豆",
            "食盐",
            "白糖",
        ),
    ),
    (
        "休闲零食",
        (
            "零食",
            "饼干",
            "薯片",
            "巧克力",
            "糖果",
            "坚果",
            "瓜子",
            "辣条",
            "果冻",
            "膨化",
            "蜜饯",
            "话梅",
            "方便面",
            "肉脯",
            "海苔",
        ),
    ),
    (
        "日用百货",
        (
            "纸巾",
            "卫生纸",
            "牙膏",
            "牙刷",
            "洗发",
            "沐浴露",
            "洗衣液",
            "洗洁精",
            "毛巾",
            "口罩",
            "垃圾袋",
            "电池",
            "湿巾",
            "抽纸",
        ),
    ),
)


def _map_spider_listing_category(title: str, categories: list[dict[str, Any]]) -> Any:
    """按标题关键词把外部商品映射到库里已有分类 id; 映射不到返回 None。"""
    by_name = {str(c.get("name") or ""): c.get("id") for c in categories}
    for cat_name, keywords in _SPIDER_LISTING_CATEGORY_RULES:
        if cat_name in by_name and any(kw in title for kw in keywords):
            return by_name[cat_name]
    return None


def build_spider_listing_engine(
    pool: Any,
    *,
    settings: Settings,
    spider_provider: str = "layered",
) -> CronSchedulerEngine:
    """蜘蛛自动上架 + VLM 截图填价引擎 (on_cron, 每日 04:30)。

    Soffy 指令: 价格不再走源站价格接口 (苏宁价格 JSONP 只回信封、京东
    到家 JS 壳、OFP 价格字段覆盖率近零——三源实测都无价)。改为两段:

      填价段 (先行, 补历史未定价): 未定价集合 = slug LIKE 'ext-%' 且无
      active 批次的商品。逐个拿 variant option_values 里的 detail_url
      调截图服务 (settings.vlm_screenshot_url, GET ?url= 带 X-Shot-Token
      → PNG), oprim.vlm_extract_price 让本地 VLM 看截图读价; 校验
      (非空、0.01<=价<=99999、confidence>=0.5) 通过后一个事务里建
      inventory_batch (active 库位按商品序轮转、默认库存、成本=售价
      60%、72h vwap 消化曲线、media=商品图) + product 翻 published +
      price_benchmark 落一行 (source_type='spider_vlm')。
      vlm_screenshot_url 未配置 (空串) 时整段跳过, tick 里
      skipped_no_screenshot=True 留痕, 等服务配好下一轮自动补价。

      上架段: 遍历 settings.spider_listing_keywords, 每词调
      oprim.spider_fetch_products 抓商品本体 (标题/图片/介绍/详情页),
      取前 spider_listing_max_per_keyword 个未上架新品。幂等键
      product.slug = "ext-{source}-{source_id}" (source: suning/off),
      variant sku_code = "EXT-{source}-{source_id}"; option_values 记
      {"来源", "detail_url", "source_id", "image_url"} 供填价段回读;
      reference_price_cents 留 NULL (未定价不用 0 冒充); description =
      介绍 + "\\n来源:{store} {detail_url}"; 分类按标题关键词映射既有
      分类名, 映射不到归第一个分类。

    状态选择 (storefront 行为核查结论): queries.list_storefront_products
    与 get_storefront_product 都硬性要求 status='published' 且
    total_stock>0 (存在有可用量的 active 批次), 加购/结算也只认批次——
    无批次商品在前台既不展示也买不了 (天然"已建档不可购买")。因此新品
    直接以 draft 建档, 只是多一层保险: 后台/报表口径里未定价商品不会
    混进 published 集合; 填价建批次后才翻 published 正式开卖。

    单品失败 try/except 计数不中断整轮; tick 返回
    {"listed", "priced", "skipped_no_screenshot", "failed",
    "skipped_has_batch"}。
    """

    async def _price_one(row: dict[str, Any], location_id: Any, stats: dict[str, Any]) -> None:
        import json

        import httpx
        from obase.uuid7 import uuid7

        option_values = row.get("option_values") or {}
        if isinstance(option_values, str):
            try:
                option_values = json.loads(option_values)
            except (json.JSONDecodeError, ValueError):
                option_values = {}
        detail_url = str(option_values.get("detail_url") or "")
        image_url = str(option_values.get("image_url") or "")
        store = str(option_values.get("来源") or "")
        if not detail_url:
            raise ValueError("spider listing: variant option_values missing detail_url")

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                settings.vlm_screenshot_url,
                params={"url": detail_url},
                headers={"X-Shot-Token": settings.vlm_screenshot_token},
            )
            resp.raise_for_status()
            png = resp.content
        verdict = await vlm_extract_price(settings.default_vlm_provider, image_bytes=png)
        price_yuan = verdict.get("price_yuan")
        confidence = float(verdict.get("confidence") or 0.0)
        if price_yuan is None or not (0.01 <= float(price_yuan) <= 99999) or confidence < 0.5:
            raise ValueError(f"spider listing: vlm price not usable: {verdict}")

        retail = int(round(float(price_yuan) * 100))
        cost = int(round(retail * 0.6))
        stock_qty = int(settings.spider_listing_default_stock)
        batch_no = (
            f"EXT-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{str(uuid7()).replace('-', '')[:8]}"
        )
        media_json = json.dumps([image_url] if image_url else [])
        curve_json = json.dumps(build_default_vwap_curve(stock_qty))
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    'INSERT INTO "inventory_batch" '
                    "(id, variant_id, location_id, batch_no, video_url, "
                    "cost_price_cents, retail_price_cents, stock_qty, currency, "
                    "media_assets, shelf_image_url, vwap_target_curve, status) "
                    "VALUES ($1, $2, $3, $4, '', $5, $6, $7, 'CNY', "
                    "$8::jsonb, $8::jsonb, $9::jsonb, 'active')",
                    uuid7(),
                    row["variant_id"],
                    location_id,
                    batch_no,
                    cost,
                    retail,
                    stock_qty,
                    media_json,
                    curve_json,
                )
                await conn.execute(
                    "UPDATE \"product\" SET status = 'published' WHERE id = $1",
                    row["product_id"],
                )
                await conn.execute(
                    'INSERT INTO "price_benchmark" '
                    "(id, variant_id, raw_item_name, source_type, competitor_name, "
                    " raw_price_cents, raw_unit, normalized_price_per_unit) "
                    "VALUES ($1, $2, $3, 'spider_vlm', $4, $5, 'item', NULL)",
                    uuid7(),
                    row["variant_id"],
                    str(row.get("title") or "")[:128],
                    store[:64],
                    retail,
                )
        stats["priced"] += 1

    async def _price_one_locked(
        row: dict[str, Any], location_id: Any, stats: dict[str, Any]
    ) -> None:
        """填价单个变体: PG advisory lock 跨进程串行化 + 锁内复查。

        生产 api 是多 worker 部署, 每个 worker 都装配本引擎且 tick 时点
        相同: 无防护时同一变体会被多个 worker 并发建 EXT- 批次 (库存
        虚增数倍)。这里按变体取会话级 advisory lock: key1 = 本管线固定
        常数 _SPIDER_PRICING_LOCK_KEY1, key2 = hashtext(variant_id
        文本); 同一变体同一时刻只允许一个 worker 进入填价。

        锁绑定在连接上: worker 进程死亡时连接关闭, PG 自动释放锁, 不
        留死锁; 但连接归还连接池后会被复用, 所以必须在 finally 里显
        式 unlock, 不能指望靠关连接解。unlock 自身失败 (连接已断) 时
        吞掉异常 —— 断连即释放, 语义不变。

        取锁后先在锁连接上复查该变体是否已有批次: 待填价集合是本轮
        开始时的一次性快照, 别的 worker 可能已在本轮内建好。复查口
        径与 _price_pending 待填价查询的 NOT EXISTS 一致 (active 且未
        软删); 命中则跳过该行, 计 skipped_has_batch (不算 priced,
        也不算 failed)。
        """
        variant_key = str(row["variant_id"])
        async with pool.acquire() as lock_conn:
            await lock_conn.execute(
                "SELECT pg_advisory_lock($1, hashtext($2))",
                _SPIDER_PRICING_LOCK_KEY1,
                variant_key,
            )
            try:
                existing = await lock_conn.fetchval(
                    'SELECT 1 FROM "inventory_batch" '
                    "WHERE variant_id = $1 AND status = 'active' "
                    "AND deleted_at IS NULL LIMIT 1",
                    row["variant_id"],
                )
                if existing is not None:
                    stats["skipped_has_batch"] += 1
                    return
                await _price_one(row, location_id, stats)
            finally:
                try:
                    await lock_conn.execute(
                        "SELECT pg_advisory_unlock($1, hashtext($2))",
                        _SPIDER_PRICING_LOCK_KEY1,
                        variant_key,
                    )
                except Exception:  # noqa: BLE001 - 连接已断时锁随连接关闭释放
                    pass

    async def _price_pending(stats: dict[str, Any]) -> None:
        stats.setdefault("skipped_has_batch", 0)
        rows = await db_query_many(
            pool,
            sql=(
                "SELECT p.id AS product_id, p.title AS title, "
                "v.id AS variant_id, v.option_values AS option_values "
                'FROM "product" p '
                'JOIN "product_variant" v ON v.product_id = p.id '
                "AND v.status = 'active' AND v.deleted_at IS NULL "
                "WHERE p.deleted_at IS NULL AND p.slug LIKE 'ext-%' "
                "AND NOT EXISTS ("
                'SELECT 1 FROM "inventory_batch" b '
                "WHERE b.variant_id = v.id AND b.status = 'active' "
                "AND b.deleted_at IS NULL"
                ") "
                "ORDER BY p.created_at DESC"
            ),
        )
        if not rows:
            return
        locations = await db_query_many(
            pool,
            sql=("SELECT id FROM \"stock_location\" WHERE status = 'active' ORDER BY created_at"),
        )
        if not locations:
            stats["failed"] += len(rows)
            logger.warning(
                "spider listing: %d unpriced products but no active stock_location",
                len(rows),
            )
            return
        # 单轮尝试上限 (settings.spider_listing_max_price_per_tick): 老积压
        # 多为源页无价的结构性失败, 无上限时单轮能跑数小时。配合上面的
        # created_at DESC 新品优先, 保证当天上新当天开卖; 积压在队列短于
        # 上限时仍会被处理到。尝试数按 priced+failed 计 (成功失败都消耗
        # 一次截图+VLM 调用)。
        max_attempts = int(settings.spider_listing_max_price_per_tick)
        attempts = 0
        for idx, row in enumerate(rows):
            if attempts >= max_attempts:
                logger.info(
                    "spider listing: pricing cap %d reached, %d products left for next tick",
                    max_attempts,
                    len(rows) - idx,
                )
                break
            attempts += 1
            try:
                await _price_one_locked(row, locations[idx % len(locations)]["id"], stats)
            except asyncpg.UniqueViolationError:
                # 纵深防御兜底: EXT 批次部分唯一索引 (p0_schema 的
                # uq_inventory_batch_spider_ext_variant) 触发 —— 另一
                # worker 已抢先建好该变体批次, 按竞态跳过计, 不算失败。
                stats["skipped_has_batch"] += 1
                logger.info(
                    "spider listing price skipped (ext batch race) product=%s",
                    row.get("product_id"),
                )
            except Exception as exc:  # noqa: BLE001 - 单品填价失败不中断整轮
                stats["failed"] += 1
                logger.warning(
                    "spider listing price failed product=%s: %s",
                    row.get("product_id"),
                    exc,
                )

    async def _create_listing(
        item: dict[str, Any],
        *,
        slug: str,
        categories: list[dict[str, Any]],
        fallback_category_id: Any,
    ) -> None:
        import json

        source = str(item.get("source") or "")
        source_id = str(item.get("source_id") or "")
        title = str(item.get("item") or "").strip()[:120]
        store = str(item.get("store") or "")
        detail_url = str(item.get("detail_url") or "")
        image_url = str(item.get("image_url") or "")
        intro = str(item.get("description") or "").strip()
        description = (f"{intro}\n" if intro else "") + f"来源：{store} {detail_url}"
        category_id = _map_spider_listing_category(title, categories) or fallback_category_id
        option_values = {
            "来源": store,
            "detail_url": detail_url,
            "source_id": source_id,
            "image_url": image_url,
        }
        async with pool.acquire() as conn:
            async with conn.transaction():
                pid = await conn.fetchval(
                    'INSERT INTO "product" (id, title, slug, description, category_id, status) '
                    "VALUES (gen_random_uuid(), $1, $2, $3, $4, 'draft') RETURNING id",
                    title,
                    slug,
                    description,
                    category_id,
                )
                await conn.fetchval(
                    'INSERT INTO "product_variant" (id, product_id, sku_code, option_values, '
                    "reference_price_cents, status) "
                    "VALUES (gen_random_uuid(), $1, $2, $3::jsonb, NULL, 'active') RETURNING id",
                    pid,
                    f"EXT-{source}-{source_id}"[:128],
                    json.dumps(option_values, ensure_ascii=False),
                )

    async def _list_new(stats: dict[str, Any]) -> None:
        categories = await db_query_many(
            pool,
            sql=(
                'SELECT id, name FROM "product_category" '
                "WHERE deleted_at IS NULL ORDER BY created_at"
            ),
        )
        fallback_category_id = categories[0]["id"] if categories else None
        for keyword in settings.spider_listing_keywords:
            try:
                products = await spider_fetch_products(spider_provider, keywords=[keyword])
            except Exception as exc:  # noqa: BLE001 - 单关键词抓取失败不中断整轮
                stats["failed"] += 1
                logger.warning("spider listing fetch failed keyword=%s: %s", keyword, exc)
                continue
            taken = 0
            for item in products:
                if taken >= settings.spider_listing_max_per_keyword:
                    break
                source = str(item.get("source") or "")
                source_id = str(item.get("source_id") or "").strip()
                title = str(item.get("item") or "").strip()
                if source not in _SPIDER_LISTING_SOURCES or not source_id or not title:
                    continue
                slug = f"ext-{source}-{source_id}"[:128]
                existing = await db_query_one(
                    pool,
                    sql='SELECT id FROM "product" WHERE slug = $1',
                    params=(slug,),
                )
                if existing:
                    continue
                try:
                    await _create_listing(
                        item,
                        slug=slug,
                        categories=categories,
                        fallback_category_id=fallback_category_id,
                    )
                    stats["listed"] += 1
                    taken += 1
                except Exception as exc:  # noqa: BLE001 - 单品上架失败不中断整轮
                    stats["failed"] += 1
                    logger.warning("spider listing create failed slug=%s: %s", slug, exc)

    async def spider_listing_tick(**_: Any) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "listed": 0,
            "priced": 0,
            "skipped_no_screenshot": False,
            "failed": 0,
            "skipped_has_batch": 0,
        }
        # 填价段先行: 先把历史未定价商品补上价开卖, 再抓新品建档。
        if settings.vlm_screenshot_url:
            try:
                await _price_pending(stats)
            except Exception as exc:  # noqa: BLE001 - 填价段整体失败不拖垮上架段
                stats["failed"] += 1
                logger.warning("spider listing pricing phase failed: %s", exc)
        else:
            stats["skipped_no_screenshot"] = True
        await _list_new(stats)
        return stats

    spider_listing_tick.__name__ = "spider_listing_tick"

    async def spider_listing_tick_logged(**_: Any) -> dict[str, Any]:
        result = await spider_listing_tick()
        logger.info("spider listing engine tick: %s", result)
        return result

    spider_listing_tick_logged.__name__ = "spider_listing_tick_logged"

    return CronSchedulerEngine(
        tasks=[spider_listing_tick_logged],
        trigger={"on_cron": "30 4 * * *"},
        config={"interval_seconds": 86400},
        name="ext-spider-listing",
    )
