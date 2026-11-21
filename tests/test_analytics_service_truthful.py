from __future__ import annotations

import asyncio
from datetime import date, timedelta

from app.analytics.service import AnalyticsService


class _Conn:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def fetchrow(self, sql, *args):
        self.queries.append((sql, args))
        if "WITH paid_orders AS" in sql:
            return self.rows["retention"]
        if "WITH today_paid_orders AS" in sql:
            return self.rows["overview"]
        raise AssertionError("unexpected analytics SQL")


class _Pool:
    def __init__(self, rows):
        self.conn = _Conn(rows)

    def acquire(self):
        return self.conn


def test_funnel_is_unavailable_without_behavior_event_source():
    service = AnalyticsService()
    assert asyncio.run(service.get_funnel("2026-10-01", "2026-10-10")) == []


def test_dashboard_uses_paid_order_query_and_marks_untracked_metrics_unavailable():
    pool = _Pool(
        {"overview": {"today_gmv_cents": 9876, "today_orders": 3, "avg_order_value_cents": 3292}}
    )
    service = AnalyticsService()
    service._pool = pool
    result = asyncio.run(service.get_dashboard_overview())
    assert result["today_gmv_cents"] == 9876
    assert result["today_orders"] == 3
    assert result["avg_order_value_cents"] == 3292
    assert result["conversion_rate"] is None
    assert result["refund_rate"] is None
    assert "today_active_users" in result["unavailable_metrics"]
    assert "payment_intent" in pool.conn.queries[0][0]


def test_retention_uses_paid_purchase_cohort_and_only_reports_matured_windows():
    cohort = date.today() - timedelta(days=8)
    pool = _Pool({"retention": {"total_users": 4, "d1_users": 2, "d7_users": 1, "d30_users": 0}})
    service = AnalyticsService()
    service._pool = pool
    result = asyncio.run(service.get_retention(cohort.isoformat(), lookback_days=30))
    assert len(result) == 1
    assert result[0].total_users == 4
    assert result[0].d1_retention == 0.5
    assert result[0].d7_retention == 0.25
    assert result[0].d30_retention is None


def test_retention_returns_empty_for_empty_cohort():
    pool = _Pool({"retention": {"total_users": 0, "d1_users": 0, "d7_users": 0, "d30_users": 0}})
    service = AnalyticsService()
    service._pool = pool
    result = asyncio.run(service.get_retention(date.today().isoformat()))
    assert result == []
