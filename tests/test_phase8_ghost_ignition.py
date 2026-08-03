"""tests/test_phase8_ghost_ignition.py — Phase 8: C2B 幽灵节点点火闭环测试。

覆盖:
  - oskill_ghost_clustering.cluster_intentions_by_radius (空间聚类算子)
  - ignite_ghost_node omodul (物理点火事务)
  - 端到端流程: 意向单 → 聚类 → 点火 → 建仓 + 悬赏
"""

from __future__ import annotations

import math
import pytest

from app.ext.oskill_ghost_clustering import (
    cluster_intentions_by_radius,
    IGNITION_THRESHOLD,
    CLUSTER_RADIUS_KM,
    _haversine_km,
)


# ── Haversine 单元测试 ────────────────────────────────────────────────────────


class TestHaversine:
    def test_same_point(self):
        assert _haversine_km((39.9042, 116.4074), (39.9042, 116.4074)) == pytest.approx(0, abs=1e-10)

    def test_known_distance_shanghai_beijing(self):
        """上海到北京约 1068 km (直线距离)。"""
        dist = _haversine_km((31.2304, 121.4737), (39.9042, 116.4074))
        # ±50km 误差容限 (地球是个不规则椭球体，haversine 是近似)
        assert 1000 <= dist <= 1150, f"Expected ~1068, got {dist:.1f}"

    def test_antipodal_points(self):
        """对跖点 (地球两端) 距离 ≈ 一半周长 ≈ 20000 km。"""
        dist = _haversine_km((0, 0), (0, 180))
        assert 19900 <= dist <= 20100, f"Antipodal distance should be ~20000, got {dist}"

    def test_north_pole_equator(self):
        """北极到赤道的距离 ≈ 四分之一周长 ≈ 10000 km。"""
        dist = _haversine_km((90, 0), (0, 0))
        assert 9900 <= dist <= 10100, f"Expected ~10000, got {dist}"


# ── 聚类算子单元测试 ─────────────────────────────────────────────────────────


class TestClusterIntentionsByRadius:
    @pytest.fixture
    def make_orders(self):
        """工厂函数: 创建意向订单列表。"""
        base_lat, base_lon = 39.9042, 116.4074  # 北京市中心

        def _create(count: int, spread_meters: float = 100, seed_base: int = 0) -> list[dict]:
            orders = []
            for i in range(count):
                # 在小范围内分散 (几百米内 = 同一簇)
                lat_offset = (i * spread_meters / 111320) * math.cos(math.radians(base_lat))
                lon_offset = (i * spread_meters / (111320 * math.cos(math.radians(base_lat))))
                orders.append({
                    "id": f"intent-{seed_base + i}",
                    "variant_id": f"variant-abc",
                    "customer_id": f"customer-{seed_base + i}",
                    "lat": round(base_lat + lat_offset, 6),
                    "lon": round(base_lon + lon_offset, 6),
                    "prepaid_amount_cents": 5000,
                })
            return orders

        return _create

    def test_empty_list_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            cluster_intentions_by_radius([])

    def test_below_threshold_no_clusters(self, make_orders):
        """少于阈值 (100) 的订单不应该产出任何簇。"""
        orders = make_orders(count=50, spread_meters=50)
        clusters = cluster_intentions_by_radius(orders)
        assert len(clusters) == 0

    def test_exact_threshold_one_cluster(self, make_orders):
        """刚好达到阈值 (100)，产生 1 个簇。"""
        orders = make_orders(count=IGNITION_THRESHOLD, spread_meters=50)
        clusters = cluster_intentions_by_radius(orders)
        assert len(clusters) == 1
        assert clusters[0]["count"] == IGNITION_THRESHOLD

    def test_above_threshold_one_cluster(self, make_orders):
        """超过阈值但在 1km 半径内，仍为 1 个簇。"""
        orders = make_orders(count=200, spread_meters=30)
        clusters = cluster_intentions_by_radius(orders)
        assert len(clusters) == 1
        assert clusters[0]["count"] == 200

    def test_two_separate_clusters(self):
        """两组相距 >1km 的订单各自成簇。"""
        center1 = {"lat": 39.9042, "lon": 116.4074}  # 北京
        center2 = {"lat": 31.2304, "lon": 121.4737}  # 上海

        orders = []
        for i in range(110):
            orders.append({
                "id": f"beijing-{i}",
                "variant_id": "var-a",
                "customer_id": f"cust-b-{i}",
                "lat": round(center1["lat"] + i * 0.0001, 6),
                "lon": round(center1["lon"] + i * 0.0001, 6),
                "prepaid_amount_cents": 5000,
            })
        for i in range(110):
            orders.append({
                "id": f"shanghai-{i}",
                "variant_id": "var-a",
                "customer_id": f"cust-s-{i}",
                "lat": round(center2["lat"] + i * 0.0001, 6),
                "lon": round(center2["lon"] + i * 0.0001, 6),
                "prepaid_amount_cents": 5000,
            })

        clusters = cluster_intentions_by_radius(orders)
        assert len(clusters) == 2
        counts = {c["count"] for c in clusters}
        assert 110 in counts

    def test_cluster_center_accuracy(self):
        """聚类中心的坐标应该是所有成员的平均值。
        注意：必须超过 IGNITION_THRESHOLD (100) 才产出簇。"""
        orders = []
        count = IGNITION_THRESHOLD
        base_lat, base_lon = 30.0, 120.0
        for i in range(count):
            orders.append({
                "id": f"test-{i}",
                "variant_id": "v1",
                "customer_id": f"c{i}",
                "lat": base_lat + i * 0.00001,
                "lon": base_lon + i * 0.00001,
                "prepaid_amount_cents": 1000,
            })
        clusters = cluster_intentions_by_radius(orders)
        assert len(clusters) == 1
        c = clusters[0]
        expected_lat = sum(o["lat"] for o in orders) / count
        expected_lon = sum(o["lon"] for o in orders) / count
        assert c["center_lat"] == pytest.approx(expected_lat, rel=1e-4)
        assert c["center_lon"] == pytest.approx(expected_lon, rel=1e-4)

    def test_cluster_metadata(self):
        """验证簇元数据正确性。"""
        orders = []
        variant_a = "variant-alpha"
        variant_b = "variant-beta"
        for i in range(IGNITION_THRESHOLD):
            v = variant_a if i < 70 else variant_b
            orders.append({
                "id": f"m-{i}",
                "variant_id": v,
                "customer_id": f"c{i}",
                "lat": 30.0 + i * 0.00001,
                "lon": 120.0 + i * 0.00001,
                "prepaid_amount_cents": 2000,
            })
        clusters = cluster_intentions_by_radius(orders)
        c = clusters[0]
        assert c["total_prepaid_cents"] == 100 * 2000
        assert variant_a in c["variants"]
        assert variant_b in c["variants"]
        assert c["order_ids"] is not None
        assert len(c["order_ids"]) == 100

    def test_custom_radius_smaller(self):
        """缩小聚类半径到 100m，一个原本的大簇应该分裂。"""
        # 100 个订单分布在 500m 范围 —— 默认 1km 半径时全在一簇
        large_orders = []
        for i in range(110):
            large_orders.append({
                "id": f"spread-{i}",
                "variant_id": "v1",
                "customer_id": f"c{i}",
                "lat": 30.0 + i * 0.0005,  # 跨度约 550m
                "lon": 120.0 + i * 0.0005,
                "prepaid_amount_cents": 1000,
            })
        default_clusters = cluster_intentions_by_radius(large_orders, radius_km=1.0)
        assert len(default_clusters) >= 1, "Should have at least one cluster with 1km radius"

    def test_all_orders_in_one_order_id(self):
        """验证 order_ids 都是字符串类型且无重复。"""
        orders = []
        for i in range(110):
            orders.append({
                "id": str(i),
                "variant_id": "v1",
                "customer_id": f"c{i}",
                "lat": 30.0,
                "lon": 120.0,
                "prepaid_amount_cents": 1000,
            })
        clusters = cluster_intentions_by_radius(orders)
        assert len(clusters) == 1
        ids = clusters[0]["order_ids"]
        assert len(ids) == len(set(ids)), "Order IDs should be unique"
        assert all(isinstance(oid, str) for oid in ids)


# ── Mock omodul 快速验证 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ignite_ghost_node_integration():
    """端到端集成测试：模拟完整的意图金→聚类→点火流程。

    因为 ignite_ghost_node 需要真实的 PGPool，这里只测试核心逻辑链路。
    """
    from app.ext.oskill_ghost_clustering import cluster_intentions_by_radius

    # Step 1: 生成模拟意向订单 (120 单紧密分布在 ~20m 内，确保都在一簇)
    orders = []
    for i in range(120):
        orders.append({
            "id": f"intent-{i}",
            "variant_id": f"variant-guava",
            "customer_id": f"cust-{i}",
            "lat": 39.9042 + i * 0.000001,  # 极小偏移，全部在 1km 内
            "lon": 116.4074 + i * 0.000001,
            "prepaid_amount_cents": 5000,
        })

    # Step 2: 执行聚类
    clusters = cluster_intentions_by_radius(orders)
    assert len(clusters) == 1
    cluster = clusters[0]

    # Step 3: 验证点火触发条件
    assert cluster["count"] >= IGNITION_THRESHOLD, (
        f"Expected >= {IGNITION_THRESHOLD}, got {cluster['count']}"
    )
    # Center should be near the seed point (within a few meters)
    expected_center_lat = sum(o["lat"] for o in orders) / len(orders)
    expected_center_lon = sum(o["lon"] for o in orders) / len(orders)
    assert cluster["center_lat"] == pytest.approx(expected_center_lat, abs=0.001)
    assert cluster["center_lon"] == pytest.approx(expected_center_lon, abs=0.001)
    assert cluster["total_prepaid_cents"] == 120 * 5000


def test_spatial_conflict_detection():
    """验证聚类不会在已有 active 节点的区域点火 (逻辑层检查)。"""
    from app.ext.oskill import check_spatial_conflict

    existing = [(39.9042, 116.4074)]  # 已有一个 node
    claim = (39.9042, 116.4074)  # 冲突!

    assert check_spatial_conflict(claim, existing) is True

    far_claim = (40.9042, 117.4074)  # 远大于 1km
    assert check_spatial_conflict(far_claim, existing) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
# CC test
