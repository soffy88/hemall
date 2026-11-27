"""app.counter_training — 柜台识别训练 harness（CPU 路线，离线跑）。

状态：照片未到（0/400），本模块只提供架子 + 评估门禁，不含任何真模型、
真权重、真照片。训练数据到了之后才开训；门禁不通过的模型绝不注册。

组成：

- dataset：读 counter_dataset/<sku>/ + manifest.json，按 note=test 切测试集。
- features：88 维颜色 / 灰度特征（只用 Pillow + 标准库，零新依赖）。
- model：最近质心小分类器，存 JSON（可读、无 pickle）。
- evaluate：留出测试集算 top-1/top-3，门禁 top-1 >= 90% 且 top-3 >= 98%。
- train：命令行入口（训练 + 评估 + 写报告）。
- registry：按环境变量加载已通过门禁的模型；未配时保持 dormant。

设计与选型理由见 docs/counter-training.md。
"""

from __future__ import annotations

from app.counter_training.dataset import Dataset, NoTrainingDataError, PhotoEntry, load_dataset
from app.counter_training.evaluate import TOP1_MIN, TOP3_MIN, EvalReport, evaluate
from app.counter_training.model import MODEL_FORMAT, CentroidModel

__all__ = [
    "MODEL_FORMAT",
    "TOP1_MIN",
    "TOP3_MIN",
    "CentroidModel",
    "Dataset",
    "EvalReport",
    "NoTrainingDataError",
    "PhotoEntry",
    "evaluate",
    "load_dataset",
]
