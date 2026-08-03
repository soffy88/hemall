"""app.ext.oskill_ghost_clustering — 幽灵节点空间聚类算子。

Phase 8 Task 1: C2B 幽灵节点点火闭环的空间维度。
纯内存计算，不查库，sync def —— 对齐 oskill 层"绝对无状态"红线。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


def _haversine_km(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """地球表面两点间 Haversine 距离 (公里)。"""
    earth_radius_km = 6371.0088
    lat1, lon1 = p1
    lat2, lon2 = p2
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return earth_radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


#: 幽灵节点点火阈值：一个聚类簇内最少意向订单数才触发物理建仓。
IGNITION_THRESHOLD = 100

#: 空间聚类半径 (公里)：同一簇内的意向订单中心点彼此不超过此距离。
CLUSTER_RADIUS_KM = 1.0


def cluster_intentions_by_radius(
    intention_orders: list[dict[str, Any]],
    *,
    radius_km: float = CLUSTER_RADIUS_KM,
    min_cluster_size: int = IGNITION_THRESHOLD,
) -> list[dict[str, Any]]:
    """对意向订单列表进行 DBSCAN-like 密度聚类 (基于固定半径的启发式聚合)。

    流程：
      1. 按坐标把意向订单分组为"种子簇"——以每个订单为圆心、radius_km
         内第一个未分配订单作为新簇种子，该簇所有成员继续扩展直到没有新邻居。
      2. 过滤掉低于 min_cluster_size 的小簇。
      3. 返回满足点火条件的簇信息。

    Args:
        intention_orders: 意向订单列表，每条至少含 "id", "lat", "lon"。
            其他字段 (variant_id, customer_id, prepaid_amount_cents) 保留
            透传到点火结果中供下游使用。
        radius_km: 聚类的最大簇直径半径 (默认 1km，跟空间冲突半径一致)。
        min_cluster_size: 最小簇规模 (默认 100，与 IGNITION_THRESHOLD 一致)。

    Returns:
        满足点火条件的簇列表，每项结构:
        {
            "cluster_id": str,          # "ghost_{"cluster_seed_idx"}"
            "center_lat": float,        # 簇内坐标均值
            "center_lon": float,
            "order_ids": [str, ...],    # 簇内意向单 ID
            "count": int,               # 意向单数量
            "total_prepaid_cents": int, # 意向金总额
            "variants": set[str],       # 涉及的 variant_id 集合
        }

    Raises:
        ValueError: intention_orders 为空或格式非法。
    """
    if not intention_orders:
        raise ValueError("cluster_intentions_by_radius: intention_orders must not be empty")

    # ── Step 1: DBSCAN-style 密度聚类 ──────────────────────────────
    assigned: set[int] = set()
    clusters: list[dict[str, Any]] = []

    for seed_idx, order in enumerate(intention_orders):
        if seed_idx in assigned:
            continue

        seed_lat = order["lat"]
        seed_lon = order["lon"]
        current_cluster: list[dict[str, Any]] = [order]
        assigned.add(seed_idx)

        # BFS 扩展：每次找当前簇边缘到未分配点的最近邻
        changed = True
        while changed:
            changed = False
            for i, candidate in enumerate(intention_orders):
                if i in assigned:
                    continue
                cand_lat = candidate["lat"]
                cand_lon = candidate["lon"]
                # 检查候选点到簇内任意已分配点的距离
                for member in current_cluster:
                    dist = _haversine_km(
                        (member["lat"], member["lon"]),
                        (cand_lat, cand_lon),
                    )
                    if dist <= radius_km:
                        current_cluster.append(candidate)
                        assigned.add(i)
                        changed = True
                        break

        # 只保留达到最小规模的簇
        if len(current_cluster) >= min_cluster_size:
            center_lat = sum(c["lat"] for c in current_cluster) / len(current_cluster)
            center_lon = sum(c["lon"] for c in current_cluster) / len(current_cluster)
            total_prepaid = sum(c.get("prepaid_amount_cents", 0) for c in current_cluster)
            variants = {str(c["variant_id"]) for c in current_cluster if c.get("variant_id")}

            clusters.append(
                {
                    "cluster_id": f"ghost_{seed_idx}",
                    "center_lat": round(center_lat, 6),
                    "center_lon": round(center_lon, 6),
                    "order_ids": [str(c["id"]) for c in current_cluster],
                    "count": len(current_cluster),
                    "total_prepaid_cents": total_prepaid,
                    "variants": variants,
                }
            )

    return clusters
