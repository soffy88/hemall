from __future__ import annotations

from unittest.mock import AsyncMock

from app.main import app


def test_production_receipt_lookup_rejects_query_string_token(client, monkeypatch):
    monkeypatch.setattr(app.state.config, "environment", "production")
    response = client.get("/store/orders/lookup?token=secret-token")
    assert response.status_code == 400
    assert "X-Receipt-Token" in response.json()["detail"]


def test_production_receipt_lookup_requires_header_token_and_no_store(client, monkeypatch):
    monkeypatch.setattr(app.state.config, "environment", "production")
    response = client.get("/store/orders/lookup")
    assert response.status_code == 401

    lookup = AsyncMock(return_value={"order_id": "order-1"})
    monkeypatch.setattr("app.storefront.queries.get_order_by_receipt_token", lookup)
    monkeypatch.setattr("app.storefront._pool", lambda _request: object())
    response = client.get("/store/orders/lookup", headers={"X-Receipt-Token": "opaque-capability"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["referrer-policy"] == "no-referrer"
    lookup.assert_awaited_once()
