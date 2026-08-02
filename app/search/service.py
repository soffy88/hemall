"""商品搜索服务层 — 查询构建 + 结果映射 + 聚合分析。

Phase 2: 将自然语言搜索意图转化为 ES DSL 查询，
提供多字段加权评分、拼写纠错、智能排序和分页功能。
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import Settings
from .client import ElasticsearchClient
from .models import (
    AggregationBucket,
    AggregationResult,
    AutoCompleteRequest,
    AutoCompleteResult,
    AutoCompleteSuggestion,
    HitProduct,
    PaginationInfo,
    ProductDocument,
    SearchQueryParams,
    SearchResponse,
)

logger = logging.getLogger("hemall.search.service")


class SearchService:
    """商品搜索引擎服务 — 查询构建 + 结果处理 + 索引维护。"""

    def __init__(self, client: ElasticsearchClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        # 搜索权重配置
        self._field_boosts = {
            "name": 10,       # 名称最高权重
            "subtitle": 8,     # 副标题次高
            "brand_name": 5,   # 品牌中等
            "description": 3,  # 描述最低
        }

    @classmethod
    async def create(cls, client: ElasticsearchClient, settings: Settings) -> SearchService:
        return cls(client, settings)

    # ── 搜索查询 ────────────────────────────────────────────────

    async def search(self, params: SearchQueryParams) -> SearchResponse:
        """执行商品搜索。

        支持:
            - 关键词全文检索 (多字段加权 BM25 评分)
            - 分类 / 品牌 / 价格区间过滤
            - 评分与库存筛选
            - 多种排序策略
            - 多维聚合分析
            - 高亮显示
        """
        if not self._client._es:
            # ES 不可用时的降级响应
            return SearchResponse(
                products=[],
                pagination=PaginationInfo(page=params.page, size=params.size, total=0, pages=0),
            )

        must_clauses: list[dict[str, Any]] = []
        filter_clauses: list[dict[str, Any]] = []

        # ── 关键词匹配 (multi_match with field boosts) ───────────
        if params.keyword:
            fields = [f"{f}^{boost}" for f, boost in self._field_boosts.items()]
            must_clauses.append({
                "multi_match": {
                    "query": params.keyword,
                    "fields": fields,
                    "type": "best_fields",
                    "fuzziness": "AUTO",  # 自动模糊匹配
                    "minimum_should_match": "75%",
                }
            })
        else:
            # 无关键词时返回所有商品
            must_clauses.append({"match_all": {}})

        # ── 过滤条件 ─────────────────────────────────────────────
        if params.category_id:
            filter_clauses.append({"term": {"category_id": params.category_id}})

        if params.brand_id:
            filter_clauses.append({"term": {"brand_id": params.brand_id}})

        if params.price_min_cents is not None or params.price_max_cents is not None:
            price_range: dict[str, Any] = {}
            if params.price_min_cents is not None:
                price_range["gte"] = params.price_min_cents
            if params.price_max_cents is not None:
                price_range["lte"] = params.price_max_cents
            filter_clauses.append({"range": {"selling_price_cents": price_range}})

        if params.min_rating is not None:
            filter_clauses.append({"range": {"rating_avg": {"gte": params.min_rating}}})

        if params.in_stock_only:
            filter_clauses.append({"range": {"total_stock": {"gt": 0}}})

        # ── 活跃商品过滤 ─────────────────────────────────────────
        filter_clauses.append({"term": {"is_active": True}})

        # ── 组合查询 ─────────────────────────────────────────────
        query_body: dict[str, Any] = {
            "query": {
                "bool": {
                    "must": must_clauses,
                    "filter": filter_clauses,
                }
            },
            "from": (params.page - 1) * params.size,
            "size": params.size,
            "timeout": "5s",
        }

        # ── 排序 ─────────────────────────────────────────────────
        query_body["sort"] = self._build_sort(params.sort_by, params.sort_order)

        # ── 高亮 ─────────────────────────────────────────────────
        if params.keyword and params.highlight_fields:
            query_body["highlight"] = {
                "fields": {field: {} for field in params.highlight_fields},
                "pre_tags": ["<em class='search-highlight'>"],
                "post_tags": ["</em>"],
            }

        # ── 聚合分析 ─────────────────────────────────────────────
        if params.facets:
            query_body["aggs"] = self._build_aggregations(params.facets)

        logger.info("Executing search: keyword=%s facets=%s", params.keyword, params.facets)
        result = await self._client.search_products(query_body)

        # ── 映射结果 ─────────────────────────────────────────────
        products = [HitProduct(**self._map_hit(hit)) for hit in result.get("hits", [])]

        aggregation = None
        aggs = result.get("aggregations")
        if aggs:
            aggregation = self._map_aggregations(aggs, result["total"], result["took"])

        total = result["total"]
        pages = max(1, (total + params.size - 1) // params.size)

        return SearchResponse(
            products=products,
            aggregation=aggregation,
            pagination=PaginationInfo(
                page=params.page,
                size=params.size,
                total=total,
                pages=pages,
            ),
        )

    async def autocomplete(self, request: AutoCompleteRequest) -> AutoCompleteResult:
        """搜索建议 / 自动补全。"""
        suggestions = await self._client.autocomplete(
            prefix=request.prefix,
            size=request.limit,
        )
        return AutoCompleteResult(
            suggestions=[AutoCompleteSuggestion(**s) for s in suggestions]
        )

    # ── 数据同步 ────────────────────────────────────────────────

    async def sync_product(self, doc: ProductDocument) -> bool:
        """同步单个商品到 ES。"""
        return await self._client.index_product(doc)

    async def bulk_sync_products(self, docs: list[ProductDocument]) -> dict[str, int]:
        """批量同步商品到 ES。"""
        return await self._client.bulk_index_products(docs)

    async def remove_product(self, product_id: str) -> bool:
        """从 ES 删除商品。"""
        return await self._client.delete_product(product_id)

    # ── 内部方法 ────────────────────────────────────────────────

    def _build_sort(self, sort_by: str, sort_order: str) -> list[dict[str, Any]]:
        """构建排序子句。"""
        order = "asc" if sort_order == "asc" else "desc"

        sort_map = {
            "relevance": {"_score": order},
            "price_newest": {"selling_price_cents": order},
            "sold_desc": {"sold_count": "desc"},
            "rating": {"rating_avg": "desc"},
        }

        sort_field = sort_map.get(sort_by, {"_score": order})
        return [sort_field]

    def _build_aggregations(self, facets: list[str]) -> dict[str, Any]:
        """构建聚合查询子句。"""
        aggs: dict[str, Any] = {}

        if "category" in facets:
            aggs["category_dist"] = {
                "terms": {"field": "category_id", "size": 50}
            }

        if "brand" in facets:
            aggs["brand_dist"] = {
                "terms": {"field": "brand_id", "size": 50}
            }

        if "price_range" in facets:
            aggs["price_ranges"] = {
                "range": {
                    "field": "selling_price_cents",
                    "ranges": [
                        {"key": "free", "to": 0},
                        {"key": "0-50", "from": 0, "to": 5000},
                        {"key": "50-100", "from": 5000, "to": 10000},
                        {"key": "100-500", "from": 10000, "to": 50000},
                        {"key": "500+", "from": 50000},
                    ],
                }
            }

        return aggs

    def _map_hit(self, hit: dict[str, Any]) -> dict[str, Any]:
        """将 ES hit 映射为 HitProduct 数据。"""
        source = hit.get("_source", {})
        highlight = hit.get("highlight", {})
        return {
            "product_id": source.get("product_id", ""),
            "name": source.get("name", ""),
            "selling_price_cents": source.get("selling_price_cents"),
            "original_price_cents": source.get("original_price_cents"),
            "promotion_price_cents": source.get("promotion_price_cents"),
            "main_image_url": source.get("main_image_url"),
            "rating_avg": source.get("rating_avg", 0.0),
            "sold_count": source.get("sold_count", 0),
            "total_stock": source.get("total_stock", 0),
            "brand_name": source.get("brand_name"),
            "category_path": source.get("category_path", []),
            "highlight": highlight,
        }

    def _map_aggregations(
        self, aggs: dict[str, Any], total: int, took_ms: float
    ) -> AggregationResult:
        """将 ES aggregations 映射为结构化结果。"""
        category_dist = self._extract_buckets(aggs.get("category_dist", {}))
        brand_dist = self._extract_buckets(aggs.get("brand_dist", {}))
        price_ranges = self._extract_buckets(aggs.get("price_ranges", {}))

        avg_price = 0.0
        if "avg_price" in aggs:
            avg_price = aggs["avg_price"].get("value", 0) / 100  # 分→元

        return AggregationResult(
            total_hits=total,
            max_score=float(total > 0),
            category_dist=category_dist,
            brand_dist=brand_dist,
            price_ranges=price_ranges,
            average_price=avg_price,
            search_time_ms=took_ms,
        )

    def _extract_buckets(self, agg_data: dict[str, Any]) -> list[AggregationBucket]:
        """递归提取 buckets 结构。"""
        buckets = agg_data.get("buckets", [])
        results = []
        for bucket in buckets:
            key = bucket.get("key_as_string") or str(bucket.get("key", ""))
            doc_count = bucket.get("doc_count", 0)
            sub_buckets = self._extract_buckets(bucket.get("sub_aggs", {}))
            results.append(AggregationBucket(key=key, doc_count=doc_count, sub_buckets=sub_buckets))
        return results
