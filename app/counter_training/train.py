"""训练 + 评估 CLI（离线跑，CPU 路线，零新依赖）。

用法：

    python -m app.counter_training.train --data <counter_dataset 目录> --out <模型.json>

退出码：0 门禁通过；2 无训练数据 / 不可训练；3 门禁不通过
（模型与报告仍保存，但模型不可注册）。

断点续跑：特征提取结果缓存在 ``<out 同名>.features.json``（按照片
sha256 索引）；中断后加 ``--resume`` 重跑，已提过的照片直接读缓存，
不再重复解码。缓存只是加速手段——删了也能完整重跑。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from app.counter_training.dataset import NoTrainingDataError, TrainingError, load_dataset
from app.counter_training.evaluate import evaluate, render_markdown
from app.counter_training.features import FEATURE_DIM, extract_features
from app.counter_training.model import CentroidModel

EXIT_OK = 0
EXIT_NO_DATA = 2
EXIT_GATE_FAIL = 3


def _load_feature_cache(cache_path: Path) -> dict[str, list[float]]:
    """读特征缓存；损坏 / 不存在返回空缓存（可完整重跑，不报错）。"""
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    cache: dict[str, list[float]] = {}
    for key, value in loaded.items():
        if (
            isinstance(key, str)
            and isinstance(value, list)
            and len(value) == FEATURE_DIM
            and all(isinstance(item, (int, float)) for item in value)
        ):
            cache[key] = [float(item) for item in value]
    return cache


def _save_feature_cache(cache_path: Path, cache: dict[str, list[float]]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}.json")
    tmp_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, cache_path)


def _load_variant_allowlist(path: Path) -> set[str]:
    """读 --variants-file：JSON 列表，元素为 variant UUID 字符串。"""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise TrainingError(f"variant 白名单不可读：{path}（{exc}）") from exc
    if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
        raise TrainingError(f"variant 白名单须为字符串列表：{path}")
    return set(loaded)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="柜台识别训练 + 评估（CPU，离线）")
    parser.add_argument("--data", required=True, help="数据集目录（counter_dataset/）")
    parser.add_argument("--out", required=True, help="模型输出路径（JSON）")
    parser.add_argument(
        "--report",
        default=None,
        help="评估报告输出路径（Markdown；默认与模型同名 .eval.md）",
    )
    parser.add_argument(
        "--variants-file",
        default=None,
        help="可选：variant 白名单 JSON 文件（字符串列表），用于再核对 variant 是否在库内",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="断点续跑：复用上次的特征缓存（<out 同名>.features.json）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_path = Path(args.out)
    report_path = Path(args.report) if args.report else out_path.with_suffix(".eval.md")
    cache_path = out_path.with_suffix(".features.json")

    variant_ids: set[str] | None = None
    if args.variants_file:
        try:
            variant_ids = _load_variant_allowlist(Path(args.variants_file))
        except TrainingError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return EXIT_NO_DATA
    try:
        dataset = load_dataset(args.data, variant_ids=variant_ids)
    except NoTrainingDataError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NO_DATA
    if args.variants_file is None:
        print("提示：未提供 --variants-file，未核对 variant 是否在库内（上传时已核对过一次）")
    for warning in dataset.warnings:
        print(f"警告：{warning}")

    train_entries = dataset.train
    labels = {entry.variant_id for entry in train_entries}
    if len(labels) < 2:
        print(f"错误：训练集只有 {len(labels)} 个 SKU，至少需要 2 个 SKU 才能训练", file=sys.stderr)
        return EXIT_NO_DATA

    cache = _load_feature_cache(cache_path) if args.resume else {}
    cache_hits = 0
    labeled_train: list[tuple[str, str, tuple[float, ...]]] = []
    test_items: list[tuple[tuple[float, ...], str, str]] = []
    for entry in train_entries + dataset.test:
        feature = cache.get(entry.sha256)
        if feature is not None:
            cache_hits += 1
            feature_tuple = tuple(feature)
        else:
            try:
                feature_tuple = extract_features(entry.path)
            except (OSError, ValueError) as exc:
                print(f"警告：{entry.sku}/{entry.path.name}：特征提取失败，已跳过（{exc}）")
                continue
            cache[entry.sha256] = list(feature_tuple)
        if entry.split == "train":
            labeled_train.append((entry.variant_id, entry.sku, feature_tuple))
        else:
            test_items.append((feature_tuple, entry.variant_id, entry.sku))
    _save_feature_cache(cache_path, cache)
    if cache_hits:
        print(f"特征缓存命中 {cache_hits} 张（--resume 断点续跑）")
    if len({item[0] for item in labeled_train}) < 2:
        print("错误：特征提取后训练集不足 2 个 SKU", file=sys.stderr)
        return EXIT_NO_DATA

    model = CentroidModel.fit(labeled_train)
    report = evaluate(model, test_items)
    model.eval_summary = report.summary()
    model.save(out_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_markdown(
            report,
            dataset_counts=dataset.counts_by_sku(),
            model_path=str(out_path),
            warnings=dataset.warnings,
        ),
        encoding="utf-8",
    )
    print(f"训练照片：{len(labeled_train)} 张，测试照片：{len(test_items)} 张")
    print(f"top-1：{report.top1:.1%}，top-3：{report.top3:.1%}")
    print(f"门禁结论：{'通过' if report.gate_passed else '不通过'}——{report.gate_reason}")
    print(f"模型：{out_path}")
    print(f"评估报告：{report_path}")
    if not report.gate_passed:
        print("模型已保存但不可注册：请补拍 / 纠正错判集中的 SKU 后重训，绝不调低门禁")
        return EXIT_GATE_FAIL
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
