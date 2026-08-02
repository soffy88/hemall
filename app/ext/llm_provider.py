"""app.ext.llm_provider — 内存态大模型文本生成 provider。

obase 没有"大模型推理"这个 provider 类别 (跟 weather/vlm/cv 一样，是 hemall
域特有的外部依赖)。真实实现应该接 GPT-4o/Claude 之类的 API 生成播报文案；
这个开发环境里没有现成的 API key，风格对齐 ManualVLMProvider：进程内内存态，
默认生成一段基于 user_prompt 拼出来的确定性占位文案 (不是随机/不可复现的)，
可用 set_response 覆写指定 prompt 组合的返回结果，供测试/演练模拟真实文案。
真实模型接入时只要注册一个新的 "llm" provider 实现替换掉它，
oprim.ext_llm_generate_text 及以上都不用改。
"""

from __future__ import annotations


class ManualLLMProvider:
    """内存态大模型 provider：默认拼一段确定性占位文案，可用 set_response 覆写。"""

    def __init__(self) -> None:
        self._overrides: dict[tuple[str, str], str] = {}

    def set_response(self, *, system_prompt: str, user_prompt: str, text: str) -> None:
        """覆写指定 (system_prompt, user_prompt) 组合的生成结果 (测试/演练用)。"""
        self._overrides[(system_prompt, user_prompt)] = text

    async def generate_text(self, *, system_prompt: str, user_prompt: str) -> str:
        """基于系统指令与用户上下文生成一段文本。"""
        override = self._overrides.get((system_prompt, user_prompt))
        if override is not None:
            return override
        first_line = user_prompt.splitlines()[0] if user_prompt else ""
        return f"[MOCK COPY] {first_line} 手慢无，hemall 底价，先到先得。"
