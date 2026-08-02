"""app.ext.omodul.scrap_batch_inventory — 过期批次报损清零。

补天计划 Task 2.2 (Theta 衰减引信) 的执行末端：一旦 NOW() > expiration_time，
inventory_decay_engine 强制注入本 omodul 把批次**报损清零**——status 置为
disposed 的同时把 stock_qty / reserved_qty 一起归零，从数据层面保证腐坏商品
不可能再流入前端 (get_nearby_feed 只认 status='active' AND stock_qty > 0，
这里连后者都不满足)。

跟 mark_batch_for_disposal 的区别：那个是"过期销毁"指令 (只改 status，库存
数字保留供审计/盘点)，这个是"报损清零"指令 (status + 库存数字同时归零)。
对过期商品走两条链是刻意的：inventory_reaper_engine (半夜 2:00) 用前者走
正式销毁仪式，inventory_decay_engine (每 1 小时) 用后者做高频防线，两条链
互不依赖，任何一条先命中都不会重复处理 (status 已非 active 的批次两条 SQL
都捞不到)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oprim import ext_notify_send


class ScrapBatchInventoryConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "scrap_batch_inventory"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"batch_id"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail"}

    notification_provider: str = "log"


class ScrapBatchInventoryInput(BaseModel):
    batch_id: str
    reason: str = "expired"


async def scrap_batch_inventory(
    config: ScrapBatchInventoryConfig,
    input_data: ScrapBatchInventoryInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """接受 inventory_decay_engine 指令，将过期批次报损清零 (status + 库存归零)。

    只处理未被硬锁的剩余库存——已经被锁进别人购物车/订单的部分不动 (报损
    的是"还在货架上的腐坏商品"，不是正在被购买的库存)；若整批已全部卖出/
    预留 (available=0)，判 failed，没有可报损的库存。

    Args:
        config: ScrapBatchInventoryConfig(notification_provider="log")。
        input_data: batch_id / reason。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 batch_id / scrapped_qty。
    """
    trail = Trail()
    fp = compute_fingerprint({"batch_id": input_data.batch_id})

    try:
        if pool is None:
            raise ValueError(
                "pool is required — scrap_batch_inventory always touches persisted stock"
            )

        async with pool.acquire() as conn:
            batch = await conn.fetchrow(
                "SELECT id, stock_qty, reserved_qty, status, location_id "
                'FROM "inventory_batch" WHERE id = $1',
                input_data.batch_id,
            )
            if batch is None:
                raise ValueError(f"batch {input_data.batch_id!r} not found")
            if batch["status"] != "active":
                raise ValueError(
                    f"batch {input_data.batch_id!r} not active (already disposed or scrapped)"
                )

            available = batch["stock_qty"] - batch["reserved_qty"]
            if available <= 0:
                raise ValueError(
                    f"batch {input_data.batch_id!r} has no available stock to scrap"
                )

            # 报损清零：status 置 disposed + 库存/预留同时归零，单条原子 UPDATE。
            await conn.execute(
                'UPDATE "inventory_batch" '
                "SET status = 'disposed', stock_qty = 0, reserved_qty = 0 "
                "WHERE id = $1 AND status = 'active'",
                input_data.batch_id,
            )
        trail.record(
            event="batch_scrapped",
            batch_id=input_data.batch_id,
            scrapped_qty=available,
            reason=input_data.reason,
        )

        await ext_notify_send(
            config.notification_provider,
            channel="email",
            template="scrap_task",
            data={
                "to": f"location:{batch['location_id']}",
                "batch_id": input_data.batch_id,
                "reason": input_data.reason,
            },
        )
        trail.record(
            event="scrap_task_dispatched", location_id=str(batch["location_id"])
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            batch_id=input_data.batch_id,
            scrapped_qty=available,
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
