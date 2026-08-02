"""app.ext.omodul.claim_origin_workflow — 果农 PWA 供应商注册。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint


class ClaimOriginWorkflowConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "claim_origin_workflow"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"wallet_account"}
    _enabled_pillars: ClassVar[set[str]] = {"decision_trail"}


class ClaimOriginWorkflowInput(BaseModel):
    wallet_account: str
    spatial_polygon: dict[str, Any]  # GeoJSON Polygon
    # v4.0 §4 追加：社交播报文案 (construct_fomo_user_prompt) 需要一个人类
    # 可读的产地名，spatial_polygon 只是坐标——可选，不填就是 NULL。
    polygon_name: str | None = None


async def claim_origin_workflow(
    config: ClaimOriginWorkflowConfig,
    input_data: ClaimOriginWorkflowInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """果农 PWA 注册：录入 spatial_polygon (原产地物理围栏)，创建供应商账号，
    trust_score/escrow_balance/status 全部用 schema 默认值 (100/0/sandbox)，
    不在这里手写默认值防止跟建表 DDL 脱节。

    Args:
        config: ClaimOriginWorkflowConfig。
        input_data: wallet_account / spatial_polygon (GeoJSON Polygon dict，
            至少 {"type": "Polygon", "coordinates": [[[lon, lat], ...]]})。
        output_dir: decision_trail 落盘目录。
        pool: obase.persistence.PgPool。必须落库，pool 为 None 直接判 failed。
        on_step: 进度回调，可选。

    Returns:
        标准 omodul 返回 dict；completed 时含 supplier_id / status ("sandbox")。
    """
    trail = Trail()
    fp = compute_fingerprint({"wallet_account": input_data.wallet_account})

    try:
        if pool is None:
            raise ValueError(
                "pool is required — claim_origin_workflow always touches persisted stock"
            )
        if input_data.spatial_polygon.get("type") != "Polygon":
            raise ValueError("spatial_polygon must be a GeoJSON Polygon")
        rings = input_data.spatial_polygon.get("coordinates") or []
        if not rings or len(rings[0]) < 3:
            raise ValueError("spatial_polygon outer ring must have at least 3 points")

        from obase.uuid7 import uuid7

        supplier_id = uuid7()
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO "supplier" (id, wallet_account, spatial_polygon, polygon_name) '
                "VALUES ($1, $2, $3, $4)",
                supplier_id,
                input_data.wallet_account,
                json.dumps(input_data.spatial_polygon),
                input_data.polygon_name,
            )
        trail.record(
            event="supplier_registered",
            supplier_id=supplier_id,
            wallet_account=input_data.wallet_account,
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            supplier_id=supplier_id,
            status_value="sandbox",
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
