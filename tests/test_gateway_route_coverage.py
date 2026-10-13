"""P0-2: 生产入口反代必须覆盖 FastAPI 实际注册的全部顶层路由前缀。

审计发现：`Caddyfile` 的路由白名单正则只列了 18 个前缀，
`/payments`、`/ext`、`/ai`、`/eventsourcing`、`/orders`、`/search`、
`/cache`、`/push`、`/risk`、`/ws` **全部不在任何 handle 中**，会落到兜底
`reverse_proxy hemal-web-1:3000` → Next.js 404：

- `docs/deployment.md` 指导商户配置 `https://<域名>/payments/wechat/notify`，
  实际会 404 → 微信/支付宝/Stripe 收款结果无法回传；
- `/ext/hardware/webhook` 404 → 边缘网桥硬件事件全丢；
- `/ws` 404 → 实时推送不可用。

本测试是防漂移门禁：
  1. 从 `app.main.app` 导出真实顶层前缀 (复用 scripts/export_route_prefixes.py)；
  2. 解析 Caddyfile 的 `handle /x*` 字面量 + `path_regexp api ^(...)` 备择集；
  3. 断言两者之并覆盖全部后端前缀 (前端自有路径除外)。

同时对 nginx*.conf 做同样的覆盖核对——nginx 靠 `location /` 兜底转发，
但一旦有人给某个前缀加 `location /x { return 404; }` 就会静默断链。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.export_route_prefixes import (  # noqa: E402
    FRONTEND_OWNED_TOP_SEGMENTS,
    ROOT_PREFIX,
    route_prefixes,
)

CADDYFILE = REPO_ROOT / "Caddyfile"
NGINX_CONFS = sorted(REPO_ROOT.glob("nginx*.conf.template"))

# 后端根路由 "/" 由网关默认兜底处理，不参与前缀覆盖断言。
_BACKEND_PREFIXES = frozenset(
    p for p in route_prefixes() if p not in FRONTEND_OWNED_TOP_SEGMENTS | {ROOT_PREFIX}
)


# ── Caddyfile 解析 ──────────────────────────────────────────────────────────


def _strip_caddy_comments(text: str) -> str:
    """去掉 # 行注释（注意不要误伤 URL 里的 #，本文件不含）。"""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _caddy_literal_handles(text: str) -> set[str]:
    """收集 `handle /store/*` / `handle /docs*` 形式的字面量前缀。"""
    handles: set[str] = set()
    for raw in _strip_caddy_comments(text).splitlines():
        line = raw.strip()
        m = re.match(r"^handle\s+(/[\w./-]*?)(?:\*|/\*)?\s*\{", line)
        if not m:
            continue
        prefix = m.group(1).rstrip("/")
        if prefix and prefix != "":
            handles.add(prefix)
    return handles


def _caddy_regexp_alternatives(text: str) -> set[str]:
    """收集 `@omodul path_regexp api ^/(a|b|c)(/.*)?$` 的备择前缀。"""
    alts: set[str] = set()
    for m in re.finditer(
        r"path_regexp\s+\S+\s+\^?/\(([^)]*)\)", _strip_caddy_comments(text)
    ):
        for alt in m.group(1).split("|"):
            alt = alt.strip().rstrip("*")
            if alt:
                alts.add("/" + alt)
    return alts


def caddy_api_prefixes() -> set[str]:
    text = CADDYFILE.read_text(encoding="utf-8")
    return _caddy_literal_handles(text) | _caddy_regexp_alternatives(text)


# ── nginx 解析 ──────────────────────────────────────────────────────────────


def _nginx_locations(text: str) -> list[tuple[str, str]]:
    """返回 [(location 路径, 块体)]，按出现顺序。"""
    locs: list[tuple[str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^\s*location\s+(\S+)\s*\{\s*$", lines[i])
        if not m:
            i += 1
            continue
        path = m.group(1)
        depth = 1
        body: list[str] = []
        i += 1
        while i < len(lines) and depth > 0:
            depth += lines[i].count("{") - lines[i].count("}")
            if depth > 0:
                body.append(lines[i])
            i += 1
        locs.append((path, "\n".join(body)))
    return locs


def _nginx_prefix_regex(text: str, prefix: str) -> bool:
    """判断 nginx 配置里是否存在覆盖该前缀的 location。"""
    for path, body in _nginx_locations(text):
        if path == prefix or path == prefix + "/" or path.startswith(prefix):
            return True
        # 正则形式 location (如 ~ ^/payments)
        if path.startswith(("~", "^", "*")) and re.search(re.escape(prefix), path):
            return True
        del body
    return False


def _nginx_serves_via_backend(text: str, path: str) -> bool:
    for loc_path, body in _nginx_locations(text):
        if loc_path != path:
            continue
        # 返回静态/404 而不转发 → 该前缀断链
        if re.search(r"\breturn\s+(404|403|301|302)\b", body):
            return False
        if "proxy_pass" in body or "fastcgi_pass" in body or "grpc_pass" in body:
            return True
    return False


def _nginx_covers_prefix(text: str, prefix: str) -> bool:
    """前缀被 `location /` 兜底转发即算覆盖。"""
    for path, body in _nginx_locations(text):
        if path != "/":
            continue
        if "proxy_pass" in body and not re.search(r"\breturn\s+404\b", body):
            return True
    # 否则必须有精确 location 且转发到后端
    return _nginx_prefix_regex(text, prefix) and _nginx_serves_via_backend(
        text, prefix
    )


# ── 测试 ────────────────────────────────────────────────────────────────────


def test_backend_prefixes_discovered():
    """前置事实：脚本真的从 app 导出了非空前缀集。"""
    assert len(_BACKEND_PREFIXES) >= 30, (
        f"路由前缀导出异常，只拿到 {sorted(_BACKEND_PREFIXES)}——"
        "惰性 router 展开可能失效，测试会失去意义"
    )


@pytest.mark.parametrize("prefix", sorted(_BACKEND_PREFIXES))
def test_caddyfile_routes_prefix_to_backend(prefix: str):
    covered = caddy_api_prefixes()
    assert prefix in covered, (
        f"Caddyfile 未把 {prefix}/* 反代到后端，请求会落到 Next.js 兜底 404。"
        f"已覆盖前缀: {sorted(covered)}"
    )


def test_caddyfile_covers_documented_payment_callbacks():
    """文档指导商户填的回调 URL 必须同时 (a) 在 app 注册 (b) 被网关反代。

    这条把 README/docs → app 路由 → Caddyfile 三者串起来：
    文档写的回调 URL 若 app 没注册，商户会收到 404；
    app 注册了但网关没反代，同样 404 —— 审计 P0-2 就是后者。
    """
    prefixes = route_prefixes()
    covered = caddy_api_prefixes()
    callbacks: set[str] = set()
    for doc in ("README.md", "docs/deployment.md", "docs/observability.md"):
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        callbacks.update(re.findall(r"/payments/[a-z_]+/(?:notify|webhook)", text))
    assert callbacks, "未能从文档中解析出任何支付回调 URL，防漂移锚点失效"

    for url in sorted(callbacks):
        assert url in prefixes.get("/payments", []), (
            f"{doc_url_of(url)} 文档指导商户配置的回调 {url} 在 app 中不存在"
        )
        assert "/payments" in covered, f"{url} 未被 Caddyfile 反代到后端"


def doc_url_of(callback_url: str) -> str:
    for doc in ("README.md", "docs/deployment.md", "docs/observability.md"):
        if callback_url in (REPO_ROOT / doc).read_text(encoding="utf-8"):
            return doc
    return "docs"


def test_caddyfile_covers_hardware_webhook_and_ws():
    covered = caddy_api_prefixes()
    assert "/ext" in covered, "/ext/hardware/webhook 404 → 边缘网桥硬件事件全丢"
    assert "/ws" in covered, "/ws 404 → 实时推送不可用"


@pytest.mark.parametrize("conf", NGINX_CONFS, ids=lambda p: p.name)
@pytest.mark.parametrize("prefix", sorted(_BACKEND_PREFIXES))
def test_nginx_conf_serves_prefix(conf: Path, prefix: str):
    text = conf.read_text(encoding="utf-8")
    assert _nginx_covers_prefix(text, prefix), (
        f"{conf.name} 未把 {prefix} 前缀转发到后端"
    )


@pytest.mark.parametrize("conf", NGINX_CONFS, ids=lambda p: p.name)
def test_nginx_conf_has_backend_catchall(conf: Path):
    text = conf.read_text(encoding="utf-8")
    paths = [p for p, _ in _nginx_locations(text)]
    assert "/" in paths, f"{conf.name} 缺少 `location /` 兜底转发，新增后端前缀会断链"


def test_nginx_confs_are_not_empty():
    assert NGINX_CONFS, "未找到 nginx*.conf.template，P0-2 的 nginx 核对形同虚设"
