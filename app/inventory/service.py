"""库存服务层 — 实时库存查询、预留/扣减/释放、出入库流水、安全库存预警。

与 obase inventory_batch 表的集成:
    inventory_batch 表已有 stock_qty / reserved_qty 字段, 本服务在其之上
    构建"出入库流水 (stock_movement)"层, 所有库存变动先写流水再更新批次表,
    保证审计可追溯性。

调用方式:
    from app.inventory.service import InventoryService

    svc = InventoryService(pool)
    snapshot = await svc.get_stock(product_id="p1", location_id=loc_id)
    await svc.reserve_stock(order_id="o1", product_id="p1", qty=2, ...)
"""

from __future__ import annotations

import logging
from typing import Any

from obase.persistence.pool import PgPool

from .models import (
    StockAlert,
    StockMovementType,
    StockSnapshot,
)

logger = logging.getLogger("hemall.inventory")


class InsufficientStockError(Exception):
    """库存不足。"""

    def __init__(
        self, product_id: str, variant_id: str | None, requested: int, available: int
    ) -> None:
        self.product_id = product_id
        self.variant_id = variant_id
        self.requested = requested
        self.available = available
        super().__init__(
            f"insufficient stock for {product_id} (variant={variant_id}): "
            f"requested {requested}, available {available}"
        )


class ReservationConflictError(Exception):
    """预留记录与库存账本不一致。"""


class InventoryService:
    """库存服务 — 封装所有库存相关操作。"""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    # ── 实时库存查询 ─────────────────────────────────────────────

    async def get_stock(
        self,
        product_id: str,
        location_id: str,
        variant_id: str | None = None,
    ) -> list[StockSnapshot]:
        """查询商品在指定仓库的库存快照 (支持多批次)。"""
        async with self._pool.acquire() as conn:
            if variant_id:
                rows = await conn.fetch(
                    """
                    SELECT ib.id as batch_id, pv.product_id, ib.variant_id,
                           ib.location_id, ib.stock_qty, ib.reserved_qty,
                           sl.name as location_name
                    FROM inventory_batch ib
                    JOIN product_variant pv ON pv.id = ib.variant_id
                    JOIN stock_location sl ON sl.id = ib.location_id
                    WHERE pv.product_id = $1 AND ib.variant_id = $2
                      AND ib.location_id = $3 AND ib.stock_qty > 0
                    ORDER BY ib.created_at ASC  -- FIFO: 老批次优先
                    """,
                    product_id,
                    variant_id,
                    location_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT ib.id as batch_id, pv.product_id, ib.variant_id,
                           ib.location_id, ib.stock_qty, ib.reserved_qty,
                           sl.name as location_name
                    FROM inventory_batch ib
                    JOIN product_variant pv ON pv.id = ib.variant_id
                    JOIN stock_location sl ON sl.id = ib.location_id
                    WHERE pv.product_id = $1
                      AND ib.location_id = $2 AND ib.stock_qty > 0
                    ORDER BY ib.created_at ASC
                    """,
                    product_id,
                    location_id,
                )

        return [
            StockSnapshot(
                product_id=str(r["product_id"]),
                variant_id=(str(r["variant_id"]) if r["variant_id"] else None),
                location_id=str(r["location_id"]),
                location_name=r["location_name"],
                total_qty=r["stock_qty"],
                available_qty=r["stock_qty"] - r["reserved_qty"],
                reserved_qty=r["reserved_qty"],
                batch_id=str(r["batch_id"]),
            )
            for r in rows
        ]

    async def get_available_stock(
        self,
        product_id: str,
        location_id: str,
        variant_id: str | None = None,
    ) -> int:
        """查询可用库存总量 (跨批次汇总)。"""
        snapshots = await self.get_stock(product_id, location_id, variant_id)
        return sum(s.available_qty for s in snapshots)

    async def check_availability(
        self,
        product_id: str,
        location_id: str,
        quantity: int,
        variant_id: str | None = None,
    ) -> bool:
        """检查库存是否充足。"""
        available = await self.get_available_stock(product_id, location_id, variant_id)
        return available >= quantity

    async def get_stock_summary(
        self,
        product_id: str | None = None,
        location_id: str | None = None,
        low_stock_only: bool = False,
    ) -> list[dict[str, Any]]:
        """库存汇总查询 (支持按商品/仓库/低库存过滤)。"""
        conditions = ["ib.stock_qty > 0"]
        params: list[Any] = []
        param_idx = 1

        if product_id:
            conditions.append(f"pv.product_id = ${param_idx}")
            params.append(product_id)
            param_idx += 1

        if location_id:
            conditions.append(f"ib.location_id = ${param_idx}")
            params.append(location_id)
            param_idx += 1

        where = " AND ".join(conditions)
        having = ""
        if low_stock_only:
            having = (
                "HAVING SUM(ib.stock_qty - ib.reserved_qty) <= COALESCE("
                "(SELECT safety_threshold FROM product_safety_stock "
                "WHERE product_id = pv.product_id::text "
                "AND variant_id IS NOT DISTINCT FROM ib.variant_id::text "
                "AND location_id = ib.location_id), 10)"
            )

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT pv.product_id, ib.variant_id, ib.location_id,
                       SUM(ib.stock_qty) as total_qty,
                       SUM(ib.reserved_qty) as reserved_qty,
                       SUM(ib.stock_qty - ib.reserved_qty) as available_qty,
                       sl.name as location_name,
                       COUNT(*) as batch_count
                FROM inventory_batch ib
                JOIN product_variant pv ON pv.id = ib.variant_id
                JOIN stock_location sl ON sl.id = ib.location_id
                WHERE {where}
                GROUP BY pv.product_id, ib.variant_id, ib.location_id, sl.name
                {having}
                ORDER BY available_qty ASC
                """,
                *params,
            )

        return [dict(r) for r in rows]

    # ── 库存预留 (下单时调用) ────────────────────────────────────

    async def reserve_stock(
        self,
        *,
        order_id: str,
        product_id: str,
        quantity: int,
        location_id: str,
        variant_id: str | None = None,
        reservation_key: str | None = None,
    ) -> list[str]:
        """为订单预留库存 (FIFO: 老批次优先)。

        返回被预留的 batch_id 列表。
        库存不足时抛出 InsufficientStockError。
        """
        allocations = await self.reserve_stock_allocations(
            order_id=order_id,
            product_id=product_id,
            quantity=quantity,
            location_id=location_id,
            variant_id=variant_id,
            reservation_key=reservation_key,
        )

        logger.info(
            "reserved %d units of %s for order %s across %d batches",
            quantity,
            product_id,
            order_id,
            len(allocations),
        )
        return [str(allocation["batch_id"]) for allocation in allocations]

    async def reserve_stock_allocations(
        self,
        *,
        order_id: str,
        product_id: str,
        quantity: int,
        location_id: str,
        variant_id: str | None = None,
        reservation_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """原子 FIFO 预留，并返回每个 batch 的精确 allocation。

        库存检查和扣 reserved_qty 必须在同一事务、同一批次行锁内完成；
        ``get_stock`` 后再更新会在并发下产生 TOCTOU 超卖。
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        base_key = reservation_key or (
            f"order:{order_id}:product:{product_id}:"
            f"variant:{variant_id or '-'}:location:{location_id}"
        )
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Serialize retries using the same idempotency key before the
                # batch scan. A unique constraint alone is too late: a second
                # transaction could increment reserved_qty before its INSERT
                # hits ON CONFLICT.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    base_key,
                )
                existing = await conn.fetch(
                    """
                    SELECT id AS reservation_id, batch_id, location_id, quantity,
                           product_id, variant_id
                    FROM inventory_reservation
                    WHERE left(idempotency_key, length($1) + 1) = $1 || ':'
                    ORDER BY created_at, id
                    """,
                    base_key,
                )
                if existing:
                    existing_qty = sum(int(row["quantity"]) for row in existing)
                    if existing_qty != quantity:
                        raise ReservationConflictError(
                            "reservation key already used for quantity "
                            f"{existing_qty}, requested {quantity}"
                        )
                    return [
                        {
                            "reservation_id": str(row["reservation_id"]),
                            "batch_id": str(row["batch_id"]),
                            "location_id": str(row["location_id"]),
                            "quantity": int(row["quantity"]),
                            "product_id": str(row["product_id"]),
                            "variant_id": (str(row["variant_id"]) if row["variant_id"] else None),
                        }
                        for row in existing
                    ]

                if variant_id is None:
                    rows = await conn.fetch(
                        """
                        SELECT ib.id, ib.stock_qty, ib.reserved_qty, ib.location_id, ib.variant_id
                        FROM inventory_batch ib
                        JOIN product_variant pv ON pv.id = ib.variant_id
                        WHERE pv.product_id = $1
                          AND ib.location_id = $2 AND ib.status = 'active'
                          AND ib.stock_qty > 0
                        ORDER BY ib.created_at ASC, ib.id ASC
                        FOR UPDATE
                        """,
                        product_id,
                        location_id,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT ib.id, ib.stock_qty, ib.reserved_qty, ib.location_id, ib.variant_id
                        FROM inventory_batch ib
                        JOIN product_variant pv ON pv.id = ib.variant_id
                        WHERE pv.product_id = $1 AND ib.variant_id = $2
                          AND ib.location_id = $3 AND ib.status = 'active'
                          AND ib.stock_qty > 0
                        ORDER BY ib.created_at ASC, ib.id ASC
                        FOR UPDATE
                        """,
                        product_id,
                        variant_id,
                        location_id,
                    )

                total_available = sum(
                    max(0, int(row["stock_qty"]) - int(row["reserved_qty"] or 0)) for row in rows
                )
                if total_available < quantity:
                    raise InsufficientStockError(product_id, variant_id, quantity, total_available)

                allocations: list[dict[str, Any]] = []
                remaining = quantity
                for row in rows:
                    if remaining <= 0:
                        break
                    available = int(row["stock_qty"]) - int(row["reserved_qty"] or 0)
                    to_reserve = min(available, remaining)
                    if to_reserve <= 0:
                        continue
                    batch_id = str(row["id"])
                    result = await conn.fetchrow(
                        """
                        UPDATE inventory_batch
                        SET reserved_qty = reserved_qty + $1, updated_at = NOW()
                        WHERE id = $2 AND stock_qty - reserved_qty >= $1
                        RETURNING id, location_id, variant_id
                        """,
                        to_reserve,
                        row["id"],
                    )
                    if result is None:
                        raise ReservationConflictError(
                            f"batch {batch_id} changed while reserving stock"
                        )
                    allocation_key = f"{base_key}:{batch_id}"
                    reservation_id = await conn.fetchval(
                        """
                        INSERT INTO inventory_reservation
                            (order_id, product_id, variant_id, location_id, batch_id,
                             quantity, idempotency_key)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (idempotency_key) DO UPDATE
                        SET updated_at = inventory_reservation.updated_at
                        RETURNING id
                        """,
                        order_id,
                        product_id,
                        str(result["variant_id"]) if result["variant_id"] else None,
                        result["location_id"],
                        result["id"],
                        to_reserve,
                        allocation_key,
                    )
                    await conn.execute(
                        """
                        INSERT INTO stock_movement
                            (product_id, variant_id, location_id, batch_id,
                             movement_type, quantity, reference_type, reference_id,
                             idempotency_key)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                            ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                            DO NOTHING
                        """,
                        product_id,
                        str(result["variant_id"]) if result["variant_id"] else None,
                        result["location_id"],
                        result["id"],
                        StockMovementType.RESERVE.value,
                        to_reserve,
                        "order",
                        order_id,
                        f"movement:{allocation_key}",
                    )
                    allocations.append(
                        {
                            "reservation_id": str(reservation_id),
                            "batch_id": batch_id,
                            "location_id": str(result["location_id"]),
                            "quantity": to_reserve,
                            "product_id": product_id,
                            "variant_id": (
                                str(result["variant_id"]) if result["variant_id"] else None
                            ),
                        }
                    )
                    remaining -= to_reserve

        await self._check_safety_stock(product_id, variant_id, location_id)
        return allocations

    # ── 库存扣减 (发货时调用) ────────────────────────────────────

    async def deduct_stock(
        self,
        *,
        order_id: str,
        product_id: str,
        quantity: int,
        location_id: str,
        variant_id: str | None = None,
    ) -> None:
        """扣减已预留库存 (发货时调用)。"""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                already_shipped = int(
                    await conn.fetchval(
                        """
                        SELECT COALESCE(SUM(quantity), 0)
                        FROM stock_movement
                        WHERE reference_type = 'order' AND reference_id = $1
                          AND movement_type = 'shipment'
                          AND product_id = $2 AND location_id = $3
                          AND ($4::text IS NULL OR variant_id = $4::text)
                        """,
                        order_id,
                        product_id,
                        location_id,
                        variant_id,
                    )
                    or 0
                )
                remaining = max(0, quantity - already_shipped)
                if remaining == 0:
                    return
                reservations = await conn.fetch(
                    """
                    SELECT * FROM inventory_reservation
                    WHERE order_id = $1 AND product_id = $2
                      AND location_id = $3
                      AND ($4::text IS NULL OR variant_id = $4::text)
                      AND status IN ('reserved', 'partially_released')
                    ORDER BY created_at, id
                    FOR UPDATE
                    """,
                    order_id,
                    product_id,
                    location_id,
                    variant_id,
                )
                available = sum(
                    int(row["quantity"]) - int(row["consumed_qty"]) - int(row["released_qty"])
                    for row in reservations
                )
                if available < remaining:
                    raise ReservationConflictError(
                        f"order {order_id} has {available} reserved units, needs {remaining}"
                    )
                for reservation in reservations:
                    if remaining <= 0:
                        break
                    free = (
                        int(reservation["quantity"])
                        - int(reservation["consumed_qty"])
                        - int(reservation["released_qty"])
                    )
                    to_consume = min(free, remaining)
                    if to_consume <= 0:
                        continue
                    updated = await conn.fetchrow(
                        """
                        UPDATE inventory_batch
                        SET stock_qty = stock_qty - $1, reserved_qty = reserved_qty - $1,
                            updated_at = NOW()
                        WHERE id = $2 AND stock_qty >= $1 AND reserved_qty >= $1
                        RETURNING id
                        """,
                        to_consume,
                        reservation["batch_id"],
                    )
                    if updated is None:
                        raise ReservationConflictError(
                            f"batch {reservation['batch_id']} reservation is inconsistent"
                        )
                    new_consumed = int(reservation["consumed_qty"]) + to_consume
                    new_status = (
                        "consumed"
                        if new_consumed + int(reservation["released_qty"])
                        >= int(reservation["quantity"])
                        else "reserved"
                    )
                    await conn.execute(
                        """
                        UPDATE inventory_reservation
                        SET consumed_qty = $1, status = $2, updated_at = NOW()
                        WHERE id = $3
                        """,
                        new_consumed,
                        new_status,
                        reservation["id"],
                    )
                    await conn.execute(
                        """
                        INSERT INTO stock_movement
                            (product_id, variant_id, location_id, batch_id,
                             movement_type, quantity, reference_type, reference_id,
                             idempotency_key)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                            ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                            DO NOTHING
                        """,
                        product_id,
                        reservation["variant_id"],
                        reservation["location_id"],
                        reservation["batch_id"],
                        StockMovementType.SHIPMENT.value,
                        to_consume,
                        "order",
                        order_id,
                        f"shipment:{order_id}:{reservation['id']}:{new_consumed}",
                    )
                    remaining -= to_consume
                if remaining:
                    raise ReservationConflictError(
                        f"could not allocate shipment for order {order_id}"
                    )

        logger.info(
            "deducted %d units of %s for order %s",
            quantity,
            product_id,
            order_id,
        )

    # ── 释放预留 (取消订单时调用) ───────────────────────────────

    async def release_reservation(
        self,
        *,
        order_id: str,
        product_id: str,
        quantity: int,
        location_id: str,
        variant_id: str | None = None,
    ) -> None:
        """释放订单预留 (取消订单/超时未支付时调用)。"""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                already_released = int(
                    await conn.fetchval(
                        """
                        SELECT COALESCE(SUM(quantity), 0)
                        FROM stock_movement
                        WHERE reference_type = 'order' AND reference_id = $1
                          AND movement_type = 'unreserve'
                          AND product_id = $2 AND location_id = $3
                          AND ($4::text IS NULL OR variant_id = $4::text)
                        """,
                        order_id,
                        product_id,
                        location_id,
                        variant_id,
                    )
                    or 0
                )
                remaining = max(0, quantity - already_released)
                if remaining == 0:
                    return
                reservations = await conn.fetch(
                    """
                    SELECT * FROM inventory_reservation
                    WHERE order_id = $1 AND product_id = $2
                      AND location_id = $3
                      AND ($4::text IS NULL OR variant_id = $4::text)
                      AND status IN ('reserved', 'partially_released')
                    ORDER BY created_at, id
                    FOR UPDATE
                    """,
                    order_id,
                    product_id,
                    location_id,
                    variant_id,
                )
                available = sum(
                    int(row["quantity"]) - int(row["consumed_qty"]) - int(row["released_qty"])
                    for row in reservations
                )
                if available < remaining:
                    raise ReservationConflictError(
                        f"order {order_id} has {available} releasable units, needs {remaining}"
                    )
                for reservation in reservations:
                    if remaining <= 0:
                        break
                    free = (
                        int(reservation["quantity"])
                        - int(reservation["consumed_qty"])
                        - int(reservation["released_qty"])
                    )
                    to_release = min(free, remaining)
                    if to_release <= 0:
                        continue
                    updated = await conn.fetchrow(
                        """
                        UPDATE inventory_batch
                        SET reserved_qty = reserved_qty - $1, updated_at = NOW()
                        WHERE id = $2 AND reserved_qty >= $1
                        RETURNING id
                        """,
                        to_release,
                        reservation["batch_id"],
                    )
                    if updated is None:
                        raise ReservationConflictError(
                            f"batch {reservation['batch_id']} reservation is inconsistent"
                        )
                    new_released = int(reservation["released_qty"]) + to_release
                    consumed = int(reservation["consumed_qty"])
                    status = (
                        "released"
                        if consumed + new_released >= int(reservation["quantity"])
                        else "partially_released"
                    )
                    await conn.execute(
                        """
                        UPDATE inventory_reservation
                        SET released_qty = $1, status = $2, updated_at = NOW()
                        WHERE id = $3
                        """,
                        new_released,
                        status,
                        reservation["id"],
                    )
                    await conn.execute(
                        """
                        INSERT INTO stock_movement
                            (product_id, variant_id, location_id, batch_id,
                             movement_type, quantity, reference_type, reference_id,
                             reason, idempotency_key)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                        ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                        DO NOTHING
                        """,
                        product_id,
                        reservation["variant_id"],
                        reservation["location_id"],
                        reservation["batch_id"],
                        StockMovementType.UNRESERVE.value,
                        to_release,
                        "order",
                        order_id,
                        "order cancelled or timed out",
                        f"unreserve:{order_id}:{reservation['id']}:{new_released}",
                    )
                    remaining -= to_release
                if remaining:
                    raise ReservationConflictError(
                        f"could not release reservation for order {order_id}"
                    )

        logger.info(
            "released %d units reservation of %s for order %s",
            quantity,
            product_id,
            order_id,
        )

    # ── 入库 (采购入库 / 退货入库) ───────────────────────────────

    async def receive_stock(
        self,
        *,
        product_id: str,
        quantity: int,
        location_id: str,
        movement_type: StockMovementType = StockMovementType.RECEIPT,
        variant_id: str | None = None,
        unit_cost: float | None = None,
        reference_type: str | None = None,
        reference_id: str | None = None,
        reason: str | None = None,
        operator_id: str | None = None,
        batch_id: str | None = None,
    ) -> str:
        """入库操作 (增加库存)。

        返回 movement_id。
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # 增加库存
                await conn.execute(
                    """
                    UPDATE inventory_batch
                    SET stock_qty = stock_qty + $1
                    WHERE id = $2
                    """,
                    quantity,
                    batch_id,
                )

                # 写流水
                movement_id = await conn.fetchval(
                    """
                    INSERT INTO stock_movement (
                        product_id, variant_id, location_id, batch_id,
                        movement_type, quantity, unit_cost,
                        reference_type, reference_id, reason, operator_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    RETURNING id
                    """,
                    product_id,
                    variant_id,
                    location_id,
                    batch_id,
                    movement_type.value,
                    quantity,
                    unit_cost,
                    reference_type,
                    reference_id,
                    reason,
                    operator_id,
                )

        logger.info(
            "received %d units of %s (type=%s, ref=%s)",
            quantity,
            product_id,
            movement_type.value,
            reference_id,
        )
        return str(movement_id)

    # ── 安全库存预警 ─────────────────────────────────────────────

    async def _check_safety_stock(
        self,
        product_id: str,
        variant_id: str | None,
        location_id: str,
    ) -> list[StockAlert] | None:
        """检查安全库存 (内部调用, 出入库后触发)。"""
        available = await self.get_available_stock(product_id, location_id, variant_id)

        # 查询安全库存阈值 (默认 10)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT safety_threshold FROM product_safety_stock
                WHERE product_id = $1
                  AND variant_id IS NOT DISTINCT FROM $2
                  AND location_id = $3
                """,
                product_id,
                variant_id,
                location_id,
            )

        threshold = row["safety_threshold"] if row else 10

        if available <= threshold:
            alert_type = "critical" if available <= 0 else "warning"
            alert = StockAlert(
                product_id=product_id,
                variant_id=variant_id,
                location_id=location_id,
                safety_threshold=threshold,
                current_available=available,
                alert_type=alert_type,
            )
            logger.warning(
                "safety stock alert: %s available=%d threshold=%d (%s)",
                product_id,
                available,
                threshold,
                alert_type,
            )
            return [alert]
        return None

    async def check_all_safety_stock(self) -> list[StockAlert]:
        """检查所有商品的安全库存状态。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT pv.product_id, ib.variant_id, ib.location_id,
                       COALESCE(pss.safety_threshold, 10) as threshold,
                       SUM(ib.stock_qty - ib.reserved_qty) as available
                FROM inventory_batch ib
                JOIN product_variant pv ON pv.id = ib.variant_id
                LEFT JOIN product_safety_stock pss
                    ON pss.product_id = pv.product_id::text
                    AND pss.variant_id IS NOT DISTINCT FROM ib.variant_id::text
                    AND pss.location_id = ib.location_id
                WHERE ib.stock_qty > 0
                GROUP BY pv.product_id, ib.variant_id, ib.location_id, pss.safety_threshold
                HAVING SUM(ib.stock_qty - ib.reserved_qty) <= COALESCE(pss.safety_threshold, 10)
                """
            )

        return [
            StockAlert(
                product_id=str(r["product_id"]),
                variant_id=(str(r["variant_id"]) if r["variant_id"] else None),
                location_id=str(r["location_id"]),
                safety_threshold=r["threshold"],
                current_available=r["available"],
                alert_type="critical" if r["available"] <= 0 else "warning",
            )
            for r in rows
        ]

    # ── 库存变动历史 ─────────────────────────────────────────────

    async def get_movement_history(
        self,
        product_id: str,
        location_id: str | None = None,
        variant_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """查询库存变动历史。"""
        conditions = ["product_id = $1"]
        params: list[Any] = [product_id]
        param_idx = 2

        if location_id:
            conditions.append(f"location_id = ${param_idx}")
            params.append(location_id)
            param_idx += 1

        if variant_id is not None:
            conditions.append(
                f"(variant_id = ${param_idx}::text OR "
                f"(variant_id IS NULL AND ${param_idx}::text IS NULL))"
            )
            params.append(variant_id)
            param_idx += 1

        where = " AND ".join(conditions)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, product_id, variant_id, location_id, batch_id,
                       movement_type, quantity, unit_cost,
                       reference_type, reference_id, reason, operator_id, created_at
                FROM stock_movement
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ${param_idx} OFFSET ${param_idx + 1}
                """,
                *params,
                limit,
                offset,
            )

        return [dict(r) for r in rows]

    # ── 安全库存配置 ─────────────────────────────────────────────

    async def set_safety_threshold(
        self,
        product_id: str,
        location_id: str,
        threshold: int,
        variant_id: str | None = None,
        alert_email: str | None = None,
    ) -> None:
        """设置商品的安全库存阈值。"""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                    INSERT INTO product_safety_stock
                        (product_id, variant_id, location_id, safety_threshold, alert_email)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (product_id, variant_id, location_id)
                DO UPDATE SET safety_threshold = EXCLUDED.safety_threshold,
                              alert_email = EXCLUDED.alert_email,
                              updated_at = NOW()
                """,
                product_id,
                variant_id,
                location_id,
                threshold,
                alert_email,
            )
