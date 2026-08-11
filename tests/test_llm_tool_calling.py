"""LLM provider 工具调用 (function calling) 契约回归。

修复前 admin_agent_chat 调 llm.chat_with_tools 必抛 AttributeError → 端点 500。
这里锁定：chat_with_tools 存在且沙盒可用、LLMResponse 带 tool_calls、chat 接受
model= 覆盖不再因重复传参崩溃、不支持工具的 provider 优雅降级 (tool_calls=None)。
"""

from __future__ import annotations

import asyncio

from app.ai_assistant.llm_provider import (
    AnthropicProvider,
    LLMMessage,
    LLMResponse,
    OpenAIProvider,
)

_MSGS = [LLMMessage(role="user", content="hi")]
_TOOLS = [
    {
        "name": "q",
        "description": "d",
        "parameters": {"type": "object", "properties": {}},
    }
]


def test_llm_response_has_tool_calls_field():
    r = LLMResponse(content="x")
    assert r.tool_calls is None


def test_openai_chat_with_tools_sandbox_no_crash():
    p = OpenAIProvider(api_key="", model_name="gpt-4")
    r = asyncio.run(p.chat_with_tools(_MSGS, tools=_TOOLS, model="gpt-4o"))
    assert isinstance(r, LLMResponse)
    assert r.tool_calls is None  # 沙盒无 key → 无工具调用, 上层走直答分支
    assert r.model == "gpt-4o"


def test_openai_chat_accepts_model_override():
    # 修复前: kwargs 里的 model 与显式 model= 重复 → TypeError。
    p = OpenAIProvider(api_key="", model_name="gpt-4")
    r = asyncio.run(p.chat(_MSGS, model="gpt-4o", temperature=0.1))
    assert isinstance(r, LLMResponse)
    assert r.model == "gpt-4o"


def test_base_chat_with_tools_graceful_degradation():
    # Anthropic 未覆盖 chat_with_tools → 用基类降级实现, 忽略 tools 直答。
    p = AnthropicProvider(api_key="", model_name="claude-x")
    r = asyncio.run(p.chat_with_tools(_MSGS, tools=_TOOLS, model="claude-y"))
    assert isinstance(r, LLMResponse)
    assert r.tool_calls is None
