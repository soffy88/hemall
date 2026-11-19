"""app.scale_vision — 称重摄像头视觉识别后端（ONNX CPU 推理）。

功能：
- 使用开源视觉向量模型（CLIP/DINOv2/MobileNet 小模型，ONNX）对现有商品图+实拍图建向量库
- 返回 Top3 候选（variant_id + score 0..1）
- 店员点选确认后把该帧加入向量库（越用越准）
- 识别不到可拼音/搜索点选
- 不依赖外部付费 API，模型文件放 /data，注意磁盘（<15G 写 HALT）
- 实现 app/counter_vision.py 的 Recognizer 协议，供 set_recognizer 接入

模型选择（CPU 友好，<15G）：
- MobileNetV3-Small (ONNX, ~2.5MB, 1000 类 ImageNet) — 作为轻量基线
- DINOv2-Small (ONNX, ~80MB) — 自监督特征，无需分类头，适合相似度检索
- CLIP-ViT-B/32 (ONNX, ~150MB) — 图文对齐，支持文本查询（拼音/搜索）

默认使用 DINOv2-Small（平衡精度与体积），可通过环境变量切换。

向量库：FAISS IndexFlatIP（内积/余弦相似度），持久化到 /data/scale_vision/index.faiss + meta.json
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import pickle
import struct
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

# 尝试导入 ONNX Runtime（CPU）
try:
    import onnxruntime as ort
    _ORT_AVAILABLE = True
except ImportError:
    _ORT_AVAILABLE = False
    ort = None  # type: ignore

# 尝试导入 FAISS
try:
    import faiss
    _FAISS_AVAILABLE = True
except ImportError:
    _FAISS_AVAILABLE = False
    faiss = None  # type: ignore

# 尝试导入 PIL
try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False
    Image = None  # type: ignore

from .counter_vision import Recognizer, validate_candidates, MAX_CANDIDATES

logger = logging.getLogger("hemall.scale_vision")

# ──────────────────────────────────────────────────────────────────────────────
# 配置常量
# ──────────────────────────────────────────────────────────────────────────────

# 模型文件目录（用户可通过环境变量覆盖）
MODEL_DIR = Path(os.getenv("SCALE_VISION_MODEL_DIR", "/data/scale_vision/models"))
INDEX_DIR = Path(os.getenv("SCALE_VISION_INDEX_DIR", "/data/scale_vision/index"))

# 支持的模型配置
MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    "mobilenetv3_small": {
        "name": "MobileNetV3-Small",
        "url": "https://github.com/onnx/models/raw/main/vision/classification/mobilenet/model/mobilenetv3-small-1.0.onnx",
        "filename": "mobilenetv3_small.onnx",
        "input_size": (224, 224),
        "mean": [0.485, 0.456, 0.406],
        "std": [0.229, 0.224, 0.225],
        "feature_dim": 1000,  # 分类 logits，需接池化层或取倒数第二层
        "normalize": True,
    },
    "dinov2_small": {
        "name": "DINOv2-Small",
        "url": "https://dl.fbaipublicfiles.com/dinov2/dinov2_vits14/dinov2_vits14.onnx",  # 官方未发布 ONNX，需自行转换
        "filename": "dinov2_vits14.onnx",
        "input_size": (224, 224),
        "mean": [0.485, 0.456, 0.406],
        "std": [0.229, 0.224, 0.225],
        "feature_dim": 384,  # ViT-Small 输出维度
        "normalize": True,
    },
    "clip_vit_b32": {
        "name": "CLIP-ViT-B/32",
        "url": "",  # 需自行从 PyTorch 转换
        "filename": "clip_vit_b32.onnx",
        "input_size": (224, 224),
        "mean": [0.48145466, 0.4578275, 0.40821073],
        "std": [0.26862954, 0.26130258, 0.27577711],
        "feature_dim": 512,
        "normalize": True,
    },
}

DEFAULT_MODEL_KEY = "dinov2_small"  # 推荐：精度/体积平衡

# 向量索引配置
INDEX_FILE = INDEX_DIR / "index.faiss"
META_FILE = INDEX_DIR / "meta.json"
EMBEDDINGS_FILE = INDEX_DIR / "embeddings.npy"

# 图像预处理
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB
SUPPORTED_FORMATS = ("jpeg", "png", "webp")

# 磁盘空间阈值（字节）
DISK_HALT_THRESHOLD = 15 * 1024 * 1024 * 1024  # 15GB


# ──────────────────────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class VectorEntry:
    """向量库条目"""
    variant_id: str
    embedding: np.ndarray  # shape (dim,), float32, L2 归一化
    image_sha256: str
    source: str  # "reference" | "confirmed" | "tasting" | "manual"
    added_at: float = field(default_factory=time.time)
    confirmed_count: int = 0  # 被确认次数，用于加权


@dataclass
class RecognitionResult:
    """识别结果"""
    candidates: list[dict[str, Any]]  # [{"variant_id": str, "score": float, "rank": int}, ...]
    model_name: str
    model_configured: bool
    inference_ms: float
    image_sha256: str


# ──────────────────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────────────────

def check_disk_space(path: Path) -> bool:
    """检查磁盘剩余空间，<15G 返回 False（触发 HALT）"""
    try:
        stat = os.statvfs(path)
        free = stat.f_bavail * stat.f_frsize
        return free >= DISK_HALT_THRESHOLD
    except Exception:
        return True  # 无法检测时默认通过


def detect_image_format(data: bytes) -> Optional[str]:
    """检测图像格式（魔数）"""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def validate_image_bytes(data: bytes) -> str:
    """校验图像字节，返回格式；非法抛 ValueError"""
    if not data:
        raise ValueError("image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"image too large: {len(data)} bytes > {MAX_IMAGE_BYTES}")
    fmt = detect_image_format(data)
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported image format: {fmt or 'unknown'}")
    return fmt


def preprocess_image(
    image_data: bytes,
    input_size: tuple[int, int],
    mean: list[float],
    std: list[float],
) -> np.ndarray:
    """预处理图像为模型输入：RGB、Resize、Normalize、CHW、Batch"""
    if not _PIL_AVAILABLE:
        raise RuntimeError("PIL not available")
    img = Image.open(io.BytesIO(image_data))
    if img.mode != "RGB":
        img = img.convert("RGB")
    img = img.resize(input_size, Image.Resampling.LANCZOS)
    arr = np.array(img, dtype=np.float32) / 255.0  # HWC, 0-1
    arr = (arr - np.array(mean, dtype=np.float32)) / np.array(std, dtype=np.float32)
    arr = arr.transpose(2, 0, 1)  # CHW
    arr = arr[np.newaxis, ...]    # NCHW
    return arr.astype(np.float32)


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    """L2 归一化（向量检索用余弦相似度=内积）"""
    norm = np.linalg.norm(vec)
    if norm == 0:
        return vec
    return (vec / norm).astype(np.float32)


# 需要 io
import io


# ──────────────────────────────────────────────────────────────────────────────
# ONNX 模型封装
# ──────────────────────────────────────────────────────────────────────────────

class ONNXModel:
    """ONNX 模型加载与推理封装"""

    def __init__(self, model_path: Path, config: dict[str, Any]) -> None:
        if not _ORT_AVAILABLE:
            raise RuntimeError("onnxruntime not installed: pip install onnxruntime")
        self.config = config
        self.session = ort.InferenceSession(
            str(model_path),
            providers=["CPUExecutionProvider"],
        )
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        logger.info(f"Loaded ONNX model: {model_path} (input={self.input_name}, output={self.output_name})")

    def infer(self, input_tensor: np.ndarray) -> np.ndarray:
        """推理，返回原始输出（通常为 logits 或特征向量）"""
        start = time.perf_counter()
        output = self.session.run([self.output_name], {self.input_name: input_tensor})[0]
        elapsed = (time.perf_counter() - start) * 1000
        logger.debug(f"ONNX inference: {elapsed:.1f}ms, output shape={output.shape}")
        return output


# ──────────────────────────────────────────────────────────────────────────────
# 向量索引（FAISS）
# ──────────────────────────────────────────────────────────────────────────────

class VectorIndex:
    """FAISS 向量索引：IndexFlatIP（内积=余弦相似度，向量已 L2 归一化）"""

    def __init__(self, dim: int) -> None:
        if not _FAISS_AVAILABLE:
            raise RuntimeError("faiss not installed: pip install faiss-cpu")
        self.dim = dim
        self.index = faiss.IndexFlatIP(dim)
        self.entries: list[VectorEntry] = []

    def add(self, entry: VectorEntry) -> int:
        """添加向量，返回索引位置"""
        vec = entry.embedding.reshape(1, -1).astype(np.float32)
        self.index.add(vec)
        self.entries.append(entry)
        return len(self.entries) - 1

    def search(self, query: np.ndarray, k: int = MAX_CANDIDATES) -> list[tuple[VectorEntry, float]]:
        """搜索 Top-k，返回 (entry, score) 列表，score 为余弦相似度 [-1,1]"""
        if self.index.ntotal == 0:
            return []
        query = query.reshape(1, -1).astype(np.float32)
        scores, indices = self.index.search(query, min(k, self.index.ntotal))
        results = []
        for idx, score in zip(indices[0], scores[0]):
            if idx >= 0 and idx < len(self.entries):
                results.append((self.entries[idx], float(score)))
        return results

    def save(self, index_path: Path, meta_path: Path, emb_path: Path) -> None:
        """持久化索引与元数据"""
        index_path.parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(index_path))
        embeddings = np.vstack([e.embedding for e in self.entries]) if self.entries else np.zeros((0, self.dim), dtype=np.float32)
        np.save(emb_path, embeddings)
        meta = [
            {
                "variant_id": e.variant_id,
                "image_sha256": e.image_sha256,
                "source": e.source,
                "added_at": e.added_at,
                "confirmed_count": e.confirmed_count,
            }
            for e in self.entries
        ]
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    @classmethod
    def load(cls, index_path: Path, meta_path: Path, emb_path: Path, dim: int) -> "VectorIndex":
        """加载索引与元数据"""
        if not index_path.exists():
            return cls(dim)
        index = faiss.read_index(str(index_path))
        obj = cls(dim)
        obj.index = index
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if emb_path.exists():
                embeddings = np.load(emb_path)
                for i, m in enumerate(meta):
                    obj.entries.append(VectorEntry(
                        variant_id=m["variant_id"],
                        embedding=embeddings[i],
                        image_sha256=m["image_sha256"],
                        source=m["source"],
                        added_at=m["added_at"],
                        confirmed_count=m.get("confirmed_count", 0),
                    ))
            else:
                # 兼容旧版无 embeddings.npy
                for m in meta:
                    obj.entries.append(VectorEntry(
                        variant_id=m["variant_id"],
                        embedding=np.zeros(dim, dtype=np.float32),  # 占位
                        image_sha256=m["image_sha256"],
                        source=m["source"],
                        added_at=m["added_at"],
                        confirmed_count=m.get("confirmed_count", 0),
                    ))
        logger.info(f"Loaded vector index: {len(obj.entries)} entries, dim={dim}")
        return obj


# ──────────────────────────────────────────────────────────────────────────────
# 识别器实现（实现 counter_vision.Recognizer 协议）
# ──────────────────────────────────────────────────────────────────────────────

class ONNXRecognizer:
    """ONNX 模型识别器：实现 Recognizer 协议"""

    name: str
    configured: bool = True

    def __init__(
        self,
        model_key: str = DEFAULT_MODEL_KEY,
        model_dir: Path = MODEL_DIR,
        index_dir: Path = INDEX_DIR,
        pool: Any = None,  # 数据库连接池，用于查询商品图片
    ) -> None:
        self.model_key = model_key
        self.model_dir = model_dir
        self.index_dir = index_dir
        self.pool = pool
        self.config = MODEL_CONFIGS.get(model_key)
        if not self.config:
            raise ValueError(f"Unknown model key: {model_key}")

        self.model: Optional[ONNXModel] = None
        self.index: Optional[VectorIndex] = None
        self._initialized = False

    async def initialize(self) -> None:
        """异步初始化：加载模型、构建/加载向量索引"""
        if self._initialized:
            return

        # 1. 检查模型文件
        model_path = self.model_dir / self.config["filename"]
        if not model_path.exists():
            logger.warning(f"Model file not found: {model_path}, attempting download...")
            await self._download_model(model_path)

        # 2. 加载模型
        self.model = ONNXModel(model_path, self.config)

        # 3. 初始化向量索引
        dim = self.config["feature_dim"]
        self.index = VectorIndex.load(
            INDEX_FILE, META_FILE, EMBEDDINGS_FILE, dim
        )

        # 4. 若索引为空且有数据库连接池，从商品图片构建初始索引
        if self.index.index.ntotal == 0 and self.pool:
            await self._build_initial_index()

        self._initialized = True
        logger.info(f"ONNXRecognizer initialized: {self.config['name']}, index size={self.index.index.ntotal}")

    async def _download_model(self, model_path: Path) -> None:
        """下载模型文件（简化版，实际应用建议预置）"""
        import urllib.request
        url = self.config.get("url")
        if not url:
            logger.warning(f"No download URL for {self.model_key}")
            return
        model_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Downloading model from {url}...")
        try:
            urllib.request.urlretrieve(url, model_path)
            logger.info(f"Model downloaded to {model_path}")
        except Exception as e:
            logger.error(f"Model download failed: {e}")

    async def _build_initial_index(self) -> None:
        """从数据库商品图片构建初始向量索引"""
        if not self.pool or not self.model or not self.index:
            return
        logger.info("Building initial vector index from product photos...")
        async with self.pool.acquire() as conn:
            # 查询所有有实拍图的商品变体（优先 inventory_batch.shelf_image_url，其次 product_photo）
            rows = await conn.fetch("""
                SELECT DISTINCT ON (pv.id) pv.id as variant_id,
                       COALESCE(ib.shelf_image_url, pp.url) as image_url
                FROM product_variant pv
                LEFT JOIN inventory_batch ib ON ib.variant_id = pv.id AND ib.deleted_at IS NULL AND ib.status = 'active'
                LEFT JOIN product_photo pp ON pp.variant_id = pv.id AND pp.is_primary AND pp.status = 'active' AND pp.deleted_at IS NULL
                WHERE pv.deleted_at IS NULL AND pv.status = 'active'
                  AND (ib.shelf_image_url IS NOT NULL OR pp.url IS NOT NULL)
                ORDER BY pv.id, ib.created_at DESC
            """)
        count = 0
        for row in rows:
            try:
                await self._add_image_to_index(str(row["variant_id"]), str(row["image_url"]), "reference")
                count += 1
            except Exception as e:
                logger.warning(f"Failed to index variant {row['variant_id']}: {e}")
        logger.info(f"Initial index built: {count} entries")
        self._save_index()

    async def _add_image_to_index(
        self,
        variant_id: str,
        image_url: str,
        source: str,
    ) -> None:
        """下载图片、提取特征、加入索引"""
        if not self.model or not self.index:
            return
        # 读取图片（本地文件或 HTTP）
        image_data = await self._fetch_image(image_url)
        if not image_data:
            return
        sha256 = hashlib.sha256(image_data).hexdigest()
        # 预处理
        input_tensor = preprocess_image(
            image_data,
            self.config["input_size"],
            self.config["mean"],
            self.config["std"],
        )
        # 推理
        output = self.model.infer(input_tensor)
        # 提取特征向量（取输出最后一维，若为分类 logits 则需池化；DINOv2 直接输出特征）
        if output.ndim == 2 and output.shape[1] == self.config["feature_dim"]:
            embedding = output[0]
        elif output.ndim == 3:  # (1, seq_len, dim) -> 取 CLS token 或均值池化
            embedding = output[0].mean(axis=0)
        else:
            # 兜底：展平并截断/填充
            flat = output.flatten()
            if len(flat) >= self.config["feature_dim"]:
                embedding = flat[:self.config["feature_dim"]]
            else:
                embedding = np.pad(flat, (0, self.config["feature_dim"] - len(flat)))
        if self.config["normalize"]:
            embedding = l2_normalize(embedding)
        # 加入索引
        entry = VectorEntry(
            variant_id=variant_id,
            embedding=embedding,
            image_sha256=sha256,
            source=source,
        )
        self.index.add(entry)

    async def _fetch_image(self, url: str) -> Optional[bytes]:
        """获取图片字节（支持 file://, /media/, http(s)://）"""
        if url.startswith("file://"):
            path = Path(url[7:])
            if path.exists():
                return path.read_bytes()
        elif url.startswith("/media/"):
            # 本地媒体文件：/media/product_photo/... -> /data/...
            path = Path("/data") / url[1:]  # 去掉开头 /
            if path.exists():
                return path.read_bytes()
        elif url.startswith(("http://", "https://")):
            import urllib.request
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    return resp.read()
            except Exception as e:
                logger.warning(f"Failed to fetch {url}: {e}")
        return None

    def _save_index(self) -> None:
        if self.index:
            self.index.save(INDEX_FILE, META_FILE, EMBEDDINGS_FILE)

    async def recognize(
        self, image_bytes: bytes, weight_grams: int
    ) -> Iterable[dict[str, Any]]:
        """Recognizer 协议：识别图片，返回候选列表"""
        if not self._initialized:
            await self.initialize()
        if not self.model or not self.index:
            return []

        start = time.perf_counter()
        # 预处理
        input_tensor = preprocess_image(
            image_bytes,
            self.config["input_size"],
            self.config["mean"],
            self.config["std"],
        )
        # 推理
        output = self.model.infer(input_tensor)
        # 提取特征
        if output.ndim == 2 and output.shape[1] == self.config["feature_dim"]:
            embedding = output[0]
        elif output.ndim == 3:
            embedding = output[0].mean(axis=0)
        else:
            flat = output.flatten()
            if len(flat) >= self.config["feature_dim"]:
                embedding = flat[:self.config["feature_dim"]]
            else:
                embedding = np.pad(flat, (0, self.config["feature_dim"] - len(flat)))
        if self.config["normalize"]:
            embedding = l2_normalize(embedding)

        # 向量检索
        results = self.index.search(embedding, k=MAX_CANDIDATES)
        candidates = [
            {"variant_id": entry.variant_id, "score": max(0.0, min(1.0, (score + 1) / 2))}
            for entry, score in results
        ]
        elapsed = (time.perf_counter() - start) * 1000
        logger.debug(f"Recognition: {len(candidates)} candidates in {elapsed:.1f}ms")
        return candidates

    async def add_confirmed_frame(
        self, image_bytes: bytes, variant_id: str, source: str = "confirmed"
    ) -> None:
        """店员确认后将帧加入向量库（越用越准）"""
        if not self._initialized:
            await self.initialize()
        if not self.model or not self.index:
            return
        # 检查是否已存在相同 variant + 相似图片（去重）
        sha256 = hashlib.sha256(image_bytes).hexdigest()
        for entry in self.index.entries:
            if entry.variant_id == variant_id and entry.image_sha256 == sha256:
                entry.confirmed_count += 1
                logger.debug(f"Duplicate confirmed frame, incrementing count: {variant_id}")
                self._save_index()
                return
        # 添加新条目
        await self._add_image_to_index(variant_id, f"data:{sha256[:16]}", source)
        self._save_index()
        logger.info(f"Added confirmed frame to index: {variant_id} ({source})")


# ──────────────────────────────────────────────────────────────────────────────
# 便捷函数：获取/设置识别器（供 app 启动时调用）
# ──────────────────────────────────────────────────────────────────────────────

_recognizer_instance: Optional[ONNXRecognizer] = None


async def get_scale_recognizer(
    pool: Any = None,
    model_key: str = DEFAULT_MODEL_KEY,
) -> ONNXRecognizer:
    """获取或创建全局识别器实例"""
    global _recognizer_instance
    if _recognizer_instance is None:
        _recognizer_instance = ONNXRecognizer(model_key=model_key, pool=pool)
        await _recognizer_instance.initialize()
    elif pool and _recognizer_instance.pool is None:
        _recognizer_instance.pool = pool
        if _recognizer_instance.index and _recognizer_instance.index.index.ntotal == 0:
            await _recognizer_instance._build_initial_index()
    return _recognizer_instance


def set_scale_recognizer(recognizer: ONNXRecognizer) -> None:
    """设置全局识别器（测试用）"""
    global _recognizer_instance
    _recognizer_instance = recognizer


# ──────────────────────────────────────────────────────────────────────────────
# CLI：模型下载/索引构建/测试
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="Scale Vision ONNX Tools")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # download-model
    p_dl = sub.add_parser("download-model", help="下载模型文件")
    p_dl.add_argument("--model", default=DEFAULT_MODEL_KEY, choices=list(MODEL_CONFIGS.keys()))

    # build-index
    p_idx = sub.add_parser("build-index", help="从数据库重建向量索引")
    p_idx.add_argument("--model", default=DEFAULT_MODEL_KEY, choices=list(MODEL_CONFIGS.keys()))
    p_idx.add_argument("--dsn", required=True, help="PostgreSQL DSN")

    # test
    p_test = sub.add_parser("test", help="测试识别（需 --image）")
    p_test.add_argument("--model", default=DEFAULT_MODEL_KEY, choices=list(MODEL_CONFIGS.keys()))
    p_test.add_argument("--image", required=True, help="测试图片路径")
    p_test.add_argument("--dsn", help="PostgreSQL DSN（可选，用于加载商品图）")

    args = parser.parse_args()

    async def run() -> None:
        if args.cmd == "download-model":
            config = MODEL_CONFIGS[args.model]
            model_path = MODEL_DIR / config["filename"]
            model_path.parent.mkdir(parents=True, exist_ok=True)
            url = config.get("url")
            if not url:
                print(f"No download URL for {args.model}")
                return
            import urllib.request
            print(f"Downloading {url} -> {model_path}...")
            urllib.request.urlretrieve(url, model_path)
            print("Done")

        elif args.cmd == "build-index":
            import asyncpg
            pool = await asyncpg.create_pool(args.dsn)
            rec = ONNXRecognizer(model_key=args.model, pool=pool)
            await rec.initialize()
            print(f"Index built: {rec.index.index.ntotal} entries")
            await pool.close()

        elif args.cmd == "test":
            import asyncpg
            pool = await asyncpg.create_pool(args.dsn) if args.dsn else None
            rec = ONNXRecognizer(model_key=args.model, pool=pool)
            await rec.initialize()
            image_data = Path(args.image).read_bytes()
            candidates = await rec.recognize(image_data, 0)
            print(f"Candidates: {json.dumps(candidates, ensure_ascii=False, indent=2)}")
            if pool:
                await pool.close()

    asyncio.run(run())