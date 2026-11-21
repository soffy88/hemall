"""回归：测试环境下全局 app 的 /store/ 令牌桶限流须禁用。

背景：全套件 TestClient 共用 127.0.0.1 一个桶 (60 令牌、1/s 补充)，
套件跑得快即 429 误杀与被测行为无关的测试 (PR #22 连红 4 次、基线
HEAD 同样红过)。修复：environment == "test" 时中间件直通；生产配额
一字不动 (专测限流的测试直接构造中间件，不走全局 app)。
"""

from __future__ import annotations

from app.middleware.ratelimit import TokenBucketRateLimitMiddleware


def test_test_env_disables_global_store_rate_limit(client) -> None:
    """70 次 /store/ 请求 (超 60 令牌桶) 均不得 429。"""
    statuses = {client.get("/store/carts/not-a-uuid").status_code for _ in range(70)}
    assert 429 not in statuses


def test_middleware_is_disabled_on_test_app() -> None:
    """全局 app 装的限流中间件在测试环境下确为禁用态。"""
    from app.main import app

    middlewares = [m for m in app.user_middleware if m.cls is TokenBucketRateLimitMiddleware]
    assert middlewares, "全局 app 未装 TokenBucketRateLimitMiddleware"
    assert middlewares[0].kwargs.get("disabled") is True
