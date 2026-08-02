"""事件溯源数据模型 — Event, AggregateRoot, Projection, Snapshot。

定义:
    - DomainEvent: 领域事件基类 (带版本号)
    - AggregateRoot: 聚合根基类 (事件追加 + 状态恢复)
    - Projection: 读模型投影接口
    - Snapshot: 聚合快照 (性能优化)
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field


# ── 事件类型枚举 ────────────────────────────────────────────────


class EventType(Enum):
    """领域事件类型。"""

    # 订单事件
    ORDER_CREATED = "order.created"
    ORDER_CONFIRMED = "order.confirmed"
    ORDER_CANCELLED = "order.cancelled"
    ORDER_SHIPPED = "order.shipped"
    ORDER_DELIVERED = "order.delivered"
    ORDER_COMPLETED = "order.completed"
    ORDER_REFUNDED = "order.refunded"

    # 支付事件
    PAYMENT_CREATED = "payment.created"
    PAYMENT_SUCCESS = "payment.success"
    PAYMENT_FAILED = "payment.failed"
    PAYMENT_REFUNDED = "payment.refunded"

    # 库存事件
    STOCK_RESERVED = "stock.reserved"
    STOCK_DEDUCTED = "stock.deducted"
    STOCK_RELEASED = "stock.released"
    STOCK_LOW_ALERT = "stock.low_alert"

    # 商品事件
    PRODUCT_CREATED = "product.created"
    PRODUCT_UPDATED = "product.updated"
    PRODUCT_DELETED = "product.deleted"


# ── 领域事件 ──────────────────────────────────────────────────────


@dataclass
class DomainEvent:
    """领域事件基类。

    所有业务事件继承此类并添加特定字段。

    Attributes:
        event_id: 事件唯一 ID (UUID)
        event_type: 事件类型 (枚举)
        aggregate_id: 触发事件的聚合根 ID
        aggregate_type: 聚合根类型 (如 Order, Product)
        occurred_at: 发生时间
        version: 聚合版本 (用于乐观锁)
        data: 事件载荷 (任意 JSON-serializable 数据)
    """

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: EventType | None = None
    aggregate_id: str = ""
    aggregate_type: str = ""
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = 1
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典 (用于持久化)。"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value if self.event_type else "",
            "aggregate_id": self.aggregate_id,
            "aggregate_type": self.aggregate_type,
            "occurred_at": self.occurred_at.isoformat(),
            "version": self.version,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DomainEvent:
        """从字典反序列化。"""
        event_type_str = d.get("event_type", "")
        try:
            event_type = EventType(event_type_str)
        except ValueError:
            event_type = None
        return cls(
            event_id=d.get("event_id", ""),
            event_type=event_type,
            aggregate_id=d.get("aggregate_id", ""),
            aggregate_type=d.get("aggregate_type", ""),
            occurred_at=datetime.fromisoformat(d["occurred_at"]) if isinstance(d.get("occurred_at"), str) else d.get("occurred_at", datetime.now()),
            version=d.get("version", 1),
            data=d.get("data", {}),
        )


# ── 聚合根 ────────────────────────────────────────────────────────


T = TypeVar("T", bound="AggregateRoot")


class AggregateRoot(ABC):
    """聚合根基类 — 维护一致性边界 + 事件追溯。

    使用方式:
        class Order(AggregateRoot):
            def __init__(self):
                super().__init__(aggregate_type="Order")

            def create(self, ...):
                event = OrderCreatedEvent(...)
                self.apply(event)

        order = Order()
        order.create(order_id="o1", ...)
        print(order.uncommitted_events)  # 获取待提交事件
    """

    def __init__(self, aggregate_id: str | None = None, aggregate_type: str = ""):
        self._aggregate_id = aggregate_id or str(uuid.uuid4())
        self._aggregate_type = aggregate_type or self.__class__.__name__
        self._version: int = 0
        self._uncommitted_events: list[DomainEvent] = []

    @property
    def aggregate_id(self) -> str:
        return self._aggregate_id

    @property
    def version(self) -> int:
        return self._version

    @property
    def uncommitted_events(self) -> list[DomainEvent]:
        """未提交的事件列表。"""
        return self._uncommitted_events

    def apply(self, event: DomainEvent) -> None:
        """应用事件到当前状态 (内部方法)。"""
        event_version = event.version or self._version + 1
        event.aggregate_id = self._aggregate_id
        event.aggregate_type = self._aggregate_type
        event.version = event_version

        # 子类重写此方法来实际更新状态
        self._apply_event(event)

        self._version = event_version
        self._uncommitted_events.append(event)

    def load_events(self, events: list[DomainEvent]) -> None:
        """从事件流重建状态 (重放历史事件)。"""
        for event in sorted(events, key=lambda e: e.version):
            self._apply_event(event)
        self._version = events[-1].version if events else 0
        self._uncommitted_events.clear()

    @abstractmethod
    def _apply_event(self, event: DomainEvent) -> None:
        """子类实现: 根据事件类型更新内部状态。"""
        ...


# ── 投影器 ────────────────────────────────────────────────────────


ProjectionType = TypeVar("ProjectionType")


class Projection(ABC, Generic[ProjectionType]):
    """读模型投影抽象基类。

    将领域事件转换为面向查询的读模型格式。
    每个投影对应一个特定的读取场景 (订单列表、销售统计等)。

    用法:
        class OrderListViewProjection(Projection[list[dict]]):
            async def on_event(self, event: DomainEvent) -> None:
                if event.event_type == EventType.ORDER_CREATED:
                    await self._update_read_model(event)
    """

    @abstractmethod
    async def on_event(self, event: DomainEvent) -> None:
        """处理单个事件并更新读模型。"""
        ...

    @abstractmethod
    async def get_view(self, **kwargs: Any) -> ProjectionType:
        """获取投影视图数据。"""
        ...


# ── 快照 ──────────────────────────────────────────────────────────


@dataclass
class Snapshot:
    """聚合快照 — 避免全量事件重放。

    每隔 N 个版本创建一次快照，恢复时只需加载最近快照 + 增量事件。

    Attributes:
        aggregate_id: 聚合根 ID
        aggregate_type: 聚合类型
        version: 快照时的版本号
        state: 聚合状态 (JSON-serializable)
        created_at: 创建时间
    """

    aggregate_id: str
    aggregate_type: str
    version: int
    state: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "aggregate_id": self.aggregate_id,
            "aggregate_type": self.aggregate_type,
            "version": self.version,
            "state": self.state,
            "created_at": self.created_at.isoformat(),
        }
