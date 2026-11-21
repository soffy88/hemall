#!/usr/bin/env python3
"""从 FastAPI app 导出已注册路由的顶层前缀，供反代配置 (Caddyfile/nginx) 核对。

审计 P0-2：生产域名 Caddyfile 只反代了部分后端前缀，`/payments/*`、`/ext/*`、
`/ws` 等全部落到 Next.js 兜底 → 支付回调 404、边缘网桥硬件事件全丢。

本脚本是"防漂移"的数据源：
    python scripts/export_route_prefixes.py            # 打印前缀表
    python scripts/export_route_prefixes.py --json     # 机器可读

tests/test_gateway_route_coverage.py 直接复用 `route_prefixes()`，把
Caddyfile / nginx*.conf 的覆盖情况与真实路由表比对，新增后端前缀而忘记
补反代配置时测试立即变红。

注：app.main 的部分 router 以惰性包装 (`_IncludedRouter`) 注册，直接读
`app.routes` 只能看到壳，必须递归展开 `original_router` 才能拿到完整路径。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 根路径 (/) 不携带顶级前缀；后端根路由是服务状态页，由网关默认处理。
ROOT_PREFIX = "/"

# 这些顶层段由 Next.js 前端渲染页面，网关必须转发给 web 而不是 api。
# 取自 Caddyfile 注释 "Next.js 顶层路由仅 /admin /login /shop /" 与
# web/app 目录实际存在的页面路由。
FRONTEND_OWNED_TOP_SEGMENTS = frozenset({"shop", "login"})


def iter_route_paths(app: object) -> Iterator[str]:
    """递归展开惰性 router，产出全部已注册路径 (含 websocket)。"""
    seen: set[int] = set()
    stack: list[object] = [app]
    while stack:
        node = stack.pop()
        routes = getattr(node, "routes", None)
        if routes is None:
            continue
        for route in routes:
            path = getattr(route, "path", None)
            if isinstance(path, str) and id(route) not in seen:
                seen.add(id(route))
                yield path
            # 惰性 include 包装：真正的路由在 original_router 上
            original = getattr(route, "original_router", None)
            if original is not None:
                stack.append(original)
            # Mount 的子应用
            sub = getattr(route, "app", None)
            if sub is not None and hasattr(sub, "routes"):
                stack.append(sub)


def top_prefix(path: str) -> str:
    """取路径的顶层前缀：/payments/wechat/notify -> /payments。"""
    parts = path.split("/")
    if len(parts) < 2 or not parts[1]:
        return ROOT_PREFIX
    return "/" + parts[1]


def route_prefixes(app: object | None = None) -> dict[str, list[str]]:
    """返回 {顶层前缀: [该前缀下的全部路径...]}，字典按前缀排序。"""
    if app is None:
        from app.main import app as fastapi_app

        app = fastapi_app
    grouped: dict[str, list[str]] = {}
    for path in iter_route_paths(app):
        grouped.setdefault(top_prefix(path), []).append(path)
    return {prefix: sorted(set(paths)) for prefix, paths in sorted(grouped.items())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    prefixes = route_prefixes()
    if args.json:
        json.dump(prefixes, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    for prefix, paths in prefixes.items():
        print(f"{prefix:16s} {len(paths):4d}  {paths[0]}")
    print(f"\n共 {len(prefixes)} 个顶层前缀")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
