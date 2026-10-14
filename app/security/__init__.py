"""hemall 安全模块 — 限流、加密、审计、CSRF。"""

from __future__ import annotations

__all__ = ["CSRFMiddleware"]


def __getattr__(name: str):
    # PEP 562 惰性导出：csrf.py 依赖 app.deps，而 app.deps 又需要本包下的
    # tokens 常量；eager import 会成环，按需加载则两边都已初始化。
    if name == "CSRFMiddleware":
        from .csrf import CSRFMiddleware

        return CSRFMiddleware
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
