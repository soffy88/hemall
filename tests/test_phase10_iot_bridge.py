"""tests/test_phase10_iot_bridge.py — Phase 10: IoT 边缘网桥防腐层 (ACL)。

覆盖:
  - oskill 算子: compute_cart_delta (重量增量→数量增量) / decide_gate_reconcile
    (三档裁决) / compute_tare_adjustment (封顶皮重漂移)
  - edge_bridge.handlers 纯翻译: topic 拆解 / pick 短键→长键 / gate 对账 /
    裁决回执映射 (零依赖可单测)
  - 主干 HTTP 鉴权三态: 无 token 401 / 错 token 403 / 正 token 无 DB 503
  - DB 集成 (TEST_PG_DSN): pick 挂批/孤儿/幂等防重, 闸口状态机
    (首见校准 → pick 累计 → pass 放行+皮重补偿 / block 拦截), HTTP 全链路
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.ext.oskill import (
    compute_cart_delta,
    compute_tare_adjustment,
    decide_gate_reconcile,
)

TEST_DSN = os.environ.get("TEST_PG_DSN")

DB_TESTS = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration"
)

# 让主干测试套件能 import 独立外挂域的纯翻译模块 (handlers.py 零第三方依赖)。
EDGE_DIR = Path(__file__).resolve().parent.parent / "edge_bridge"
if str(EDGE_DIR) not in sys.path:
    sys.path.insert(0, str(EDGE_DIR))

from handlers import (  # noqa: E402
    build_gate_payload,
    build_gate_reply,
    build_pick_payload,
    parse_edge_topic,
)


# ── oskill: 物理→商业算子 ─────────────────────────────────────────────────


class TestComputeCartDelta:
    def test_takeaway_is_negative(self):
        """拿走 185g (单件 90g) → 2 件入库 (负增量 = 顾客拿走)。"""
        assert compute_cart_delta(-185, 90) == -2

    def test_putback_is_positive(self):
        assert compute_cart_delta(90, 90) == 1

    def test_rounds_to_nearest_unit(self):
        """称重噪声: 178g / 90g ≈ 1.98 → 2 件。"""
        assert compute_cart_delta(-178, 90) == -2

    def test_noise_below_half_unit_is_zero(self):
        """44g / 90g ≈ 0.49 → 0 件 (不产生 0 元购物车行)。"""
        assert compute_cart_delta(-44, 90) == 0

    def test_zero_unit_rejected(self):
        with pytest.raises(ValueError):
            compute_cart_delta(-100, 0)


class TestDecideGateReconcile:
    def test_within_tolerance_passes(self):
        assert decide_gate_reconcile(2650, 2640, 20) == "pass"

    def test_tolerance_boundary_passes(self):
        assert decide_gate_reconcile(2660, 2640, 20) == "pass"

    def test_one_to_two_times_tolerance_rechecks(self):
        """超差 30g (tolerance=20) → 黄灯复核，不放行。"""
        assert decide_gate_reconcile(2670, 2640, 20) == "recheck"

    def test_over_double_tolerance_blocks(self):
        assert decide_gate_reconcile(2750, 2640, 20) == "block"

    def test_negative_tolerance_rejected(self):
        with pytest.raises(ValueError):
            decide_gate_reconcile(100, 100, -1)


class TestComputeTareAdjustment:
    def test_drift_absorbed_into_tare(self):
        """放行时残差 8g → tare 补偿 +8 (封顶内)。"""
        assert compute_tare_adjustment(2648, 2640, 15) == 8

    def test_drift_clamped_at_max(self):
        """残差 40g 超过 max 15 → 只补偿 15 (防单次大幅跳变)。"""
        assert compute_tare_adjustment(2680, 2640, 15) == 15

    def test_negative_drift(self):
        assert compute_tare_adjustment(2630, 2640, 15) == -10

    def test_negative_max_rejected(self):
        with pytest.raises(ValueError):
            compute_tare_adjustment(100, 100, -1)


# ── edge_bridge.handlers: 纯翻译 ──────────────────────────────────────────


class TestParseEdgeTopic:
    def test_pick_topic(self):
        assert parse_edge_topic("cn/v1/n/n1/s/s03/pick") == {
            "node_id": "n1",
            "entity": "s",
            "entity_id": "s03",
            "signal": "pick",
        }

    def test_gate_topic(self):
        assert parse_edge_topic("cn/v1/n/n1/g/g2/req") == {
            "node_id": "n1",
            "entity": "g",
            "entity_id": "g2",
            "signal": "req",
        }

    def test_malformed_topic_rejected(self):
        with pytest.raises(ValueError):
            parse_edge_topic("n1/s/s03/pick")


class TestBuildPickPayload:
    def test_short_to_long_keys(self):
        payload = build_pick_payload(
            {"m_id": "m-001", "dw": -185, "t_id": "042", "ts": 1715340982900},
            "cn/v1/n/n1/s/s03/pick",
        )
        assert payload == {
            "message_id": "m-001",
            "node_id": "n1",
            "shelf_id": "s03",
            "delta_weight": -185,
            "tote_id": "042",
            "timestamp": 1715340982900,
        }

    def test_missing_timestamp_is_null(self):
        payload = build_pick_payload(
            {"m_id": "m-002", "dw": 90, "t_id": "042"},
            "cn/v1/n/n1/s/s03/pick",
        )
        assert payload["timestamp"] is None

    def test_wrong_signal_rejected(self):
        with pytest.raises(ValueError):
            build_pick_payload(
                {"m_id": "x", "dw": 1, "t_id": "t"}, "cn/v1/n/n1/g/g1/req"
            )

    def test_missing_field_rejected(self):
        with pytest.raises(ValueError):
            build_pick_payload({"m_id": "x"}, "cn/v1/n/n1/s/s03/pick")


class TestBuildGatePayload:
    def test_short_to_long_keys(self):
        payload = build_gate_payload(
            {"m_id": "g-001", "t_id": "042", "rw": 2650},
            "cn/v1/n/n1/g/g1/req",
        )
        assert payload == {"gate_id": "g1", "tote_id": "042", "raw_weight_grams": 2650}


class TestBuildGateReply:
    def test_maps_decision_to_mqtt_reply(self):
        topic, reply = build_gate_reply(
            {"node_id": "n1", "entity_id": "g1"},
            {"action": "pass", "led": "green"},
            {"m_id": "g-001"},
        )
        assert topic == "cn/v1/n/n1/g/g1/res"
        assert reply == {"req_id": "g-001", "act": "pass", "led": "green"}

    def test_falls_back_to_block_when_decision_absent(self):
        """主干宕机降级: 空决策 → block/red (网桥兜底语义)。"""
        _, reply = build_gate_reply(
            {"node_id": "n1", "entity_id": "g1"}, {}, {"m_id": "x"}
        )
        assert reply == {"req_id": "x", "act": "block", "led": "red"}

    def test_accepts_bridge_side_key_act(self):
        """兼容主干返回 "act" 键 (SPEC §4 网桥骨架读的键)。"""
        _, reply = build_gate_reply(
            {"node_id": "n1", "entity_id": "g1"},
            {"act": "recheck", "led": "yellow"},
            {"m_id": "g-002"},
        )
        assert reply == {"req_id": "g-002", "act": "recheck", "led": "yellow"}


# ── 主干 HTTP 鉴权三态 (无需 DB) ──────────────────────────────────────────


class TestHardwareWebhookAuth:
    def test_missing_token_401(self, client):
        r = client.post(
            "/ext/hardware/webhook/pick",
            json={
                "message_id": "m1",
                "node_id": "n1",
                "shelf_id": "s1",
                "delta_weight": -100,
                "tote_id": "t1",
            },
        )
        assert r.status_code == 401

    def test_wrong_token_403(self, client):
        r = client.post(
            "/ext/hardware/webhook/pick",
            headers={"Authorization": "Bearer not-the-secret"},
            json={
                "message_id": "m1",
                "node_id": "n1",
                "shelf_id": "s1",
                "delta_weight": -100,
                "tote_id": "t1",
            },
        )
        assert r.status_code == 403

    def test_valid_token_without_db_503(self, client):
        """凭据对了但 DB 不可达 → 503 (handler 层在鉴权之后才碰库)。"""
        r = client.post(
            "/ext/hardware/webhook/pick",
            headers={"Authorization": "Bearer dev-hardware-secret-change-me"},
            json={
                "message_id": "m1",
                "node_id": "n1",
                "shelf_id": "s1",
                "delta_weight": -100,
                "tote_id": "t1",
            },
        )
        assert r.status_code == 503


# ── DB 集成: pick / 闸口状态机 (TEST_PG_DSN) ──────────────────────────────


@DB_TESTS
async def _seed_batch(pool) -> tuple[str, str]:
    """种一个批次 + 一条货架位映射，返回 (batch_id, shelf_id)。"""
    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, "
            "lat, lng, status) VALUES ($1, 'p10_loc', 'cn-east', 'host_p10', '车库', "
            "31.23, 121.47, 'active') RETURNING id",
            uuid7(),
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,$2,$3,'published') "
            "RETURNING id",
            uuid7(),
            "AIoT重力货架测试品",
            f"p10-slug-{uuid7().replace('-', '')}",
        )
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"P10-{uuid7().replace('-', '')}",
        )
        batch_id = await conn.fetchval(
            "INSERT INTO inventory_batch (id, batch_no, variant_id, location_id, video_url, "
            "cost_price_cents, retail_price_cents, stock_qty, reserved_qty, "
            "expiration_time, status) "
            "VALUES ($1,$2,$3,$4,'https://v',500,990,50,0,$5,'active') RETURNING id",
            uuid7(),
            f"P10-{uuid7().replace('-', '')}",
            var_id,
            loc_id,
            __import__("datetime").datetime.now(__import__("datetime").UTC)
            + __import__("datetime").timedelta(days=30),
        )
        shelf_id = "s-iot-01"
        await conn.execute(
            "INSERT INTO hardware_shelf (shelf_id, node_id, batch_id, unit_weight_grams, status) "
            "VALUES ($1, 'n-iot-01', $2, 90, 'active')",
            shelf_id,
            batch_id,
        )
    return str(batch_id), shelf_id


@pytest.fixture
async def cn_pool():
    from db_test_utils import cleanup_db_test_rows

    from obase.persistence.pool import PgPool

    from app.ext.schema import ensure_ext_schema

    pool = await PgPool.create(
        name="hemall_phase10_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await cleanup_db_test_rows(pool)
    await ensure_ext_schema(pool)
    yield pool
    await pool.close()


@DB_TESTS
class TestHardwarePickDb:
    async def test_pick_resolves_shelf_and_quantifies(self, cn_pool, tmp_path):
        """pick → shelf 映射 → qty_delta 折算 → processed 落账。"""
        from pydantic import BaseModel

        from app.ext.hardware_webhook import hardware_pick

        batch_id, shelf_id = await _seed_batch(cn_pool)

        class _FakeRequest:
            app = type(
                "A",
                (),
                {
                    "state": type(
                        "S",
                        (),
                        {
                            "pool": cn_pool,
                            "config": type("C", (), {"hardware_secret": "x"})(),
                        },
                    )()
                },
            )()

        class _Pick(BaseModel):
            message_id: str
            node_id: str
            shelf_id: str
            delta_weight: int
            tote_id: str
            timestamp: int | None = None

        result = await hardware_pick(
            _Pick(
                message_id="m-pick-001",
                node_id="n-iot-01",
                shelf_id=shelf_id,
                delta_weight=-185,
                tote_id="tote-042",
            ),
            _FakeRequest(),
            _=None,
        )
        assert result["status"] == "processed"
        assert result["batch_id"] == batch_id
        assert result["qty_delta"] == -2  # 185g / 90g → 2 件拿走

        async with cn_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM hardware_event WHERE message_id = $1", "m-pick-001"
            )
            assert row["status"] == "processed"
            assert str(row["batch_id"]) == batch_id
            assert row["qty_delta"] == -2
            assert row["unit_weight_grams"] == 90

    async def test_pick_without_shelf_mapping_goes_orphan(self, cn_pool, tmp_path):
        """货架位未映射 → 孤儿队列 (status=orphan)，不假装知道商业归属。"""
        from pydantic import BaseModel

        from app.ext.hardware_webhook import hardware_pick

        class _Cfg:
            hardware_secret = "dev-hardware-secret-change-me"

        class _State:
            pool = cn_pool
            config = _Cfg()

        class _FakeRequest:
            app = type("A", (), {"state": _State()})()

        class _Pick(BaseModel):
            message_id: str
            node_id: str
            shelf_id: str
            delta_weight: int
            tote_id: str
            timestamp: int | None = None

        result = await hardware_pick(
            _Pick(
                message_id="m-pick-orphan",
                node_id="n-iot-01",
                shelf_id="s-unmapped",
                delta_weight=-185,
                tote_id="tote-042",
            ),
            _FakeRequest(),
            _=None,
        )
        assert result["status"] == "orphan"
        assert result["batch_id"] is None
        assert result["qty_delta"] == 0

    async def test_duplicate_message_id_is_idempotent(self, cn_pool, tmp_path):
        """同一 message_id 重发 → duplicate，不重复计账。"""
        from pydantic import BaseModel

        from app.ext.hardware_webhook import hardware_pick

        class _Cfg:
            hardware_secret = "dev-hardware-secret-change-me"

        class _FakeRequest:
            app = type(
                "A", (), {"state": type("S", (), {"pool": cn_pool, "config": _Cfg()})()}
            )()

        class _Pick(BaseModel):
            message_id: str
            node_id: str
            shelf_id: str
            delta_weight: int
            tote_id: str
            timestamp: int | None = None

        batch_id, shelf_id = await _seed_batch(cn_pool)
        body = _Pick(
            message_id="m-pick-dedup",
            node_id="n-iot-01",
            shelf_id=shelf_id,
            delta_weight=-90,
            tote_id="tote-042",
        )
        first = await hardware_pick(body, _FakeRequest(), _=None)
        assert first["status"] == "processed"
        second = await hardware_pick(body, _FakeRequest(), _=None)
        assert second["status"] == "duplicate"

        async with cn_pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT count(*) FROM hardware_event WHERE message_id = $1",
                "m-pick-dedup",
            )
            assert n == 1


@DB_TESTS
class TestGateReconcileDb:
    async def test_first_sighting_calibrates_and_passes(self, cn_pool, tmp_path):
        """闸口首见: raw 作为皮重基线，直接放行 (绿)。"""
        from pydantic import BaseModel

        from app.ext.hardware_webhook import gate_reconcile

        class _FakeRequest:
            app = type("A", (), {"state": type("S", (), {"pool": cn_pool})()})()

        class _Gate(BaseModel):
            gate_id: str
            tote_id: str
            raw_weight_grams: int

        result = await gate_reconcile(
            _Gate(gate_id="g-iot-01", tote_id="tote-042", raw_weight_grams=500),
            _FakeRequest(),
            _=None,
        )
        assert result["action"] == "pass"
        assert result["led"] == "green"
        assert result["tare_adj"] == 0

        async with cn_pool.acquire() as conn:
            gate = await conn.fetchrow(
                "SELECT * FROM hardware_gate WHERE gate_id = $1", "g-iot-01"
            )
            assert gate["status"] == "idle"
            assert gate["tare_weight_grams"] == 500

    async def test_picks_accumulate_and_pass_with_tare_adjustment(
        self, cn_pool, tmp_path
    ):
        """tare 500 + 拣货 2×90g=180 → 期望 680；实测 685 (公差 20) → pass，皮重 +5。"""
        from pydantic import BaseModel

        from app.ext.hardware_webhook import gate_reconcile, hardware_pick

        batch_id, shelf_id = await _seed_batch(cn_pool)

        class _Cfg:
            hardware_secret = "dev-hardware-secret-change-me"

        class _FakeRequest:
            app = type(
                "A", (), {"state": type("S", (), {"pool": cn_pool, "config": _Cfg()})()}
            )()

        class _Pick(BaseModel):
            message_id: str
            node_id: str
            shelf_id: str
            delta_weight: int
            tote_id: str
            timestamp: int | None = None

        class _Gate(BaseModel):
            gate_id: str
            tote_id: str
            raw_weight_grams: int

        # 首见校准 (皮重 500)
        await gate_reconcile(
            _Gate(gate_id="g-iot-02", tote_id="tote-042", raw_weight_grams=500),
            _FakeRequest(),
            _=None,
        )
        # 顾客拿走 2 件 (90g/件)
        await hardware_pick(
            _Pick(
                message_id="m-pick-g2",
                node_id="n-iot-01",
                shelf_id=shelf_id,
                delta_weight=-180,
                tote_id="tote-042",
            ),
            _FakeRequest(),
            _=None,
        )
        # 闸口实测 685 → 期望 500+180=680, 超差 5 ≤ 20 → pass, tare 补偿 +5
        result = await gate_reconcile(
            _Gate(gate_id="g-iot-02", tote_id="tote-042", raw_weight_grams=685),
            _FakeRequest(),
            _=None,
        )
        assert result["action"] == "pass"
        assert result["led"] == "green"
        assert result["expected_weight_grams"] == 680
        assert result["tare_adj"] == 5

        async with cn_pool.acquire() as conn:
            gate = await conn.fetchrow(
                "SELECT * FROM hardware_gate WHERE gate_id = $1", "g-iot-02"
            )
            assert gate["tare_weight_grams"] == 505  # 500 + 漂移补偿 5
            assert gate["expected_weight_grams"] == 505  # 放行后归位到新 tare
            assert gate["status"] == "idle"

    async def test_mismatch_blocks(self, cn_pool, tmp_path):
        """实测与期望严重不符 (超差 2 倍公差) → block (红灯拦截)。"""
        from pydantic import BaseModel

        from app.ext.hardware_webhook import gate_reconcile

        class _FakeRequest:
            app = type("A", (), {"state": type("S", (), {"pool": cn_pool})()})()

        class _Gate(BaseModel):
            gate_id: str
            tote_id: str
            raw_weight_grams: int

        await gate_reconcile(
            _Gate(gate_id="g-iot-03", tote_id="tote-099", raw_weight_grams=500),
            _FakeRequest(),
            _=None,
        )
        # 期望 500 (无 pick 累计)，实测 700 → 超差 200 >> 40 → block
        result = await gate_reconcile(
            _Gate(gate_id="g-iot-03", tote_id="tote-099", raw_weight_grams=700),
            _FakeRequest(),
            _=None,
        )
        assert result["action"] == "block"
        assert result["led"] == "red"

        async with cn_pool.acquire() as conn:
            gate = await conn.fetchrow(
                "SELECT * FROM hardware_gate WHERE gate_id = $1", "g-iot-03"
            )
            assert gate["status"] == "blocked"


@DB_TESTS
def test_http_roundtrip_pick_and_gate():
    """HTTP 全链路: 鉴权 + pick + 闸口对账 (真实 Postgres)。"""
    import asyncio

    from fastapi.testclient import TestClient

    from app.deps import get_settings
    from app.main import create_app

    os.environ["HEMALL_PG_DSN"] = TEST_DSN
    get_settings.cache_clear()

    async def _seed() -> tuple[str, str]:
        from db_test_utils import cleanup_db_test_rows

        from obase.persistence.pool import PgPool

        from app.ext.schema import ensure_ext_schema

        pool = await PgPool.create(
            name="hemall_phase10_http", dsn=TEST_DSN, min_size=1, max_size=2
        )
        try:
            await cleanup_db_test_rows(pool)
            await ensure_ext_schema(pool)
            return await _seed_batch(pool)
        finally:
            await pool.close()

    batch_id, shelf_id = asyncio.run(_seed())
    headers = {"Authorization": "Bearer dev-hardware-secret-change-me"}

    app = create_app()
    with TestClient(app) as client:
        db_health = client.get("/health/ready").json()["checks"]["database"]["status"]
        if db_health != "up":
            pytest.skip("database not reachable at TEST_PG_DSN")

        # 1. pick (公开鉴权端点)
        r = client.post(
            "/ext/hardware/webhook/pick",
            headers=headers,
            json={
                "message_id": "m-http-1",
                "node_id": "n-iot-01",
                "shelf_id": shelf_id,
                "delta_weight": -90,
                "tote_id": "tote-http",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "processed"
        assert r.json()["qty_delta"] == -1

        # 2. 重发同一 message_id → duplicate
        r2 = client.post(
            "/ext/hardware/webhook/pick",
            headers=headers,
            json={
                "message_id": "m-http-1",
                "node_id": "n-iot-01",
                "shelf_id": shelf_id,
                "delta_weight": -90,
                "tote_id": "tote-http",
            },
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "duplicate"

        # 3. 闸口首见 → pass (绿)
        r3 = client.post(
            "/ext/hardware/webhook/gate-reconcile",
            headers=headers,
            json={
                "gate_id": "g-http-1",
                "tote_id": "tote-http",
                "raw_weight_grams": 500,
            },
        )
        assert r3.status_code == 200, r3.text
        assert r3.json()["action"] == "pass"
        assert r3.json()["led"] == "green"

        # 4. 无 token → 401 (防未授权打桩)
        r4 = client.post(
            "/ext/hardware/webhook/pick",
            json={
                "message_id": "m-http-2",
                "node_id": "n1",
                "shelf_id": "s1",
                "delta_weight": -1,
                "tote_id": "t",
            },
        )
        assert r4.status_code == 401

    get_settings.cache_clear()
