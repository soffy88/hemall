"""app.ext.omodul.mark_batch_for_disposal — 过期批次强制销毁。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oprim import ext_notify_send


class MarkBatchForDisposalConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "mark_batch_for_disposal"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"batch_id"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail"}

    notification_provider: str = "log"


class MarkBatchForDisposalInput(BaseModel):
    batch_id: str
    reason: str = "expired"


async def mark_batch_for_disposal(
    config: MarkBatchForDisposalConfig,
    input_data: MarkBatchForDisposalInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """接受死神引擎 (inventory_reaper_engine) 指令，将过期商品状态改为
    disposed，下发丢弃任务给仓管 (通知)。

    只处理未售罄的库存部分——已经被硬锁 (locked_qty>0，意味着已经在别人的
    购物车/订单里) 的部分不动，避免把正在被购买的东西标废弃；若整批已全部
    卖出/预留 (available=0)，直接判 failed，没有可销毁的库存。

    Args:
        config: MarkBatchForDisposalConfig(notification_provider="log")。
        input_data: batch_id / reason。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 batch_id / disposed_qty。
    """
    trail = Trail()
    fp = compute_fingerprint({"batch_id": input_data.batch_id})

    try:
        if pool is None:
            raise ValueError(
                "pool is required — mark_batch_for_disposal always touches persisted stock"
            )

        async with pool.acquire() as conn:
            batch = await conn.fetchrow(
                "SELECT id, stock_qty, reserved_qty, status, location_id "
                'FROM "inventory_batch" WHERE id = $1',
                input_data.batch_id,
            )
            if batch is None:
                raise ValueError(f"batch {input_data.batch_id!r} not found")
            if batch["status"] == "disposed":
                raise ValueError(f"batch {input_data.batch_id!r} already disposed")

            available = batch["stock_qty"] - batch["reserved_qty"]
            if available <= 0:
                raise ValueError(
                    f"batch {input_data.batch_id!r} has no available (unreserved) stock to dispose"
                )

            await conn.execute(
                "UPDATE \"inventory_batch\" SET status = 'disposed' WHERE id = $1",
                input_data.batch_id,
            )
        trail.record(
            event="batch_disposed",
            batch_id=input_data.batch_id,
            disposed_qty=available,
            reason=input_data.reason,
        )

        await ext_notify_send(
            config.notification_provider,
            channel="email",
            template="dispose_task",
            data={
                "to": f"location:{batch['location_id']}",
                "batch_id": input_data.batch_id,
                "reason": input_data.reason,
            },
        )
        trail.record(
            event="dispose_task_dispatched", location_id=str(batch["location_id"])
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            batch_id=input_data.batch_id,
            disposed_qty=available,
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
