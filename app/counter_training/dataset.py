"""数据集加载：读 counter_dataset/<sku>/ + manifest.json。

布局（与 POST /store/counter/dataset-photos 的落盘一致）：

    counter_dataset/
        <sku>/
            manifest.json        # build_manifest_entry 写的一条条记录
            <时间戳>_<uuid8>.jpg  # 照片本体

规则：

- 拆分：manifest 里 note 字段写 ``test`` 的照片进测试集，其余进训练集
  （规范：每 SKU 40 张 = 30 训练 + 10 测试，测试集单独拍、单独记）。
- 标签：训练标签以 manifest 的 ``variant_id`` 为准（上传时接口已核对
  variant 真实存在）；目录名 sku 只作报告用的展示标签，不编映射。
- 对不上的处理：一律记 warning 并跳过该照片，绝不编映射、绝不猜：
  manifest 缺失 / 记录缺失 / variant_id 非法 / 不在白名单内 /
  sha256 与 manifest 不符 / 图片不可读 / 重复照片。
- 空数据：目录不存在，或可用训练照片为 0 张，抛 NoTrainingDataError
  （训练与评估明确报错退出，不生成假模型、不写假评估报告）。
- 本模块只读数据集，绝不写 counter_dataset/（写操作只有训练产物，
  目标由 --out 显式指定）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from app.counter_vision import MAX_IMAGE_BYTES, detect_image_format

#: 测试集标记：manifest 的 note 字段写 ``test`` 即为测试集照片。
TEST_NOTE = "test"

MANIFEST_NAME = "manifest.json"


class NoTrainingDataError(ValueError):
    """数据集为空 / 不可用：训练与评估明确报错退出，不生成假模型。"""


class TrainingError(ValueError):
    """数据集可用但不可训练（如类别不足）。"""


@dataclass(frozen=True)
class PhotoEntry:
    """一张可用照片：路径 + 标签 + 拆分。"""

    path: Path
    sku: str
    variant_id: str
    note: str
    sha256: str
    split: str  # "train" 或 "test"


@dataclass
class Dataset:
    """加载结果：可用照片 + 加载全程的警告（跳过的照片都在里面）。"""

    root: Path
    entries: list[PhotoEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def train(self) -> list[PhotoEntry]:
        return [entry for entry in self.entries if entry.split == "train"]

    @property
    def test(self) -> list[PhotoEntry]:
        return [entry for entry in self.entries if entry.split == "test"]

    def counts_by_sku(self) -> dict[str, dict[str, int]]:
        """每 SKU 的训练 / 测试张数（供评估报告用）。"""
        counts: dict[str, dict[str, int]] = {}
        for entry in self.entries:
            slot = counts.setdefault(entry.sku, {"train": 0, "test": 0})
            slot[entry.split] += 1
        return counts


def _readable_image(data: bytes) -> bool:
    """Pillow 能完整解码才算可读（只验魔数不够，截断图也要拦住）。"""
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
    except Exception:  # noqa: BLE001 - 任何解码失败都只算“不可读”，跳过即可
        return False
    return True


def _load_manifest(
    manifest_path: Path, *, sku: str, warnings: list[str]
) -> dict[str, dict[str, Any]] | None:
    """读某 SKU 的 manifest.json，返回 {文件名: 记录}；缺失 / 损坏返回 None。"""
    if not manifest_path.exists():
        return None
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        warnings.append(f"{sku}：manifest.json 解析失败，已按缺失处理")
        return None
    if not isinstance(loaded, list):
        warnings.append(f"{sku}：manifest.json 不是列表，已按缺失处理")
        return None
    by_file: dict[str, dict[str, Any]] = {}
    for item in loaded:
        if isinstance(item, dict) and isinstance(item.get("file"), str):
            by_file[item["file"]] = item
    return by_file


def load_dataset(root: str | Path, *, variant_ids: Collection[str] | None = None) -> Dataset:
    """加载数据集目录。

    ``variant_ids``：可选的库内 variant 白名单（UUID 字符串集合）；
    给了就逐张核对，不在白名单内的跳过。不给时不核对——上传接口在
    入库时已核对过 variant 真实存在，训练时再核对属于加一道保险
    （可用 --variants-file 从库里导出一份白名单再训）。
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise NoTrainingDataError(f"无训练数据：数据集目录不存在：{root_path}")
    entries: list[PhotoEntry] = []
    warnings: list[str] = []
    seen_sha: set[str] = set()

    for sku_dir in sorted(root_path.iterdir()):
        if not sku_dir.is_dir() or sku_dir.name.startswith("."):
            continue
        sku = sku_dir.name
        by_file = _load_manifest(sku_dir / MANIFEST_NAME, sku=sku, warnings=warnings)
        files = sorted(
            path
            for path in sku_dir.iterdir()
            if path.is_file() and path.name != MANIFEST_NAME and not path.name.startswith(".")
        )
        if by_file is None:
            warnings.append(
                f"{sku}：缺少 manifest.json，{len(files)} 张照片无 variant 映射，已全部跳过"
            )
            continue
        for path in files:
            record = by_file.get(path.name)
            if record is None:
                warnings.append(f"{sku}/{path.name}：manifest 中无记录（无 variant 映射），已跳过")
                continue
            variant_id = record.get("variant_id")
            if not isinstance(variant_id, str):
                warnings.append(f"{sku}/{path.name}：manifest 缺少 variant_id，已跳过")
                continue
            try:
                uuid.UUID(variant_id)
            except ValueError:
                warnings.append(f"{sku}/{path.name}：variant_id 非法 UUID，已跳过")
                continue
            if variant_ids is not None and variant_id not in variant_ids:
                warnings.append(f"{sku}/{path.name}：variant_id 不在白名单内，已跳过")
                continue
            try:
                data = path.read_bytes()
            except OSError:
                warnings.append(f"{sku}/{path.name}：文件不可读，已跳过")
                continue
            if len(data) > MAX_IMAGE_BYTES:
                warnings.append(f"{sku}/{path.name}：文件超 {MAX_IMAGE_BYTES} 字节，已跳过")
                continue
            if detect_image_format(data) is None:
                warnings.append(f"{sku}/{path.name}：非 jpeg/png/webp，已跳过")
                continue
            if not _readable_image(data):
                warnings.append(f"{sku}/{path.name}：图片无法解码，已跳过")
                continue
            digest = hashlib.sha256(data).hexdigest()
            manifest_sha = record.get("sha256")
            if isinstance(manifest_sha, str) and manifest_sha and manifest_sha != digest:
                warnings.append(f"{sku}/{path.name}：sha256 与 manifest 不符，已跳过")
                continue
            if digest in seen_sha:
                warnings.append(f"{sku}/{path.name}：与已有照片内容重复，已跳过")
                continue
            seen_sha.add(digest)
            note = record.get("note") if isinstance(record.get("note"), str) else ""
            split = "test" if note.strip().lower() == TEST_NOTE else "train"
            entries.append(
                PhotoEntry(
                    path=path,
                    sku=sku,
                    variant_id=variant_id,
                    note=note,
                    sha256=digest,
                    split=split,
                )
            )

    train_entries = [entry for entry in entries if entry.split == "train"]
    if not train_entries:
        raise NoTrainingDataError(
            f"无训练数据：{root_path} 内可用训练照片 0 张"
            f"（可用照片共 {len(entries)} 张，警告 {len(warnings)} 条）；"
            "请先按 docs/counter-training.md 拍照片并上传"
        )
    if variant_ids is None:
        warnings.append(
            "未提供 variant 白名单：未核对 variant 是否在库内"
            "（上传接口在入库时已核对过一次；可用 --variants-file 再核一道）"
        )
    train_counts: dict[str, int] = {}
    for entry in train_entries:
        train_counts[entry.sku] = train_counts.get(entry.sku, 0) + 1
    for sku in sorted(train_counts):
        if train_counts[sku] < 3:
            warnings.append(f"{sku}：训练照片仅 {train_counts[sku]} 张（建议每 SKU 至少 30 张）")
    return Dataset(root=root_path, entries=entries, warnings=warnings)
