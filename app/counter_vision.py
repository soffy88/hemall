"""app.counter_vision — 柜台视觉识别接线：纯函数 + 幂等 DDL + 可插拔识别器。

依据：设计稿《收银台电子称 + 系统自动识别》（§5 接入点）。
不 import 任何 app 内模块（同 app/weigh.py 的先例），避免循环 import。

状态：模型未训。默认识别器是 NullRecognizer：返回空候选并记日志，
绝不编分数、绝不瞎猜；调用方收到空候选即退回拍照点选流程。
真正的模型以后以 Recognizer 实现接入（set_recognizer），本模块不含
任何权重、推理代码与第三方模型依赖。

铁律：
- 候选必须来自识别器原始输出的校验（variant_id 为 UUID、score 在 0..1 内），
  脏数据直接丢弃，绝不写进评估账本。
- 识别超时（3 秒）或识别器抛错 → 空候选，不阻塞柜台、不谎报结果。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

#: 单帧/单张照片最大 10MB（手机实拍直传也够，超限 413 拒收）。
MAX_IMAGE_BYTES = 10 * 1024 * 1024

#: 识别超时：超过即返回空候选，柜台退回点选（设计稿 §5）。
RECOGNIZE_TIMEOUT_SECONDS = 3.0

#: 单次识别最多返回的候选数（设计稿：前 3 候选）。
MAX_CANDIDATES = 3

#: 帧/照片只接受这三种格式（按文件魔数判定，不信任客户端 content-type）。
SUPPORTED_IMAGE_FORMATS = ("jpeg", "png", "webp")

COUNTER_VISION_DDL = """
-- ── 柜台视觉识别接线：帧存档 + 识别评估账本（设计稿 §5）──
CREATE TABLE IF NOT EXISTS counter_frame (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    frame_path TEXT NOT NULL,
    content_type TEXT NOT NULL,
    byte_size INT NOT NULL CHECK (byte_size > 0),
    sha256 TEXT NOT NULL,
    camera_id TEXT,
    captured_at TIMESTAMPTZ,
    weigh_message_id TEXT,
    ticket_id UUID,
    client_frame_id TEXT,
    source VARCHAR(20) NOT NULL DEFAULT 'camera'
        CHECK (source IN ('camera', 'recognize')),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_counter_frame_client_id
    ON counter_frame(client_frame_id) WHERE client_frame_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_counter_frame_ticket
    ON counter_frame(ticket_id, created_at);

CREATE TABLE IF NOT EXISTS counter_recognition_event (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    frame_id UUID NOT NULL REFERENCES counter_frame(id) ON DELETE CASCADE,
    weight_grams INT CHECK (weight_grams IS NULL OR weight_grams > 0),
    candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
    model_name TEXT,
    model_configured BOOLEAN NOT NULL DEFAULT FALSE,
    created_by TEXT NOT NULL,
    confirmed_variant_id UUID,
    confirmed_none BOOLEAN NOT NULL DEFAULT FALSE,
    confirmed_at TIMESTAMPTZ,
    confirmed_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_counter_recognition_frame
    ON counter_recognition_event(frame_id, created_at);
CREATE INDEX IF NOT EXISTS idx_counter_recognition_created
    ON counter_recognition_event(created_at);
"""


# ── 图像格式判定（魔数，不信任客户端声明）─────────────────────────────────


def detect_image_format(data: bytes) -> tuple[str, str] | None:
    """返回 (format, extension)，未知格式返回 None。

    只要 JPEG / PNG / WebP：通用摄像头与手机实拍的输出；GIF/动图不是
    柜台帧，直接拒收，避免识别器吃到非预期输入。
    """
    if data.startswith(b"\xff\xd8\xff"):
        return ("jpeg", "jpg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ("png", "png")
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("webp", "webp")
    return None


def validate_image_bytes(data: bytes, *, max_bytes: int = MAX_IMAGE_BYTES) -> tuple[str, str]:
    """校验一帧/一张照片的字节：大小 + 魔数。非法抛 ValueError（调用方转 400）。"""
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise ValueError("image is empty")
    if len(data) > max_bytes:
        raise ValueError(f"image too large: {len(data)} bytes > {max_bytes}")
    detected = detect_image_format(bytes(data))
    if detected is None:
        raise ValueError("unsupported image format: only jpeg/png/webp are accepted")
    return detected


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── SKU 与文件名 ───────────────────────────────────────────────────────────


_SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def sanitize_sku(value: object) -> str:
    """数据集 SKU 目录名：只允字母数字 + _ . -，首字符须为字母数字。

    拒绝一切路径穿越（/、\\、.. 都不在字符集内）。非法抛 ValueError。
    """
    if not isinstance(value, str):
        raise ValueError("sku must be a string")
    sku = value.strip()
    if not _SKU_RE.match(sku):
        raise ValueError("invalid sku: use 1-64 chars of letters, digits, _ . -")
    return sku


def _timestamp_prefix(when: datetime | None = None) -> str:
    moment = when or datetime.now(UTC)
    return moment.strftime("%Y%m%dT%H%M%S")


def frame_filename(frame_id: str, ext: str, *, when: datetime | None = None) -> str:
    return f"{_timestamp_prefix(when)}_{frame_id[:8]}.{ext}"


def dataset_filename(entry_uuid: str, ext: str, *, when: datetime | None = None) -> str:
    return f"{_timestamp_prefix(when)}_{entry_uuid[:8]}.{ext}"


# ── 识别器：可插拔接口 + 默认空实现 ───────────────────────────────────────


class Recognizer(Protocol):
    """识别器契约：输入一帧图 + 当前重量克数，输出原始候选列表。

    原始输出由 validate_candidates 校验后才进账本；识别器本身可以
    返回脏数据，账本层负责丢弃。name 供账本留痕，configured 标记
    是否为真实模型（False = 未配置，调用方须按“无识别”处理）。
    """

    name: str
    configured: bool

    async def recognize(
        self, image_bytes: bytes, weight_grams: int
    ) -> Iterable[dict[str, Any]]: ...


class NullRecognizer:
    """默认识别器：模型未训，永远返回空候选。绝不编分数、绝不瞎猜。"""

    name = "null"
    configured = False

    async def recognize(self, image_bytes: bytes, weight_grams: int) -> list[dict[str, Any]]:
        return []


_RECOGNIZER: Recognizer = NullRecognizer()


def get_recognizer() -> Recognizer:
    return _RECOGNIZER


def set_recognizer(recognizer: Recognizer | None) -> None:
    """接入真实模型时调用；传 None 恢复默认空实现（测试与回退用）。"""
    global _RECOGNIZER
    _RECOGNIZER = recognizer if recognizer is not None else NullRecognizer()


def validate_candidates(raw: object, *, limit: int = MAX_CANDIDATES) -> list[dict[str, Any]]:
    """校验识别器原始输出，返回干净的候选列表（按分数降序、截断到 limit）。

    规则：variant_id 须能解析为 UUID；score 须为 0..1 的数；同 variant
    只留最高分。任何非法项直接丢弃——脏数据绝不写进评估账本。
    """
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, dict)):
        return []
    best: dict[str, float] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        variant_id = item.get("variant_id")
        score = item.get("score")
        if not isinstance(variant_id, str):
            continue
        try:
            uuid.UUID(variant_id)
        except ValueError:
            continue
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        score_f = float(score)
        if score_f < 0.0 or score_f > 1.0 or score_f != score_f:  # NaN 也拒收
            continue
        if variant_id not in best or score_f > best[variant_id]:
            best[variant_id] = score_f
    ordered = sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{"variant_id": vid, "score": score} for vid, score in ordered]


async def run_recognition(
    recognizer: Recognizer,
    image_bytes: bytes,
    weight_grams: int,
    *,
    timeout_seconds: float = RECOGNIZE_TIMEOUT_SECONDS,
) -> tuple[list[dict[str, Any]], bool]:
    """跑一次识别，返回 (candidates, model_configured)。

    超时或识别器抛错 → 空候选（调用方退回点选）；model_configured
    如实反映识别器是否真实配置，未配置时调用方须按“无识别”处理。
    """
    configured = bool(getattr(recognizer, "configured", True))
    try:
        raw = await asyncio.wait_for(
            recognizer.recognize(image_bytes, weight_grams), timeout=timeout_seconds
        )
    except TimeoutError:
        return [], configured
    except Exception:  # noqa: BLE001 - 识别器任何异常都转空候选，不阻塞柜台
        return [], configured
    return validate_candidates(raw), configured


# ── 数据集 manifest ────────────────────────────────────────────────────────


def build_manifest_entry(
    *,
    filename: str,
    sku: str,
    variant_id: str | None,
    captured_at: str | None,
    photographer: str,
    note: str,
    uploaded_by: str,
    sha256: str,
    byte_size: int,
) -> dict[str, Any]:
    """一条 manifest 记录：文件、SKU、拍摄时间、拍摄人、备注，全部留痕。"""
    return {
        "file": filename,
        "sku": sku,
        "variant_id": variant_id,
        "captured_at": captured_at,
        "photographer": photographer,
        "note": note,
        "uploaded_by": uploaded_by,
        "uploaded_at": datetime.now(UTC).isoformat(),
        "sha256": sha256,
        "byte_size": byte_size,
    }


def append_manifest_entry(manifest_path: Path, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """把一条记录追加进某 SKU 的 manifest.json（不存在则新建），返回追加后全量。

    写盘走 tmp + os.replace，避免半截写入；manifest 损坏则备份后重建
    （目录下的文件本身永不重名，目录清单永远是权威索引，manifest 只是
    加速用的索引——丢了也能靠扫目录重建）。
    """
    entries: list[dict[str, Any]] = []
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                entries = [e for e in loaded if isinstance(e, dict)]
        except (ValueError, OSError):
            backup = manifest_path.with_suffix(f".corrupt.{_timestamp_prefix()}.json")
            try:
                os.replace(manifest_path, backup)
            except OSError:
                pass
            entries = []
    entries.append(entry)
    tmp_path = manifest_path.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}.json")
    tmp_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, manifest_path)
    return entries
