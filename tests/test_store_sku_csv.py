"""门店合并 SKU 清单：99 项、来源列、计价方式与“不编价”约束。"""

from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "store-sku-pricing.csv"


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


def test_csv_prices_are_blank_and_images_pending():
    for row in _rows():
        assert row["待填进价(元/单位)"] == ""
        assert row["待填售价(元/单位)"] == ""
        assert row["图片"] == "待拍照"
        assert row["计价方式"] in ("weight", "piece")
        if row["计价方式"] == "weight":
            assert int(row["计价基准克"]) == 500


def test_weight_piece_categories_match_business_rule():
    rows = _rows()
    by_name = {r["商品名称"]: r for r in rows}
    # 蔬果肉禽鱼豆干千张按重量；大米/油/调味瓶装/豆腐盒/火龙果/西瓜按件。
    for name in ("菜心", "前腿肉", "鲫鱼", "豆干", "千张豆皮", "苹果"):
        assert by_name[name]["计价方式"] == "weight"
    for name in ("大米", "食用油", "生抽", "嫩豆腐", "老豆腐", "火龙果", "西瓜"):
        assert by_name[name]["计价方式"] == "piece"
