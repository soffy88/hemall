"""补天计划 Task 1.2 — 零登录公开端点令牌桶限流 (Token Bucket)。

架构师指令：针对所有无鉴权 (零登录自助) 的公开端点 (如下单、查库存)，在网关
层引入基于 IP / Device_ID 的令牌桶限流，防刷防爬。

实现形态是纯 ASGI 中间件：只对显式传入的公开路径集合生效 (路径集合由
main.py 从 registry 的 require_auth=False 端点 + 手写公开端点推导，admin/
内部端点不在集合里，不受影响)，按 (client_ip, x-device-id) 二元组做桶键——
同一设备换 IP 或同一 IP 多个设备都不会共享额度。

桶语义：
    capacity = 60  (突发容忍：60 个令牌)
    refill   = 1/s (稳态 60 请求/分钟)
超过容量返回 429 + Retry-After (需等待的秒数)。GET 只读端点共享同一套桶参数，
本域读取量小，60/min 够用；后续若出现高流量读场景 (如 Feed 轮询) 可在
main.py 按路径组拆成两个桶参数。

诚实的空白：单进程内存态桶 (asyncio 单事件循环内安全，多 worker 各持一份，
限流不是严格全局)——与 slowapi 的 Redis 后端共存：登录路由继续走 slowapi
(Redis 分布式)，这里覆盖零登录自助端点。生产多实例部署时如需严格全局限流，
把桶状态换成 Redis INCR 即可，接口 (allow(key)) 不变。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi.responses import JSONResponse

logger = logging.getLogger("hemall.middleware.ratelimit")

#: 默认令牌桶参数：突发容量 60，稳态 1 token/秒 (60 请求/分钟)。
DEFAULT_CAPACITY = 60
DEFAULT_REFILL_PER_SEC = 1.0
#: 桶表上限，防内存无限增长 (超出后整体清空重建，宁可丢限流状态不可 OOM)。
_MAX_BUCKETS = 100_000


class TokenBucket:
    """单键令牌桶。非线程安全——限流中间件在 asyncio 单事件循环内调用。"""

    __slots__ = ("capacity", "tokens", "last_refill", "refill_per_sec")

    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()

    def allow(self) -> tuple[bool, float]:
        """尝试取一个令牌。返回 (是否放行, 若拒绝则还需等待的秒数)。"""
        now = time.monotonic()
        self.tokens = min(
            self.capacity,
            self.tokens + (now - self.last_refill) * self.refill_per_sec,
        )
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0.0
        if self.refill_per_sec <= 0:
            # 永不补充的桶 (测试/禁用场景)：无等待上限，返回 0 表示不产生 Retry-After。
            return False, 0.0
        return False, (1.0 - self.tokens) / self.refill_per_sec


class TokenBucketRateLimiter:
    """IP+Device_ID 键控令牌桶限流器 (纯内存，进程内共享)。"""

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        refill_per_sec: float = DEFAULT_REFILL_PER_SEC,
    ) -> None:
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self._buckets: dict[str, TokenBucket] = {}

    def _bucket_for(self, key: str) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= _MAX_BUCKETS:
                self._buckets.clear()
            bucket = TokenBucket(self.capacity, self.refill_per_sec)
            self._buckets[key] = bucket
        return bucket

    def allow(self, key: str) -> tuple[bool, float]:
        """对 key 尝试取令牌。返回 (放行?, 拒绝时等待秒数)。"""
        return self._bucket_for(key).allow()

    def key_for(self, client_ip: str, device_id: str | None) -> str:
        """限流键：IP + Device_ID 二元组。无 Device_ID 时退化为纯 IP。"""
        return f"{client_ip}:{device_id or 'anon'}"


class TokenBucketRateLimitMiddleware:
    """纯 ASGI 中间件：对公开路径做令牌桶限流。

    支持两种匹配：
      - public_paths: 精确路径集合 (ext 声明式公开端点，从 registry 推导)。
      - path_prefixes: 路径前缀集合 (如 "/store/" 覆盖整个零登录商城前端)。

    用法 (main.py)：
        app.add_middleware(
            TokenBucketRateLimitMiddleware,
            public_paths={...},
            path_prefixes={"/store/"},
        )
    """

    def __init__(
        self,
        app: Any,
        *,
        public_paths: set[str] | None = None,
        path_prefixes: set[str] | None = None,
        capacity: int = DEFAULT_CAPACITY,
        refill_per_sec: float = DEFAULT_REFILL_PER_SEC,
    ) -> None:
        self.app = app
        self.public_paths = public_paths or set()
        self.path_prefixes = path_prefixes or set()
        self.limiter = TokenBucketRateLimiter(capacity, refill_per_sec)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        limited = path in self.public_paths or any(
            path.startswith(prefix) for prefix in self.path_prefixes
        )
        if not limited:
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        client = scope.get("client") or ("unknown", 0)
        client_ip = client[0] if isinstance(client, (tuple, list)) else str(client)
        device_id = headers.get("x-device-id") or None
        key = self.limiter.key_for(client_ip, device_id)

        allowed, retry_after = self.limiter.allow(key)
        if not allowed:
            logger.info(
                "rate limit exceeded: path=%s key=%s retry_after=%.1fs",
                scope["path"],
                key,
                retry_after,
            )
            response = JSONResponse(
                status_code=429,
                content={
                    "detail": "rate limit exceeded",
                    "retry_after": f"{retry_after:.0f}s",
                },
                headers={"Retry-After": str(int(retry_after) + 1)},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
