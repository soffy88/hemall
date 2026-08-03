"""tests/test_phase9_feed_quant.py — Phase 9: BFF v9.0 做市量化算子测试。

覆盖:
  - compute_discount_rate (直降比例)
  - classify_feed_tag (tag_type 派生: clearance/fresh/standard)
  - is_panic_stock (库存恐慌阈值)
  - normalize_velocity (流速归一化)
  - build_feed_item (BFF v9.0 扁平契约)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.ext.feed_quant import (
    build_feed_item,
    classify_feed_tag,
    compute_discount_rate,
    is_panic_stock,
    normalize_velocity,
)

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)


# ── 直降比例 ────────────────────────────────────────────────────────────────


class TestComputeDiscountRate:
    def test_normal_discount(self):
        """35.9 元划线价 → 19.9 元现价 = 45% 直降。"""
        assert compute_discount_rate(1990, 3590) == 45

    def test_no_benchmark(self):
        assert compute_discount_rate(1990, 0) == 0

    def test_benchmark_lower_than_retail(self):
        """基准价低于现价 (反向) → 0%，不出现负数。"""
        assert compute_discount_rate(3590, 1990) == 0

    def test_equal_prices(self):
        assert compute_discount_rate(1000, 1000) == 0

    def test_threshold_boundary(self):
        """59% 直降 → 59，40% 直降 → 40。"""
        assert compute_discount_rate(4100, 10000) == 59
        assert compute_discount_rate(6000, 10000) == 40


# ── tag_type 派生 ────────────────────────────────────────────────────────────


class TestClassifyFeedTag:
    def test_clearance_by_discount(self):
        """直降 45% ≥ 40% → clearance。"""
        assert classify_feed_tag(1990, 3590, NOW - timedelta(days=10), None, NOW) == "clearance"

    def test_clearance_boundary_40pct(self):
        """直降刚好 40% → clearance (>= 40%)。"""
        assert classify_feed_tag(6000, 10000, NOW - timedelta(days=10), None, NOW) == "clearance"

    def test_clearance_by_expiry(self):
        """直降 10% 但 6 小时后过期 → clearance (临期)。"""
        assert (
            classify_feed_tag(
                9000, 10000, NOW - timedelta(days=5), NOW + timedelta(hours=6), NOW
            )
            == "clearance"
        )

    def test_fresh_new_batch(self):
        """入库 24h 内、降幅 10% → fresh。"""
        assert (
            classify_feed_tag(
                9000, 10000, NOW - timedelta(hours=24), NOW + timedelta(days=7), NOW
            )
            == "fresh"
        )

    def test_fresh_discount_too_big(self):
        """入库 24h 内但降幅 50% → clearance (降幅优先)。"""
        assert (
            classify_feed_tag(
                5000, 10000, NOW - timedelta(hours=24), NOW + timedelta(days=7), NOW
            )
            == "clearance"
        )

    def test_standard_old_batch(self):
        """入库 10 天、降幅 10%、距过期 3 天 → standard。"""
        assert (
            classify_feed_tag(
                9000, 10000, NOW - timedelta(days=10), NOW + timedelta(days=3), NOW
            )
            == "standard"
        )

    def test_standard_fresh_expiry_window(self):
        """入库 24h 但 10 小时后过期 → clearance (临期优先于溯源)。"""
        assert (
            classify_feed_tag(
                9000, 10000, NOW - timedelta(hours=24), NOW + timedelta(hours=10), NOW
            )
            == "clearance"
        )

    def test_no_benchmark_no_expiry(self):
        """无基准价无过期时间 → standard。"""
        assert classify_feed_tag(1990, 0, NOW - timedelta(days=3), None, NOW) == "standard"


# ── 库存恐慌 ─────────────────────────────────────────────────────────────────


class TestIsPanicStock:
    def test_five_or_less_is_panic(self):
        assert is_panic_stock(5) is True
        assert is_panic_stock(3) is True
        assert is_panic_stock(1) is True
        assert is_panic_stock(0) is True

    def test_six_plus_not_panic(self):
        assert is_panic_stock(6) is False
        assert is_panic_stock(50) is False


# ── 流速归一化 ───────────────────────────────────────────────────────────────


class TestNormalizeVelocity:
    def test_one_hour(self):
        assert normalize_velocity(5, 1.0) == 5.0

    def test_two_hours_window(self):
        assert normalize_velocity(10, 2.0) == 5.0

    def test_zero_window(self):
        assert normalize_velocity(10, 0) == 0.0

    def test_rounding(self):
        assert normalize_velocity(3, 2.0) == 1.5


# ── BFF v9.0 feed_item 契约 ─────────────────────────────────────────────────


class TestBuildFeedItem:
    def test_full_row(self):
        row = {
            "id": "batch_cherry_001",
            "variant_id": "v1",
            "product_id": "p1",
            "title": "J级智利车厘子 250g",
            "sku_code": "CHERRY-A",
            "retail_price_cents": 1990,
            "benchmark_price_cents": 3590,
            "stock_qty": 3,
            "expiration_time": NOW + timedelta(hours=6),
            "created_at": NOW - timedelta(days=10),
            "video_url": "https://cdn/video.mp4",
            "location_name": "3号车库节点",
            "affinity": 0.9,
            "boosted": True,
            "observed_velocity": 2.5,
        }
        item = build_feed_item(row, NOW)
        assert item["batch_id"] == "batch_cherry_001"
        assert item["sku_name"] == "J级智利车厘子 250g"
        assert item["tag_type"] == "clearance"  # 临期 + 降幅达标
        assert item["retail_price"] == 1990
        assert item["benchmark_price"] == 3590
        assert item["stock_qty"] == 3
        assert item["observed_velocity"] == 2.5
        assert item["media_url"] == "https://cdn/video.mp4"
        assert item["affinity_boosted"] is True

    def test_minimal_row(self):
        """缺字段也不炸——BFF 层必须防御脏数据。"""
        item = build_feed_item({"id": "b1", "title": "土豆"}, NOW)
        assert item["batch_id"] == "b1"
        assert item["sku_name"] == "土豆"
        assert item["tag_type"] == "standard"
        assert item["retail_price"] == 0
        assert item["benchmark_price"] == 0
        assert item["stock_qty"] == 0
        assert item["observed_velocity"] == 0.0
        assert item["media_url"] is None
        assert item["affinity_boosted"] is False

    def test_naive_datetime_defense(self):
        """asyncpg 返回 naive datetime 时补 UTC，不因 tz 缺失炸掉。"""
        row = {
            "id": "b2",
            "title": "牛奶",
            "retail_price_cents": 1800,  # 10% 降幅 (不触发 clearance)
            "benchmark_price_cents": 2000,
            "stock_qty": 10,
            "created_at": datetime(2026, 7, 31, 12, 0, 0),  # naive, 72h 前 (非 fresh)
            "expiration_time": datetime(2026, 8, 10, 12, 0, 0),  # naive, 7 天后 (非临期)
        }
        item = build_feed_item(row, NOW)
        assert item["tag_type"] == "standard"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
