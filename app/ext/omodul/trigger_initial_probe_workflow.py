"""app.ext.omodul.trigger_initial_probe_workflow — 零号探针触发器 (Admin Ops)。

补天计划 Task 3.1：market_maker_probe_engine 的"诚实空白"——SPEC 全篇没有
任何地方创建第一条 probe_order_logs 记录，引擎只处理已经存在的 'testing'
探针。本 omodul 由运营后台在批次上架时手动激活：输入 batch_id + initial_price，
插入一条 status='testing' 的探针日志并把批次零售价同步为 initial_price
(顾客端立刻看到试探价，跟引擎"调价即写库"的哲学一致)，之后引擎的每小时
tick 会自动捡到这条探针继续试探/出清——这就是"激活"。

幂等语义：同一批次同时只有一条活跃 (status='testing') 探针。重复触发时若
批次已有一条活跃探针，直接 failed 报"already probing"，防止多探针并行试探
互相打架 (引擎按 batch 逐条处理，两条 testing 探针会让价格来回跳)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint


class TriggerInitialProbeWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "trigger_initial_probe_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"batch_id"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail"}


class TriggerInitialProbeWorkflowInput(BaseModel):
    batch_id: str
    initial_price: int = Field(..., ge=1, description="试探起始价 (分)")


async def trigger_initial_probe_workflow(
    config: TriggerInitialProbeWorkflowConfig,
    input_data: TriggerInitialProbeWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """为批次插入第一条试探单日志，激活 market_maker_probe_engine。

    Args:
        config: TriggerInitialProbeWorkflowConfig。
        input_data: batch_id / initial_price (分)。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 probe_id / batch_id /
        initial_price / probe_status ("testing")。
    """
    trail = Trail()
    fp = compute_fingerprint({"batch_id": input_data.batch_id})

    try:
        if pool is None:
            raise ValueError(
                "pool is required — trigger_initial_probe_workflow always touches persisted stock"
            )

        from obase.uuid7 import uuid7

        async with pool.acquire() as conn:
            batch = await conn.fetchrow(
                "SELECT id, status, stock_qty, reserved_qty "
                'FROM "inventory_batch" WHERE id = $1',
                input_data.batch_id,
            )
            if batch is None:
                raise ValueError(f"batch {input_data.batch_id!r} not found")
            if batch["status"] != "active":
                raise ValueError(
                    f"batch {input_data.batch_id!r} not active (cannot start a probe)"
                )
            if batch["stock_qty"] - batch["reserved_qty"] <= 0:
                raise ValueError(
                    f"batch {input_data.batch_id!r} has no available stock to probe"
                )

            existing = await conn.fetchrow(
                "SELECT id FROM \"probe_order_log\" "
                "WHERE batch_id = $1 AND status = 'testing'",
                input_data.batch_id,
            )
            if existing is not None:
                raise ValueError(
                    f"batch {input_data.batch_id!r} already has an active probe"
                )

            probe_id = uuid7()
            await conn.execute(
                'INSERT INTO "probe_order_log" (id, batch_id, probe_price_cents, status) '
                "VALUES ($1, $2, $3, 'testing')",
                probe_id,
                input_data.batch_id,
                input_data.initial_price,
            )
            # 同步顾客端可见价 = 试探起始价 (跟引擎"调价即写库"一致)。
            await conn.execute(
                "UPDATE \"inventory_batch\" SET retail_price_cents = $1 WHERE id = $2",
                input_data.initial_price,
                input_data.batch_id,
            )
        trail.record(
            event="initial_probe_armed",
            batch_id=input_data.batch_id,
            probe_id=str(probe_id),
            initial_price=input_data.initial_price,
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            probe_id=str(probe_id),
            batch_id=input_data.batch_id,
            initial_price=input_data.initial_price,
            probe_status="testing",
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
