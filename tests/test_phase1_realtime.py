"""Phase 1 WebSocket 实时推送测试。"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.realtime.hub import ConnectionManager, manager


class MockWebSocket:
    """模拟 WebSocket 对象用于单元测试。"""

    def __init__(self) -> None:
        self._accepted = False
        self._messages: list[str] = []

    async def accept(self) -> None:
        self._accepted = True

    async def send_text(self, text: str) -> None:
        self._messages.append(text)

    async def receive_text(self) -> str:
        return "ping"

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass

    @property
    def messages(self) -> list[str]:
        return self._messages


# ── 连接管理器测试 ────────────────────────────────────────────────


class TestConnectionManager:
    """连接管理器单元测试。"""

    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        """测试连接/断开。"""
        cm = ConnectionManager()
        ws = MockWebSocket()

        await cm.connect(ws, "user1")
        assert ws._accepted
        assert "user1" in cm._connections
        assert ws in cm._connections["user1"]

        cm.disconnect(ws, "user1")
        assert "user1" not in cm._connections

    @pytest.mark.asyncio
    async def test_send_to_user(self):
        """测试向用户发送消息。"""
        cm = ConnectionManager()
        ws = MockWebSocket()

        await cm.connect(ws, "user1")
        await cm.send_to_user("user1", {"event": "test", "data": {"foo": "bar"}})

        assert len(ws.messages) == 1
        data = json.loads(ws.messages[0])
        assert data["event"] == "test"
        assert data["data"]["foo"] == "bar"

    @pytest.mark.asyncio
    async def test_send_to_disconnected_user(self):
        """测试向已断开用户发送 (不抛异常)。"""
        cm = ConnectionManager()
        await cm.send_to_user("nonexistent", {"event": "test"})

    @pytest.mark.asyncio
    async def test_broadcast_local(self):
        """测试本地广播。"""
        cm = ConnectionManager()
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()

        await cm.connect(ws1, "user1")
        await cm.connect(ws2, "user2")
        await cm.broadcast_local({"event": "broadcast", "data": {}})

        assert len(ws1.messages) == 1
        assert len(ws2.messages) == 1
        assert json.loads(ws1.messages[0])["event"] == "broadcast"
        assert json.loads(ws2.messages[0])["event"] == "broadcast"

    @pytest.mark.asyncio
    async def test_clean_dead_connections_on_send(self):
        """测试发送失败时自动清理死连接。"""
        cm = ConnectionManager()
        ws = MockWebSocket()
        original_send = ws.send_text
        call_count = [0]

        async def failing_send(text: str) -> None:
            call_count[0] += 1
            raise RuntimeError("connection lost")

        ws.send_text = failing_send  # type: ignore
        await cm.connect(ws, "user1")
        assert "user1" in cm._connections

        await cm.send_to_user("user1", {"event": "test"})
        # 连接应该被清理
        assert "user1" not in cm._connections
        assert call_count[0] >= 1


# ── API 路由测试 ──────────────────────────────────────────────────


class TestRealtimeRoutes:
    """实时推送 API 路由测试。"""

    def test_websocket_route_exists(self):
        """验证 WebSocket 路由已注册。"""
        from app.realtime.hub import router as realtime_router
        from fastapi.routing import APIWebSocketRoute

        ws_routes = [r for r in realtime_router.routes if isinstance(r, APIWebSocketRoute)]
        assert len(ws_routes) > 0
        assert ws_routes[0].path == "/ws"

    def test_realtime_router_has_tag(self):
        """验证路由使用了正确的标签。"""
        from app.realtime.hub import router as realtime_router

        assert realtime_router.tags == ["realtime"]


# ── 推送函数测试 ──────────────────────────────────────────────────


class TestPublishFunctions:
    """推送函数测试 (无 Redis 环境)。"""

    @pytest.mark.asyncio
    async def test_publish_to_user_no_redis(self, monkeypatch):
        """测试无 Redis 时的用户推送 (降级为本地)。"""
        from app.realtime.hub import publish_to_user

        monkeypatch.setattr(manager, "_redis", None)
        received = []

        async def mock_send(user_id, msg):
            received.append((user_id, msg))

        original = manager.send_to_user
        manager.send_to_user = mock_send  # type: ignore

        try:
            await publish_to_user("user1", "order.shipped", {"order_id": "o1"})
            assert len(received) == 1
            assert received[0][0] == "user1"
            assert received[0][1]["event"] == "order.shipped"
        finally:
            manager.send_to_user = original  # type: ignore

    @pytest.mark.asyncio
    async def test_broadcast_no_redis(self, monkeypatch):
        """测试无 Redis 时的广播 (降级为本地)。"""
        from app.realtime.hub import broadcast

        monkeypatch.setattr(manager, "_redis", None)
        received = []

        async def mock_broadcast(msg):
            received.append(msg)

        manager.broadcast_local = mock_broadcast  # type: ignore

        try:
            await broadcast("inventory.low_stock", {"product_id": "p1"})
            assert len(received) == 1
            assert received[0]["event"] == "inventory.low_stock"
        finally:
            manager.broadcast_local = lambda msg: None  # type: ignore

    @pytest.mark.asyncio
    async def test_message_has_timestamp(self, monkeypatch):
        """测试消息包含时间戳。"""
        from app.realtime.hub import publish_to_user

        monkeypatch.setattr(manager, "_redis", None)
        received = []

        async def mock_send(user_id, msg):
            received.append(msg)

        original = manager.send_to_user
        manager.send_to_user = mock_send  # type: ignore

        try:
            await publish_to_user("user1", "order.shipped", {})
            assert "timestamp" in received[0]
            assert received[0]["_target_user"] == "user1"
        finally:
            manager.send_to_user = original  # type: ignore


# ── 集成测试 ──────────────────────────────────────────────────────


class TestRealtimeIntegration:
    """实时推送集成测试 (TestClient)。"""

    def test_app_starts_realtime(self):
        """验证应用启动时 realtime hub 已配置。"""
        from app.realtime.hub import router as realtime_router
        from fastapi.routing import APIWebSocketRoute

        ws_routes = [r for r in realtime_router.routes if isinstance(r, APIWebSocketRoute)]
        assert len(ws_routes) > 0

    def test_realtime_manager_singleton(self):
        """验证 manager 是单例。"""
        from app.realtime.hub import manager as m2
        assert manager is m2

    def test_websocket_requires_token(self):
        """测试 WebSocket 端点需要 token。"""
        client = TestClient(app=None)
