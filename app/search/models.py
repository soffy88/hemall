"""商品搜索数据模型 — ES Document, Query DSL 映射, 聚合分析。

定义:
    - ProductDocument: 索引文档字段结构 + DDL (MySQL 源)
    - SearchQueryParams: 用户搜索请求参数
    - SearchResult / AggregationResult: 响应模型
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from ..config import Settings


# ── 商品文档模型 ────────────────────────────────────────────────


class ProductDocument(BaseModel):
    """Elasticsearch 商品文档结构。

    同步自 MySQL `product` + `product_category` + `warehouse_product_stock`。
    """

    # 核心字段
    product_id: str = Field(description="商品 ID")
    name: str = Field(description="商品名称 (需分词)")
    description: str | None = Field(None, description="商品描述 (全文检索)")
    subtitle: str | None = Field(None, description="副标题/卖点")

    # 分类与品牌
    category_id: str | None = Field(None, description="分类 ID")
    category_path: list[str] = Field(default_factory=list, description="分类树路径 (用于多级筛选)")
    brand_name: str | None = Field(None, description="品牌名称")
    brand_id: str | None = Field(None, description="品牌 ID")

    # 价格 (cents: 分为单位, 避免浮点精度问题)
    original_price_cents: int | None = Field(None, description="原价 (分)")
    selling_price_cents: int | None = Field(None, description="售价 (分)")
    promotion_price_cents: int | None = Field(None, description="促销价 (分)")

    # SKU 与库存
    skus: list[dict[str, Any]] = Field(default_factory=list, description="SKU 列表")
    total_stock: int = Field(default=0, description="总库存 (各仓库汇总)")

    # 评分与销量
    rating_avg: float = Field(default=0.0, description="平均评分 0-5")
    review_count: int = Field(default=0, description="评价数")
    sold_count: int = Field(default=0, description="已售数量")

    # 标签与属性
    tags: list[str] = Field(default_factory=list, description="营销标签 (new/hot/promo/sale)")
    attributes: dict[str, Any] = Field(default_factory=dict, description="SPU 级扩展属性")

    # 媒体
    main_image_url: str | None = Field(None, description="主图 URL")
    image_urls: list[str] = Field(default_factory=list, description="详情图列表")

    # 门店与卖家
    seller_id: str | None = Field(None, description="卖家/店铺 ID")
    seller_name: str | None = Field(None, description="店铺名称")
    warehouse_code: str | None = Field(None, description="发货仓库代码")

    # 元数据
    is_active: bool = Field(default=True, description="是否上架")
    indexed_at: str | None = Field(None, description="最近一次索引时间 (ISO8601)")


class SynonymEntry(BaseModel):
    """ES 同义词词典条目。"""

    synonyms: list[str] = Field(description="同义词组，逗号分隔")
    expanded: bool = Field(default=False, description="是否展开为 OR 关系")


# ── 搜索查询参数 ──────────────────────────────────────────────


class SearchQueryParams(BaseModel):
    """商品搜索请求参数。"""

    keyword: str | None = Field(None, description="搜索关键词")
    category_id: str | None = Field(None, description="按分类筛选")
    brand_id: str | None = Field(None, description="按品牌筛选")
    price_min_cents: int | None = Field(None, ge=0, description="最低价格 (分)")
    price_max_cents: int | None = Field(None, ge=0, description="最高价格 (分)")
    min_rating: float | None = Field(None, ge=0, le=5, description="最低评分")
    in_stock_only: bool = Field(default=False, description="仅显示有货商品")
    sort_by: str = Field(default="relevance", description="排序字段 (relevance/price_newest/sold_desc/rating)")
    sort_order: str = Field(default="desc", description="排序方向 (asc/desc)")
    page: int = Field(default=1, ge=1, description="页码")
    size: int = Field(default=20, ge=1, le=100, description="每页条数")
    highlight_fields: list[str] = Field(default_factory=lambda: ["name", "subtitle"], description="高亮字段")
    facets: list[str] = Field(
        default_factory=lambda: ["category", "brand", "price_range"],
        description="需要返回的聚合维度",
    )


class AutoCompleteRequest(BaseModel):
    """搜索建议/自动补全请求。"""

    prefix: str = Field(min_length=1, max_length=50, description="输入前缀")
    limit: int = Field(default=10, ge=1, le=50, description="返回条数")


# ── 搜索结果 ──────────────────────────────────────────────────


class HitProduct(BaseModel):
    """ES 命中商品摘要。"""

    product_id: str
    name: str
    selling_price_cents: int | None
    original_price_cents: int | None
    promotion_price_cents: int | None
    main_image_url: str | None
    rating_avg: float
    sold_count: int
    total_stock: int
    brand_name: str | None
    category_path: list[str]
    highlight: dict[str, list[str]] = Field(default_factory=dict)


class AggregationBucket(BaseModel):
    """聚合桶。"""

    key: str
    doc_count: int
    sub_buckets: list["AggregationBucket"] = Field(default_factory=list)


class AggregationResult(BaseModel):
    """多维聚合分析结果。"""

    total_hits: int = Field(description="符合条件的总商品数")
    max_score: float = Field(description="最高匹配度分数")
    category_dist: list[AggregationBucket] = Field(default_factory=list, description="分类分布")
    brand_dist: list[AggregationBucket] = Field(default_factory=list, description="品牌分布")
    price_ranges: list[AggregationBucket] = Field(
        default_factory=list, description="价格区间分布 [0-50, 50-100, 100-500, 500+]"
    )
    average_price: float = Field(default=0.0, description="平均售价 (元)")
    search_time_ms: float = Field(default=0.0, description="搜索耗时 (ms)")


class SearchResponse(BaseModel):
    """完整搜索响应。"""

    products: list[HitProduct] = Field(default_factory=list)
    aggregation: AggregationResult | None = Field(None, description="聚合分析结果")
    pagination: PaginationInfo = Field(default_factory=lambda: PaginationInfo(page=1, size=20, total=0, pages=0))


class PaginationInfo(BaseModel):
    """分页信息。"""

    page: int
    size: int
    total: int
    pages: int


class AutoCompleteResult(BaseModel):
    """自动补全结果。"""

    suggestions: list[AutoCompleteSuggestion] = Field(default_factory=list)


class AutoCompleteSuggestion(BaseModel):
    """单个搜索建议。"""

    text: str
    product_count: int | None = None
    type: str = Field(default="keyword", description="类型: keyword/category/brand")


# ── 索引管理 DDL ──────────────────────────────────────────────


def build_es_index_ddl(settings: Settings) -> str:
    """生成 Elasticsearch 索引初始化 SQL (从 MySQL 同步用)。

    实际 ES 索引通过 _bulk API 或 Logstash 创建，此函数提供
    MySQL 端的数据对齐参考 schema。
    """
    return f"""
    -- hemall ES 商品索引源表结构说明
    -- 此文件仅供开发参考, 实际 ES 索引通过 elasticsearch-dsl / kibanadb 管理

    CREATE TABLE IF NOT EXISTS es_product_sync (
        product_id      VARCHAR(36) PRIMARY KEY COMMENT '商品ID',
        sync_status     ENUM('pending','indexed','failed') DEFAULT 'pending' COMMENT '同步状态',
        es_version      INT UNSIGNED DEFAULT 0 COMMENT 'ES版本号 (乐观锁)',
        sync_created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        sync_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_status (sync_status),
        INDEX idx_updated (sync_updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    """


def get_elasticsearch_url(settings: Settings) -> str:
    """获取 Elasticsearch 连接地址。

    优先级:
        1. ELASTICSEARCH_URL
        2. HEMALL_ELASTICSEARCH_URL
        3. http://localhost:9200 (默认)
    """
    return (
        settings.elasticsearch_url
        or settings.hemall_elasticsearch_url
        or "http://localhost:9200"
    )
