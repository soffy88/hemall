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
from datetime import datetime, timezone
from typing import Any

from obase.persistence.pool import PgPool

from .models import (
    StockAlert,
    StockMovement,
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
                    SELECT ib.id as batch_id, ib.product_id, ib.variant_id,
                           ib.location_id, ib.stock_qty, ib.reserved_qty,
                           sl.name as location_name
                    FROM inventory_batch ib
                    JOIN stock_location sl ON sl.id = ib.location_id
                    WHERE ib.product_id = $1 AND ib.variant_id = $2
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
                    SELECT ib.id as batch_id, ib.product_id, ib.variant_id,
                           ib.location_id, ib.stock_qty, ib.reserved_qty,
                           sl.name as location_name
                    FROM inventory_batch ib
                    JOIN stock_location sl ON sl.id = ib.location_id
                    WHERE ib.product_id = $1 AND ib.variant_id IS NULL
                      AND ib.location_id = $2 AND ib.stock_qty > 0
                    ORDER BY ib.created_at ASC
                    """,
                    product_id,
                    location_id,
                )

        return [
            StockSnapshot(
                product_id=r["product_id"],
                variant_id=r["variant_id"],
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
        available = await self.get_available_stock(
            product_id, location_id, variant_id
        )
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
            conditions.append(f"ib.product_id = ${param_idx}")
            params.append(product_id)
            param_idx += 1

        if location_id:
            conditions.append(f"ib.location_id = ${param_idx}")
            params.append(location_id)
            param_idx += 1

        where = " AND ".join(conditions)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT ib.product_id, ib.variant_id, ib.location_id,
                       SUM(ib.stock_qty) as total_qty,
                       SUM(ib.reserved_qty) as reserved_qty,
                       SUM(ib.stock_qty - ib.reserved_qty) as available_qty,
                       sl.name as location_name,
                       COUNT(*) as batch_count
                FROM inventory_batch ib
                JOIN stock_location sl ON sl.id = ib.location_id
                WHERE {where}
                GROUP BY ib.product_id, ib.variant_id, ib.location_id, sl.name
                {"HAVING SUM(ib.stock_qty - ib.reserved_qty) <= COALESCE(" +
                 f"(SELECT safety_threshold FROM product_safety_stock " +
                 f"WHERE product_id = ib.product_id AND variant_id IS NOT DISTINCT FROM ib.variant_id " +
                 f"AND location_id = ib.location_id), 10)" if low_stock_only else ""}
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
    ) -> list[str]:
        """为订单预留库存 (FIFO: 老批次优先)。

        返回被预留的 batch_id 列表。
        库存不足时抛出 InsufficientStockError。
        """
        snapshots = await self.get_stock(product_id, location_id, variant_id)
        total_available = sum(s.available_qty for s in snapshots)

        if total_available < quantity:
            raise InsufficientStockError(
                product_id, variant_id, quantity, total_available
            )

        reserved_batches: list[str] = []
        remaining = quantity

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for snapshot in snapshots:
                    if remaining <= 0:
                        break

                    batch_available = snapshot.available_qty
                    to_reserve = min(batch_available, remaining)

                    # 更新 reserved_qty
                    await conn.execute(
                        """
                        UPDATE inventory_batch
                        SET reserved_qty = reserved_qty + $1
                        WHERE id = $2 AND stock_qty - reserved_qty >= $1
                        """,
                        to_reserve,
                        snapshot.batch_id,
                    )

                    # 写流水
                    await conn.execute(
                        """
                        INSERT INTO stock_movement (
                            product_id, variant_id, location_id, batch_id,
                            movement_type, quantity, reference_type, reference_id
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        """,
                        product_id,
                        variant_id,
                        location_id,
                        snapshot.batch_id,
                        StockMovementType.RESERVE.value,
                        to_reserve,
                        "order",
                        order_id,
                    )

                    reserved_batches.append(snapshot.batch_id)
                    remaining -= to_reserve

        # 检查安全库存
        await self._check_safety_stock(product_id, variant_id, location_id)

        logger.info(
            "reserved %d units of %s for order %s across %d batches",
            quantity,
            product_id,
            order_id,
            len(reserved_batches),
        )
        return reserved_batches

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
                # 减少 reserved_qty 和 stock_qty
                result = await conn.execute(
                    """
                    UPDATE inventory_batch
                    SET stock_qty = stock_qty - $1,
                        reserved_qty = reserved_qty - $1
                    WHERE product_id = $2
                      AND (variant_id = $3 OR (variant_id IS NULL AND $3 IS NULL))
                      AND location_id = $4
                      AND reserved_qty >= $1
                    """,
                    quantity,
                    product_id,
                    variant_id,
                    location_id,
                )

                # 写流水
                await conn.execute(
                    """
                    INSERT INTO stock_movement (
                        product_id, variant_id, location_id,
                        movement_type, quantity, reference_type, reference_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    product_id,
                    variant_id,
                    location_id,
                    StockMovementType.SHIPMENT.value,
                    quantity,
                    "order",
                    order_id,
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
                await conn.execute(
                    """
                    UPDATE inventory_batch
                    SET reserved_qty = reserved_qty - $1
                    WHERE product_id = $2
                      AND (variant_id = $3 OR (variant_id IS NULL AND $3 IS NULL))
                      AND location_id = $4
                      AND reserved_qty >= $1
                    """,
                    quantity,
                    product_id,
                    variant_id,
                    location_id,
                )

                # 写流水
                await conn.execute(
                    """
                    INSERT INTO stock_movement (
                        product_id, variant_id, location_id,
                        movement_type, quantity, reference_type, reference_id, reason
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    product_id,
                    variant_id,
                    location_id,
                    StockMovementType.UNRESERVE.value,
                    quantity,
                    "order",
                    order_id,
                    "order cancelled or timed out",
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
        available = await self.get_available_stock(
            product_id, location_id, variant_id
        )

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
                SELECT ib.product_id, ib.variant_id, ib.location_id,
                       COALESCE(pss.safety_threshold, 10) as threshold,
                       SUM(ib.stock_qty - ib.reserved_qty) as available
                FROM inventory_batch ib
                LEFT JOIN product_safety_stock pss
                    ON pss.product_id = ib.product_id
                    AND pss.variant_id IS NOT DISTINCT FROM ib.variant_id
                    AND pss.location_id = ib.location_id
                WHERE ib.stock_qty > 0
                GROUP BY ib.product_id, ib.variant_id, ib.location_id, pss.safety_threshold
                HAVING SUM(ib.stock_qty - ib.reserved_qty) <= COALESCE(pss.safety_threshold, 10)
                """
            )

        return [
            StockAlert(
                product_id=r["product_id"],
                variant_id=r["variant_id"],
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
                f"(variant_id = ${param_idx} OR (variant_id IS NULL AND ${param_idx} IS NULL))"
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
                INSERT INTO product_safety_stock (product_id, variant_id, location_id, safety_threshold, alert_email)
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
