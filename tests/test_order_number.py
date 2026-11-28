"""tests/test_order_number.py — 柜台订单号 YYYYMMDD-NNNN。

覆盖：
- 纯函数：拼号、格式校验（非法日期/序号拒绝）。
- DB 集成 (TEST_PG_DSN)：同一天序号递增、跨天重置、10 路并发不重号、
  customer_order.order_no 唯一索引拒重号、默认取北京时间今天。
- 柜台结算集成见 tests/test_counter_settlement_order.py
  （test_settle_assigns_sequential_unique_order_no）。
- 测试用日期固定在 2031 年，与真实业务日期隔离；teardown 按日期清表。
"""

from __future__ import annotations

import asyncio
import os
from datetime import date

import pytest

from app.order_number import (
    beijing_today,
    format_order_no,
    is_valid_order_no,
    next_order_no,
)

TEST_DSN = os.environ.get("TEST_PG_DSN")

DB_TESTS = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


# ── 纯函数 ─────────────────────────────────────────────────────────────────


class TestFormatOrderNo:
    def test_format(self):
        assert format_order_no(date(2026, 11, 28), 1) == "20261128-0001"
        assert format_order_no(date(2026, 11, 28), 42) == "20261128-0042"
        assert format_order_no(date(2026, 11, 28), 1234) == "20261128-1234"

    def test_invalid_seq_raises(self):
        with pytest.raises(ValueError):
            format_order_no(date(2026, 11, 28), 0)
        with pytest.raises(ValueError):
            format_order_no(date(2026, 11, 28), -1)
        with pytest.raises(ValueError):
            format_order_no(date(2026, 11, 28), True)


class TestIsValidOrderNo:
    def test_valid(self):
        assert is_valid_order_no("20261128-0001") is True
        assert is_valid_order_no("20260228-9999") is True

    def test_invalid(self):
        assert is_valid_order_no("20261128-001") is False
        assert is_valid_order_no("20261128-00001") is False
        assert is_valid_order_no("202611280001") is False
        assert is_valid_order_no("20261301-0001") is False
        assert is_valid_order_no("20260230-0001") is False
        assert is_valid_order_no(None) is False
        assert is_valid_order_no(20261128) is False


# ── DB 集成 ────────────────────────────────────────────────────────────────


@pytest.fixture
async def order_number_pool():
    from obase.persistence.pool import PgPool

    from app.p0_schema import ensure_p0_schema

    pool = await PgPool.create(
        name="hemall_order_number_test", dsn=TEST_DSN, min_size=1, max_size=10
    )
    await ensure_p0_schema(pool)
    used_dates: list[date] = []
    yield pool, used_dates
    async with pool.acquire() as conn:
        if used_dates:
            await conn.execute(
                "DELETE FROM order_number_sequence WHERE order_date = ANY($1::date[])",
                used_dates,
            )
            prefixes = [d.strftime("%Y%m%d") + "-%" for d in used_dates]
            await conn.execute(
                'DELETE FROM "customer_order" WHERE order_no LIKE ANY($1::text[])',
                prefixes,
            )
    await pool.close()


async def _next(pool, day: date) -> str:
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await next_order_no(conn, order_date=day)


@DB_TESTS
class TestNextOrderNoDb:
    async def test_sequence_increments_same_day(self, order_number_pool):
        pool, used = order_number_pool
        day = date(2031, 3, 5)
        used.append(day)
        assert await _next(pool, day) == "20310305-0001"
        assert await _next(pool, day) == "20310305-0002"
        assert await _next(pool, day) == "20310305-0003"

    async def test_sequence_resets_next_day(self, order_number_pool):
        pool, used = order_number_pool
        day1 = date(2031, 3, 6)
        day2 = date(2031, 3, 7)
        used.extend([day1, day2])
        assert await _next(pool, day1) == "20310306-0001"
        assert await _next(pool, day1) == "20310306-0002"
        assert await _next(pool, day2) == "20310307-0001"
        assert await _next(pool, day1) == "20310306-0003"

    async def test_concurrent_calls_never_duplicate(self, order_number_pool):
        pool, used = order_number_pool
        day = date(2031, 3, 8)
        used.append(day)

        async def one() -> str:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    return await next_order_no(conn, order_date=day)

        got = await asyncio.gather(*[one() for _ in range(10)])
        assert len(set(got)) == 10
        assert sorted(got) == [f"20310308-{i:04d}" for i in range(1, 11)]

    async def test_default_date_is_beijing_today(self, order_number_pool):
        pool, used = order_number_pool
        today = beijing_today()
        used.append(today)
        async with pool.acquire() as conn:
            async with conn.transaction():
                got = await next_order_no(conn)
        assert is_valid_order_no(got)
        assert got.startswith(today.strftime("%Y%m%d"))

    async def test_order_no_unique_index_rejects_duplicates(self, order_number_pool):
        from asyncpg import UniqueViolationError
        from obase.uuid7 import uuid7

        pool, used = order_number_pool
        day = date(2031, 3, 9)
        used.append(day)
        insert = (
            'INSERT INTO "customer_order" (id, customer_id, currency, status, '
            "subtotal_cents, discount_cents, tax_cents, shipping_cents, "
            "grand_total_cents, billing_address, shipping_address, discount_kind, "
            "voucher_redeemed_cents, channel, order_no) "
            "VALUES ($1, NULL, 'CNY', 'completed', 100, 0, 0, 0, 100, '{}'::jsonb, "
            "'{}'::jsonb, 'none', 0, 'counter', '20310309-0001')"
        )
        async with pool.acquire() as conn:
            first_id, dup_id = uuid7(), uuid7()
            null_id1, null_id2 = uuid7(), uuid7()
            await conn.execute(insert, first_id)
            with pytest.raises(UniqueViolationError):
                await conn.execute(insert, dup_id)
            # NULL 订单号不占唯一位：多行留空不冲突。
            await conn.execute(
                'INSERT INTO "customer_order" (id, customer_id, currency, status, '
                "subtotal_cents, discount_cents, tax_cents, shipping_cents, "
                "grand_total_cents, billing_address, shipping_address, discount_kind, "
                "voucher_redeemed_cents, channel) "
                "VALUES ($1, NULL, 'CNY', 'completed', 100, 0, 0, 0, 100, '{}'::jsonb, "
                "'{}'::jsonb, 'none', 0, 'counter')",
                null_id1,
            )
            await conn.execute(
                'INSERT INTO "customer_order" (id, customer_id, currency, status, '
                "subtotal_cents, discount_cents, tax_cents, shipping_cents, "
                "grand_total_cents, billing_address, shipping_address, discount_kind, "
                "voucher_redeemed_cents, channel) "
                "VALUES ($1, NULL, 'CNY', 'completed', 100, 0, 0, 0, 100, '{}'::jsonb, "
                "'{}'::jsonb, 'none', 0, 'counter')",
                null_id2,
            )
            # NULL 行不在 teardown 的 LIKE 清理范围内，在此按 id 清掉。
            await conn.execute(
                'DELETE FROM "customer_order" WHERE id = ANY($1::uuid[])',
                [first_id, null_id1, null_id2],
            )
