"""注册点：加载已通过评估门禁的训练模型；未配 / 不通过时保持 dormant。

当前状态（照片未到，0 个模型文件）：本模块只提供接口，不被任何启动
流程调用——识别端点默认仍是 NullRecognizer（空候选 +
model_configured=false）。照片到了、训练通过门禁之后，注册分两步：

1. 模型文件放服务器固定位置，环境变量 COUNTER_MODEL_PATH 指向它；
2. 启动流程里调一次 register_trained_recognizer()（接线须另批，
   见 docs/counter-training.md）。

加载门禁（缺一不可，缺一个就返回 None，绝不降级加载）：

- COUNTER_MODEL_PATH 指向的模型文件存在且格式合法；
- 模型内嵌的评估摘要 gate_passed 为真；
- 摘要里的 top-1/top-3 数字重新核对过阈值（top-1 >= 90%，top-3 >= 98%），
  且测试集张数 > 0（不只信一个 flag）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.counter_training.features import extract_features_from_bytes
from app.counter_training.model import MODEL_FORMAT, CentroidModel
from app.counter_vision import MAX_CANDIDATES, Recognizer, set_recognizer

#: 模型文件路径的环境变量名（部署时注入，不进代码、不进聊天）。
MODEL_ENV_VAR = "COUNTER_MODEL_PATH"


class TrainedRecognizer:
    """已训模型的 Recognizer 包装：configured=True，返回前 3 候选。"""

    name = MODEL_FORMAT
    configured = True

    def __init__(self, model: CentroidModel) -> None:
        self._model = model

    async def recognize(self, image_bytes: bytes, weight_grams: int) -> list[dict[str, Any]]:
        """识别一帧：特征提取失败时返回空候选（不抛错、不阻塞柜台）。"""
        try:
            feature = extract_features_from_bytes(image_bytes)
        except Exception:  # noqa: BLE001 - 图片非法时一律空候选，调用方退回点选
            return []
        return self._model.predict_topk(feature, k=MAX_CANDIDATES)


def load_registered_recognizer(model_path: str | Path | None = None) -> Recognizer | None:
    """加载已通过门禁的模型；未配路径 / 文件非法 / 门禁不通过返回 None。

    显式传 model_path 主要供测试用；生产用环境变量 COUNTER_MODEL_PATH。
    """
    raw = model_path if model_path is not None else os.environ.get(MODEL_ENV_VAR)
    if not raw:
        return None
    try:
        model = CentroidModel.load(Path(raw))
    except (OSError, ValueError):
        return None
    if not model.eval_passed():
        return None
    return TrainedRecognizer(model)


def register_trained_recognizer(model_path: str | Path | None = None) -> bool:
    """加载并设为当前识别器；成功返回 True，未配 / 不通过返回 False。

    返回 False 时当前识别器保持不变（默认 NullRecognizer），绝不出现
    “半个模型”状态。启动接线时调这一个函数即可。
    """
    recognizer = load_registered_recognizer(model_path)
    if recognizer is None:
        return False
    set_recognizer(recognizer)
    return True
