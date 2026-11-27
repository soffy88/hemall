"""线上称重定价纯函数：单位换算、四舍五入、容差与档位解析。"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.weigh_price import (
    DEFAULT_WEIGHT_OPTIONS,
    diff_policy,
    parse_weight_options,
    resolve_weight_options,
    settlement_status_for,
    unit_price_to_per_jin_cents,
    validate_weight_grams,
    weigh_amount_cents,
)


class TestUnitPrice:
    def test_jin_is_identity(self):
        assert unit_price_to_per_jin_cents(398, "斤") == 398
        assert unit_price_to_per_jin_cents(398, "jin") == 398

    def test_kg_to_jin(self):
        assert unit_price_to_per_jin_cents(1000, "kg") == 500
        assert unit_price_to_per_jin_cents(999, "kg") == 500  # 499.5 四舍五入
        assert unit_price_to_per_jin_cents(997, "kg") == 499  # 498.5 → 499

    def test_piece_price_has_no_per_jin(self):
        with pytest.raises(ValueError):
            unit_price_to_per_jin_cents(500, "piece")


class TestAmountRounding:
    def test_half_up_to_cent(self):
        # 500g × 3.98元/斤 = 398 分
        assert weigh_amount_cents(500, 398) == 398
        # 350g × 4.19元/斤 = 293.3 → 293
        assert weigh_amount_cents(350, 419) == 293
        # 250g × 4.19元/斤 = 209.5 → 210（四舍五入到分）
        assert weigh_amount_cents(250, 419) == 210
        # 1000g × 3.33元/斤 = 666 分
        assert weigh_amount_cents(1000, 333) == 666

    def test_custom_basis(self):
        assert weigh_amount_cents(1000, 2500, price_basis_grams=1000) == 2500

    def test_invalid(self):
        with pytest.raises(ValueError):
            weigh_amount_cents(0, 398)
        with pytest.raises(ValueError):
            weigh_amount_cents(500, -1)
        with pytest.raises(ValueError):
            weigh_amount_cents(500, 398, price_basis_grams=0)


class TestDiffPolicy:
    def test_within_tolerance_refund_and_charge(self):
        refund = diff_policy(4000, 3600, tolerance_percent=10)
        assert refund["diff_cents"] == -400
        assert refund["within_tolerance"] is True
        assert refund["action"] == "refund"

        charge = diff_policy(4000, 4400, tolerance_percent=10)
        assert charge["diff_cents"] == 400
        assert charge["within_tolerance"] is True
        assert charge["action"] == "charge"

    def test_outside_tolerance_needs_confirmation(self):
        policy = diff_policy(4000, 4500, tolerance_percent=10)
        assert policy["within_tolerance"] is False
        assert policy["action"] == "confirm_charge"
        assert settlement_status_for(policy["action"]) == "needs_confirmation"

    def test_exact_tolerance_boundary_is_within(self):
        policy = diff_policy(4000, 4400, tolerance_percent=10)
        assert policy["within_tolerance"] is True

    def test_zero_diff(self):
        policy = diff_policy(4000, 4000)
        assert policy["action"] == "none"
        assert policy["diff_cents"] == 0

    def test_rounding_tolerance_down_to_cent(self):
        # 333 × 10% = 33.3 → 33 分
        assert diff_policy(333, 366)["tolerance_cents"] == 33


class TestWeightOptions:
    def test_default_when_empty(self):
        assert resolve_weight_options(None) == DEFAULT_WEIGHT_OPTIONS

    def test_parse_json_string_and_list(self):
        assert parse_weight_options('[{"label":"约1斤","grams":500}]') == [
            {"label": "约1斤", "grams": 500}
        ]
        assert parse_weight_options([{"label": "半斤", "grams": 250}]) == [
            {"label": "半斤", "grams": 250}
        ]

    def test_invalid_options_skipped(self):
        assert parse_weight_options([{"label": "零", "grams": 0}, {"grams": "500"}]) == []

    def test_validate_weight_grams(self):
        assert validate_weight_grams(1) == 1
        assert validate_weight_grams(15000) == 15000
        with pytest.raises(ValueError):
            validate_weight_grams(0)
        with pytest.raises(ValueError):
            validate_weight_grams(15001)
        with pytest.raises(ValueError):
            validate_weight_grams(True)


def test_weigh_settings_defaults():
    settings = Settings(environment="staging")
    assert settings.weigh_price_unit == "jin"
    assert settings.weigh_diff_tolerance_percent == 10
    # 资料缺失默认不隐藏（严格门禁是开关，默认关）。
    assert settings.catalog_strict_data_gate is False
    # 酒类默认上架，但年龄门禁仍在。
    assert settings.alcohol_sales_enabled is True
