"""P1 履约 SLA 纯算法单测 — compute_delivery_sla / 下一班车推演。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.ext.oskill import (
    SLA_COMPENSATION_CENTS,
    WAVE_SHIPPING_PRICE_CENTS,
    WAVE_SCHEDULE_HOURS,
    _next_wave_at,
    compute_delivery_sla,
)

NOW = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)


def test_wave_sla_uses_next_wave_plus_lead():
    next_wave = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)
    sla = compute_delivery_sla(
        NOW, shipping_cents=WAVE_SHIPPING_PRICE_CENTS, paid_at=NOW, next_wave_at=next_wave
    )
    assert sla["shipping_type"] == "wave"
    assert sla["promised_at"] == next_wave + timedelta(hours=2)
    assert sla["compensation_cents"] == SLA_COMPENSATION_CENTS


def test_express_sla_is_paid_plus_2h():
    sla = compute_delivery_sla(NOW, shipping_cents=500, paid_at=NOW)
    assert sla["shipping_type"] == "express"
    assert sla["promised_at"] == NOW + timedelta(hours=2)


def test_pickup_sla_is_paid_plus_1h():
    sla = compute_delivery_sla(NOW, shipping_cents=0, paid_at=NOW)
    assert sla["shipping_type"] == "pickup"
    assert sla["promised_at"] == NOW + timedelta(hours=1)


def test_next_wave_at_before_1000():
    assert _next_wave_at(NOW) == datetime(2026, 8, 2, 10, 0, tzinfo=UTC)


def test_next_wave_at_after_1600_rolls_to_next_day():
    late = datetime(2026, 8, 2, 17, 0, tzinfo=UTC)
    expected = datetime(2026, 8, 3, WAVE_SCHEDULE_HOURS[0], 0, tzinfo=UTC)
    assert _next_wave_at(late) == expected


def test_wave_sla_without_next_wave_at_infers_schedule():
    sla = compute_delivery_sla(NOW, shipping_cents=WAVE_SHIPPING_PRICE_CENTS, paid_at=NOW)
    assert sla["promised_at"] == datetime(2026, 8, 2, 12, 0, tzinfo=UTC)  # 10:00 班车 + 2h
