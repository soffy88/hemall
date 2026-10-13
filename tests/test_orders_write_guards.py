"""Audit: orders service write guards and state machine invariants.

These tests pin the guard that `transition_status` must only succeed when the
UPDATE hits a row (CAS on version + status), and that payment_intent_id is
written in the same transaction with status <> 'cancelled' protection.
"""

from __future__ import annotations

import pytest

from app.orders.models import InvalidOrderTransitionError, OrderStatus
from app.orders.service import OrderService, OrderNotFoundError
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

    with pytest.raises(Exception):  # ConcurrentOrderTransitionError or similar
        await svc.confirm_order(
            order_id="o-1",
            payment_intent_id="pi-123",
        )


async def test_cancel_order_releases_inventory():
    """cancel_order triggers _release_order_inventory in the same transaction."""
    from app.orders.service import OrderInventoryConflictError

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