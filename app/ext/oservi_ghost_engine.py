"""app.ext.oservi_ghost_engine — 幽灵节点点火引擎。

Phase 8 Task 1: oservi 层面的自动化调度 —— 扫描意向金池，执行空间聚类，
触发点火事务。

引擎职责：
  - on_interval (每 5 分钟): 扫描全局 pending 状态的 crowd_intent /
    intention_order，按坐标执行 DBSCAN 聚类。
  - 对每个达到阈值 (≥100 单) 的簇，拉起 ignite_ghost_node omodul。
  - 生成 bounty 悬赏并推送通知。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from .oskill_ghost_clustering import cluster_intentions_by_radius, IGNITION_THRESHOLD
from .omodul.ignite_ghost_node import (
    IgniteGhostNodeConfig,
    IgniteGhostNodeInput,
    ignite_ghost_node,
)
from .oprim import db_query_many
from oservi.engines.cron_scheduler_engine import CronSchedulerEngine

logger = logging.getLogger("hemall.ext.oservi_ghost")


def _output_dir(settings: Settings, engine_name: str, omodul_name: str) -> Path:
    """oservi 引擎内部调用 omodul 时用的 output_dir。"""
    out = Path(settings.output_root) / "oservi" / engine_name / omodul_name
    out.mkdir(parents=True, exist_ok=True)
    return out


def build_ghost_ignition_engine(
    pool: Any, *, settings: Settings, notification_provider: str = "log"
) -> CronSchedulerEngine:
    """幽灵节点点火引擎 (on_interval，每 5 分钟)。

    扫描所有 pending 状态的意向订单 (crowd_intent + intention_order)，
    将它们合并为统一的空间数据源 → 调 oskill.cluster_intentions_by_radius
    做 1km 半径聚类 → 对达标簇逐个拉起 ignite_ghost_node omodul。

    如果该区域已有 active 微仓，跳过点火 (避免重复建仓冲突)。
    """

    async def ghost_ignition_tick(**_: Any) -> dict[str, Any]:
        now = datetime.now(UTC)
        # 获取最近 7 天内产生的 pending 意向单 (带坐标信息)
        # crowd_intent 表本身不含 lat/lon，需要从 customer 或外部关联表获取
        # intention_order 是 Phase 8 新增表，直接含坐标

        intention_orders_raw: list[dict[str, Any]] = []

        # ── 从 intention_order 表采集 (Phase 8 新表) ────────────────
        intention_order_rows = await db_query_many(
            pool,
            sql=(
                'SELECT id, variant_id, customer_id, lat, lng, prepaid_cents, status '
                'FROM "intention_order" '
                "WHERE status = 'pending' AND created_at > NOW() - INTERVAL '7 days'"
            ),
        )
        for row in intention_order_rows:
            if row.get("lat") is not None and row.get("lng") is not None:
                intention_orders_raw.append({
                    "id": str(row["id"]),
                    "variant_id": str(row.get("variant_id", "")),
                    "customer_id": str(row.get("customer_id", "")),
                    "lat": float(row["lat"]),
                    "lon": float(row["lng"]),
                    "prepaid_amount_cents": int(row.get("prepaid_cents", 0)),
                })

        # ── 从 crowd_intent 补充 (有 GPS 数据的旧意向单) ────────────
        # crowd_intent 没有直接的 lat/lon 列，但可以通过 customer 表关联地址
        # 暂时简化处理：只采集 intention_order 表的数据，crowd_intent
        # 需要前端配合上报 GPS 后才入库 intention_order 视图层
        crowd_intent_rows = await db_query_many(
            pool,
            sql=(
                'SELECT ci.id, ci.variant_id, ci.customer_id, ci.prepaid_amount_cents, '
                'c.lat AS lat, c.lng AS lon '
                'FROM "crowd_intent" ci '
                'JOIN "customer" c ON c.id = ci.customer_id '
                "WHERE ci.status = 'pending' "
                "AND ci.created_at > NOW() - INTERVAL '7 days' "
                "AND c.lat IS NOT NULL AND c.lng IS NOT NULL"
            ),
        )
        for row in crowd_intent_rows:
            if row.get("lat") is not None and row.get("lng") is not None:
                intention_orders_raw.append({
                    "id": str(row["id"]),
                    "variant_id": str(row.get("variant_id", "")),
                    "customer_id": str(row.get("customer_id", "")),
                    "lat": float(row["lat"]),
                    "lon": float(row["lng"]),
                    "prepaid_amount_cents": int(row.get("prepaid_amount_cents", 0)),
                })

        if not intention_orders_raw:
            return {"scanned": 0, "clusters_found": 0, "nodes_ignited": [], "errors": []}

        # ── 空间聚类 ─────────────────────────────────────────────────
        clusters = cluster_intentions_by_radius(intention_orders_raw)
        logger.info(
            "ghost ignition: scanned %d orders, found %d clusters",
            len(intention_orders_raw), len(clusters),
        )

        ignited_nodes: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []

        for cluster in clusters:
            cluster_id = cluster["cluster_id"]

            # 安全检查: 该坐标附近是否已有 active 微仓?
            near_existing = await db_query_many(
                pool,
                sql=(
                    'SELECT id FROM "stock_location" '
                    "WHERE lat IS NOT NULL AND lng IS NOT NULL "
                    "AND ST_DWithin( "
                    "  ST_MakePoint(lat, lng), "
                    "  ST_MakePoint($1, $2), 0.5)"
                ),
                params=(cluster["center_lon"], cluster["center_lat"]),
            )
            if near_existing:
                skipped.append({
                    "cluster_id": cluster_id,
                    "reason": "nearby_active_node_exists",
                })
                continue

            # 拉点火 omodul
            result = await ignite_ghost_node(
                IgniteGhostNodeConfig(),
                IgniteGhostNodeInput(
                    cluster_id=cluster_id,
                    center_lat=cluster["center_lat"],
                    center_lon=cluster["center_lon"],
                    order_ids=cluster["order_ids"],
                    count=cluster["count"],
                    total_prepaid_cents=cluster["total_prepaid_cents"],
                    variants=list(cluster["variants"]),
                    assigned_lord_id=None,  # 让 omodul 自动匹配附近领主
                ),
                _output_dir(settings, "ghost_ignition_engine", "ignite_ghost_node"),
                pool=pool,
            )

            if result["status"] == "completed":
                ignited_nodes.append({
                    "cluster_id": cluster_id,
                    "location_id": result.get("location_id"),
                    "bounty_id": result.get("bounty_id"),
                    "count": cluster["count"],
                    "nearest_lord": result.get("nearest_lord"),
                })
            else:
                errors.append({
                    "cluster_id": cluster_id,
                    "error": result.get("error", {}).get("message", "unknown"),
                })

        return {
            "scanned_orders": len(intention_orders_raw),
            "clusters_found": len(clusters),
            "clusters_ignited": len(ignited_nodes),
            "clusters_skipped": len(skipped),
            "nodes_ignited": ignited_nodes,
            "skipped_reasons": skipped,
            "errors": errors,
        }

    ghost_ignition_tick.__name__ = "ghost_ignition_tick"

    return CronSchedulerEngine(
        tasks=[ghost_ignition_tick],
        trigger={"on_interval": 300},
        config={"interval_seconds": 300},
        name="ext-ghost-ignition",
    )
