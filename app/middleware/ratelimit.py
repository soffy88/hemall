"""补天计划 Task 1.2 — 零登录公开端点令牌桶限流 (Token Bucket)。

架构师指令：针对所有无鉴权 (零登录自助) 的公开端点 (如下单、查库存)，在网关
层引入基于 IP / Device_ID 的令牌桶限流，防刷防爬。

实现形态是纯 ASGI 中间件：只对显式传入的公开路径集合生效 (路径集合由
main.py 从 registry 的 require_auth=False 端点 + 手写公开端点推导，admin/
内部端点不在集合里，不受影响)，按服务端认定的对端 IP 做桶键。

安全要点：桶键的主维度是**服务端认定的对端 IP**，绝不掺入客户端可控的头
(如 X-Device-Id)——否则攻击者只需轮换该头即可为自己无限开桶绕过限流,
并撑爆桶表触发全量清空。`X-Device-Id` 只作为**附加维度**与 IP 桶同时判定
(两者都放行才放行), 因此共享 NAT 下的多台设备各有配额, 而任何单台设备都
无法突破该 IP 的总配额。部署在可信反代之后时, 由中间件解析可信 XFF
(trusted_proxy_hops) 还原真实对端 IP——跳数配错会让全站共享一个桶 (审计
P1-5 曾因此使生产限流退化成"全站 60/min"), 故 compose/Helm 必须显式注入
`HEMALL_RATELIMIT_TRUSTED_PROXY_HOPS`, Caddy 必须用
`{http.request.client_ip}` 而不是 `{remote_host}` 透传。

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

import hashlib
import logging
import math
import time
from typing import Any

from fastapi.responses import JSONResponse

logger = logging.getLogger("hemall.middleware.ratelimit")

#: 默认令牌桶参数：突发容量 60，稳态 1 token/秒 (60 请求/分钟)。
DEFAULT_CAPACITY = 60
DEFAULT_REFILL_PER_SEC = 1.0
#: 桶表上限，防内存无限增长 (超出后整体清空重建，宁可丢限流状态不可 OOM)。
_MAX_BUCKETS = 100_000
#: `X-Device-Id` 摘录前的长度上限 (客户端可控值，必须限长)。
_MAX_DEVICE_ID_LEN = 128


def _real_client_ip(xff: str | None, peer_ip: str, trusted_hops: int) -> str:
    """从可信反代注入的 X-Forwarded-For 解析真实对端 IP。

    XFF 形如 "client, proxy1, proxy2" (左→右: 最初客户端 → 每一跳追加)。每一跳
    追加的是"它收到的来源地址"，因此链长恰好等于前置可信反代的层数，最左一项
    才是真实客户端；ASGI 对端 (最右一跳) 自己不在链里。取 `parts[-trusted_hops]`
    即在跳数正确时取到真实客户端。

    链比可信跳数短 (被伪造/缺失) 时回退到 ASGI 对端 peer_ip, 不轻信任何客户端
    提供的值。
    """
    if not xff:
        return peer_ip
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if len(parts) < trusted_hops:
        return peer_ip
    return parts[-trusted_hops]


def _normalized_device_id(device_id: str | None) -> str | None:
    """把客户端可控的 `X-Device-Id` 规整成定长摘要, 缺失时返回 None。

    该值完全由客户端决定，绝不能直接进桶表 / Redis key：
      - 限长上限：防止超长串撑爆键；
      - 摘要化：桶表里存的是定长 16 hex 字符而非原始串。
    它只是限流的**附加维度**，主维度永远是服务端认定的客户端 IP
    (见 `TokenBucketRateLimiter.key_for` 与 `_check_device_budget`)。
    """
    if not device_id:
        return None
    trimmed = device_id.strip()
    if not trimmed:
        return None
    # 单一客户端标识不可能很长；超长的一律截断后摘要，避免任何长度依赖。
    trimmed = trimmed[:_MAX_DEVICE_ID_LEN]
    return hashlib.sha256(trimmed.encode("utf-8")).hexdigest()[:16]


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
    """IP (+可选 Device-ID) 双维度键控令牌桶限流器 (纯内存，进程内共享)。

    两级配额，取**更严**者：

    - **IP 级** (`capacity`, 默认 60/min) —— 主维度，服务端认定的真实对端 IP。
      客户端带不带 X-Device-Id 都必须过这一关，因此轮换设备 ID 无法绕过。
    - **设备级** (`device_capacity`, 默认 IP 配额的 1/4, 下限 1) —— 附加维度，
      仅在客户端提供了 X-Device-Id 时生效。作用是让**单台设备**无法把共享
      NAT 出口 IP 的整份配额吃光：同一 NAT 下 4 台设备各打满设备桶恰好等于
      IP 配额，一致且不会互相饿死。

    没有设备头时只有 IP 级桶生效，行为与历史实现完全一致（不放大、不收紧）。
    """

    #: 设备桶配额 = IP 桶配额 * 该比例 (下限 1)。
    DEVICE_CAPACITY_RATIO = 0.25

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        refill_per_sec: float = DEFAULT_REFILL_PER_SEC,
    ) -> None:
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self.device_capacity = max(1, int(capacity * self.DEVICE_CAPACITY_RATIO))
        self._buckets: dict[str, TokenBucket] = {}

    def capacity_for(self, key: str) -> int:
        """键带设备维度 (`ip|hex`) 用设备级配额，纯 IP 键用 IP 级配额。"""
        return self.device_capacity if "|" in key else self.capacity

    def _bucket_for(self, key: str) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= _MAX_BUCKETS:
                # 淘汰最旧 10% 桶而非全量清空，避免攻击者挤爆桶表洗白限流。
                for old_key in list(self._buckets)[: _MAX_BUCKETS // 10]:
                    del self._buckets[old_key]
            bucket = TokenBucket(self.capacity_for(key), self.refill_per_sec)
            self._buckets[key] = bucket
        return bucket

    def allow(self, key: str) -> tuple[bool, float]:
        """对 key 尝试取令牌。返回 (放行?, 拒绝时等待秒数)。"""
        return self._bucket_for(key).allow()

    def key_for(self, client_ip: str, device_id: str | None = None) -> str:
        """限流主键：服务端认定的客户端 IP + 可选的设备维度摘要。

        IP 永远是**主维度**。`X-Device-Id` 只作为附加维度拼在后面，且必须先经
        `_normalized_device_id` 限长摘要——绝不单独作为键，否则攻击者只需轮换
        该头即可无限开桶彻底绕过 IP 限流 (顺带撑爆桶表触发全量清空)。

        IP 由 ASGI scope 认定；若部署在可信反代之后，由中间件解析可信 XFF
        (trusted_proxy_hops) 得到真实对端——这一跳是 audit P1-5 的要害：
        跳数配错会让全站共享同一个桶。
        """
        digest = _normalized_device_id(device_id)
        return f"{client_ip}|{digest}" if digest else client_ip


_REDIS_TOKEN_BUCKET_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local tokens = tonumber(redis.call('GET', KEYS[1]))
local last = tonumber(redis.call('GET', KEYS[2]))
if tokens == nil then tokens = capacity end
if last == nil then last = now end
tokens = math.min(capacity, tokens + math.max(0, now - last) / 1000 * refill)
local allowed = 0
local retry_ms = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry_ms = math.ceil((1 - tokens) / refill * 1000)
end
redis.call('SET', KEYS[1], tostring(tokens), 'PX', ttl)
redis.call('SET', KEYS[2], tostring(now), 'PX', ttl)
return {allowed, retry_ms}
"""


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
        trusted_proxy_hops: int = 0,
        redis_url: str | None = None,
        fail_closed: bool = False,
    ) -> None:
        self.app = app
        self.public_paths = public_paths or set()
        self.path_prefixes = path_prefixes or set()
        self.limiter = TokenBucketRateLimiter(capacity, refill_per_sec)
        self.redis_url = redis_url
        self.fail_closed = fail_closed
        self._redis: Any | None = None
        # >0 时信任前置反代注入的 X-Forwarded-For, 取倒数第 trusted_proxy_hops 跳
        # 作为真实对端 IP (跳数 = 自己到公网之间可信反代的层数)。0 = 直连, 只认
        # ASGI 对端。切勿在无可信反代时开启——XFF 客户端可伪造。
        self.trusted_proxy_hops = trusted_proxy_hops

    async def _allow(
        self,
        key: str,
        budget_keys: tuple[str, ...] = (),
    ) -> tuple[bool | None, float]:
        """取令牌。`budget_keys` 是需要**全部**满足的附加维度键 (见 `key_for`)。

        判定顺序很关键：**先扣更细的设备桶，再扣 IP 桶**。
        - 设备桶拒绝时不扣 IP 配额 → 一台被限速的设备不会加速耗尽共享 NAT 出口
          的 IP 配额，同 IP 下其他设备不受牵连；
        - IP 桶拒绝时设备令牌已扣也无妨——此时全 IP 已限流，损失不可观测。
        反过来 (先 IP 后设备) 会让设备维度的保护目标落空。
        """
        for extra in budget_keys:
            extra_allowed, extra_retry = await self._spend(extra)
            if extra_allowed is not True:
                return extra_allowed, extra_retry
        return await self._spend(key)

    async def _spend(self, key: str) -> tuple[bool | None, float]:
        if not self.redis_url:
            return self.limiter.allow(key)
        try:
            if self._redis is None:
                from redis.asyncio import from_url

                self._redis = from_url(self.redis_url, decode_responses=True)
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
            base = f"hemall:ratelimit:{digest}"
            capacity = self.limiter.capacity_for(key)
            ttl_ms = max(1000, math.ceil(capacity / self.limiter.refill_per_sec * 2000))
            result = await self._redis.eval(
                _REDIS_TOKEN_BUCKET_LUA,
                2,
                f"{base}:tokens",
                f"{base}:last",
                capacity,
                self.limiter.refill_per_sec,
                ttl_ms,
            )
            return bool(int(result[0])), max(0.0, float(result[1]) / 1000.0)
        except Exception:
            logger.exception("shared Redis rate limiter unavailable")
            if self.fail_closed:
                return None, 0.0
            return self.limiter.allow(key)

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
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        client = scope.get("client") or ("unknown", 0)
        client_ip = client[0] if isinstance(client, (tuple, list)) else str(client)
        if self.trusted_proxy_hops > 0:
            client_ip = _real_client_ip(
                headers.get("x-forwarded-for"), client_ip, self.trusted_proxy_hops
            )
        device_digest = _normalized_device_id(headers.get("x-device-id"))
        # 主键 = IP 级配额 (无设备头时与之相同)。
        key = self.limiter.key_for(client_ip)
        # 附加维度 = IP + 设备。只在客户端确实带了设备标识时才有第二个桶；
        # 它只会让限制更严，不会给 IP 配额开旁路。
        budget_keys: tuple[str, ...] = ()
        if device_digest:
            device_key = self.limiter.key_for(client_ip, headers.get("x-device-id"))
            if device_key != key:
                budget_keys = (device_key,)

        allowed, retry_after = await self._allow(key, budget_keys)
        if allowed is None:
            response = JSONResponse(
                status_code=503,
                content={"detail": "shared rate limiter unavailable"},
                headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
            return
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
