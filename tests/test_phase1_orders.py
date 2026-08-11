"""Phase 1 订单生命周期管理测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.orders.models import (
    InvalidOrderTransitionError,
    OrderSnapshot,
    OrderStatus,
    TERMINAL_STATUSES,
    VALID_ORDER_TRANSITIONS,
    validate_order_transition,
)


# ── 状态机测试 ──────────────────────────────────────────────────────


class TestOrderStateMachine:
    """订单状态机转换测试。"""

    def test_happy_path_full_lifecycle(self):
        """完整生命周期: pending → confirmed → processing → packed → shipped → delivered → completed"""
        path = [
            (OrderStatus.PENDING, OrderStatus.CONFIRMED),
            (OrderStatus.CONFIRMED, OrderStatus.PROCESSING),
            (OrderStatus.PROCESSING, OrderStatus.PACKED),
            (OrderStatus.PACKED, OrderStatus.SHIPPED),
            (OrderStatus.SHIPPED, OrderStatus.DELIVERED),
            (OrderStatus.DELIVERED, OrderStatus.COMPLETED),
        ]
        for from_s, to_s in path:
            validate_order_transition("test-order", from_s, to_s)

    def test_pending_cancel(self):
        """pending → cancelled (超时未支付 / 用户取消)"""
        validate_order_transition("o1", OrderStatus.PENDING, OrderStatus.CANCELLED)

    def test_confirmed_cancel(self):
        """confirmed → cancelled (商家取消)"""
        validate_order_transition("o1", OrderStatus.CONFIRMED, OrderStatus.CANCELLED)

    def test_processing_cancel(self):
        """processing → cancelled"""
        validate_order_transition("o1", OrderStatus.PROCESSING, OrderStatus.CANCELLED)

    def test_packed_cancel(self):
        """packed → cancelled (打包后仍可取消)"""
        validate_order_transition("o1", OrderStatus.PACKED, OrderStatus.CANCELLED)

    def test_shipped_return(self):
        """shipped → returning (退货)"""
        validate_order_transition("o1", OrderStatus.SHIPPED, OrderStatus.RETURNING)

    def test_delivered_return(self):
        """delivered → returning"""
        validate_order_transition("o1", OrderStatus.DELIVERED, OrderStatus.RETURNING)

    def test_returning_refund(self):
        """returning → refunded (退货成功)"""
        validate_order_transition("o1", OrderStatus.RETURNING, OrderStatus.REFUNDED)

    def test_returning_completed(self):
        """returning → completed (退货被拒, 交易完成)"""
        validate_order_transition("o1", OrderStatus.RETURNING, OrderStatus.COMPLETED)

    # ── 非法转换 ────────────────────────────────────────────────

    def test_invalid_pending_to_shipped(self):
        """pending 不能直接到 shipped"""
        with pytest.raises(InvalidOrderTransitionError):
            validate_order_transition("o1", OrderStatus.PENDING, OrderStatus.SHIPPED)

    def test_invalid_pending_to_delivered(self):
        with pytest.raises(InvalidOrderTransitionError):
            validate_order_transition("o1", OrderStatus.PENDING, OrderStatus.DELIVERED)

    def test_invalid_confirmed_to_shipped(self):
        """confirmed 不能跳过 processing/packed 直接发货"""
        with pytest.raises(InvalidOrderTransitionError):
            validate_order_transition("o1", OrderStatus.CONFIRMED, OrderStatus.SHIPPED)

    def test_invalid_cancelled_to_confirmed(self):
        """终态不能恢复"""
        with pytest.raises(InvalidOrderTransitionError):
            validate_order_transition(
                "o1", OrderStatus.CANCELLED, OrderStatus.CONFIRMED
            )

    def test_invalid_completed_to_cancelled(self):
        with pytest.raises(InvalidOrderTransitionError):
            validate_order_transition(
                "o1", OrderStatus.COMPLETED, OrderStatus.CANCELLED
            )

    def test_invalid_refunded_to_pending(self):
        with pytest.raises(InvalidOrderTransitionError):
            validate_order_transition("o1", OrderStatus.REFUNDED, OrderStatus.PENDING)

    def test_invalid_delivered_to_confirmed(self):
        """不能回退"""
        with pytest.raises(InvalidOrderTransitionError):
            validate_order_transition(
                "o1", OrderStatus.DELIVERED, OrderStatus.CONFIRMED
            )

    def test_invalid_processing_to_delivered(self):
        """不能跳过打包/发货"""
        with pytest.raises(InvalidOrderTransitionError):
            validate_order_transition(
                "o1", OrderStatus.PROCESSING, OrderStatus.DELIVERED
            )

    def test_invalid_pending_to_returning(self):
        """未发货不能退货"""
        with pytest.raises(InvalidOrderTransitionError):
            validate_order_transition("o1", OrderStatus.PENDING, OrderStatus.RETURNING)

    def test_invalid_confirmed_to_returning(self):
        with pytest.raises(InvalidOrderTransitionError):
            validate_order_transition(
                "o1", OrderStatus.CONFIRMED, OrderStatus.RETURNING
            )

    # ── 终态测试 ────────────────────────────────────────────────

    def test_terminal_statuses(self):
        """终态不能再转换"""
        for terminal in TERMINAL_STATUSES:
            assert not VALID_ORDER_TRANSITIONS.get(terminal), (
                f"{terminal.value} should be terminal but has transitions"
            )

    def test_terminal_set_completeness(self):
        """确认终态集合完整"""
        expected = {
            OrderStatus.COMPLETED,
            OrderStatus.CANCELLED,
            OrderStatus.REFUNDED,
            OrderStatus.FAILED,
        }
        assert TERMINAL_STATUSES == expected


# ── 数据模型测试 ───────────────────────────────────────────────────


class TestOrderSnapshot:
    def test_grand_total_yuan(self):
        order = OrderSnapshot(
            id="o1",
            status=OrderStatus.PENDING,
            grand_total_cents=1299,
        )
        assert order.grand_total_yuan == Decimal("12.99")

    def test_is_terminal(self):
        for status in TERMINAL_STATUSES:
            order = OrderSnapshot(id="o1", status=status)
            assert order.is_terminal

    def test_not_terminal(self):
        for status in [OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.SHIPPED]:
            order = OrderSnapshot(id="o1", status=status)
            assert not order.is_terminal

    def test_can_cancel(self):
        assert OrderSnapshot(id="o1", status=OrderStatus.PENDING).can_cancel
        assert OrderSnapshot(id="o1", status=OrderStatus.CONFIRMED).can_cancel
        assert OrderSnapshot(id="o1", status=OrderStatus.PROCESSING).can_cancel
        assert not OrderSnapshot(id="o1", status=OrderStatus.SHIPPED).can_cancel
        assert not OrderSnapshot(id="o1", status=OrderStatus.DELIVERED).can_cancel

    def test_can_return(self):
        assert not OrderSnapshot(id="o1", status=OrderStatus.PENDING).can_return
        assert not OrderSnapshot(id="o1", status=OrderStatus.CONFIRMED).can_return
        assert OrderSnapshot(id="o1", status=OrderStatus.SHIPPED).can_return
        assert OrderSnapshot(id="o1", status=OrderStatus.DELIVERED).can_return
        assert not OrderSnapshot(id="o1", status=OrderStatus.COMPLETED).can_return


# ── API 路由测试 ───────────────────────────────────────────────────


class TestOrderRoutes:
    """订单 API 路由基本测试。"""

    def test_order_routes_exist(self):
        """验证订单路由已注册。"""
        from app.main import app

        paths = app.openapi()["paths"]
        order_paths = [p for p in paths if p.startswith("/orders")]
        assert len(order_paths) >= 8, (
            f"expected >=8 order paths, got {len(order_paths)}"
        )

        expected_routes = [
            "/orders/",
            "/orders/{order_id}",
            "/orders/{order_id}/confirm",
            "/orders/{order_id}/cancel",
            "/orders/{order_id}/ship",
            "/orders/{order_id}/deliver",
            "/orders/{order_id}/complete",
            "/orders/{order_id}/history",
            "/orders/stats/summary",
        ]
        for route in expected_routes:
            assert route in paths, f"missing order route: {route}"

    def test_orders_require_auth(self, client):
        """后台订单端点无员工 JWT 一律 401。"""
        r = client.post("/orders/test-id/confirm", json={})
        assert r.status_code == 401

    def test_confirm_order_validation(self, client, auth_headers):
        """测试确认订单请求体校验。"""
        # 无 DB 时返回 503
        r = client.post(
            "/orders/test-id/confirm",
            json={"payment_intent_id": "pay-123"},
            headers=auth_headers,
        )
        assert r.status_code in (404, 503)  # 404 (order not found) or 503 (no DB)

    def test_cancel_order_validation(self, client, auth_headers):
        r = client.post(
            "/orders/test-id/cancel",
            json={"reason": "changed mind"},
            headers=auth_headers,
        )
        assert r.status_code in (404, 503)

    def test_ship_order_validation(self, client, auth_headers):
        r = client.post(
            "/orders/test-id/ship",
            json={"tracking_number": "SF123456", "carrier": "顺丰"},
            headers=auth_headers,
        )
        assert r.status_code in (404, 503)
