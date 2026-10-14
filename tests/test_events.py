"""oservi 事件派发装配测试。"""

from __future__ import annotations

from app.config import Settings
from app.events import build_event_dispatcher, fire_event, logging_subscriber
from app.middleware.metrics import EVENT_DISPATCH_FAILURES_TOTAL


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
    """engine=None 是"没装配事件引擎"的常态，必须静默 no-op 且返回 None。"""
    assert await fire_event(None, "x", {}) is None
    assert await fire_event(None, "order.placed", {"order_id": "o1"}) is None


async def test_fire_event_swallows_dispatch_errors(caplog):
    """旁路失败要"吞异常 + 留证据"。

    原实现只写日志但测试一条断言都没有（审计 P2-1）：把 except 整段删掉、
    或者把 logger.warning 改成 logger.debug，这个测试照样绿。现在断言日志里
    真的留下了 event 名和异常。
    """
    import logging

    class BadEngine:
        async def dispatch(self, event, payload, *, on_step=None):
            raise RuntimeError("dispatch down")

    with caplog.at_level(logging.WARNING, logger="hemall.events"):
        before = EVENT_DISPATCH_FAILURES_TOTAL.labels(event_type="checkout.completed")._value.get()
        assert await fire_event(BadEngine(), "checkout.completed", {"order_id": "o1"}) is None
        assert (
            EVENT_DISPATCH_FAILURES_TOTAL.labels(event_type="checkout.completed")._value.get()
            == before + 1
        )

    warnings = [r for r in caplog.records if r.name == "hemall.events"]
    assert warnings, "旁路失败必须留下 WARNING 日志，不能静默吞掉"
    assert any("checkout.completed" in r.getMessage() for r in warnings)
    assert any("dispatch down" in r.getMessage() for r in warnings)


async def test_fire_event_returns_none_on_success(caplog):
    """成功路径不产生 WARNING。"""

    class OkEngine:
        def __init__(self):
            self.calls = []

        async def dispatch(self, event, payload, *, on_step=None):
            self.calls.append((event, payload))
            return {"status": "completed"}

    import logging

    engine = OkEngine()
    with caplog.at_level(logging.WARNING, logger="hemall.events"):
        assert await fire_event(engine, "order.placed", {"k": 1}) is None
    assert engine.calls == [("order.placed", {"k": 1})]
    assert [r for r in caplog.records if r.name == "hemall.events"] == []


async def test_default_logging_subscriber():
    out = await logging_subscriber(event="ping", payload={"k": 1})
    assert out["ok"] is True
