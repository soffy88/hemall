"""门店合并 SKU 清单：99 项、来源列、计价方式与参考价约束。"""

from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "store-sku-pricing.csv"
ASSETS = ROOT / "assets" / "store-sku"


def _rows() -> list[dict[str, str]]:
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_csv_has_99_unique_skus_and_sources():
    rows = _rows()
    assert len(rows) == 99
    assert len({r["SKU"] for r in rows}) == 99
    assert {r["来源"] for r in rows} <= {"99", "73", "两者"}
    # 99 清单与 73 清单是并集：73 独有的已全部并入（来源=两者）。
    assert sum(1 for r in rows if r["来源"] == "两者") >= 73
    # 7 大类预算上限合计（不含补货周转金）与用户清单一致。
    budget = {}
    for row in rows:
        budget[row["类目slug"]] = int(row["品类预算上限元"])
    assert sum(budget.values()) == 7400


def test_reference_prices_are_numeric_or_blank_and_flagged_confirm():
    for row in _rows():
        for col in ("待填进价(元/单位)", "待填售价(元/单位)"):
            value = row[col]
            if value != "":
                assert float(value) > 0
        # 参考价不是供应商当日报价，必须保留人工确认标记。
        assert "参考价-待供应商确认" in row["备注"]
        # 有参考价就必须能追溯来源名称、URL 与日期。
        if row["参考进价"]:
            assert row["进价来源"] and row["进价URL"] and row["进价日期"]
        if row["参考零售价"]:
            assert row["零售来源"] and row["零售URL"] and row["零售日期"]
        assert row["计价方式"] in ("weight", "piece")
        if row["计价方式"] == "weight":
            assert int(row["计价基准克"]) == 500


def test_images_are_reference_files_or_pending():
    for row in _rows():
        image = row["图片"]
        if image.startswith("assets/store-sku/"):
            rel = image.split("；")[0]
            path = ROOT / rel
            assert path.is_file(), rel
            assert path.stat().st_size <= 500_000
            assert "参考图-待换实拍" in image
            assert row["图片来源"]
        else:
            assert image.startswith("待拍照")


def test_margin_check_flags_insufficient_gross_margin():
    for row in _rows():
        w = row["参考进价"]
        s = row["建议售价"]
        if w and s:
            expected = (float(s) - float(w)) / float(s)
            if expected < 0.15:
                assert "毛利不足需复核" in row["毛利率"]


def test_merge_decisions_cover_all_skus_and_reuse_existing_ids():
    rows = _rows()
    with (ROOT / "merge-decisions.csv").open(encoding="utf-8-sig", newline="") as fh:
        decisions = list(csv.DictReader(fh))
    assert len(decisions) == 99
    assert {d["SKU"] for d in decisions} == {r["SKU"] for r in rows}
    assert {d["决定"] for d in decisions} <= {"合并", "新建"}
    merges = [d for d in decisions if d["决定"] == "合并"]
    assert len(merges) == 19
    for d in merges:
        assert len(d["现有商品ID"]) == 36
        assert d["现有商品名称"]
    # 不同品不合并，避免把明确区分的商品压成一条。
    by_sku = {d["SKU"]: d for d in decisions}
    for sku in ("EGT-002", "GRN-014", "GRN-022", "GRN-002", "GRN-003", "GRN-008"):
        assert by_sku[sku]["决定"] == "新建"
