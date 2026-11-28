"""特征提取：88 维（8x8 灰度 + 每通道 8 档直方图），只用 Pillow + 标准库。

选型说明（见 docs/counter-training.md）：柜台散货第一版用颜色 + 轮廓
灰度就够做基线；Pillow 已是项目依赖（上传口也用它），零新依赖、
CPU 秒级、确定性（同一张图永远同一组特征，断点续跑的缓存才可信）。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

#: 特征总维数：8x8 灰度（64）+ 3 通道 x 8 档直方图（24）。
FEATURE_DIM = 88

#: 特征提取算法版本：算法一变就 +1；训练缓存认这个版本，旧缓存自动作废。
#: 只看维数不够——同维数改算法，旧向量悄悄复用等于用错特征训练。
FEATURES_VERSION = 1

GRAY_SIZE = 8
HIST_BINS = 8
HIST_SIZE = 64


def features_from_image(image: Image.Image) -> tuple[float, ...]:
    """从已打开的图片提 88 维特征（灰度值归一化到 0..1，直方图按像素数归一化）。"""
    rgb = image.convert("RGB")
    small = rgb.resize((GRAY_SIZE, GRAY_SIZE), Image.Resampling.LANCZOS)
    gray = [value / 255.0 for value in small.convert("L").tobytes()]
    hist_source = rgb.resize((HIST_SIZE, HIST_SIZE), Image.Resampling.LANCZOS)
    raw = hist_source.tobytes()
    total = float(HIST_SIZE * HIST_SIZE)
    hist: list[float] = []
    for channel in range(3):
        bins = [0] * HIST_BINS
        for index in range(channel, len(raw), 3):
            bins[min(raw[index] * HIST_BINS // 256, HIST_BINS - 1)] += 1
        hist.extend(count / total for count in bins)
    return tuple(gray + hist)


def extract_features_from_bytes(data: bytes) -> tuple[float, ...]:
    """从图片字节提特征（识别端点在线推理用同一条路，保证训练 / 推理一致）。"""
    with Image.open(BytesIO(data)) as image:
        image.load()
        return features_from_image(image)


def extract_features(path: Path) -> tuple[float, ...]:
    """从图片文件提特征。"""
    return extract_features_from_bytes(path.read_bytes())
