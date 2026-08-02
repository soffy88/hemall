"""app.ext.omodul.commission_new_location — 新微仓节点注册。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import (
    BaseConfig,
    Trail,
    build_result,
    compute_fingerprint,
    write_report,
)

from ..oskill import recompute_voronoi_grid

#: 共享 stock_location 表要求 region_code NOT NULL，但没有区域概念的
#: ClearNode 节点从没填过这个字段——统一之后借用商城默认区域码，不新增
#: Input 字段 (调用方目前也没有区域信息可传)。
_DEFAULT_REGION_CODE = "cn-east"


class CommissionNewLocationConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "commission_new_location"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"host_id", "address"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail", "report"}


class CommissionNewLocationInput(BaseModel):
    host_id: str
    address: str
    lat: float
    lon: float


async def commission_new_location(
    config: CommissionNewLocationConfig,
    input_data: CommissionNewLocationInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """新节点车库通电联网后，自动执行基础数据注册，拉起空间网格重绘。

    注册进 stock_locations 后，拉全部 active 节点坐标 (含新节点自己) 调
    oskill.recompute_voronoi_grid，算出新节点的最近邻——这就是"新店上线秒级
    自动分流"要的信息，下游路由/派单只要知道"这个节点从谁那儿抢流量"。
    只有新节点自己 (全城仅此一个 active 节点) 时，Voronoi 图没有意义
    (少于 2 个点)，如实记录 grid_recomputed=False，不假装算出了结果。

    Args:
        config: CommissionNewLocationConfig。
        input_data: host_id / address / lat / lon。
        output_dir: decision_trail + report 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 location_id / grid_recomputed
        (bool) / neighbors (grid_recomputed=True 时该节点的最近邻列表)。
    """
    trail = Trail()
    fp = compute_fingerprint(
        {"host_id": input_data.host_id, "address": input_data.address}
    )

    try:
        if pool is None:
            raise ValueError(
                "pool is required — commission_new_location always touches persisted stock"
            )

        from obase.uuid7 import uuid7

        location_id = uuid7()
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO "stock_location" '
                "(id, name, region_code, host_id, address, lat, lng, status) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, 'active')",
                location_id,
                input_data.address,
                _DEFAULT_REGION_CODE,
                input_data.host_id,
                input_data.address,
                input_data.lat,
                input_data.lon,
            )
            trail.record(
                event="location_registered",
                location_id=location_id,
                host_id=input_data.host_id,
            )

            # 共享 stock_location 的 lat/lng 是可空列 (通用商城不强制要求
            # 门店有坐标)——ClearNode 自己的旧版 lat/lon 是 NOT NULL，统一
            # 之后不能再假设"active 节点都有坐标"，WHERE 里过滤掉没坐标的。
            all_active = await conn.fetch(
                'SELECT id, lat, lng FROM "stock_location" '
                "WHERE status = 'active' AND lat IS NOT NULL AND lng IS NOT NULL"
            )

        neighbors: list[str] = []
        grid_recomputed = False
        if len(all_active) >= 2:
            # asyncpg 把 UUID 列驱动成 uuid.UUID 对象，这里转成字符串——
            # 下面 grid[location_id] 用字符串 location_id 查表，类型不一致
            # 会直接 KeyError。
            node_coordinates = [
                (str(r["id"]), float(r["lat"]), float(r["lng"])) for r in all_active
            ]
            grid = recompute_voronoi_grid(node_coordinates)
            neighbors = grid[location_id]["neighbors"]
            grid_recomputed = True
            trail.record(
                event="grid_recomputed",
                location_id=location_id,
                neighbors=neighbors,
                total_nodes=len(all_active),
            )
        else:
            trail.record(
                event="grid_recompute_skipped",
                detail="fewer than 2 active locations; Voronoi grid undefined",
            )

        report_path = write_report(
            f"新节点 {location_id} 已注册\n\n"
            f"- host_id: {input_data.host_id}\n"
            f"- address: {input_data.address}\n"
            f"- lat/lon: {input_data.lat}, {input_data.lon}\n\n"
            + (
                f"空间网格已重绘，最近邻节点: {', '.join(neighbors) or '(无)'}\n"
                if grid_recomputed
                else "全城仅此一个节点，暂不构成 Voronoi 网格。\n"
            ),
            output_dir=output_dir,
            name=f"location_{location_id}_commission",
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            report_path=report_path,
            location_id=location_id,
            grid_recomputed=grid_recomputed,
            neighbors=neighbors,
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
