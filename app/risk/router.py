"""风控引擎 API 路由 — 风险评估、规则管理、审核队列。

Phase 3 Priority 2: 实时风控拦截，保护平台安全。

API:
    POST /risk/evaluate      # 风险评估
    GET  /risk/rules         # 获取风控规则列表
    POST /risk/rules        # 添加自定义规则
    GET  /risk/review       # 获取待审核交易
    POST /risk/review/{id}  # 审核交易结果
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import Settings
from ..deps import get_current_user, get_settings
from .engine import RiskEngine, RiskEvent, RiskRule

# 风控管理 API：改规则、看/裁决审核队列 —— 后台风控台专用，一律要求员工 JWT。
router = APIRouter(
    prefix="/risk", tags=["risk"], dependencies=[Depends(get_current_user)]
)
logger = logging.getLogger("hemall.risk.router")


def get_risk_engine(
    settings: Settings = Depends(get_settings),
) -> RiskEngine:
    """获取风控引擎实例。"""
    from app.main import get_app_state

    app_state = get_app_state()
    if not hasattr(app_state, "risk_engine") or app_state.risk_engine is None:
        raise HTTPException(status_code=503, detail="RiskEngine not initialized")
    return app_state.risk_engine


@router.post("/evaluate")
async def evaluate_risk(
    event_type: str = Query(..., description="事件类型: login/payment/order/register"),
    user_id: str = Query(..., description="用户 ID"),
    ip_address: str = Query("", description="请求 IP"),
    device_id: str = Query("", description="设备指纹"),
    payload: dict[str, Any] | None = None,
    engine: RiskEngine = Depends(get_risk_engine),
) -> dict[str, Any]:
    """评估事件风险等级。"""
    event = RiskEvent(
        event_type=event_type,
        user_id=user_id,
        ip_address=ip_address,
        device_id=device_id,
        payload=payload or {},
    )
    result = await engine.evaluate(event)
    return {"data": result.model_dump(mode="json")}


@router.get("/rules")
async def list_rules(
    engine: RiskEngine = Depends(get_risk_engine),
) -> dict[str, Any]:
    """获取风控规则列表。"""
    rules = [
        {
            "rule_id": r.rule_id,
            "name": r.name,
            "category": r.category.value,
            "condition": r.condition,
            "score": r.score,
            "enabled": r.enabled,
        }
        for r in engine._rules
    ]
    return {"data": rules}


@router.post("/rules")
async def add_rule(
    rule: RiskRule,
    engine: RiskEngine = Depends(get_risk_engine),
) -> dict[str, Any]:
    """添加自定义风控规则。"""
    engine.add_rule(rule)
    return {"status": "added", "rule_id": rule.rule_id}


@router.get("/review")
async def get_review_queue(
    limit: int = Query(20, ge=1, le=100),
    engine: RiskEngine = Depends(get_risk_engine),
) -> dict[str, Any]:
    """获取待人工审核的交易队列。"""
    # 模拟数据 - 实际应从数据库查询
    reviews = []
    return {"data": reviews}


@router.post("/review/{event_id}")
async def review_transaction(
    event_id: str,
    reviewer: str = Query(..., description="审核人"),
    result: str = Query(..., description="审核结果: approved/rejected"),
    engine: RiskEngine = Depends(get_risk_engine),
) -> dict[str, Any]:
    """审核交易结果。"""
    return {"status": "reviewed", "event_id": event_id, "result": result}
