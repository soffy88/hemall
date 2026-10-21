"""app.ext.vlm_ollama_provider — 本地 Ollama MiniCPM-V 真视觉 provider。

宿主机 Ollama 部署 minicpm-v47-1b-local:latest (MiniCPM-V 4.7 1B, Q4_K_M,
vision)；api 容器经 http://172.17.0.1:11434 调原生 /api/chat (messages 带
images: [base64], stream=false)。三个能力:

  - identify_product(image_bytes): 单图识货 → 商品名/品类/描述/关键词/置信度
    (拍照上架用)
  - same_product(image_a, image_b): 双图比对 → 是否同一件商品 (以图更新用)
  - assess_damage(evidence_img, original_batch_video): 与 ManualVLMProvider
    同签名同返回形状的判损 (客诉证据图)

纪律: 模型不可达 / 输出不可解析 / 证据图读不到一律抛异常, 绝不伪造结论——
调用方负责降级 (ingest 降级文件名推导; 判损链路由上层感知失败)。CPU 推理
慢, httpx 超时默认 120s (Settings.vlm_ollama_timeout_s)。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("hemall.ext.vlm_ollama")

_IDENTIFY_PROMPT = (
    "你是商超商品识别助手。看图中这件商品, 只输出一个 JSON 对象, 不要输出任何"
    "其他文字, 不要用 markdown 代码块。格式: "
    '{"title": "简洁中文商品名(如 丹东草莓)", '
    '"category_hint": "品类(如 水果/蔬菜/生鲜/饮料/日用百货)", '
    '"description": "一句话描述(产地/规格/卖点, 没把握就只描述外观)", '
    '"keywords": ["关键词1", "关键词2"], '
    '"confidence": 0.0到1.0之间的置信度}。'
    "看不清或图中不是商品时 title 填空字符串、confidence 填低值。"
)

_SAME_PRODUCT_PROMPT = (
    "图1和图2是不是同一件商品 (同品名、同规格; 允许拍摄角度/光线/背景不同)? "
    "只输出一个 JSON 对象, 不要输出任何其他文字, 不要用 markdown 代码块。格式: "
    '{"same": true 或 false, "confidence": 0.0到1.0之间的置信度, '
    '"reason": "一句话理由"}。'
)

_DAMAGE_PROMPT = (
    "这是一张顾客投诉时提交的商品证据图。判断图中商品的损坏情况, 只输出一个 "
    "JSON 对象, 不要输出任何其他文字, 不要用 markdown 代码块。格式: "
    '{"damage_type": "损坏类型(完好无损填 none, 如 破损/变质/挤压变形/渗漏/异物)", '
    '"severity": 0.0到1.0之间的严重度(完好为 0), '
    '"fraud_probability": 0.0到1.0之间的证据造假嫌疑概率'
    "(翻拍屏幕/图片与实物明显不符等才给高值, 正常实拍给低值)}。"
)

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.MULTILINE)


def _parse_json_object(text: str) -> dict[str, Any]:
    """防御性解析模型输出为 JSON 对象: 剥代码围栏, 再退到首尾大括号截取。

    Raises:
        ValueError: 剥离/截取后仍不是合法 JSON 对象。
    """
    cleaned = _FENCE_RE.sub("", text or "").strip()
    candidates = [cleaned]
    if "{" in cleaned and "}" in cleaned:
        candidates.append(cleaned[cleaned.find("{") : cleaned.rfind("}") + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"VLM output is not a JSON object: {(text or '')[:200]!r}")


def _clamp01(value: Any, field: str) -> float:
    """把模型给的数值字段钳到 [0, 1]; 非数值直接判失败 (不猜)。"""
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"VLM field {field} is not numeric: {value!r}") from exc
    return min(1.0, max(0.0, num))


class OllamaVLMProvider:
    """经 Ollama /api/chat 调本地 MiniCPM-V 的真 VLM provider。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_s: float = 120.0,
        output_root: Path | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = float(timeout_s)
        self._output_root = Path(output_root) if output_root is not None else None

    # ── 底层调用 ──────────────────────────────────────────────────────

    async def _chat_json(self, prompt: str, images: list[bytes]) -> dict[str, Any]:
        payload = {
            "model": self._model,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [
                        base64.b64encode(img).decode("ascii") for img in images
                    ],
                }
            ],
            "options": {"temperature": 0.1},
        }
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(f"{self._base_url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = str((data.get("message") or {}).get("content") or "")
        return _parse_json_object(content)

    # ── 能力 ──────────────────────────────────────────────────────────

    async def identify_product(self, *, image_bytes: bytes) -> dict[str, Any]:
        """识别单张商品图 → {title, category_hint, description, keywords, confidence}。

        Raises:
            ValueError: 图为空、模型输出不可解析或识别不出商品名。
            httpx.HTTPError: 模型不可达/超时/HTTP 错误。
        """
        if not image_bytes:
            raise ValueError("identify_product: empty image_bytes")
        result = await self._chat_json(_IDENTIFY_PROMPT, [image_bytes])
        title = str(result.get("title") or "").strip()
        if not title:
            raise ValueError("VLM identify_product returned empty title")
        keywords_raw = result.get("keywords") or []
        if isinstance(keywords_raw, str):
            keywords = [
                k.strip() for k in re.split(r"[,，、]", keywords_raw) if k.strip()
            ]
        elif isinstance(keywords_raw, list):
            keywords = [str(k).strip() for k in keywords_raw if str(k).strip()]
        else:
            keywords = []
        return {
            "title": title,
            "category_hint": str(result.get("category_hint") or "").strip(),
            "description": str(result.get("description") or "").strip(),
            "keywords": keywords,
            "confidence": _clamp01(result.get("confidence", 0.0), "confidence"),
        }

    async def same_product(self, *, image_a: bytes, image_b: bytes) -> dict[str, Any]:
        """判断两张图是否同一件商品 → {same, confidence, reason}。"""
        if not image_a or not image_b:
            raise ValueError("same_product: empty image bytes")
        result = await self._chat_json(_SAME_PRODUCT_PROMPT, [image_a, image_b])
        same_raw = result.get("same")
        if isinstance(same_raw, str):
            same = same_raw.strip().lower() in {"true", "yes", "是"}
        else:
            same = bool(same_raw)
        return {
            "same": same,
            "confidence": _clamp01(result.get("confidence", 0.0), "confidence"),
            "reason": str(result.get("reason") or "").strip(),
        }

    async def assess_damage(
        self, *, evidence_img: str, original_batch_video: str
    ) -> dict[str, Any]:
        """判损: 与 ManualVLMProvider 同签名同返回形状。

        evidence_img 可为 http(s) URL、/media/... 媒体路径 (映射到
        output_root 下的实际文件) 或裸文件路径; 读不到图 / 模型失败抛异常,
        不伪造结论。original_batch_video 仅按契约回显 (视频帧比对不在本
        provider 范围内)。
        """
        image_bytes = await self._load_image_bytes(evidence_img)
        result = await self._chat_json(_DAMAGE_PROMPT, [image_bytes])
        damage_type = str(result.get("damage_type") or "").strip()
        if not damage_type:
            raise ValueError("VLM assess_damage returned empty damage_type")
        return {
            "evidence_img": evidence_img,
            "original_batch_video": original_batch_video,
            "damage_type": damage_type,
            "severity": _clamp01(result.get("severity", 0.0), "severity"),
            "fraud_probability": _clamp01(
                result.get("fraud_probability", 0.0), "fraud_probability"
            ),
        }

    # ── 证据图读取 ────────────────────────────────────────────────────

    async def _load_image_bytes(self, ref: str) -> bytes:
        if ref.startswith(("http://", "https://")):
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.get(ref)
                resp.raise_for_status()
                return resp.content
        path = self._resolve_local_path(ref)
        if path is None or not path.is_file():
            raise FileNotFoundError(
                f"assess_damage: evidence image not readable: {ref}"
            )
        return path.read_bytes()

    def _resolve_local_path(self, ref: str) -> Path | None:
        """把媒体 URL 映射到 output_root 下的文件 (防路径越界); 裸路径原样用。"""
        if ref.startswith("/media/agent_ingest/"):
            if self._output_root is None:
                return None
            base = (
                self._output_root
                / "ext_bespoke"
                / "agent_ingest"
                / "agent_ingest"
            )
            return self._contained(base, ref[len("/media/agent_ingest/") :])
        if ref.startswith("/media/"):
            if self._output_root is None:
                return None
            return self._contained(self._output_root, ref[len("/media/") :])
        return Path(ref)

    @staticmethod
    def _contained(base: Path, rel: str) -> Path | None:
        candidate = (base / rel).resolve()
        return candidate if candidate.is_relative_to(base.resolve()) else None
