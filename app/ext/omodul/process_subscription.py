"""app.ext.omodul.process_subscription — 收会员费，开通购买权限。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint


class ProcessSubscriptionConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "process_subscription"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"customer_id"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail", "cost"}

    payment_provider: str = "manual"
    currency: str = "CNY"


class ProcessSubscriptionInput(BaseModel):
    customer_id: str
    plan_fee_cents: int
    duration_days: int = 365


async def process_subscription(
    config: ProcessSubscriptionConfig,
    input_data: ProcessSubscriptionInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """收取会员系统算力费，开通用户的进场购买权限。

    统一之后 customer_id 是真 customer.id FK，membership 是本函数直接需要的
    最小补充表。重复订阅 (customer_id 已有未过期的 active 会员) 直接拒绝，
    不重复扣费。

    Args:
        config: ProcessSubscriptionConfig(payment_provider="manual", currency="CNY")。
        input_data: customer_id / plan_fee_cents / duration_days(默认 365)。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 membership_id / expires_at。
    """
    trail = Trail()
    fp = compute_fingerprint({"customer_id": input_data.customer_id})

    try:
        if pool is None:
            raise ValueError(
                "pool is required — process_subscription always touches persisted stock"
            )
        if input_data.plan_fee_cents <= 0:
            raise ValueError("plan_fee_cents must be positive")

        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                'SELECT id, expires_at FROM "membership" '
                "WHERE customer_id = $1 AND status = 'active' AND expires_at > NOW()",
                input_data.customer_id,
            )
        if existing is not None:
            raise ValueError(
                f"customer {input_data.customer_id!r} already has an active membership "
                f"(expires_at={existing['expires_at'].isoformat()})"
            )

        from obase.provider_registry import ProviderRegistry

        provider = ProviderRegistry.get().generic("payment", config.payment_provider)
        auth_result = await provider.authorize(
            amount=input_data.plan_fee_cents,
            currency=config.currency,
            meta={"customer_id": input_data.customer_id, "purpose": "membership"},
        )
        capture_result = await provider.capture(intent_id=auth_result["intent_id"])
        trail.record(
            event="fee_captured",
            intent_id=capture_result["intent_id"],
            amount=input_data.plan_fee_cents,
        )

        from obase.uuid7 import uuid7

        membership_id = uuid7()
        expires_at = datetime.now(UTC) + timedelta(days=input_data.duration_days)
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO "membership" (id, customer_id, status, intent_id, expires_at) '
                "VALUES ($1, $2, 'active', $3, $4)",
                membership_id,
                input_data.customer_id,
                capture_result["intent_id"],
                expires_at,
            )
        trail.record(
            event="membership_activated",
            membership_id=membership_id,
            expires_at=expires_at.isoformat(),
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            cost_usd=0.0,
            membership_id=membership_id,
            expires_at=expires_at.isoformat(),
        )

    except Exception as exc:
        trail.record(event="error", detail=str(exc))
        trail_path = trail.write(output_dir) if output_dir else None
        return build_result(
            status="failed",
            error={"type": type(exc).__name__, "message": str(exc)},
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
        )
