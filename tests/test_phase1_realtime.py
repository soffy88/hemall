"""Phase 1 WebSocket 实时推送测试。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
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
        """向没有连接的用户发送必须是 no-op，且**不凭空建出连接集**。

        原实现这条测试零断言：把 `send_to_user` 整个换成 `raise RuntimeError`
        或者反过来加一条 `_connections[user_id] = set()` 的副作用，它照样绿。
        """
        cm = ConnectionManager()
        await cm.send_to_user("nonexistent", {"event": "test"})

        assert "nonexistent" not in cm._connections, "给未知用户发消息不得创建连接集"
        assert cm._connections == {}

        # 对已存在的用户，发送后连接仍应保留（不会被误清)。
        ws = MockWebSocket()
        await cm.connect(ws, "user1")
        await cm.send_to_user("nonexistent", {"event": "test"})
        assert "user1" in cm._connections
        assert ws.messages == []

    @pytest.mark.asyncio
    async def test_send_to_user_drops_dead_sockets(self):
        """发送失败的连接要被剔除，空集合要被清理——这是 send_to_user 的真实职责。"""

        class _DeadSocket(MockWebSocket):
            async def send_text(self, text: str) -> None:
                raise RuntimeError("connection reset")

        cm = ConnectionManager()
        dead = _DeadSocket()
        await cm.connect(dead, "user1")
        await cm.send_to_user("user1", {"event": "x"})
        assert "user1" not in cm._connections, "发送失败的连接应被剔除"
        assert cm._connections == {}

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
        from fastapi.routing import APIWebSocketRoute

        from app.realtime.hub import router as realtime_router

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
        from fastapi.routing import APIWebSocketRoute

        from app.realtime.hub import router as realtime_router

        ws_routes = [r for r in realtime_router.routes if isinstance(r, APIWebSocketRoute)]
        assert len(ws_routes) > 0

    def test_realtime_manager_singleton(self):
        """验证 manager 是单例。"""
        from app.realtime.hub import manager as m2

        assert manager is m2

    def test_websocket_rejects_missing_and_invalid_token(self):
        """/ws 必须鉴权：缺 token / 垃圾 token 一律 4001 关闭。

        这条测试原本是 `TestClient(app=None)` 的 no-op——不发请求、不断言，
        却顶着"验证 WS 端点需要 token"的名字。实际后果是 /ws 里那处
        `jwt_decode(..., algorithms=[...])` 的形参笔误（应为 algorithm）长期
        无人发现：TypeError 被 except 吞掉，WS 端点实际上永远连不上。
        """
        from fastapi import WebSocketDisconnect

        with TestClient(app) as client:
            for bad in ("", "not-a-jwt", "a.b.c"):
                with pytest.raises(WebSocketDisconnect) as exc:
                    with client.websocket_connect(f"/ws?token={bad}"):
                        pass
                assert exc.value.code == 4001, f"token={bad!r} 应被 4001 拒绝"

    def test_websocket_accepts_staff_and_customer_tokens(self):
        """合法主体令牌（staff / customer）能连上，并可用 ping/pong 心跳。"""
        from app.deps import get_settings
        from app.security.tokens import (
            TOKEN_TYPE_CUSTOMER,
            TOKEN_TYPE_STAFF,
            sign_token,
        )

        settings = get_settings()
        cases = [
            (TOKEN_TYPE_STAFF, {"sub": "u1"}),
            (TOKEN_TYPE_CUSTOMER, {"customer_id": "c1"}),
        ]
        with TestClient(app) as client:
            for typ, body in cases:
                token = sign_token(
                    body,
                    typ=typ,
                    secret=settings.jwt_secret,
                    algorithm=settings.jwt_algorithm,
                    expires_in_minutes=10,
                )
                with client.websocket_connect(f"/ws?token={token}") as ws:
                    ws.send_text("ping")
                    assert json.loads(ws.receive_text())["event"] == "pong"

    @pytest.mark.parametrize("typ", ["cart", "receipt", "pickup"])
    def test_websocket_rejects_capability_tokens(self, typ):
        """能力令牌（购物车/收据/取货码）不得订阅实时推送。"""
        from fastapi import WebSocketDisconnect

        from app.deps import get_settings
        from app.security.tokens import sign_token

        settings = get_settings()
        token = sign_token(
            {"cart_id": "c1", "order_id": "o1", "node_name": "n"},
            typ=typ,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
            expires_in_minutes=10,
        )
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect(f"/ws?token={token}"):
                    pass
            assert exc.value.code == 4001

    def test_websocket_rejects_untyped_token(self):
        """没有 typ 的裸令牌一律拒绝（黑名单的老形状）。"""
        from fastapi import WebSocketDisconnect
        from obase.crypto.util import CryptoUtil

        from app.deps import get_settings

        settings = get_settings()
        token = CryptoUtil.jwt_sign(
            payload={"sub": "attacker", "customer_id": "victim"},
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect(f"/ws?token={token}"):
                    pass
            assert exc.value.code == 4001

    def test_websocket_subject_comes_from_the_right_claim(self):
        """staff 取 sub、customer 取 customer_id —— 别把两者搞混。"""
        from app.deps import get_settings
        from app.security.tokens import (
            TOKEN_TYPE_CUSTOMER,
            TOKEN_TYPE_STAFF,
            sign_token,
        )

        settings = get_settings()
        cases = [
            (TOKEN_TYPE_STAFF, {"sub": "staff-1"}, "staff-1"),
            (TOKEN_TYPE_CUSTOMER, {"customer_id": "cust-9"}, "cust-9"),
        ]
        with TestClient(app) as client:
            for typ, body, expected in cases:
                token = sign_token(
                    body,
                    typ=typ,
                    secret=settings.jwt_secret,
                    algorithm=settings.jwt_algorithm,
                    expires_in_minutes=10,
                )
                with client.websocket_connect(f"/ws?token={token}") as ws:
                    ws.send_text("ping")
                    ws.receive_text()
                    assert expected in manager._connections, f"typ={typ} 应当以 {expected} 注册连接"
                    manager.disconnect(list(manager._connections[expected])[0], expected)
