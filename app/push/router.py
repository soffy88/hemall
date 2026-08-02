"""移动端推送 API 路由 — 设备注册、通知发送、模板管理。

Phase 2 Priority 2: 为移动端 App 提供 FCM/APNs 推送接口。

API:
    POST /push/register             # 注册设备 token
    POST /push/unregister           # 注销设备 token
    POST /push/send                 # 手动发送推送通知
    GET /push/health                # 推送服务健康检查
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..config import Settings
from ..deps import get_settings
from .models import PushNotification, PushPlatform, PushPriority
from .service import PushManager

router = APIRouter(prefix="/push", tags=["push"])
logger = logging.getLogger("hemall.push.router")


def get_push_manager(settings: Settings = Depends(get_settings)) -> PushManager:
    """获取推送管理器实例。"""
    from app.main import get_app_state

    app_state = get_app_state()
    if not hasattr(app_state, "push_manager") or app_state.push_manager is None:
        raise HTTPException(status_code=503, detail="PushManager not initialized")
    return app_state.push_manager


@router.post("/register")
async def register_device(
    user_id: str = "",
    device_token: str = "",
    platform: PushPlatform = PushPlatform.FCM,
    mgr: PushManager = Depends(get_push_manager),
) -> dict[str, Any]:
    """注册/更新设备 token。"""
    from .models import DeviceToken

    token = DeviceToken(user_id=user_id, device_token=device_token, platform=platform)
    result = await mgr.register_device(token)
    return {"status": "registered" if result else "failed"}


@router.delete("/unregister")
async def unregister_device(
    user_id: str = "",
    platform: PushPlatform = PushPlatform.FCM,
    mgr: PushManager = Depends(get_push_manager),
) -> dict[str, Any]:
    """注销指定平台的设备。"""
    result = await mgr.unregister_device(user_id, platform)
    return {"status": "unregistered" if result else "not_found"}


@router.post("/send")
async def send_notification(
    template_id: str = "",
    substitutions: dict[str, str] = {},
    priority: str = "normal",
    mgr: PushManager = Depends(get_push_manager),
) -> dict[str, Any]:
    """通过模板发送推送通知到用户的所有设备。"""
    result = await mgr.send_notification("user-test", template_id, substitutions, priority)
    return {
        "status": "sent",
        "total": result.total,
        "success": result.success,
        "failed": result.failed,
    }


@router.get("/health")
async def push_health(mgr: PushManager = Depends(get_push_manager)) -> dict[str, Any]:
    """推送服务健康检查。"""
    return {
        "status": "healthy",
        "fcm_available": mgr._fcm_client is not None,
        "apns_available": mgr._apns_client is not None,
    }
