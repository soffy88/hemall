from __future__ import annotations

import asyncio

from app.middleware.ratelimit import TokenBucketRateLimitMiddleware


class _FakeRedis:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def eval(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.result


def _middleware(*, fail_closed=True):
    async def app(_scope, _receive, _send):
        return None

    return TokenBucketRateLimitMiddleware(
        app,
        redis_url="redis://shared/0",
        fail_closed=fail_closed,
        capacity=3,
        refill_per_sec=1.0,
    )


def test_distributed_limiter_uses_atomic_redis_script():
    middleware = _middleware()
    fake = _FakeRedis([0, 1250])
    middleware._redis = fake
    allowed, retry_after = asyncio.run(middleware._allow("203.0.113.10"))
    assert allowed is False
    assert retry_after == 1.25
    assert len(fake.calls) == 1
    args = fake.calls[0]
    assert args[1] == 2
    assert args[2].startswith("hemall:ratelimit:") and args[2].endswith(":tokens")
    assert args[3].endswith(":last")
    assert "redis.call('TIME')" in args[0]


def test_distributed_limiter_fails_closed_if_redis_is_unavailable():
    middleware = _middleware(fail_closed=True)
    middleware._redis = _FakeRedis(error=ConnectionError("redis down"))
    allowed, retry_after = asyncio.run(middleware._allow("203.0.113.11"))
    assert allowed is None
    assert retry_after == 0.0


def test_development_limiter_falls_back_to_process_local_bucket():
    middleware = _middleware(fail_closed=False)
    middleware._redis = _FakeRedis(error=ConnectionError("redis down"))
    allowed, retry_after = asyncio.run(middleware._allow("203.0.113.12"))
    assert allowed is True
    assert retry_after == 0.0
