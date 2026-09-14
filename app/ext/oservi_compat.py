"""Stable async contracts for the public oservi engine releases.

The public v1.4 package exposes blocking scheduler ``run()`` methods, while
hemall needs one async tick in the application event loop.  Keeping this small
adapter in the service layer lets clean runners use the pinned public package
without depending on an unpublished sibling checkout.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("hemall.ext.oservi.compat")


class CronSchedulerEngine:
    """Async one-tick scheduler contract used by hemall's service adapters."""

    trigger_mode = "on_cron"

    def __init__(
        self,
        *,
        tasks: list[Callable[..., Any]] | Callable[..., Any],
        trigger: dict[str, Any],
        config: dict[str, Any],
        name: str,
    ) -> None:
        self.name = name
        self.task_list = tasks if isinstance(tasks, list) else [tasks]
        self.trigger = trigger
        self.config = config
        self._running = False
        self._tick_count = 0
        self._last_error: str | None = None

    async def _invoke(self, task: Callable[..., Any], **kwargs: Any) -> Any:
        result = task(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def _resolve_interval(self) -> float:
        if "interval_seconds" in self.config:
            return float(self.config["interval_seconds"])
        value = self.trigger.get("on_interval", self.trigger.get("on_cron", 60))
        return float(value) if isinstance(value, (int, float)) else 60.0

    async def run_once(self) -> list[Any]:
        results: list[Any] = []
        for task_no, task in enumerate(self.task_list):
            try:
                results.append(await self._invoke(task, tick_no=self._tick_count, task_no=task_no))
            except Exception as exc:  # noqa: BLE001 - one task cannot stop a tick
                self._last_error = (
                    f"task {task_no} {getattr(task, '__name__', task)}: {type(exc).__name__}: {exc}"
                )
                logger.warning("engine %s task %s failed: %s", self.name, task_no, exc)
                results.append({"error": str(exc), "task_no": task_no})
        self._tick_count += 1
        return results

    async def _run_loop(self) -> None:
        self._running = True
        while self._running:
            await self.run_once()
            await asyncio.sleep(self._resolve_interval())

    def run(self) -> None:
        """Blocking compatibility entry point for non-async callers."""
        asyncio.run(self._run_loop())

    def stop(self) -> None:
        self._running = False

    def health(self) -> dict[str, Any]:
        return {
            "status": "healthy" if self._running else "stopped",
            "details": {
                "name": self.name,
                "running": self._running,
                "tick_count": self._tick_count,
                "tasks_count": len(self.task_list),
                "last_error": self._last_error,
            },
        }


class EventWebhookDispatcherEngine:
    """Async fan-out dispatcher contract used by hemall."""

    trigger_mode = "on_signal"

    def __init__(
        self,
        *,
        subscribers: list[Callable[..., Any]] | Callable[..., Any],
        trigger: dict[str, Any],
        config: dict[str, Any],
        name: str,
    ) -> None:
        self.name = name
        self.subscriber_list = subscribers if isinstance(subscribers, list) else [subscribers]
        self.trigger = trigger
        self.config = config
        self._running = False
        self._dispatch_count = 0
        self._last_error: str | None = None

    def run(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    async def dispatch(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        on_step: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        async def invoke(subscriber: Callable[..., Any]) -> Any:
            result = subscriber(event=event, payload=payload)
            if inspect.isawaitable(result):
                return await result
            return result

        raw_results = await asyncio.gather(
            *(invoke(subscriber) for subscriber in self.subscriber_list),
            return_exceptions=True,
        )
        self._dispatch_count += 1
        results: list[Any] = []
        errors: list[dict[str, Any]] = []
        for subscriber, result in zip(self.subscriber_list, raw_results, strict=True):
            name = getattr(subscriber, "__name__", repr(subscriber))
            ok = not isinstance(result, BaseException)
            if ok:
                results.append(result)
            else:
                self._last_error = f"subscriber {name}: {result}"
                errors.append({"subscriber": name, "error": str(result)})
            if on_step is not None:
                try:
                    on_step({"subscriber": name, "ok": ok})
                except Exception as exc:  # noqa: BLE001 - telemetry callback only
                    logger.warning("event step callback failed for %s: %s", name, exc)
        return {
            "status": "completed",
            "event": event,
            "results": results,
            "errors": errors,
        }

    def health(self) -> dict[str, Any]:
        return {
            "status": "healthy" if self._running else "stopped",
            "details": {
                "name": self.name,
                "running": self._running,
                "subscribers_count": len(self.subscriber_list),
                "dispatch_count": self._dispatch_count,
                "last_error": self._last_error,
            },
        }
