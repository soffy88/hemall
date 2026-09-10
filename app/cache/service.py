"""Redis 缓存管理器 — 连接、读写、TTL 清理、批量操作。

Phase 2: 为 hemall 提供高性能数据缓存能力，
覆盖商品详情、库存余量、分类树等热点数据。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

import redis.asyncio as redis

from ..config import Settings
from .models import CacheConfig, CacheKey, CacheRegion, CACHE_TTL_MAP

logger = logging.getLogger("hemall.cache")


class CacheManager:
    """Redis 缓存管理器 — 单例模式 + 异步连接池。

    功能:
        - 通用 key-value 读写 (JSON 序列化)
        - 按 region 自动 TTL
        - 缓存预热 / 失效
        - 分布式锁支持
        - 健康检查
    """

    _instance: CacheManager | None = None

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._redis_url = settings.redis_url or "redis://localhost:6379/0"
        self._pool: redis.Redis | None = None
        self._started = False
        # 本实例持有的锁 token：release 时仅删自己持有的锁，避免误删他人锁。
        self._lock_tokens: dict[str, str] = {}

    @classmethod
    async def create(cls, settings: Settings) -> CacheManager:
        """工厂方法: 创建并启动缓存管理器。"""
        if cls._instance is None:
            cls._instance = cls(settings)
        await cls._instance.start()
        return cls._instance

    @classmethod
    def get_instance(cls) -> CacheManager:
        """获取已初始化的单例实例。"""
        if cls._instance is None:
            raise RuntimeError("CacheManager not initialized. Call create() first.")
        return cls._instance

    # ── 生命周期 ────────────────────────────────────────────────

    async def start(self) -> None:
        """启动 Redis 连接池 + 后台任务。"""
        if self._started:
            return
        try:
            self._pool = redis.from_url(
                self._redis_url,
                decode_responses=True,
                max_connections=50,
            )
            await self._pool.ping()
            self._started = True
            logger.info("CacheManager started: %s", self._redis_url)
        except Exception as exc:
            logger.warning("CacheManager init failed (degraded): %s", exc)
            self._pool = None

    async def stop(self) -> None:
        """关停。"""
        if self._pool:
            await self._pool.close()
            self._pool = None
        self._started = False
        logger.info("CacheManager stopped")

    @property
    def is_available(self) -> bool:
        return self._pool is not None and self._started

    # ── 基础读写 ────────────────────────────────────────────────

    async def get(self, key: str) -> Any | None:
        """获取缓存值 (反序列化)。"""
        if not self.is_available:
            return None
        try:
            raw = await self._pool.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:
            logger.warning("Cache GET failed for %s: %s", key, exc)
            return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
        region: CacheRegion | None = None,
    ) -> bool:
        """设置缓存值 (序列化) + TTL 控制。"""
        if not self.is_available:
            return False

        try:
            data = json.dumps(value, default=str)
            if ttl is None and region:
                ttl = CACHE_TTL_MAP.get(region, 300)

            if ttl:
                await self._pool.setex(key, ttl, data)
            else:
                await self._pool.set(key, data)
            return True
        except Exception as exc:
            logger.warning("Cache SET failed for %s: %s", key, exc)
            return False

    async def delete(self, key: str) -> bool:
        """删除缓存 key。"""
        if not self.is_available:
            return False
        try:
            await self._pool.delete(key)
            return True
        except Exception as exc:
            logger.warning("Cache DELETE failed for %s: %s", key, exc)
            return False

    async def exists(self, key: str) -> bool:
        """检查 key 是否存在。"""
        if not self.is_available:
            return False
        try:
            return bool(await self._pool.exists(key))
        except Exception:
            return False

    # ── 计数器 / 排行榜 ───────────────────────────────────────

    async def increment(self, key: str, amount: int = 1) -> int:
        """原子递增计数器。"""
        if not self.is_available:
            return 0
        try:
            return await self._pool.incrby(key, amount)
        except Exception:
            return 0

    async def get_rank(self, key: str) -> list[tuple[str, int]]:
        """获取 Sorted Set 排行榜前 N 名。"""
        if not self.is_available:
            return []
        try:
            results = await self._pool.zrevrange(key, 0, 9, withscores=True)
            return [(item.decode() if isinstance(item, bytes) else item, score) for item in results]
        except Exception:
            return []

    # ── 缓存预热 ────────────────────────────────────────────────

    async def warmup_inventory_stock(
        self, warehouse_product_pairs: list[tuple[str, str]]
    ) -> dict[str, int]:
        """预热库存缓存。

        Args:
            warehouse_product_pairs: [(warehouse_code, product_code), ...]

        Returns:
            {"warmed": n, "failed": m}
        """
        warmed = 0
        failed = 0
        for wh, prod in warehouse_product_pairs[:100]:  # 限制批量大小
            key = CacheKey.inventory_stock(wh, prod)
            # 模拟从 DB 查询库存 (实际应调用 inventory service)
            mock_stock = {"available": 100, "reserved": 5, "in_warehouse": 50}
            result = await self.set(key, mock_stock, region=CacheRegion.INVENTORY_STOCK)
            if result:
                warmed += 1
            else:
                failed += 1
        logger.info("Inventory cache warmup: %d warmed, %d failed", warmed, failed)
        return {"warmed": warmed, "failed": failed}

    async def warmup_categories(self, category_data: Any) -> bool:
        """预热分类树缓存。"""
        key = CacheKey.category_tree()
        return await self.set(key, category_data, region=CacheRegion.CATEGORY_TREE, ttl=7200)

    # ── 批量操作 ────────────────────────────────────────────────

    async def mget(self, keys: list[str]) -> dict[str, Any | None]:
        """批量获取。"""
        if not self.is_available:
            return {k: None for k in keys}
        try:
            values = await self._pool.mget(keys)
            results = {}
            for k, v in zip(keys, values):
                results[k] = json.loads(v) if v else None
            return results
        except Exception as exc:
            logger.warning("Cache MGET failed: %s", exc)
            return {k: None for k in keys}

    async def mset(self, mapping: dict[str, Any], ttl: int | None = None) -> int:
        """批量设置。"""
        if not self.is_available:
            return 0
        try:
            pipe = self._pipeline()
            for k, v in mapping.items():
                data = json.dumps(v, default=str)
                if ttl:
                    pipe.setex(k, ttl, data)
                else:
                    pipe.set(k, data)
            await pipe.execute()
            return len(mapping)
        except Exception as exc:
            logger.warning("Cache MSET failed: %s", exc)
            return 0

    async def flush_prefix(self, prefix: str) -> int:
        """按前缀批量删除。"""
        if not self.is_available:
            return 0
        try:
            cursor = 0
            deleted = 0
            while True:
                cursor, keys = await self._pool.scan(cursor, match=f"{prefix}:*", count=100)
                if keys:
                    await self._pool.delete(*keys)
                    deleted += len(keys)
                if cursor == 0:
                    break
            logger.info("Flushed %d keys with prefix %s", deleted, prefix)
            return deleted
        except Exception as exc:
            logger.warning("Cache FLUSH PREFIX failed: %s", exc)
            return 0

    def _pipeline(self) -> redis.client.Pipeline:
        """获取 pipeline 连接。"""
        return self._pool.pipeline() if self._pool else None  # type: ignore

    # ── 分布式锁 ────────────────────────────────────────────────

    async def acquire_lock(
        self,
        lock_name: str,
        timeout: int = 10,
        retry_interval: float = 0.1,
    ) -> bool:
        """基于 Redis SET NX 的分布式锁。

        Returns:
            True 表示获取成功，False 表示超时。
        """
        if not self.is_available:
            return False

        lock_key = f"hemall:lock:{lock_name}"
        end_time = time.time() + timeout
        token = uuid.uuid4().hex

        while time.time() < end_time:
            try:
                acquired = await self._pool.set(lock_key, token, nx=True, ex=timeout)
                if acquired:
                    self._lock_tokens[lock_key] = token
                    return True
            except Exception:
                pass
            await asyncio.sleep(retry_interval)

        return False

    async def release_lock(self, lock_name: str) -> bool:
        """释放分布式锁（仅释放本实例持有的锁，Lua 原子比对 token）。"""
        if not self.is_available:
            return False
        lock_key = f"hemall:lock:{lock_name}"
        token = self._lock_tokens.pop(lock_key, None)
        if token is None:
            return False
        try:
            released = await self._pool.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                lock_key,
                token,
            )
            return bool(released)
        except Exception:
            return False

    # ── 健康检查 ────────────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """缓存服务健康状态。"""
        if not self.is_available:
            return {"status": "unavailable"}
        try:
            info = await self._pool.info("memory")
            return {
                "status": "healthy",
                "used_memory_mb": info.get("used_memory_human", "N/A"),
                "connected_clients": (await self._pool.info())["connected_clients"],
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}
