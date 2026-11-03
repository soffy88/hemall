"""tests/test_counter_closeout.py — 柜台收尾件：现金结算 + 交接班 + 日结报表。

覆盖：
- 纯函数：找零算术、交班应有/差异算术、行项成本（缺成本/缺基准 → None）。
- DB 集成 (TEST_PG_DSN)：现金结算流（open/pending → settled、重结 409、
  非现金 422、实收不足 422、非本人 403、结算行落账、行项 settlement_status）、
  交接班流（重复开班 409、差异正负如实记、只算本班本人结算、他人班次 403）、
  日结报表（合计/分列/毛利口径与排除计数/日期格式 400）。
- 不建单：结算不创建 customer_order、不扣库存（建单链路缺口见
  app/counter_closeout.py 模块注释）。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.counter_closeout import (
    compute_change_cents,
    compute_shift_expected_cents,
    compute_shift_variance_cents,
    weigh_line_cost_cents,
)

TEST_DSN = os.environ.get("TEST_PG_DSN")

DB_TESTS = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


# ── 纯函数 ─────────────────────────────────────────────────────────────────


class TestComputeChange:
    def test_exact(self):
        assert compute_change_cents(293, 293) == 0

    def test_overpay(self):
        assert compute_change_cents(293, 500) == 207

    def test_underpay_raises(self):
        with pytest.raises(ValueError, match="less than amount due"):
            compute_change_cents(293, 100)

    def test_negative_inputs_raise(self):
        with pytest.raises(ValueError):
            compute_change_cents(-1, 100)
        with pytest.raises(ValueError):
            compute_change_cents(100, -1)


class TestShiftArithmetic:
    def test_expected_is_float_plus_due(self):
        assert compute_shift_expected_cents(50000, 12345) == 62345

    def test_variance_positive_negative_zero(self):
        assert compute_shift_variance_cents(62345, 62345) == 0
        assert compute_shift_variance_cents(62400, 62345) == 55
        assert compute_shift_variance_cents(62000, 62345) == -345

    def test_negative_inputs_raise(self):
        with pytest.raises(ValueError):
            compute_shift_expected_cents(-1, 0)
        with pytest.raises(ValueError):
            compute_shift_variance_cents(-1, 0)


class TestWeighLineCost:
    def test_cost(self):
        assert weigh_line_cost_cents(350, 500, 500) == 350
        assert weigh_line_cost_cents(250, 500, 501) == 251  # 250.5 → 251

    def test_missing_inputs_are_none(self):
        assert weigh_line_cost_cents(None, 500, 500) is None
        assert weigh_line_cost_cents(350, None, 500) is None
        assert weigh_line_cost_cents(350, 500, None) is None

    def test_invalid_inputs_are_none(self):
        assert weigh_line_cost_cents(0, 500, 500) is None
        assert weigh_line_cost_cents(350, 0, 500) is None
        assert weigh_line_cost_cents(350, 500, -1) is None


# ── DB 集成 ────────────────────────────────────────────────────────────────


def _fake_request(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


@pytest.fixture
async def closeout_pool():
    from db_test_utils import cleanup_db_test_rows
    from obase.persistence.pool import PgPool

    from app.ext.schema import ensure_ext_schema
    from app.p0_schema import ensure_p0_schema

    pool = await PgPool.create(name="hemall_closeout_test", dsn=TEST_DSN, min_size=1, max_size=5)
    await cleanup_db_test_rows(pool)
    await ensure_p0_schema(pool)
    await ensure_ext_schema(pool)
    state: dict = {"tickets": [], "batches": [], "variants": [], "products": [], "customers": []}
    yield pool, state
    async with pool.acquire() as conn:
        await conn.execute(
            'DELETE FROM "counter_settlement" WHERE ticket_id = ANY($1::uuid[])',
            state["tickets"],
        )
        await conn.execute(
            'DELETE FROM "counter_shift" WHERE cashier_id = ANY($1)', state["customers"]
        )
        if state["tickets"]:
            await conn.execute(
                'DELETE FROM "hardware_event" WHERE ticket_id = ANY($1::uuid[])',
                state["tickets"],
            )
        await conn.execute("DELETE FROM \"hardware_event\" WHERE message_id LIKE 'cctest-%'")
        if state["tickets"]:
            await conn.execute(
                'DELETE FROM "counter_ticket" WHERE id = ANY($1::uuid[])', state["tickets"]
            )
        if state["batches"]:
            await conn.execute(
                "DELETE FROM inventory_batch WHERE id = ANY($1::uuid[])", state["batches"]
            )
        if state["variants"]:
            await conn.execute(
                "DELETE FROM product_variant WHERE id = ANY($1::uuid[])", state["variants"]
            )
        if state["products"]:
            await conn.execute("DELETE FROM product WHERE id = ANY($1::uuid[])", state["products"])
        if state["customers"]:
            await conn.execute(
                'DELETE FROM "customer" WHERE id = ANY($1::uuid[])', state["customers"]
            )
    await pool.close()


async def _seed_merchant(pool, state, tag: str) -> str:
    import uuid

    cid = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO "customer" (id, email, status, is_merchant, created_at, updated_at)'
            " VALUES ($1, $2, 'active', true, NOW(), NOW())",
            cid,
            f"cc-{tag}@test.dev",
        )
    state["customers"].append(cid)
    return cid


async def _seed_batch(
    pool, state, tag: str, *, price: int, spec: str, cost: int | None
) -> tuple[str, str]:
    """种一个可售批次，返回 (batch_id, variant_id)；cost 为批次成本价（分）。"""
    import datetime
    import uuid

    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, "
            "lat, lng, status) VALUES ($1, 'cc_loc', 'cn-east', 'host_cc', '柜台', "
            "31.23, 121.47, 'active') RETURNING id",
            uuid7(),
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,$2,$3,'published') "
            "RETURNING id",
            uuid7(),
            f"收尾测试品{tag}",
            f"cc-slug-{tag}-{uuid.uuid4().hex[:8]}",
        )
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, option_values, "
            "reference_price_cents, status) VALUES ($1,$2,$3,$4,$5,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"CC-{tag}",
            f'{{"规格": "{spec}"}}',
            price,
        )
        batch_id = await conn.fetchval(
            "INSERT INTO inventory_batch (id, batch_no, variant_id, location_id, video_url, "
            "cost_price_cents, retail_price_cents, stock_qty, reserved_qty, "
            "expiration_time, status, inspection_status) "
            "VALUES ($1,$2,$3,$4,'https://v',$5,$6,50,0,$7,'active','passed') RETURNING id",
            uuid7(),
            f"CC-{tag}",
            var_id,
            loc_id,
            cost,
            price,
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30),
        )
    state["products"].append(str(prod_id))
    state["variants"].append(str(var_id))
    state["batches"].append(str(batch_id))
    return str(batch_id), str(var_id)


async def _ticket_with_line(pool, state, merchant_id, batch_id, weight_grams=350):
    import app.storefront as sf

    req = _fake_request(pool)
    principal = {"customer_id": merchant_id}
    created = await sf.counter_create_ticket(req, {}, principal)
    ticket_id = created["ticket_id"]
    state["tickets"].append(ticket_id)
    added = await sf.counter_add_line(
        req,
        ticket_id,
        {"batch_id": batch_id, "weight_grams": weight_grams, "source": "manual"},
        principal,
    )
    return ticket_id, added["total_cents"]


@DB_TESTS
class TestCashSettleDb:
    async def test_settle_from_open_records_settlement(self, closeout_pool):
        import app.storefront as sf

        pool, state = closeout_pool
        batch_id, _ = await _seed_batch(pool, state, "s1", price=419, spec="500g/份", cost=500)
        merchant_id = await _seed_merchant(pool, state, "m1")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, total = await _ticket_with_line(pool, state, merchant_id, batch_id)
        assert total == 293

        async with pool.acquire() as conn:
            orders_before = await conn.fetchval("SELECT COUNT(*) FROM customer_order")
        settled = await sf.counter_settle_ticket(
            req, ticket_id, {"method": "cash", "amount_received_cents": 300}, principal
        )
        assert settled["status"] == "settled"
        assert settled["settlement"]["amount_due_cents"] == 293
        assert settled["settlement"]["amount_received_cents"] == 300
        assert settled["settlement"]["change_cents"] == 7
        assert settled["settlement"]["method"] == "cash"

        async with pool.acquire() as conn:
            assert (
                await conn.fetchval("SELECT COUNT(*) FROM customer_order") == orders_before
            )  # 结算不造单
            line_status = await conn.fetchval(
                "SELECT settlement_status FROM hardware_event WHERE ticket_id = $1",
                ticket_id,
            )
            assert line_status == "settled"

        with pytest.raises(Exception, match="not settleable"):
            await sf.counter_settle_ticket(
                req, ticket_id, {"method": "cash", "amount_received_cents": 300}, principal
            )

    async def test_settle_from_pending_settlement(self, closeout_pool):
        import app.storefront as sf

        pool, state = closeout_pool
        batch_id, _ = await _seed_batch(pool, state, "s2", price=419, spec="500g/份", cost=500)
        merchant_id = await _seed_merchant(pool, state, "m2")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, _ = await _ticket_with_line(pool, state, merchant_id, batch_id)
        await sf.counter_mark_pending_settlement(req, ticket_id, principal)
        settled = await sf.counter_settle_ticket(
            req, ticket_id, {"method": "cash", "amount_received_cents": 293}, principal
        )
        assert settled["status"] == "settled"
        assert settled["settlement"]["change_cents"] == 0

    async def test_settle_rejects(self, closeout_pool):
        from fastapi import HTTPException

        import app.storefront as sf

        pool, state = closeout_pool
        batch_id, _ = await _seed_batch(pool, state, "s3", price=419, spec="500g/份", cost=500)
        merchant_id = await _seed_merchant(pool, state, "m3")
        other_id = await _seed_merchant(pool, state, "m3b")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, _ = await _ticket_with_line(pool, state, merchant_id, batch_id)

        with pytest.raises(HTTPException, match="only cash"):
            await sf.counter_settle_ticket(
                req, ticket_id, {"method": "wechat", "amount_received_cents": 293}, principal
            )
        with pytest.raises(HTTPException, match="method is required"):
            await sf.counter_settle_ticket(
                req, ticket_id, {"amount_received_cents": 293}, principal
            )
        with pytest.raises(HTTPException, match="less than amount due"):
            await sf.counter_settle_ticket(
                req, ticket_id, {"method": "cash", "amount_received_cents": 100}, principal
            )
        with pytest.raises(HTTPException, match="another merchant"):
            await sf.counter_settle_ticket(
                req,
                ticket_id,
                {"method": "cash", "amount_received_cents": 293},
                {"customer_id": other_id},
            )

    async def test_settle_void_ticket_conflicts(self, closeout_pool):
        import app.storefront as sf

        pool, state = closeout_pool
        batch_id, _ = await _seed_batch(pool, state, "s4", price=419, spec="500g/份", cost=500)
        merchant_id = await _seed_merchant(pool, state, "m4")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, _ = await _ticket_with_line(pool, state, merchant_id, batch_id)
        await sf.counter_void_ticket(req, ticket_id, principal)
        with pytest.raises(Exception, match="not settleable"):
            await sf.counter_settle_ticket(
                req, ticket_id, {"method": "cash", "amount_received_cents": 293}, principal
            )


@DB_TESTS
class TestShiftDb:
    async def test_shift_open_close_variance(self, closeout_pool):
        import app.storefront as sf

        pool, state = closeout_pool
        batch_id, _ = await _seed_batch(pool, state, "h1", price=419, spec="500g/份", cost=500)
        merchant_id = await _seed_merchant(pool, state, "h1")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}

        opened = await sf.counter_open_shift(
            req, {"opening_float_cents": 50000, "note": "早班"}, principal
        )
        shift_id = opened["shift"]["shift_id"]
        assert opened["shift"]["status"] == "open"
        with pytest.raises(Exception, match="already has an open shift"):
            await sf.counter_open_shift(req, {"opening_float_cents": 1}, principal)

        ticket_id, _ = await _ticket_with_line(pool, state, merchant_id, batch_id)
        await sf.counter_settle_ticket(
            req, ticket_id, {"method": "cash", "amount_received_cents": 300}, principal
        )

        closed = await sf.counter_close_shift(
            req, shift_id, {"counted_cash_cents": 50293, "handover_to": "next"}, principal
        )
        assert closed["shift"]["status"] == "closed"
        assert closed["shift"]["expected_cash_cents"] == 50293
        assert closed["shift"]["variance_cents"] == 0
        with pytest.raises(Exception, match="not open"):
            await sf.counter_close_shift(req, shift_id, {"counted_cash_cents": 1}, principal)

    async def test_shift_variance_negative_recorded(self, closeout_pool):
        import app.storefront as sf

        pool, state = closeout_pool
        merchant_id = await _seed_merchant(pool, state, "h2")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        opened = await sf.counter_open_shift(req, {"opening_float_cents": 10000}, principal)
        closed = await sf.counter_close_shift(
            req, opened["shift"]["shift_id"], {"counted_cash_cents": 9900}, principal
        )
        assert closed["shift"]["variance_cents"] == -100
        assert closed["shift"]["expected_cash_cents"] == 10000

    async def test_shift_counts_only_own_in_window_settlements(self, closeout_pool):
        import app.storefront as sf

        pool, state = closeout_pool
        batch_id, _ = await _seed_batch(pool, state, "h3", price=419, spec="500g/份", cost=500)
        merchant_id = await _seed_merchant(pool, state, "h3")
        other_id = await _seed_merchant(pool, state, "h3b")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}

        # 班前已结算一单（直接插行，settled_at 在开班前）：不计入本班。
        pre_id, _ = await _ticket_with_line(pool, state, merchant_id, batch_id)
        await sf.counter_settle_ticket(
            req, pre_id, {"method": "cash", "amount_received_cents": 293}, principal
        )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE counter_settlement SET settled_at = NOW() - INTERVAL '2 hours' "
                "WHERE ticket_id = $1",
                pre_id,
            )

        opened = await sf.counter_open_shift(req, {"opening_float_cents": 1000}, principal)
        shift_id = opened["shift"]["shift_id"]

        # 另一收银员班内结算：不计入本班。
        other_ticket, _ = await _ticket_with_line(pool, state, other_id, batch_id)
        await sf.counter_settle_ticket(
            req,
            other_ticket,
            {"method": "cash", "amount_received_cents": 293},
            {"customer_id": other_id},
        )

        closed = await sf.counter_close_shift(
            req, shift_id, {"counted_cash_cents": 1000}, principal
        )
        assert closed["shift"]["expected_cash_cents"] == 1000
        assert closed["shift"]["variance_cents"] == 0

    async def test_shift_close_other_cashier_forbidden(self, closeout_pool):
        import app.storefront as sf

        pool, state = closeout_pool
        merchant_id = await _seed_merchant(pool, state, "h4")
        other_id = await _seed_merchant(pool, state, "h4b")
        req = _fake_request(pool)
        opened = await sf.counter_open_shift(
            req, {"opening_float_cents": 0}, {"customer_id": merchant_id}
        )
        with pytest.raises(Exception, match="another cashier"):
            await sf.counter_close_shift(
                req,
                opened["shift"]["shift_id"],
                {"counted_cash_cents": 0},
                {"customer_id": other_id},
            )


@DB_TESTS
class TestDailyReportDb:
    async def test_report_totals_and_margin_caliber(self, closeout_pool):
        import datetime

        import app.storefront as sf

        pool, state = closeout_pool
        costed_batch, _ = await _seed_batch(pool, state, "r1", price=419, spec="500g/份", cost=500)
        merchant_id = await _seed_merchant(pool, state, "r1")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}

        t1, _ = await _ticket_with_line(pool, state, merchant_id, costed_batch)
        t2, _ = await _ticket_with_line(pool, state, merchant_id, costed_batch)
        opened = await sf.counter_open_shift(req, {"opening_float_cents": 1000}, principal)
        await sf.counter_settle_ticket(
            req, t1, {"method": "cash", "amount_received_cents": 293}, principal
        )
        await sf.counter_settle_ticket(
            req, t2, {"method": "cash", "amount_received_cents": 293}, principal
        )
        # 无批次行（历史/手工遗留行）：计入单据行数但不得污染毛利。
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO "hardware_event" (message_id, event_type, status, '
                "weight_grams, unit_price_cents, price_basis_grams, amount_cents, "
                "source, ticket_id, batch_id) VALUES ('cctest-nobatch', 'weigh', "
                "'processed', 100, 419, 500, 100, 'manual', $1, NULL)",
                t2,
            )

        closed = await sf.counter_close_shift(
            req, opened["shift"]["shift_id"], {"counted_cash_cents": 1500}, principal
        )
        assert closed["shift"]["variance_cents"] == -86  # 应有 1000+586，实盘 1500

        today = datetime.date.today().isoformat()
        report = await sf.counter_daily_report(req, date=today, principal=principal)
        assert report["date"] == today
        assert report["settled_tickets"] == 2
        assert report["total_sales_cents"] == 586
        assert report["by_method"] == [{"method": "cash", "tickets": 2, "total_cents": 586}]
        # 毛利只计有批次行：收入 586，成本 2×350=700 → -114；无批次行排除 1 行。
        assert report["margin_revenue_cents"] == 586
        assert report["margin_cost_cents"] == 700
        assert report["margin_cents"] == -114
        assert report["margin_costed_lines"] == 2
        assert report["margin_excluded_lines"] == 1
        assert report["shifts_closed"] == 1
        assert report["cash_variance_total_cents"] == -86

    async def test_report_bad_date_and_non_merchant(self, closeout_pool):
        from fastapi import HTTPException

        import app.storefront as sf

        pool, state = closeout_pool
        merchant_id = await _seed_merchant(pool, state, "r2")
        plain_id = await _seed_merchant(pool, state, "r2b")
        async with pool.acquire() as conn:
            await conn.execute('UPDATE "customer" SET is_merchant = false WHERE id = $1', plain_id)
        req = _fake_request(pool)
        with pytest.raises(HTTPException, match="YYYY-MM-DD"):
            await sf.counter_daily_report(
                req, date="not-a-date", principal={"customer_id": merchant_id}
            )
        with pytest.raises(HTTPException, match="merchant account required"):
            await sf.counter_daily_report(req, date=None, principal={"customer_id": plain_id})
