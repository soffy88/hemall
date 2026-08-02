"""app.ext.oservi_lifecycle — 把 oservi.py 装配出的全部引擎接进 FastAPI 生命周期。

早期版本用 ``threading.Thread(target=engine.run)`` 包 CronSchedulerEngine——
实测直接炸了：``engine.run()`` 内部 ``asyncio.run(...)`` 会在那个线程另起一个
全新的 event loop，但传给 ``build_*_engine`` 的 ``pool`` (asyncpg 连接池) 是在
主线程的 event loop 里创建的。asyncpg 的 Pool/Connection 不允许跨 event loop
使用，实测第一个 tick 就抛 ``cannot perform operation: another operation is
in progress`` / ``ConnectionDoesNotExistError``。

正确做法：不用 ``engine.run()`` (那是引擎骨架自己文档里写的"给纯同步/线程
上下文用"的入口)，改成直接反复调用它公开的 ``run_once()``——虽然它自己的
docstring 写"供测试使用"，但这只是个普通 async 方法，没有理由不能被一个我们
自己写的、跑在主 event loop 里的调度循环反复调用。用 ``asyncio.create_task``
把这个循环挂在跟 pool 同一个 event loop 上，tick 永远和 pool 同源，不会跨
loop；停止也变成对 asyncio.Task 调用 cancel()，是标准的、可靠的取消方式
(会在 sleep 中间正确抛 CancelledError)，不再有旧版"跨线程 set() 不保证唤醒"
的问题。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from oservi.engines.cron_scheduler_engine import CronSchedulerEngine
from oservi.engines.event_webhook_dispatcher import EventWebhookDispatcherEngine

from ..config import Settings
from .oservi import (
    build_affiliate_settlement_engine,
    build_autonomous_triage_engine,
    build_batch_broadcast_engine,
    build_delivery_wave_engine,
    build_demand_aggregator_engine,
    build_inventory_reaper_engine,
    build_market_maker_engine,
    build_market_maker_probe_engine,
    build_mercenary_routing_engine,
    build_node_host_settlement_engine,
    build_social_broadcast_engine,
    build_spatial_fomo_engine,
    build_tote_balancing_engine,
    build_weather_arbitrage_engine,
)

logger = logging.getLogger("hemall.ext.oservi_lifecycle")


async def _run_cron_loop(engine: CronSchedulerEngine, interval_seconds: float) -> None:
    """在当前 (主) event loop 里反复调 engine.run_once()，tick 间隔 sleep。"""
    while True:
        try:
            await engine.run_once()
        except Exception:  # noqa: BLE001 - 单次 tick 失败不该杀死整个调度循环
            logger.exception("ext cron engine '%s' tick failed", engine.name)
        await asyncio.sleep(interval_seconds)


@dataclass
class ExtOservi:
    """全部引擎实例 + 后台 task 句柄，挂在 app.state.ext_oservi 上。"""

    demand_aggregator: EventWebhookDispatcherEngine
    batch_broadcast: EventWebhookDispatcherEngine
    weather_arbitrage: CronSchedulerEngine
    inventory_reaper: CronSchedulerEngine
    delivery_wave: CronSchedulerEngine
    node_host_settlement: CronSchedulerEngine
    autonomous_triage: EventWebhookDispatcherEngine
    market_maker: CronSchedulerEngine
    tote_balancing: CronSchedulerEngine
    spatial_fomo: EventWebhookDispatcherEngine
    social_broadcast: CronSchedulerEngine
    market_maker_probe: CronSchedulerEngine
    mercenary_routing: CronSchedulerEngine
    affiliate_settlement: CronSchedulerEngine
    _tasks: list[asyncio.Task] = field(default_factory=list)

    def _cron_engines(self) -> tuple[CronSchedulerEngine, ...]:
        return (
            self.weather_arbitrage,
            self.inventory_reaper,
            self.delivery_wave,
            self.node_host_settlement,
            self.market_maker,
            self.tote_balancing,
            self.social_broadcast,
            self.market_maker_probe,
            self.mercenary_routing,
            self.affiliate_settlement,
        )

    def start_cron_engines(self) -> None:
        """10 个 CronSchedulerEngine 各挂一个 asyncio.Task，在当前 event loop 里跑。

        必须在有运行中 event loop 的 async 上下文里调用 (如 FastAPI lifespan)。
        """
        for engine in self._cron_engines():
            interval = float(engine.config.get("interval_seconds", 3600))
            task = asyncio.create_task(
                _run_cron_loop(engine, interval), name=engine.name
            )
            self._tasks.append(task)
        logger.info("started %d ext cron engine tasks", len(self._tasks))

    def stop(self) -> None:
        """取消全部后台调度 task (标准 asyncio 取消语义，sleep 中会被正确打断)。"""
        for task in self._tasks:
            task.cancel()


def build_ext_oservi(pool: Any, settings: Settings) -> ExtOservi:
    """装配全部 SPEC §5 引擎 (v1.0 六个 + v2.0 全自动仲裁)。不在这里启动常驻
    调度，调用方按需 start_cron_engines() (方便测试期只装配不启动)。
    """
    batch_broadcast = build_batch_broadcast_engine(
        notification_provider=settings.default_notification_provider
    )
    demand_aggregator = build_demand_aggregator_engine(
        notification_provider=settings.default_notification_provider
    )
    weather_arbitrage = build_weather_arbitrage_engine(
        pool, broadcast_engine=batch_broadcast
    )
    inventory_reaper = build_inventory_reaper_engine(pool, settings=settings)
    delivery_wave = build_delivery_wave_engine(pool)
    node_host_settlement = build_node_host_settlement_engine(pool, settings=settings)
    autonomous_triage = build_autonomous_triage_engine(pool, settings=settings)
    market_maker = build_market_maker_engine(pool, broadcast_engine=batch_broadcast)
    tote_balancing = build_tote_balancing_engine(
        pool, notification_provider=settings.default_notification_provider
    )
    spatial_fomo = build_spatial_fomo_engine(
        pool,
        settings=settings,
        notification_provider=settings.default_notification_provider,
    )
    social_broadcast = build_social_broadcast_engine(pool, settings=settings)
    market_maker_probe = build_market_maker_probe_engine(pool)
    mercenary_routing = build_mercenary_routing_engine(pool)
    affiliate_settlement = build_affiliate_settlement_engine(pool)

    return ExtOservi(
        demand_aggregator=demand_aggregator,
        batch_broadcast=batch_broadcast,
        weather_arbitrage=weather_arbitrage,
        inventory_reaper=inventory_reaper,
        delivery_wave=delivery_wave,
        node_host_settlement=node_host_settlement,
        autonomous_triage=autonomous_triage,
        market_maker=market_maker,
        tote_balancing=tote_balancing,
        spatial_fomo=spatial_fomo,
        social_broadcast=social_broadcast,
        market_maker_probe=market_maker_probe,
        mercenary_routing=mercenary_routing,
        affiliate_settlement=affiliate_settlement,
    )
