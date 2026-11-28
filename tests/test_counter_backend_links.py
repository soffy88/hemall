"""tests/test_counter_backend_links.py — 深化第一批（后端补链）测试。

覆盖：
- 秤表：ensure_p0_schema 建出 scale_device/scale_reading/scale_stable_event，
  重复执行幂等；/admin/scale/devices 建设备能落库。
- 积分钩子：柜台结算成功后 points_ledger 有一行且余额对得上；散客不计分；
  重结 409 不重复计分；库存不足整笔回滚时不计分。
- 储值收银：足额结算成功且余额扣减正确；余额不足 422 且不建单不扣库存；
  重结不重复扣；散客单据走储值 422。
- 夜市价接口：柜台商品列表与票据行在夜市时段内/外返回夜市价字段
  （只返回字段，不改实收金额）。
- MP2 settle：改调柜台结算后建单扣库存与柜台一致；qr 请求被拒。
"""

from __future__ import annotations

import datetime
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

TEST_DSN = os.environ.get("TEST_PG_DSN")

DB_TESTS = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


def _fake_request(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


@pytest.fixture
async def links_pool():
    from db_test_utils import cleanup_db_test_rows
    from obase.persistence.pool import PgPool

    from app.ext.schema import ensure_ext_schema
    from app.p0_schema import ensure_p0_schema

    pool = await PgPool.create(
        name="hemall_counter_links_test", dsn=TEST_DSN, min_size=1, max_size=5
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
        if state["tickets"]:
            await conn.execute(
                'DELETE FROM "points_ledger" WHERE order_id IN '
                '(SELECT id FROM "customer_order" WHERE counter_ticket_id = ANY($1::uuid[]))',
                state["tickets"],
            )
        if state["customers"]:
            await conn.execute(
                "DELETE FROM stored_value_ledger WHERE customer_id = ANY($1::uuid[])",
                state["customers"],
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
    cid = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO "customer" (id, email, status, is_merchant, created_at, updated_at)'
            " VALUES ($1, $2, 'active', true, NOW(), NOW())",
            cid,
            f"link-{tag}@test.dev",
        )
    state["customers"].append(cid)
    return cid


async def _seed_customer(pool, state, tag: str) -> str:
    cid = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO "customer" (id, email, status, is_merchant, created_at, updated_at)'
            " VALUES ($1, $2, 'active', false, NOW(), NOW())",
            cid,
            f"link-buyer-{tag}@test.dev",
        )
    state["customers"].append(cid)
    return cid


async def _seed_batch(
    pool,
    state,
    tag: str,
    *,
    price: int,
    spec: str,
    stock: int = 50,
    night_clearance: bool = False,
) -> tuple[str, str]:
    """种一个可售批次，返回 (batch_id, variant_id)。"""
    import datetime as _dt

    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, "
            "lat, lng, status) VALUES ($1, 'link_loc', 'cn-east', 'host_link', '柜台', "
            "31.23, 121.47, 'active') RETURNING id",
            uuid7(),
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,$2,$3,'published') "
            "RETURNING id",
            uuid7(),
            f"补链测试品{tag}",
            f"link-slug-{tag}-{uuid.uuid4().hex[:8]}",
        )
        if night_clearance:
            await conn.execute("UPDATE product SET night_clearance = true WHERE id = $1", prod_id)
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, option_values, "
            "reference_price_cents, status) VALUES ($1,$2,$3,$4,$5,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"LINK-{tag}",
            f'{{"规格": "{spec}"}}',
            price,
        )
        batch_id = await conn.fetchval(
            "INSERT INTO inventory_batch (id, batch_no, variant_id, location_id, video_url, "
            "cost_price_cents, retail_price_cents, stock_qty, reserved_qty, "
            "expiration_time, status, inspection_status) "
            "VALUES ($1,$2,$3,$4,'https://v',$5,$6,$7,0,$8,'active','passed') RETURNING id",
            uuid7(),
            f"LINK-{tag}",
            var_id,
            loc_id,
            500,
            price,
            stock,
            _dt.datetime.now(_dt.UTC) + _dt.timedelta(days=30),
        )
    state["locations"].append(str(loc_id))
    state["products"].append(str(prod_id))
    state["variants"].append(str(var_id))
    state["batches"].append(str(batch_id))
    return str(batch_id), str(var_id)


async def _ticket_with_line(
    pool, state, merchant_id, batch_id, *, weight_grams=350, customer_id=None
):
    import app.storefront as sf

    req = _fake_request(pool)
    principal = {"customer_id": merchant_id}
    body: dict = {}
    if customer_id is not None:
        body["customer_id"] = customer_id
    created = await sf.counter_create_ticket(req, body, principal)
    ticket_id = created["ticket_id"]
    state["tickets"].append(ticket_id)
    added = await sf.counter_add_line(
        req,
        ticket_id,
        {"batch_id": batch_id, "weight_grams": weight_grams, "source": "manual"},
        principal,
    )
    return ticket_id, added["total_cents"]


def _night(dt_hour: int) -> datetime.datetime:
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime(2026, 11, 28, dt_hour, 30, tzinfo=tz)


# ── 1. 秤表 ────────────────────────────────────────────────────────────────


@DB_TESTS
class TestScaleTables:
    async def test_scale_tables_created_and_idempotent(self, links_pool):
        from app.p0_schema import ensure_p0_schema

        pool, _state = links_pool
        # 启动 DDL 重复执行幂等（夹具已执行过一次，这里再跑两次）。
        await ensure_p0_schema(pool)
        await ensure_p0_schema(pool)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name IN ('scale_device', 'scale_reading', 'scale_stable_event')"
            )
        assert {r["table_name"] for r in rows} == {
            "scale_device",
            "scale_reading",
            "scale_stable_event",
        }

    async def test_create_device_persists(self, links_pool):
        from app.scale import ScaleService
        from app.scale_router import DeviceCreate, create_device, list_devices

        pool, _state = links_pool
        created = await create_device(
            DeviceCreate(name="link-scale", port_path="/dev/ttyUSB9"),
            service=ScaleService(pool),
            principal={"email": "link-staff@test.dev"},
        )
        assert created["port_path"] == "/dev/ttyUSB9"
        devices = await list_devices(service=ScaleService(pool), _=None)
        assert any(d["id"] == created["id"] for d in devices)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM scale_device WHERE id = $1", created["id"])


# ── 2. 积分钩子 ────────────────────────────────────────────────────────────


@DB_TESTS
class TestPointsHook:
    async def test_settle_accrues_points(self, links_pool):
        import app.storefront as sf

        pool, state = links_pool
        batch_id, _ = await _seed_batch(pool, state, "p1", price=419, spec="500g/份")
        merchant_id = await _seed_merchant(pool, state, "pm1")
        buyer_id = await _seed_customer(pool, state, "pb1")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, total = await _ticket_with_line(
            pool, state, merchant_id, batch_id, customer_id=buyer_id
        )
        assert total == 293

        settled = await sf.counter_settle_ticket(
            req, ticket_id, {"method": "cash", "amount_received_cents": 300}, principal
        )
        assert settled["status"] == "settled"
        assert settled["order"] is not None
        assert settled["order"]["lines"][0]["product_title"] == "补链测试品p1"
        assert settled["order"]["lines"][0]["weight_grams"] == 350
        assert settled["points_accrual"]["accrued"] is True
        assert settled["points_accrual"]["points"] == 2
        assert settled["points_accrual"]["base_cents"] == 293

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT kind, points, base_cents, balance_after FROM points_ledger "
                "WHERE order_id = $1",
                settled["order"]["order_id"],
            )
            assert len(rows) == 1
            assert rows[0]["kind"] == "accrual"
            assert int(rows[0]["points"]) == 2
            assert int(rows[0]["base_cents"]) == 293
            assert int(rows[0]["balance_after"]) == 2
            balance = await conn.fetchval(
                'SELECT points_balance FROM "customer" WHERE id = $1', buyer_id
            )
            assert int(balance) == 2
            lifetime = await conn.fetchval(
                'SELECT lifetime_eligible_cents FROM "customer" WHERE id = $1', buyer_id
            )
            assert int(lifetime) == 293

    async def test_settle_walkin_accrues_nothing(self, links_pool):
        import app.storefront as sf

        pool, state = links_pool
        batch_id, _ = await _seed_batch(pool, state, "p2", price=419, spec="500g/份")
        merchant_id = await _seed_merchant(pool, state, "pm2")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, _ = await _ticket_with_line(pool, state, merchant_id, batch_id)
        settled = await sf.counter_settle_ticket(
            req, ticket_id, {"method": "cash", "amount_received_cents": 300}, principal
        )
        assert settled["order"] is not None
        assert settled.get("points_accrual") is None
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM points_ledger WHERE order_id = $1",
                settled["order"]["order_id"],
            )
        assert int(count) == 0

    async def test_resettle_does_not_double_accrue(self, links_pool):
        import app.storefront as sf

        pool, state = links_pool
        batch_id, _ = await _seed_batch(pool, state, "p3", price=419, spec="500g/份")
        merchant_id = await _seed_merchant(pool, state, "pm3")
        buyer_id = await _seed_customer(pool, state, "pb3")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, _ = await _ticket_with_line(
            pool, state, merchant_id, batch_id, customer_id=buyer_id
        )
        await sf.counter_settle_ticket(
            req, ticket_id, {"method": "cash", "amount_received_cents": 300}, principal
        )
        with pytest.raises(HTTPException, match="409"):
            await sf.counter_settle_ticket(
                req, ticket_id, {"method": "cash", "amount_received_cents": 300}, principal
            )
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM points_ledger WHERE customer_id = $1", buyer_id
            )
            balance = await conn.fetchval(
                'SELECT points_balance FROM "customer" WHERE id = $1', buyer_id
            )
        assert int(count) == 1
        assert int(balance) == 2

    async def test_settle_survives_points_failure(self, links_pool, monkeypatch):
        """积分累积报错只记日志，不拖垮整单（Codex 分流 #2，已修，锁死）。"""
        import app.membership as mb
        import app.storefront as sf

        async def _boom(conn, order_id):
            raise RuntimeError("points boom")

        monkeypatch.setattr(mb, "accrue_points_conn", _boom)
        pool, state = links_pool
        batch_id, _ = await _seed_batch(pool, state, "p9", price=419, spec="500g/份")
        merchant_id = await _seed_merchant(pool, state, "pm9")
        buyer_id = await _seed_customer(pool, state, "pb9")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, total = await _ticket_with_line(
            pool, state, merchant_id, batch_id, customer_id=buyer_id
        )
        assert total == 293
        settled = await sf.counter_settle_ticket(
            req, ticket_id, {"method": "cash", "amount_received_cents": 300}, principal
        )
        assert settled["status"] == "settled"
        assert settled["order"] is not None
        assert settled["points_accrual"] is None

    async def test_failed_settle_accrues_nothing(self, links_pool):
        import app.storefront as sf

        pool, state = links_pool
        batch_id, _ = await _seed_batch(pool, state, "p4", price=419, spec="500g/份", stock=0)
        merchant_id = await _seed_merchant(pool, state, "pm4")
        buyer_id = await _seed_customer(pool, state, "pb4")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, _ = await _ticket_with_line(
            pool, state, merchant_id, batch_id, customer_id=buyer_id
        )
        with pytest.raises(HTTPException, match="409"):
            await sf.counter_settle_ticket(
                req, ticket_id, {"method": "cash", "amount_received_cents": 300}, principal
            )
        async with pool.acquire() as conn:
            order_id = await conn.fetchval(
                'SELECT id FROM "customer_order" WHERE counter_ticket_id = $1', ticket_id
            )
            assert order_id is None
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM points_ledger WHERE customer_id = $1", buyer_id
            )
            assert int(count) == 0
            ticket = await conn.fetchrow(
                'SELECT status FROM "counter_ticket" WHERE id = $1', ticket_id
            )
            assert ticket["status"] == "open"


# ── 4. 储值收银 ────────────────────────────────────────────────────────────


@DB_TESTS
class TestStoredValueSettle:
    async def _topup(self, pool, customer_id, amount, ref):
        from app.membership import topup_stored_value

        return await topup_stored_value(
            pool,
            customer_id=customer_id,
            amount_cents=amount,
            external_payment_ref=ref,
        )

    async def test_stored_value_settle(self, links_pool):
        import app.storefront as sf

        pool, state = links_pool
        batch_id, _ = await _seed_batch(pool, state, "s1", price=419, spec="500g/份")
        merchant_id = await _seed_merchant(pool, state, "sm1")
        buyer_id = await _seed_customer(pool, state, "sb1")
        await self._topup(pool, buyer_id, 10000, "link-topup-s1")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, total = await _ticket_with_line(
            pool, state, merchant_id, batch_id, customer_id=buyer_id
        )
        assert total == 293

        settled = await sf.counter_settle_ticket(
            req, ticket_id, {"payment_method": "stored_value"}, principal
        )
        assert settled["status"] == "settled"
        assert settled["settlement"]["method"] == "stored_value"
        assert settled["settlement"]["change_cents"] == 0
        assert settled["order"] is not None
        assert settled["stored_value"]["spent"] is True
        assert settled["stored_value"]["stored_value_balance_cents"] == 9707
        # 储值单同样累积分。
        assert settled["points_accrual"]["accrued"] is True

        async with pool.acquire() as conn:
            balance = await conn.fetchval(
                'SELECT stored_value_balance_cents FROM "customer" WHERE id = $1', buyer_id
            )
            assert int(balance) == 9707
            rows = await conn.fetch(
                "SELECT kind, amount_cents, balance_after_cents, idempotency_key "
                "FROM stored_value_ledger WHERE customer_id = $1 AND kind = 'spend'",
                buyer_id,
            )
            assert len(rows) == 1  # 只一笔扣款（另有一笔 topup，不在此查）
            spend = rows[0]
            assert spend["kind"] == "spend"
            assert int(spend["amount_cents"]) == 293
            assert int(spend["balance_after_cents"]) == 9707
            assert spend["idempotency_key"] == f"stored-spend:{ticket_id}"
            stock = await conn.fetchval(
                "SELECT stock_qty FROM inventory_batch WHERE id = $1", batch_id
            )
            assert int(stock) == 49

    async def test_stored_value_insufficient(self, links_pool):
        import app.storefront as sf

        pool, state = links_pool
        batch_id, _ = await _seed_batch(pool, state, "s2", price=419, spec="500g/份")
        merchant_id = await _seed_merchant(pool, state, "sm2")
        buyer_id = await _seed_customer(pool, state, "sb2")
        await self._topup(pool, buyer_id, 100, "link-topup-s2")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, _ = await _ticket_with_line(
            pool, state, merchant_id, batch_id, customer_id=buyer_id
        )
        with pytest.raises(HTTPException, match="insufficient stored value"):
            await sf.counter_settle_ticket(
                req, ticket_id, {"payment_method": "stored_value"}, principal
            )
        async with pool.acquire() as conn:
            order_id = await conn.fetchval(
                'SELECT id FROM "customer_order" WHERE counter_ticket_id = $1', ticket_id
            )
            assert order_id is None
            stock = await conn.fetchval(
                "SELECT stock_qty FROM inventory_batch WHERE id = $1", batch_id
            )
            assert int(stock) == 50
            balance = await conn.fetchval(
                'SELECT stored_value_balance_cents FROM "customer" WHERE id = $1', buyer_id
            )
            assert int(balance) == 100
            spends = await conn.fetchval(
                "SELECT COUNT(*) FROM stored_value_ledger "
                "WHERE customer_id = $1 AND kind = 'spend'",
                buyer_id,
            )
            assert int(spends) == 0
            ticket = await conn.fetchrow(
                'SELECT status FROM "counter_ticket" WHERE id = $1', ticket_id
            )
            assert ticket["status"] == "open"

    async def test_stored_value_resettle_no_double_spend(self, links_pool):
        import app.storefront as sf

        pool, state = links_pool
        batch_id, _ = await _seed_batch(pool, state, "s3", price=419, spec="500g/份")
        merchant_id = await _seed_merchant(pool, state, "sm3")
        buyer_id = await _seed_customer(pool, state, "sb3")
        await self._topup(pool, buyer_id, 10000, "link-topup-s3")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, _ = await _ticket_with_line(
            pool, state, merchant_id, batch_id, customer_id=buyer_id
        )
        await sf.counter_settle_ticket(
            req, ticket_id, {"payment_method": "stored_value"}, principal
        )
        with pytest.raises(HTTPException, match="409"):
            await sf.counter_settle_ticket(
                req, ticket_id, {"payment_method": "stored_value"}, principal
            )
        async with pool.acquire() as conn:
            balance = await conn.fetchval(
                'SELECT stored_value_balance_cents FROM "customer" WHERE id = $1', buyer_id
            )
            assert int(balance) == 9707
            spends = await conn.fetchval(
                "SELECT COUNT(*) FROM stored_value_ledger "
                "WHERE customer_id = $1 AND kind = 'spend'",
                buyer_id,
            )
            assert int(spends) == 1

    async def test_stored_value_requires_customer(self, links_pool):
        import app.storefront as sf

        pool, state = links_pool
        batch_id, _ = await _seed_batch(pool, state, "s4", price=419, spec="500g/份")
        merchant_id = await _seed_merchant(pool, state, "sm4")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, _ = await _ticket_with_line(pool, state, merchant_id, batch_id)
        with pytest.raises(HTTPException, match="requires a customer"):
            await sf.counter_settle_ticket(
                req, ticket_id, {"payment_method": "stored_value"}, principal
            )
        async with pool.acquire() as conn:
            order_id = await conn.fetchval(
                'SELECT id FROM "customer_order" WHERE counter_ticket_id = $1', ticket_id
            )
            assert order_id is None


# ── 5. 夜市价接口 ──────────────────────────────────────────────────────────


@DB_TESTS
class TestNightPrice:
    async def test_counter_products_night_price(self, links_pool, monkeypatch):
        import app.storefront as sf

        pool, state = links_pool
        await _seed_batch(pool, state, "n1", price=419, spec="500g/份", night_clearance=True)
        await _seed_batch(pool, state, "n2", price=500, spec="500g/份")
        merchant_id = await _seed_merchant(pool, state, "nm1")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}

        # 夜市时段内（20:30，8 折档）：清货品夜市价 335，非清货品无夜市价。
        monkeypatch.setattr("app.storefront.night_now", lambda: _night(20))
        result = await sf.counter_products(req, principal)
        by_title = {p["title"]: p for p in result["products"]}
        night_item = by_title["补链测试品n1"]
        assert night_item["night_clearance"] is True
        assert night_item["night_price_cents"] == 335  # 419 - round(419*0.20)
        plain_item = by_title["补链测试品n2"]
        assert plain_item["night_clearance"] is False
        assert plain_item["night_price_cents"] is None

        # 夜市时段外（19:30）：清货品夜市价等于原价。
        monkeypatch.setattr("app.storefront.night_now", lambda: _night(19))
        result = await sf.counter_products(req, principal)
        by_title = {p["title"]: p for p in result["products"]}
        assert by_title["补链测试品n1"]["night_price_cents"] == 419

    async def test_counter_ticket_lines_night_price(self, links_pool, monkeypatch):
        import app.storefront as sf

        pool, state = links_pool
        batch_id, _ = await _seed_batch(
            pool, state, "n3", price=419, spec="500g/份", night_clearance=True
        )
        merchant_id = await _seed_merchant(pool, state, "nm3")
        req = _fake_request(pool)
        principal = {"customer_id": merchant_id}
        ticket_id, total = await _ticket_with_line(
            pool, state, merchant_id, batch_id, weight_grams=500
        )
        assert total == 419  # 实收金额仍按原价，不因夜市价字段改变
        monkeypatch.setattr("app.storefront.night_now", lambda: _night(21))
        detail = await sf.counter_get_ticket(req, ticket_id, principal)
        assert len(detail["lines"]) == 1
        line = detail["lines"][0]
        assert line["night_clearance"] is True
        assert line["night_price_cents"] == 293  # 419 - round(419*0.30)，7 折档
        assert line["amount_cents"] == 419


# ── 6. MP2 settle ───────────────────────────────────────────────────────────


@DB_TESTS
class TestMp2Settle:
    async def test_mp2_settle_uses_counter_order(self, links_pool):
        from app.scale_router import SettleRequest, settle_pos

        pool, state = links_pool
        batch_id, variant_id = await _seed_batch(pool, state, "m1", price=419, spec="500g/份")
        buyer_id = await _seed_customer(pool, state, "mb1")
        req = _fake_request(pool)
        result = await settle_pos(
            req,
            SettleRequest(
                lines=[
                    {
                        "batch_id": batch_id,
                        "variant_id": variant_id,
                        "weight_grams": 350,
                        "unit_price_cents": 419,
                        "price_basis_grams": 500,
                        "amount_cents": 293,
                    }
                ],
                customer_id=buyer_id,
                payment_method="cash",
                cash_received=300,
            ),
            {"email": "link-pos@test.dev"},
        )
        assert result["total_cents"] == 293
        # customer_order 在仓内 schema 里没有 order_no 列，返回 None（键保留）。
        assert result["order_no"] is None
        assert len(result["lines"]) == 1
        state["tickets"].append(result["ticket_id"])
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT channel, status, subtotal_cents, grand_total_cents, customer_id "
                'FROM "customer_order" WHERE id = $1',
                result["order_id"],
            )
            assert row["channel"] == "counter"
            assert row["status"] == "completed"
            assert int(row["subtotal_cents"]) == 293
            assert int(row["grand_total_cents"]) == 293
            assert str(row["customer_id"]) == buyer_id
            stock = await conn.fetchval(
                "SELECT stock_qty FROM inventory_batch WHERE id = $1", batch_id
            )
            assert int(stock) == 49
            res = await conn.fetchrow(
                "SELECT status, consumed_qty FROM inventory_reservation WHERE order_id = $1",
                result["order_id"],
            )
            assert res["status"] == "consumed"
            assert int(res["consumed_qty"]) == 1
            settlement = await conn.fetchrow(
                'SELECT method, amount_due_cents FROM "counter_settlement" WHERE ticket_id = $1',
                result["ticket_id"],
            )
            assert settlement["method"] == "cash"
            assert int(settlement["amount_due_cents"]) == 293
            points = await conn.fetchval(
                "SELECT COUNT(*) FROM points_ledger WHERE order_id = $1",
                result["order_id"],
            )
            assert int(points) == 1

    async def test_mp2_settle_qr_rejected(self, links_pool):
        from app.scale_router import SettleRequest, settle_pos

        pool, state = links_pool
        batch_id, variant_id = await _seed_batch(pool, state, "m2", price=419, spec="500g/份")
        req = _fake_request(pool)
        with pytest.raises(HTTPException, match="only cash"):
            await settle_pos(
                req,
                SettleRequest(
                    lines=[
                        {
                            "batch_id": batch_id,
                            "variant_id": variant_id,
                            "weight_grams": 350,
                            "unit_price_cents": 419,
                            "price_basis_grams": 500,
                            "amount_cents": 293,
                        }
                    ],
                    payment_method="qr",
                    cash_received=0,
                ),
                {"email": "link-pos-qr@test.dev"},
            )
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM \"counter_ticket\" WHERE created_by = 'link-pos-qr@test.dev'"
            )
            assert int(count) == 0
            stock = await conn.fetchval(
                "SELECT stock_qty FROM inventory_batch WHERE id = $1", batch_id
            )
            assert int(stock) == 50

    async def test_mp2_settle_cash_short(self, links_pool):
        from app.scale_router import SettleRequest, settle_pos

        pool, state = links_pool
        batch_id, variant_id = await _seed_batch(pool, state, "m3", price=419, spec="500g/份")
        req = _fake_request(pool)
        with pytest.raises(HTTPException, match="cash received less than total"):
            await settle_pos(
                req,
                SettleRequest(
                    lines=[
                        {
                            "batch_id": batch_id,
                            "variant_id": variant_id,
                            "weight_grams": 350,
                            "unit_price_cents": 419,
                            "price_basis_grams": 500,
                            "amount_cents": 293,
                        }
                    ],
                    payment_method="cash",
                    cash_received=100,
                ),
                {"email": "link-pos@test.dev"},
            )
