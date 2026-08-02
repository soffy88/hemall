"""app.ext.omodul.dispatch_host_dividend — 宿主场地分润结算。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oprim import ext_pay_transfer


class DispatchHostDividendConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "dispatch_host_dividend"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"host_id", "location_id", "period_date"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail", "cost"}

    payout_provider: str = "manual"
    dividend_per_tote_cents: int = 300


class DispatchHostDividendInput(BaseModel):
    host_id: str
    location_id: str
    payout_account: str
    tote_count: int
    period_date: str | None = None  # ISO date; 默认今天


async def dispatch_host_dividend(
    config: DispatchHostDividendConfig,
    input_data: DispatchHostDividendInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """按节点中转的 Tote 数量，给提供车库场地的宿主大爷结算电费与分润。

    tote_count 由调用方传入——按 tote 实际中转量聚合需要一张 tote 流转日志
    表 (SPEC §1.1 没给)，那张表 + 对应的聚合统计逻辑留待后续 phase
    (未来大概率是 oservi.node_host_settlement_engine 的职责，它按日扫描
    流转日志算出 tote_count 后调这个 omodul)；这里只负责"给定已知的
    tote_count，执行打款 + 记账"这个事务本身。

    Args:
        config: DispatchHostDividendConfig(payout_provider="manual",
            dividend_per_tote_cents=300)。
        input_data: host_id / location_id / payout_account / tote_count /
            period_date(可选，默认今天)。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 amount / transfer_id。
    """
    trail = Trail()
    period_obj = (
        date.fromisoformat(input_data.period_date)
        if input_data.period_date
        else date.today()
    )
    period = period_obj.isoformat()
    fp = compute_fingerprint(
        {
            "host_id": input_data.host_id,
            "location_id": input_data.location_id,
            "period_date": period,
        }
    )

    try:
        if pool is None:
            raise ValueError(
                "pool is required — dispatch_host_dividend always touches persisted stock"
            )
        if input_data.tote_count <= 0:
            raise ValueError("tote_count must be positive")

        amount = input_data.tote_count * config.dividend_per_tote_cents
        trail.record(
            event="dividend_computed", tote_count=input_data.tote_count, amount=amount
        )

        transfer_result = await ext_pay_transfer(
            config.payout_provider, account=input_data.payout_account, amount=amount
        )
        trail.record(
            event="host_paid", transfer_id=transfer_result["transfer_id"], amount=amount
        )

        from obase.uuid7 import uuid7

        ledger_id = uuid7()
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO "host_dividend_ledger" '
                "(id, host_id, location_id, tote_count, amount_cents, payout_transfer_id, period_date) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                ledger_id,
                input_data.host_id,
                input_data.location_id,
                input_data.tote_count,
                amount,
                transfer_result["transfer_id"],
                period_obj,
            )
        trail.record(event="ledger_recorded", ledger_id=ledger_id)

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            cost_usd=0.0,
            amount=amount,
            transfer_id=transfer_result["transfer_id"],
            ledger_id=ledger_id,
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
