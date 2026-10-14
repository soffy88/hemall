"""Audit P1-1: inventory write guards must fail loudly instead of silently.

`receive_stock` used to run

    UPDATE inventory_batch SET stock_qty = stock_qty + $1 WHERE id = $2

and throw away asyncpg's ``UPDATE n`` status string.  A non-existent, soft
deleted or non-active batch therefore produced a green 200 while writing a
``+quantity`` ``stock_movement`` row: the ledger and the real batch balance
silently diverged, forever, with no error anywhere.

These tests pin the guard, and pin the same class of guard on the other
write paths in the same modules that were audited alongside it.
"""

from __future__ import annotations

import pytest

from app.inventory.service import InventoryService
from tests.service_fakes import FakeConn, FakePool, Recording

pytestmark = pytest.mark.asyncio


def _svc(conn: FakeConn) -> InventoryService:
    return InventoryService(FakePool(conn))


async def test_receive_stock_rejects_batch_that_update_did_not_match():
    """UPDATE 0 rows must raise instead of writing a phantom receipt."""
    from app.inventory.service import InventoryBatchNotFoundError

    conn = FakeConn()
    # The guarded UPDATE ... RETURNING id matches nothing.
    conn.on(r"UPDATE\s+inventory_batch", None)
    svc = _svc(conn)

    with pytest.raises(InventoryBatchNotFoundError) as exc:
        await svc.receive_stock(
            product_id="p-1",
            quantity=10,
            location_id="loc-1",
            batch_id="00000000-0000-0000-0000-00000000dead",
        )

    assert "00000000-0000-0000-0000-00000000dead" in str(exc.value)


async def test_receive_stock_does_not_write_movement_when_batch_missing():
    """The phantom ``stock_movement`` row is the actual corruption; forbid it."""
    from app.inventory.service import InventoryBatchNotFoundError

    conn = FakeConn()
    conn.on(r"UPDATE\s+inventory_batch", None)
    svc = _svc(conn)

    with pytest.raises(InventoryBatchNotFoundError):
        await svc.receive_stock(
            product_id="p-1",
            quantity=10,
            location_id="loc-1",
            batch_id="missing",
        )

    assert not conn.did_issue(r"INSERT\s+INTO\s+stock_movement")


async def test_receive_stock_update_requires_active_batch():
    """Guard must be status-aware, not just id-aware: only active batches."""
    conn = FakeConn()
    conn.on(r"UPDATE\s+inventory_batch", Recording({"id": "batch-1"}))
    svc = _svc(conn)

    await svc.receive_stock(
        product_id="p-1",
        quantity=3,
        location_id="loc-1",
        batch_id="batch-1",
    )

    update_sql = conn.issued(r"UPDATE\s+inventory_batch")[0][0]
    assert "status = 'active'" in update_sql
    assert "RETURNING id" in update_sql


async def test_receive_stock_writes_movement_on_success():
    conn = FakeConn()
    conn.on(r"UPDATE\s+inventory_batch", Recording({"id": "batch-1"}))
    conn.on(r"INSERT\s+INTO\s+stock_movement", "movement-1")
    svc = _svc(conn)

    movement_id = await svc.receive_stock(
        product_id="p-1",
        quantity=3,
        location_id="loc-1",
        batch_id="batch-1",
    )

    assert movement_id == "movement-1"
    assert conn.did_issue(r"INSERT\s+INTO\s+stock_movement")
    assert conn.committed == 1
    assert conn.rolled_back == 0

async def test_deduct_stock_requires_reservation_row_update_to_match():
    """`deduct_stock` guarded inventory_batch but not inventory_reservation.

    A silent 0-row reservation update leaves consumed_qty at its old value
    while stock_qty was already decremented, so the same units can be
    deducted (and shipped) a second time.
    """
    from app.inventory.service import ReservationConflictError

    conn = FakeConn()
    conn.on(r"SELECT\s+COALESCE\(SUM\(quantity\)", 0)
    conn.on(
        r"FROM\s+inventory_reservation",
        [Recording({"id": "res-1", "batch_id": "batch-1", "quantity": 5,
                    "consumed_qty": 0, "released_qty": 0,
                    "variant_id": None, "location_id": "loc-1"})],
    )
    conn.on(r"UPDATE\s+inventory_batch", Recording({"id": "batch-1"}))
    # Guarded UPDATE inventory_reservation ... RETURNING id matches nothing.
    conn.on(r"UPDATE\s+inventory_reservation", None)
    svc = _svc(conn)

    with pytest.raises(ReservationConflictError):
        await svc.deduct_stock(
            order_id="o-1", product_id="p-1", quantity=5,
            location_id="loc-1", variant_id=None,
        )


async def test_release_reservation_requires_reservation_row_update_to_match():
    from app.inventory.service import ReservationConflictError

    conn = FakeConn()
    conn.on(r"SELECT\s+COALESCE\(SUM\(quantity\)", 0)
    conn.on(
        r"FROM\s+inventory_reservation",
        [Recording({"id": "res-1", "batch_id": "batch-1", "quantity": 5,
                    "consumed_qty": 0, "released_qty": 0,
                    "variant_id": None, "location_id": "loc-1"})],
    )
    conn.on(r"UPDATE\s+inventory_batch", Recording({"id": "batch-1"}))
    conn.on(r"UPDATE\s+inventory_reservation", None)
    svc = _svc(conn)

    with pytest.raises(ReservationConflictError):
        await svc.release_reservation(
            order_id="o-1", product_id="p-1", quantity=5,
            location_id="loc-1", variant_id=None,
        )


# ── Query methods ──────────────────────────────────────────────────

async def test_get_stock_returns_snapshots():
    conn = FakeConn()
    conn.on(
        r"FROM\s+inventory_batch",
        [Recording({"batch_id": "batch-1", "product_id": "p-1", "variant_id": None,
                    "location_id": "loc-1", "stock_qty": 10, "reserved_qty": 2,
                    "location_name": "Warehouse A"})],
    )
    svc = _svc(conn)

    snapshots = await svc.get_stock(product_id="p-1", location_id="loc-1")

    assert len(snapshots) == 1
    assert snapshots[0].batch_id == "batch-1"
    assert snapshots[0].total_qty == 10
    assert snapshots[0].available_qty == 8
    assert snapshots[0].reserved_qty == 2


async def test_get_stock_with_variant_id():
    conn = FakeConn()
    conn.on(
        r"FROM\s+inventory_batch",
        [Recording({"batch_id": "batch-1", "product_id": "p-1", "variant_id": "var-1",
                    "location_id": "loc-1", "stock_qty": 5, "reserved_qty": 1,
                    "location_name": "Warehouse A"})],
    )
    svc = _svc(conn)

    snapshots = await svc.get_stock(product_id="p-1", location_id="loc-1", variant_id="var-1")

    assert len(snapshots) == 1
    assert snapshots[0].variant_id == "var-1"


async def test_get_available_stock_sums_across_batches():
    conn = FakeConn()
    conn.on(
        r"FROM\s+inventory_batch",
        [
            Recording({"batch_id": "batch-1", "product_id": "p-1", "variant_id": None,
                       "location_id": "loc-1", "stock_qty": 10, "reserved_qty": 2,
                       "location_name": "Warehouse A"}),
            Recording({"batch_id": "batch-2", "product_id": "p-1", "variant_id": None,
                       "location_id": "loc-1", "stock_qty": 5, "reserved_qty": 0,
                       "location_name": "Warehouse A"}),
        ],
    )
    svc = _svc(conn)

    available = await svc.get_available_stock(product_id="p-1", location_id="loc-1")

    assert available == 13  # (10-2) + (5-0)


async def test_check_availability_returns_true_when_enough():
    conn = FakeConn()
    conn.on(
        r"FROM\s+inventory_batch",
        [Recording({"batch_id": "batch-1", "product_id": "p-1", "variant_id": None,
                    "location_id": "loc-1", "stock_qty": 10, "reserved_qty": 2,
                    "location_name": "Warehouse A"})],
    )
    svc = _svc(conn)

    assert await svc.check_availability("p-1", "loc-1", 5) is True
    assert await svc.check_availability("p-1", "loc-1", 8) is True
    assert await svc.check_availability("p-1", "loc-1", 9) is False


async def test_get_stock_summary_filters_by_product_and_location():
    conn = FakeConn()
    conn.on(
        r"FROM\s+inventory_batch",
        [Recording({"product_id": "p-1", "variant_id": None, "location_id": "loc-1",
                    "total_qty": 15, "reserved_qty": 3, "available_qty": 12,
                    "location_name": "Warehouse A", "batch_count": 2})],
    )
    svc = _svc(conn)

    summary = await svc.get_stock_summary(product_id="p-1", location_id="loc-1")

    assert len(summary) == 1
    assert summary[0]["product_id"] == "p-1"
    assert summary[0]["available_qty"] == 12


async def test_get_stock_summary_low_stock_only():
    conn = FakeConn()
    # HAVING clause filters to available <= threshold (default 10)
    conn.on(
        r"FROM\s+inventory_batch",
        [Recording({"product_id": "p-1", "variant_id": None, "location_id": "loc-1",
                    "total_qty": 8, "reserved_qty": 1, "available_qty": 7,
                    "location_name": "Warehouse A", "batch_count": 1})],
    )
    svc = _svc(conn)

    summary = await svc.get_stock_summary(low_stock_only=True)

    assert len(summary) == 1
    assert summary[0]["available_qty"] == 7


# ── Reserve stock (success paths) ──────────────────────────────────

async def test_reserve_stock_allocations_success():
    conn = FakeConn()
    # Advisory lock
    conn.on(r"SELECT\s+pg_advisory_xact_lock", None)
    # No existing reservation
    conn.on(r"FROM\s+inventory_reservation", [])
    # Available batches (first query in reserve_stock_allocations) - matches FOR UPDATE
    conn.on(
        r"FROM\s+inventory_batch.*FOR UPDATE",
        [Recording({"id": "batch-1", "stock_qty": 10, "reserved_qty": 0,
                    "location_id": "loc-1", "variant_id": None})],
    )
    # UPDATE inventory_batch returns row
    conn.on(r"UPDATE\s+inventory_batch", Recording({"id": "batch-1", "location_id": "loc-1", "variant_id": None}))
    # INSERT inventory_reservation
    conn.on(r"INSERT\s+INTO\s+inventory_reservation", "res-1")
    # INSERT stock_movement
    conn.on(r"INSERT\s+INTO\s+stock_movement", "mov-1")
    # _check_safety_stock: get_available_stock -> get_stock (second query to inventory_batch) - matches JOIN stock_location
    conn.on(
        r"FROM\s+inventory_batch.*JOIN\s+stock_location",
        [Recording({"batch_id": "batch-1", "product_id": "p-1", "variant_id": None,
                    "location_id": "loc-1", "stock_qty": 7, "reserved_qty": 3,
                    "location_name": "Warehouse A"})],
    )
    # _check_safety_stock: product_safety_stock
    conn.on(r"FROM\s+product_safety_stock", None)
    svc = _svc(conn)

    allocations = await svc.reserve_stock_allocations(
        order_id="o-1", product_id="p-1", quantity=3,
        location_id="loc-1", variant_id=None,
    )

    assert len(allocations) == 1
    assert allocations[0]["batch_id"] == "batch-1"
    assert allocations[0]["quantity"] == 3
    assert conn.committed == 1


async def test_reserve_stock_allocations_reuses_existing():
    conn = FakeConn()
    conn.on(r"SELECT\s+pg_advisory_xact_lock", None)
    # Existing reservation with matching quantity (query returns id AS reservation_id)
    conn.on(
        r"FROM\s+inventory_reservation",
        [Recording({"reservation_id": "res-1", "batch_id": "batch-1", "quantity": 3,
                    "product_id": "p-1", "variant_id": None, "location_id": "loc-1"})],
    )
    svc = _svc(conn)

    allocations = await svc.reserve_stock_allocations(
        order_id="o-1", product_id="p-1", quantity=3,
        location_id="loc-1", variant_id=None,
    )

    assert len(allocations) == 1
    assert allocations[0]["reservation_id"] == "res-1"


async def test_reserve_stock_allocations_fails_on_insufficient_stock():
    from app.inventory.service import InsufficientStockError

    conn = FakeConn()
    conn.on(r"SELECT\s+pg_advisory_xact_lock", None)
    conn.on(r"FROM\s+inventory_reservation", [])
    conn.on(
        r"FROM\s+inventory_batch",
        [Recording({"id": "batch-1", "stock_qty": 2, "reserved_qty": 0,
                    "location_id": "loc-1", "variant_id": None})],
    )
    svc = _svc(conn)

    with pytest.raises(InsufficientStockError):
        await svc.reserve_stock_allocations(
            order_id="o-1", product_id="p-1", quantity=5,
            location_id="loc-1", variant_id=None,
        )


# ── Deduct stock (success path) ────────────────────────────────────

async def test_deduct_stock_success():
    conn = FakeConn()
    # already_shipped = 0
    conn.on(r"SELECT\s+COALESCE\(SUM\(quantity\)", 0)
    # Reservations
    conn.on(
        r"FROM\s+inventory_reservation",
        [Recording({"id": "res-1", "quantity": 5, "consumed_qty": 0, "released_qty": 0,
                    "batch_id": "batch-1", "product_id": "p-1", "variant_id": None,
                    "location_id": "loc-1"})],
    )
    # UPDATE inventory_batch
    conn.on(r"UPDATE\s+inventory_batch", Recording({"id": "batch-1"}))
    # UPDATE inventory_reservation
    conn.on(r"UPDATE\s+inventory_reservation", Recording({"id": "res-1"}))
    # INSERT stock_movement
    conn.on(r"INSERT\s+INTO\s+stock_movement", "mov-1")
    svc = _svc(conn)

    await svc.deduct_stock(
        order_id="o-1", product_id="p-1", quantity=3,
        location_id="loc-1", variant_id=None,
    )

    assert conn.did_issue(r"UPDATE\s+inventory_batch")
    assert conn.did_issue(r"INSERT\s+INTO\s+stock_movement")
    assert conn.committed == 1


async def test_deduct_stock_skips_when_already_shipped():
    conn = FakeConn()
    # already_shipped = 5 (>= requested 3)
    conn.on(r"SELECT\s+COALESCE\(SUM\(quantity\)", 5)
    svc = _svc(conn)

    await svc.deduct_stock(
        order_id="o-1", product_id="p-1", quantity=3,
        location_id="loc-1", variant_id=None,
    )

    assert not conn.did_issue(r"UPDATE\s+inventory_batch")
    # Transaction still commits (early return inside transaction block)
    assert conn.committed == 1


# ── Release reservation (success path) ─────────────────────────────

async def test_release_reservation_success():
    conn = FakeConn()
    # already_released = 0
    conn.on(r"SELECT\s+COALESCE\(SUM\(quantity\)", 0)
    # Reservations
    conn.on(
        r"FROM\s+inventory_reservation",
        [Recording({"id": "res-1", "quantity": 5, "consumed_qty": 0, "released_qty": 0,
                    "batch_id": "batch-1", "product_id": "p-1", "variant_id": None,
                    "location_id": "loc-1"})],
    )
    # UPDATE inventory_batch
    conn.on(r"UPDATE\s+inventory_batch", Recording({"id": "batch-1"}))
    # UPDATE inventory_reservation
    conn.on(r"UPDATE\s+inventory_reservation", Recording({"id": "res-1"}))
    # INSERT stock_movement
    conn.on(r"INSERT\s+INTO\s+stock_movement", "mov-1")
    svc = _svc(conn)

    await svc.release_reservation(
        order_id="o-1", product_id="p-1", quantity=3,
        location_id="loc-1", variant_id=None,
    )

    assert conn.did_issue(r"UPDATE\s+inventory_batch")
    assert conn.did_issue(r"INSERT\s+INTO\s+stock_movement")
    assert conn.committed == 1


async def test_release_reservation_skips_when_already_released():
    conn = FakeConn()
    conn.on(r"SELECT\s+COALESCE\(SUM\(quantity\)", 5)
    svc = _svc(conn)

    await svc.release_reservation(
        order_id="o-1", product_id="p-1", quantity=3,
        location_id="loc-1", variant_id=None,
    )

    assert not conn.did_issue(r"UPDATE\s+inventory_batch")
    # Transaction still commits (early return inside transaction block)
    assert conn.committed == 1


# ── _check_safety_stock ────────────────────────────────────────────

async def test_check_safety_stock_dispatches_alert_when_below_threshold():
    from app.inventory.models import StockAlert

    conn = FakeConn()
    # get_available_stock returns 5
    conn.on(
        r"FROM\s+inventory_batch",
        [Recording({"batch_id": "batch-1", "product_id": "p-1", "variant_id": None,
                    "location_id": "loc-1", "stock_qty": 5, "reserved_qty": 0,
                    "location_name": "Warehouse A"})],
    )
    # product_safety_stock returns threshold 10
    conn.on(r"FROM\s+product_safety_stock", Recording({"safety_threshold": 10}))
    svc = _svc(conn)

    alerts = await svc._check_safety_stock("p-1", None, "loc-1", event_engine=None)

    assert alerts is not None
    assert len(alerts) == 1
    assert isinstance(alerts[0], StockAlert)
    assert alerts[0].alert_type == "warning"


async def test_check_safety_stock_returns_none_when_above_threshold():
    conn = FakeConn()
    conn.on(
        r"FROM\s+inventory_batch",
        [Recording({"batch_id": "batch-1", "product_id": "p-1", "variant_id": None,
                    "location_id": "loc-1", "stock_qty": 20, "reserved_qty": 0,
                    "location_name": "Warehouse A"})],
    )
    conn.on(r"FROM\s+product_safety_stock", Recording({"safety_threshold": 10}))
    svc = _svc(conn)

    alerts = await svc._check_safety_stock("p-1", None, "loc-1", event_engine=None)

    assert alerts is None


async def test_check_safety_stock_critical_when_zero():

    conn = FakeConn()
    conn.on(
        r"FROM\s+inventory_batch",
        [Recording({"batch_id": "batch-1", "product_id": "p-1", "variant_id": None,
                    "location_id": "loc-1", "stock_qty": 0, "reserved_qty": 0,
                    "location_name": "Warehouse A"})],
    )
    conn.on(r"FROM\s+product_safety_stock", Recording({"safety_threshold": 10}))
    svc = _svc(conn)

    alerts = await svc._check_safety_stock("p-1", None, "loc-1", event_engine=None)

    assert alerts is not None
    assert alerts[0].alert_type == "critical"


# ── check_all_safety_stock ────────────────────────────────────────

async def test_check_all_safety_stock_returns_alerts():
    from app.inventory.models import StockAlert

    conn = FakeConn()
    conn.on(
        r"FROM\s+inventory_batch",
        [Recording({"product_id": "p-1", "variant_id": None, "location_id": "loc-1",
                    "threshold": 10, "available": 5})],
    )
    svc = _svc(conn)

    alerts = await svc.check_all_safety_stock()

    assert len(alerts) == 1
    assert isinstance(alerts[0], StockAlert)
    assert alerts[0].product_id == "p-1"
    assert alerts[0].alert_type == "warning"


# ── get_movement_history ──────────────────────────────────────────

async def test_get_movement_history_returns_movements():
    conn = FakeConn()
    conn.on(
        r"FROM\s+stock_movement",
        [Recording({"id": "mov-1", "product_id": "p-1", "variant_id": None,
                    "location_id": "loc-1", "batch_id": "batch-1",
                    "movement_type": "receipt", "quantity": 10, "unit_cost": None,
                    "reference_type": None, "reference_id": None, "reason": None,
                    "operator_id": None, "created_at": "2024-01-01T00:00:00"})],
    )
    svc = _svc(conn)

    history = await svc.get_movement_history("p-1", location_id="loc-1", limit=10)

    assert len(history) == 1
    assert history[0]["id"] == "mov-1"
    assert history[0]["movement_type"] == "receipt"


# ── set_safety_threshold ──────────────────────────────────────────

async def test_set_safety_threshold_executes_upsert():
    conn = FakeConn()
    conn.on(r"INSERT\s+INTO\s+product_safety_stock", "OK")
    svc = _svc(conn)

    await svc.set_safety_threshold("p-1", "loc-1", 20, variant_id="var-1", alert_email="test@example.com")

    assert conn.did_issue(r"INSERT\s+INTO\s+product_safety_stock")
    # No transaction used, so committed stays 0
