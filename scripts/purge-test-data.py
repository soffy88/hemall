"""scripts/purge-test-data.py — 一次性清扫共享 TEST_PG_DSN 上的测试遗留数据。

背景：hemall 的 DB 集成测试全部共用同一个开发库 (TEST_PG_DSN)。各套件
(phase2~phase9/oservi/e2e) 的种数据长期累积，导致库里堆满测试节点/批次/
商品/顾客 (曾实测 836 个微仓、796 个批次、752 个变体)，让"全局扫描型"
引擎 (竞对爬虫 / 协同过滤 / 衰减) 的测试断言变成跟数据库年龄赛跑。

本脚本删除所有**非种子**数据 (保留 init-scripts/002-seed-storefront.sql
写死的固定 UUID 行，特征：id 以 00000000-0000-4000-8000- 开头)，按 FK
依赖序清空测试数据图，把共享库恢复成"种子 + 空"的干净基线。此后每次测试
运行由各套件 fixture 的 tests/db_test_utils.cleanup_db_test_rows 自行控制
增量，不再需要本脚本。

用法:  TEST_PG_DSN=postgresql://... .venv/bin/python scripts/purge-test-data.py
幂等:  可重复执行 (第二次几乎没有可删的行)。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

DSN = os.environ.get("TEST_PG_DSN") or os.environ.get("HEMALL_PG_DSN")
if not DSN:
    raise SystemExit("TEST_PG_DSN/HEMALL_PG_DSN not set; refusing to guess the DB")

#: 种子数据固定 UUID 前缀 (init-scripts/002-seed-storefront.sql 写死)。
SEED_ID_PREFIX = "00000000-0000-4000-8000-%"


async def _main() -> None:
    from obase.persistence.pool import PgPool

    pool = await PgPool.create(name="hemall_purge", dsn=DSN, min_size=1, max_size=3)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1. 纯测试表 (无种子行) 全部清空——按 FK 依赖序排列：
                #    批次/订单/节点/变体/商品/顾客 的子表 → 主表。
                for table in (
                    # inventory_batch / customer_order 的直接子表
                    "batch_battle_report",
                    "claim",
                    "stock_movement",
                    "stock_location_loss_ledger",
                    "channel_broadcast_log",
                    "probe_order_log",
                    "cart_lock",
                    "device_purchase_log",
                    "order_line_item",
                    "douyin_conversion_log",
                    "order_status_history",
                    "fulfillment",
                    "return_request",
                    "swap",
                    # stock_location 的直接子表
                    "tote_deposit",
                    "tote",
                    "host_dividend_ledger",
                    "bounty_post",
                    "digital_lord_contract",
                    "product_safety_stock",
                    # product_variant / product / customer 的直接子表
                    "price_list_item",
                    "price_benchmark",
                    "crowd_intent",
                    "intention_order",
                    "membership",
                    "customer_address",
                    "product_option",
                    "product_collection_item",
                    "sales_channel_product",
                    "labor_ledger",
                    # 订单/购物车主表 (customer_order 引用 cart，须先删订单)
                    "customer_order",
                    "payment_session",
                    "cart_line_item",
                    "cart_discount",
                    "cart_gift_card",
                    "cart",
                ):
                    await conn.execute(f'DELETE FROM "{table}"')

                # 2. 主表只保留种子固定 UUID 行 (00000000-0000-4000-8000-*)
                for table in (
                    "inventory_batch",
                    "product_variant",
                    "product",
                    "stock_location",
                ):
                    await conn.execute(
                        f"DELETE FROM {table} WHERE id::text NOT LIKE '{SEED_ID_PREFIX}'"
                    )

                # customer 无种子行：测试注册的顾客全部清掉。
                await conn.execute("DELETE FROM customer")

                # region：种子只有 5 个固定 code，其余全是测试建的配送区域。
                # tax_rate.region_code 是 region 的唯一 FK 子表，先清。
                await conn.execute('DELETE FROM "tax_rate"')
                await conn.execute(
                    "DELETE FROM region WHERE code NOT IN "
                    "('cn-east','cn-north','cn-south','cn-southwest','cn-central')"
                )

                for table in (
                    "inventory_batch",
                    "product_variant",
                    "product",
                    "stock_location",
                    "customer",
                    "region",
                ):
                    remaining = await conn.fetchval(f"SELECT count(*) FROM {table}")
                    print(f"{table}: {remaining} rows remaining")

        print("purge complete — shared TEST_PG_DSN restored to seed baseline")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(_main())
