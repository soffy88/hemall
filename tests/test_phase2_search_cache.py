"""Phase 2 Elasticsearch 搜索 + Redis 缓存层测试。"""

from __future__ import annotations

import json

import pytest

from app.cache.models import CacheKey, CacheRegion
from app.search.models import ProductDocument, SearchQueryParams


class TestProductDocument:
    """商品文档模型测试。"""

    def test_product_document_creation(self):
        doc = ProductDocument(
            product_id="prod-1",
            name="Test Product",
            selling_price_cents=1999,
            total_stock=100,
        )
        assert doc.product_id == "prod-1"
        assert doc.selling_price_cents == 1999

    def test_product_document_default_values(self):
        doc = ProductDocument(product_id="p1", name="Test")
        assert doc.total_stock == 0
        assert doc.rating_avg == 0.0
        assert doc.is_active is True


class TestSearchQueryParams:
    """搜索查询参数测试。"""

    def test_default_params(self):
        params = SearchQueryParams()
        assert params.page == 1
        assert params.size == 20
        assert params.sort_by == "relevance"

    def test_custom_pagination(self):
        params = SearchQueryParams(page=2, size=50)
        assert params.page == 2
        assert params.size == 50


class TestCacheKey:
    """缓存 key 生成器测试。"""

    def test_product_detail_key(self):
        key = CacheKey.product_detail("sku-123")
        assert key == "hemall:product:detail:sku-123"

    def test_inventory_stock_key(self):
        key = CacheKey.inventory_stock("WH-SH", "PROD-001")
        assert key == "hemall:inventory:stock:WH-SH/PROD-001"

    def test_user_profile_key(self):
        key = CacheKey.user_profile("user-456")
        assert key == "hemall:user:profile:user-456"


class TestCacheRegionTTL:
    """缓存区域 TTL 策略测试。"""

    def test_cache_region_enum(self):
        assert len(CacheRegion) >= 4  # 至少有这几个区域

    def test_ttl_map_contains_regions(self):
        from app.cache.models import CACHE_TTL_MAP
        assert CacheRegion.PRODUCT_DETAIL in CACHE_TTL_MAP
        assert CACHE_TTL_MAP[CacheRegion.PRODUCT_DETAIL] > 0


class TestCacheRouterIntegration:
    """缓存路由集成测试。"""

    def test_cache_routes_exist(self):
        from app.main import app

        paths = app.openapi()["paths"]
        cache_paths = [p for p in paths if "/cache/" in p]
        assert len(cache_paths) >= 3, f"expected >=3 cache routes, got {len(cache_paths)}"

        expected = ["/cache/health", "/cache/prefix/{prefix}", "/cache/warmup"]
        for route in expected:
            assert route in paths or any(route.replace("{prefix}", "*") in p for p in paths)

    def test_search_routes_exist(self):
        from app.main import app

        # 检查 search 路由是否已注册到 router 列表中
        from app.search.router import router as search_router
        assert len(search_router.routes) > 0


class TestSearchClient:
    """ES 客户端测试 (无真实 ES 时的 mock)。"""

    @pytest.mark.asyncio
    async def test_es_client_init_fails_gracefully(self):
        """验证 ES 不可用时不会崩溃。"""
        from app.search.client import ElasticsearchClient
        from pydantic_settings import BaseSettings

        class MockSettings(BaseSettings):
            elasticsearch_url: str = "http://invalid-host:9999"
            es_index_name: str = "test-index"
            es_max_connections: int = 5
            es_request_timeout: int = 5
            es_autocomplete_index: str = "test-autocomplete"

        client = ElasticsearchClient(MockSettings())
        await client.initialize()
        # 不应抛异常
        assert not client._initialized


class TestCacheService:
    """Redis 缓存服务测试。"""

    @pytest.mark.asyncio
    async def test_cache_manager_init_without_redis(self):
        """验证 Redis 不可用时不会崩溃。"""
        from app.cache.service import CacheManager
        from app.config import Settings

        class MockSettings:
            redis_url = "redis://invalid-host:9999/0"

        mgr = CacheManager(MockSettings())
        await mgr.start()
        # 应降级到不可用状态而非崩溃
        assert mgr.is_available is False
