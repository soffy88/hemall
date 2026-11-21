"""tests/test_counter_settlement_order.py — 柜台现金结算建单：建单 + 直接扣库存。

覆盖：
- 纯函数：重量/基准向上取整折算库存份数、计价行校验（缺项拒绝）。
- DB 集成 (TEST_PG_DSN)：结算建单（渠道/状态/金额/行项与单据一致、
  散客 customer_id 为空、discount 0/kind none/券 0、不写 voucher_ledger）、
  库存直接扣减（批次 stock-1、预留 consumed、shipment movement）、
  结算幂等（重结 409、一单一单、库存不再动）、多行同批次聚合、
  小重量按一份扣、库存不足整笔回滚（409、无结算无订单）、空单据不造单、
  unpriced 行不参与建单、缺批次已计价行拒单回滚。
- 清理按 FK 安全顺序：order_line_item → payment_outbox → payment_intent →
  customer_order → counter_settlement → hardware_event → counter_ticket →
  inventory_batch → stock_location → variant → product → customer。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.counter_settlement_order import require_weigh_line_pricing, weigh_stock_units

TEST_DSN = os.environ.get("TEST_PG_DSN")

DB_TESTS = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


# ── 纯函数 ─────────────────────────────────────────────────────────────────


class TestWeighStockUnits:
    def test_exact_multiple(self):
        assert weigh_stock_units(500, 500) == 1
        assert weigh_stock_units(1000, 500) == 2

    def test_partial_rounds_up(self):
        assert weigh_stock_units(501, 500) == 2
        assert weigh_stock_units(350, 500) == 1
        assert weigh_stock_units(100, 500) == 1
        assert weigh_stock_units(1500, 1000) == 2

    def test_invalid_inputs_raise(self):
        with pytest.raises(ValueError):
            weigh_stock_units(0, 500)
        with pytest.raises(ValueError):
            weigh_stock_units(-1, 500)
        with pytest.raises(ValueError):
            weigh_stock_units(100, 0)
        with pytest.raises(ValueError):
            weigh_stock_units(100, -500)
        with pytest.raises(ValueError):
            weigh_stock_units(True, 500)


class TestRequireWeighLinePricing:
    def test_valid_passes(self):
        assert (
            require_weigh_line_pricing(
                batch_id="b",
                weight_grams=350,
                unit_price_cents=419,
                price_basis_grams=500,
                amount_cents=293,
            )
            is None
        )

    def test_missing_fields_raise(self):
        base = {
            "batch_id": "b",
            "weight_grams": 350,
            "unit_price_cents": 419,
            "price_basis_grams": 500,
            "amount_cents": 293,
        }
        for key, bad in (
            ("batch_id", None),
            ("weight_grams", 0),
            ("weight_grams", None),
            ("unit_price_cents", None),
            ("price_basis_grams", None),
            ("price_basis_grams", 0),
            ("amount_cents", None),
        ):
            kwargs = {**base, key: bad}
            with pytest.raises(ValueError):
                require_weigh_line_pricing(**kwargs)


# ── DB 集成 ────────────────────────────────────────────────────────────────


def _fake_request(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


@pytest.fixture
async def settlement_pool():
    from db_test_utils import cleanup_db_test_rows
    from obase.persistence.pool import PgPool

    from app.ext.schema import ensure_ext_schema
    from app.p0_schema import ensure_p0_schema

    pool = await PgPool.create(
        name="hemall_settlement_order_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await cleanup_db_test_rows(pool)
    await ensure_p0_schema(pool)
    await ensure_ext_schema(pool)
    state: dict = {
        "tickets": [],
        "batches": [],
        "variants": [],
        "products": [],
        "customers": [],
        "locations": [],
    }
    yield pool, state
    async with pool.acquire() as conn:
        # FK 安全顺序：行项 → 支付 outbox/intent → 订单 → 结算 → 事件 → 单据 → 批次……
        await conn.execute(
            'DELETE FROM "order_line_item" WHERE order_id IN '
            '(SELECT id FROM "customer_order" WHERE counter_ticket_id = ANY($1::uuid[]))',
            state["tickets"],
        )
        await conn.execute(
            'DELETE FROM "payment_outbox" WHERE payment_intent_id IN '
            '(SELECT id FROM "payment_intent" WHERE order_id IN '
            '(SELECT id::text FROM "customer_order" '
            "WHERE counter_ticket_id = ANY($1::uuid[])))",
            state["tickets"],
        )
        await conn.execute(
            'DELETE FROM "payment_intent" WHERE order_id IN '
            '(SELECT id::text FROM "customer_order" '
            "WHERE counter_ticket_id = ANY($1::uuid[]))",
            state["tickets"],
        )
        await conn.execute(
            'DELETE FROM "inventory_reservation" WHERE order_id IN '
            '(SELECT id::text FROM "customer_order" '
            "WHERE counter_ticket_id = ANY($1::uuid[]))",
            state["tickets"],
        )
        await conn.execute(
            "DELETE FROM \"stock_movement\" WHERE reference_type = 'order' "
            'AND reference_id IN (SELECT id::text FROM "customer_order" '
            "WHERE counter_ticket_id = ANY($1::uuid[]))",
            state["tickets"],
        )
        await conn.execute(
            'DELETE FROM "customer_order" WHERE counter_ticket_id = ANY($1::uuid[])',
            state["tickets"],
        )
        await conn.execute(
            'DELETE FROM "counter_settlement" WHERE ticket_id = ANY($1::uuid[])',
            state["tickets"],
        )
        if state["tickets"]:
            await conn.execute(
                'DELETE FROM "hardware_event" WHERE ticket_id = ANY($1::uuid[])',
                state["tickets"],
            )
        await conn.execute("DELETE FROM \"hardware_event\" WHERE message_id LIKE 'csotest-%'")
        if state["tickets"]:
            await conn.execute(
                'DELETE FROM "counter_ticket" WHERE id = ANY($1::uuid[])', state["tickets"]
            )
        if state["batches"]:
            await conn.execute(
                "DELETE FROM inventory_batch WHERE id = ANY($1::uuid[])", state["batches"]
            )
        if state["locations"]:
            await conn.execute(
                "DELETE FROM stock_location WHERE id = ANY($1::uuid[])", state["locations"]
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
            f"cso-{tag}@test.dev",
        )
    state["customers"].append(cid)
    return cid


async def _seed_batch(
    pool, state, tag: str, *, price: int, spec: str, cost: int | None, stock: int = 50
) -> tuple[str, str]:
    """种一个可售批次，返回 (batch_id, variant_id)。"""
    import datetime
    import uuid

    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, "
            "lat, lng, status) VALUES ($1, 'cso_loc', 'cn-east', 'host_cso', '柜台', "
            "31.23, 121.47, 'active') RETURNING id",
            uuid7(),
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,$2,$3,'published') "
            "RETURNING id",
            uuid7(),
            f"结算建单测试品{tag}",
            f"cso-slug-{tag}-{uuid.uuid4().hex[:8]}",
        )
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, option_values, "
            "reference_price_cents, status) VALUES ($1,$2,$3,$4,$5,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"CSO-{tag}",
            f'{{"规格": "{spec}"}}',
            price,
        )
        batch_id = await conn.fetchval(
            "INSERT INTO inventory_batch (id, batch_no, variant_id, location_id, video_url, "
            "cost_price_cents, retail_price_cents, stock_qty, reserved_qty, "
            "expiration_time, status, inspection_status) "
            "VALUES ($1,$2,$3,$4,'https://v',$5,$6,$7,0,$8,'active','passed') RETURNING id",
            uuid7(),
            f"CSO-{tag}",
            var_id,
            loc_id,
            cost,
            price,
            stock,
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30),
        )
    state["locations"].append(str(loc_id))
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
class TestSettleCreatesOrderDb:
    async def test_settle_creates_counter_order_and_deducts_stock(self, settlement_pool):
        import app.storefront as sf

        pool, state = settlement_pool
        batch_id, variant_id = await _seed_batch(
            pool, state, "o1", price=419, spec="500g/份", cost=500
        )
        merchant_id = await _seed_merchant(pool, state, "m1")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, total = await _ticket_with_line(pool, state, merchant_id, batch_id)
        assert total == 293

        settled = await sf.counter_settle_ticket(
            req, ticket_id, {"method": "cash", "amount_received_cents": 300}, principal
        )
        assert settled["status"] == "settled"
        order = settled["order"]
        assert order is not None
        assert order["channel"] == "counter"
        assert order["status"] == "completed"
        assert order["customer_id"] is None
        assert order["counter_ticket_id"] == ticket_id
        assert order["subtotal_cents"] == 293
        assert order["grand_total_cents"] == 293
        assert order["discount_cents"] == 0
        assert len(order["lines"]) == 1
        line = order["lines"][0]
        assert line["batch_id"] == batch_id
        assert line["variant_id"] == variant_id
        assert line["quantity"] == 1
        assert line["weight_grams"] == 350
        assert line["unit_price_cents"] == 419
        assert line["line_total_cents"] == 293

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT channel, status, customer_id, cart_id, subtotal_cents, "
                "discount_cents, grand_total_cents, discount_kind, voucher_redeemed_cents, "
                'counter_ticket_id FROM "customer_order" WHERE id = $1',
                order["order_id"],
            )
            assert row["channel"] == "counter"
            assert row["status"] == "completed"
            assert row["customer_id"] is None
            assert row["cart_id"] is None
            assert int(row["subtotal_cents"]) == 293
            assert int(row["discount_cents"]) == 0
            assert int(row["grand_total_cents"]) == 293
            assert row["discount_kind"] == "none"
            assert int(row["voucher_redeemed_cents"]) == 0
            assert str(row["counter_ticket_id"]) == ticket_id
            db_line = await conn.fetchrow(
                "SELECT batch_id, variant_id, quantity, preorder_qty, unit_price_cents, "
                'line_total_cents, weight_grams FROM "order_line_item" WHERE order_id = $1',
                order["order_id"],
            )
            assert str(db_line["batch_id"]) == batch_id
            assert str(db_line["variant_id"]) == variant_id
            assert int(db_line["quantity"]) == 1
            assert int(db_line["preorder_qty"]) == 0
            assert int(db_line["weight_grams"]) == 350
            batch = await conn.fetchrow(
                "SELECT stock_qty, reserved_qty FROM inventory_batch WHERE id = $1", batch_id
            )
            assert int(batch["stock_qty"]) == 49
            assert int(batch["reserved_qty"]) == 0
            reservation = await conn.fetchrow(
                "SELECT quantity, consumed_qty, released_qty, status "
                "FROM inventory_reservation WHERE order_id = $1",
                order["order_id"],
            )
            assert int(reservation["quantity"]) == 1
            assert int(reservation["consumed_qty"]) == 1
            assert int(reservation["released_qty"]) == 0
            assert reservation["status"] == "consumed"
            movement = await conn.fetchrow(
                "SELECT movement_type, quantity FROM stock_movement "
                "WHERE reference_type = 'order' AND reference_id = $1 "
                "AND movement_type = 'shipment'",
                order["order_id"],
            )
            assert movement is not None
            assert int(movement["quantity"]) == 1
            voucher_rows = await conn.fetchval(
                "SELECT COUNT(*) FROM voucher_ledger WHERE order_id = $1", order["order_id"]
            )
            assert int(voucher_rows) == 0

    async def test_resettle_conflicts_and_keeps_single_order(self, settlement_pool):
        import app.storefront as sf

        pool, state = settlement_pool
        batch_id, _ = await _seed_batch(pool, state, "o2", price=419, spec="500g/份", cost=500)
        merchant_id = await _seed_merchant(pool, state, "m2")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, _ = await _ticket_with_line(pool, state, merchant_id, batch_id)
        settled = await sf.counter_settle_ticket(
            req, ticket_id, {"method": "cash", "amount_received_cents": 293}, principal
        )
        order_id = settled["order"]["order_id"]

        with pytest.raises(Exception, match="not settleable"):
            await sf.counter_settle_ticket(
                req, ticket_id, {"method": "cash", "amount_received_cents": 293}, principal
            )

        async with pool.acquire() as conn:
            count = await conn.fetchval(
                'SELECT COUNT(*) FROM "customer_order" WHERE counter_ticket_id = $1::uuid',
                ticket_id,
            )
            assert int(count) == 1
            assert (
                await conn.fetchval(
                    'SELECT id FROM "customer_order" WHERE counter_ticket_id = $1::uuid',
                    ticket_id,
                )
                is not None
            )
            batch = await conn.fetchrow(
                "SELECT stock_qty FROM inventory_batch WHERE id = $1", batch_id
            )
            assert int(batch["stock_qty"]) == 49
            assert str(
                await conn.fetchval('SELECT id FROM "customer_order" WHERE id = $1', order_id)
            ) == str(order_id)

    async def test_two_lines_same_batch_aggregate_stock(self, settlement_pool):
        import app.storefront as sf

        pool, state = settlement_pool
        batch_id, _ = await _seed_batch(pool, state, "o3", price=419, spec="500g/份", cost=500)
        merchant_id = await _seed_merchant(pool, state, "m3")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, _ = await _ticket_with_line(pool, state, merchant_id, batch_id)
        await sf.counter_add_line(
            req,
            ticket_id,
            {"batch_id": batch_id, "weight_grams": 350, "source": "manual"},
            principal,
        )
        settled = await sf.counter_settle_ticket(
            req, ticket_id, {"method": "cash", "amount_received_cents": 586}, principal
        )
        assert settled["order"]["grand_total_cents"] == 586
        assert len(settled["order"]["lines"]) == 2
        async with pool.acquire() as conn:
            batch = await conn.fetchrow(
                "SELECT stock_qty, reserved_qty FROM inventory_batch WHERE id = $1", batch_id
            )
            assert int(batch["stock_qty"]) == 48
            assert int(batch["reserved_qty"]) == 0

    async def test_small_weight_deducts_one_unit(self, settlement_pool):
        import app.storefront as sf

        pool, state = settlement_pool
        batch_id, _ = await _seed_batch(pool, state, "o4", price=419, spec="500g/份", cost=500)
        merchant_id = await _seed_merchant(pool, state, "m4")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, total = await _ticket_with_line(pool, state, merchant_id, batch_id, 100)
        assert total == 84  # 100*419/500 = 83.8 → 84
        settled = await sf.counter_settle_ticket(
            req, ticket_id, {"method": "cash", "amount_received_cents": 84}, principal
        )
        assert settled["order"]["lines"][0]["quantity"] == 1
        assert settled["order"]["lines"][0]["weight_grams"] == 100
        async with pool.acquire() as conn:
            batch = await conn.fetchrow(
                "SELECT stock_qty FROM inventory_batch WHERE id = $1", batch_id
            )
            assert int(batch["stock_qty"]) == 49

    async def test_insufficient_stock_rolls_back_whole_settlement(self, settlement_pool):
        import app.storefront as sf

        pool, state = settlement_pool
        batch_id, _ = await _seed_batch(
            pool, state, "o5", price=419, spec="500g/份", cost=500, stock=1
        )
        merchant_id = await _seed_merchant(pool, state, "m5")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, _ = await _ticket_with_line(pool, state, merchant_id, batch_id)
        await sf.counter_add_line(
            req,
            ticket_id,
            {"batch_id": batch_id, "weight_grams": 350, "source": "manual"},
            principal,
        )
        with pytest.raises(Exception, match="insufficient stock"):
            await sf.counter_settle_ticket(
                req, ticket_id, {"method": "cash", "amount_received_cents": 586}, principal
            )
        async with pool.acquire() as conn:
            ticket = await conn.fetchrow(
                'SELECT status FROM "counter_ticket" WHERE id = $1::uuid', ticket_id
            )
            assert ticket["status"] == "open"
            assert (
                int(
                    await conn.fetchval(
                        'SELECT COUNT(*) FROM "counter_settlement" WHERE ticket_id = $1::uuid',
                        ticket_id,
                    )
                )
                == 0
            )
            assert (
                int(
                    await conn.fetchval(
                        'SELECT COUNT(*) FROM "customer_order" WHERE counter_ticket_id = $1::uuid',
                        ticket_id,
                    )
                )
                == 0
            )
            batch = await conn.fetchrow(
                "SELECT stock_qty, reserved_qty FROM inventory_batch WHERE id = $1", batch_id
            )
            assert int(batch["stock_qty"]) == 1
            assert int(batch["reserved_qty"]) == 0

    async def test_empty_ticket_settles_without_order(self, settlement_pool):
        import app.storefront as sf

        pool, state = settlement_pool
        merchant_id = await _seed_merchant(pool, state, "m6")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        created = await sf.counter_create_ticket(req, {}, principal)
        ticket_id = created["ticket_id"]
        state["tickets"].append(ticket_id)
        settled = await sf.counter_settle_ticket(
            req, ticket_id, {"method": "cash", "amount_received_cents": 0}, principal
        )
        assert settled["status"] == "settled"
        assert settled["order"] is None
        async with pool.acquire() as conn:
            assert (
                int(
                    await conn.fetchval(
                        'SELECT COUNT(*) FROM "customer_order" WHERE counter_ticket_id = $1::uuid',
                        ticket_id,
                    )
                )
                == 0
            )

    async def test_unpriced_line_does_not_join_order(self, settlement_pool):
        import app.storefront as sf

        pool, state = settlement_pool
        batch_id, _ = await _seed_batch(pool, state, "o7", price=419, spec="500g/份", cost=500)
        merchant_id = await _seed_merchant(pool, state, "m7")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, total = await _ticket_with_line(pool, state, merchant_id, batch_id)
        assert total == 293
        await sf.counter_simulate_weigh(
            req, {"weight_grams": 200, "ticket_id": ticket_id}, principal
        )
        settled = await sf.counter_settle_ticket(
            req, ticket_id, {"method": "cash", "amount_received_cents": 293}, principal
        )
        assert settled["order"]["grand_total_cents"] == 293
        assert len(settled["order"]["lines"]) == 1
        async with pool.acquire() as conn:
            statuses = await conn.fetch(
                "SELECT status, settlement_status FROM hardware_event "
                "WHERE ticket_id = $1::uuid ORDER BY created_at",
                ticket_id,
            )
            assert [r["status"] for r in statuses] == ["processed", "unpriced"]
            assert statuses[1]["settlement_status"] == "unsettled"

    async def test_processed_line_without_batch_rejects_settlement(self, settlement_pool):
        import app.storefront as sf

        pool, state = settlement_pool
        merchant_id = await _seed_merchant(pool, state, "m8")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        created = await sf.counter_create_ticket(req, {}, principal)
        ticket_id = created["ticket_id"]
        state["tickets"].append(ticket_id)
        async with pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO "hardware_event" (message_id, event_type, status, '
                "weight_grams, unit_price_cents, price_basis_grams, amount_cents, "
                "source, ticket_id, batch_id) VALUES ('csotest-nobatch', 'weigh', "
                "'processed', 100, 419, 500, 100, 'manual', $1::uuid, NULL)",
                ticket_id,
            )
        with pytest.raises(Exception, match="has no batch"):
            await sf.counter_settle_ticket(
                req, ticket_id, {"method": "cash", "amount_received_cents": 100}, principal
            )
        async with pool.acquire() as conn:
            assert (
                int(
                    await conn.fetchval(
                        'SELECT COUNT(*) FROM "counter_settlement" WHERE ticket_id = $1::uuid',
                        ticket_id,
                    )
                )
                == 0
            )
            assert (
                int(
                    await conn.fetchval(
                        'SELECT COUNT(*) FROM "customer_order" WHERE counter_ticket_id = $1::uuid',
                        ticket_id,
                    )
                )
                == 0
            )
