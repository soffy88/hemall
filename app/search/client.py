"""Elasticsearch 异步客户端 — 连接管理、索引操作、搜索执行。

Phase 2: 为 hemall 提供高性能商品全文检索能力。
- 支持异步 bulk 批量写入/更新
- 支持多字段加权搜索 + 模糊匹配
- 支持聚合分析 (分类/品牌/价格分布)
- 自动重试与降级策略
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from elasticsearch import AsyncElasticsearch, exceptions as es_exceptions

from ..config import Settings
from .models import ProductDocument

logger = logging.getLogger("hemall.search.client")


class ElasticsearchClient:
    """ES 客户端封装 — 单例模式 + 连接池管理。"""

    _instance: ElasticsearchClient | None = None
    _es: AsyncElasticsearch | None = None

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._index_name = settings.es_index_name or "hemall_products"
        self._autocomplete_index = settings.es_autocomplete_index or "hemall_autocomplete"
        self._initialized = False
        # 同义词字典 (内存缓存)
        self._synonyms: dict[str, list[str]] = {}

    @classmethod
    async def create(cls, settings: Settings) -> ElasticsearchClient:
        """工厂方法: 创建并初始化 ES 客户端。"""
        if cls._instance is None:
            cls._instance = cls(settings)
        await cls._instance.initialize()
        return cls._instance

    @classmethod
    def get_instance(cls) -> ElasticsearchClient:
        """获取已初始化的单例实例。"""
        if cls._instance is None:
            raise RuntimeError("ElasticsearchClient not initialized. Call create() first.")
        return cls._instance

    @property
    def index_name(self) -> str:
        return self._index_name

    @property
    def autocomplete_index(self) -> str:
        return self._autocomplete_index

    # ── 初始化 / 关停 ───────────────────────────────────────

    async def initialize(self) -> None:
        """建立 ES 连接并确保索引存在。"""
        try:
            url = self._load_es_url_from_settings()
            self._es = AsyncElasticsearch(
                [url],
                maxsize=self._settings.es_max_connections or 10,
                request_timeout=self._settings.es_request_timeout or 30,
                retry_on_timeout=True,
            )
            # 健康检查
            if await self._es.ping():
                logger.info("Elasticsearch connected: %s", url)
                await self._ensure_indexes()
                self._initialized = True
            else:
                logger.warning("Elasticsearch ping failed — search will be degraded")
        except Exception as exc:
            logger.warning("Elasticsearch init failed (search degraded): %s", exc)

    async def close(self) -> None:
        """关闭连接池。"""
        if self._es:
            await self._es.close()
            self._es = None
            self._initialized = False
            logger.info("Elasticsearch client closed")

    def _load_es_url_from_settings(self) -> str:
        """从配置读取 ES URL。"""
        url = (
            self._settings.elasticsearch_url
            or self._settings.hemall_elasticsearch_url
            or "http://localhost:9200"
        )
        if not url.startswith("http"):
            url = f"http://{url}"
        return url

    # ── 索引管理 ──────────────────────────────────────────────

    async def _ensure_indexes(self) -> None:
        """确保商品索引和分析器存在。"""
        if not self._es:
            return

        try:
            # 商品主索引
            product_index_def = self._get_product_index_mapping()
            exists = await self._es.indices.exists(index=self._index_name)
            if not exists:
                await self._es.indices.create(
                    index=self._index_name,
                    body=product_index_def,
                )
                logger.info("Created product index: %s", self._index_name)
            else:
                logger.info("Product index already exists: %s", self._index_name)

            # 自动补全索引
            exists = await self._es.indices.exists(index=self._autocomplete_index)
            if not exists:
                await self._es.indices.create(
                    index=self._autocomplete_index,
                    body=self._get_autocomplete_mapping(),
                )
                logger.info("Created autocomplete index: %s", self._autocomplete_index)
        except es_exceptions.NotFoundError:
            pass  # ES 未运行时的优雅降级
        except Exception as exc:
            logger.error("Failed to ensure indexes: %s", exc)

    def _get_product_index_mapping(self) -> dict[str, Any]:
        """商品索引 mapping + analyzer 定义。"""
        return {
            "mappings": {
                "properties": {
                    # 核心搜索字段 (standard analyzer + Chinese support)
                    "name": {
                        "type": "text",
                        "analyzer": "ik_max_word",
                        "search_analyzer": "ik_smart",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                    "description": {
                        "type": "text",
                        "analyzer": "ik_max_word",
                    },
                    "subtitle": {
                        "type": "text",
                        "analyzer": "ik_max_word",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                    # 分类树 (用于多级筛选)
                    "category_path": {
                        "type": "keyword",
                    },
                    "category_id": {"type": "keyword"},
                    # 品牌
                    "brand_name": {
                        "type": "text",
                        "analyzer": "ik_smart",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                    "brand_id": {"type": "keyword"},
                    # 价格 (用于 range filter & aggregation)
                    "original_price_cents": {"type": "integer"},
                    "selling_price_cents": {"type": "integer"},
                    "promotion_price_cents": {"type": "integer"},
                    # 库存
                    "total_stock": {"type": "integer"},
                    # 评分与销量
                    "rating_avg": {"type": "float"},
                    "review_count": {"type": "integer"},
                    "sold_count": {"type": "integer"},
                    # 标签
                    "tags": {"type": "keyword"},
                    # SKU
                    "skus": {
                        "type": "nested",
                        "properties": {
                            "sku_id": {"type": "keyword"},
                            "specs": {"type": "keyword"},
                            "price_cents": {"type": "integer"},
                            "stock": {"type": "integer"},
                        },
                    },
                    # 媒体
                    "main_image_url": {"type": "keyword"},
                    "image_urls": {"type": "keyword"},
                    # 元数据
                    "is_active": {"type": "boolean"},
                    "indexed_at": {"type": "date"},
                }
            },
            "settings": {
                "analysis": {
                    "analyzer": {
                        "ik_max_word": {"type": "custom", "tokenizer": "ik_max_word"},
                        "ik_smart": {"type": "custom", "tokenizer": "ik_smart"},
                    }
                }
            },
        }

    def _get_autocomplete_mapping(self) -> dict[str, Any]:
        """自动补全索引 mapping (edge_ngram)。"""
        return {
            "mappings": {
                "properties": {
                    "text": {
                        "type": "completion",
                        "analyzer": "ik_max_word",
                    },
                    "context": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "keyword"},
                            "category_id": {"type": "keyword"},
                        },
                    },
                }
            }
        }

    # ── 数据索引 ──────────────────────────────────────────────

    async def index_product(self, doc: ProductDocument) -> bool:
        """索引单个商品文档 (upsert)。"""
        if not self._es:
            return False

        try:
            await self._es.index(
                index=self._index_name,
                id=doc.product_id,
                document=doc.model_dump(mode="json"),
            )
            return True
        except Exception as exc:
            logger.error("Failed to index product %s: %s", doc.product_id, exc)
            return False

    async def bulk_index_products(self, docs: list[ProductDocument]) -> dict[str, Any]:
        """批量索引商品 (bulk API)。"""
        if not self._es:
            return {"success": 0, "failed": len(docs)}

        actions = []
        for doc in docs:
            actions.append({"index": {"_index": self._index_name, "_id": doc.product_id}})
            actions.append(doc.model_dump(mode="json"))

        try:
            response = await self._es.bulk(operations=actions, refresh="wait_for")
            results = response.get("items", [])
            success = sum(1 for item in results if "error" not in item.get("index", {}))
            failed = len(results) - success
            logger.info("Bulk indexed %d products (%d failed)", success, failed)
            return {"success": success, "failed": failed}
        except Exception as exc:
            logger.error("Bulk index failed: %s", exc)
            return {"success": 0, "failed": len(docs)}

    async def delete_product(self, product_id: str) -> bool:
        """删除商品索引。"""
        if not self._es:
            return False
        try:
            await self._es.delete(index=self._index_name, id=product_id)
            return True
        except es_exceptions.NotFoundError:
            return False
        except Exception as exc:
            logger.error("Delete product %s failed: %s", product_id, exc)
            return False

    # ── 搜索执行 ──────────────────────────────────────────────

    async def search_products(self, query: dict[str, Any]) -> dict[str, Any]:
        """执行自定义 ES 搜索查询。

        返回原始 hit 结果 + aggregations。
        """
        if not self._es:
            return {"hits": [], "aggregations": {}, "took": 0}

        start_time = time.time()
        try:
            response = await self._es.search(
                index=self._index_name,
                body=query,
            )
            took_ms = (time.time() - start_time) * 1000
            return {
                "hits": response["hits"]["hits"],
                "total": response["hits"]["total"]["value"],
                "max_score": response["hits"]["hits"][0]["_score"] if response["hits"]["hits"] else 0,
                "aggregations": response.get("aggregations", {}),
                "took": took_ms,
            }
        except Exception as exc:
            logger.error("Search query failed: %s", exc)
            return {"hits": [], "aggregations": {}, "took": 0}

    async def autocomplete(
        self, prefix: str, size: int = 10, context: dict | None = None
    ) -> list[dict[str, Any]]:
        """搜索建议 / 自动补全 (completion suggester)。"""
        if not self._es:
            return []

        try:
            suggestion_body = {
                "prefix": prefix,
                "size": size,
                "completion": {
                    "field": "text",
                },
            }
            if context:
                suggestion_body["completion"]["contexts"] = context

            response = await self._es.search(
                index=self._autocomplete_index,
                body={
                    "size": 0,
                    "suggest": {
                        "text_suggestions": suggestion_body,
                    },
                },
            )
            suggestions = response.get("suggest", {}).get("text_suggestions", [])
            results = []
            for option in suggestions[0].get("options", []):
                results.append({
                    "text": option.get("_source", {}).get("text", ""),
                    "product_count": option.get("_source", {}).get("product_count"),
                    "type": option.get("_source", {}).get("type", "keyword"),
                })
            return results
        except Exception as exc:
            logger.warning("Autocomplete failed: %s", exc)
            return []
