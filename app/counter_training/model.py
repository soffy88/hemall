"""最近质心小分类器：训练 = 每类求特征均值；预测 = 按质心距离排序。

选型（见 docs/counter-training.md）：CPU、零新依赖、确定性、可读 JSON。
分数是距离 softmax 的排序置信度（temperature=0.1），不是校准概率；
只用于前 3 候选排序，绝不声称是校准概率。

模型文件是 JSON（不用 pickle，避免任意代码执行）：格式、标签、质心、
训练张数、评估摘要都在文件里；注册时只认评估摘要通过且数字重新核对
过阈值的模型。
"""

from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.counter_training.evaluate import TOP1_MIN, TOP3_MIN
from app.counter_training.features import FEATURE_DIM

#: 模型文件格式标识（改特征或改分类器时必须升版，旧文件直接拒载）。
MODEL_FORMAT = "counter-centroid-v1"

DEFAULT_TEMPERATURE = 0.1


@dataclass
class CentroidModel:
    """最近质心模型：labels 与 centroids 一一对应，skus 供报告展示。"""

    labels: list[str]
    skus: dict[str, str]
    centroids: list[tuple[float, ...]]
    temperature: float = DEFAULT_TEMPERATURE
    train_counts: dict[str, int] = field(default_factory=dict)
    eval_summary: dict[str, Any] | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if len(self.labels) != len(self.centroids):
            raise ValueError("labels 与 centroids 数量不一致")
        for centroid in self.centroids:
            if len(centroid) != FEATURE_DIM:
                raise ValueError(f"质心维度须为 {FEATURE_DIM}")

    @classmethod
    def fit(
        cls,
        labeled: list[tuple[str, str, tuple[float, ...]]],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> CentroidModel:
        """训练：labeled 是 (variant_id, sku, 特征) 的列表，质心 = 类内均值。"""
        by_label: dict[str, list[tuple[float, ...]]] = {}
        skus: dict[str, str] = {}
        counts: dict[str, int] = {}
        for variant_id, sku, feature in labeled:
            by_label.setdefault(variant_id, []).append(feature)
            skus[variant_id] = sku
            counts[variant_id] = counts.get(variant_id, 0) + 1
        labels = sorted(by_label)
        centroids = []
        for label in labels:
            rows = by_label[label]
            count = len(rows)
            centroids.append(
                tuple(sum(row[dim] for row in rows) / count for dim in range(FEATURE_DIM))
            )
        return cls(
            labels=labels,
            skus=skus,
            centroids=centroids,
            temperature=temperature,
            train_counts=counts,
            created_at=datetime.now(UTC).isoformat(),
        )

    def predict_topk(self, feature: tuple[float, ...], *, k: int = 3) -> list[dict[str, Any]]:
        """返回前 k 候选 [{variant_id, score}]（score 在 0..1 内，按分数降序）。"""
        if len(feature) != FEATURE_DIM:
            raise ValueError(f"特征维度须为 {FEATURE_DIM}")
        distances = [
            math.sqrt(sum((a - b) ** 2 for a, b in zip(feature, centroid, strict=True)))
            for centroid in self.centroids
        ]
        order = sorted(range(len(self.labels)), key=lambda index: distances[index])
        weights = [math.exp(-distance / self.temperature) for distance in distances]
        total = sum(weights)
        return [
            {"variant_id": self.labels[index], "score": weights[index] / total}
            for index in order[: max(k, 0)]
        ]

    def eval_passed(self) -> bool:
        """注册门禁复核：摘要须标通过，且数字重新核对过阈值（不只信一个 flag）。"""
        if not self.eval_summary:
            return False
        if self.eval_summary.get("gate_passed") is not True:
            return False
        top1 = self.eval_summary.get("top1")
        top3 = self.eval_summary.get("top3")
        test_n = self.eval_summary.get("test_n")
        return (
            isinstance(top1, (int, float))
            and isinstance(top3, (int, float))
            and isinstance(test_n, int)
            and test_n > 0
            and top1 >= TOP1_MIN
            and top3 >= TOP3_MIN
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": MODEL_FORMAT,
            "created_at": self.created_at,
            "feature": {"kind": "gray8x8+rgb-hist8", "dim": FEATURE_DIM},
            "temperature": self.temperature,
            "labels": list(self.labels),
            "skus": dict(self.skus),
            "centroids": [list(centroid) for centroid in self.centroids],
            "train_counts": dict(self.train_counts),
            "eval": self.eval_summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CentroidModel:
        """从 JSON 字典恢复模型；格式 / 维度 / 数值非法一律抛 ValueError。"""
        if data.get("format") != MODEL_FORMAT:
            raise ValueError(f"模型格式不受支持：{data.get('format')!r}")
        feature = data.get("feature")
        if not isinstance(feature, dict) or feature.get("dim") != FEATURE_DIM:
            raise ValueError("模型特征维度不受支持")
        labels = data.get("labels")
        centroids_raw = data.get("centroids")
        if (
            not isinstance(labels, list)
            or not labels
            or not all(isinstance(x, str) for x in labels)
        ):
            raise ValueError("模型 labels 非法")
        if not isinstance(centroids_raw, list) or len(centroids_raw) != len(labels):
            raise ValueError("模型 centroids 与 labels 数量不一致")
        centroids: list[tuple[float, ...]] = []
        for raw in centroids_raw:
            if (
                not isinstance(raw, list)
                or len(raw) != FEATURE_DIM
                or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in raw)
            ):
                raise ValueError("模型 centroid 非法")
            centroids.append(tuple(float(v) for v in raw))
        skus = data.get("skus")
        if not isinstance(skus, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in skus.items()
        ):
            raise ValueError("模型 skus 非法")
        temperature = data.get("temperature", DEFAULT_TEMPERATURE)
        if not isinstance(temperature, (int, float)) or temperature <= 0:
            raise ValueError("模型 temperature 非法")
        eval_summary = data.get("eval")
        if eval_summary is not None and not isinstance(eval_summary, dict):
            raise ValueError("模型 eval 摘要非法")
        train_counts = data.get("train_counts", {})
        if not isinstance(train_counts, dict):
            raise ValueError("模型 train_counts 非法")
        created_at = data.get("created_at", "")
        return cls(
            labels=list(labels),
            skus=dict(skus),
            centroids=centroids,
            temperature=float(temperature),
            train_counts={str(k): int(v) for k, v in train_counts.items()},
            eval_summary=eval_summary,
            created_at=created_at if isinstance(created_at, str) else "",
        )

    def save(self, path: Path) -> Path:
        """写模型文件（tmp + os.replace，避免半截写入）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}.json")
        tmp_path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp_path, path)
        return path

    @classmethod
    def load(cls, path: Path) -> CentroidModel:
        """读模型文件；不存在 / 解析失败 / 格式非法抛 OSError / ValueError。"""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ValueError(f"模型文件不可读：{path}（{exc}）") from exc
        if not isinstance(data, dict):
            raise ValueError(f"模型文件不是 JSON 对象：{path}")
        return cls.from_dict(data)
