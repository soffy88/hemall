from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from obase.crypto.util import CryptoUtil

from app.config import Settings
from app.deps import get_settings
from app.storefront import _authorize_cart, _has_cart_capability, _sign_cart_capability


class _FakeConn:
    def __init__(self, owner: str | None = None):
        self.owner = owner

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def fetchrow(self, _query, _cart_id):
        return {"customer_id": self.owner} if self.owner is not None or self.owner is None else None

    async def fetchval(self, query, *args):
        if query.lstrip().upper().startswith("UPDATE CART"):
            if self.owner is None:
                self.owner = args[0]
                return self.owner
            return None
        return self.owner


class _FakePool:
    def __init__(self, owner: str | None = None):
        self.conn = _FakeConn(owner)

    def acquire(self):
        return self.conn


def _request(settings: Settings, pool: _FakePool, token: str | None = None) -> Request:
    app = SimpleNamespace(state=SimpleNamespace(config=settings, pool=pool))
    headers = [] if token is None else [(b"x-cart-token", token.encode())]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/store/carts/cart-1",
        "raw_path": b"/store/carts/cart-1",
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 1234),
        "server": ("testserver", 80),
        "app": app,
    }
    return Request(scope)


def test_guest_cart_requires_capability():
    settings = Settings()
    request = _request(settings, _FakePool())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_authorize_cart(request, "cart-1", None))
    assert exc.value.status_code == 404


def test_guest_cart_accepts_only_cart_scoped_signed_capability():
    settings = Settings()
    token = _sign_cart_capability("cart-1", settings.jwt_secret, settings.jwt_algorithm)
    request = _request(settings, _FakePool(), token)
    assert _has_cart_capability(request, "cart-1")
    assert not _has_cart_capability(request, "cart-2")
    assert asyncio.run(_authorize_cart(request, "cart-1", None)) is None


def test_guest_capability_cannot_access_customer_owned_cart():
    settings = Settings()
    token = _sign_cart_capability("cart-1", settings.jwt_secret, settings.jwt_algorithm)
    request = _request(settings, _FakePool(owner="customer-2"), token)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_authorize_cart(request, "cart-1", None))
    assert exc.value.status_code == 404


def test_customer_cannot_access_another_customers_cart():
    settings = Settings()
    request = _request(settings, _FakePool(owner="customer-2"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_authorize_cart(request, "cart-1", {"customer_id": "customer-1"}))
    assert exc.value.status_code == 404


def test_customer_can_claim_guest_cart_only_with_capability():
    settings = Settings()
    pool = _FakePool()
    token = _sign_cart_capability("cart-1", settings.jwt_secret, settings.jwt_algorithm)
    request = _request(settings, pool, token)
    assert (
        asyncio.run(_authorize_cart(request, "cart-1", {"customer_id": "customer-1"}))
        == "customer-1"
    )
    assert pool.conn.owner == "customer-1"


def test_customer_cannot_claim_guest_cart_without_capability():
    settings = Settings()
    request = _request(settings, _FakePool())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_authorize_cart(request, "cart-1", {"customer_id": "customer-1"}))
    assert exc.value.status_code == 404


def test_checkout_rejects_client_selected_customer_without_auth(client):
    response = client.post("/store/checkout", json={"cart_id": "cart-1", "customer_id": "victim"})
    assert response.status_code == 401


def test_checkout_rejects_customer_id_not_matching_customer_token(client):
    settings = get_settings()
    token = CryptoUtil.jwt_sign(
        payload={"typ": "customer", "customer_id": "customer-1", "email": "c@example.test"},
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    response = client.post(
        "/store/checkout",
        json={"cart_id": "cart-1", "customer_id": "victim"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
