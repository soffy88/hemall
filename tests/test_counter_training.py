"""tests/test_counter_training.py — 柜台识别训练 harness：纯单元测试，不碰 DB。

fixture 声明：本文件所有照片均为代码现场生成的纯色小图（Pillow 现画），
仅验证 pipeline 的机械结构——加载 / 切分 / 特征 / 训练 / 评估数学 /
注册门禁。它们绝不是训练数据：绝不放进 counter_dataset/，绝不用来
声称训出模型。真模型只能等 400 张柜台实拍照片到了之后再训。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.counter_training.dataset import NoTrainingDataError, load_dataset
from app.counter_training.evaluate import TOP1_MIN, TOP3_MIN, evaluate
from app.counter_training.features import FEATURE_DIM, FEATURES_VERSION, extract_features
from app.counter_training.model import CentroidModel
from app.counter_training.registry import (
    MODEL_ENV_VAR,
    load_registered_recognizer,
    register_trained_recognizer,
)
from app.counter_training.train import main as train_main
from app.counter_vision import (
    NullRecognizer,
    get_recognizer,
    set_recognizer,
    try_load_configured_recognizer,
)

SKU_A = "sku-a"
SKU_B = "sku-b"
VAR_A = "11111111-1111-4111-8111-111111111111"
VAR_B = "22222222-2222-4222-8222-222222222222"

#: 合成 fixture 颜色：A 红系、B 绿系（每张微差，模拟同 SKU 不同摆放）。
TRAIN_COLORS = {
    SKU_A: [(200, 30, 30), (210, 35, 35), (190, 25, 25), (205, 40, 30)],
    SKU_B: [(30, 200, 30), (35, 210, 35), (25, 190, 25), (40, 205, 30)],
}
TEST_COLORS = {
    SKU_A: [(198, 32, 32), (192, 26, 30)],
    SKU_B: [(32, 198, 32), (28, 192, 30)],
}
#: 门禁失败 fixture：测试集颜色对调（A 拍成绿、B 拍成红），准确率应归零。
#: 注意：颜色须与全部训练色不同，否则会被去重（同一内容只算一张照片）跳过。
SWAPPED_TEST_COLORS = {
    SKU_A: [(28, 196, 28), (34, 204, 34)],
    SKU_B: [(196, 28, 28), (204, 34, 34)],
}


def _png_bytes(rgb: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (32, 32), rgb)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _build_dataset(
    root: Path,
    *,
    test_colors: dict[str, list[tuple[int, int, int]]] | None = None,
    variant_ids: dict[str, str] | None = None,
) -> Path:
    """在 tmp 下建一个合成数据集目录：每 SKU 4 训 + 2 测，带 manifest.json。"""
    test_colors = test_colors if test_colors is not None else TEST_COLORS
    variant_ids = variant_ids if variant_ids is not None else {SKU_A: VAR_A, SKU_B: VAR_B}
    for sku, colors in ((SKU_A, TRAIN_COLORS[SKU_A]), (SKU_B, TRAIN_COLORS[SKU_B])):
        sku_dir = root / sku
        sku_dir.mkdir(parents=True)
        records = []
        for index, rgb in enumerate(colors):
            data = _png_bytes(rgb)
            name = f"20261128T00000{index}_{uuid.uuid4().hex[:8]}.png"
            (sku_dir / name).write_bytes(data)
            records.append(
                {
                    "file": name,
                    "sku": sku,
                    "variant_id": variant_ids[sku],
                    "captured_at": None,
                    "photographer": "fixture",
                    "note": "",
                    "uploaded_by": "fixture",
                    "uploaded_at": "2026-11-28T00:00:00+00:00",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "byte_size": len(data),
                }
            )
        for index, rgb in enumerate(test_colors[sku]):
            data = _png_bytes(rgb)
            name = f"20261128T00001{index}_{uuid.uuid4().hex[:8]}.png"
            (sku_dir / name).write_bytes(data)
            records.append(
                {
                    "file": name,
                    "sku": sku,
                    "variant_id": variant_ids[sku],
                    "captured_at": None,
                    "photographer": "fixture",
                    "note": "test",
                    "uploaded_by": "fixture",
                    "uploaded_at": "2026-11-28T00:00:00+00:00",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "byte_size": len(data),
                }
            )
        (sku_dir / "manifest.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return root


@pytest.fixture(autouse=True)
def _reset_recognizer():
    set_recognizer(None)
    yield
    set_recognizer(None)
    os.environ.pop(MODEL_ENV_VAR, None)


# ── 数据集加载 ───────────────────────────────────────────────────────────────


class TestLoadDataset:
    def test_splits_train_test_by_note(self, tmp_path: Path):
        dataset = load_dataset(_build_dataset(tmp_path / "data"), variant_ids={VAR_A, VAR_B})
        assert len(dataset.train) == 8
        assert len(dataset.test) == 4
        assert dataset.counts_by_sku() == {
            SKU_A: {"train": 4, "test": 2},
            SKU_B: {"train": 4, "test": 2},
        }
        assert dataset.warnings == []

    def test_empty_dir_raises_no_training_data(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(NoTrainingDataError, match="无训练数据"):
            load_dataset(empty)

    def test_missing_dir_raises_no_training_data(self, tmp_path: Path):
        with pytest.raises(NoTrainingDataError, match="无训练数据"):
            load_dataset(tmp_path / "no-such-dir")

    def test_file_without_manifest_record_is_skipped(self, tmp_path: Path):
        root = _build_dataset(tmp_path / "data")
        (root / SKU_A / "orphan.png").write_bytes(_png_bytes((200, 30, 30)))
        dataset = load_dataset(root)
        assert len(dataset.entries) == 12
        assert any("manifest 中无记录" in warning for warning in dataset.warnings)

    def test_manifest_record_without_sha256_is_skipped(self, tmp_path: Path):
        """manifest 记录缺 sha256 校验码时照片须跳过（Codex 分流 H2）。"""
        data = _build_dataset(tmp_path / "data")
        manifest_path = tmp_path / "data" / SKU_A / "manifest.json"
        records = json.loads(manifest_path.read_text(encoding="utf-8"))
        del records[0]["sha256"]
        manifest_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        dataset = load_dataset(data)
        assert any("缺少 sha256" in warning for warning in dataset.warnings)
        assert len(dataset.train) + len(dataset.test) == 11

    def test_unreadable_image_is_skipped(self, tmp_path: Path):
        root = _build_dataset(tmp_path / "data")
        garbage = b"not an image at all"
        (root / SKU_A / "garbage.png").write_bytes(garbage)
        manifest_path = root / SKU_A / "manifest.json"
        records = json.loads(manifest_path.read_text(encoding="utf-8"))
        records.append(
            {
                "file": "garbage.png",
                "sku": SKU_A,
                "variant_id": VAR_A,
                "note": "",
                "sha256": hashlib.sha256(garbage).hexdigest(),
                "byte_size": len(garbage),
            }
        )
        manifest_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        dataset = load_dataset(root)
        assert len(dataset.entries) == 12
        assert any("garbage.png" in warning for warning in dataset.warnings)

    def test_variant_not_in_allowlist_is_skipped(self, tmp_path: Path):
        dataset = load_dataset(_build_dataset(tmp_path / "data"), variant_ids={VAR_A})
        assert {entry.variant_id for entry in dataset.entries} == {VAR_A}
        assert any("不在白名单内" in warning for warning in dataset.warnings)

    def test_sku_dir_without_manifest_is_skipped(self, tmp_path: Path):
        root = tmp_path / "data"
        sku_dir = root / SKU_A
        sku_dir.mkdir(parents=True)
        (sku_dir / "photo.png").write_bytes(_png_bytes((200, 30, 30)))
        with pytest.raises(NoTrainingDataError, match="无训练数据"):
            load_dataset(root)


# ── 特征 ───────────────────────────────────────────────────────────────────


class TestFeatures:
    def test_dim_and_determinism(self, tmp_path: Path):
        path = tmp_path / "red.png"
        path.write_bytes(_png_bytes((200, 30, 30)))
        first = extract_features(path)
        assert len(first) == FEATURE_DIM == 88
        assert extract_features(path) == first

    def test_different_colors_differ(self, tmp_path: Path):
        red_path = tmp_path / "red.png"
        green_path = tmp_path / "green.png"
        red_path.write_bytes(_png_bytes((200, 30, 30)))
        green_path.write_bytes(_png_bytes((30, 200, 30)))
        assert extract_features(red_path) != extract_features(green_path)


# ── 训练 / 评估数学 ────────────────────────────────────────────────────────


def _trained_model(tmp_path: Path) -> CentroidModel:
    dataset = load_dataset(_build_dataset(tmp_path / "data"))
    labeled = [
        (entry.variant_id, entry.sku, extract_features(entry.path)) for entry in dataset.train
    ]
    return CentroidModel.fit(labeled)


class TestModelAndEval:
    def test_separable_synthetic_top1_is_perfect(self, tmp_path: Path):
        model = _trained_model(tmp_path)
        dataset = load_dataset(tmp_path / "data")
        items = [
            (extract_features(entry.path), entry.variant_id, entry.sku) for entry in dataset.test
        ]
        report = evaluate(model, items)
        assert report.top1 == 1.0
        assert report.top3 == 1.0
        assert report.gate_passed is True
        assert report.test_n == 4

    def test_swapped_test_set_fails_gate(self, tmp_path: Path):
        model = _trained_model(tmp_path)
        swapped = _build_dataset(tmp_path / "swapped", test_colors=SWAPPED_TEST_COLORS)
        dataset = load_dataset(swapped)
        items = [
            (extract_features(entry.path), entry.variant_id, entry.sku) for entry in dataset.test
        ]
        report = evaluate(model, items)
        assert report.top1 == 0.0
        assert report.gate_passed is False

    def test_empty_test_set_cannot_pass(self, tmp_path: Path):
        model = _trained_model(tmp_path)
        report = evaluate(model, [])
        assert report.test_n == 0
        assert report.gate_passed is False
        assert "无测试集" in report.gate_reason

    def test_gate_requires_per_sku_bars(self, tmp_path: Path):
        """总体达标但某一品类全错时门禁须不通过（Codex 分流 H1）。

        10 个 SKU 各 10 张测试照：9 个全对、1 个全错。
        总体 top-1 恰好 90%、top-3 100%——旧的只看总体门禁会放行。
        """
        from types import SimpleNamespace

        def predict(feature, k=3):
            variant_id = feature[0]
            if variant_id == "v-bad":
                return [
                    {"variant_id": "v-other", "score": 0.9},
                    {"variant_id": "v-bad", "score": 0.05},
                    {"variant_id": "v-other2", "score": 0.05},
                ]
            return [
                {"variant_id": variant_id, "score": 0.9},
                {"variant_id": "v-other", "score": 0.05},
                {"variant_id": "v-other2", "score": 0.05},
            ]

        skus = {f"v-{index}": f"SKU-{index}" for index in range(9)}
        skus["v-bad"] = "SKU-bad"
        model = SimpleNamespace(predict_topk=predict, skus=skus)
        items = []
        for index in range(9):
            vid = f"v-{index}"
            for _ in range(10):
                items.append(((vid,), vid, f"SKU-{index}"))
        for _ in range(10):
            items.append((("v-bad",), "v-bad", "SKU-bad"))
        report = evaluate(model, items, expected_variant_ids=set(skus))
        assert report.top1 == 0.9
        assert report.top3 == 1.0
        assert report.gate_passed is False
        assert "SKU-bad" in report.gate_reason

    def test_gate_fails_when_sku_has_no_test_photos(self, tmp_path: Path):
        """模型会认的 SKU 无测试照片时门禁须不通过（Codex 分流 H1 覆盖面）。"""
        from types import SimpleNamespace

        def predict(feature, k=3):
            variant_id = feature[0]
            return [{"variant_id": variant_id, "score": 0.9}]

        skus = {"v-a": "SKU-a", "v-b": "SKU-b"}
        model = SimpleNamespace(predict_topk=predict, skus=skus)
        items = [(("v-a",), "v-a", "SKU-a") for _ in range(10)]
        report = evaluate(model, items, expected_variant_ids=set(skus))
        assert report.gate_passed is False
        assert "未测品类" in report.gate_reason
        assert "SKU-b" in report.gate_reason

    def test_thresholds_are_90_and_98(self):
        assert TOP1_MIN == 0.90
        assert TOP3_MIN == 0.98

    def test_model_save_load_roundtrip(self, tmp_path: Path):
        model = _trained_model(tmp_path)
        path = tmp_path / "model.json"
        model.save(path)
        loaded = CentroidModel.load(path)
        assert loaded.labels == model.labels
        assert loaded.centroids == model.centroids
        assert loaded.eval_passed() is False  # 未嵌评估摘要：不可注册

    def test_predict_scores_are_valid(self, tmp_path: Path):
        model = _trained_model(tmp_path)
        dataset = load_dataset(tmp_path / "data")
        feature = extract_features(dataset.test[0].path)
        ranked = model.predict_topk(feature, k=3)
        assert ranked[0]["variant_id"] == dataset.test[0].variant_id
        assert all(0.0 <= candidate["score"] <= 1.0 for candidate in ranked)


# ── CLI：空数据 / 门禁 / 断点续跑 ──────────────────────────────────────────


class TestTrainCli:
    def test_empty_data_exits_2_and_writes_nothing(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()
        out = tmp_path / "model.json"
        assert train_main(["--data", str(empty), "--out", str(out)]) == 2
        assert not out.exists()
        assert not (tmp_path / "model.eval.md").exists()

    def test_passing_run_writes_model_and_report(self, tmp_path: Path):
        data = _build_dataset(tmp_path / "data")
        out = tmp_path / "model.json"
        assert train_main(["--data", str(data), "--out", str(out)]) == 0
        assert out.exists()
        report = (tmp_path / "model.eval.md").read_text(encoding="utf-8")
        assert "top-1 准确率：100.0%" in report
        assert "门禁结论：通过" in report
        loaded = CentroidModel.load(out)
        assert loaded.eval_passed() is True

    def test_gate_fail_exits_3_but_model_is_saved_unregistrable(self, tmp_path: Path):
        data = _build_dataset(tmp_path / "data", test_colors=SWAPPED_TEST_COLORS)
        out = tmp_path / "model.json"
        assert train_main(["--data", str(data), "--out", str(out)]) == 3
        assert out.exists()
        loaded = CentroidModel.load(out)
        assert loaded.eval_passed() is False
        assert load_registered_recognizer(out) is None

    def test_resume_reuses_feature_cache(self, tmp_path: Path):
        data = _build_dataset(tmp_path / "data")
        out = tmp_path / "model.json"
        assert train_main(["--data", str(data), "--out", str(out)]) == 0
        cache_path = tmp_path / "model.features.json"
        assert cache_path.exists()
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        assert cached["version"] == FEATURES_VERSION
        assert len(cached["features"]) == 12
        assert train_main(["--data", str(data), "--out", str(out), "--resume"]) == 0

    def test_feature_cache_ignores_unversioned_and_wrong_version(self, tmp_path: Path):
        """旧版平面缓存与版本不符的缓存一律作废（Codex 分流 H4）。"""
        from app.counter_training.train import _load_feature_cache, _save_feature_cache

        cache_path = tmp_path / "model.features.json"
        cache_path.write_text(json.dumps({"aaa": [0.0] * FEATURE_DIM}), encoding="utf-8")
        assert _load_feature_cache(cache_path) == {}
        cache_path.write_text(
            json.dumps(
                {
                    "version": FEATURES_VERSION + 99,
                    "features": {"aaa": [0.0] * FEATURE_DIM},
                }
            ),
            encoding="utf-8",
        )
        assert _load_feature_cache(cache_path) == {}
        _save_feature_cache(cache_path, {"aaa": [0.1] * FEATURE_DIM})
        assert _load_feature_cache(cache_path) == {"aaa": [0.1] * FEATURE_DIM}

    def test_does_not_create_counter_dataset_in_cwd(self, tmp_path: Path, monkeypatch):
        data = _build_dataset(tmp_path / "data")
        workdir = tmp_path / "work"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        out = tmp_path / "model.json"
        assert train_main(["--data", str(data), "--out", str(out)]) == 0
        assert not (workdir / "counter_dataset").exists()


# ── 注册点：dormant / 门禁 ────────────────────────────────────────────────


class TestRegistry:
    def test_dormant_without_env(self, monkeypatch):
        monkeypatch.delenv(MODEL_ENV_VAR, raising=False)
        assert load_registered_recognizer() is None
        assert try_load_configured_recognizer() is None
        assert isinstance(get_recognizer(), NullRecognizer)

    def test_refuses_missing_and_corrupt_files(self, tmp_path: Path):
        assert load_registered_recognizer(tmp_path / "nope.json") is None
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        assert load_registered_recognizer(bad) is None

    def test_refuses_model_without_passing_eval(self, tmp_path: Path):
        model = _trained_model(tmp_path)
        path = tmp_path / "model.json"
        model.save(path)
        assert load_registered_recognizer(path) is None

    def test_loads_passing_model_and_recognizes(self, tmp_path: Path):
        data = _build_dataset(tmp_path / "data")
        out = tmp_path / "model.json"
        assert train_main(["--data", str(data), "--out", str(out)]) == 0
        recognizer = load_registered_recognizer(out)
        assert recognizer is not None
        assert recognizer.configured is True
        assert recognizer.name == "counter-centroid-v1"

    async def test_trained_recognizer_returns_top_candidate(self, tmp_path: Path):
        data = _build_dataset(tmp_path / "data")
        out = tmp_path / "model.json"
        assert train_main(["--data", str(data), "--out", str(out)]) == 0
        recognizer = load_registered_recognizer(out)
        assert recognizer is not None
        candidates = await recognizer.recognize(_png_bytes((200, 30, 30)), 500)
        assert candidates[0]["variant_id"] == VAR_A
        assert candidates[0]["score"] > candidates[-1]["score"]

    def test_register_sets_current_recognizer(self, tmp_path: Path, monkeypatch):
        data = _build_dataset(tmp_path / "data")
        out = tmp_path / "model.json"
        assert train_main(["--data", str(data), "--out", str(out)]) == 0
        monkeypatch.setenv(MODEL_ENV_VAR, str(out))
        assert register_trained_recognizer() is True
        assert get_recognizer().configured is True
        assert register_trained_recognizer(tmp_path / "nope.json") is False
        assert get_recognizer().configured is True  # 注册失败不改变当前识别器
