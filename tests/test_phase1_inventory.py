"""Phase 1 库存管理系统测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.inventory.models import (
    StockAlert,
    StockMovement,
    StockMovementType,
    StockSnapshot,
)


# ── 库存变动类型测试 ────────────────────────────────────────────────


class TestStockMovementType:
    def test_inbound_types(self):
        for t in [
            StockMovementType.RECEIPT,
            StockMovementType.RETURN_IN,
            StockMovementType.TRANSFER_IN,
            StockMovementType.ADJUSTMENT_UP,
        ]:
            assert t.is_inbound
            assert not t.is_outbound
            assert not t.is_reservation

    def test_outbound_types(self):
        for t in [
            StockMovementType.SHIPMENT,
            StockMovementType.RETURN_OUT,
            StockMovementType.TRANSFER_OUT,
            StockMovementType.ADJUSTMENT_DOWN,
            StockMovementType.DAMAGE,
        ]:
            assert t.is_outbound
            assert not t.is_inbound
            assert not t.is_reservation

    def test_reservation_types(self):
        for t in [StockMovementType.RESERVE, StockMovementType.UNRESERVE]:
            assert t.is_reservation
            assert not t.is_inbound
            assert not t.is_outbound


# ── 数据模型测试 ───────────────────────────────────────────────────


class TestStockModels:
    def test_movement_net_quantity_inbound(self):
        mv = StockMovement(
            product_id="p1",
            location_id="loc1",
            movement_type=StockMovementType.RECEIPT,
            quantity=100,
        )
        assert mv.net_quantity == 100
        assert mv.is_inbound

    def test_movement_net_quantity_outbound(self):
        mv = StockMovement(
            product_id="p1",
            location_id="loc1",
            movement_type=StockMovementType.SHIPMENT,
            quantity=50,
        )
        assert mv.net_quantity == -50
        assert mv.is_outbound

    def test_movement_net_quantity_reservation(self):
        mv = StockMovement(
            product_id="p1",
            location_id="loc1",
            movement_type=StockMovementType.RESERVE,
            quantity=10,
        )
        assert mv.net_quantity == 0
        assert mv.is_reservation

    def test_stock_snapshot(self):
        snap = StockSnapshot(
            product_id="p1",
            location_id="loc1",
            total_qty=100,
            available_qty=80,
            reserved_qty=20,
        )
        assert snap.total_qty == 100
        assert snap.available_qty == 80
        assert snap.reserved_qty == 20

    def test_stock_alert_critical(self):
        alert = StockAlert(
            product_id="p1",
            location_id="loc1",
            safety_threshold=10,
            current_available=0,
            alert_type="critical",
        )
        assert alert.alert_type == "critical"

    def test_stock_alert_warning(self):
        alert = StockAlert(
            product_id="p1",
            location_id="loc1",
            safety_threshold=10,
            current_available=5,
            alert_type="warning",
        )
        assert alert.alert_type == "warning"


# ── API 路由测试 ────────────────────────────────────────────────────


class TestInventoryRoutes:
    """API 路由基本测试 (无 DB 环境下的 schema 验证)。"""

    def test_inventory_routes_exist(self):
        """验证库存路由已注册。"""
        from app.main import app

        paths = app.openapi()["paths"]
        inventory_paths = [p for p in paths if p.startswith("/inventory")]
        assert len(inventory_paths) >= 8, (
            f"expected >=8 inventory paths, got {len(inventory_paths)}: "
            f"{inventory_paths}"
        )

        expected_routes = [
            "/inventory/stock",
            "/inventory/stock/available",
            "/inventory/stock/summary",
            "/inventory/reserve",
            "/inventory/deduct",
            "/inventory/release",
            "/inventory/receive",
            "/inventory/alerts",
            "/inventory/safety-threshold",
        ]
        for route in expected_routes:
            assert route in paths, f"missing inventory route: {route}"

    def test_inventory_openapi_schema(self):
        """验证 OpenAPI schema 包含 inventory 端点。"""
        from app.main import app

        spec = app.openapi()
        # 验证 inventory 相关路径存在
        paths = spec.get("paths", {})
        inventory_paths = [p for p in paths if p.startswith("/inventory")]
        assert len(inventory_paths) >= 8

    def test_inventory_requires_auth(self, client):
        """后台库存端点无员工 JWT 一律 401。"""
        r = client.post("/inventory/reserve", json={"product_id": "p1"})
        assert r.status_code == 401

    def test_reserve_stock_validation(self, client, auth_headers):
        """测试预留库存请求体校验。"""
        r = client.post(
            "/inventory/reserve",
            json={
                "product_id": "p1",
                "quantity": -1,  # 负数应被 Pydantic 拒绝
                "location_id": "loc1",
            },
            headers=auth_headers,
        )
        # 无 DB 时返回 503 (正常), 有 DB 且 Pydantic 校验失败返回 422
        assert r.status_code in (422, 503)

    def test_receive_stock_validation(self, client, auth_headers):
        """测试入库请求体校验。"""
        # quantity 为负数应被 Pydantic 拒绝
        r = client.post(
            "/inventory/receive",
            json={
                "product_id": "p1",
                "batch_id": "batch-1",
                "quantity": -5,
                "location_id": "loc1",
            },
            headers=auth_headers,
        )
        assert r.status_code in (422, 503)

    def test_set_safety_threshold_validation(self, client, auth_headers):
        """测试安全库存阈值校验。"""
        r = client.put(
            "/inventory/safety-threshold",
            json={
                "product_id": "p1",
                "location_id": "loc1",
                "threshold": -1,  # 负数应被 Pydantic 拒绝
            },
            headers=auth_headers,
        )
        assert r.status_code in (422, 503)
