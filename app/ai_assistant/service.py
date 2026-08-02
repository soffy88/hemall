"""AI 助手服务层 — 统一管理对话、意图识别、情感分析、内容生成。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from .llm_provider import LLMMessage, LLMProvider, LLMResponse, create_llm_provider
from .models import (
    AIConfig,
    AIResponse,
    ChatRequest,
    Conversation,
    ProductDescriptionRequest,
    ProductQuestion,
    ReviewAnalysis,
    SentimentResult,
    SentimentType,
    UserIntent,
)
from .rag_engine import RAGEngine
from .knowledge import KnowledgeBase

logger = logging.getLogger("hemall.ai.service")


class AIAssistantService:
    """AI 助手服务 — 统一入口。

    功能:
        - 多轮对话管理 (Session-based)
        - 商品智能问答 (RAG)
        - 意图识别 (LLM-based)
        - 情感分析 (Review sentiment)
        - 商品描述生成 (SEO optimized)
        - 流式响应 (SSE)
    """

    def __init__(self, config: AIConfig, pool: Any = None):
        self._config = config
        self._pool = pool
        self._llm: LLMProvider | None = None
        self._rag: RAGEngine | None = None
        self._knowledge: KnowledgeBase | None = None
        self._sessions: dict[str, Conversation] = {}
        self._initialized = False

    def _set_local_router_kb(self) -> None:
        """将知识库实例注册到路由模块 (避免循环导入)。"""
        if self._knowledge is not None:
            try:
                from . import router as _router
                _router.set_knowledge_base(self._knowledge)
            except Exception:  # noqa: BLE001
                pass

    async def initialize(self, pool: Any = None) -> None:
        """初始化 AI 助手服务。"""
        if pool:
            self._pool = pool

        # 创建知识库
        self._knowledge = KnowledgeBase(self._pool)
        await self._knowledge.initialize(self._pool)
        self._set_local_router_kb()

        # 创建 LLM Provider
        self._llm = create_llm_provider({
            "provider": self._config.provider,
            "api_key": self._config.api_key,
            "model_name": self._config.model_name,
            "api_base": self._config.api_base,
            "timeout": self._config.timeout_seconds,
        })

        # 创建 RAG 引擎
        self._rag = RAGEngine(
            llm_provider=self._llm,
            pool=self._pool,
            top_k=self._config.top_k_retrieval,
            similarity_threshold=self._config.similarity_threshold,
        )

        if self._pool:
            await self._rag.initialize(self._pool)

        self._initialized = True
        logger.info("AIAssistantService initialized (provider=%s, model=%s)",
                     self._config.provider, self._config.model_name)

    async def close(self) -> None:
        """关闭服务。"""
        if hasattr(self._llm, "close"):
            await self._llm.close()
        self._sessions.clear()
        logger.info("AIAssistantService closed")

    # ── 对话管理 ────────────────────────────────────────────────

    def _get_or_create_session(self, session_id: str | None, user_id: str | None) -> Conversation:
        """获取或创建对话会话。"""
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]

        new_session_id = session_id or str(uuid.uuid4())
        conversation = Conversation(session_id=new_session_id, user_id=user_id)
        self._sessions[new_session_id] = conversation
        return conversation

    async def chat(self, request: ChatRequest) -> AIResponse:
        """处理聊天请求 (完整流程)。

        流程:
            1. 获取/创建会话
            2. 识别用户意图
            3. RAG 检索相关商品
            4. LLM 生成回答
            5. 保存对话历史
        """
        if not self._initialized:
            await self.initialize()

        # 获取会话
        session = self._get_or_create_session(request.session_id, request.user_id)

        # 添加用户消息
        session.add_message("user", request.message)

        # 意图识别
        intent, confidence = await self._detect_intent(request.message)

        # RAG 检索
        context_docs = await self._rag.retrieve_context(request.message)

        # 构建 LLM 消息
        messages = self._build_messages(session, context_docs)

        # LLM 生成回答
        response = await self._llm.chat(
            messages,
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
        )

        # 保存助手回复
        session.add_message("assistant", response.content, {
            "intent": intent.value,
            "confidence": confidence,
            "model": response.model,
            "latency_ms": response.latency_ms,
        })

        # 提取推荐商品
        suggested_products = self._extract_product_ids(response.content, context_docs)

        return AIResponse(
            message=response.content,
            session_id=session.session_id,
            intent=intent,
            confidence=confidence,
            suggested_products=suggested_products,
            metadata={
                "model": response.model,
                "usage": response.usage,
                "latency_ms": response.latency_ms,
                "sources": [doc.split("\n")[0] for doc in context_docs],
            },
        )

    async def chat_stream(self, request: ChatRequest) -> AsyncIterator[str]:
        """流式聊天响应 (SSE)。"""
        if not self._initialized:
            await self.initialize()

        session = self._get_or_create_session(request.session_id, request.user_id)
        session.add_message("user", request.message)

        context_docs = await self._rag.retrieve_context(request.message)
        messages = self._build_messages(session, context_docs)

        full_response = ""
        async for chunk in self._llm.chat_stream(
            messages,
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
        ):
            full_response += chunk
            yield chunk

        # 保存完整响应
        session.add_message("assistant", full_response)

    # ── 商品问答 ────────────────────────────────────────────────

    async def ask_about_product(self, request: ProductQuestion) -> AIResponse:
        """针对特定商品的问答。"""
        if not self._initialized:
            await self.initialize()

        # 检索该商品的文档
        context_docs = await self._rag.retrieve_context(
            f"商品 {request.product_id}: {request.question}"
        )

        messages = [
            LLMMessage(role="system", content=self._config.system_prompt),
        ]

        if context_docs:
            context_text = "\n\n".join(context_docs)
            messages.append(LLMMessage(
                role="system",
                content=f"以下是相关商品信息:\n{context_text}"
            ))

        messages.append(LLMMessage(role="user", content=request.question))

        response = await self._llm.chat(messages, temperature=0.7, max_tokens=800)

        return AIResponse(
            message=response.content,
            session_id=str(uuid.uuid4()),
            intent=UserIntent.PRODUCT_INQUIRY,
            confidence=0.85 if context_docs else 0.4,
            suggested_products=[request.product_id],
            metadata={"model": response.model, "product_id": request.product_id},
        )

    # ── 商品描述生成 ────────────────────────────────────────────

    async def generate_product_description(self, request: ProductDescriptionRequest) -> dict[str, Any]:
        """AI 生成商品描述 (SEO 优化)。"""
        if not self._initialized:
            await self.initialize()

        style_prompts = {
            "professional": "Write a professional, informative product description.",
            "casual": "Write a casual, friendly product description.",
            "luxury": "Write an elegant, luxurious product description.",
            "technical": "Write a detailed, technical product description.",
        }

        style_guide = style_prompts.get(request.style, style_prompts["professional"])

        messages = [
            LLMMessage(role="system", content=f"""You are an expert e-commerce copywriter.
{style_guide}
Target audience: {request.target_audience}
Include SEO keywords: {', '.join(request.keywords) if request.keywords else 'auto-detect'}
Generate:
1. A compelling headline (1 line)
2. A short description (2-3 sentences)
3. Key features (bullet points, 3-5 items)
4. SEO meta description (150-160 characters)
Return in JSON format."""),
            LLMMessage(role="user", content=f"Product ID: {request.product_id}\nGenerate a product description."),
        ]

        response = await self._llm.chat(messages, temperature=0.8, max_tokens=1000)

        return {
            "product_id": request.product_id,
            "style": request.style,
            "generated_content": response.content,
            "model": response.model,
        }

    # ── 情感分析 ────────────────────────────────────────────────

    async def analyze_sentiment(self, text: str) -> SentimentResult:
        """分析文本情感。"""
        if not self._initialized:
            await self.initialize()

        messages = [
            LLMMessage(role="system", content="""Analyze the sentiment of the following text.
Return JSON with:
- sentiment: positive/negative/neutral/mixed
- confidence: 0.0-1.0
- keywords: list of key sentiment words
- aspects: dict of aspect -> sentiment (e.g., {"price": "negative", "quality": "positive"})"""),
            LLMMessage(role="user", content=text),
        ]

        response = await self._llm.chat(messages, temperature=0.3, max_tokens=500)

        # 解析 LLM 响应
        sentiment = SentimentType.NEUTRAL
        confidence = 0.5
        keywords = []
        aspects = {}

        try:
            import json
            data = json.loads(response.content)
            sentiment = SentimentType(data.get("sentiment", "neutral"))
            confidence = data.get("confidence", 0.5)
            keywords = data.get("keywords", [])
            aspects = {k: SentimentType(v) for k, v in data.get("aspects", {}).items()}
        except (json.JSONDecodeError, ValueError, KeyError):
            # 解析失败时，基于简单规则判断
            if any(word in text.lower() for word in ["great", "excellent", "love", "amazing"]):
                sentiment = SentimentType.POSITIVE
                confidence = 0.7
            elif any(word in text.lower() for word in ["bad", "terrible", "hate", "awful"]):
                sentiment = SentimentType.NEGATIVE
                confidence = 0.7

        return SentimentResult(
            text=text,
            sentiment=sentiment,
            confidence=confidence,
            aspects=aspects,
            keywords=keywords,
        )

    async def analyze_review(self, product_id: str, review_id: str, review_text: str) -> ReviewAnalysis:
        """分析商品评论。"""
        sentiment = await self.analyze_sentiment(review_text)

        # 生成摘要
        messages = [
            LLMMessage(role="system", content="Summarize this product review in 1-2 sentences. Highlight key points and any issues."),
            LLMMessage(role="user", content=review_text),
        ]

        response = await self._llm.chat(messages, temperature=0.5, max_tokens=200)

        return ReviewAnalysis(
            product_id=product_id,
            review_id=review_id,
            sentiment=sentiment,
            summary=response.content,
            highlights=self._extract_highlights(review_text),
            issues=self._extract_issues(review_text),
        )

    # ── 内部方法 ────────────────────────────────────────────────

    def _build_messages(self, session: Conversation, context_docs: list[str]) -> list[LLMMessage]:
        """构建 LLM 消息列表。"""
        messages = [LLMMessage(role="system", content=self._config.system_prompt)]

        # 添加检索到的上下文
        if context_docs:
            context_text = "\n\n---\n\n".join(context_docs)
            messages.append(LLMMessage(
                role="system",
                content=f"Relevant product information:\n{context_text}"
            ))

        # 添加对话历史
        for msg in session.get_history(self._config.max_history_turns):
            messages.append(LLMMessage(role=msg["role"], content=msg["content"]))

        return messages

    async def _detect_intent(self, message: str) -> tuple[UserIntent, float]:
        """识别用户意图。"""
        # 简单规则匹配 (快速路径)
        message_lower = message.lower()

        if any(word in message_lower for word in ["退款", "退货", "refund", "return"]):
            return UserIntent.REFUND_REQUEST, 0.9
        if any(word in message_lower for word in ["投诉", "complaint", "problem", "issue"]):
            return UserIntent.COMPLAINT, 0.85
        if any(word in message_lower for word in ["物流", "快递", "shipping", "delivery", "tracking"]):
            return UserIntent.SHIPPING_INQUIRY, 0.9
        if any(word in message_lower for word in ["推荐", "recommend", "suggest", "which"]):
            return UserIntent.RECOMMENDATION, 0.8
        if any(word in message_lower for word in ["对比", "compare", "vs", "difference"]):
            return UserIntent.COMPARISON, 0.85
        if any(word in message_lower for word in ["买", "购买", "buy", "purchase", "order"]):
            return UserIntent.PURCHASE_INTENT, 0.8

        # LLM 意图识别 (复杂场景)
        messages = [
            LLMMessage(role="system", content="""Classify the user's intent into one of:
product_inquiry, purchase_intent, complaint, refund_request, shipping_inquiry, 
general_question, recommendation, comparison, unknown
Return only the intent name and confidence (0.0-1.0), separated by comma."""),
            LLMMessage(role="user", content=message),
        ]

        try:
            response = await self._llm.chat(messages, temperature=0.3, max_tokens=50)
            parts = response.content.strip().split(",")
            intent = UserIntent(parts[0].strip())
            confidence = float(parts[1].strip()) if len(parts) > 1 else 0.7
            return intent, confidence
        except Exception:
            return UserIntent.GENERAL_QUESTION, 0.5

    def _extract_product_ids(self, response: str, context_docs: list[str]) -> list[str]:
        """从响应和上下文中提取商品 ID。"""
        product_ids = set()
        for doc in context_docs:
            if doc.startswith("[商品: "):
                pid = doc.split(": ")[1].split("]")[0]
                product_ids.add(pid)
        return list(product_ids)

    def _extract_highlights(self, text: str) -> list[str]:
        """提取评论亮点。"""
        highlights = []
        positive_keywords = ["good", "great", "excellent", "love", "amazing", "perfect", "好", "优秀", "完美"]
        for keyword in positive_keywords:
            if keyword in text.lower():
                # 提取包含关键词的句子
                sentences = text.split(".")
                for sentence in sentences:
                    if keyword in sentence.lower() and len(sentence.strip()) > 10:
                        highlights.append(sentence.strip())
                        break
        return highlights[:3]

    def _extract_issues(self, text: str) -> list[str]:
        """提取评论问题。"""
        issues = []
        negative_keywords = ["bad", "terrible", "awful", "problem", "issue", "broken", "差", "问题", "坏"]
        for keyword in negative_keywords:
            if keyword in text.lower():
                sentences = text.split(".")
                for sentence in sentences:
                    if keyword in sentence.lower() and len(sentence.strip()) > 10:
                        issues.append(sentence.strip())
                        break
        return issues[:3]
