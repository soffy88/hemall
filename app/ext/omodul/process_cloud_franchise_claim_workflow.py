"""app.ext.omodul.process_cloud_franchise_claim_workflow — 抖音粉丝云加盟认领。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

from ..oskill import check_spatial_conflict

#: 共享 stock_location 表要求 region_code NOT NULL，云加盟认领没有区域概念
#: 可传——跟 commission_new_location 同一个默认值。
_DEFAULT_REGION_CODE = "cn-east"


class ProcessCloudFranchiseClaimWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "process_cloud_franchise_claim_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"douyin_uid"}
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail"}


class ProcessCloudFranchiseClaimWorkflowInput(BaseModel):
    douyin_uid: str
    address: str
    lat: float
    lon: float


async def process_cloud_franchise_claim_workflow(
    config: ProcessCloudFranchiseClaimWorkflowConfig,
    input_data: ProcessCloudFranchiseClaimWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """抖音粉丝 0 元下单认领【微仓硬件盲盒】的事务：秒级核验该 GPS 坐标方圆
    1 公里内是否已有节点 (含还没装硬件的 pending_hardware 节点，它们也已经
    物理占了地)，无冲突则批准云加盟，生成新节点 (status='pending_hardware')。

    空间冲突判定用 oskill.check_spatial_conflict (纯 Python haversine)，不是
    SPEC 原文的 PostGIS ``ST_DWithin``——共享 stock_location 表只有 lat/lng
    两列，没有启用 PostGIS 扩展，跟供应商溯源/空间围栏点火全部走 haversine
    是同一个既有约定 (见该 oskill 函数的 docstring)。

    冲突判定拒绝走的是"业务决策"分支 (status=completed, decision=rejected)，
    不是异常/failed——跟 execute_liability_routing_workflow 的 rejected 分支
    同一个先例，"认领被拒"是一个正常的业务结果，不是系统故障。

    认领成功只写 douyin_host_uid，不写 host_id (VARCHAR(32)，装不下
    douyin_uid 的 VARCHAR(64) 定长，也没有必要——host_id 是传统入驻流程走完
    后才会有的"正式场地宿主"标识，这条云加盟认领路径此刻还没有那个概念，
    如实留 NULL)。真正的"点火"(status 转 active + 领主契约生效) 由
    bind_digital_lord_contract_workflow 在意向金达标时触发，两个 omodul
    共同完成 SPEC 描述的"认领 → 点火"两阶段流程。

    Args:
        config: ProcessCloudFranchiseClaimWorkflowConfig。
        input_data: douyin_uid / address / lat / lon。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 decision ("approved"/
        "rejected")，approved 时另含 location_id。
    """
    trail = Trail()
    fp = compute_fingerprint({"douyin_uid": input_data.douyin_uid})

    try:
        if pool is None:
            raise ValueError(
                "pool is required — process_cloud_franchise_claim_workflow always touches persisted stock"
            )

        async with pool.acquire() as conn:
            existing = await conn.fetch(
                'SELECT lat, lng FROM "stock_location" '
                "WHERE lat IS NOT NULL AND lng IS NOT NULL"
            )
        existing_coords = [(float(r["lat"]), float(r["lng"])) for r in existing]

        conflict = check_spatial_conflict(
            (input_data.lat, input_data.lon), existing_coords
        )
        trail.record(
            event="spatial_conflict_checked",
            conflict=conflict,
            existing_node_count=len(existing_coords),
        )

        if conflict:
            trail_path = trail.write(output_dir)
            return build_result(
                status="completed",
                error=None,
                fingerprint=fp,
                trail=trail,
                trail_path=trail_path,
                decision="rejected",
                reason="spatial_conflict",
            )

        from obase.uuid7 import uuid7

        location_id = uuid7()
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO "stock_location" '
                "(id, name, region_code, address, lat, lng, status, douyin_host_uid) "
                "VALUES ($1, $2, $3, $4, $5, $6, 'pending_hardware', $7)",
                location_id,
                input_data.address,
                _DEFAULT_REGION_CODE,
                input_data.address,
                input_data.lat,
                input_data.lon,
                input_data.douyin_uid,
            )
        trail.record(
            event="claim_approved",
            location_id=location_id,
            douyin_uid=input_data.douyin_uid,
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            decision="approved",
            location_id=location_id,
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
