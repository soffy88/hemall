"""app.ext.omodul.confirm_batch_pick — 拣货确认，转出库 + 记计件工资。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oprim import generate_id_v7
from ..oskill import calculate_piece_rate_wage

#: 基础计件单价 (非积压状态下的标准单价)；实际计件工资走
#: oskill.calculate_piece_rate_wage 按积压深度激增，这只是它的 base_wage 输入。
_BASE_WAGE_PER_UNIT_CENTS = 500


class ConfirmBatchPickConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "confirm_batch_pick"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"order_line_item_id", "worker_id"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail"}


class ConfirmBatchPickInput(BaseModel):
    order_line_item_id: str
    worker_id: str


async def confirm_batch_pick(
    config: ConfirmBatchPickConfig,
    input_data: ConfirmBatchPickInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """大妈扫码或天花板 CV 摄像头验证正确后，记一笔 pending 状态的计件工资
    (labor_ledger)，供 dispatch_labor_payment 按 worker_id 聚合结算——这是
    confirm_batch_pick "验证正确"这一事件的自然副作用。

    **统一改造说明**：ClearNode 自己原来的两阶段库存模型是"结账只锁库存
    (locked_qty)，拣货确认才真正转出库 (stock_qty 才真的扣减)"。统一之后
    走的是共享 omodul.complete_checkout 的一阶段模型——结账当场就把预留
    转成永久出库 (stock_qty 和 reserved_qty 同时扣减，见该 omodul 自己的
    docstring)，等这个函数拿到 order_line_item 时库存早就finalize 完了，
    没有"locked_qty 彻底消除"这一步可做，也不该再调一次
    oprim.db_confirm_batch_pick (拿 reserved_qty 去扣会直接失败，因为这时
    reserved_qty 已经是 0)。这个函数改成只负责"记录这次拣货事件 + 算计件
    工资"，不再碰库存数字。

    计件单价走 oskill.calculate_piece_rate_wage 按"当前积压深度"激增，不是
    固定价——积压深度 = 全系统还没被拣货确认 (labor_ledger 里没有对应记录)
    的 order_line_item 数量，直接从 schema 现有字段推导，不需要额外的
    "已拣货"状态列。

    Args:
        config: ConfirmBatchPickConfig。
        input_data: order_line_item_id / worker_id。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 batch_id / wage_amount / queue_depth。
    """
    trail = Trail()
    fp = compute_fingerprint(
        {
            "order_line_item_id": input_data.order_line_item_id,
            "worker_id": input_data.worker_id,
        }
    )

    try:
        if pool is None:
            raise ValueError(
                "pool is required — confirm_batch_pick always touches persisted stock"
            )

        async with pool.acquire() as conn:
            oli = await conn.fetchrow(
                'SELECT id, batch_id, quantity FROM "order_line_item" WHERE id = $1',
                input_data.order_line_item_id,
            )
            if oli is None:
                raise ValueError(
                    f"order_line_item {input_data.order_line_item_id!r} not found"
                )

            # labor_ledger.order_line_item_id 物理列还是旧的 VARCHAR(32)，
            # order_line_item.id 现在是 UUID——str() 转一下再比较/存，否则
            # 两边类型对不上。
            queue_depth = await conn.fetchval(
                'SELECT COUNT(*) FROM "order_line_item" oli '
                "WHERE NOT EXISTS ("
                '  SELECT 1 FROM "labor_ledger" ll WHERE ll.order_line_item_id = oli.id::text'
                ")"
            )
        trail.record(
            event="order_line_item_loaded",
            batch_id=str(oli["batch_id"]),
            qty=oli["quantity"],
        )
        trail.record(event="queue_depth_measured", queue_depth=queue_depth)

        per_unit_wage = calculate_piece_rate_wage(
            queue_depth, base_wage=_BASE_WAGE_PER_UNIT_CENTS
        )
        wage_amount = per_unit_wage * oli["quantity"]
        ledger_id = generate_id_v7("labor")
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO "labor_ledger" (id, worker_id, order_line_item_id, wage_amount, status) '
                "VALUES ($1, $2, $3, $4, 'pending')",
                ledger_id,
                input_data.worker_id,
                input_data.order_line_item_id,
                wage_amount,
            )
        trail.record(
            event="wage_recorded",
            ledger_id=ledger_id,
            wage_amount=wage_amount,
            per_unit_wage=per_unit_wage,
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            batch_id=str(oli["batch_id"]),
            wage_amount=wage_amount,
            queue_depth=queue_depth,
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
