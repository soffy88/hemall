"""hemall 事件装配 — oservi event_webhook_dispatcher 的服务层接线。

SPEC §6: complete_checkout 后并发拉起履约下发/WMS/税务记账等。oservi 的
EventWebhookDispatcherEngine 提供 asyncio.gather 并发扇出骨架; 服务层在此装配
具体订阅者 (依赖倒置: 引擎不知业务, 订阅者由服务层注入)。

注意: 引擎的订阅者契约是 ``sub(*, event, payload)``, 与 omodul 的
``(config, input_data, output_dir, ...)`` 签名不同——故订阅者是服务层写的
适配器 callable, 不是直接塞 omodul。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from oservi.engines.event_webhook_dispatcher import EventWebhookDispatcherEngine

from .config import Settings

logger = logging.getLogger("hemall.events")


async def logging_subscriber(*, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """默认订阅者: 把事件落日志。生产可替换/追加为履约下发、WMS、税务记账等。"""
    logger.info("event received: %s payload_keys=%s", event, sorted(payload.keys()))
    return {"subscriber": "logging", "event": event, "ok": True}


def build_event_dispatcher(
    settings: Settings,
    subscribers: list[Callable[..., Any]] | None = None,
) -> EventWebhookDispatcherEngine:
    """装配事件派发引擎 (on_signal)。默认挂 logging 订阅者; 可注入更多。"""
    subs = subscribers if subscribers is not None else [logging_subscriber]
    engine = EventWebhookDispatcherEngine(
        subscribers=subs,
        trigger={"on_signal": True},
        config={},
        name="hemall-order-events",
    )
    engine.run()  # on_signal: 非阻塞, 仅置就绪标志
    return engine


async def fire_event(engine: EventWebhookDispatcherEngine | None, event: str, payload: dict[str, Any]) -> None:
    """ best-effort 派发事件; 派发失败只记日志, 不影响主业务流。"""
    if engine is None:
        return
    try:
        await engine.dispatch(event, payload)
    except Exception as exc:  # noqa: BLE001 - 事件派发是旁路, 不阻断下单
        logger.warning("event dispatch failed for %s: %s", event, exc)
