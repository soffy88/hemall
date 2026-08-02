"""Phase 1 限流 Redis 后端测试。"""

from __future__ import annotations

import os

from app.security.rate_limit import _resolve_storage_uri, is_redis_backend, limiter


class TestRateLimitStorage:
    """限流存储后端解析测试。"""

    def test_memory_fallback(self, monkeypatch):
        """无 Redis 配置时降级为内存态。"""
        monkeypatch.delenv("RATE_LIMIT_STORAGE_URI", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("HEMALL_REDIS_URL", raising=False)
        uri = _resolve_storage_uri()
        assert uri == "memory://"

    def test_explicit_storage_uri(self, monkeypatch):
        """显式配置优先级最高。"""
        monkeypatch.setenv("RATE_LIMIT_STORAGE_URI", "redis://custom:6379/1")
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("HEMALL_REDIS_URL", raising=False)
        uri = _resolve_storage_uri()
        assert uri == "redis://custom:6379/1"

    def test_redis_url_fallback(self, monkeypatch):
        """无显式配置时复用 REDIS_URL。"""
        monkeypatch.delenv("RATE_LIMIT_STORAGE_URI", raising=False)
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.delenv("HEMALL_REDIS_URL", raising=False)
        uri = _resolve_storage_uri()
        assert uri == "redis://localhost:6379/0"

    def test_redis_url_prefix_added(self, monkeypatch):
        """REDIS_URL 无 redis:// 前缀时自动补全。"""
        monkeypatch.delenv("RATE_LIMIT_STORAGE_URI", raising=False)
        monkeypatch.setenv("REDIS_URL", "localhost:6379/0")
        monkeypatch.delenv("HEMALL_REDIS_URL", raising=False)
        uri = _resolve_storage_uri()
        assert uri == "redis://localhost:6379/0"

    def test_hemall_redis_url(self, monkeypatch):
        """HEMALL_REDIS_URL 作为第三优先级。"""
        monkeypatch.delenv("RATE_LIMIT_STORAGE_URI", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.setenv("HEMALL_REDIS_URL", "redis://prod-redis:6379/0")
        uri = _resolve_storage_uri()
        assert uri == "redis://prod-redis:6379/0"

    def test_is_redis_backend(self, monkeypatch):
        """检查 is_redis_backend 正确识别后端类型。"""
        # 默认测试环境无 Redis
        monkeypatch.delenv("RATE_LIMIT_STORAGE_URI", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("HEMALL_REDIS_URL", raising=False)
        _ = _resolve_storage_uri()  # 触发解析
        # limiter 已在模块加载时初始化
        assert isinstance(is_redis_backend(), bool)

    def test_limiter_default_limits(self):
        """验证默认限流配置。"""
        assert limiter is not None
        assert len(limiter._default_limits) > 0

    def test_limiter_storage_uri_set(self):
        """验证存储 URI 已设置。"""
        assert limiter._storage_uri is not None
        assert limiter._storage_uri.startswith("memory://") or limiter._storage_uri.startswith("redis://")
