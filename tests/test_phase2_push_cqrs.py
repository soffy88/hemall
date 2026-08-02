"""Phase 2 移动端推送 + CQRS 事件溯源测试。"""

from __future__ import annotations

import pytest

from app.push.models import (
    DeviceToken,
    PushNotification,
    PushPlatform,
    PushPriority,
    PushResult,
)
from app.eventsourcing.models import (
    AggregateRoot,
    DomainEvent,
    EventType,
    Projection,
    Snapshot,
)


class TestPushNotification:
    """推送通知模型测试。"""

    def test_push_notification_creation(self):
        notification = PushNotification(
            title="Test",
            body="Body",
            target_device_ids=["token-123"],
            platform=PushPlatform.FCM,
            priority=PushPriority.HIGH,
        )
        assert notification.title == "Test"
        assert notification.platform == PushPlatform.FCM

    def test_device_token_creation(self):
        token = DeviceToken(
            user_id="user-1",
            device_token="fcm-token-xyz",
            platform=PushPlatform.FCM,
            model="iPhone 15",
        )
        assert token.user_id == "user-1"
        assert token.model == "iPhone 15"


class TestPushPlatform:
    """推送平台枚举测试。"""

    def test_fcm_platform(self):
        assert PushPlatform.FCM.value == "fcm"
        assert PushPlatform.APNS.value == "apns"

    def test_push_priority_enum(self):
        priorities = [e.value for e in PushPriority]
        assert "low" in priorities
        assert "high" in priorities


class TestDomainEvent:
    """领域事件模型测试。"""

    def test_domain_event_creation(self):
        event = DomainEvent(
            event_type=EventType.ORDER_CREATED,
            aggregate_id="order-1",
            aggregate_type="Order",
            data={"amount": 999},
        )
        assert event.event_type == EventType.ORDER_CREATED
        assert event.aggregate_id == "order-1"

    def test_event_serialization(self):
        event = DomainEvent(
            event_type=EventType.PAYMENT_SUCCESS,
            aggregate_id="pay-1",
            version=1,
            data={"status": "success"},
        )
        d = event.to_dict()
        assert d["event_type"] == EventType.PAYMENT_SUCCESS.value

    def test_event_deserialization(self):
        d = {
            "event_id": "evt-1",
            "event_type": "order.shipped",
            "aggregate_id": "order-1",
            "aggregate_type": "Order",
            "version": 1,
            "data": {"tracking": "SF123"},
            "occurred_at": "2025-01-01T00:00:00",
        }
        event = DomainEvent.from_dict(d)
        assert event.event_id == "evt-1"
        assert event.data["tracking"] == "SF123"


class TestAggregateRoot:
    """聚合根基类测试。"""

    def test_aggregate_base(self):
        """验证 AggregateRoot 可以实例化。"""
        from app.eventsourcing.models import AggregateRoot

        class MockAggregate(AggregateRoot):
            def __init__(self):
                super().__init__(aggregate_id="agg-1", aggregate_type="Mock")

            def _apply_event(self, event: DomainEvent) -> None:
                pass  # 模拟实现

        agg = MockAggregate()
        assert agg.aggregate_id == "agg-1"
        assert len(agg.uncommitted_events) == 0


class TestSnapshot:
    """快照模型测试。"""

    def test_snapshot_creation(self):
        snapshot = Snapshot(
            aggregate_id="order-1",
            aggregate_type="Order",
            version=5,
            state={"status": "completed"},
        )
        assert snapshot.version == 5
        d = snapshot.to_dict()
        assert d["state"]["status"] == "completed"


class TestCQRSService:
    """CQRS 服务层集成测试。"""

    def test_eventbus_health(self):
        from app.main import app

        # 检查 eventsourcing 路由是否已注册
        from app.eventsourcing.router import router as es_router
        assert len(es_router.routes) > 0

    def test_eventsourcing_routes_exist(self):
        from app.main import app

        paths = app.openapi()["paths"]
        es_paths = [p for p in paths if "/eventsourcing/" in p]
        assert len(es_paths) >= 2
