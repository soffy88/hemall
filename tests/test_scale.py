"""Tests for app.scale — 秤接入协议解析、稳定判定、模拟器"""

import time

import pytest

from app.scale import (
    ScaleReading,
    ScaleSource,
    SimulatorScale,
    get_stable_weight,
    is_stable,
    parse_dahua,
    parse_dingjian,
    parse_generic,
    parse_scale_line,
    parse_toledo,
)


class TestProtocolParsers:
    """协议解析器单测"""

    def test_parse_dahua_kg(self):
        line = "ST,GS,+0001.500kg"
        reading = parse_dahua(line)
        assert reading is not None
        assert reading.weight_grams == 1500
        assert reading.unit == "kg"
        assert reading.stable is True  # 包含 "ST"

    def test_parse_dahua_g(self):
        line = "ST,GS,+00500g"
        reading = parse_dahua(line)
        assert reading is not None
        assert reading.weight_grams == 500
        assert reading.unit == "g"

    def test_parse_dahua_negative(self):
        line = "ST,GS,-0000.100kg"
        reading = parse_dahua(line)
        assert reading is not None
        assert reading.weight_grams == -100

    def test_parse_dingjian(self):
        line = "GS     0.000 kg"
        reading = parse_dingjian(line)
        assert reading is not None
        assert reading.weight_grams == 0

    def test_parse_dingjian_nt(self):
        line = "NT     1.234 kg"
        reading = parse_dingjian(line)
        assert reading is not None
        assert reading.weight_grams == 1234

    def test_parse_toledo(self):
        line = "S  0.500 kg"
        reading = parse_toledo(line)
        assert reading is not None
        assert reading.weight_grams == 500

    def test_parse_toledo_st(self):
        line = "ST  1.000 kg"
        reading = parse_toledo(line)
        assert reading is not None
        assert reading.weight_grams == 1000
        assert reading.stable is True

    def test_parse_generic(self):
        line = "Weight: 1234 g"
        reading = parse_generic(line)
        assert reading is not None
        assert reading.weight_grams == 1234

    def test_parse_generic_kg(self):
        line = "1.500kg"
        reading = parse_generic(line)
        assert reading is not None
        assert reading.weight_grams == 1500

    def test_parse_unknown_returns_none(self):
        line = "INVALID DATA"
        assert parse_dahua(line) is None
        assert parse_dingjian(line) is None
        assert parse_toledo(line) is None
        assert parse_generic(line) is None

    def test_parse_scale_line_priority(self):
        """特定协议优先于通用协议"""
        line = "ST,GS,+0001.000kg"  # 大华格式
        reading = parse_scale_line(line)
        assert reading is not None
        assert reading.weight_grams == 1000


class TestStableDetection:
    """稳定判定单测"""

    def make_reading(self, weight: int, stable: bool = False) -> ScaleReading:
        return ScaleReading(
            weight_grams=weight,
            unit="g",
            raw=f"SIM {weight}g",
            stable=stable,
            timestamp=time.time(),
        )

    def test_is_stable_insufficient_readings(self):
        window = [self.make_reading(1000), self.make_reading(1000)]
        assert is_stable(window, count=3) is False

    def test_is_stable_within_threshold(self):
        window = [
            self.make_reading(1000),
            self.make_reading(1001),
            self.make_reading(1000),
        ]
        assert is_stable(window, count=3, threshold_g=2) is True

    def test_is_stable_exceeds_threshold(self):
        window = [
            self.make_reading(1000),
            self.make_reading(1005),  # 差值 5 > 2
            self.make_reading(1000),
        ]
        assert is_stable(window, count=3, threshold_g=2) is False

    def test_get_stable_weight_returns_median(self):
        window = [
            self.make_reading(1000),
            self.make_reading(1002),
            self.make_reading(1001),
        ]
        stable = get_stable_weight(window, count=3, threshold_g=5)
        assert stable is not None
        assert stable.weight_grams == 1001  # 中位数

    def test_get_stable_weight_returns_none_when_unstable(self):
        window = [
            self.make_reading(1000),
            self.make_reading(1010),  # 差值 10 > 2
            self.make_reading(1000),
        ]
        stable = get_stable_weight(window, count=3, threshold_g=2)
        assert stable is None


class TestSimulatorScale:
    """模拟秤单测"""

    @pytest.mark.asyncio
    async def test_simulator_sequence(self):
        sim = SimulatorScale(stable_count=2, stable_threshold_g=1)
        # 序列末尾加 -1 触发自动断开
        sim.set_sequence([0, 500, 1000, 1000, 1000, -1])
        await sim.connect()

        readings = []
        for _ in range(6):  # 多读一次触发断开
            r = await sim.next_reading()
            if r:
                readings.append(r.weight_grams)

        assert readings == [0, 500, 1000, 1000, 1000]
        assert not sim.is_connected  # 负数触发自动断开

    @pytest.mark.asyncio
    async def test_simulator_stable_detection(self):
        sim = SimulatorScale(stable_count=3, stable_threshold_g=1)
        # 最后三次相同 -> 稳定
        sim.set_sequence([0, 500, 999, 1000, 1000, 1000])
        await sim.connect()

        stable = await sim.get_stable_weight()
        assert stable is not None
        assert stable.weight_grams == 1000
        assert stable.source == ScaleSource.SIMULATOR

    @pytest.mark.asyncio
    async def test_simulator_noise(self):
        sim = SimulatorScale(stable_count=3, stable_threshold_g=5, noise_g=2)
        sim.set_sequence([1000, 1000, 1000, 1000])
        await sim.connect()

        # 有噪声但仍在阈值内
        stable = await sim.get_stable_weight()
        assert stable is not None
        assert 990 <= stable.weight_grams <= 1010

    @pytest.mark.asyncio
    async def test_simulator_tare_zero(self):
        sim = SimulatorScale()
        sim.set_sequence([1000, 1000])
        await sim.connect()
        sim.tare()
        sim.zero()
        # 验证 tare/zero 不抛异常且后续读数正常
        reading = await sim.next_reading()
        assert reading is not None
        assert reading.weight_grams == 1000
        await sim.disconnect()


class TestEdgeCases:
    """边界情况"""

    def test_max_weight_validation(self):
        from app.scale import MAX_WEIGHT_GRAMS

        line = f"ST,GS,+{MAX_WEIGHT_GRAMS / 1000:.3f}kg"
        reading = parse_dahua(line)
        assert reading.weight_grams == MAX_WEIGHT_GRAMS

    def test_zero_weight(self):
        line = "ST,GS,+0000.000kg"
        reading = parse_dahua(line)
        assert reading.weight_grams == 0

    def test_decimal_precision(self):
        """测试小数精度换算"""
        line = "ST,GS,+0001.234kg"
        reading = parse_dahua(line)
        assert reading.weight_grams == 1234  # 四舍五入

    def test_whitespace_handling(self):
        line = "  ST,GS,+0001.000kg  \r\n"
        reading = parse_scale_line(line)
        assert reading is not None
        assert reading.weight_grams == 1000
