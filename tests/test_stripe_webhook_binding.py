from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from obase.provider_registry import ProviderRegistry

from app.payments.router import router


class _StripeGateway:
    def __init__(self, event):
        self.event = event

    async def verify_callback(self, _headers, _body):
        return self.event


class _Conn:
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def fetchrow(self, *_args):
        return self.row


class _Pool:
    def __init__(self, row):
        self.conn = _Conn(row)

    def acquire(self):
        return self.conn


@pytest.mark.asyncio
async def test_stripe_webhook_rejects_local_intent_with_different_provider_intent_id():
    local_intent = {
        "id": "local-intent-1",
        "provider_intent_id": "pi-real-1",
        "provider": "stripe",
    }
    event = {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi-attacker-2",
                "amount_received": 500,
                "currency": "usd",
                "metadata": {"out_trade_no": "local-intent-1"},
            }
        },
    }
    app = FastAPI()
    app.include_router(router)
    app.state.pool = _Pool(local_intent)
    ProviderRegistry.get().register_generic(
        "payment_gateway", "stripe", _StripeGateway(event), replace=True
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://stripe.test"
    ) as client:
        response = await client.post("/payments/stripe/webhook", content=b"signed")
    assert response.status_code == 400
    assert response.json()["detail"] == "stripe payment intent mismatch"
