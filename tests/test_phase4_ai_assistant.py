"""Phase 4+ AI 助手模块测试 — LLM Provider、RAG 引擎、AI 服务。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai_assistant.llm_provider import (
    LLMMessage,
    LLMProvider,
    LLMResponse,
    OpenAIProvider,
    AnthropicProvider,
    LocalLLMProvider,
    create_llm_provider,
)
from app.ai_assistant.models import (
    AIConfig,
    AIResponse,
    ChatRequest,
    Conversation,
    Message,
    ProductDescriptionRequest,
    ProductQuestion,
    SentimentResult,
    SentimentType,
    UserIntent,
)
from app.ai_assistant.rag_engine import RAGEngine
from app.ai_assistant.service import AIAssistantService


# ── LLM Provider 测试 ─────────────────────────────────────────────


def test_llm_message():
    """测试 LLM 消息格式。"""
    msg = LLMMessage(role="user", content="Hello")
    assert msg.to_openai_format() == {"role": "user", "content": "Hello"}


def test_llm_response():
    """测试 LLM 响应。"""
    response = LLMResponse(
        content="Hello!",
        finish_reason="stop",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        model="gpt-4",
        latency_ms=150.5,
    )
    assert response.content == "Hello!"
    assert response.model == "gpt-4"


@pytest.mark.asyncio
async def test_openai_provider_sandbox():
    """测试 OpenAI Provider SANDBOX 模式 (无 API Key)。"""
    provider = OpenAIProvider(api_key="", model_name="gpt-4")

    messages = [LLMMessage(role="user", content="Hello")]
    response = await provider.chat(messages)

    assert "[SANDBOX]" in response.content
    assert response.model == "gpt-4"


@pytest.mark.asyncio
async def test_openai_provider_embed_sandbox():
    """测试 OpenAI Embedding SANDBOX 模式。"""
    provider = OpenAIProvider(api_key="")
    embedding = await provider.embed("test text")

    assert len(embedding) == 1536
    assert all(v == 0.0 for v in embedding)


@pytest.mark.asyncio
async def test_anthropic_provider_sandbox():
    """测试 Anthropic Provider SANDBOX 模式。"""
    provider = AnthropicProvider(api_key="", model_name="claude-3-sonnet-20240229")

    messages = [LLMMessage(role="user", content="Hello")]
    response = await provider.chat(messages)

    assert "[SANDBOX]" in response.content


@pytest.mark.asyncio
async def test_local_llm_provider():
    """测试本地 LLM Provider (不连接实际服务)。"""
    provider = LocalLLMProvider(model_name="llama3", api_base="http://localhost:11434")

    # 由于没有实际服务，应该返回错误
    messages = [LLMMessage(role="user", content="Hello")]
    response = await provider.chat(messages)

    assert "[ERROR]" in response.content


def test_create_llm_provider_openai():
    """测试 LLM Provider 工厂函数 (OpenAI)。"""
    config = {"provider": "openai", "api_key": "test", "model_name": "gpt-4"}
    provider = create_llm_provider(config)
    assert isinstance(provider, OpenAIProvider)


def test_create_llm_provider_anthropic():
    """测试 LLM Provider 工厂函数 (Anthropic)。"""
    config = {"provider": "anthropic", "api_key": "test", "model_name": "claude-3"}
    provider = create_llm_provider(config)
    assert isinstance(provider, AnthropicProvider)


def test_create_llm_provider_local():
    """测试 LLM Provider 工厂函数 (Local)。"""
    config = {"provider": "local", "model_name": "llama3"}
    provider = create_llm_provider(config)
    assert isinstance(provider, LocalLLMProvider)


def test_create_llm_provider_unknown():
    """测试 LLM Provider 工厂函数 (未知提供商)。"""
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        create_llm_provider({"provider": "unknown"})


# ── 对话模型测试 ──────────────────────────────────────────────────


def test_conversation():
    """测试对话会话管理。"""
    conv = Conversation(session_id="test-123", user_id="user-456")

    # 添加消息
    msg1 = conv.add_message("user", "Hello")
    msg2 = conv.add_message("assistant", "Hi there!")

    assert len(conv.messages) == 2
    assert msg1.role == "user"
    assert msg2.role == "assistant"


def test_conversation_history():
    """测试对话历史获取。"""
    conv = Conversation(session_id="test-123", user_id="user-456")

    # 添加多条消息
    for i in range(15):
        conv.add_message("user" if i % 2 == 0 else "assistant", f"Message {i}")

    history = conv.get_history(max_turns=10)
    assert len(history) == 10


# ── RAG 引擎测试 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rag_engine_build_document():
    """测试 RAG 引擎文档构建。"""
    provider = OpenAIProvider(api_key="")
    rag = RAGEngine(llm_provider=provider)

    product_data = {
        "name": "iPhone 15 Pro",
        "brand_name": "Apple",
        "description": "Latest iPhone with titanium design",
        "selling_price_cents": 999900,
        "rating_avg": 4.8,
        "sold_count": 10000,
        "tags": ["hot", "new"],
        "specifications": [
            {"name": "Screen", "value": "6.1 inch"},
            {"name": "Chip", "value": "A17 Pro"},
        ],
    }

    doc = rag._build_product_document(product_data)

    assert "iPhone 15 Pro" in doc
    assert "Apple" in doc
    assert "9999.00" in doc
    assert "Screen: 6.1 inch" in doc


@pytest.mark.asyncio
async def test_rag_engine_retrieve_context():
    """测试 RAG 引擎上下文检索 (无数据库)。"""
    provider = OpenAIProvider(api_key="")
    rag = RAGEngine(llm_provider=provider)

    # 无数据库时应返回空列表
    docs = await rag.retrieve_context("iPhone")
    assert isinstance(docs, list)


# ── AI 服务测试 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ai_service_chat():
    """测试 AI 助手服务对话。"""
    config = AIConfig(provider="openai", api_key="", model_name="gpt-4")
    service = AIAssistantService(config)

    request = ChatRequest(
        message="Hello",
        session_id="test-session",
        user_id="test-user",
    )

    response = await service.chat(request)

    assert isinstance(response, AIResponse)
    assert response.session_id == "test-session"
    assert "[SANDBOX]" in response.message


@pytest.mark.asyncio
async def test_ai_service_intent_detection():
    """测试 AI 助手意图识别。"""
    config = AIConfig(provider="openai", api_key="")
    service = AIAssistantService(config)
    await service.initialize()

    # 测试退款意图
    intent, confidence = await service._detect_intent("我要退款")
    assert intent == UserIntent.REFUND_REQUEST
    assert confidence >= 0.9

    # 测试物流查询意图
    intent, confidence = await service._detect_intent("我的快递到哪了")
    assert intent == UserIntent.SHIPPING_INQUIRY

    # 测试购买意图
    intent, confidence = await service._detect_intent("我想买这个商品")
    assert intent == UserIntent.PURCHASE_INTENT


@pytest.mark.asyncio
async def test_ai_service_sentiment_analysis():
    """测试 AI 助手情感分析。"""
    config = AIConfig(provider="openai", api_key="")
    service = AIAssistantService(config)

    # 正面情感
    result = await service.analyze_sentiment("This product is amazing! I love it!")
    assert result.sentiment == SentimentType.POSITIVE
    assert result.confidence > 0.5

    # 负面情感
    result = await service.analyze_sentiment("Terrible quality, I hate it")
    assert result.sentiment == SentimentType.NEGATIVE


@pytest.mark.asyncio
async def test_ai_service_product_description():
    """测试 AI 助手商品描述生成。"""
    config = AIConfig(provider="openai", api_key="")
    service = AIAssistantService(config)

    request = ProductDescriptionRequest(
        product_id="prod-123",
        style="professional",
        target_audience="tech enthusiasts",
        keywords=["iPhone", "smartphone", "Apple"],
    )

    result = await service.generate_product_description(request)

    assert result["product_id"] == "prod-123"
    assert result["style"] == "professional"
    assert "generated_content" in result


@pytest.mark.asyncio
async def test_ai_service_session_management():
    """测试 AI 助手会话管理。"""
    config = AIConfig(provider="openai", api_key="")
    service = AIAssistantService(config)

    # 创建会话
    session = service._get_or_create_session("test-session", "user-123")
    assert session.session_id == "test-session"

    # 获取已存在会话
    session2 = service._get_or_create_session("test-session", "user-123")
    assert session2 is session

    # 创建新会话 (无 session_id)
    session3 = service._get_or_create_session(None, "user-456")
    assert session3.session_id != "test-session"


@pytest.mark.asyncio
async def test_ai_service_chat_stream():
    """测试 AI 助手流式对话。"""
    config = AIConfig(provider="openai", api_key="")
    service = AIAssistantService(config)

    request = ChatRequest(message="Hello", session_id="test-stream")

    chunks = []
    async for chunk in service.chat_stream(request):
        chunks.append(chunk)

    # SANDBOX 模式应返回模拟响应
    assert len(chunks) > 0
    assert "[SANDBOX]" in "".join(chunks)


# ── 知识库测试 ──────────────────────────────────────────────────

from app.ai_assistant.knowledge import FAQItem, KnowledgeBase, KnowledgeCategory


@pytest.mark.asyncio
async def test_knowledge_base_add_and_get():
    """测试知识库添加和获取。"""
    kb = KnowledgeBase()
    await kb.initialize()

    item = FAQItem(
        category="shipping",
        title="物流查询",
        question="如何查询我的订单物流信息？",
        answer="您可以在'我的订单'页面点击'查看物流'获取实时物流信息。",
        keywords=["物流", "快递", "tracking"],
    )

    result = await kb.add_item(item)
    assert result.item_id is not None
    assert result.category == KnowledgeCategory.SHIPPING

    # 获取条目
    fetched = await kb.get_item(result.item_id)
    assert fetched is not None
    assert fetched.answer == item.answer
    assert fetched.view_count == 1  # 被查看一次


@pytest.mark.asyncio
async def test_knowledge_base_search():
    """测试知识库检索。"""
    kb = KnowledgeBase()
    await kb.initialize()

    # 添加多个条目
    await kb.add_item(FAQItem(
        category="return", title="退换货政策", question="怎么申请退货？",
        answer="7天无理由退货", keywords=["退货", "退款"],
    ))
    await kb.add_item(FAQItem(
        category="shipping", title="物流时效", question="多久能收到货？",
        answer="默认3-5天", keywords=["物流", "时效"],
    ))

    # 检索退货相关
    results = await kb.search("怎么退货")
    assert len(results) >= 1
    assert results[0].category == KnowledgeCategory.RETURN

    # 检索物流相关
    results = await kb.search("物流怎么查询", category="shipping")
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_knowledge_base_mark_helpful():
    """测试知识库帮助反馈。"""
    kb = KnowledgeBase()
    await kb.initialize()

    item = FAQItem(
        category="payment", title="支付方式", question="支持哪些支付方式？",
        answer="微信支付和支付宝", keywords=["支付"],
    )
    result = await kb.add_item(item)

    success = await kb.mark_helpful(result.item_id)
    assert success
    assert result.helpful_count == 1


@pytest.mark.asyncio
async def test_knowledge_base_stats():
    """测试知识库统计。"""
    kb = KnowledgeBase()
    await kb.initialize()

    await kb.add_item(FAQItem(category="order", title="订单", question="q", answer="a"))
    await kb.add_item(FAQItem(category="payment", title="支付", question="q", answer="a"))

    stats = await kb.get_stats()
    assert stats["total_items"] == 2
    assert stats["by_category"]["order"] == 1
    assert stats["by_category"]["payment"] == 1


@pytest.mark.asyncio
async def test_knowledge_category_enum():
    """测试知识类别枚举。"""
    assert KnowledgeCategory.ORDER.value == "order"
    assert KnowledgeCategory.SHIPPING.value == "shipping"
    assert KnowledgeCategory.PAYMENT.value == "payment"
    assert KnowledgeCategory.PRODUCT.value == "product"
    assert KnowledgeCategory.RETURN.value == "return"


@pytest.mark.asyncio
async def test_knowledge_invalid_category():
    """测试无效类别自动降级为 GENERAL。"""
    kb = KnowledgeBase()
    await kb.initialize()

    item = FAQItem(
        category="invalid_category",
        title="测试", question="q", answer="a",
    )
    result = await kb.add_item(item)
    assert result.category == KnowledgeCategory.GENERAL


if __name__ == "__main__":
    pytest.main([__file__, "-v"])