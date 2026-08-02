"""RAG 引擎 — 检索增强生成，基于商品知识的智能问答。

核心流程:
    1. 商品文档向量化 (Embedding) → 存入 pgvector
    2. 用户问题向量化
    3. 向量相似度检索 Top-K 相关文档
    4. 构建 Prompt (System + Context + User Query)
    5. LLM 生成回答
"""

from __future__ import annotations

import logging
from typing import Any

from .llm_provider import LLMMessage, LLMProvider, LLMResponse
from .models import ProductEmbedding

logger = logging.getLogger("hemall.ai.rag")


class RAGEngine:
    """检索增强生成引擎。"""

    def __init__(
        self,
        llm_provider: LLMProvider,
        pool: Any = None,
        top_k: int = 5,
        similarity_threshold: float = 0.7,
    ):
        self._llm = llm_provider
        self._pool = pool
        self._top_k = top_k
        self._similarity_threshold = similarity_threshold

    async def initialize(self, pool: Any) -> None:
        """初始化 RAG 引擎 (创建向量表)。"""
        self._pool = pool
        await self._create_vector_table()
        logger.info("RAG engine initialized")

    async def _create_vector_table(self) -> None:
        """创建 pgvector 向量表。"""
        ddl = """
        CREATE TABLE IF NOT EXISTS ai_product_embeddings (
            id SERIAL PRIMARY KEY,
            product_id VARCHAR(36) NOT NULL UNIQUE,
            embedding vector(1536),
            text_content TEXT NOT NULL,
            model_name VARCHAR(100) DEFAULT 'text-embedding-ada-002',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_product_embedding ON ai_product_embeddings
            USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
        """
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(ddl)
        except Exception as exc:
            logger.warning("Failed to create vector table (pgvector may not be installed): %s", exc)

    async def index_product(
        self,
        product_id: str,
        product_data: dict[str, Any],
    ) -> ProductEmbedding | None:
        """将商品信息向量化并存储。

        Args:
            product_id: 商品 ID
            product_data: 商品数据 (name, description, specs, etc.)

        Returns:
            ProductEmbedding 或 None (失败时)
        """
        # 构建商品文档文本
        text_content = self._build_product_document(product_data)

        # 向量化
        embedding = await self._llm.embed(text_content)

        if not embedding or all(v == 0.0 for v in embedding):
            logger.warning("Failed to generate embedding for product %s", product_id)
            return None

        result = ProductEmbedding(
            product_id=product_id,
            embedding=embedding,
            text_content=text_content,
        )

        # 存储到数据库
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO ai_product_embeddings (product_id, embedding, text_content)
                    VALUES ($1, $2::vector, $3)
                    ON CONFLICT (product_id) DO UPDATE
                    SET embedding = $2::vector, text_content = $3, updated_at = NOW()
                    """,
                    product_id,
                    str(embedding),
                    text_content,
                )
            logger.info("Indexed product %s", product_id)
        except Exception as exc:
            logger.error("Failed to store embedding for product %s: %s", product_id, exc)

        return result

    async def retrieve_context(self, query: str, top_k: int | None = None) -> list[str]:
        """检索与查询最相关的商品文档。

        Args:
            query: 用户查询文本
            top_k: 返回的文档数量

        Returns:
            相关商品文档文本列表
        """
        top_k = top_k or self._top_k

        # 向量化查询
        query_embedding = await self._llm.embed(query)

        if not query_embedding or all(v == 0.0 for v in query_embedding):
            logger.warning("Failed to generate query embedding")
            return []

        # 向量相似度检索
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT product_id, text_content,
                           1 - (embedding <=> $1::vector) as similarity
                    FROM ai_product_embeddings
                    WHERE 1 - (embedding <=> $1::vector) > $2
                    ORDER BY embedding <=> $1::vector
                    LIMIT $3
                    """,
                    str(query_embedding),
                    self._similarity_threshold,
                    top_k,
                )

            documents = []
            for row in rows:
                documents.append(f"[商品: {row['product_id']}] (相似度: {row['similarity']:.2f})\n{row['text_content']}")

            logger.info("Retrieved %d documents for query: %s", len(documents), query[:50])
            return documents
        except Exception as exc:
            logger.error("Vector search failed: %s", exc)
            return []

    async def generate_answer(
        self,
        query: str,
        context_documents: list[str],
        chat_history: list[dict[str, str]] | None = None,
    ) -> LLMResponse:
        """基于检索到的上下文生成回答。

        Args:
            query: 用户问题
            context_documents: 检索到的相关文档
            chat_history: 对话历史

        Returns:
            LLMResponse 包含生成的回答
        """
        # 构建 System Prompt
        system_prompt = """You are Hemall's intelligent shopping assistant. 
Answer customer questions based on the provided product information.
Be helpful, accurate, and friendly. If you don't know the answer, say so honestly.
Always respond in the same language as the user's question.

Available product information:
{context}"""

        context_text = "\n\n---\n\n".join(context_documents) if context_documents else "No relevant product information found."

        messages = [
            LLMMessage(role="system", content=system_prompt.format(context=context_text)),
        ]

        # 添加对话历史
        if chat_history:
            for msg in chat_history[-6:]:  # 最近 3 轮对话
                messages.append(LLMMessage(role=msg["role"], content=msg["content"]))

        # 添加当前查询
        messages.append(LLMMessage(role="user", content=query))

        # 调用 LLM 生成回答
        response = await self._llm.chat(messages, temperature=0.7, max_tokens=1000)
        return response

    async def query(self, user_question: str, chat_history: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """完整的 RAG 查询流程。

        Args:
            user_question: 用户问题
            chat_history: 对话历史

        Returns:
            包含答案、检索到的文档、置信度的字典
        """
        # 1. 检索相关文档
        context_docs = await self.retrieve_context(user_question)

        # 2. 生成回答
        response = await self.generate_answer(user_question, context_docs, chat_history)

        return {
            "answer": response.content,
            "sources": [doc.split("\n")[0] for doc in context_docs],  # 提取商品 ID
            "confidence": 0.8 if context_docs else 0.3,
            "model": response.model,
            "latency_ms": response.latency_ms,
        }

    def _build_product_document(self, product_data: dict[str, Any]) -> str:
        """将商品数据转换为文档文本。"""
        parts = []

        if name := product_data.get("name"):
            parts.append(f"商品名称: {name}")
        if subtitle := product_data.get("subtitle"):
            parts.append(f"副标题: {subtitle}")
        if brand := product_data.get("brand_name"):
            parts.append(f"品牌: {brand}")
        if category := product_data.get("category_path"):
            parts.append(f"分类: {category}")
        if description := product_data.get("description"):
            parts.append(f"描述: {description}")
        if price := product_data.get("selling_price_cents"):
            parts.append(f"售价: ¥{price / 100:.2f}")
        if rating := product_data.get("rating_avg"):
            parts.append(f"评分: {rating}/5.0")
        if sold := product_data.get("sold_count"):
            parts.append(f"销量: {sold}")

        # 规格参数
        specs = product_data.get("specifications", [])
        if specs:
            parts.append("规格参数:")
            for spec in specs[:10]:
                parts.append(f"  - {spec.get('name')}: {spec.get('value')}")

        # 商品标签
        tags = product_data.get("tags", [])
        if tags:
            parts.append(f"标签: {', '.join(tags)}")

        return "\n".join(parts)