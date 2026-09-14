"""订单服务层 — 状态机驱动、生命周期管理、库存/支付联动。"""

from __future__ import annotations

import json
import logging
from typing import Any

from obase.persistence.pool import PgPool

from .models import (
    ConcurrentOrderTransitionError,
    InvalidOrderTransitionError,
    OrderSnapshot,
    OrderStatus,
    StatusHistoryEntry,
    validate_order_transition,
)

logger = logging.getLogger("hemall.orders")


class OrderNotFoundError(Exception):
    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        super().__init__(f"order not found: {order_id}")


class OrderInventoryConflictError(Exception):
    """订单行与逐批 reservation 账本不一致，拒绝状态变更。"""


class OrderService:
    """订单服务 — 封装订单全生命周期操作。"""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    # ── 订单查询 ─────────────────────────────────────────────────

    async def get_order(self, order_id: str) -> OrderSnapshot | None:
        """获取订单详情。"""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, cart_id, customer_id, region_code, currency, status, version,
                       subtotal_cents, discount_cents, tax_cents, shipping_cents,
                       grand_total_cents, payment_provider_name, payment_intent_id,
                       billing_address, shipping_address, created_at, updated_at
                FROM customer_order
                WHERE id = $1
                """,
                order_id,
            )

        if row is None:
            return None

        return OrderSnapshot(
            id=str(row["id"]),
            version=int(row.get("version", 0) or 0),
            cart_id=str(row["cart_id"]) if row["cart_id"] else None,
            customer_id=str(row["customer_id"]) if row["customer_id"] else None,
            status=OrderStatus(row["status"]),
            currency=row["currency"],
            subtotal_cents=row["subtotal_cents"],
            discount_cents=row["discount_cents"],
            tax_cents=row["tax_cents"],
            shipping_cents=row["shipping_cents"],
            grand_total_cents=row["grand_total_cents"],
            payment_provider_name=row["payment_provider_name"],
            payment_intent_id=row["payment_intent_id"],
            billing_address=row["billing_address"],
            shipping_address=row["shipping_address"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get_order_items(self, order_id: str) -> list[dict[str, Any]]:
        """获取订单行项目。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT oli.id, oli.order_id, oli.batch_id, oli.quantity,
                       oli.unit_price_cents, oli.line_total_cents,
                       pv.product_id, ib.variant_id, ib.location_id, ib.stock_qty,
                       oli.location_id AS wired_location_id, oli.reservation_id
                FROM order_line_item oli
                JOIN inventory_batch ib ON ib.id = oli.batch_id
                JOIN product_variant pv ON pv.id = ib.variant_id
                WHERE oli.order_id = $1
                """,
                order_id,
            )
        return [dict(r) for r in rows]

    async def list_orders(
        self,
        customer_id: str | None = None,
        status: OrderStatus | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[OrderSnapshot]:
        """订单列表查询 (支持按客户/状态过滤)。"""
        conditions: list[str] = []
        params: list[Any] = []
        idx = 1

        if customer_id:
            conditions.append(f"customer_id = ${idx}")
            params.append(customer_id)
            idx += 1

        if status:
            conditions.append(f"status = ${idx}")
            params.append(status.value)
            idx += 1

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, cart_id, customer_id, currency, status, version,
                       subtotal_cents, discount_cents, tax_cents, shipping_cents,
                       grand_total_cents, payment_provider_name, payment_intent_id,
                       created_at, updated_at
                FROM customer_order
                {where}
                ORDER BY created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}
                """,
                *params,
                limit,
                offset,
            )

        return [
            OrderSnapshot(
                id=str(r["id"]),
                version=int(r.get("version", 0) or 0),
                cart_id=str(r["cart_id"]) if r["cart_id"] else None,
                customer_id=str(r["customer_id"]) if r["customer_id"] else None,
                status=OrderStatus(r["status"]),
                currency=r["currency"],
                subtotal_cents=r["subtotal_cents"],
                discount_cents=r["discount_cents"],
                tax_cents=r["tax_cents"],
                shipping_cents=r["shipping_cents"],
                grand_total_cents=r["grand_total_cents"],
                payment_provider_name=r["payment_provider_name"],
                payment_intent_id=r["payment_intent_id"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    async def count_orders(
        self,
        customer_id: str | None = None,
        status: OrderStatus | None = None,
    ) -> int:
        """订单计数。"""
        conditions: list[str] = []
        params: list[Any] = []
        idx = 1

        if customer_id:
            conditions.append(f"customer_id = ${idx}")
            params.append(customer_id)
            idx += 1

        if status:
            conditions.append(f"status = ${idx}")
            params.append(status.value)
            idx += 1

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        async with self._pool.acquire() as conn:
            return await conn.fetchval(f"SELECT COUNT(*) FROM customer_order {where}", *params)

    # ── 状态机驱动 ───────────────────────────────────────────────

    async def transition_status(
        self,
        order_id: str,
        new_status: OrderStatus,
        reason: str | None = None,
        operator_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> OrderSnapshot:
        """驱动订单状态转换。

        1. 查询当前状态
        2. 校验转换合法性
        3. 更新订单状态
        4. 写入历史记录
        5. 触发联动 (取消订单→释放库存, 确认→扣减库存等)
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(
                    """
                    SELECT id, status, version
                    FROM customer_order
                    WHERE id = $1
                    FOR UPDATE
                    """,
                    order_id,
                )
                if current is None:
                    raise OrderNotFoundError(order_id)
                old_status = OrderStatus(current["status"])
                current_version = int(current["version"] or 0)
                if expected_version is not None and expected_version != current_version:
                    raise ConcurrentOrderTransitionError(
                        order_id, expected_version, current_version
                    )
                validate_order_transition(order_id, old_status, new_status)

                # 库存操作与订单 transition 共用同一 PostgreSQL 事务。
                # 只按 inventory_reservation.batch_id 更新，绝不再按商品汇总扣减。
                if new_status == OrderStatus.CANCELLED:
                    await self._release_order_inventory(conn, order_id)
                elif new_status == OrderStatus.SHIPPED:
                    await self._ship_order_inventory(conn, order_id)

                updated = await conn.fetchrow(
                    """
                    UPDATE customer_order
                    SET status = $1, version = version + 1, updated_at = NOW()
                    WHERE id = $2 AND status = $3 AND version = $4
                    RETURNING id, version
                    """,
                    new_status.value,
                    order_id,
                    old_status.value,
                    current_version,
                )
                if updated is None:
                    # 即使调用者没有显式传 expected_version，数据库条件仍保证
                    # 一个版本只能成功一次。
                    raise ConcurrentOrderTransitionError(
                        order_id, current_version, current_version + 1
                    )

                # 写历史记录
                await conn.execute(
                    """
                    INSERT INTO order_status_history (
                        order_id, from_status, to_status, reason, operator_id, metadata
                    ) VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    order_id,
                    old_status.value,
                    new_status.value,
                    reason,
                    operator_id,
                    json.dumps(metadata or {}, ensure_ascii=False),
                )

        logger.info(
            "order %s transitioned: %s → %s",
            order_id,
            old_status.value,
            new_status.value,
        )
        result = await self.get_order(order_id)
        if result is None:  # pragma: no cover - row was just updated in the transaction
            raise OrderNotFoundError(order_id)
        return result

    async def confirm_order(
        self,
        order_id: str,
        payment_intent_id: str | None = None,
        operator_id: str | None = None,
    ) -> OrderSnapshot:
        """确认订单 (支付成功回调)。"""
        # 先更新 payment_intent_id
        async with self._pool.acquire() as conn:
            if payment_intent_id:
                await conn.execute(
                    """
                    UPDATE customer_order
                    SET payment_intent_id = $1, updated_at = NOW()
                    WHERE id = $2
                    """,
                    payment_intent_id,
                    order_id,
                )

        return await self.transition_status(
            order_id,
            OrderStatus.CONFIRMED,
            reason="payment confirmed",
            operator_id=operator_id,
        )

    async def cancel_order(
        self,
        order_id: str,
        reason: str = "user requested",
        operator_id: str | None = None,
    ) -> OrderSnapshot:
        """取消订单 (自动释放库存)。"""
        return await self.transition_status(
            order_id,
            OrderStatus.CANCELLED,
            reason=reason,
            operator_id=operator_id,
        )

    async def ship_order(
        self,
        order_id: str,
        tracking_number: str | None = None,
        carrier: str | None = None,
        operator_id: str | None = None,
    ) -> OrderSnapshot:
        """发货。"""
        metadata: dict[str, Any] = {}
        if tracking_number:
            metadata["tracking_number"] = tracking_number
        if carrier:
            metadata["carrier"] = carrier

        return await self.transition_status(
            order_id,
            OrderStatus.SHIPPED,
            reason="order shipped",
            operator_id=operator_id,
            metadata=metadata,
        )

    async def deliver_order(
        self,
        order_id: str,
        operator_id: str | None = None,
    ) -> OrderSnapshot:
        """标记送达。"""
        return await self.transition_status(
            order_id,
            OrderStatus.DELIVERED,
            reason="delivered",
            operator_id=operator_id,
        )

    async def complete_order(
        self,
        order_id: str,
        operator_id: str | None = None,
    ) -> OrderSnapshot:
        """客户确认收货 / 自动完成。"""
        return await self.transition_status(
            order_id,
            OrderStatus.COMPLETED,
            reason="order completed",
            operator_id=operator_id,
        )

    # ── 精确库存联动 (与订单 transition 共用同一事务) ───────────────

    async def _release_order_inventory(self, conn: Any, order_id: str) -> None:
        """按 reservation/batch 精确释放订单库存。

        这里不再按 product/location 汇总更新。每一条 reservation 都先锁住，
        再只更新它自己的 batch；订单状态和库存账本要么一起提交，要么一起回滚。
        """

        reservations = await conn.fetch(
            """
            SELECT id, product_id, variant_id, location_id, batch_id,
                   quantity, consumed_qty, released_qty
            FROM inventory_reservation
            WHERE order_id = $1
              AND status IN ('reserved', 'partially_released')
            ORDER BY created_at, id
            FOR UPDATE
            """,
            order_id,
        )
        for reservation in reservations:
            releasable = (
                int(reservation["quantity"])
                - int(reservation["consumed_qty"])
                - int(reservation["released_qty"])
            )
            if releasable <= 0:
                continue

            batch = await conn.fetchrow(
                """
                UPDATE inventory_batch
                SET reserved_qty = reserved_qty - $1, updated_at = NOW()
                WHERE id = $2 AND reserved_qty >= $1
                RETURNING id
                """,
                releasable,
                reservation["batch_id"],
            )
            if batch is None:
                raise OrderInventoryConflictError(
                    f"reservation {reservation['id']} is inconsistent with batch "
                    f"{reservation['batch_id']}"
                )

            released_qty = int(reservation["released_qty"]) + releasable
            await conn.execute(
                """
                UPDATE inventory_reservation
                SET released_qty = $1, status = 'released', updated_at = NOW()
                WHERE id = $2
                """,
                released_qty,
                reservation["id"],
            )
            await conn.execute(
                """
                INSERT INTO stock_movement
                    (product_id, variant_id, location_id, batch_id,
                     movement_type, quantity, reference_type, reference_id,
                     reason, idempotency_key)
                VALUES ($1, $2, $3, $4, 'unreserve', $5, 'order', $6, $7, $8)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                reservation["product_id"],
                reservation["variant_id"],
                reservation["location_id"],
                reservation["batch_id"],
                releasable,
                order_id,
                "order cancelled",
                f"order-cancel:{order_id}:{reservation['id']}:{released_qty}",
            )

    async def _ship_order_inventory(self, conn: Any, order_id: str) -> None:
        """把订单 reservation 精确转换成 shipment，且保证只能转换一次。"""

        reservations = await conn.fetch(
            """
            SELECT id, order_line_item_id, product_id, variant_id, location_id,
                   batch_id, quantity, consumed_qty, released_qty
            FROM inventory_reservation
            WHERE order_id = $1
              AND status IN ('reserved', 'partially_released')
            ORDER BY created_at, id
            FOR UPDATE
            """,
            order_id,
        )
        line_count = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM order_line_item WHERE order_id = $1",
                order_id,
            )
            or 0
        )
        if line_count and not reservations:
            # 没有 reservation 就拒绝发货，避免“订单显示已发货但库存未扣”或
            # 重新按商品汇总扣错别的批次。历史订单应先做一次可审计迁移。
            raise OrderInventoryConflictError(f"order {order_id} has no inventory reservations")

        for reservation in reservations:
            shippable = (
                int(reservation["quantity"])
                - int(reservation["consumed_qty"])
                - int(reservation["released_qty"])
            )
            if shippable <= 0:
                continue

            batch = await conn.fetchrow(
                """
                UPDATE inventory_batch
                SET stock_qty = stock_qty - $1,
                    reserved_qty = reserved_qty - $1,
                    updated_at = NOW()
                WHERE id = $2 AND stock_qty >= $1 AND reserved_qty >= $1
                RETURNING id
                """,
                shippable,
                reservation["batch_id"],
            )
            if batch is None:
                raise OrderInventoryConflictError(
                    f"reservation {reservation['id']} cannot be shipped from batch "
                    f"{reservation['batch_id']}"
                )

            consumed_qty = int(reservation["consumed_qty"]) + shippable
            await conn.execute(
                """
                UPDATE inventory_reservation
                SET consumed_qty = $1, status = 'consumed', updated_at = NOW()
                WHERE id = $2
                """,
                consumed_qty,
                reservation["id"],
            )
            await conn.execute(
                """
                INSERT INTO stock_movement
                    (product_id, variant_id, location_id, batch_id,
                     movement_type, quantity, reference_type, reference_id,
                     reason, idempotency_key)
                VALUES ($1, $2, $3, $4, 'shipment', $5, 'order', $6, $7, $8)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                reservation["product_id"],
                reservation["variant_id"],
                reservation["location_id"],
                reservation["batch_id"],
                shippable,
                order_id,
                "order shipped",
                f"order-ship:{order_id}:{reservation['id']}:{consumed_qty}",
            )

            line_id = reservation["order_line_item_id"]
            if line_id:
                await conn.execute(
                    """
                    UPDATE order_line_item
                    SET fulfilled_qty = LEAST(quantity, fulfilled_qty + $1)
                    WHERE id = $2 AND order_id = $3
                    """,
                    shippable,
                    line_id,
                    order_id,
                )
            else:
                # 兼容迁移前未写 order_line_item_id 的 reservation，但仍然
                # 只按 batch 精确绑定，不能按 product 汇总。
                await conn.execute(
                    """
                    UPDATE order_line_item
                    SET fulfilled_qty = LEAST(quantity, fulfilled_qty + $1)
                    WHERE id = (
                        SELECT id FROM order_line_item
                        WHERE order_id = $2 AND batch_id = $3
                          AND fulfilled_qty < quantity
                        ORDER BY id
                        LIMIT 1
                    )
                    """,
                    shippable,
                    order_id,
                    reservation["batch_id"],
                )

    # ── 状态联动 (内部) ──────────────────────────────────────────

    async def _on_cancel(self, order: OrderSnapshot, reason: str | None) -> None:
        """订单取消 → 释放预留库存。"""
        try:
            from app.inventory.service import InventoryService

            inv_svc = InventoryService(self._pool)
            items = await self.get_order_items(order.id)

            for item in items:
                # 尝试释放预留 (best-effort, 不阻塞取消)
                try:
                    await inv_svc.release_reservation(
                        order_id=order.id,
                        product_id=item["product_id"],
                        quantity=item["quantity"],
                        location_id=str(item.get("location_id", "")),
                        variant_id=item.get("variant_id"),
                    )
                except Exception as exc:
                    logger.warning(
                        "failed to release inventory for order %s item %s: %s",
                        order.id,
                        item["id"],
                        exc,
                    )

            logger.info("released inventory for cancelled order %s", order.id)
        except Exception as exc:
            logger.warning("cancel inventory release failed for order %s: %s", order.id, exc)

    async def _on_confirm(self, order: OrderSnapshot) -> None:
        """订单确认 → 预留库存 (如果尚未预留)。"""
        # 注意: 实际场景中库存预留在 checkout 时已完成
        # 这里作为兜底处理
        logger.info("order %s confirmed, inventory already reserved at checkout", order.id)

    async def _on_ship(self, order: OrderSnapshot) -> None:
        """订单发货 → 扣减已预留库存。"""
        try:
            from app.inventory.service import InventoryService

            inv_svc = InventoryService(self._pool)
            items = await self.get_order_items(order.id)

            for item in items:
                try:
                    await inv_svc.deduct_stock(
                        order_id=order.id,
                        product_id=item["product_id"],
                        quantity=item["quantity"],
                        location_id=str(item.get("location_id", "")),
                        variant_id=item.get("variant_id"),
                    )
                except Exception as exc:
                    logger.warning(
                        "failed to deduct inventory for order %s item %s: %s",
                        order.id,
                        item["id"],
                        exc,
                    )
        except Exception as exc:
            logger.warning("ship inventory deduction failed for order %s: %s", order.id, exc)

    # ── 历史追踪 ─────────────────────────────────────────────────

    async def get_status_history(self, order_id: str) -> list[StatusHistoryEntry]:
        """获取订单状态变更历史。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, order_id, from_status, to_status, reason,
                       operator_id, metadata, created_at
                FROM order_status_history
                WHERE order_id = $1
                ORDER BY created_at ASC
                """,
                order_id,
            )

        return [
            StatusHistoryEntry(
                id=str(r["id"]),
                order_id=str(r["order_id"]),
                from_status=r["from_status"],
                to_status=r["to_status"],
                reason=r["reason"],
                operator_id=r["operator_id"],
                metadata=dict(r["metadata"]) if r["metadata"] else None,
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ── 订单统计 ─────────────────────────────────────────────────

    async def get_order_stats(self, customer_id: str | None = None) -> dict[str, int]:
        """按状态统计订单数量。"""
        where = ""
        params: list[Any] = []
        if customer_id:
            where = "WHERE customer_id = $1"
            params.append(customer_id)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT status, COUNT(*) as cnt
                FROM customer_order
                {where}
                GROUP BY status
                """,
                *params,
            )

        return {row["status"]: row["cnt"] for row in rows}
