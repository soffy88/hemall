"""app.ext.vlm_provider — 内存态多模态视觉 provider。

obase 没有"多模态视觉大模型"这个 provider 类别 (跟 weather/payout 一样，是
hemall 扩展域特有的外部依赖)。真实实现应该接 GPT-4V/Gemini Pro Vision 之类的
API 去比对客诉证据图与原产地批次视频；这个开发环境里没有现成的凭据/网关，
风格对齐 ManualWeatherProvider：进程内内存态，默认判定"完好无损、无造假嫌疑"，
可用 set_assessment 按证据图 URL 覆写指定判定结果，供测试/演练模拟真实损坏
场景。真实模型接入时只要注册一个新的 "vlm" provider 实现替换掉它，
oprim.vlm_assess_damage / oskill / omodul 都不用改。
"""

from __future__ import annotations

from typing import Any


class ManualVLMProvider:
    """内存态多模态视觉 provider：默认"完好无损"，可用 set_assessment 覆写。"""

    def __init__(self) -> None:
        self._overrides: dict[str, dict[str, Any]] = {}

    def set_assessment(
        self,
        *,
        evidence_img: str,
        damage_type: str,
        severity: float,
        fraud_probability: float,
    ) -> None:
        """覆写指定证据图的判定结果 (测试/演练用；真实 provider 不需要这个方法)。"""
        self._overrides[evidence_img] = {
            "damage_type": damage_type,
            "severity": severity,
            "fraud_probability": fraud_probability,
        }

    async def assess_damage(
        self, *, evidence_img: str, original_batch_video: str
    ) -> dict[str, Any]:
        """比对证据图与原产地批次视频，返回损坏类型/严重度/造假概率。"""
        override = self._overrides.get(evidence_img)
        result = override or {
            "damage_type": "none",
            "severity": 0.0,
            "fraud_probability": 0.0,
        }
        return {
            "evidence_img": evidence_img,
            "original_batch_video": original_batch_video,
            **result,
        }
