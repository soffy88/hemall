"""app.ext.omodul.execute_ambient_replenishment — 环境式自动补货。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oprim import (
    db_lock_batch_inventory,
    db_query_many,
    db_unlock_batch_inventory,
    math_haversine_distance,
)
from ..oskill import predict_household_burn_rate


class ExecuteAmbientReplenishmentConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "execute_ambient_replenishment"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"customer_id", "variant_id"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail", "cost"}

    payment_provider: str = "manual"
    currency: str = "CNY"


class ExecuteAmbientReplenishmentInput(BaseModel):
    # 统一之前扩展域自己的 orders 表压根没有顾客身份列，customer_ref
    # 只是个不落库的自由字符串；统一之后 customer_order.customer_id 是真
    # FK，这里跟着改成真 customer.id，创建的订单从此能在顾客账户下查到。
    customer_id: str
    variant_id: str
    qty: int
    family_size: int
    purchase_history: list[dict[str, Any]]
    customer_lat: float
    customer_lon: float


async def execute_ambient_replenishment(
    config: ExecuteAmbientReplenishmentConfig,
    input_data: ExecuteAmbientReplenishmentInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """环境式自动补货：预测家庭存货见底日期，到期直接扣款，推单至离用户最近的微仓。

    SPEC 没有给"顾客购买历史"和"顾客配送坐标"的落地表——跟 process_subscription/
    dispatch_host_dividend 同样的处理方式：这两样由调用方直接传入 (purchase_history/
    customer_lat/customer_lon)，不臆造一张跨域客户主档表。所以这里没法从库里
    反查购买历史，只能信任调用方 (未来的 oservi 定时引擎会自己维护一份顾客
    订阅计划并喂历史数据进来)；统一之后新建的 customer_order 会带上真实
    customer_id，至少下单历史本身能在顾客账户下查到了。

    先用 oskill.predict_household_burn_rate 算见底时间；还没到期直接返回
    replenished=False，不碰钱不锁库存——这是幂等触发的关键：oservi 未来会定期
    (如每天) 调这个函数，多数调用应该是"还没到期，什么都不做"。

    到期后，在全部有该 variant 可用库存的 active 批次里，用
    oprim.math_haversine_distance 挑离顾客最近的那个仓，锁库存 → 扣款 → 建单，
    锁库存/扣款失败都补偿回滚 (锁库存失败直接判 failed；扣款失败补偿解锁)，
    风格对齐 complete_checkout。

    Args:
        config: ExecuteAmbientReplenishmentConfig(payment_provider="manual", currency="CNY")。
        input_data: customer_id / variant_id / qty (本次补货数量) /
            family_size (家庭人口数) / purchase_history (历史购买记录，
            每条至少含 "purchased_at" datetime 和 "quantity" int) /
            customer_lat / customer_lon (顾客配送坐标)。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 replenished (bool) /
        predicted_depletion_at (ISO str)，replenished=True 时另含
        order_id / batch_id / location_id / distance_km / grand_total_cents。
    """
    trail = Trail()
    fp = compute_fingerprint(
        {"customer_id": input_data.customer_id, "variant_id": input_data.variant_id}
    )
    locked_batch_id: str | None = None

    try:
        if pool is None:
            raise ValueError(
                "pool is required — execute_ambient_replenishment always touches persisted stock"
            )
        if input_data.qty <= 0:
            raise ValueError("qty must be positive")

        predicted_depletion_at = predict_household_burn_rate(
            input_data.purchase_history, family_size=input_data.family_size
        )
        trail.record(
            event="burn_rate_predicted",
            predicted_depletion_at=predicted_depletion_at.isoformat(),
        )

        now = datetime.now(predicted_depletion_at.tzinfo or UTC)
        if now < predicted_depletion_at:
            trail.record(event="not_due_yet", now=now.isoformat())
            trail_path = trail.write(output_dir)
            return build_result(
                status="completed",
                error=None,
                fingerprint=fp,
                trail=trail,
                trail_path=trail_path,
                cost_usd=0.0,
                replenished=False,
                predicted_depletion_at=predicted_depletion_at.isoformat(),
            )

        candidates = await db_query_many(
            pool,
            sql=(
                "SELECT b.id AS batch_id, b.retail_price_cents, l.id AS location_id, "
                "l.lat, l.lng AS lon "
                'FROM "inventory_batch" b '
                'JOIN "stock_location" l ON l.id = b.location_id '
                "WHERE b.variant_id = $1 AND b.status = 'active' AND l.status = 'active' "
                "AND l.lat IS NOT NULL AND l.lng IS NOT NULL "
                "AND b.stock_qty - b.reserved_qty >= $2"
            ),
            params=(input_data.variant_id, input_data.qty),
        )
        if not candidates:
            raise ValueError(
                f"no available inventory for variant {input_data.variant_id!r} "
                f"with qty={input_data.qty}"
            )

        nearest = min(
            candidates,
            key=lambda c: math_haversine_distance(
                float(c["lat"]),
                lon1=float(c["lon"]),
                lat2=input_data.customer_lat,
                lon2=input_data.customer_lon,
            ),
        )
        distance_km = math_haversine_distance(
            float(nearest["lat"]),
            lon1=float(nearest["lon"]),
            lat2=input_data.customer_lat,
            lon2=input_data.customer_lon,
        )
        location_id = str(nearest["location_id"])
        trail.record(
            event="nearest_location_resolved",
            location_id=location_id,
            distance_km=distance_km,
        )

        batch_id = str(nearest["batch_id"])
        lock_result = await db_lock_batch_inventory(
            pool, batch_id=batch_id, lock_qty=input_data.qty
        )
        if lock_result["status"] == "failed":
            raise ValueError(
                f"lock failed for batch {batch_id!r}: {lock_result['error']['message']}"
            )
        locked_batch_id = batch_id
        trail.record(event="batch_locked", batch_id=locked_batch_id, qty=input_data.qty)

        grand_total = lock_result["retail_price"] * input_data.qty

        from obase.provider_registry import ProviderRegistry

        provider = ProviderRegistry.get().generic("payment", config.payment_provider)
        auth_result = await provider.authorize(
            amount=grand_total,
            currency=config.currency,
            meta={
                "customer_id": input_data.customer_id,
                "purpose": "ambient_replenishment",
            },
        )
        capture_result = await provider.capture(intent_id=auth_result["intent_id"])
        trail.record(
            event="payment_captured",
            intent_id=capture_result["intent_id"],
            amount=grand_total,
        )

        from obase.uuid7 import uuid7

        order_id = uuid7()
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO "customer_order" '
                "(id, customer_id, currency, status, subtotal_cents, discount_cents, "
                "tax_cents, shipping_cents, grand_total_cents, payment_provider_name, "
                "payment_intent_id) "
                "VALUES ($1, $2, $3, 'paid', $4, 0, 0, 0, $4, $5, $6)",
                order_id,
                input_data.customer_id,
                config.currency,
                grand_total,
                config.payment_provider,
                capture_result["intent_id"],
            )
            await conn.execute(
                'INSERT INTO "order_line_item" '
                "(id, order_id, batch_id, quantity, unit_price_cents, line_total_cents) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                uuid7(),
                order_id,
                locked_batch_id,
                input_data.qty,
                lock_result["retail_price"],
                lock_result["retail_price"] * input_data.qty,
            )
        trail.record(event="order_created", order_id=order_id, grand_total=grand_total)

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            cost_usd=0.0,
            replenished=True,
            predicted_depletion_at=predicted_depletion_at.isoformat(),
            order_id=order_id,
            batch_id=locked_batch_id,
            location_id=location_id,
            distance_km=distance_km,
            grand_total_cents=grand_total,
        )

    except Exception as exc:
        if pool is not None and locked_batch_id is not None:
            try:
                await db_unlock_batch_inventory(
                    pool, batch_id=locked_batch_id, unlock_qty=input_data.qty
                )
                trail.record(
                    event="compensating_unlock",
                    batch_id=locked_batch_id,
                    qty=input_data.qty,
                )
            except Exception as unlock_exc:  # noqa: BLE001 - 补偿失败不掩盖原始错误
                trail.record(
                    event="compensating_unlock_failed",
                    batch_id=locked_batch_id,
                    detail=str(unlock_exc),
                )
        trail.record(event="error", detail=str(exc))
        trail_path = trail.write(output_dir) if output_dir else None
        return build_result(
            status="failed",
            error={"type": type(exc).__name__, "message": str(exc)},
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
        )
