"""CQRS 核心服务 — EventStore, ProjectorRegistry, CommandBus。

Phase 2 Priority 5: 实现完整的事件溯源基础设施。

架构:
    ┌─────────────┐     ┌──────────────┐     ┌─────────────┐
    │CommandHandler│────►│EventStore   │────►│Projections  │
    │              │◄────│ (PostgreSQL)│◄────│ (Read Models│
    └─────────────┘     └──────────────┘     └─────────────┘
                              │
                        ┌─────▼─────┐
                        │ Snapshots │
                        └───────────┘
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Generic, TypeVar

from ..config import Settings
from .models import AggregateRoot, DomainEvent, EventType, Projection, Snapshot

logger = logging.getLogger("hemall.eventsourcing.service")


# ── 事件存储 ──────────────────────────────────────────────────────


class EventStore:
    """事件存储 — PostgreSQL 持久化 + 内存缓存。

    使用 append-only 表记录所有领域事件，支持:
        - 按聚合 ID 追加事件
        - 按版本范围查询
        - 快照读写
        - 一致性保证 (乐观锁)
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._snapshot_threshold = settings.event_snapshot_threshold or 10
        self._initialized = False

    async def initialize(self, pool: Any) -> None:
        """初始化事件存储表结构。"""
        self._pool = pool
        try:
            await self._create_tables()
            self._initialized = True
            logger.info("EventStore initialized")
        except Exception as exc:
            logger.warning("EventStore init failed: %s", exc)

    async def _create_tables(self) -> None:
        """创建事件表和快照表 DDL (PostgreSQL 方言; asyncpg 单次 execute 只接受单条语句)。"""
        statements = [
            """
            CREATE TABLE IF NOT EXISTS event_store (
                event_id        VARCHAR(36) PRIMARY KEY,
                event_type      VARCHAR(100) NOT NULL,
                aggregate_id    VARCHAR(36) NOT NULL,
                aggregate_type  VARCHAR(50) NOT NULL,
                version         INT NOT NULL,
                data            JSONB NOT NULL,
                occurred_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_event_store_aggregate
                ON event_store (aggregate_id, version)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_event_store_agg_version
                ON event_store (aggregate_id, version)
            """,
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                aggregate_id    VARCHAR(36) PRIMARY KEY,
                aggregate_type  VARCHAR(50) NOT NULL,
                version         INT NOT NULL,
                state           JSONB NOT NULL,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ]
        async with self._pool.acquire() as conn:
            for ddl in statements:
                await conn.execute(ddl)

    async def append_events(self, events: list[DomainEvent]) -> int:
        """追加事件到存储 (事务性，(aggregate_id, version) 唯一约束做乐观锁)。"""
        if not self._initialized:
            return 0

        inserted = 0
        try:
            async with self._pool.transaction() as conn:
                for event in events:
                    d = event.to_dict()
                    sql = """
                        INSERT INTO event_store 
                        (event_id, event_type, aggregate_id, aggregate_type, version, data, occurred_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """
                    await conn.execute(
                        sql,
                        d["event_id"], d["event_type"], d["aggregate_id"],
                        d["aggregate_type"], d["version"], str(d["data"]),
                        d["occurred_at"].isoformat(),
                    )
                    inserted += 1
        except Exception as exc:
            # 唯一约束冲突 = 并发写同一版本（乐观锁命中），向上传递由调用方重试。
            logger.warning("append_events failed (likely version conflict): %s", exc)
            raise

        # 检查是否需要创建快照
        if len(events) >= 1:
            last_event = events[-1]
            if last_event.version % self._snapshot_threshold == 0:
                await self._try_create_snapshot(last_event.aggregate_id, last_event)

        return inserted

    async def get_events(
        self,
        aggregate_id: str,
        from_version: int = 0,
        max_count: int = 1000,
    ) -> list[DomainEvent]:
        """获取聚合事件流 (用于状态恢复)。"""
        if not self._initialized:
            return []

        sql = """
            SELECT event_id, event_type, aggregate_id, aggregate_type,
                   version, data, occurred_at
            FROM event_store
            WHERE aggregate_id = $1 AND version > $2
            ORDER BY version ASC
            LIMIT $3
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, aggregate_id, from_version, max_count)

        events = []
        for row in rows:
            d = dict(row)
            d["data"] = d.get("data", "{}") if isinstance(d.get("data"), str) else d["data"]
            events.append(DomainEvent.from_dict(d))
        return events

    async def get_latest_snapshot(
        self, aggregate_id: str
    ) -> Snapshot | None:
        """获取最新的聚合快照。"""
        if not self._initialized:
            return None

        sql = "SELECT aggregate_id, aggregate_type, version, state, created_at FROM snapshots WHERE aggregate_id = $1 ORDER BY version DESC LIMIT 1"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(sql, aggregate_id)
            if not row:
                return None

        d = dict(row)
        d["state"] = d.get("state", "{}") if isinstance(d.get("state"), str) else d["state"]
        return Snapshot(
            aggregate_id=d["aggregate_id"],
            aggregate_type=d["aggregate_type"],
            version=d["version"],
            state=d["state"],
            created_at=datetime.fromisoformat(d["created_at"]) if isinstance(d.get("created_at"), str) else d.get("created_at"),
        )

    async def _try_create_snapshot(self, aggregate_id: str, event: DomainEvent) -> bool:
        """尝试创建快照。"""
        try:
            sql = """
                INSERT INTO snapshots (aggregate_id, aggregate_type, version, state)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (aggregate_id) DO UPDATE
                SET version = EXCLUDED.version, state = EXCLUDED.state
            """
            async with self._pool.acquire() as conn:
                await conn.execute(
                    sql,
                    aggregate_id, event.aggregate_type, event.version, str(event.data),
                )
            logger.debug("Snapshot created for %s at version %d", aggregate_id, event.version)
            return True
        except Exception as exc:
            logger.warning("Snapshot creation failed: %s", exc)
            return False


# ── 投影管理器 ────────────────────────────────────────────────────


T = TypeVar("T")


class ProjectorRegistry:
    """投影注册表 — 管理所有读模型投影。

    每个投影监听特定的事件类型，当事件发生时自动更新对应的读模型。
    """

    def __init__(self) -> None:
        self._projections: dict[EventType, list[Projection]] = {}

    def register(self, projection: Projection, event_types: list[EventType]) -> None:
        """注册投影到指定事件类型。"""
        for et in event_types:
            if et not in self._projections:
                self._projections[et] = []
            self._projections[et].append(projection)

    async def dispatch(self, event: DomainEvent) -> None:
        """分发事件到所有注册的投影。"""
        if event.event_type not in self._projections:
            return

        tasks = []
        for proj in self._projections[event.event_type]:
            tasks.append(asyncio.create_task(proj.on_event(event)))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failed = sum(1 for r in results if isinstance(r, Exception))
            if failed:
                logger.warning("Projection dispatch failed for %d projections", failed)


# ── 命令总线 ──────────────────────────────────────────────────────


class CommandBus:
    """命令总线 — 路由命令到对应的 Handler。

    用法:
        bus = CommandBus()
        bus.register(CreateOrderCommand, CreateOrderHandler)
        await bus.dispatch(CreateOrderCommand(...))
    """

    def __init__(self) -> None:
        self._handlers: dict[type, Any] = {}

    def register(self, command_type: type, handler: Any) -> None:
        """注册命令处理器。"""
        self._handlers[command_type] = handler

    async def dispatch(self, command: Any) -> Any:
        """分派命令到处理器。"""
        handler = self._handlers.get(type(command))
        if not handler:
            raise ValueError(f"No handler for {type(command).__name__}")
        return await handler.handle(command)


# ── 事件总线 ──────────────────────────────────────────────────────


class EventBus:
    """事件总线 — 协调事件存储与投影更新。

    在聚合根提交事件时调用:
        1. 写入事件存储
        2. 触发投影更新
    """

    def __init__(self, store: EventStore, registry: ProjectorRegistry):
        self._store = store
        self._registry = registry

    async def publish(self, events: list[DomainEvent]) -> None:
        """发布事件 (持久化 + 投影)。"""
        if not events:
            return

        # 1. 持久化事件
        await self._store.append_events(events)

        # 2. 触发投影更新 (异步)
        tasks = [self._registry.dispatch(event) for event in events]
        await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("Published %d events to EventBus", len(events))
