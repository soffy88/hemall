"""P1-5: 限流键必须是真实客户端 IP，且 `X-Device-Id` 只能加分不能绕过。

审计发现三个叠加问题，让生产限流退化成"全站共享一个 60/min 桶"：

1. `Settings.ratelimit_trusted_proxy_hops` 默认 0，而全仓库没有任何地方设置
   `HEMALL_RATELIMIT_TRUSTED_PROXY_HOPS` → `_real_client_ip` 直接返回 ASGI
   对端，即 Caddy 容器 IP，**所有访客同一个 key**；
2. `Caddyfile` 每个反代块都 `header_up X-Forwarded-For {remote_host}`，
   把真实客户端 IP 覆写成"反代的下一跳"，即使后端配了 hops 也还原不对；
3. README 声称按 "IP + X-Device-Id" 限流，实现只有 IP（docstring 里的
   "IP+Device_ID 键控"同样不实）。

后果：main.py:515-522 在 production 下 `fail_closed=True` 且共享 Redis，
一旦打满就是全站 429（含支付/下单页面 `/store/*`）。

本测试是行为门禁：
- 多客户端隔离：不同真实 IP 互不影响，同 IP 打到上限被拒；
- 可信跳数：hops>0 时按 XFF 还原真实客户端 IP，hops=0 时只认 ASGI 对端
  （防止伪造 XFF 绕过）；
- Device-Id 只是附加维度：轮换 X-Device-Id 不能突破 IP 级配额。
"""

from __future__ import annotations

import asyncio

import pytest

from app.middleware.ratelimit import (
    TokenBucketRateLimitMiddleware,
    _real_client_ip,
)

# ── ASGI 驱动小工具 ─────────────────────────────────────────────────────────


class _Recorder:
    """捕获下游是否被调用，以及最终响应状态码。"""

    def __init__(self) -> None:
        self.calls = 0
        self.status = 200

    async def __call__(self, scope, receive, send) -> None:
        self.calls += 1

    async def app(self, scope, receive, send) -> None:
        await self(scope, receive, send)


def _send(middleware, *, path="/store/feed", client="10.0.0.9", headers=None) -> int:
    """发一个请求，返回最终 HTTP 状态码 (200=放行, 429=限流, 503=fail-closed)。"""
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "path": path,
        "method": "GET",
        "headers": raw,
        "client": (client, 5555),
    }
    status = 200

    async def send(message) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(middleware(scope, receive, send))
    return status


def _middleware(*, capacity=5, trusted_proxy_hops=0, redis_url=None, fail_closed=False):
    downstream = _Recorder()
    mw = TokenBucketRateLimitMiddleware(
        downstream,
        path_prefixes={"/store/"},
        public_paths={"/payments/wechat/notify"},
        capacity=capacity,
        refill_per_sec=0.0001,  # 近似不补充，便于确定性断言
        trusted_proxy_hops=trusted_proxy_hops,
        redis_url=redis_url,
        fail_closed=fail_closed,
    )
    return mw, downstream


# ── _real_client_ip 单元 ────────────────────────────────────────────────────


def test_real_client_ip_contract():
    """XFF 链长 == 前置可信反代层数，从右数 trusted_hops 跳即真实客户端。

    每一跳追加的是"它收到的来源地址"，所以链里不含 ASGI 对端自己：
      1 跳: cloudflared 写入 "client"                  -> 取 parts[-1]
      2 跳: 上游再写 "client, proxy1"                  -> 取 parts[-2]
    链比可信跳数短说明缺层或被伪造，回退 ASGI 对端。
    """
    assert _real_client_ip("203.0.113.5", "10.0.0.2", 1) == "203.0.113.5"
    assert _real_client_ip("203.0.113.5, 10.0.0.1", "10.0.0.3", 2) == "203.0.113.5"
    assert _real_client_ip("203.0.113.5, 10.0.0.1, 10.0.0.2", "10.0.0.4", 3) == "203.0.113.5"


def test_real_client_ip_ignores_spoofed_left_entries():
    """链比可信跳数长 = 前面还有没登记的代理/伪造段，只信右侧可信跳数。"""
    assert _real_client_ip("1.2.3.4, 203.0.113.5", "10.0.0.9", 1) == "203.0.113.5"


def test_real_client_ip_rejects_shorter_chain_than_trusted_hops():
    """链比可信跳数短 = 缺层或被伪造，回退 ASGI 对端，不轻信客户端值。"""
    assert _real_client_ip("203.0.113.5", "10.0.0.9", 3) == "10.0.0.9"
    assert _real_client_ip(None, "10.0.0.9", 1) == "10.0.0.9"
    assert _real_client_ip("", "10.0.0.9", 1) == "10.0.0.9"
    assert _real_client_ip("  ", "10.0.0.9", 1) == "10.0.0.9"


# ── 多客户端隔离 (核心) ─────────────────────────────────────────────────────


def test_distinct_client_ips_do_not_share_a_bucket():
    """直接连接 (hops=0) 时不同 ASGI 对端各用各的配额。"""
    mw, _ = _middleware(capacity=2)
    for _ in range(2):
        assert _send(mw, client="198.51.100.1") == 200
    # 该 IP 已打满
    assert _send(mw, client="198.51.100.1") == 429
    # 另一个 IP 完全不受影响 —— 这是审计 P1-5 声称要恢复的隔离
    assert _send(mw, client="198.51.100.2") == 200
    assert _send(mw, client="198.51.100.2") == 200
    assert _send(mw, client="198.51.100.3") == 200


def test_trusted_proxy_hops_isolates_clients_behind_one_proxy():
    """hops=1 + 网关注入的真实 XFF：所有请求的 ASGI 对端都是 Caddy 容器。"""
    mw, _ = _middleware(capacity=2, trusted_proxy_hops=1)
    caddy_peer = "172.20.0.7"  # Caddy 容器 IP，对所有访客相同
    for _ in range(2):
        assert _send(mw, client=caddy_peer, headers={"X-Forwarded-For": "203.0.113.1"}) == 200
    assert _send(mw, client=caddy_peer, headers={"X-Forwarded-For": "203.0.113.1"}) == 429
    # 隔壁访客不受牵连
    assert _send(mw, client=caddy_peer, headers={"X-Forwarded-For": "203.0.113.2"}) == 200


def test_without_trusted_hops_xff_is_ignored():
    """hops=0 时必须无视客户端伪造的 XFF，否则限流可被一行头绕过。"""
    mw, _ = _middleware(capacity=2, trusted_proxy_hops=0)
    # 换 20 个伪造 XFF 也不会开出新桶：ASGI 对端相同即同一配额
    for i in range(20):
        status = _send(mw, client="10.0.0.5", headers={"X-Forwarded-For": f"1.1.1.{i}"})
        assert status == (200 if i < 2 else 429), f"第 {i} 次请求状态码异常: {status}"


# ── X-Device-Id 是附加维度，不能绕过 IP 配额 ─────────────────────────────────


def test_device_id_rotation_cannot_bypass_ip_limit():
    """README 声称 "IP + X-Device-Id" 限流：实现必须做到加维度而不开旁路。"""
    mw, _ = _middleware(capacity=3)
    for i in range(3):
        assert _send(mw, client="198.51.100.7", headers={"X-Device-Id": f"dev-{i}"}) == 200
    # 轮换设备 ID 不能再要到令牌
    for i in range(3, 10):
        assert _send(mw, client="198.51.100.7", headers={"X-Device-Id": f"dev-{i}"}) == 429


def test_device_id_still_separates_devices_behind_one_nat_ip():
    """共享 NAT 的两台设备各有独立设备桶 (这正是 "IP + X-Device-Id" 的意义)。

    capacity=4 → IP 桶 4 令牌, 设备桶 = 4 * 0.25 = 1 令牌。
    phone-a 打满自己的设备桶后仍被拦, 而同 NAT 下 phone-b 照常可用 ——
    IP 配额 (4) 远未被消耗完。
    """
    mw, _ = _middleware(capacity=4, trusted_proxy_hops=1)
    nat = "203.0.113.50"
    hx = {"X-Forwarded-For": nat}

    assert _send(mw, client="172.20.0.7", headers={**hx, "X-Device-Id": "phone-a"}) == 200
    assert _send(mw, client="172.20.0.7", headers={**hx, "X-Device-Id": "phone-a"}) == 429

    for i in range(3):
        assert _send(mw, client="172.20.0.7", headers={**hx, "X-Device-Id": f"phone-b{i}"}) == 200
    # 4 次消耗光 IP 桶的 4 枚令牌后, 无论换什么设备 ID 都被 IP 桶挡住
    assert _send(mw, client="172.20.0.7", headers={**hx, "X-Device-Id": "phone-z"}) == 429


def test_single_device_cannot_burn_whole_nat_quota():
    """单个设备不能把整个 NAT IP 的配额吃光 (设备桶是 IP 桶的 1/4)。"""
    mw, _ = _middleware(capacity=8, trusted_proxy_hops=1)
    nat = "203.0.113.51"
    hx = {"X-Forwarded-For": nat}
    # 设备桶 = 8 * 0.25 = 2 令牌
    assert _send(mw, client="172.20.0.7", headers={**hx, "X-Device-Id": "rogue"}) == 200
    assert _send(mw, client="172.20.0.7", headers={**hx, "X-Device-Id": "rogue"}) == 200
    assert _send(mw, client="172.20.0.7", headers={**hx, "X-Device-Id": "rogue"}) == 429
    # 同 IP 的正常设备还有 6 枚 IP 配额可用
    for i in range(3):
        assert _send(mw, client="172.20.0.7", headers={**hx, "X-Device-Id": f"ok-{i}"}) == 200


def test_no_device_header_uses_ip_budget_only():
    """不带设备头时行为与历史实现一致：只有 IP 级 60/min，不放大也不收紧。"""
    from app.middleware.ratelimit import TokenBucketRateLimiter

    limiter = TokenBucketRateLimiter(capacity=60, refill_per_sec=1.0)
    assert limiter.device_capacity == 15
    assert limiter.capacity_for("203.0.113.9") == 60
    assert limiter.capacity_for(limiter.key_for("203.0.113.9", "dev-1")) == 15


def test_device_capacity_never_drops_below_one():
    from app.middleware.ratelimit import TokenBucketRateLimiter

    assert TokenBucketRateLimiter(capacity=1).device_capacity == 1
    assert TokenBucketRateLimiter(capacity=3).device_capacity == 1


def test_hostile_device_id_is_bounded_and_hashed():
    """客户端可控值必须限长并摘要化，不得直接进 Redis key / 桶表。"""
    from app.middleware.ratelimit import TokenBucketRateLimiter

    limiter = TokenBucketRateLimiter(capacity=10, refill_per_sec=1.0)
    key = limiter.key_for("203.0.113.9", "x" * 4096)
    assert len(key) < 200, "设备 ID 未限长，桶表键可被撑爆"
    assert "x" * 100 not in key, "原始设备 ID 不应直接出现在键里"
    assert key.startswith("203.0.113.9"), "IP 必须是键的主维度"


def test_empty_device_id_falls_back_to_ip_only_key():
    from app.middleware.ratelimit import TokenBucketRateLimiter

    limiter = TokenBucketRateLimiter(capacity=10, refill_per_sec=1.0)
    assert limiter.key_for("203.0.113.9", None) == limiter.key_for("203.0.113.9", "")


# ── 网关侧配置防漂移 ────────────────────────────────────────────────────────


def _compose_env_text() -> str:

    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(root.glob("docker-compose*.yml"))
        if "app:" in p.read_text(encoding="utf-8") or "api:" in p.read_text(encoding="utf-8")
    )


def test_production_compose_sets_trusted_proxy_hops():
    """生产 compose 必须告诉后端"前面有 1 层可信反代"，否则键退化成容器 IP。"""
    assert "HEMALL_RATELIMIT_TRUSTED_PROXY_HOPS" in _compose_env_text(), (
        "docker-compose*.yml 未注入 HEMALL_RATELIMIT_TRUSTED_PROXY_HOPS——"
        "限流键会退化成反代容器 IP，全站共享一个桶 (audit P1-5)"
    )


def test_helm_sets_trusted_proxy_hops():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    deployment = (root / "charts" / "hemal" / "templates" / "deployment.yaml").read_text(
        encoding="utf-8"
    )
    assert "HEMALL_RATELIMIT_TRUSTED_PROXY_HOPS" in deployment


def test_caddyfile_preserves_real_client_ip():
    """Caddy 不得把 XFF 覆写成"反代的下一跳"，否则后端无从还原真实客户端。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / "Caddyfile").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "header_up X-Forwarded-For {remote_host}" not in body, (
        "Caddyfile 仍把 X-Forwarded-For 覆写为 {remote_host}，真实客户端 IP 丢失 (audit P1-5)"
    )
    assert "{http.request.client_ip}" in body, (
        "Caddyfile 应使用 {http.request.client_ip}（已考虑 trusted_proxies 的真实客户端 IP）"
    )


def test_caddyfile_declares_trusted_proxies():
    """{http.request.client_ip} 只有配置了 trusted_proxies 才会解析 XFF。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / "Caddyfile").read_text(encoding="utf-8")
    assert "trusted_proxies" in text, "Caddyfile 未声明 trusted_proxies，{client_ip} 会退化成下一跳"


@pytest.mark.parametrize(
    "conf", ["nginx.conf.template", "nginx.mall-sxueji.conf.template"]
)
def test_nginx_passes_client_ip_through(conf: str):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / conf).read_text(encoding="utf-8")
    assert "proxy_set_header X-Real-IP $remote_addr;" in text, (
        f"{conf} 未透传 X-Real-IP"
    )
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in text, (
        f"{conf} 未按累加方式透传 X-Forwarded-For"
    )
