"""LLM Provider 抽象层 — 支持 OpenAI / Anthropic / 本地模型。

统一接口封装不同 LLM 提供商，便于切换和降级。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

logger = logging.getLogger("hemall.ai.llm_provider")


@dataclass
class LLMMessage:
    """LLM 消息格式。"""

    role: str  # system / user / assistant
    content: str

    def to_openai_format(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}

    def to_anthropic_format(self) -> dict[str, str]:
        if self.role == "system":
            return {"type": "system", "content": self.content}
        return {"role": self.role, "content": self.content}


@dataclass
class LLMResponse:
    """LLM 响应。"""

    content: str
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    latency_ms: float = 0.0
    #: 原生 function calling 触发的工具调用 (OpenAI 格式: 每项含 function.name /
    #: function.arguments)。无工具调用 (或 provider 不支持) 时为 None。
    tool_calls: list[dict[str, Any]] | None = None


class LLMProvider(ABC):
    """LLM 提供商基类。"""

    @abstractmethod
    async def chat(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> LLMResponse:
        """同步聊天接口。"""
        ...

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """流式聊天接口。"""
        ...

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """文本向量化。"""
        ...

    async def chat_with_tools(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> LLMResponse:
        """带工具 (function calling) 的对话。

        默认降级实现：不支持原生工具调用的 provider 忽略 tools、直接作答
        (tool_calls=None)，调用方据此走"无工具直答"分支——绝不因缺能力而 500。
        支持原生工具调用的 provider (如 OpenAI) 覆盖本方法。
        """
        return await self.chat(
            messages, temperature=temperature, max_tokens=max_tokens, **kwargs
        )


class OpenAIProvider(LLMProvider):
    """OpenAI GPT 系列模型提供商。"""

    def __init__(
        self,
        api_key: str,
        model_name: str = "gpt-4",
        api_base: str = "https://api.openai.com/v1",
        timeout: int = 30,
    ):
        self._api_key = api_key
        self._model_name = model_name
        self._api_base = api_base
        self._timeout = timeout
        self._client = None
        self._initialized = False

    async def _ensure_client(self) -> None:
        """延迟初始化 OpenAI 客户端。"""
        if not self._initialized:
            try:
                import openai

                self._client = openai.AsyncOpenAI(
                    api_key=self._api_key,
                    base_url=self._api_base,
                    timeout=self._timeout,
                )
                self._initialized = True
                logger.info("OpenAI client initialized: model=%s", self._model_name)
            except ImportError:
                logger.warning("openai package not installed, using fallback mode")
                self._initialized = True
            except Exception as exc:
                logger.error("Failed to initialize OpenAI client: %s", exc)
                self._initialized = True

    async def chat(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> LLMResponse:
        """OpenAI Chat Completion API。"""
        await self._ensure_client()

        # 调用方可用 model= 覆盖模型；从 kwargs 摘出避免与下方 model= 重复传参。
        model = kwargs.pop("model", None) or self._model_name

        if not self._client:
            return LLMResponse(
                content="[SANDBOX] OpenAI not configured. This is a simulated response.",
                finish_reason="stop",
                usage={"prompt_tokens": 0, "completion_tokens": 0},
                model=model,
            )

        import time

        start = time.time()
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=[m.to_openai_format() for m in messages],
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
            latency = (time.time() - start) * 1000

            choice = response.choices[0]
            return LLMResponse(
                content=choice.message.content or "",
                finish_reason=choice.finish_reason or "stop",
                usage={
                    "prompt_tokens": response.usage.prompt_tokens
                    if response.usage
                    else 0,
                    "completion_tokens": response.usage.completion_tokens
                    if response.usage
                    else 0,
                },
                model=response.model,
                latency_ms=latency,
            )
        except Exception as exc:
            logger.error("OpenAI chat error: %s", exc)
            return LLMResponse(
                content=f"[ERROR] LLM request failed: {exc}",
                finish_reason="error",
                model=self._model_name,
            )

    async def chat_with_tools(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> LLMResponse:
        """OpenAI 原生 function calling。

        tools 为 {name, description, parameters} 列表 (ToolDefinition.model_dump)，
        此处包装成 OpenAI tools schema。返回的 tool_calls 保持 OpenAI 结构
        (含 function.name / function.arguments)，供调用方按名派发执行。
        """
        await self._ensure_client()
        model = kwargs.pop("model", None) or self._model_name

        if not self._client:
            # 无 key: 退化为无工具直答, 让上层走 "no tool_calls" 分支 (不 500)。
            return LLMResponse(
                content="[SANDBOX] OpenAI not configured. This is a simulated response.",
                finish_reason="stop",
                model=model,
                tool_calls=None,
            )

        openai_tools = [{"type": "function", "function": t} for t in tools]
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=[m.to_openai_format() for m in messages],
                tools=openai_tools,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
            choice = response.choices[0]
            raw_calls = choice.message.tool_calls or []
            tool_calls = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.function.name,
                        "arguments": c.function.arguments,
                    },
                }
                for c in raw_calls
            ] or None
            return LLMResponse(
                content=choice.message.content or "",
                finish_reason=choice.finish_reason or "stop",
                model=response.model,
                tool_calls=tool_calls,
            )
        except Exception as exc:
            logger.error("OpenAI chat_with_tools error: %s", exc)
            return LLMResponse(
                content=f"[ERROR] LLM tool call failed: {exc}",
                finish_reason="error",
                model=model,
                tool_calls=None,
            )

    async def chat_stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """OpenAI 流式 Chat Completion。"""
        await self._ensure_client()

        if not self._client:
            yield "[SANDBOX] OpenAI not configured. "
            yield "This is a simulated streaming response."
            return

        try:
            stream = await self._client.chat.completions.create(
                model=self._model_name,
                messages=[m.to_openai_format() for m in messages],
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                **kwargs,
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as exc:
            logger.error("OpenAI stream error: %s", exc)
            yield f"[ERROR] {exc}"

    async def embed(self, text: str) -> list[float]:
        """OpenAI Embedding API。"""
        await self._ensure_client()

        if not self._client:
            # 返回零向量 (SANDBOX 模式)
            return [0.0] * 1536

        try:
            response = await self._client.embeddings.create(
                model="text-embedding-ada-002",
                input=text,
            )
            return response.data[0].embedding
        except Exception as exc:
            logger.error("OpenAI embed error: %s", exc)
            return [0.0] * 1536


class AnthropicProvider(LLMProvider):
    """Anthropic Claude 系列模型提供商。"""

    def __init__(
        self,
        api_key: str,
        model_name: str = "claude-3-sonnet-20240229",
        timeout: int = 30,
    ):
        self._api_key = api_key
        self._model_name = model_name
        self._timeout = timeout
        self._client = None
        self._initialized = False

    async def _ensure_client(self) -> None:
        if not self._initialized:
            try:
                import anthropic

                self._client = anthropic.AsyncAnthropic(
                    api_key=self._api_key,
                    timeout=self._timeout,
                )
                self._initialized = True
                logger.info("Anthropic client initialized: model=%s", self._model_name)
            except ImportError:
                logger.warning("anthropic package not installed, using fallback mode")
                self._initialized = True
            except Exception as exc:
                logger.error("Failed to initialize Anthropic client: %s", exc)
                self._initialized = True

    async def chat(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> LLMResponse:
        await self._ensure_client()

        # 调用方可用 model= 覆盖；摘出避免与下方 model= 重复传参。
        model = kwargs.pop("model", None) or self._model_name

        if not self._client:
            return LLMResponse(
                content="[SANDBOX] Anthropic not configured.",
                finish_reason="stop",
                model=model,
            )

        import time

        start = time.time()
        try:
            system_msgs = [m for m in messages if m.role == "system"]
            chat_msgs = [m for m in messages if m.role != "system"]

            system_prompt = system_msgs[0].content if system_msgs else ""

            response = await self._client.messages.create(
                model=model,
                system=system_prompt,
                messages=[{"role": m.role, "content": m.content} for m in chat_msgs],
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
            latency = (time.time() - start) * 1000

            return LLMResponse(
                content=response.content[0].text,
                finish_reason=response.stop_reason or "end_turn",
                usage={
                    "prompt_tokens": response.usage.input_tokens,
                    "completion_tokens": response.usage.output_tokens,
                },
                model=response.model,
                latency_ms=latency,
            )
        except Exception as exc:
            logger.error("Anthropic chat error: %s", exc)
            return LLMResponse(
                content=f"[ERROR] {exc}", finish_reason="error", model=self._model_name
            )

    async def chat_stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        await self._ensure_client()

        if not self._client:
            yield "[SANDBOX] Anthropic not configured."
            return

        try:
            system_msgs = [m for m in messages if m.role == "system"]
            chat_msgs = [m for m in messages if m.role != "system"]
            system_prompt = system_msgs[0].content if system_msgs else ""

            async with self._client.messages.stream(
                model=self._model_name,
                system=system_prompt,
                messages=[{"role": m.role, "content": m.content} for m in chat_msgs],
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as exc:
            logger.error("Anthropic stream error: %s", exc)
            yield f"[ERROR] {exc}"

    async def embed(self, text: str) -> list[float]:
        # Anthropic 不提供 embedding API，使用 OpenAI 替代
        logger.warning("Anthropic does not support embeddings, returning zero vector")
        return [0.0] * 1536


class LocalLLMProvider(LLMProvider):
    """本地 LLM 提供商 (Ollama / vLLM / llama.cpp)。"""

    def __init__(
        self,
        model_name: str = "llama3",
        api_base: str = "http://localhost:11434",
        timeout: int = 60,
    ):
        self._model_name = model_name
        self._api_base = api_base
        self._timeout = timeout
        self._httpx_client = None

    async def _get_client(self):
        if not self._httpx_client:
            import httpx

            self._httpx_client = httpx.AsyncClient(timeout=self._timeout)
        return self._httpx_client

    async def chat(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> LLMResponse:
        """Ollama Chat API (兼容 OpenAI 格式)。"""
        try:
            client = await self._get_client()
            response = await client.post(
                f"{self._api_base}/v1/chat/completions",
                json={
                    "model": self._model_name,
                    "messages": [m.to_openai_format() for m in messages],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    **kwargs,
                },
            )
            data = response.json()
            choice = data["choices"][0]
            return LLMResponse(
                content=choice["message"]["content"],
                finish_reason=choice.get("finish_reason", "stop"),
                usage=data.get("usage", {}),
                model=self._model_name,
            )
        except Exception as exc:
            logger.error("Local LLM error: %s", exc)
            return LLMResponse(
                content=f"[ERROR] {exc}", finish_reason="error", model=self._model_name
            )

    async def chat_stream(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.7,
        max_tokens: int = 1000,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        try:
            client = await self._get_client()
            async with client.stream(
                "POST",
                f"{self._api_base}/v1/chat/completions",
                json={
                    "model": self._model_name,
                    "messages": [m.to_openai_format() for m in messages],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": True,
                    **kwargs,
                },
            ) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        import json

                        chunk = json.loads(line[6:])
                        if chunk.get("choices") and chunk["choices"][0].get(
                            "delta", {}
                        ).get("content"):
                            yield chunk["choices"][0]["delta"]["content"]
        except Exception as exc:
            logger.error("Local LLM stream error: %s", exc)
            yield f"[ERROR] {exc}"

    async def embed(self, text: str) -> list[float]:
        try:
            client = await self._get_client()
            response = await client.post(
                f"{self._api_base}/v1/embeddings",
                json={"model": self._model_name, "input": text},
            )
            data = response.json()
            return data["data"][0]["embedding"]
        except Exception as exc:
            logger.error("Local LLM embed error: %s", exc)
            return [0.0] * 768

    async def close(self) -> None:
        if self._httpx_client:
            await self._httpx_client.aclose()


def create_llm_provider(config: dict[str, Any]) -> LLMProvider:
    """工厂函数：根据配置创建 LLM 提供商。"""
    provider = config.get("provider", "openai")

    if provider == "openai":
        return OpenAIProvider(
            api_key=config.get("api_key", ""),
            model_name=config.get("model_name", "gpt-4"),
            api_base=config.get("api_base", "https://api.openai.com/v1"),
            timeout=config.get("timeout", 30),
        )
    elif provider == "anthropic":
        return AnthropicProvider(
            api_key=config.get("api_key", ""),
            model_name=config.get("model_name", "claude-3-sonnet-20240229"),
            timeout=config.get("timeout", 30),
        )
    elif provider == "local":
        return LocalLLMProvider(
            model_name=config.get("model_name", "llama3"),
            api_base=config.get("api_base", "http://localhost:11434"),
            timeout=config.get("timeout", 60),
        )
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")
