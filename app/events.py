"""hemall 事件装配 — oservi event_webhook_dispatcher 的服务层接线。

SPEC §6: complete_checkout 后并发拉起履约下发/WMS/税务记账等。oservi 的
EventWebhookDispatcherEngine 提供 asyncio.gather 并发扇出骨架; 服务层在此装配
具体订阅者 (依赖倒置: 引擎不知业务, 订阅者由服务层注入)。

注意: 引擎的订阅者契约是 ``sub(*, event, payload)``, 与 omodul 的
``(config, input_data, output_dir, ...)`` 签名不同——故订阅者是服务层写的
适配器 callable, 不是直接塞 omodul。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any

from .config import Settings
from .ext.oservi_compat import EventWebhookDispatcherEngine

logger = logging.getLogger("hemall.events")


class _EventDispatcherAdapter:
    """Keep hemall's async event contract stable across public oservi releases.

    oservi v1.4 exposes a blocking Redis-consumer ``run()`` and later releases
    expose a non-blocking ``dispatch()`` API.  The application lifespan is
    already running an event loop, so calling the former directly would invoke
    ``asyncio.run`` from inside that loop.  The adapter uses the native API when
    available and provides the same fan-out semantics for v1.4.
    """

    def __init__(self, engine: EventWebhookDispatcherEngine) -> None:
        self._engine = engine
        self._running = False

    def run(self) -> None:
        if callable(getattr(self._engine, "dispatch", None)):
            self._engine.run()
        self._running = True

    def stop(self) -> None:
        self._running = False
        stop = getattr(self._engine, "stop", None)
        if callable(stop):
            stop()

    def health(self) -> dict[str, Any]:
        details = {}
        health = getattr(self._engine, "health", None)
        if callable(health):
            details = dict(health().get("details", {}))
        details["adapter"] = True
        return {"status": "healthy" if self._running else "stopped", "details": details}

    async def dispatch(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        dispatch = getattr(self._engine, "dispatch", None)
        if callable(dispatch):
            return await dispatch(event, payload)

        subscribers = getattr(self._engine, "subscriber_list", [])

        async def invoke(subscriber: Callable[..., Any]) -> Any:
            result = subscriber(event=event, payload=payload)
            if inspect.isawaitable(result):
                return await result
            return result

        raw_results = await asyncio.gather(
            *(invoke(subscriber) for subscriber in subscribers),
            return_exceptions=True,
        )
        results: list[Any] = []
        errors: list[dict[str, Any]] = []
        for subscriber, result in zip(subscribers, raw_results, strict=True):
            name = getattr(subscriber, "__name__", repr(subscriber))
            if isinstance(result, BaseException):
                errors.append({"subscriber": name, "error": str(result)})
            else:
                results.append(result)
        return {"status": "completed", "event": event, "results": results, "errors": errors}


async def logging_subscriber(*, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """默认订阅者: 把事件落日志。生产可替换/追加为履约下发、WMS、税务记账等。"""
    logger.info("event received: %s payload_keys=%s", event, sorted(payload.keys()))
    return {"subscriber": "logging", "event": event, "ok": True}


def build_event_dispatcher(
    settings: Settings,
    subscribers: list[Callable[..., Any]] | None = None,
) -> _EventDispatcherAdapter:
    """装配事件派发引擎 (on_signal)。默认挂 logging 订阅者; 可注入更多。"""
    subs = subscribers if subscribers is not None else [logging_subscriber]
    engine = EventWebhookDispatcherEngine(
        subscribers=subs,
        trigger={"on_signal": True},
        config={},
        name="hemall-order-events",
    )
    adapter = _EventDispatcherAdapter(engine)
    adapter.run()
    return adapter


async def fire_event(engine: Any, event: str, payload: dict[str, Any]) -> None:
    """best-effort 派发事件; 派发失败只记日志, 不影响主业务流。"""
    if engine is None:
        return
    try:
        await engine.dispatch(event, payload)
    except Exception as exc:  # noqa: BLE001 - 事件派发是旁路, 不阻断下单
        logger.warning("event dispatch failed for %s: %s", event, exc)
