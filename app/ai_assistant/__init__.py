"""hemall AI 助手模块 — LLM 驱动的商品问答、智能客服和内容生成。

Phase 4+ Priority 1: 利用大语言模型提升用户体验和运营效率。

核心功能:
    - 商品智能问答 (基于商品信息的 RAG)
    - 智能客服助手 (多轮对话 + 意图识别)
    - 商品描述自动生成 (SEO 优化)
    - 用户评论情感分析
    - 购物意图理解和推荐

技术架构:
    - OpenAI GPT-4 / Claude / 本地 LLM 适配
    - RAG (Retrieval-Augmented Generation) 模式
    - 向量数据库 (pgvector) 存储商品 embedding
    - 对话历史管理
    - 流式响应 (SSE)
"""

from __future__ import annotations
