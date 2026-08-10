"""edge_bridge.bridge_main — clearnode-iot-bridge 守护进程入口。

独立外挂域 (Sidecar / Anti-Corruption Layer)：左手通过 MQTT (EMQX) 挂载
物理世界的硬件碎片 (ESP32 重力货架 pick / 闸口称重)，右手通过 RESTful API
向 hemall 主干发送结构化商业指令。本模块**不导入任何主干代码**——协议转换
逻辑在 handlers.py (纯函数)，这里只做三件事：

    1. MQTT 订阅与回调分发 (paho-mqtt，QoS 1)
    2. HTTP 转发 + 熔断 + 死信队列 (硬件事件绝对不丢失)
    3. 主干宕机时的硬件降级 (闸口一律 block/red + 回执)

配置全部走环境变量 (MQTT_BROKER / HEMALL_API_URL / HARDWARE_SECRET /
DLQ_PATH / RETRY_INTERVAL / CIRCUIT_*)——与 hemall 主干零耦合。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Any

import httpx
import paho.mqtt.client as mqtt

from handlers import (
    build_gate_payload,
    build_gate_reply,
    build_pick_payload,
    parse_edge_topic,
)

# ── 配置 (环境变量，与 hemall 解耦) ──────────────────────────────────────
MQTT_BROKER = os.getenv("MQTT_BROKER", "emqx")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_KEEPALIVE = int(os.getenv("MQTT_KEEPALIVE", "60"))
# 订阅两个信号：重力货架 pick + 闸口称重 req。
MQTT_SUB_TOPICS = os.getenv("MQTT_SUB_TOPICS", "cn/v1/+/s/+/pick,cn/v1/+/g/+/req")
HEMALL_API_URL = os.getenv(
    "HEMALL_API_URL", "http://hemall-api:8000/ext/hardware/webhook"
)
HARDWARE_SECRET = os.getenv("HARDWARE_SECRET", "super-secret-key")
DLQ_PATH = os.getenv("DLQ_PATH", "/data/dlq.sqlite")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "3.0"))
RETRY_INTERVAL = float(os.getenv("RETRY_INTERVAL", "5.0"))
# 熔断：连续失败超过阈值 → 熔断开 (拒发主干，硬件降级)；冷却期后试探关闭。
CIRCUIT_MAX_FAILURES = int(os.getenv("CIRCUIT_MAX_FAILURES", "5"))
CIRCUIT_COOLDOWN = float(os.getenv("CIRCUIT_COOLDOWN", "30.0"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logger = logging.getLogger("clearnode-iot-bridge")


class DeadLetterQueue:
    """本地 SQLite 死信队列：主干不可达时先落盘，后台轮询重发。

    硬件事件是物理事实 (重量已经发生了)，绝不能丢——宁可晚到也不能消失。
    """

    def __init__(self, path: str = DLQ_PATH) -> None:
        self._path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS dlq ("
            " id TEXT PRIMARY KEY,"
            " api_path TEXT NOT NULL,"  # 'pick' | 'gate-reconcile'
            " payload TEXT NOT NULL,"  # 待转发的主干标准 payload (JSON)
            " attempts INT NOT NULL DEFAULT 0,"
            " created_at REAL NOT NULL,"
            " last_attempt_at REAL"
            ")"
        )
        self._conn.commit()

    def push(self, api_path: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO dlq (id, api_path, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), api_path, json.dumps(payload), time.time()),
        )
        self._conn.commit()

    def pop_all(self) -> list[tuple[str, str, dict[str, Any]]]:
        rows = self._conn.execute(
            "SELECT id, api_path, payload FROM dlq ORDER BY created_at"
        ).fetchall()
        return [(r[0], r[1], json.loads(r[2])) for r in rows]

    def drop(self, dlq_id: str) -> None:
        self._conn.execute("DELETE FROM dlq WHERE id = ?", (dlq_id,))
        self._conn.commit()

    def size(self) -> int:
        return self._conn.execute("SELECT count(*) FROM dlq").fetchone()[0]


class CircuitBreaker:
    """熔断器：连续失败计数，超阈值断开 (暂停转发、硬件降级)。"""

    def __init__(self, max_failures: int, cooldown: float) -> None:
        self._max = max_failures
        self._cooldown = cooldown
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        # 冷却期后自动试探关闭 (half-open)。
        return time.time() - self._opened_at < self._cooldown

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._max:
            self._opened_at = time.time()
            logger.warning(
                "circuit breaker OPEN after %d consecutive failures", self._max
            )


class Bridge:
    """网桥主体：MQTT 回调 → asyncio 事件循环 → HTTP 转发 / 熔断 / DLQ。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dlq = DeadLetterQueue()
        self._breaker = CircuitBreaker(CIRCUIT_MAX_FAILURES, CIRCUIT_COOLDOWN)
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {HARDWARE_SECRET}"},
            timeout=HTTP_TIMEOUT,
        )

    # ── MQTT 侧 ─────────────────────────────────────────────────────────

    def on_connect(self, client: mqtt.Client, userdata, flags, reason_code, props) -> None:
        if reason_code != 0:
            logger.error("MQTT connect failed: %s", reason_code)
            return
        for topic in MQTT_SUB_TOPICS.split(","):
            client.subscribe(topic.strip(), qos=1)
            logger.info("subscribed %s (QoS 1)", topic.strip())

    def on_message(self, client: mqtt.Client, userdata, msg) -> None:
        """MQTT 回调分发中心 (paho 线程) → asyncio 事件循环。"""
        if self._loop is None:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("dropping non-JSON payload on %s", msg.topic)
            return
        asyncio.run_coroutine_threadsafe(
            self._dispatch(payload, msg.topic, client), self._loop
        )

    async def _dispatch(self, payload: dict, topic: str, client: mqtt.Client) -> None:
        """按信号类型路由：pick → 单向转发；gate → 对账并下发回执。"""
        try:
            if topic.endswith("/pick"):
                await self._handle_pick(payload, topic)
            elif topic.endswith("/req"):
                await self._handle_gate(payload, topic, client)
            else:
                logger.warning("unroutable topic %s", topic)
        except Exception as exc:  # noqa: BLE001 - 单个信号异常不得杀死守护进程
            logger.exception("dispatch failed on %s: %s", topic, exc)

    async def _handle_pick(self, payload: dict, topic: str) -> None:
        api_payload = build_pick_payload(payload, topic)
        if not api_payload["message_id"]:
            api_payload["message_id"] = str(uuid.uuid4())
        ok = await self._forward("pick", api_payload)
        if not ok:
            # 主干不可达：进死信队列等后台重发，事件绝不丢失。
            self._dlq.push("pick", api_payload)
            logger.info("pick %s -> DLQ (hemall unreachable)", api_payload["message_id"])

    async def _handle_gate(self, payload: dict, topic: str, client: mqtt.Client) -> None:
        ctx = parse_edge_topic(topic)
        api_payload = build_gate_payload(payload, topic)
        if self._breaker.is_open:
            # 熔断：不碰主干，直接硬件降级 (红/系统错误)。
            decision = {"action": "block", "led": "red"}
            logger.warning("gate %s degraded while circuit open", api_payload["gate_id"])
        else:
            decision = await self._forward_gate(api_payload)
        reply_topic, reply_payload = build_gate_reply(ctx, decision, payload)
        client.publish(reply_topic, json.dumps(reply_payload), qos=1)
        logger.info("gate %s -> %s on %s", api_payload["gate_id"], reply_payload["act"], reply_topic)

    # ── HTTP 侧 (防腐层) ───────────────────────────────────────────────

    async def _forward(self, api_path: str, api_payload: dict) -> bool:
        """POST 到主干；成功记 success 并返回 True，失败计数熔断返回 False。"""
        if self._breaker.is_open:
            return False
        try:
            resp = await self._http.post(f"{HEMALL_API_URL}/{api_path}", json=api_payload)
            resp.raise_for_status()
            self._breaker.record_success()
            return True
        except Exception:  # noqa: BLE001 - 网络/主干异常统一按失败处理
            self._breaker.record_failure()
            return False

    async def _forward_gate(self, api_payload: dict) -> dict[str, Any]:
        """闸口对账：期望 {'action'|'act', 'led'}；主干宕机时硬件降级兜底。"""
        try:
            resp = await self._http.post(
                f"{HEMALL_API_URL}/gate-reconcile", json=api_payload
            )
            resp.raise_for_status()
            self._breaker.record_success()
            return resp.json()
        except Exception:  # noqa: BLE001
            self._breaker.record_failure()
            logger.warning("gate reconcile failed; hardware degraded to block")
            return {"action": "block", "led": "red"}

    # ── 后台任务 ───────────────────────────────────────────────────────

    async def _dlq_worker(self) -> None:
        """死信队列重发循环：每隔 RETRY_INTERVAL 重试一次，成功即出队。"""
        while True:
            await asyncio.sleep(RETRY_INTERVAL)
            if self._breaker.is_open:
                continue
            for dlq_id, api_path, payload in self._dlq.pop_all():
                try:
                    resp = await self._http.post(
                        f"{HEMALL_API_URL}/{api_path}", json=payload
                    )
                    resp.raise_for_status()
                    self._dlq.drop(dlq_id)
                    self._breaker.record_success()
                    logger.info("DLQ replay ok: %s", api_path)
                except Exception:  # noqa: BLE001
                    self._breaker.record_failure()
                    logger.warning("DLQ replay failed for %s", dlq_id)

    # ── 生命周期 ───────────────────────────────────────────────────────

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"clearnode-bridge-{uuid.uuid4().hex[:8]}"
        )
        client.on_connect = self.on_connect
        client.on_message = self.on_message
        client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        client.loop_start()  # paho 网络线程；回调经 run_coroutine_threadsafe 进本循环

        dlq_task = asyncio.create_task(self._dlq_worker())
        logger.info(
            "clearnode-iot-bridge up: broker=%s:%d hemall=%s topics=%s",
            MQTT_BROKER, MQTT_PORT, HEMALL_API_URL, MQTT_SUB_TOPICS,
        )
        try:
            await asyncio.Event().wait()  # 常驻
        finally:
            dlq_task.cancel()
            client.loop_stop()
            await self._http.aclose()


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(Bridge().run())
    except KeyboardInterrupt:
        logger.info("bridge shutting down")


if __name__ == "__main__":
    main()
