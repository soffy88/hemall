"""Prometheus 指标采集中间件。

Phase 0 交付：/metrics 端点暴露 QPS/延迟/错误率等核心指标。
"""

from __future__ import annotations

import time
from typing import Callable

from fastapi import Request, Response
from prometheus_client import Counter, Gauge, Histogram
from starlette.middleware.base import BaseHTTPMiddleware

# ── 指标定义 (进程级单例) ────────────────────────────────────────────────

#: HTTP 请求总数 (按 method/path/status 分组)
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)

#: HTTP 请求延迟直方图 (秒)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

#: 当前活跃请求数
HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "Number of HTTP requests currently being processed",
    ["method"],
)

#: DB 连接池状态 (由外部更新)
DB_POOL_SIZE = Gauge("db_pool_size", "Database connection pool size", ["pool_name"])
DB_POOL_USED = Gauge("db_pool_used", "Database connections in use", ["pool_name"])

#: Redis 连接状态 (由外部更新)
REDIS_CONNECTED = Gauge("redis_connected", "Redis connection status")


# ── 指标采集中间件 ──────────────────────────────────────────────────────


class MetricsMiddleware(BaseHTTPMiddleware):
    """Prometheus 指标采集中间件。

    - 记录每个请求的 QPS/延迟
    - 跟踪活跃请求数
    - 路径分组 (避免 /products/{id} 产生无限基数)
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 路径规范化：/products/{uuid} → /products/{id}
        path = self._normalize_path(request.url.path)

        method = request.method
        HTTP_REQUESTS_IN_PROGRESS.labels(method=method).inc()

        start_time = time.perf_counter()

        try:
            response = await call_next(request)
            status = str(response.status_code)
        except Exception:
            status = "500"
            raise
        finally:
            duration = time.perf_counter() - start_time
            HTTP_REQUESTS_IN_PROGRESS.labels(method=method).dec()
            HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status=status).inc()
            HTTP_REQUEST_DURATION.labels(method=method, path=path).observe(duration)

        return response

    def _normalize_path(self, path: str) -> str:
        """规范化路径，避免高基数问题。

        /products/550e8400-e29b-41d4-a716-446655440000 → /products/{id}
        /orders/123/items/456 → /orders/{id}/items/{id}
        """
        import re

        # UUID 模式
        path = re.sub(
            r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            "/{id}",
            path,
            flags=re.IGNORECASE,
        )
        # 数字 ID 模式
        path = re.sub(r"/\d+", "/{id}", path)
        return path


# ── 指标辅助函数 ────────────────────────────────────────────────────────


def update_db_pool_metrics(pool_name: str, size: int, used: int) -> None:
    """更新 DB 连接池指标 (由 deps.py 调用)。"""
    DB_POOL_SIZE.labels(pool_name=pool_name).set(size)
    DB_POOL_USED.labels(pool_name=pool_name).set(used)


def update_redis_connected(connected: bool) -> None:
    """更新 Redis 连接状态指标。"""
    REDIS_CONNECTED.set(1 if connected else 0)
