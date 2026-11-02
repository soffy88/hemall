"""tests/test_counter_weigh.py — 柜台称重试点：计价纯函数 + 称重端点 + 单据流。

覆盖：
- 纯函数：计价基准解析（500g/份→500、1kg→1000、无单位→None）、金额四舍五入。
- 主干 HTTP 鉴权三态：无 token 401 / 错 token 403 / 正 token 无 DB 503。
- DB 集成 (TEST_PG_DSN)：称重计价落账/幂等防重、无价拒记、原始重量确认挂单、
  柜台单据流（开单→记行→合计→待结算→作废）、照片格只列可计价商品、模拟器标记。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.weigh import extract_unit_texts, parse_basis_grams, weigh_amount_cents

TEST_DSN = os.environ.get("TEST_PG_DSN")

DB_TESTS = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")


# ── 纯函数 ─────────────────────────────────────────────────────────────────


class TestParseBasisGrams:
    def test_grams(self):
        assert parse_basis_grams(["500g/份"]) == 500

    def test_kilograms(self):
        assert parse_basis_grams(["1kg"]) == 1000
        assert parse_basis_grams(["0.5kg"]) == 500

    def test_no_unit_is_none(self):
        assert parse_basis_grams(["散装"]) is None
        assert parse_basis_grams([""]) is None
        assert parse_basis_grams([]) is None

    def test_volume_units_do_not_count_as_weight(self):
        assert parse_basis_grams(["2L"]) is None


class TestExtractUnitTexts:
    def test_dict_values(self):
        assert extract_unit_texts({"规格": "500g/份"}) == ["500g/份"]

    def test_json_string(self):
        assert extract_unit_texts('{"规格": "1kg"}') == ["1kg"]

    def test_plain_string(self):
        assert extract_unit_texts("500g") == ["500g"]

    def test_none(self):
        assert extract_unit_texts(None) == []


class TestWeighAmount:
    def test_half_up_rounding(self):
        assert weigh_amount_cents(350, 419, 500) == 293  # 293.3 → 293
        assert weigh_amount_cents(250, 419, 500) == 210  # 209.5 → 210
        assert weigh_amount_cents(500, 419, 500) == 419

    def test_invalid_inputs_raise(self):
        with pytest.raises(ValueError):
            weigh_amount_cents(0, 419, 500)
        with pytest.raises(ValueError):
            weigh_amount_cents(100, -1, 500)
        with pytest.raises(ValueError):
            weigh_amount_cents(100, 419, 0)


# ── 主干 HTTP 鉴权三态 (无需 DB) ────────────────────────────────────────────


class TestWeighWebhookAuth:
    def test_missing_token_401(self, client):
        r = client.post(
            "/ext/hardware/webhook/weigh",
            json={"message_id": "wtest-auth", "weight_grams": 350},
        )
        assert r.status_code == 401

    def test_wrong_token_403(self, client):
        r = client.post(
            "/ext/hardware/webhook/weigh",
            headers={"Authorization": "Bearer <redacted>"},
            json={"message_id": "wtest-auth", "weight_grams": 350},
        )
        assert r.status_code == 403

    def test_valid_token_without_db_503(self, client):
        """凭据对了但 DB 不可达 → 503（handler 层在鉴权之后才碰库）。"""
        from app.deps import get_settings

        secret = get_settings().hardware_secret
        r = client.post(
            "/ext/hardware/webhook/weigh",
            headers={"Authorization": f"Bearer {secret}"},
            json={"message_id": "wtest-auth", "weight_grams": 350},
        )
        assert r.status_code == 503


# ── DB 集成 ────────────────────────────────────────────────────────────────


def _fake_request(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


@pytest.fixture
async def weigh_pool():
    from db_test_utils import cleanup_db_test_rows
    from obase.persistence.pool import PgPool

    from app.ext.schema import ensure_ext_schema
    from app.p0_schema import ensure_p0_schema

    pool = await PgPool.create(name="hemall_weigh_test", dsn=TEST_DSN, min_size=1, max_size=5)
    await cleanup_db_test_rows(pool)
    await ensure_p0_schema(pool)
    await ensure_ext_schema(pool)
    state: dict = {"tickets": [], "batches": [], "variants": [], "products": [], "customers": []}
    yield pool, state
    async with pool.acquire() as conn:
        if state["tickets"]:
            await conn.execute(
                'DELETE FROM "hardware_event" WHERE ticket_id = ANY($1::uuid[])',
                state["tickets"],
            )
        await conn.execute("DELETE FROM \"hardware_event\" WHERE message_id LIKE 'wtest-%'")
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
            f"cw-{tag}@test.dev",
        )
    state["customers"].append(cid)
    return cid


async def _seed_batch(pool, state, tag: str, *, price: int | None, spec: str) -> tuple[str, str]:
    """种一个可售批次，返回 (batch_id, variant_id)。"""
    import datetime
    import uuid

    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, "
            "lat, lng, status) VALUES ($1, 'cw_loc', 'cn-east', 'host_cw', '柜台', "
            "31.23, 121.47, 'active') RETURNING id",
            uuid7(),
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,$2,$3,'published') "
            "RETURNING id",
            uuid7(),
            f"称重测试品{tag}",
            f"cw-slug-{tag}-{uuid.uuid4().hex[:8]}",
        )
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, option_values, "
            "reference_price_cents, status) VALUES ($1,$2,$3,$4,$5,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"CW-{tag}",
            f'{{"规格": "{spec}"}}',
            price,
        )
        batch_id = await conn.fetchval(
            "INSERT INTO inventory_batch (id, batch_no, variant_id, location_id, video_url, "
            "cost_price_cents, retail_price_cents, stock_qty, reserved_qty, "
            "expiration_time, status, inspection_status) "
            "VALUES ($1,$2,$3,$4,'https://v',500,$5,50,0,$6,'active','passed') RETURNING id",
            uuid7(),
            f"CW-{tag}",
            var_id,
            loc_id,
            price,
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30),
        )
    state["products"].append(str(prod_id))
    state["variants"].append(str(var_id))
    state["batches"].append(str(batch_id))
    return str(batch_id), str(var_id)


@DB_TESTS
class TestHardwareWeighDb:
    async def test_weigh_priced_and_idempotent(self, weigh_pool):
        from app.ext.hardware_webhook import WeighEvent, hardware_weigh

        pool, state = weigh_pool
        batch_id, _ = await _seed_batch(pool, state, "a", price=419, spec="500g/份")

        body = WeighEvent(message_id="wtest-weigh-001", weight_grams=350, batch_id=batch_id)
        result = await hardware_weigh(body, _fake_request(pool), _=None)
        assert result["status"] == "processed"
        assert result["amount_cents"] == 293
        assert result["unit_price_cents"] == 419
        assert result["price_basis_grams"] == 500
        assert result["source"] == "scale"

        again = await hardware_weigh(body, _fake_request(pool), _=None)
        assert again["status"] == "duplicate"

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT COUNT(*) AS n FROM "hardware_event" WHERE message_id = $1',
                "wtest-weigh-001",
            )
            assert row["n"] == 1

    async def test_weigh_without_batch_is_unpriced(self, weigh_pool):
        from app.ext.hardware_webhook import WeighEvent, hardware_weigh

        pool, _ = weigh_pool
        body = WeighEvent(message_id="wtest-weigh-002", weight_grams=700, tote_id="counter-1")
        result = await hardware_weigh(body, _fake_request(pool), _=None)
        assert result["status"] == "unpriced"
        assert result["amount_cents"] is None

    async def test_weigh_rejects_out_of_range(self, weigh_pool):
        from app.ext.hardware_webhook import WeighEvent, hardware_weigh

        pool, _ = weigh_pool
        with pytest.raises(Exception, match="out of range"):
            await hardware_weigh(
                WeighEvent(message_id="wtest-weigh-003", weight_grams=15001),
                _fake_request(pool),
                _=None,
            )

    async def test_weigh_rejects_unknown_price_basis(self, weigh_pool):
        from app.ext.hardware_webhook import WeighEvent, hardware_weigh

        pool, state = weigh_pool
        batch_id, _ = await _seed_batch(pool, state, "b", price=419, spec="散装")
        with pytest.raises(Exception, match="price basis unknown"):
            await hardware_weigh(
                WeighEvent(message_id="wtest-weigh-004", weight_grams=350, batch_id=batch_id),
                _fake_request(pool),
                _=None,
            )

    async def test_confirm_unpriced_event(self, weigh_pool):
        from app.ext.hardware_webhook import (
            WeighEvent,
            confirm_weigh_event,
            hardware_weigh,
        )

        pool, state = weigh_pool
        batch_id, _ = await _seed_batch(pool, state, "c", price=419, spec="500g/份")
        merchant_id = await _seed_merchant(pool, state, "m1")
        await hardware_weigh(
            WeighEvent(message_id="wtest-weigh-005", weight_grams=350, tote_id="counter-1"),
            _fake_request(pool),
            _=None,
        )
        async with pool.acquire() as conn:
            ticket_id = await conn.fetchval(
                "INSERT INTO \"counter_ticket\" (status, created_by) VALUES ('open', $1) "
                "RETURNING id",
                merchant_id,
            )
        state["tickets"].append(str(ticket_id))
        async with pool.acquire() as conn:
            confirmed = await confirm_weigh_event(
                conn,
                message_id="wtest-weigh-005",
                batch_id=batch_id,
                variant_id=None,
                ticket_id=str(ticket_id),
            )
        assert confirmed["status"] == "processed"
        assert confirmed["amount_cents"] == 293
        assert confirmed["ticket_id"] == str(ticket_id)
        # 二次认领拒绝
        async with pool.acquire() as conn:
            with pytest.raises(ValueError, match="already claimed"):
                await confirm_weigh_event(
                    conn,
                    message_id="wtest-weigh-005",
                    batch_id=batch_id,
                    variant_id=None,
                    ticket_id=str(ticket_id),
                )

    async def test_confirm_ticket_bound_unpriced_event(self, weigh_pool):
        """已挂单据的 unpriced 事件（秤先发重量、柜台后确认）就地计价，不拒认领。"""
        from app.ext.hardware_webhook import (
            WeighEvent,
            confirm_weigh_event,
            hardware_weigh,
        )

        pool, state = weigh_pool
        batch_id, _ = await _seed_batch(pool, state, "c2", price=419, spec="500g/份")
        merchant_id = await _seed_merchant(pool, state, "m1b")
        async with pool.acquire() as conn:
            ticket_id = await conn.fetchval(
                "INSERT INTO \"counter_ticket\" (status, created_by) VALUES ('open', $1) "
                "RETURNING id",
                merchant_id,
            )
        state["tickets"].append(str(ticket_id))
        result = await hardware_weigh(
            WeighEvent(
                message_id="wtest-weigh-006",
                weight_grams=350,
                ticket_id=str(ticket_id),
            ),
            _fake_request(pool),
            _=None,
        )
        assert result["status"] == "unpriced"
        async with pool.acquire() as conn:
            confirmed = await confirm_weigh_event(
                conn,
                message_id="wtest-weigh-006",
                batch_id=batch_id,
                variant_id=None,
                ticket_id=str(ticket_id),
            )
        assert confirmed["status"] == "processed"
        assert confirmed["amount_cents"] == 293
        assert confirmed["ticket_id"] == str(ticket_id)


@DB_TESTS
class TestCounterFlowDb:
    async def test_ticket_flow_and_settlement(self, weigh_pool):
        import app.storefront as sf

        pool, state = weigh_pool
        batch_id, variant_id = await _seed_batch(pool, state, "d", price=419, spec="500g/份")
        merchant_id = await _seed_merchant(pool, state, "m2")
        principal = {"customer_id": merchant_id}
        req = _fake_request(pool)

        created = await sf.counter_create_ticket(req, {}, principal)
        ticket_id = created["ticket_id"]
        state["tickets"].append(ticket_id)
        assert created["status"] == "open"

        added = await sf.counter_add_line(
            req,
            ticket_id,
            {"batch_id": batch_id, "weight_grams": 350, "source": "manual"},
            principal,
        )
        assert added["line"]["amount_cents"] == 293
        assert added["total_cents"] == 293

        detail = await sf.counter_get_ticket(req, ticket_id, principal)
        assert detail["ticket"]["total_cents"] == 293
        assert len(detail["lines"]) == 1

        # 模拟器注入 + 确认挂单
        sim = await sf.counter_simulate_weigh(
            req, {"ticket_id": ticket_id, "weight_grams": 500}, principal
        )
        assert sim["simulator"] is True
        assert sim["status"] == "unpriced"
        latest = await sf.counter_latest_weight(req, ticket_id=ticket_id, principal=principal)
        assert latest["weight_grams"] == 500
        confirmed = await sf.counter_add_line(
            req,
            ticket_id,
            {"variant_id": variant_id, "weigh_message_id": sim["message_id"]},
            principal,
        )
        assert confirmed["line"]["amount_cents"] == 419
        assert confirmed["total_cents"] == 293 + 419

        settled = await sf.counter_mark_pending_settlement(req, ticket_id, principal)
        assert settled["status"] == "pending_settlement"
        assert settled["payment"] is None
        with pytest.raises(Exception, match="not open"):
            await sf.counter_void_ticket(req, ticket_id, principal)

    async def test_products_lists_only_priced(self, weigh_pool):
        import app.storefront as sf

        pool, state = weigh_pool
        await _seed_batch(pool, state, "e", price=419, spec="500g/份")
        await _seed_batch(pool, state, "f", price=None, spec="500g/份")
        merchant_id = await _seed_merchant(pool, state, "m3")
        result = await sf.counter_products(_fake_request(pool), {"customer_id": merchant_id})
        titles = [p["title"] for p in result["products"]]
        assert "称重测试品e" in titles
        assert "称重测试品f" not in titles
        row = next(p for p in result["products"] if p["title"] == "称重测试品e")
        assert row["unit_price_cents"] == 419
        assert row["price_basis_grams"] == 500

    async def test_non_merchant_is_forbidden(self, weigh_pool):
        from fastapi import HTTPException

        import app.storefront as sf

        pool, state = weigh_pool
        plain_id = await _seed_merchant(pool, state, "m4")
        async with pool.acquire() as conn:
            await conn.execute('UPDATE "customer" SET is_merchant = false WHERE id = $1', plain_id)
        with pytest.raises(HTTPException, match="merchant account required"):
            await sf.counter_products(_fake_request(pool), {"customer_id": plain_id})
