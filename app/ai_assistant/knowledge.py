"""AI 知识库 — 基于 RAG 的 FAQ 管理和上下文增强。

Phase 4+ Priority 2: 构建可维护的知识库，提升 AI 助手的回答质量。

核心功能:
    - FAQ 管理 (CRUD)
    - 知识点向量化存储
    - 知识检索与匹配
    - 知识库统计
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("hemall.ai.knowledge")


# ── 知识条目 ──────────────────────────────────────────────────────


class KnowledgeCategory(Enum):
    ORDER = "order"          # 订单相关
    SHIPPING = "shipping"    # 物流相关
    PAYMENT = "payment"      # 支付相关
    PRODUCT = "product"      # 商品相关
    RETURN = "return"        # 退换货相关
    ACCOUNT = "account"      # 账号相关
    PROMOTION = "promotion"  # 活动优惠
    GENERAL = "general"      # 通用知识


@dataclass
class KnowledgeItem:
    """知识条目。"""

    item_id: str
    category: KnowledgeCategory
    title: str
    question: str  # 用户常见问题
    answer: str    # 标准答案
    keywords: list[str] = field(default_factory=list)
    embedding: list[float] | None = None
    is_active: bool = True
    view_count: int = 0
    helpful_count: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "category": self.category.value,
            "title": self.title,
            "question": self.question,
            "answer": self.answer,
            "keywords": self.keywords,
            "is_active": self.is_active,
            "view_count": self.view_count,
            "helpful_count": self.helpful_count,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class FAQItem(BaseModel):
    """FAQ 输入模型。"""

    category: str
    title: str
    question: str
    answer: str
    keywords: list[str] = field(default_factory=list)


# ── 知识库服务 ────────────────────────────────────────────────────


class KnowledgeBase:
    """知识库服务 — FAQ 管理和检索。"""

    def __init__(self, pool: Any = None):
        self._pool = pool
        self._items: dict[str, KnowledgeItem] = {}
        self._initialized = False

    async def initialize(self, pool: Any = None) -> None:
        """初始化知识库。"""
        if pool:
            self._pool = pool
        await self._ensure_table()
        self._initialized = True
        logger.info("KnowledgeBase initialized with %d items", len(self._items))

    async def _ensure_table(self) -> None:
        """确保知识库表存在。"""
        ddl = """
        CREATE TABLE IF NOT EXISTS ai_knowledge_base (
            item_id VARCHAR(36) PRIMARY KEY,
            category VARCHAR(20) NOT NULL,
            title TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            keywords TEXT[] DEFAULT '{}',
            is_active BOOLEAN DEFAULT TRUE,
            view_count INT DEFAULT 0,
            helpful_count INT DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        """
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(ddl)
        except Exception as exc:
            logger.warning("Failed to create knowledge base table: %s", exc)

    # ── 条目管理 ──────────────────────────────────────────────────

    async def add_item(self, item: FAQItem) -> KnowledgeItem:
        """添加知识条目。"""
        try:
            category = KnowledgeCategory(item.category)
        except ValueError:
            category = KnowledgeCategory.GENERAL

        knowledge_item = KnowledgeItem(
            item_id=str(uuid.uuid4()),
            category=category,
            title=item.title,
            question=item.question,
            answer=item.answer,
            keywords=item.keywords,
        )

        self._items[knowledge_item.item_id] = knowledge_item

        # 持久化到数据库
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO ai_knowledge_base (item_id, category, title, question, answer, keywords)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        """,
                        knowledge_item.item_id,
                        category.value,
                        item.title,
                        item.question,
                        item.answer,
                        item.keywords,
                    )
            except Exception as exc:
                logger.warning("Failed to persist knowledge item: %s", exc)

        logger.info("Added knowledge item: %s (%s)", knowledge_item.item_id, category.value)
        return knowledge_item

    async def get_item(self, item_id: str) -> KnowledgeItem | None:
        """获取知识条目。"""
        item = self._items.get(item_id)
        if item:
            item.view_count += 1
        return item

    async def update_item(self, item_id: str, answer: str | None = None, title: str | None = None) -> KnowledgeItem | None:
        """更新知识条目。"""
        item = self._items.get(item_id)
        if not item:
            return None

        if answer:
            item.answer = answer
        if title:
            item.title = title

        item.updated_at = datetime.now(timezone.utc)

        # 同步到数据库
        if self._pool:
            try:
                new_answer = item.answer
                new_title = item.title
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE ai_knowledge_base SET answer=$1, title=$2, updated_at=NOW() WHERE item_id=$3",
                        new_answer, new_title, item_id,
                    )
            except Exception as exc:
                logger.warning("Failed to update knowledge item: %s", exc)

        return item

    async def delete_item(self, item_id: str) -> bool:
        """删除知识条目。"""
        if item_id not in self._items:
            return False

        del self._items[item_id]

        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute("DELETE FROM ai_knowledge_base WHERE item_id=$1", item_id)
            except Exception as exc:
                logger.warning("Failed to delete knowledge item: %s", exc)

        return True

    # ── 检索 ──────────────────────────────────────────────────────

    async def search(self, query: str, category: str | None = None, limit: int = 5) -> list[KnowledgeItem]:
        """检索知识库。

        使用关键词重叠 + 类别过滤的简单匹配。
        对于精确的语义检索，可结合向量搜索 (pgvector + Embedding)。
        """
        query_lower = query.lower()
        results: list[tuple[KnowledgeItem, float]] = []

        for item in self._items.values():
            if not item.is_active:
                continue
            if category and item.category.value != category:
                continue

            # 计算匹配分数
            score = self._match_score(item, query_lower)
            if score > 0:
                results.append((item, score))

        # 按分数排序
        results.sort(key=lambda x: x[1], reverse=True)
        items = [item for item, _ in results[:limit]]

        # 更新查看次数
        for item in items:
            item.view_count += 1

        return items

    def _match_score(self, item: KnowledgeItem, query_lower: str) -> float:
        """计算知识条目与查询的匹配分数。"""
        score = 0.0

        # 标题匹配
        if item.title.lower() in query_lower:
            score += 1.0
        elif any(word in item.title.lower().split() for word in query_lower.split()):
            score += 0.6

        # 问题匹配
        if item.question.lower() in query_lower:
            score += 1.0
        elif query_lower in item.question.lower() or item.question.lower() in query_lower:
            score += 0.8

        # 关键词匹配
        hit_count = sum(1 for kw in item.keywords if kw.lower() in query_lower)
        if hit_count > 0:
            score += 0.4 * min(hit_count, 3)

        # 答案匹配 (弱相关)
        if query_lower in item.answer.lower():
            score += 0.3

        return score

    # ── 帮助反馈 ──────────────────────────────────────────────────

    async def mark_helpful(self, item_id: str) -> bool:
        """标记知识条目有帮助。"""
        item = self._items.get(item_id)
        if not item:
            return False

        item.helpful_count += 1
        return True

    async def get_stats(self) -> dict[str, Any]:
        """知识库统计。"""
        category_count: dict[str, int] = {}
        for item in self._items.values():
            category_count[item.category.value] = category_count.get(item.category.value, 0) + 1

        return {
            "total_items": len(self._items),
            "active_items": sum(1 for item in self._items.values() if item.is_active),
            "total_views": sum(item.view_count for item in self._items.values()),
            "total_helpful": sum(item.helpful_count for item in self._items.values()),
            "by_category": category_count,
        }