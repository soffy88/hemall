"""留出测试集评估 + 门禁。

门禁（铁律，不可调低）：测试集 top-1 >= 90% 且 top-3 >= 98% 才算通过。
无测试集（note=test 的照片 0 张）时无法验证，门禁直接不通过——
没有“目测差不多就放行”这一档。

评估用模型在测试集上的真实预测算数，不读训练集分数充数；
每 SKU 明细 + 最常见错判都写进报告，照片到了之后直接看报告定夺。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.counter_training.model import CentroidModel

#: 门禁阈值：top-1 >= 90%，top-3 >= 98%。
TOP1_MIN = 0.90
TOP3_MIN = 0.98


@dataclass(frozen=True)
class PerSkuEval:
    """单个 SKU（variant）在测试集上的明细。"""

    sku: str
    variant_id: str
    test_n: int
    top1_correct: int
    top3_correct: int
    top_confusions: tuple[tuple[str, int], ...] = ()

    @property
    def top1_acc(self) -> float:
        return self.top1_correct / self.test_n if self.test_n else 0.0

    @property
    def top3_acc(self) -> float:
        return self.top3_correct / self.test_n if self.test_n else 0.0


@dataclass(frozen=True)
class EvalReport:
    """一次评估的结论：总体准确率 + 每 SKU 明细 + 门禁结论。"""

    top1: float
    top3: float
    test_n: int
    per_sku: tuple[PerSkuEval, ...] = ()
    gate_passed: bool = False
    gate_reason: str = ""

    def summary(self) -> dict[str, Any]:
        """嵌进模型文件的评估摘要（注册时只认这份摘要 + 重新核对阈值）。"""
        return {
            "top1": self.top1,
            "top3": self.top3,
            "test_n": self.test_n,
            "gate_passed": self.gate_passed,
            "gate_reason": self.gate_reason,
        }


def evaluate(
    model: CentroidModel,
    test_items: list[tuple[tuple[float, ...], str, str]],
) -> EvalReport:
    """在测试集上评估：test_items 是 (特征, variant_id, sku) 的列表。"""
    test_n = len(test_items)
    if test_n == 0:
        return EvalReport(
            top1=0.0,
            top3=0.0,
            test_n=0,
            gate_passed=False,
            gate_reason="无测试集（note=test 的照片 0 张），无法验证，门禁不通过",
        )
    top1_ok = 0
    top3_ok = 0
    per_counts: dict[str, dict[str, Any]] = {}
    for feature, variant_id, sku in test_items:
        ranked = model.predict_topk(feature, k=3)
        pred_ids = [candidate["variant_id"] for candidate in ranked]
        slot = per_counts.setdefault(
            variant_id,
            {"sku": sku, "test_n": 0, "top1": 0, "top3": 0, "confusions": {}},
        )
        slot["test_n"] += 1
        if pred_ids and pred_ids[0] == variant_id:
            top1_ok += 1
            slot["top1"] += 1
        else:
            wrong = pred_ids[0] if pred_ids else ""
            wrong_sku = model.skus.get(wrong, wrong) if wrong else "（无候选）"
            confusions = slot["confusions"]
            confusions[wrong_sku] = confusions.get(wrong_sku, 0) + 1
        if variant_id in pred_ids:
            top3_ok += 1
            slot["top3"] += 1
    top1 = top1_ok / test_n
    top3 = top3_ok / test_n
    passed = top1 >= TOP1_MIN and top3 >= TOP3_MIN
    if passed:
        reason = f"通过：top-1 {top1:.1%} >= {TOP1_MIN:.0%}，top-3 {top3:.1%} >= {TOP3_MIN:.0%}"
    else:
        failing = []
        if top1 < TOP1_MIN:
            failing.append(f"top-1 {top1:.1%} < {TOP1_MIN:.0%}")
        if top3 < TOP3_MIN:
            failing.append(f"top-3 {top3:.1%} < {TOP3_MIN:.0%}")
        reason = "不通过：" + "，".join(failing)
    per_sku = tuple(
        PerSkuEval(
            sku=slot["sku"],
            variant_id=variant_id,
            test_n=slot["test_n"],
            top1_correct=slot["top1"],
            top3_correct=slot["top3"],
            top_confusions=tuple(sorted(slot["confusions"].items(), key=lambda item: -item[1])[:2]),
        )
        for variant_id, slot in sorted(per_counts.items(), key=lambda item: item[1]["sku"])
    )
    return EvalReport(
        top1=top1,
        top3=top3,
        test_n=test_n,
        per_sku=per_sku,
        gate_passed=passed,
        gate_reason=reason,
    )


def render_markdown(
    report: EvalReport,
    *,
    dataset_counts: dict[str, dict[str, int]],
    model_path: str,
    warnings: list[str],
) -> str:
    """把评估报告写成 Markdown（格式见 docs/counter-training-eval-template.md）。"""
    lines = [
        "# 柜台识别评估报告",
        "",
        f"- 模型文件：`{model_path}`",
        f"- 测试集张数：{report.test_n}",
        f"- top-1 准确率：{report.top1:.1%}（门禁 >= {TOP1_MIN:.0%}）",
        f"- top-3 准确率：{report.top3:.1%}（门禁 >= {TOP3_MIN:.0%}）",
        f"- 门禁结论：{'通过' if report.gate_passed else '不通过'}——{report.gate_reason}",
        "",
        "## 数据集构成",
        "",
        "| SKU | 训练张数 | 测试张数 |",
        "| --- | ---: | ---: |",
    ]
    for sku in sorted(dataset_counts):
        counts = dataset_counts[sku]
        lines.append(f"| {sku} | {counts['train']} | {counts['test']} |")
    lines += [
        "",
        "## 每 SKU 明细",
        "",
        "| SKU | 测试张数 | top-1 正确 | top-1 准确率 | top-3 正确 | top-3 准确率 | 最常见错判 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in report.per_sku:
        confusions = (
            "；".join(f"{name} x{count}" for name, count in item.top_confusions)
            if item.top_confusions
            else "—"
        )
        lines.append(
            f"| {item.sku} | {item.test_n} | {item.top1_correct} | {item.top1_acc:.1%} | "
            f"{item.top3_correct} | {item.top3_acc:.1%} | {confusions} |"
        )
    lines += ["", "## 加载警告", ""]
    if warnings:
        lines += [f"- {warning}" for warning in warnings]
    else:
        lines.append("- 无")
    lines += [
        "",
        "## 门禁结论",
        "",
        (
            "通过：模型可注册（见 docs/counter-training.md 的注册步骤）。"
            if report.gate_passed
            else "不通过：模型已保存但不可注册。请补拍 / 纠正错判集中的 SKU 后重训，绝不调低门禁。"
        ),
        "",
    ]
    return "\n".join(lines)
