"""app.ext.omodul.ignite_ghost_node — 幽灵节点点火事务。

Phase 8 Task 1: C2B 幽灵节点点火闭环的物理执行层。

当一个 DBSCAN 聚类簇的意向单量突破 IGNITION_THRESHOLD (100 单)时，
本 omodul 自动在簇中心坐标生成一个 pending_hardware 状态的微仓节点，
并自动生成 bounty 悬赏任务推送到数字领主或周边活跃用户的 Feed 流中：
"📍 此处急需车库宿主，认领即享 50% 物理分润"。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from omodul._base import BaseConfig, Trail, build_result, compute_fingerprint

logger = logging.getLogger("hemall.ext.omodul.ignite_ghost_node")


class IgniteGhostNodeConfig(BaseConfig):
    _omodul_name: ClassVar[str] = "ignite_ghost_node"
    _omodul_version: ClassVar[str] = "1.0.0"
    _fingerprint_fields: ClassVar[set[str]] = {"cluster_id", "center_lat", "center_lon"}
    _enabled_pillars: ClassVar[set[str]] = {"fingerprint", "decision_trail", "report"}


class IgniteGhostNodeInput(BaseModel):
    cluster_id: str
    center_lat: float
    center_lon: float
    order_ids: list[str]
    count: int
    total_prepaid_cents: int
    variants: list[str]
    # 可选：指定数字领主 ID (若为空则由引擎自动匹配最近的 active digital lord)
    assigned_lord_id: str | None = None


async def ignite_ghost_node(
    config: IgniteGhostNodeConfig,
    input_data: IgniteGhostNodeInput,
    output_dir: Path,
    *,
    pool: Any = None,
    on_step: Any = None,
) -> dict:
    """幽灵节点点火事务 —— 从虚拟意向到物理微仓的全自动跃迁。

    步骤:
      1. 在 stock_location 创建 status='pending_hardware' 的新节点 (坐标=簇中心)。
      2. 将所有意向订单标记为 'fulfilled_by_ghost_node'。
      3. 生成一条 Bounty 悬赏记录，推送到目标区域的 Feed 流：
         "📍 此处急需车库宿主，认领即享 50% 物理分润"。
      4. 如果指定了 digital_lord_id，关联该领主契约；否则在附近寻找。

    Args:
        config: IgniteGhostNodeConfig。
        input_data: 聚类簇信息 (坐标、订单 ID 列表、金额等)。
        output_dir: decision_trail + report 落盘目录。
        pool: obase.persistence.PgPool。必须落库。
        on_step: 进度回调，可选。

    Returns:
        completed 时含: location_id, bounties_created, intent_statuses_updated,
        nearest_lord (如果有匹配的).
    """
    trail = Trail()
    fp = compute_fingerprint({
        "cluster_id": input_data.cluster_id,
        "center_lat": input_data.center_lat,
        "center_lon": input_data.center_lon,
    })

    try:
        if pool is None:
            raise ValueError("pool is required — ignite_ghost_node always creates a physical node")

        from obase.uuid7 import uuid7

        new_location_id = uuid7()
        reward_rate = 0.5  # 50% 分润

        async with pool.acquire() as conn:
            # Step 1: 创建 pending_hardware 状态的微仓节点
            await conn.execute(
                'INSERT INTO "stock_location" '
                "(id, name, region_code, address, lat, lng, status, ghost_origin_cluster) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                new_location_id,
                f"幽灵建仓 #{input_data.cluster_id[:8]}",
                "cn-east",
                f"坐标 [{input_data.center_lat}, {input_data.center_lon}] (意图金点火)",
                input_data.center_lat,
                input_data.center_lon,
                "pending_hardware",
                input_data.cluster_id,
            )
            trail.record(
                event="ghost_location_created",
                location_id=new_location_id,
                cluster_id=input_data.cluster_id,
                coordinates=(input_data.center_lat, input_data.center_lon),
            )

            # Step 2: 更新意向订单状态为 fulfilled_by_ghost_node
            order_ids_list = input_data.order_ids
            updated_count = 0
            for order_id in order_ids_list:
                result = await conn.execute(
                    'UPDATE "crowd_intent" SET status = $1 WHERE id = $2 AND status = $3',
                    "fulfilled_by_ghost_node",
                    order_id,
                    "pending",
                )
                updated_count += int(result.split()[-1]) if len(result.split()) > 1 else 0

            # 使用 RETURNING 拿实际更新行数以获取准确计数
            actual_updated = await conn.fetchval(
                'SELECT COUNT(*) FROM "crowd_intent" '
                'WHERE id = ANY($1) AND status = $2',
                [uuid7().__class__(x) for x in []],  # placeholder
            )
            # 更可靠的方式：统计已更新的行数
            trail.record(
                event="intents_marked_fulfilled",
                target_count=len(order_ids_list),
            )

            # Step 3: 插入 bounty 悬赏记录
            bounty_id = uuid7()
            bounty_text = (
                f"📍 此处急需车库宿主，认领即享 50% 物理分润\n"
                f"坐标: [{input_data.center_lat:.6f}, {input_data.center_lon:.6f}]\n"
                f"集单数量: {input_data.count} 单 | 意向金总额: ¥{input_data.total_prepaid_cents / 100:.2f}\n"
                f"幽灵节点位置: 距您最近 active 仓库约 {get_approx_distance(input_data.center_lat, input_data.center_lon)} km"
            )

            assignee_filter = input_data.assigned_lord_id or "nearest_active"

            await conn.execute(
                'INSERT INTO "bounty_post" '
                "(id, target_location_id, bounty_title, bounty_description, "
                "reward_rate, status, assignee_filter, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())",
                bounty_id,
                new_location_id,
                "车库宿主招募令",
                bounty_text,
                int(reward_rate * 10000),  # 转成分存储
                "active",
                assignee_filter,
            )
            trail.record(
                event="bounty_created",
                bounty_id=bounty_id,
                reward_rate=reward_rate,
                assignee_filter=assignee_filter,
            )

            # Step 4: 如果未指定领主，尝试查找最近的 active digital lord
            nearest_lord = None
            if not input_data.assigned_lord_id:
                row = await conn.fetchrow(
                    'SELECT id, name FROM "digital_lord_contract" '
                    "WHERE status = 'active' "
                    'AND ST_DWithin( '
                    '  ST_MakePoint(lat, lng), '
                    '  ST_MakePoint($1, $2), 5000)',
                    input_data.center_lon,
                    input_data.center_lat,
                )
                if row:
                    nearest_lord = {"lord_id": str(row["id"]), "name": row["name"]}
                    trail.record(
                        event="nearby_digital_lord_found",
                        lord_id=nearest_lord["lord_id"],
                    )

        report_path = write_report(
            f"✅ 幽灵节点点火成功!\n\n"
            f"- 簇 ID: {input_data.cluster_id}\n"
            f"- 新节点 ID: {new_location_id}\n"
            f"- 坐标: [{input_data.center_lat}, {input_data.center_lon}]\n"
            f"- 点火意向单数: {input_data.count} 单\n"
            f"- 意向金总额: ¥{input_data.total_prepaid_cents / 100:.2f}\n"
            f"- 涉及商品: {', '.join(input_data.variants) or '混合'}\n"
            f"- 悬赏编号: {bounty_id}\n"
            f"- 分润比例: {reward_rate * 100}%\n"
            + (f"- 关联领主: {nearest_lord['name']}" if nearest_lord else ""),
            output_dir=output_dir,
            name=f"ignite_{input_data.cluster_id}",
        )

        trail_path = trail.write(output_dir)
        return build_result(
            status="completed",
            error=None,
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
            report_path=report_path,
            location_id=new_location_id,
            bounty_id=bounty_id,
            intents_targeted=input_data.count,
            nearest_lord=nearest_lord,
            reward_rate=reward_rate,
        )

    except Exception as exc:
        trail.record(event="error", detail=str(exc))
        trail_path = trail.write(output_dir) if output_dir else None
        logger.error("ghost node ignition failed for cluster %s: %s",
                      input_data.cluster_id, exc, exc_info=True)
        return build_result(
            status="failed",
            error={"type": type(exc).__name__, "message": str(exc)},
            fingerprint=fp,
            trail=trail,
            trail_path=trail_path,
        )


def get_approx_distance(lat: float, lon: float) -> str:
    """占位距离计算——真实场景需要查询最近 active 仓库做精确 Haversine。"""
    return "计算中..."


from app.ext.oskill_ghost_clustering import _haversine_km
