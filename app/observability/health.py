"""健康检查端点 — 服务存活与依赖状态。

Phase 0 交付：/health/live (存活探针) + /health/ready (就绪探针)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response

from ..deps import get_settings

router = APIRouter(prefix="/health", tags=["observability"])


async def check_db_health(pool: Any | None) -> dict[str, Any]:
    """检查数据库连接。"""
    if pool is None:
        return {"status": "down", "message": "pool not initialized"}

    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "up"}
    except Exception as e:
        return {"status": "down", "message": str(e)}


async def check_redis_health(redis_url: str) -> dict[str, Any]:
    """检查 Redis 连接。"""
    try:
        import redis.asyncio as redis

        client = redis.from_url(redis_url)
        await client.ping()
        await client.close()
        return {"status": "up"}
    except Exception as e:
        return {"status": "down", "message": str(e)}


@router.get("/live")
async def health_live() -> dict[str, str]:
    """存活探针 (liveness probe) — 进程存活即健康。

    Kubernetes 用此判断是否重启容器。
    """
    return {"status": "alive"}


@router.get("/ready")
async def health_ready(
    response: Response,
    settings: Any = Depends(get_settings),
) -> dict[str, Any]:
    """就绪探针 (readiness probe) — 依赖就绪才可接收流量。

    Kubernetes 用此判断是否加入 Service 负载均衡。
    降级策略：DB/Redis 不可用时返 503 + degraded，K8s/网关据此摘流。
    """
    # 延迟导入，避免与 main.py 的循环导入
    from ..main import get_app_state

    state = get_app_state()
    checks: dict[str, dict[str, Any]] = {}
    overall_status = "healthy"

    # DB 检查
    db_result = await check_db_health(state.pool)
    checks["database"] = db_result
    if db_result["status"] == "down":
        overall_status = "degraded"

    # Redis 检查
    redis_result = await check_redis_health(settings.redis_url)
    checks["redis"] = redis_result
    if redis_result["status"] == "down":
        overall_status = "degraded"

    if overall_status != "healthy":
        response.status_code = 503
    return {
        "status": overall_status,
        "version": state.version,
        "checks": checks,
    }


@router.get("/metrics/summary")
async def health_metrics_summary() -> dict[str, Any]:
    """指标摘要 — 快速查看核心指标 (不依赖 Prometheus)。"""
    from prometheus_client import REGISTRY

    metrics_summary: dict[str, Any] = {
        "http_requests_total": 0,
        "active_requests": 0,
    }

    try:
        for metric in REGISTRY.collect():
            if metric.name == "http_requests_total":
                for sample in metric.samples:
                    metrics_summary["http_requests_total"] += int(sample.value)
            elif metric.name == "http_requests_in_progress":
                for sample in metric.samples:
                    metrics_summary["active_requests"] += int(sample.value)
    except Exception:
        pass  # 指标采集失败不影响健康检查

    return metrics_summary
