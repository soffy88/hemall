"""tests/db_test_utils.py — DB 集成测试共享工具：环境债清扫。

DB 集成测试共用同一个 TEST_PG_DSN (开发/CI 共享库)；各套件种数据长期累积
会让"全局扫描型"引擎 (竞对爬虫 / 协同过滤 / 衰减引擎) 的测试变成跟数据库
年龄赛跑——扫描预算扫不到本次新种的坐标，断言就开始碰运气。

这里提供按稳定命名前缀 + 完整 FK 依赖序的清扫函数，供各套件的 cn_pool
fixture 在 seeding 前调用，让每次运行回到"只有本次新种数据"的确定性状态。
只删除命名前缀命中的测试遗留行，不动种子数据 (00000000-... 固定 UUID) 与
任何人工数据。
"""

from __future__ import annotations

from typing import Any

import asyncpg

# ── 各套件种数据的稳定命名模式 ──────────────────────────────────────────
# phase7/skypatch/phase9 (本仓库仍在运行的套件) 每次运行都各自清扫；
# 历史套件 (p2-p9/oservi/e2e) 的遗留由 scripts/purge-test-data.py 一次性清。
LOCATION_NAMES = (
    "phase7_loc",
    "skypatch_loc",
    "p9_loc",  # phase9 套件种的节点
)
BATCH_PATTERNS = ("BATCH-%", "skypatch-batch-%")
VARIANT_PATTERNS = ("SKU-%", "skypatch-sku-%")
PRODUCT_PATTERNS = ("phase7-slug-%", "skypatch-slug-%", "p9-slug-%")
CUSTOMER_PATTERNS = ("p7-%@test.dev", "p9-%@test.dev", "p9b-%@test.dev")
DEVICE_IDS = ("dev-phase7-test", "dev-dedup-test")


async def _delete(conn: Any, table: str, where: str) -> None:
    """删除某表符合 where 条件的行；表不存在 (环境缺 DDL) 时静默跳过。"""
    try:
        await conn.execute(f'DELETE FROM "{table}" {where}')
    except asyncpg.exceptions.UndefinedTableError:
        pass


async def cleanup_db_test_rows(pool: Any) -> None:
    """按命名前缀清扫本仓库各 DB 集成套件的遗留测试行 (幂等，FK 依赖序)。

    子表先行：凡 FK 指向 批次/节点/变体/商品/顾客/订单 的引用行全部清掉，
    再删主表。所有 DELETE 都带表存在性守卫——不同环境缺个别扩展表不炸。
    """
    batches = "SELECT id FROM inventory_batch WHERE " + " OR ".join(
        f"batch_no LIKE '{p}'" for p in BATCH_PATTERNS
    )
    locations = (
        "SELECT id FROM stock_location WHERE name IN ("
        + ", ".join(f"'{n}'" for n in LOCATION_NAMES)
        + ")"
    )
    variants = "SELECT id FROM product_variant WHERE " + " OR ".join(
        f"sku_code LIKE '{p}'" for p in VARIANT_PATTERNS
    )
    products = "SELECT id FROM product WHERE " + " OR ".join(
        f"slug LIKE '{p}'" for p in PRODUCT_PATTERNS
    )
    customers = "SELECT id FROM customer WHERE " + " OR ".join(
        f"email LIKE '{p}'" for p in CUSTOMER_PATTERNS
    )
    orders = f"SELECT id FROM customer_order WHERE customer_id IN ({customers})"

    async with pool.acquire() as conn:
        # 0. Phase 10 IoT 账本 (FK 批次; 按测试命名约定清) ——先于批次主表删除。
        await _delete(
            conn,
            "hardware_event",
            f"WHERE batch_id IN ({batches}) OR tote_id LIKE 'tote-%'",
        )
        await _delete(
            conn,
            "hardware_shelf",
            f"WHERE batch_id IN ({batches}) OR node_id LIKE 'n-iot-%'",
        )
        await _delete(
            conn, "hardware_gate", "WHERE node_id LIKE 'n-iot-%' OR gate_id LIKE 'g-%'"
        )

        # 1. 批次/订单/顾客/变体/商品的直接子表 (先子后父)
        await _delete(
            conn,
            "batch_battle_report",
            f"WHERE order_id IN ({orders}) OR batch_id IN ({batches}) OR user_id IN ({customers})",
        )
        await _delete(
            conn,
            "claim",
            f"WHERE batch_id IN ({batches}) OR user_id IN ({customers}) OR order_id IN ({orders})",
        )
        await _delete(
            conn,
            "stock_movement",
            f"WHERE batch_id IN ({batches}) OR location_id IN ({locations})",
        )
        await _delete(
            conn,
            "stock_location_loss_ledger",
            f"WHERE batch_id IN ({batches}) OR location_id IN ({locations})",
        )
        await _delete(conn, "channel_broadcast_log", f"WHERE batch_id IN ({batches})")
        await _delete(conn, "probe_order_log", f"WHERE batch_id IN ({batches})")
        await _delete(conn, "cart_lock", f"WHERE batch_id IN ({batches})")
        await _delete(conn, "cart_line_item", f"WHERE batch_id IN ({batches})")
        await _delete(
            conn,
            "device_purchase_log",
            f"WHERE device_id IN {tuple(DEVICE_IDS)} "
            f"OR batch_id IN ({batches}) OR variant_id IN ({variants}) "
            f"OR product_id IN ({products}) OR order_id IN ({orders})",
        )
        await _delete(
            conn,
            "order_line_item",
            f"WHERE batch_id IN ({batches}) OR order_id IN ({orders})",
        )
        await _delete(conn, "douyin_conversion_log", f"WHERE order_id IN ({orders})")
        await _delete(conn, "order_status_history", f"WHERE order_id IN ({orders})")
        await _delete(conn, "fulfillment", f"WHERE order_id IN ({orders})")
        await _delete(conn, "return_request", f"WHERE order_id IN ({orders})")
        await _delete(conn, "swap", f"WHERE order_id IN ({orders})")
        await _delete(conn, "customer_order", f"WHERE customer_id IN ({customers})")

        # 2. 节点子表
        await _delete(
            conn,
            "tote_deposit",
            f"WHERE tote_id IN (SELECT id FROM tote WHERE current_location_id IN ({locations}))",
        )
        await _delete(conn, "tote", f"WHERE current_location_id IN ({locations})")
        await _delete(
            conn, "host_dividend_ledger", f"WHERE location_id IN ({locations})"
        )
        await _delete(conn, "bounty_post", f"WHERE target_location_id IN ({locations})")
        await _delete(
            conn, "digital_lord_contract", f"WHERE location_id IN ({locations})"
        )
        await _delete(
            conn, "product_safety_stock", f"WHERE location_id IN ({locations})"
        )

        # 3. 变体/商品/顾客子表
        await _delete(conn, "price_list_item", f"WHERE variant_id IN ({variants})")
        # spider 源条目由爬虫引擎写入，含未匹配 (variant_id NULL) 的行——按
        # 变体前缀扫不到它们，按 source_type 一并清 (测试库里都是测试数据)。
        await _delete(
            conn,
            "price_benchmark",
            f"WHERE variant_id IN ({variants}) OR source_type = 'spider'",
        )
        await _delete(
            conn,
            "crowd_intent",
            f"WHERE variant_id IN ({variants}) OR customer_id IN ({customers})",
        )
        await _delete(
            conn,
            "intention_order",
            f"WHERE variant_id IN ({variants}) OR customer_id IN ({customers})",
        )
        await _delete(conn, "membership", f"WHERE customer_id IN ({customers})")
        await _delete(conn, "customer_address", f"WHERE customer_id IN ({customers})")
        await _delete(conn, "product_option", f"WHERE product_id IN ({products})")
        await _delete(
            conn, "product_collection_item", f"WHERE product_id IN ({products})"
        )
        await _delete(
            conn, "sales_channel_product", f"WHERE product_id IN ({products})"
        )

        # 4. 主表
        await _delete(
            conn,
            "inventory_batch",
            "WHERE " + " OR ".join(f"batch_no LIKE '{p}'" for p in BATCH_PATTERNS),
        )
        await _delete(
            conn,
            "product_variant",
            "WHERE " + " OR ".join(f"sku_code LIKE '{p}'" for p in VARIANT_PATTERNS),
        )
        await _delete(
            conn,
            "product",
            "WHERE " + " OR ".join(f"slug LIKE '{p}'" for p in PRODUCT_PATTERNS),
        )
        await _delete(
            conn,
            "stock_location",
            "WHERE name IN (" + ", ".join(f"'{n}'" for n in LOCATION_NAMES) + ")",
        )
        await _delete(
            conn,
            "customer",
            "WHERE "
            + " OR ".join(f"email LIKE '{p}'" for p in CUSTOMER_PATTERNS)
            + " OR invited_by IN ("
            + customers
            + ")",
        )
