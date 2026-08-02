"""AI 助手数据模型 — 对话、消息、意图、情感分析结果。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── 意图类型 ──────────────────────────────────────────────────────


class UserIntent(Enum):
    """用户意图分类。"""

    PRODUCT_INQUIRY = "product_inquiry"  # 商品咨询
    PURCHASE_INTENT = "purchase_intent"  # 购买意向
    COMPLAINT = "complaint"  # 投诉
    REFUND_REQUEST = "refund_request"  # 退款请求
    SHIPPING_INQUIRY = "shipping_inquiry"  # 物流查询
    GENERAL_QUESTION = "general_question"  # 一般问题
    RECOMMENDATION = "recommendation"  # 推荐请求
    COMPARISON = "comparison"  # 商品对比
    UNKNOWN = "unknown"  # 未知意图


class SentimentType(Enum):
    """情感分析结果。"""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    MIXED = "mixed"


# ── 对话模型 ──────────────────────────────────────────────────────


@dataclass
class Message:
    """对话消息。"""

    role: str  # user / assistant / system
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }


@dataclass
class Conversation:
    """对话会话。"""

    session_id: str
    user_id: str | None
    messages: list[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_message(self, role: str, content: str, metadata: dict[str, Any] | None = None) -> Message:
        """添加消息到会话。"""
        msg = Message(role=role, content=content, metadata=metadata or {})
        self.messages.append(msg)
        self.updated_at = datetime.now(timezone.utc)
        return msg

    def get_history(self, max_turns: int = 10) -> list[dict[str, str]]:
        """获取对话历史 (用于 LLM 上下文)。"""
        recent = self.messages[-max_turns:] if len(self.messages) > max_turns else self.messages
        return [{"role": m.role, "content": m.content} for m in recent]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "messages": [m.to_dict() for m in self.messages],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": self.metadata,
        }


# ── AI 响应模型 ──────────────────────────────────────────────────


class AIResponse(BaseModel):
    """AI 助手响应。"""

    message: str
    session_id: str
    intent: UserIntent | None = None
    confidence: float = 0.0
    suggested_products: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class ChatRequest(BaseModel):
    """聊天请求。"""

    message: str
    session_id: str | None = None
    user_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


class ProductQuestion(BaseModel):
    """商品咨询请求。"""

    product_id: str
    question: str
    user_id: str | None = None


class ProductDescriptionRequest(BaseModel):
    """商品描述生成请求。"""

    product_id: str
    style: str = "professional"  # professional / casual / luxury / technical
    target_audience: str = "general"
    keywords: list[str] = field(default_factory=list)


# ── 情感分析 ──────────────────────────────────────────────────────


class SentimentResult(BaseModel):
    """情感分析结果。"""

    text: str
    sentiment: SentimentType
    confidence: float
    aspects: dict[str, SentimentType] = field(default_factory=dict)  # 方面级情感
    keywords: list[str] = field(default_factory=list)


class ReviewAnalysis(BaseModel):
    """评论分析结果。"""

    product_id: str
    review_id: str
    sentiment: SentimentResult
    summary: str
    highlights: list[str]
    issues: list[str]


# ── 商品 Embedding ──────────────────────────────────────────────


class ProductEmbedding(BaseModel):
    """商品向量表示 (用于 RAG)。"""

    product_id: str
    embedding: list[float]  # 1536 维 (OpenAI) 或 768 维 (本地模型)
    text_content: str  # 原始文本 (用于相似度匹配后展示)
    model_name: str = "text-embedding-ada-002"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ── 配置模型 ──────────────────────────────────────────────────────


class AIConfig(BaseModel):
    """AI 助手配置。"""

    # LLM 配置
    provider: str = "openai"  # openai / anthropic / local
    model_name: str = "gpt-4"
    api_key: str = ""
    api_base: str = "https://api.openai.com/v1"

    # 生成参数
    temperature: float = 0.7
    max_tokens: int = 1000
    top_p: float = 0.9

    # RAG 配置
    embedding_model: str = "text-embedding-ada-002"
    embedding_dimension: int = 1536
    top_k_retrieval: int = 5
    similarity_threshold: float = 0.7

    # 对话配置
    max_history_turns: int = 10
    system_prompt: str = "You are a helpful shopping assistant for Hemall e-commerce platform."

    # 安全配置
    enable_content_filter: bool = True
    max_retry_attempts: int = 3
    timeout_seconds: int = 30
