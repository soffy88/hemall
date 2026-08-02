"""结构化日志中间件 — 请求级日志 + TraceID 传播。

Phase 0 交付：所有日志输出 JSON 格式，含 trace_id/span_id，支持 ELK 采集。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

# ── structlog 配置 (进程启动期调用一次) ─────────────────────────────────


def configure_structlog() -> None:
    """配置 structlog 输出 JSON 格式，集成标准 logging。"""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ── 请求日志中间件 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class RequestLogInfo:
    """请求日志信息聚合。"""

    trace_id: str
    method: str
    path: str
    status_code: int
    latency_ms: float
    client_ip: str
    user_agent: str | None


class LoggingMiddleware(BaseHTTPMiddleware):
    """请求级结构化日志中间件。

    - 从请求头读取/生成 trace_id (X-Trace-ID)
    - 记录请求方法/路径/状态码/延迟
    - 输出 JSON 格式，便于 ELK/ Loki 采集
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.perf_counter()
        trace_id = request.headers.get("x-trace-id", str(uuid4()))

        # 绑定 trace_id 到 structlog 上下文 (当前协程内自动传播)
        logger = structlog.get_logger().bind(
            trace_id=trace_id,
            method=request.method,
            path=request.url.path,
            client_ip=request.client.host if request.client else "unknown",
        )

        # 向下游传递 trace_id (微服务间链路追踪)
        # 注意：这里不能修改 request.headers (immutable)，由调用方在网关层设置

        try:
            response = await call_next(request)
        except Exception:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.exception(
                "request_failed",
                latency_ms=latency_ms,
            )
            raise

        latency_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            "request_completed",
            status_code=response.status_code,
            latency_ms=round(latency_ms, 2),
        )

        # 在响应头中返回 trace_id (方便前端/调试追踪)
        response.headers["x-trace-id"] = trace_id

        return response


# ── 日志辅助函数 ────────────────────────────────────────────────────────


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """获取绑定 logger，推荐在模块顶层调用。

    Usage:
        logger = get_logger(__name__)
        logger.info("something_happened", extra_field="value")
    """
    return structlog.get_logger(name)
