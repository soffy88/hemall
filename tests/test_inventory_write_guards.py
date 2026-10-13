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
