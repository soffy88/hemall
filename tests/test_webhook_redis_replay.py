from __future__ import annotations

import asyncio
import hashlib
import hmac
import time

from app.security.webhook import WebhookSignatureMiddleware


class _FakeRedis:
    def __init__(self):
        self.seen = set()

    async def set(self, key, value, *, ex, nx):
        assert ex >= 600 and nx is True
        if key in self.seen:
            return None
        self.seen.add(key)
        return True


async def _request(middleware, body, headers):
    messages = []
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/hook",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 1234),
    }
    await middleware(scope, receive, send)
    return messages


def test_webhook_nonce_claim_is_shared_and_rejects_replay():
    body = b'{"event":"payment"}'
    secret = "s" * 32
    timestamp = str(int(time.time()))
    nonce = "same-nonce"
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    calls = []

    async def app(_scope, receive, send):
        await receive()
        calls.append("accepted")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = WebhookSignatureMiddleware(
        app,
        secret=secret,
        protected_paths={"/hook"},
        redis_url="redis://shared/0",
        fail_closed=True,
    )
    middleware._redis = _FakeRedis()
    headers = {
        "x-hemall-signature": signature,
        "x-hemall-timestamp": timestamp,
        "x-hemall-nonce": nonce,
    }
    first = asyncio.run(_request(middleware, body, headers))
    second = asyncio.run(_request(middleware, body, headers))
    assert first[0]["status"] == 200
    assert second[0]["status"] == 403
    assert calls == ["accepted"]


def test_webhook_replay_store_fails_closed_in_production():
    body = b"payload"
    secret = "s" * 32
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    class BrokenRedis:
        async def set(self, *_args, **_kwargs):
            raise ConnectionError("redis down")

    async def app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = WebhookSignatureMiddleware(
        app,
        secret=secret,
        protected_paths={"/hook"},
        redis_url="redis://shared/0",
        fail_closed=True,
    )
    middleware._redis = BrokenRedis()
    messages = asyncio.run(
        _request(
            middleware,
            body,
            {
                "x-hemall-signature": signature,
                "x-hemall-timestamp": timestamp,
                "x-hemall-nonce": "new-nonce",
            },
        )
    )
    assert messages[0]["status"] == 503
