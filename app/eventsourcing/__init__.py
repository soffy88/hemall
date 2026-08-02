"""hemall CQRS 事件溯源模块 — Command/Query/Event 架构。

Phase 2 Priority 5: 为 hemall 提供事件驱动的数据一致性保障和分析能力。

架构:
    ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
    │ Commands │────►│ Aggregates│────►│ Events   │────►│ Projections│
    │          │◄────│(Domain)  │◄────│ Store    │◄────│(Read DB)  │
    └──────────┘     └──────────┘     └──────────┘     └──────────┘
        │                               │
        └───────────► EventBus ◄────────┘
                      │
                ┌─────▼─────┐
                │ Listeners │
                │ (async)   │
                └───────────┘

特性:
    - 聚合根 + 领域事件发布
    - 事件持久化 (append-only log)
    - 自动投影更新 (Event → Read Model)
    - 快照支持 (性能优化)
    - 事件版本控制 + 迁移
"""

from __future__ import annotations
