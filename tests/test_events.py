"""oservi 事件派发装配测试。"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.events import build_event_dispatcher, fire_event, logging_subscriber


def test_dispatcher_builds_and_healthy():
    engine = build_event_dispatcher(Settings())
    assert engine.health()["status"] in {"healthy", "stopped"}


async def test_dispatch_fans_out_to_subscribers():
    seen = []

    async def sub_a(*, event, payload):
        seen.append(("a", event))
        return {"subscriber": "a", "ok": True}

    async def sub_b(*, event, payload):
        seen.append(("b", event))
        return {"subscriber": "b", "ok": True}

    engine = build_event_dispatcher(Settings(), subscribers=[sub_a, sub_b])
    result = await engine.dispatch("order.placed", {"order_id": "o1"})
    assert result["status"] == "completed"
    assert result["errors"] == []
    assert {e[0] for e in seen} == {"a", "b"}


async def test_subscriber_error_captured_not_raised():
    async def boom(*, event, payload):
        raise RuntimeError("subscriber exploded")

    engine = build_event_dispatcher(Settings(), subscribers=[boom])
    result = await engine.dispatch("order.placed", {})
    # 引擎捕获异常进 errors, 不向上抛
    assert result["status"] == "completed"
    assert len(result["errors"]) == 1


async def test_fire_event_none_engine_is_noop():
    await fire_event(None, "x", {})  # 不应抛


async def test_fire_event_swallows_dispatch_errors():
    class BadEngine:
        async def dispatch(self, event, payload, *, on_step=None):
            raise RuntimeError("dispatch down")

    await fire_event(BadEngine(), "x", {})  # 旁路失败只记日志, 不抛


async def test_default_logging_subscriber():
    out = await logging_subscriber(event="ping", payload={"k": 1})
    assert out["ok"] is True
