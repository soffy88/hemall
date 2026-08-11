"""AI 助手 API 路由 — 对话、商品问答、描述生成、情感分析。

Phase 4+ Priority 1: LLM 驱动的智能助手接口。

API:
    POST /ai/chat                    # 多轮对话 (支持流式)
    POST /ai/chat/stream             # 流式对话 (SSE)
    POST /ai/product/ask             # 商品智能问答
    POST /ai/product/description     # 商品描述生成
    POST /ai/sentiment               # 情感分析
    POST /ai/review/analyze          # 评论分析
    GET  /ai/sessions                # 获取对话列表
    DELETE /ai/sessions/{id}         # 删除对话
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..config import Settings
from ..deps import get_current_user, get_settings
from .knowledge import FAQItem, KnowledgeBase, KnowledgeCategory
from .models import AIResponse, ChatRequest, ProductDescriptionRequest, ProductQuestion
from .service import AIAssistantService

# AI 助手端点全程调 LLM (烧钱)，无鉴权即敞口刷账单。顾客前端未接此路由 (走
# /store)，故一律要求员工 JWT；将来若要面向顾客，应加顾客 token + 限流的专用变体。
router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(get_current_user)])
logger = logging.getLogger("hemall.ai.router")


_kb: KnowledgeBase | None = None


def set_knowledge_base(kb: KnowledgeBase) -> None:
    """注册知识库实例 (由 service 调用)。"""
    global _kb
    _kb = kb


def get_knowledge_base() -> KnowledgeBase:
    if _kb is None:
        raise HTTPException(status_code=503, detail="KnowledgeBase not initialized")
    return _kb


def get_ai_service(
    settings: Settings = Depends(get_settings),
) -> AIAssistantService:
    """获取 AI 助手服务实例。"""
    from app.main import get_app_state

    app_state = get_app_state()
    if not hasattr(app_state, "ai_service") or app_state.ai_service is None:
        raise HTTPException(
            status_code=503, detail="AI Assistant Service not initialized"
        )
    return app_state.ai_service


@router.post("/chat")
async def chat(
    request: ChatRequest,
    svc: AIAssistantService = Depends(get_ai_service),
) -> dict[str, Any]:
    """多轮对话接口。"""
    response = await svc.chat(request)
    return {"data": response.model_dump(mode="json")}


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    svc: AIAssistantService = Depends(get_ai_service),
) -> StreamingResponse:
    """流式对话接口 (Server-Sent Events)。"""

    async def event_generator():
        async for chunk in svc.chat_stream(request):
            yield f"data: {chunk}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/product/ask")
async def ask_about_product(
    request: ProductQuestion,
    svc: AIAssistantService = Depends(get_ai_service),
) -> dict[str, Any]:
    """商品智能问答。"""
    response = await svc.ask_about_product(request)
    return {"data": response.model_dump(mode="json")}


@router.post("/product/description")
async def generate_description(
    request: ProductDescriptionRequest,
    svc: AIAssistantService = Depends(get_ai_service),
) -> dict[str, Any]:
    """AI 生成商品描述。"""
    result = await svc.generate_product_description(request)
    return {"data": result}


@router.post("/sentiment")
async def analyze_sentiment(
    text: str = Query(..., description="待分析文本"),
    svc: AIAssistantService = Depends(get_ai_service),
) -> dict[str, Any]:
    """情感分析。"""
    result = await svc.analyze_sentiment(text)
    return {"data": result.model_dump(mode="json")}


@router.post("/review/analyze")
async def analyze_review(
    product_id: str = Query(..., description="商品 ID"),
    review_id: str = Query(..., description="评论 ID"),
    review_text: str = Query(..., description="评论文本"),
    svc: AIAssistantService = Depends(get_ai_service),
) -> dict[str, Any]:
    """评论分析。"""
    result = await svc.analyze_review(product_id, review_id, review_text)
    return {"data": result.model_dump(mode="json")}


@router.get("/sessions")
async def list_sessions(
    svc: AIAssistantService = Depends(get_ai_service),
) -> dict[str, Any]:
    """获取所有对话会话。"""
    sessions = [
        {
            "session_id": s.session_id,
            "user_id": s.user_id,
            "message_count": len(s.messages),
            "created_at": s.created_at.isoformat(),
            "updated_at": s.updated_at.isoformat(),
        }
        for s in svc._sessions.values()
    ]
    return {"data": sessions}


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    svc: AIAssistantService = Depends(get_ai_service),
) -> dict[str, Any]:
    """删除对话会话。"""
    if session_id in svc._sessions:
        del svc._sessions[session_id]
        return {"status": "deleted", "session_id": session_id}
    raise HTTPException(status_code=404, detail="Session not found")


# ── 知识库端点 ────────────────────────────────────────────────────


@router.post("/knowledge/add")
async def add_knowledge(
    item: FAQItem,
    kb: KnowledgeBase = Depends(get_knowledge_base),
) -> dict[str, Any]:
    """添加知识条目。"""
    result = await kb.add_item(item)
    return {"data": result.to_dict()}


@router.get("/knowledge/{item_id}")
async def get_knowledge_item(
    item_id: str,
    kb: KnowledgeBase = Depends(get_knowledge_base),
) -> dict[str, Any]:
    """获取知识条目。"""
    item = await kb.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    return {"data": item.to_dict()}


@router.get("/knowledge/search")
async def search_knowledge(
    query: str = Query(..., description="查询文本"),
    category: str | None = Query(None, description="类别过滤"),
    limit: int = Query(5, ge=1, le=20),
    kb: KnowledgeBase = Depends(get_knowledge_base),
) -> dict[str, Any]:
    """检索知识库。"""
    items = await kb.search(query, category, limit)
    return {"data": [item.to_dict() for item in items]}


@router.get("/knowledge/stats")
async def knowledge_stats(
    kb: KnowledgeBase = Depends(get_knowledge_base),
) -> dict[str, Any]:
    """知识库统计。"""
    stats = await kb.get_stats()
    return {"data": stats}


@router.post("/knowledge/{item_id}/helpful")
async def mark_helpful(
    item_id: str,
    kb: KnowledgeBase = Depends(get_knowledge_base),
) -> dict[str, Any]:
    """标记知识条目有帮助。"""
    success = await kb.mark_helpful(item_id)
    if not success:
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    return {"status": "marked", "item_id": item_id}
