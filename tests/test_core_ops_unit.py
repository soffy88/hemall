"""门店运营纯函数测试（不需要数据库）。"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.queries import _row_to_dict
from app.store_ops import _csv_response, _parse_day


def test_row_to_dict_unwraps_legacy_double_encoded_addresses():
    row = {
        "shipping_address": '{"city": "惠州市", "recipient_name": "张三"}',
        "billing_address": '{"city": "惠州市"}',
        "status": "pending",
    }
    out = _row_to_dict(row)
    assert out["shipping_address"]["city"] == "惠州市"
    assert out["billing_address"]["city"] == "惠州市"
    assert out["status"] == "pending"


def test_parse_day_defaults_today_and_rejects_bad_input():
    assert _parse_day(None) == datetime.now(ZoneInfo("Asia/Shanghai")).date()
    assert _parse_day("2026-11-10") == date(2026, 11, 10)
    with pytest.raises(HTTPException):
        _parse_day("2026/11/10")


def test_csv_response_has_utf8_bom_and_filename():
    resp = _csv_response("x.csv", ["a", "b"], [[1, "菜心"]])
    assert resp.body.startswith(b"\xef\xbb\xbf")
    assert "x.csv" in resp.headers["content-disposition"]
    assert "菜心" in resp.body.decode("utf-8")


def test_customer_cancel_requires_customer_login(client):
    resp = client.post("/store/orders/019fc6a2-0000-4000-8000-000000000000/cancel", json={})
    assert resp.status_code == 401


def test_store_ops_endpoints_require_staff(client):
    for path in ("/admin/store-ops/daily-report", "/admin/store-ops/journal"):
        resp = client.get(path)
        assert resp.status_code in (401, 403)
