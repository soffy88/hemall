"""app.ext.omodul.execute_ambient_intake_workflow — 顶棚 CV 信号自动点亮微仓。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oprim import cv_parse_intake_stream


class ExecuteAmbientIntakeWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "execute_ambient_intake_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = set()
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail"}

    cv_provider: str = "manual"


class ExecuteAmbientIntakeWorkflowInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    video_stream: bytes


async def execute_ambient_intake_workflow(
    config: ExecuteAmbientIntakeWorkflowConfig,
    input_data: ExecuteAmbientIntakeWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """接收顶棚 CV 信号，无需人工，直接把已预登记的批次点亮为 active。

    批次本身假定已经通过某个上游流程 (供应商上报/采购单) 预登记进
    inventory_batches (状态未必是 active)——这个函数只负责"物理到货确认"这
    一步：调 oprim.cv_parse_intake_stream 从视频流里识别出 batch_id/货架位/
    截图，然后把对应批次转 active、记下货架位与截图 URL，点亮微仓绿灯。
    如果 CV 识别不出 batch_id (没匹配上任何预登记批次)，直接判 failed，不
    瞎猜哪个批次到了。

    Args:
        config: ExecuteAmbientIntakeWorkflowConfig(cv_provider="manual")。
        input_data: video_stream (顶棚摄像头原始视频字节流)。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 batch_id / shelf_slot /
        shelf_image_url。
    """
    trail = Trail()
    fp = compute_fingerprint({})

    try:
        if pool is None:
            raise ValueError(
                "pool is required — execute_ambient_intake_workflow always touches persisted stock"
            )

        cv_result = await cv_parse_intake_stream(
            config.cv_provider, video_stream=input_data.video_stream
        )
        batch_id = cv_result.get("batch_id")
        if not batch_id:
            raise ValueError(
                "cv_parse_intake_stream could not identify a batch_id from the video stream"
            )
        trail.record(
            event="cv_intake_parsed",
            batch_id=batch_id,
            shelf_slot=cv_result.get("shelf_slot"),
        )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE \"inventory_batch\" SET status = 'active', "
                "shelf_image_url = $1, shelf_slot = $2 "
                "WHERE id = $3 RETURNING id",
                cv_result.get("snapshot_url"),
                cv_result.get("shelf_slot"),
                batch_id,
            )
            if row is None:
                raise ValueError(f"batch {batch_id!r} not found")
        trail.record(event="batch_lit_active", batch_id=batch_id)

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            batch_id=batch_id,
            shelf_slot=cv_result.get("shelf_slot"),
            shelf_image_url=cv_result.get("snapshot_url"),
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
