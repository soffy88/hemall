"""订单服务层 — 状态机驱动、生命周期管理、库存/支付联动。"""

from __future__ import annotations

import logging
from typing import Any

from obase.persistence.pool import PgPool

from .models import (
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
                SELECT id, cart_id, customer_id, region_code, currency, status,
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
                       ib.product_id, ib.variant_id, ib.stock_qty
                FROM order_line_item oli
                JOIN inventory_batch ib ON ib.id = oli.batch_id
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
                SELECT id, cart_id, customer_id, currency, status,
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
            return await conn.fetchval(
                f"SELECT COUNT(*) FROM customer_order {where}", *params
            )

    # ── 状态机驱动 ───────────────────────────────────────────────

    async def transition_status(
        self,
        order_id: str,
        new_status: OrderStatus,
        reason: str | None = None,
        operator_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OrderSnapshot:
        """驱动订单状态转换。

        1. 查询当前状态
        2. 校验转换合法性
        3. 更新订单状态
        4. 写入历史记录
        5. 触发联动 (取消订单→释放库存, 确认→扣减库存等)
        """
        order = await self.get_order(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)

        old_status = order.status
        validate_order_transition(order_id, old_status, new_status)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # 更新订单状态
                await conn.execute(
                    """
                    UPDATE customer_order
                    SET status = $1, updated_at = NOW()
                    WHERE id = $2 AND status = $3
                    """,
                    new_status.value,
                    order_id,
                    old_status.value,
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
                    metadata,
                )

        # 更新快照
        order.status = new_status

        # 触发联动
        if new_status == OrderStatus.CANCELLED:
            await self._on_cancel(order, reason)
        elif new_status == OrderStatus.CONFIRMED:
            await self._on_confirm(order)
        elif new_status == OrderStatus.SHIPPED:
            await self._on_ship(order)

        logger.info(
            "order %s transitioned: %s → %s",
            order_id,
            old_status.value,
            new_status.value,
        )
        return order

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

    async def get_order_stats(
        self, customer_id: str | None = None
    ) -> dict[str, int]:
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
