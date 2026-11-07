"""P0-04/05/06: 订单状态口径、超时关单和服务端运费。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.shipping import default_shipping_rules, quote_shipping


def _settings(**overrides: object) -> Settings:
    return Settings(
        pg_dsn="postgresql://invalid:invalid@127.0.0.1:1/none",
        **overrides,
    )


def test_shipping_quote_ignores_client_price_and_applies_threshold():
    rules = default_shipping_rules(
        _settings(
            shipping_standard_cents=1500,
            shipping_free_threshold_cents=5000,
        )
    )

    paid = quote_shipping(rules, method_name="standard", subtotal_cents=4999)
    free = quote_shipping(rules, method_name="标准配送", subtotal_cents=5000)

    assert paid["price_cents"] == 1500
    assert paid["quoted_by_server"] is True
    assert free["price_cents"] == 0
    assert free["free_shipping_applied"] is True


def test_shipping_quote_rejects_unknown_method():
    rules = default_shipping_rules(_settings())
    with pytest.raises(ValueError, match="unknown shipping method"):
        quote_shipping(rules, method_name="free-by-client", subtotal_cents=999999)


@pytest.mark.asyncio
async def test_delivery_and_sla_queries_include_confirmed(monkeypatch):
    from app.ext import oservi

    seen: list[tuple[str, tuple[object, ...]]] = []

    async def fake_query(pool, *, sql, params=()):
        seen.append((sql, params))
        return []

    monkeypatch.setattr(oservi, "db_query_many", fake_query)

    delivery = oservi.build_delivery_wave_engine(object())
    await delivery.run_once()
    sla = oservi.build_sla_promise_engine(object(), settings=_settings())
    await sla.run_once()

    assert "status IN (" in seen[0][0]
    assert "confirmed" in seen[0][1]
    assert "status IN (" in seen[1][0]
    assert "confirmed" in seen[1][1]


@pytest.mark.asyncio
async def test_order_timeout_engine_cancels_each_selected_order(monkeypatch):
    from app.ext import oservi
    from app.orders.service import OrderService

    queries: list[tuple[str, tuple[object, ...]]] = []
    cancelled: list[tuple[str, str, str | None]] = []

    async def fake_query(pool, *, sql, params=()):
        queries.append((sql, params))
        return [{"id": "order-a"}, {"id": "order-b"}]

    async def fake_cancel(self, order_id, reason="user requested", operator_id=None):
        cancelled.append((order_id, reason, operator_id))
        if order_id == "order-b":
            raise RuntimeError("simulated failure")
        return object()

    monkeypatch.setattr(oservi, "db_query_many", fake_query)
    monkeypatch.setattr(OrderService, "cancel_order", fake_cancel)
    settings = SimpleNamespace(
        order_payment_timeout_minutes=30,
        order_timeout_engine_interval_seconds=60,
    )

    engine = oservi.build_order_timeout_engine(object(), settings)
    result = (await engine.run_once())[0]

    sql, params = queries[0]
    assert "o.status IN ('pending', 'draft')" in sql
    assert "NOT EXISTS" in sql
    assert params == (30, 100)
    assert cancelled == [
        ("order-a", "payment timeout (30m)", "system:order-timeout"),
        ("order-b", "payment timeout (30m)", "system:order-timeout"),
    ]
    assert result["cancelled_count"] == 1
    assert result["errors"][0]["order_id"] == "order-b"
