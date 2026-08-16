"""CQRS 事件溯源 API 路由 — 事件查询、聚合状态、快照管理。

暴露事件溯源能力给其他服务:
    GET /eventsourcing/events/{aggregate_id}  # 获取事件流
    POST /eventsourcing/snapshot              # 手动创建快照
    GET /eventsourcing/projections            # 查看投影状态

诚实降级原则: 事件溯源仍未接入全局状态 (app_state.event_store 为空) 时,
返回真实空态并明示未接入, 绝不伪造事件/投影数据。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..config import Settings
from ..deps import get_settings
from .service import EventBus, EventStore, ProjectorRegistry

router = APIRouter(prefix="/eventsourcing", tags=["eventsourcing"])
logger = logging.getLogger("hemall.eventsourcing.router")


def get_event_bus(
    settings: Settings = Depends(get_settings),
) -> EventBus:
    """获取事件总线实例。"""
    from app.main import get_app_state

    app_state = get_app_state()
    if not hasattr(app_state, "event_bus") or app_state.event_bus is None:
        raise HTTPException(status_code=503, detail="EventBus not initialized")
    return app_state.event_bus


def _get_event_store() -> EventStore | None:
    """从全局状态取事件存储; 未接入时返回 None (调用方诚实降级)。"""
    from app.main import get_app_state

    app_state = get_app_state()
    store = getattr(app_state, "event_store", None)
    if store is None or not getattr(store, "_initialized", False):
        return None
    return store


def _get_registry() -> ProjectorRegistry | None:
    """从全局状态取投影注册表; 未接入时返回 None。"""
    from app.main import get_app_state

    app_state = get_app_state()
    registry = getattr(app_state, "projector_registry", None)
    if registry is None:
        return None
    return registry


@router.get("/events/{aggregate_id}")
async def get_aggregate_events(
    aggregate_id: str,
    from_version: int = 0,
    max_count: int = 100,
) -> dict[str, Any]:
    """获取聚合的事件流 (用于调试/审计)。未接入时明示空态。"""
    store = _get_event_store()
    if store is None:
        return {
            "aggregate_id": aggregate_id,
            "available": False,
            "detail": "EventStore 未接入 (app_state.event_store 为空)",
            "events": [],
            "from_version": from_version,
            "max_count": max_count,
        }
    events = await store.get_events(aggregate_id, from_version, max_count)
    return {
        "aggregate_id": aggregate_id,
        "available": True,
        "events": [e.to_dict() for e in events],
        "from_version": from_version,
        "max_count": max_count,
    }


@router.post("/snapshot")
async def create_snapshot(
    aggregate_id: str = "",
    aggregate_type: str = "",
) -> dict[str, Any]:
    """手动触发聚合快照创建 (取该聚合最新事件生成快照)。"""
    store = _get_event_store()
    if store is None:
        raise HTTPException(status_code=503, detail="EventStore 未接入")
    if not aggregate_id:
        raise HTTPException(status_code=422, detail="aggregate_id 必填")

    events = await store.get_events(aggregate_id, 0, 1)
    if not events:
        raise HTTPException(status_code=404, detail=f"聚合 {aggregate_id} 无事件可快照")
    latest = events[-1]
    created = await store._try_create_snapshot(aggregate_id, latest)
    if not created:
        raise HTTPException(status_code=500, detail="快照创建失败")
    return {
        "status": "created",
        "aggregate_id": aggregate_id,
        "version": latest.version,
    }


@router.get("/projections")
async def list_projections() -> dict[str, Any]:
    """列出所有已注册的投影及其状态 (真实注册表, 未接入时为空)。"""
    registry = _get_registry()
    if registry is None:
        return {
            "available": False,
            "detail": "ProjectorRegistry 未接入 (app_state.projector_registry 为空)",
            "projections": [],
        }
    projections = []
    for event_type, projs in registry._projections.items():
        for proj in projs:
            projections.append(
                {"name": type(proj).__name__, "event_type": event_type.value}
            )
    return {"available": True, "projections": projections}


@router.get("/health")
async def health_check() -> dict[str, Any]:
    """事件溯源服务健康检查 (如实上报各组件接入状态, 不谎报)。"""
    from app.main import get_app_state

    app_state = get_app_state()
    store = _get_event_store()
    registry = _get_registry()
    event_bus = getattr(app_state, "event_bus", None)

    components = {
        "event_store": "connected" if store is not None else "not_initialized",
        "projector_registry": "active" if registry is not None else "not_initialized",
        "event_bus": "operational" if event_bus is not None else "not_initialized",
    }
    all_ready = all(v == k and v != "not_initialized" for k, v in components.items())
    status = "healthy" if all_ready else "degraded"
    return {"status": status, "components": components}