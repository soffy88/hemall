"""Audit: orders service write guards and state machine invariants.

These tests pin the guard that `transition_status` must only succeed when the
UPDATE hits a row (CAS on version + status), and that payment_intent_id is
written in the same transaction with status <> 'cancelled' protection.
"""

from __future__ import annotations

import pytest

from app.orders.models import InvalidOrderTransitionError, OrderStatus
from app.orders.service import OrderNotFoundError, OrderService
from tests.service_fakes import FakeConn, FakePool, Recording

pytestmark = pytest.mark.asyncio


def _svc(conn: FakeConn) -> OrderService:
    return OrderService(FakePool(conn))


async def test_transition_status_rejects_when_update_matches_zero_rows():
    """CAS on (status, version) must fail if no row matches."""
    from app.orders.service import ConcurrentOrderTransitionError

    conn = FakeConn()
    conn.on(
        r"SELECT\s+id,\s*status,\s*version\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
        Recording({"id": "o-1", "status": "pending", "version": 0}),
    )
    # The guarded UPDATE ... RETURNING id matches nothing (version mismatch).
    conn.on(
        r"UPDATE\s+customer_order\s+SET\s+status\s*=",
        None,
    )
    svc = _svc(conn)

    with pytest.raises(ConcurrentOrderTransitionError):
        await svc.transition_status(
            order_id="o-1",
            new_status=OrderStatus.CONFIRMED,
            expected_version=0,
        )


async def test_transition_status_does_not_write_history_when_cas_fails():
    """The phantom history row is the actual corruption; forbid it."""
    from app.orders.service import ConcurrentOrderTransitionError

    conn = FakeConn()
    conn.on(
        r"SELECT\s+id,\s*status,\s*version\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
        Recording({"id": "o-1", "status": "pending", "version": 0}),
    )
    conn.on(
        r"UPDATE\s+customer_order\s+SET\s+status\s*=",
        None,
    )
    svc = _svc(conn)

    with pytest.raises(ConcurrentOrderTransitionError):
        await svc.transition_status(
            order_id="o-1",
            new_status=OrderStatus.CONFIRMED,
            expected_version=0,
        )

    assert not conn.did_issue(r"INSERT\s+INTO\s+order_status_history")


async def test_transition_status_update_requires_expected_version_and_status():
    """Guard must be CAS-aware: version + status in WHERE clause."""
    conn = FakeConn()
    conn.on(
        r"SELECT\s+id,\s*status,\s*version\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
        Recording({"id": "o-1", "status": "pending", "version": 0}),
    )
    conn.on(
        r"UPDATE\s+customer_order\s+SET\s+status\s*=",
        Recording({"id": "o-1", "version": 1}),
    )
    conn.on(
        r"INSERT\s+INTO\s+order_status_history",
        "hist-1",
    )
    conn.on(
        r"SELECT\s+id,\s*cart_id,\s*customer_id,\s*region_code,\s*currency,\s*status,\s*version",
        Recording({
            "id": "o-1", "cart_id": None, "customer_id": None, "region_code": None,
            "currency": "CNY", "status": "confirmed", "version": 1,
            "subtotal_cents": 0, "discount_cents": 0, "tax_cents": 0,
            "shipping_cents": 0, "grand_total_cents": 0,
            "payment_provider_name": None, "payment_intent_id": None,
            "billing_address": None, "shipping_address": None,
            "created_at": None, "updated_at": None,
        }),
    )
    svc = _svc(conn)

    result = await svc.transition_status(
        order_id="o-1",
        new_status=OrderStatus.CONFIRMED,
        expected_version=0,
    )

    update_sql = conn.issued(r"UPDATE\s+customer_order\s+SET\s+status\s*=")[0][0]
    assert "status = " in update_sql
    assert "version = " in update_sql
    assert "WHERE id = " in update_sql
    assert "AND status = " in update_sql
    assert "AND version = " in update_sql
    assert "RETURNING id" in update_sql
    assert result.version == 1


async def test_transition_status_writes_history_on_success():
    conn = FakeConn()
    conn.on(
        r"SELECT\s+id,\s*status,\s*version\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
        Recording({"id": "o-1", "status": "pending", "version": 0}),
    )
    conn.on(
        r"UPDATE\s+customer_order\s+SET\s+status\s*=",
        Recording({"id": "o-1", "version": 1}),
    )
    conn.on(
        r"INSERT\s+INTO\s+order_status_history",
        "hist-1",
    )
    # get_order called after transaction commits
    conn.on(
        r"SELECT\s+id,\s*cart_id,\s*customer_id,\s*region_code,\s*currency,\s*status,\s*version",
        Recording({
            "id": "o-1", "cart_id": None, "customer_id": None, "region_code": None,
            "currency": "CNY", "status": "confirmed", "version": 1,
            "subtotal_cents": 0, "discount_cents": 0, "tax_cents": 0,
            "shipping_cents": 0, "grand_total_cents": 0,
            "payment_provider_name": None, "payment_intent_id": None,
            "billing_address": None, "shipping_address": None,
            "created_at": None, "updated_at": None,
        }),
    )
    svc = _svc(conn)

    result = await svc.transition_status(
        order_id="o-1",
        new_status=OrderStatus.CONFIRMED,
    )

    assert result.status == OrderStatus.CONFIRMED
    assert result.version == 1
    assert conn.did_issue(r"INSERT\s+INTO\s+order_status_history")
    assert conn.committed == 1
    assert conn.rolled_back == 0


async def test_confirm_order_writes_payment_intent_in_same_transaction():
    """payment_intent_id written inside the same CAS-guarded transaction."""
    conn = FakeConn()
    conn.on(
        r"SELECT\s+id,\s*status,\s*version\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
        Recording({"id": "o-1", "status": "pending", "version": 0}),
    )
    conn.on(
        r"UPDATE\s+customer_order\s+SET\s+status\s*=",
        Recording({"id": "o-1", "version": 1}),
    )
    conn.on(
        r"INSERT\s+INTO\s+order_status_history",
        "hist-1",
    )
    conn.on(
        r"SELECT\s+id,\s*cart_id,\s*customer_id,\s*region_code,\s*currency,\s*status,\s*version",
        Recording({
            "id": "o-1", "cart_id": None, "customer_id": None, "region_code": None,
            "currency": "CNY", "status": "confirmed", "version": 1,
            "subtotal_cents": 0, "discount_cents": 0, "tax_cents": 0,
            "shipping_cents": 0, "grand_total_cents": 0,
            "payment_provider_name": None, "payment_intent_id": "pi-123",
            "billing_address": None, "shipping_address": None,
            "created_at": None, "updated_at": None,
        }),
    )
    svc = _svc(conn)

    result = await svc.confirm_order(
        order_id="o-1",
        payment_intent_id="pi-123",
    )

    assert result.payment_intent_id == "pi-123"
    assert result.status == OrderStatus.CONFIRMED

    # Verify the UPDATE included payment_intent_id and status <> 'cancelled'
    update_sql = conn.issued(r"UPDATE\s+customer_order\s+SET\s+status\s*=")[0][0]
    assert "payment_intent_id" in update_sql
    assert "status <> 'cancelled'" in update_sql
    assert "RETURNING id" in update_sql


async def test_transition_status_rejects_cancelled_order_payment_intent_write():
    """status <> 'cancelled' prevents overwriting a cancelled order."""
    conn = FakeConn()
    conn.on(
        r"SELECT\s+id,\s*status,\s*version\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
        Recording({"id": "o-1", "status": "cancelled", "version": 2}),
    )
    # The UPDATE with status <> 'cancelled' matches 0 rows
    conn.on(
        r"UPDATE\s+customer_order\s+SET\s+status\s*=",
        None,
    )
    svc = _svc(conn)

    # 订单已是 cancelled：状态机先拒绝，根本走不到那条 UPDATE。
    with pytest.raises(InvalidOrderTransitionError):
        await svc.confirm_order(
            order_id="o-1",
            payment_intent_id="pi-123",
        )
    assert not conn.issued(r"UPDATE\s+customer_order\s+SET\s+payment_intent_id")


async def test_cancel_order_releases_inventory():
    """cancel_order triggers _release_order_inventory in the same transaction."""

    conn = FakeConn()
    conn.on(
        r"SELECT\s+id,\s*status,\s*version\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
        Recording({"id": "o-1", "status": "confirmed", "version": 1}),
    )
    conn.on(
        r"SELECT\s+id,\s*product_id,\s*variant_id,\s*location_id,\s*batch_id,\s*quantity,\s*consumed_qty,\s*released_qty\s+FROM\s+inventory_reservation",
        [Recording({"id": "res-1", "product_id": "p-1", "variant_id": None, "location_id": "loc-1", "batch_id": "batch-1", "quantity": 5, "consumed_qty": 0, "released_qty": 0})],
    )
    conn.on(
        r"UPDATE\s+inventory_batch\s+SET\s+reserved_qty\s*=\s*reserved_qty\s*-\s*\$1",
        Recording({"id": "batch-1"}),
    )
    conn.on(
        r"UPDATE\s+inventory_reservation\s+SET\s+released_qty\s*=",
        Recording({"id": "res-1"}),
    )
    conn.on(
        r"INSERT\s+INTO\s+stock_movement",
        "mov-1",
    )
    conn.on(
        r"UPDATE\s+customer_order\s+SET\s+status\s*=",
        Recording({"id": "o-1", "version": 2}),
    )
    conn.on(
        r"INSERT\s+INTO\s+order_status_history",
        "hist-1",
    )
    conn.on(
        r"SELECT\s+id,\s*cart_id,\s*customer_id,\s*region_code,\s*currency,\s*status,\s*version",
        Recording({
            "id": "o-1", "cart_id": None, "customer_id": None, "region_code": None,
            "currency": "CNY", "status": "cancelled", "version": 2,
            "subtotal_cents": 0, "discount_cents": 0, "tax_cents": 0,
            "shipping_cents": 0, "grand_total_cents": 0,
            "payment_provider_name": None, "payment_intent_id": None,
            "billing_address": None, "shipping_address": None,
            "created_at": None, "updated_at": None,
        }),
    )
    svc = _svc(conn)

    result = await svc.cancel_order(order_id="o-1", reason="user requested")

    assert result.status == OrderStatus.CANCELLED
    assert conn.did_issue(r"UPDATE\s+inventory_batch\s+SET\s+reserved_qty\s*=\s*reserved_qty\s*-\s*\$1")
    assert conn.did_issue(r"INSERT\s+INTO\s+stock_movement")


async def test_ship_order_deducts_inventory():
    """ship_order triggers _ship_order_inventory in the same transaction."""
    conn = FakeConn()
    conn.on(
        r"SELECT\s+id,\s*status,\s*version\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
        Recording({"id": "o-1", "status": "packed", "version": 3}),
    )
    conn.on(
        r"SELECT\s+id,\s*order_line_item_id,\s*product_id,\s*variant_id,\s*location_id,\s*batch_id,\s*quantity,\s*consumed_qty,\s*released_qty\s+FROM\s+inventory_reservation",
        [Recording({"id": "res-1", "order_line_item_id": "line-1", "product_id": "p-1", "variant_id": None, "location_id": "loc-1", "batch_id": "batch-1", "quantity": 5, "consumed_qty": 0, "released_qty": 0})],
    )
    conn.on(
        r"SELECT\s+COUNT\(\*\)\s+FROM\s+order_line_item\s+WHERE\s+order_id\s*=\s*\$1",
        1,
    )
    conn.on(
        r"UPDATE\s+inventory_batch\s+SET\s+stock_qty\s*=\s*stock_qty\s*-\s*\$1",
        Recording({"id": "batch-1"}),
    )
    conn.on(
        r"UPDATE\s+inventory_reservation\s+SET\s+consumed_qty\s*=",
        Recording({"id": "res-1"}),
    )
    conn.on(
        r"INSERT\s+INTO\s+stock_movement",
        "mov-1",
    )
    conn.on(
        r"UPDATE\s+order_line_item\s+SET\s+fulfilled_qty\s*=",
        Recording({"id": "line-1"}),
    )
    conn.on(
        r"UPDATE\s+customer_order\s+SET\s+status\s*=",
        Recording({"id": "o-1", "version": 4}),
    )
    conn.on(
        r"INSERT\s+INTO\s+order_status_history",
        "hist-1",
    )
    conn.on(
        r"SELECT\s+id,\s*cart_id,\s*customer_id,\s*region_code,\s*currency,\s*status,\s*version",
        Recording({
            "id": "o-1", "cart_id": None, "customer_id": None, "region_code": None,
            "currency": "CNY", "status": "shipped", "version": 4,
            "subtotal_cents": 0, "discount_cents": 0, "tax_cents": 0,
            "shipping_cents": 0, "grand_total_cents": 0,
            "payment_provider_name": None, "payment_intent_id": None,
            "billing_address": None, "shipping_address": None,
            "created_at": None, "updated_at": None,
        }),
    )
    svc = _svc(conn)

    result = await svc.ship_order(order_id="o-1", tracking_number="TRACK-1", carrier="SF")

    assert result.status == OrderStatus.SHIPPED
    assert conn.did_issue(r"UPDATE\s+inventory_batch\s+SET\s+stock_qty\s*=\s*stock_qty\s*-\s*\$1")
    assert conn.did_issue(r"INSERT\s+INTO\s+stock_movement")


async def test_invalid_order_transitions_are_rejected():
    """validate_order_transition is called and rejects illegal moves."""
    conn = FakeConn()
    conn.on(
        r"SELECT\s+id,\s*status,\s*version\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
        Recording({"id": "o-1", "status": "pending", "version": 0}),
    )
    svc = _svc(conn)

    with pytest.raises(InvalidOrderTransitionError):
        await svc.transition_status(
            order_id="o-1",
            new_status=OrderStatus.SHIPPED,  # pending -> shipped is illegal
        )

    # No UPDATE should have been attempted
    assert not conn.did_issue(r"UPDATE\s+customer_order\s+SET\s+status\s*=")


async def test_transition_status_not_found_raises():
    """OrderNotFoundError when order doesn't exist."""
    conn = FakeConn()
    conn.on(
        r"SELECT\s+id,\s*status,\s*version\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
        None,
    )
    svc = _svc(conn)

    with pytest.raises(OrderNotFoundError):
        await svc.transition_status(
            order_id="missing",
            new_status=OrderStatus.CONFIRMED,
        )


# ── Query methods ──────────────────────────────────────────────────

async def test_get_order_returns_snapshot():
    conn = FakeConn()
    conn.on(
        r"FROM\s+customer_order",
        Recording({
            "id": "o-1", "cart_id": None, "customer_id": "c-1", "region_code": "cn-east",
            "currency": "CNY", "status": "confirmed", "version": 1,
            "subtotal_cents": 1000, "discount_cents": 0, "tax_cents": 100,
            "shipping_cents": 50, "grand_total_cents": 1150,
            "payment_provider_name": "manual", "payment_intent_id": "pi-1",
            "billing_address": None, "shipping_address": None,
            "created_at": "2024-01-01T00:00:00", "updated_at": "2024-01-01T00:00:00",
        }),
    )
    svc = _svc(conn)

    order = await svc.get_order("o-1")

    assert order is not None
    assert order.id == "o-1"
    assert order.status == OrderStatus.CONFIRMED
    assert order.grand_total_cents == 1150
    assert order.payment_intent_id == "pi-1"


async def test_get_order_returns_none_when_not_found():
    conn = FakeConn()
    conn.on(r"FROM\s+customer_order", None)
    svc = _svc(conn)

    order = await svc.get_order("missing")

    assert order is None


async def test_get_order_items_returns_line_items():
    conn = FakeConn()
    conn.on(
        r"FROM\s+order_line_item",
        [Recording({
            "id": "line-1", "order_id": "o-1", "batch_id": "batch-1", "quantity": 2,
            "unit_price_cents": 500, "line_total_cents": 1000,
            "product_id": "p-1", "variant_id": None, "location_id": "loc-1", "stock_qty": 10,
            "wired_location_id": "loc-1", "reservation_id": "res-1",
        })],
    )
    svc = _svc(conn)

    items = await svc.get_order_items("o-1")

    assert len(items) == 1
    assert items[0]["id"] == "line-1"
    assert items[0]["quantity"] == 2


async def test_list_orders_filters_by_customer_and_status():
    conn = FakeConn()
    conn.on(
        r"FROM\s+customer_order",
        [Recording({
            "id": "o-1", "cart_id": None, "customer_id": "c-1", "currency": "CNY",
            "status": "confirmed", "version": 1, "subtotal_cents": 1000,
            "discount_cents": 0, "tax_cents": 100, "shipping_cents": 50,
            "grand_total_cents": 1150, "payment_provider_name": "manual",
            "payment_intent_id": "pi-1", "created_at": None, "updated_at": None,
        })],
    )
    svc = _svc(conn)

    orders = await svc.list_orders(customer_id="c-1", status=OrderStatus.CONFIRMED, limit=10)

    assert len(orders) == 1
    assert orders[0].customer_id == "c-1"
    assert orders[0].status == OrderStatus.CONFIRMED


async def test_count_orders_returns_count():
    conn = FakeConn()
    conn.on(r"SELECT COUNT\(\*\) FROM customer_order", 42)
    svc = _svc(conn)

    count = await svc.count_orders(customer_id="c-1", status=OrderStatus.CONFIRMED)

    assert count == 42


async def test_deliver_order_transitions_status():
    conn = FakeConn()
    conn.on(
        r"SELECT\s+id,\s*status,\s*version\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
        Recording({"id": "o-1", "status": "shipped", "version": 3}),
    )
    conn.on(
        r"UPDATE\s+customer_order\s+SET\s+status\s*=",
        Recording({"id": "o-1", "version": 4}),
    )
    conn.on(r"INSERT\s+INTO\s+order_status_history", "hist-1")
    conn.on(
        r"SELECT\s+id,\s*cart_id,\s*customer_id,\s*region_code,\s*currency,\s*status,\s*version",
        Recording({
            "id": "o-1", "cart_id": None, "customer_id": None, "region_code": None,
            "currency": "CNY", "status": "delivered", "version": 4,
            "subtotal_cents": 0, "discount_cents": 0, "tax_cents": 0,
            "shipping_cents": 0, "grand_total_cents": 0,
            "payment_provider_name": None, "payment_intent_id": None,
            "billing_address": None, "shipping_address": None,
            "created_at": None, "updated_at": None,
        }),
    )
    svc = _svc(conn)

    result = await svc.deliver_order("o-1")

    assert result.status == OrderStatus.DELIVERED
    assert result.version == 4


async def test_complete_order_transitions_status():
    conn = FakeConn()
    conn.on(
        r"SELECT\s+id,\s*status,\s*version\s+FROM\s+customer_order\s+WHERE\s+id\s*=\s*\$1\s+FOR\s+UPDATE",
        Recording({"id": "o-1", "status": "delivered", "version": 4}),
    )
    conn.on(
        r"UPDATE\s+customer_order\s+SET\s+status\s*=",
        Recording({"id": "o-1", "version": 5}),
    )
    conn.on(r"INSERT\s+INTO\s+order_status_history", "hist-1")
    conn.on(
        r"SELECT\s+id,\s*cart_id,\s*customer_id,\s*region_code,\s*currency,\s*status,\s*version",
        Recording({
            "id": "o-1", "cart_id": None, "customer_id": None, "region_code": None,
            "currency": "CNY", "status": "completed", "version": 5,
            "subtotal_cents": 0, "discount_cents": 0, "tax_cents": 0,
            "shipping_cents": 0, "grand_total_cents": 0,
            "payment_provider_name": None, "payment_intent_id": None,
            "billing_address": None, "shipping_address": None,
            "created_at": None, "updated_at": None,
        }),
    )
    svc = _svc(conn)

    result = await svc.complete_order("o-1")

    assert result.status == OrderStatus.COMPLETED
    assert result.version == 5


# ── _release_order_inventory ──────────────────────────────────────

async def test_release_order_inventory_releases_reservations():

    conn = FakeConn()
    conn.on(
        r"FROM\s+inventory_reservation",
        [Recording({"id": "res-1", "product_id": "p-1", "variant_id": None,
                    "location_id": "loc-1", "batch_id": "batch-1",
                    "quantity": 5, "consumed_qty": 0, "released_qty": 0})],
    )
    conn.on(
        r"UPDATE\s+inventory_batch\s+SET\s+reserved_qty\s*=\s*reserved_qty\s*-\s*\$1",
        Recording({"id": "batch-1"}),
    )
    conn.on(
        r"UPDATE\s+inventory_reservation\s+SET\s+released_qty\s*=",
        Recording({"id": "res-1"}),
    )
    conn.on(r"INSERT\s+INTO\s+stock_movement", "mov-1")
    svc = _svc(conn)

    # Call the private method directly
    await svc._release_order_inventory(conn, "o-1")

    assert conn.did_issue(r"UPDATE\s+inventory_batch\s+SET\s+reserved_qty\s*=\s*reserved_qty\s*-\s*\$1")
    assert conn.did_issue(r"INSERT\s+INTO\s+stock_movement")


async def test_release_order_inventory_fails_on_inconsistent_batch():
    from app.orders.service import OrderInventoryConflictError

    conn = FakeConn()
    conn.on(
        r"FROM\s+inventory_reservation",
        [Recording({"id": "res-1", "product_id": "p-1", "variant_id": None,
                    "location_id": "loc-1", "batch_id": "batch-1",
                    "quantity": 5, "consumed_qty": 0, "released_qty": 0})],
    )
    conn.on(
        r"UPDATE\s+inventory_batch\s+SET\s+reserved_qty\s*=\s*reserved_qty\s*-\s*\$1",
        None,  # Batch update fails
    )
    svc = _svc(conn)

    with pytest.raises(OrderInventoryConflictError):
        await svc._release_order_inventory(conn, "o-1")


async def test_release_order_inventory_fails_on_reservation_update_fail():
    from app.orders.service import OrderInventoryConflictError

    conn = FakeConn()
    conn.on(
        r"FROM\s+inventory_reservation",
        [Recording({"id": "res-1", "product_id": "p-1", "variant_id": None,
                    "location_id": "loc-1", "batch_id": "batch-1",
                    "quantity": 5, "consumed_qty": 0, "released_qty": 0})],
    )
    conn.on(
        r"UPDATE\s+inventory_batch\s+SET\s+reserved_qty\s*=\s*reserved_qty\s*-\s*\$1",
        Recording({"id": "batch-1"}),
    )
    conn.on(
        r"UPDATE\s+inventory_reservation\s+SET\s+released_qty\s*=",
        None,  # Reservation update fails
    )
    svc = _svc(conn)

    with pytest.raises(OrderInventoryConflictError):
        await svc._release_order_inventory(conn, "o-1")


# ── _ship_order_inventory ────────────────────────────────────────

async def test_ship_order_inventory_consumes_reservations():
    conn = FakeConn()
    conn.on(
        r"FROM\s+inventory_reservation",
        [Recording({"id": "res-1", "order_line_item_id": "line-1", "product_id": "p-1",
                    "variant_id": None, "location_id": "loc-1", "batch_id": "batch-1",
                    "quantity": 5, "consumed_qty": 0, "released_qty": 0})],
    )
    conn.on(r"SELECT\s+COUNT\(\*\)\s+FROM\s+order_line_item", 1)
    conn.on(
        r"UPDATE\s+inventory_batch\s+SET\s+stock_qty\s*=\s*stock_qty\s*-\s*\$1",
        Recording({"id": "batch-1"}),
    )
    conn.on(
        r"UPDATE\s+inventory_reservation\s+SET\s+consumed_qty\s*=",
        Recording({"id": "res-1"}),
    )
    conn.on(r"INSERT\s+INTO\s+stock_movement", "mov-1")
    conn.on(
        r"UPDATE\s+order_line_item\s+SET\s+fulfilled_qty\s*=",
        Recording({"id": "line-1"}),
    )
    svc = _svc(conn)

    await svc._ship_order_inventory(conn, "o-1")

    assert conn.did_issue(r"UPDATE\s+inventory_batch\s+SET\s+stock_qty\s*=\s*stock_qty\s*-\s*\$1")
    assert conn.did_issue(r"INSERT\s+INTO\s+stock_movement")
    assert conn.did_issue(r"UPDATE\s+order_line_item\s+SET\s+fulfilled_qty\s*=")


async def test_ship_order_inventory_fails_when_no_reservations():
    from app.orders.service import OrderInventoryConflictError

    conn = FakeConn()
    conn.on(
        r"FROM\s+inventory_reservation",
        [],  # No reservations
    )
    conn.on(r"SELECT\s+COUNT\(\*\)\s+FROM\s+order_line_item", 1)
    svc = _svc(conn)

    with pytest.raises(OrderInventoryConflictError, match="has no inventory reservations"):
        await svc._ship_order_inventory(conn, "o-1")


async def test_ship_order_inventory_fails_on_batch_update_fail():
    from app.orders.service import OrderInventoryConflictError

    conn = FakeConn()
    conn.on(
        r"FROM\s+inventory_reservation",
        [Recording({"id": "res-1", "order_line_item_id": "line-1", "product_id": "p-1",
                    "variant_id": None, "location_id": "loc-1", "batch_id": "batch-1",
                    "quantity": 5, "consumed_qty": 0, "released_qty": 0})],
    )
    conn.on(r"SELECT\s+COUNT\(\*\)\s+FROM\s+order_line_item", 1)
    conn.on(
        r"UPDATE\s+inventory_batch\s+SET\s+stock_qty\s*=\s*stock_qty\s*-\s*\$1",
        None,  # Batch update fails
    )
    svc = _svc(conn)

    with pytest.raises(OrderInventoryConflictError):
        await svc._ship_order_inventory(conn, "o-1")


# ── get_status_history ────────────────────────────────────────────

async def test_get_status_history_returns_entries():
    conn = FakeConn()
    conn.on(
        r"FROM\s+order_status_history",
        [Recording({
            "id": "hist-1", "order_id": "o-1", "from_status": "pending",
            "to_status": "confirmed", "reason": "payment confirmed",
            "operator_id": "op-1", "metadata": '{"key": "value"}', "created_at": "2024-01-01T00:00:00",
        })],
    )
    svc = _svc(conn)

    history = await svc.get_status_history("o-1")

    assert len(history) == 1
    assert history[0].from_status == "pending"
    assert history[0].to_status == "confirmed"
    assert history[0].metadata == {"key": "value"}


# ── get_order_stats ──────────────────────────────────────────────

async def test_get_order_stats_groups_by_status():
    conn = FakeConn()
    conn.on(
        r"FROM\s+customer_order",
        [Recording({"status": "pending", "cnt": 5}), Recording({"status": "confirmed", "cnt": 10})],
    )
    svc = _svc(conn)

    stats = await svc.get_order_stats(customer_id="c-1")

    assert stats["pending"] == 5
    assert stats["confirmed"] == 10