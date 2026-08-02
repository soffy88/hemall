"""WebSocket 实时推送 — 连接管理 + Redis Pub/Sub 分布式广播。

架构:
    ┌──────────┐    WebSocket     ┌──────────────┐
    │  Client  │ ◄─────────────► │  Connection  │
    └──────────┘                  │   Manager    │
                                  └──────┬───────┘
                                         │ subscribe
                                  ┌──────▼───────┐
                                  │ Redis Pub/Sub│
                                  │  (broadcast) │
                                  └──────┬───────┘
                                         │ publish
                    ┌────────────────────┼────────────────────┐
                    │                    │                    │
              ┌─────▼────┐        ┌──────▼─────┐       ┌──────▼─────┐
              │  Order   │        │ Inventory  │       │  Payment  │
              │ Service  │        │  Service   │       │  Service  │
              └──────────┘        └────────────┘       └────────────┘

用法 (客户端):
    const ws = new WebSocket('ws://localhost:8000/ws?token=<jwt>');
    ws.onmessage = (e) => console.log(JSON.parse(e.data));

用法 (服务端推送):
    from app.realtime.hub import publish_to_user, broadcast

    await publish_to_user(user_id='u1', event='order.shipped', data={'order_id': 'o1'})
    await broadcast(event='inventory.low_stock', data={'product_id': 'p1'})
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as redis
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..deps import get_settings

logger = logging.getLogger("hemall.realtime")

router = APIRouter(tags=["realtime"])


# ── 连接管理器 ───────────────────────────────────────────────────────


class ConnectionManager:
    """WebSocket 连接管理 + Redis Pub/Sub 分布式广播。

    单实例: 内存态 set[WebSocket] 即可。
    多实例: 每个实例订阅 Redis channel, 收到消息后广播给本地连接。
    """

    def __init__(self) -> None:
        #: user_id → set[WebSocket] — 本实例的活跃连接
        self._connections: dict[str, set[WebSocket]] = {}
        #: Redis pub/sub 客户端 (懒初始化)
        self._redis: redis.Redis | None = None
        self._pubsub_task: asyncio.Task | None = None
        self._started = False

    async def start(self, redis_url: str) -> None:
        """启动 Redis Pub/Sub 订阅 (在 lifespan 中调用)。"""
        if self._started:
            return
        self._started = True

        try:
            self._redis = redis.from_url(redis_url)
            await self._redis.ping()
            self._pubsub_task = asyncio.create_task(self._listen_redis())
            logger.info("realtime hub started (Redis pub/sub)")
        except Exception as exc:
            logger.warning(
                "realtime Redis pub/sub init failed (degraded to local-only): %s", exc
            )
            self._redis = None

    async def stop(self) -> None:
        """关停。"""
        if self._pubsub_task:
            self._pubsub_task.cancel()
            try:
                await self._pubsub_task
            except asyncio.CancelledError:
                pass
        if self._redis:
            await self._redis.close()
        self._connections.clear()
        self._started = False
        logger.info("realtime hub stopped")

    async def connect(self, websocket: WebSocket, user_id: str) -> None:
        """接受 WebSocket 连接并注册到连接池。"""
        await websocket.accept()
        if user_id not in self._connections:
            self._connections[user_id] = set()
        self._connections[user_id].add(websocket)
        logger.info("WebSocket connected: user=%s (total=%d)", user_id, len(self._connections[user_id]))

    def disconnect(self, websocket: WebSocket, user_id: str) -> None:
        """从连接池移除。"""
        if user_id in self._connections:
            self._connections[user_id].discard(websocket)
            if not self._connections[user_id]:
                del self._connections[user_id]
        logger.info("WebSocket disconnected: user=%s", user_id)

    async def send_to_user(self, user_id: str, message: dict[str, Any]) -> None:
        """向指定用户的所有连接发送消息 (本实例)。

        跨实例广播请用 publish_to_user。
        """
        if user_id in self._connections:
            text = json.dumps(message, default=str)
            dead: list[WebSocket] = []
            for ws in self._connections[user_id]:
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._connections[user_id].discard(ws)
            # 清理空连接集
            if not self._connections[user_id]:
                del self._connections[user_id]

    async def broadcast_local(self, message: dict[str, Any]) -> None:
        """向本实例所有连接广播。"""
        text = json.dumps(message, default=str)
        for user_id in list(self._connections.keys()):
            dead: list[WebSocket] = []
            for ws in self._connections.get(user_id, set()):
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._connections[user_id].discard(ws)

    # ── Redis Pub/Sub (跨实例广播) ──────────────────────────────

    async def _listen_redis(self) -> None:
        """监听 Redis pub/sub channel, 收到消息后广播给本地连接。"""
        if not self._redis:
            return

        pubsub = self._redis.pubsub()
        await pubsub.subscribe("hemall:broadcast", "hemall:user")

        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    data = json.loads(message["data"])
                    target = data.get("_target_user")
                    if target:
                        await self.send_to_user(target, data)
                    else:
                        await self.broadcast_local(data)
                except Exception as exc:
                    logger.warning("pubsub message parse failed: %s", exc)
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe()
            await pubsub.close()

    async def _publish_redis(self, channel: str, message: dict[str, Any]) -> None:
        """通过 Redis 发布消息 (跨实例广播)。"""
        if not self._redis:
            # 无 Redis 时只做本地广播
            if channel == "hemall:user":
                target = message.get("_target_user")
                if target:
                    await self.send_to_user(target, message)
            else:
                await self.broadcast_local(message)
            return

        await self._redis.publish(channel, json.dumps(message, default=str))


# ── 全局单例 ─────────────────────────────────────────────────────────


manager = ConnectionManager()


# ── 推送 API (供其他服务调用) ────────────────────────────────────────


async def publish_to_user(
    user_id: str, event: str, data: dict[str, Any] | None = None
) -> None:
    """向指定用户推送事件 (跨实例, 通过 Redis pub/sub)。"""
    message = {
        "event": event,
        "data": data or {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "_target_user": user_id,
    }
    await manager._publish_redis("hemall:user", message)


async def broadcast(event: str, data: dict[str, Any] | None = None) -> None:
    """向所有在线用户广播事件 (跨实例, 通过 Redis pub/sub)。"""
    message = {
        "event": event,
        "data": data or {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await manager._publish_redis("hemall:broadcast", message)


# ── WebSocket 端点 ───────────────────────────────────────────────────


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(..., description="JWT token for authentication"),
) -> None:
    """WebSocket 实时推送端点。

    客户端连接时通过 query param 传入 JWT:
        ws://localhost:8000/ws?token=<jwt>

    认证失败立即关闭连接 (code=4001)。
    """
    # 验证 JWT
    try:
        from obase.crypto.util import CryptoUtil

        settings = get_settings()
        payload = CryptoUtil.jwt_decode(
            token=token,
            secret=settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        user_id = payload.get("sub", "anonymous")
    except Exception:
        await websocket.close(code=4001, reason="invalid token")
        return

    await manager.connect(websocket, user_id)

    try:
        while True:
            # 保持连接, 等待客户端消息 (心跳/ping)
            data = await websocket.receive_text()
            # 客户端可发送 ping, 服务端回复 pong
            if data == "ping":
                await websocket.send_text(json.dumps({"event": "pong"}))
    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)
    except Exception as exc:
        logger.warning("WebSocket error for user %s: %s", user_id, exc)
        manager.disconnect(websocket, user_id)
