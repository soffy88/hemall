"""hemall 缓存层 — Redis 高性能缓存管理。

Phase 2 Priority 4: 为商品详情、库存余量等热点数据提供
Redis 缓存加速，降低数据库查询压力。

架构:
    ┌─────────┐     ┌──────────┐     ┌────────────┐
    │ Client  │────►│ CacheMgr │────►│   Redis    │
    └─────────┘     └──────────┘     └────────────┘
                        │
                  ┌─────▼─────┐
                  │ TTL 清理  │
                  │ Background│
                  └───────────┘
"""

from __future__ import annotations
