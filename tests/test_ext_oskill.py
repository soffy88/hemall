"""hemall 扩展域 oskill 层单测 — 纯内存算法，不需要数据库，无条件运行。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.ext.oskill import (
    adjust_demand_curve_by_weather,
    build_fomo_system_prompt,
    calculate_broadcast_priority,
    calculate_decay_price,
    calculate_lord_tax,
    calculate_membership_dividend,
    calculate_next_probe_price,
    calculate_piece_rate_wage,
    calculate_vwap_deviation,
    check_spatial_conflict,
    compute_market_maker_price,
    compute_mercenary_bounty_rate,
    compute_supplier_trust_score,
    compute_transparent_shipping_options,
    compute_user_savings_yield,
    construct_fomo_user_prompt,
    evaluate_claim_credibility,
    match_neighbor_route,
    normalize_sku_price,
    predict_household_burn_rate,
    recompute_voronoi_grid,
    resolve_display_batch,
    validate_supplier_margin,
    verify_media_provenance,
)


# ── resolve_display_batch ────────────────────────────────────────────────


def test_resolve_display_batch_fifo_picks_oldest():
    batches = [
        {"id": "new", "intake_time": datetime.now(UTC), "retail_price": 100},
        {
            "id": "old",
            "intake_time": datetime.now(UTC) - timedelta(days=3),
            "retail_price": 200,
        },
    ]
    assert resolve_display_batch(batches, strategy="fifo")["id"] == "old"


def test_resolve_display_batch_price_desc_picks_highest():
    batches = [
        {"id": "cheap", "intake_time": datetime.now(UTC), "retail_price": 100},
        {"id": "pricey", "intake_time": datetime.now(UTC), "retail_price": 200},
    ]
    assert resolve_display_batch(batches, strategy="price_desc")["id"] == "pricey"


def test_resolve_display_batch_rejects_empty_and_unknown_strategy():
    with pytest.raises(ValueError):
        resolve_display_batch([], strategy="fifo")
    with pytest.raises(ValueError):
        resolve_display_batch(
            [{"id": "x", "intake_time": datetime.now(UTC), "retail_price": 1}],
            strategy="teleport",
        )


# ── compute_transparent_shipping_options ────────────────────────────────


def test_compute_transparent_shipping_options_scales_with_distance_and_weight():
    near = compute_transparent_shipping_options(1.0, weight=500)
    far = compute_transparent_shipping_options(20.0, weight=5000)
    near_express = next(o["price"] for o in near if o["type"] == "express")
    far_express = next(o["price"] for o in far if o["type"] == "express")
    assert far_express > near_express

    wave = next(o["price"] for o in near if o["type"] == "wave")
    pickup = next(o["price"] for o in near if o["type"] == "pickup")
    assert wave == 200
    assert pickup == 0


def test_compute_transparent_shipping_options_rejects_negative():
    with pytest.raises(ValueError):
        compute_transparent_shipping_options(-1.0, weight=100)
    with pytest.raises(ValueError):
        compute_transparent_shipping_options(1.0, weight=-1)


# ── recompute_voronoi_grid ────────────────────────────────────────────────


def test_recompute_voronoi_grid_mutual_neighbors():
    nodes = [("a", 31.0, 121.0), ("b", 31.1, 121.1), ("c", 30.9, 121.2)]
    grid = recompute_voronoi_grid(nodes)
    assert set(grid.keys()) == {"a", "b", "c"}
    # 3 点的 Voronoi 图里两两都接壤
    for loc_id, entry in grid.items():
        others = {n for n, _, _ in nodes if n != loc_id}
        assert set(entry["neighbors"]) == others


def test_recompute_voronoi_grid_rejects_too_few_or_duplicate():
    with pytest.raises(ValueError):
        recompute_voronoi_grid([("a", 31.0, 121.0)])
    with pytest.raises(ValueError):
        recompute_voronoi_grid([("a", 31.0, 121.0), ("a", 31.1, 121.1)])


# ── verify_media_provenance ──────────────────────────────────────────────


def test_verify_media_provenance_close_point_passes():
    assert (
        verify_media_provenance(
            {"gps_lat": 31.001, "gps_lon": 121.001}, supplier_loc=(31.0, 121.0)
        )
        is True
    )


def test_verify_media_provenance_far_point_fails():
    assert (
        verify_media_provenance(
            {"gps_lat": 40.0, "gps_lon": 100.0}, supplier_loc=(31.0, 121.0)
        )
        is False
    )


def test_verify_media_provenance_missing_gps_fails_closed():
    assert verify_media_provenance({}, supplier_loc=(31.0, 121.0)) is False


# ── calculate_decay_price ─────────────────────────────────────────────────


def test_calculate_decay_price_decays_from_markup_to_floor():
    fresh = calculate_decay_price(
        datetime.now(UTC), shelf_life_hours=24, base_cost=2000
    )
    half = calculate_decay_price(
        datetime.now(UTC) - timedelta(hours=12), shelf_life_hours=24, base_cost=2000
    )
    expired = calculate_decay_price(
        datetime.now(UTC) - timedelta(hours=48), shelf_life_hours=24, base_cost=2000
    )

    assert fresh == 3000  # 1.5x cost
    assert half == 2500  # halfway between 1.5x and 1.0x
    assert expired == 2000  # floored at cost, never goes below


def test_calculate_decay_price_rejects_invalid_params():
    with pytest.raises(ValueError):
        calculate_decay_price(datetime.now(UTC), shelf_life_hours=0, base_cost=2000)
    with pytest.raises(ValueError):
        calculate_decay_price(datetime.now(UTC), shelf_life_hours=24, base_cost=0)


# ── adjust_demand_curve_by_weather ───────────────────────────────────────


def test_adjust_demand_curve_by_weather_below_threshold_unchanged():
    assert (
        adjust_demand_curve_by_weather(3900, rain_probability=0.3, rain_intensity=1)
        == 3900
    )


def test_adjust_demand_curve_by_weather_storm_discounts():
    price = adjust_demand_curve_by_weather(3900, rain_probability=0.9, rain_intensity=3)
    assert price < 3900
    assert price >= 3900 * 0.5  # 最多打 5 折


def test_adjust_demand_curve_by_weather_rejects_invalid_params():
    with pytest.raises(ValueError):
        adjust_demand_curve_by_weather(0, rain_probability=0.5, rain_intensity=1)
    with pytest.raises(ValueError):
        adjust_demand_curve_by_weather(1000, rain_probability=1.5, rain_intensity=1)
    with pytest.raises(ValueError):
        adjust_demand_curve_by_weather(1000, rain_probability=0.5, rain_intensity=-1)


# ── calculate_piece_rate_wage ─────────────────────────────────────────────


def test_calculate_piece_rate_wage_surges_with_queue_depth():
    low = calculate_piece_rate_wage(5, base_wage=500)
    high = calculate_piece_rate_wage(55, base_wage=500)
    assert low == 500
    assert high == 750  # 55 // 10 = 5 -> +50%


def test_calculate_piece_rate_wage_caps_at_3x():
    extreme = calculate_piece_rate_wage(10_000, base_wage=500)
    assert extreme == 1500  # capped at 3x


def test_calculate_piece_rate_wage_rejects_invalid_params():
    with pytest.raises(ValueError):
        calculate_piece_rate_wage(-1, base_wage=500)
    with pytest.raises(ValueError):
        calculate_piece_rate_wage(5, base_wage=0)


# ── compute_user_savings_yield ────────────────────────────────────────────


def test_compute_user_savings_yield_basic():
    assert compute_user_savings_yield(6000, node_retail_price=3900) == 2100


def test_compute_user_savings_yield_floors_at_zero_when_more_expensive():
    assert compute_user_savings_yield(2000, node_retail_price=3900) == 0


def test_compute_user_savings_yield_rejects_negative():
    with pytest.raises(ValueError):
        compute_user_savings_yield(-1, node_retail_price=100)


# ── predict_household_burn_rate ───────────────────────────────────────────


def test_predict_household_burn_rate_larger_family_burns_sooner():
    history = [
        {"purchased_at": datetime.now(UTC) - timedelta(days=20), "quantity": 1},
        {"purchased_at": datetime.now(UTC) - timedelta(days=10), "quantity": 1},
        {"purchased_at": datetime.now(UTC), "quantity": 1},
    ]
    small_family = predict_household_burn_rate(history, family_size=3)
    large_family = predict_household_burn_rate(history, family_size=6)
    assert large_family < small_family


def test_predict_household_burn_rate_rejects_insufficient_history_or_bad_family_size():
    with pytest.raises(ValueError):
        predict_household_burn_rate(
            [{"purchased_at": datetime.now(UTC), "quantity": 1}], family_size=3
        )
    history = [
        {"purchased_at": datetime.now(UTC) - timedelta(days=10), "quantity": 1},
        {"purchased_at": datetime.now(UTC), "quantity": 1},
    ]
    with pytest.raises(ValueError):
        predict_household_burn_rate(history, family_size=0)


# ── evaluate_claim_credibility (v2.0) ──────────────────────────────────────


def test_evaluate_claim_credibility_high_trust_low_risk_is_instant():
    assert (
        evaluate_claim_credibility(100, batch_anomaly_rate=0.0, route_risk=0.0)
        == "instant"
    )


def test_evaluate_claim_credibility_low_trust_high_risk_is_honeypot():
    assert (
        evaluate_claim_credibility(0, batch_anomaly_rate=1.0, route_risk=1.0)
        == "honeypot"
    )


def test_evaluate_claim_credibility_boundary_is_instant_not_honeypot():
    # distrust(0)*0.5 + anomaly(1.0)*0.3 + risk(0)*0.2 = 0.30，等于阈值本身
    # (> 阈值才判 honeypot，等于阈值仍是 instant)。
    assert (
        evaluate_claim_credibility(100, batch_anomaly_rate=1.0, route_risk=0.0)
        == "instant"
    )
    # 同样的组合再加一点点 route_risk，刚好越过阈值 -> honeypot。
    assert (
        evaluate_claim_credibility(100, batch_anomaly_rate=1.0, route_risk=0.01)
        == "honeypot"
    )


def test_evaluate_claim_credibility_rejects_out_of_range_params():
    with pytest.raises(ValueError):
        evaluate_claim_credibility(101, batch_anomaly_rate=0.0, route_risk=0.0)
    with pytest.raises(ValueError):
        evaluate_claim_credibility(50, batch_anomaly_rate=1.1, route_risk=0.0)
    with pytest.raises(ValueError):
        evaluate_claim_credibility(50, batch_anomaly_rate=0.0, route_risk=-0.1)


# ── compute_supplier_trust_score (v2.0) ────────────────────────────────────


def test_compute_supplier_trust_score_upheld_penalizes_rejected_rewards():
    base = compute_supplier_trust_score([])
    assert base == 100
    penalized = compute_supplier_trust_score([{"outcome": "upheld"}])
    assert penalized == 90
    # 单条 rejected 会把 100+1=101 算出来，但函数内部已经封顶到 100。
    rewarded = compute_supplier_trust_score([{"outcome": "rejected"}])
    assert rewarded == 100


def test_compute_supplier_trust_score_caps_at_0_and_100():
    assert compute_supplier_trust_score([{"outcome": "rejected"}] * 10) == 100
    assert compute_supplier_trust_score([{"outcome": "upheld"}] * 20) == 0


def test_compute_supplier_trust_score_new_violation_deducts_by_severity():
    score = compute_supplier_trust_score([], new_violation={"severity": 1.0})
    assert score == 70  # 100 - round(1.0*30)


def test_compute_supplier_trust_score_rejects_bad_severity():
    with pytest.raises(ValueError):
        compute_supplier_trust_score([], new_violation={"severity": 1.5})


# ── calculate_vwap_deviation / compute_market_maker_price (v2.0) ──────────


def test_calculate_vwap_deviation_negative_means_undersold():
    curve = {"expected_cumulative": [10, 30, 60, 90], "stddev": 10}
    assert calculate_vwap_deviation(20, target_curve=curve, elapsed_hours=2) == -4.0
    assert calculate_vwap_deviation(60, target_curve=curve, elapsed_hours=2) == 0.0


def test_calculate_vwap_deviation_clamps_elapsed_hours_to_curve_end():
    curve = {"expected_cumulative": [10, 30, 60], "stddev": 10}
    # elapsed_hours 超出曲线长度 -> 钉在最后一个点 (60)
    assert calculate_vwap_deviation(60, target_curve=curve, elapsed_hours=99) == 0.0


def test_calculate_vwap_deviation_rejects_invalid_curve():
    with pytest.raises(ValueError):
        calculate_vwap_deviation(10, target_curve={}, elapsed_hours=0)
    with pytest.raises(ValueError):
        calculate_vwap_deviation(
            10, target_curve={"expected_cumulative": [1], "stddev": 0}, elapsed_hours=0
        )
    with pytest.raises(ValueError):
        calculate_vwap_deviation(
            -1, target_curve={"expected_cumulative": [1], "stddev": 1}, elapsed_hours=0
        )


def test_compute_market_maker_price_marks_down_when_undersold():
    price = compute_market_maker_price(1000, vwap_deviation=-2.0, theta_decay=0.0)
    assert price < 1000
    baseline = compute_market_maker_price(1000, vwap_deviation=0.0, theta_decay=0.0)
    assert baseline == 1000


def test_compute_market_maker_price_theta_decay_always_reduces_price():
    no_decay = compute_market_maker_price(1000, vwap_deviation=0.0, theta_decay=0.0)
    with_decay = compute_market_maker_price(1000, vwap_deviation=0.0, theta_decay=0.5)
    assert with_decay < no_decay


def test_compute_market_maker_price_rejects_invalid_params():
    with pytest.raises(ValueError):
        compute_market_maker_price(0, vwap_deviation=0.0, theta_decay=0.0)
    with pytest.raises(ValueError):
        compute_market_maker_price(1000, vwap_deviation=0.0, theta_decay=1.5)


# ── calculate_membership_dividend (v2.0) ───────────────────────────────────


def test_calculate_membership_dividend_scales_with_referrals():
    assert calculate_membership_dividend(0, base_fee=9900) == 0
    assert calculate_membership_dividend(5, base_fee=9900) == round(9900 * 0.1) * 5


def test_calculate_membership_dividend_can_exceed_base_fee():
    # 超过阈值转正向分红：额度 > base_fee 时调用方自己拆分减免/分红
    amount = calculate_membership_dividend(20, base_fee=9900)
    assert amount > 9900


def test_calculate_membership_dividend_rejects_invalid_params():
    with pytest.raises(ValueError):
        calculate_membership_dividend(-1, base_fee=9900)
    with pytest.raises(ValueError):
        calculate_membership_dividend(1, base_fee=0)


# ── match_neighbor_route (v2.0) ─────────────────────────────────────────────


def test_match_neighbor_route_picks_nearest_willing_neighbor():
    buyer = (31.0, 121.0)
    orders = [
        {"order_id": "far", "loc": (31.0009, 121.0), "willing_to_deliver": True},
        {"order_id": "near", "loc": (31.0002, 121.0), "willing_to_deliver": True},
        {
            "order_id": "unwilling_but_closest",
            "loc": (31.00005, 121.0),
            "willing_to_deliver": False,
        },
    ]
    result = match_neighbor_route(buyer, orders)
    assert result is not None
    assert result["neighbor_order_id"] == "near"
    assert 0 <= result["distance_km"] <= 0.1
    assert 100 <= result["commission_cents"] <= 500


def test_match_neighbor_route_returns_none_when_nobody_in_range():
    buyer = (31.0, 121.0)
    orders = [{"order_id": "too_far", "loc": (32.0, 121.0), "willing_to_deliver": True}]
    assert match_neighbor_route(buyer, orders) is None


# ── build_fomo_system_prompt / construct_fomo_user_prompt / calculate_broadcast_priority (v4.0) ──


def test_build_fomo_system_prompt_is_stable_and_nonempty():
    p1 = build_fomo_system_prompt()
    p2 = build_fomo_system_prompt()
    assert p1 == p2
    assert "80 字" in p1


def test_construct_fomo_user_prompt_computes_discount_pct():
    batch_info = {
        "id": "batch_x",
        "variant_desc": "30枚/箱",
        "supplier_polygon_name": "某某农场",
        "retail_price": 3900,
        "stock_qty": 30,
        "broadcast_type": "fresh_arrival",
    }
    prompt = construct_fomo_user_prompt(batch_info, market_price=7800)
    assert "batch_x" in prompt
    assert "暴降 50%" in prompt
    assert "39.0元" in prompt
    assert "78.0元" in prompt
    assert "30 箱" in prompt


def test_construct_fomo_user_prompt_rejects_non_positive_prices():
    batch_info = {
        "id": "batch_x",
        "variant_desc": "x",
        "supplier_polygon_name": "x",
        "retail_price": 3900,
        "stock_qty": 1,
        "broadcast_type": "clearance",
    }
    with pytest.raises(ValueError):
        construct_fomo_user_prompt(batch_info, market_price=0)
    with pytest.raises(ValueError):
        construct_fomo_user_prompt({**batch_info, "retail_price": 0}, market_price=7800)


def test_calculate_broadcast_priority_scales_with_stock_and_urgency():
    urgent = calculate_broadcast_priority(100, theta_decay_hours=2)
    relaxed = calculate_broadcast_priority(100, theta_decay_hours=20)
    assert urgent > relaxed


def test_calculate_broadcast_priority_zero_when_already_expired():
    assert calculate_broadcast_priority(100, theta_decay_hours=0) == 0
    assert calculate_broadcast_priority(100, theta_decay_hours=-5) == 0


def test_calculate_broadcast_priority_rejects_negative_stock():
    with pytest.raises(ValueError):
        calculate_broadcast_priority(-1, theta_decay_hours=5)


# ── normalize_sku_price / validate_supplier_margin / calculate_next_probe_price (v5.0) ──


def test_normalize_sku_price_grams_and_kg():
    assert normalize_sku_price(1280, raw_unit="500g") == 1280 / 500
    assert normalize_sku_price(1280, raw_unit="1kg") == 1280 / 1000


def test_normalize_sku_price_liter_matches_regardless_of_case():
    # 回归测试：修复前的 SPEC 原文正则先 .lower() 输入再去匹配大写 "L"，
    # 永远匹配不上——升这个单位实际上从来没被正确识别过。
    assert normalize_sku_price(6000, raw_unit="2L") == 6000 / 2000
    assert normalize_sku_price(6000, raw_unit="2l") == 6000 / 2000


def test_normalize_sku_price_returns_none_for_unparseable_unit():
    # 不是 SPEC 原文那种"按件计"的 float(raw_price) 兜底——那会把整件价
    # 混进"每克价格"这一列，后续 AVG() 会被静默污染。
    assert normalize_sku_price(500, raw_unit="1件") is None
    assert normalize_sku_price(500, raw_unit="1盒") is None


def test_normalize_sku_price_rejects_negative_price():
    with pytest.raises(ValueError):
        normalize_sku_price(-1, raw_unit="500g")


def test_validate_supplier_margin_accepts_at_or_above_50_percent():
    assert validate_supplier_margin(1.0, benchmark_price=2.0) is True  # 正好 50%
    assert validate_supplier_margin(0.5, benchmark_price=2.0) is True  # 75%


def test_validate_supplier_margin_rejects_below_50_percent():
    assert validate_supplier_margin(1.5, benchmark_price=2.0) is False  # 25%


def test_validate_supplier_margin_rejects_invalid_params():
    with pytest.raises(ValueError):
        validate_supplier_margin(-1, benchmark_price=2.0)
    with pytest.raises(ValueError):
        validate_supplier_margin(1.0, benchmark_price=0)


def test_calculate_next_probe_price_holds_when_velocity_meets_target():
    assert (
        calculate_next_probe_price(1000, observed_velocity=0.5, target_velocity=0.5)
        == 1000
    )
    assert (
        calculate_next_probe_price(1000, observed_velocity=0.8, target_velocity=0.5)
        == 1000
    )


def test_calculate_next_probe_price_cuts_5_percent_when_undersold():
    assert (
        calculate_next_probe_price(1000, observed_velocity=0.1, target_velocity=0.5)
        == 950
    )


def test_calculate_next_probe_price_rejects_invalid_params():
    with pytest.raises(ValueError):
        calculate_next_probe_price(0, observed_velocity=0.1, target_velocity=0.5)
    with pytest.raises(ValueError):
        calculate_next_probe_price(1000, observed_velocity=-1, target_velocity=0.5)
    with pytest.raises(ValueError):
        calculate_next_probe_price(1000, observed_velocity=0.1, target_velocity=0)


# ── compute_mercenary_bounty_rate ────────────────────────────────────────


def test_compute_mercenary_bounty_rate_zero_when_safe():
    assert compute_mercenary_bounty_rate(24.0) == 0.0
    assert compute_mercenary_bounty_rate(100.0) == 0.0


def test_compute_mercenary_bounty_rate_max_when_panicking():
    assert compute_mercenary_bounty_rate(4.0) == 0.80
    assert compute_mercenary_bounty_rate(0.0) == 0.80
    # 已过期但还没清理掉的库存，视同最危险情形处理
    assert compute_mercenary_bounty_rate(-5.0) == 0.80


def test_compute_mercenary_bounty_rate_interpolates_linearly():
    # 中点 (14 小时) 应该正好是 40%
    assert compute_mercenary_bounty_rate(14.0) == 0.40


# ── calculate_lord_tax ───────────────────────────────────────────────────


def test_calculate_lord_tax_active_node():
    assert calculate_lord_tax(100000, is_active_node=True) == 200  # 万分之二


def test_calculate_lord_tax_inactive_node_is_zero():
    assert calculate_lord_tax(100000, is_active_node=False) == 0


# ── check_spatial_conflict ────────────────────────────────────────────────


def test_check_spatial_conflict_detects_nearby_node():
    assert check_spatial_conflict((31.5, 121.5), [(31.5005, 121.5005)]) is True


def test_check_spatial_conflict_allows_far_node():
    assert check_spatial_conflict((31.5, 121.5), [(39.9, 116.4)]) is False


def test_check_spatial_conflict_empty_existing_locations():
    assert check_spatial_conflict((31.5, 121.5), []) is False
