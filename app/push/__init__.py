"""hemall 移动端推送模块 — FCM (Firebase) + APNs (Apple)。

Phase 2 Priority 2: 为 iOS/Android 设备提供离线消息推送能力。

架构:
    ┌──────────┐     ┌──────────────┐     ┌──────────────┐
    │ hemall   │────►│ Push Manager │────►│ Firebase /   │
    │ Service  │◄────│              │◄────│ Apple Server │
    └──────────┘     └──────────────┘     └──────────────┘

功能:
    - FCM HTTP v1 API (Android/iOS)
    - APNs HTTPS Provider (iOS)
    - 设备 token 管理
    - 模板化通知消息
    - 灰度推送 / 批量推送
    - 推送送达率统计
"""

from __future__ import annotations
