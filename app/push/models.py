"""推送数据模型 — 通知结构、设备 Token、推送平台枚举。

定义:
    - PushNotification: 统一通知消息体
    - DeviceToken: 设备注册信息
    - PushPlatform: 推送平台 (FCM/APNs)
    - PushResponse: 推送结果
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class PushPlatform(Enum):
    """推送目标平台。"""

    FCM = "fcm"              # Firebase Cloud Messaging (Android/iOS)
    APNS = "apns"            # Apple Push Notification Service (iOS)


class PushPriority(Enum):
    """推送优先级。"""

    LOW = "low"              # 低优先级 (可能延迟送达)
    NORMAL = "normal"        # 正常优先级
    HIGH = "high"            # 高优先级 (立即推送)


@dataclass
class PushNotification:
    """统一的推送通知消息。

    封装 FCM / APNs 通用字段，底层自动转换为对应平台的格式。

    Attributes:
        title: 通知标题
        body: 通知内容
        target_device_ids: 目标设备 token 列表
        data: 自定义附加数据 (JSON-serializable)
        platform: 目标平台 (auto-detect from device tokens if None)
        priority: 推送优先级
        sound: iOS 提示音 ("default" / 自定义)
        badge: iOS 角标数 (None = 不改变)
        ttl_seconds: 消息存活时间 (秒)，仅 FCM
        collapse_key: 消息折叠键 (FCM 分组)
    """

    title: str
    body: str
    target_device_ids: list[str]
    data: dict[str, str] = field(default_factory=dict)
    platform: PushPlatform | None = None
    priority: PushPriority = PushPriority.NORMAL
    sound: str = "default"
    badge: int | None = None
    ttl_seconds: int = 86400  # FCM 默认 1 天
    collapse_key: str | None = None


@dataclass
class DeviceToken:
    """设备 Token 注册信息。

    Attributes:
        user_id: 用户 ID
        device_token: 推送 token (FCM token / APNs token)
        platform: 设备平台
        app_version: App 版本号
        model: 设备型号
        registered_at: 注册时间
    """

    user_id: str
    device_token: str
    platform: PushPlatform
    app_version: str | None = None
    model: str | None = None
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PushResult(BaseModel):
    """单条推送结果。"""

    device_token: str
    success: bool
    error_code: int | None = None
    error_message: str | None = None


class PushBatchResponse(BaseModel):
    """批量推送响应。"""

    total: int
    success: int
    failed: int
    results: list[PushResult] = field(default_factory=list)


class TemplateMessage(BaseModel):
    """预定义通知模板。"""

    template_id: str  # 如: order.shipped, payment.success
    title_template: str  # 如: "订单 {order_id} 已发货"
    body_template: str   # 如: "您的包裹正在路上，物流单号: {tracking_number}"
    placeholders: list[str] = field(default_factory=list)  # 待替换变量列表
    default_data: dict[str, str] = field(default_factory=dict)
