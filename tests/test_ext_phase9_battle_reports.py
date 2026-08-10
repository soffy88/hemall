"""tests/test_ext_phase9_battle_reports.py — Phase 9 (补天): 全自动战报式评价体系。

覆盖:
  - oskill 算子: 贝叶斯平滑动态评分 / 贡献激励奖励 (带图加钱、不看极性)
  - oprim 原子: ext_vlm_parse_battle_report (LLM JSON 注入 + 越界钳制 +
    规则兜底解析, 正/负向都确定性可测)
  - omodul 事务: 订单溯源校验 / 批次锚定 / 一单一报防重 / system_balance
    发奖闭环 / 差评联动供应商信誉扣减 (需真实 Postgres, TEST_PG_DSN)
  - 读接口: queries.get_batch_battle_report_summary 聚合 + HTTP 端点
    POST /store/submit_battle_report_workflow + GET /store/batches/{id}/battle-reports
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.ext.oprim import _BATTLE_REPORT_SYSTEM_PROMPT
from app.ext.oskill import (
    calculate_battle_report_reward,
    compute_batch_dynamic_rating,
)

TEST_DSN = os.environ.get("TEST_PG_DSN")

DB_TESTS = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration"
)


# ── oskill: 贝叶斯平滑动态评分 ─────────────────────────────────────────────


class TestComputeBatchDynamicRating:
    def test_first_report_seeds_rating(self):
        """首条战报 (无历史) → 评分 = 极性映射值, 条数 = 1。"""
        rating, count = compute_batch_dynamic_rating(0.0, 0, 0.84)
        assert count == 1
        assert rating == round((0.84 + 1.0) / 2.0 * 100.0, 2)  # 92.0

    def test_extreme_negative_first(self):
        """-1.0 愤怒 → 0 分 (可视化评分下限)。"""
        rating, count = compute_batch_dynamic_rating(0.0, 0, -1.0)
        assert rating == 0.0
        assert count == 1

    def test_extreme_positive_first(self):
        assert compute_batch_dynamic_rating(0.0, 0, 1.0)[0] == 100.0

    def test_smoothing_dilutes_single_extreme(self):
        """已有 9 条 90 分的评价, 一条 -1.0 差评只把评分拉到 81, 不直接毁掉批次。"""
        rating, count = compute_batch_dynamic_rating(90.0, 9, -1.0)
        assert count == 10
        assert rating == round((90.0 * 9 + 0.0) / 10.0, 2)  # 81.0

    def test_history_inertia_grows_with_count(self):
        """100 条满分后一条零分, 几乎不动 (贝叶斯惯性)。"""
        rating, count = compute_batch_dynamic_rating(99.0, 99, -1.0)
        assert rating > 98.0

    def test_validation(self):
        with pytest.raises(ValueError):
            compute_batch_dynamic_rating(-1.0, 0, 0.0)
        with pytest.raises(ValueError):
            compute_batch_dynamic_rating(50.0, -1, 0.0)
        with pytest.raises(ValueError):
            compute_batch_dynamic_rating(50.0, 0, 1.5)


# ── oskill: 贡献激励奖励 ────────────────────────────────────────────────────


class TestCalculateBattleReportReward:
    def test_text_only_base_reward(self):
        assert calculate_battle_report_reward(has_image=False) == 20

    def test_with_image_bonus(self):
        assert calculate_battle_report_reward(has_image=True) == 50

    def test_reward_ignores_sentiment(self):
        """差评战报同样拿满基础奖励 (帮系统排雷), 奖励只由是否带图决定。"""
        assert calculate_battle_report_reward(has_image=False) == 20
        assert calculate_battle_report_reward(has_image=True) == 50


# ── oprim: 多模态战报提纯 ──────────────────────────────────────────────────


@pytest.fixture
def llm_provider():
    from obase.provider_registry import ProviderRegistry

    from app.ext.llm_provider import ManualLLMProvider

    reg = ProviderRegistry.get()
    provider = ManualLLMProvider()
    reg.register_generic("llm", "manual", provider, replace=True)
    return provider


async def _vlm(text: str, image_url: str | None = None) -> dict:
    from app.ext.oprim import ext_vlm_parse_battle_report

    return await ext_vlm_parse_battle_report("manual", text=text, image_url=image_url)


class TestVlmParseBattleReport:
    async def test_llm_json_is_used(self, llm_provider):
        """模型返回合法 JSON → 原样提纯入库 (有图交叉验证场景)。"""
        llm_provider.set_response(
            system_prompt=_BATTLE_REPORT_SYSTEM_PROMPT,
            user_prompt="Text: 脆甜多汁\nImage: https://cdn.example/x.jpg",
            text=json.dumps(
                {
                    "freshness_index": 0.92,
                    "sentiment_polarity": 0.84,
                    "keywords": ["脆甜", "多汁"],
                },
                ensure_ascii=False,
            ),
        )
        out = await _vlm("脆甜多汁", image_url="https://cdn.example/x.jpg")
        assert out["freshness_index"] == 0.92
        assert out["sentiment_polarity"] == 0.84
        assert out["keywords"] == ["脆甜", "多汁"]

    async def test_out_of_range_is_clamped(self, llm_provider):
        """模型乱给越界值 → 钳制回合法域, 否则 DECIMAL(3,2) 列装不下。"""
        llm_provider.set_response(
            system_prompt=_BATTLE_REPORT_SYSTEM_PROMPT,
            user_prompt="Text: x\nImage: (无)",
            text=json.dumps(
                {"freshness_index": 3.5, "sentiment_polarity": -7.0, "keywords": []}
            ),
        )
        out = await _vlm("x")
        assert out["freshness_index"] == 1.0
        assert out["sentiment_polarity"] == -1.0
        assert len(out["keywords"]) <= 3

    async def test_mock_placeholder_falls_back_positive(self, llm_provider):
        """mock provider 生成不了 JSON → 规则兜底, 正向文本确定性判正。"""
        out = await _vlm("又脆又甜，个头大，特别满意")
        assert out["sentiment_polarity"] > 0
        assert out["freshness_index"] > 0.5
        assert out["keywords"]

    async def test_mock_placeholder_falls_back_negative(self, llm_provider):
        """规则兜底对差评同样有效: 负向文本 → 负极性 + 低新鲜度。"""
        out = await _vlm("烂了，发酸，不新鲜，差评")
        assert out["sentiment_polarity"] < 0
        assert out["freshness_index"] < 0.5

    async def test_non_json_text_falls_back(self, llm_provider):
        llm_provider.set_response(
            system_prompt=_BATTLE_REPORT_SYSTEM_PROMPT,
            user_prompt="Text: 不错\nImage: (无)",
            text="纯文本不是JSON",
        )
        out = await _vlm("不错")
        assert -1.0 <= out["sentiment_polarity"] <= 1.0
        assert 0.0 <= out["freshness_index"] <= 1.0


# ── DB 集成: omodul 事务 + 读接口 (TEST_PG_DSN) ────────────────────────────


@pytest.fixture
async def cn_pool(tmp_path_factory):
    from obase.persistence.pool import PgPool
    from obase.provider_registry import ProviderRegistry

    from app.ext.llm_provider import ManualLLMProvider
    from app.ext.schema import ensure_ext_schema

    ProviderRegistry.get().register_generic(
        "llm", "manual", ManualLLMProvider(), replace=True
    )
    pool = await PgPool.create(
        name="hemall_phase9_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    from db_test_utils import cleanup_db_test_rows

    await cleanup_db_test_rows(pool)
    await ensure_ext_schema(pool)
    yield pool
    await pool.close()


async def _seed_order(
    pool,
    *,
    order_status: str = "delivered",
    supplier_trust: int = 100,
    batch_ids: list[str] | None = None,
) -> dict:
    """种一个供应商 → 微仓 → 商品 → 批次 → 顾客 → 已履约订单(含行项)。"""
    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        sup_id = await conn.fetchval(
            "INSERT INTO supplier (id, wallet_account, spatial_polygon, polygon_name, "
            "trust_score, escrow_balance, status) "
            "VALUES ($1, $2, '{}', 'p9_polygon', $3, 0, 'active') RETURNING id",
            uuid7(),
            f"wallet-{str(uuid7())[:8]}",
            supplier_trust,
        )
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, "
            "lat, lng, status) VALUES ($1, 'p9_loc', 'cn-east', 'host_p9', '车库', "
            "31.23, 121.47, 'active') RETURNING id",
            uuid7(),
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1, $2, $3, 'active') "
            "RETURNING id",
            uuid7(),
            "阳光玫瑰葡萄",
            f"p9-slug-{uuid7().replace('-', '')}",
        )
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, status) "
            "VALUES ($1, $2, $3, 'active') RETURNING id",
            uuid7(),
            prod_id,
            f"SKU-{uuid7().replace('-', '')[:16].upper()}",
        )
        batch_ids = batch_ids or [uuid7()]
        for bid in batch_ids:
            await conn.execute(
                "INSERT INTO inventory_batch (id, batch_no, variant_id, location_id, "
                "supplier_id, video_url, media_assets, cost_price_cents, "
                "retail_price_cents, stock_qty, reserved_qty, expiration_time, status) "
                "VALUES ($1, $2, $3, $4, $5, 'http://v', '[]', 500, 990, 10, 0, $6, 'active')",
                bid,
                f"BATCH-{uuid7().replace('-', '')}",
                var_id,
                loc_id,
                sup_id,
                datetime.now(UTC) + timedelta(days=30),
            )
        cust_id = await conn.fetchval(
            "INSERT INTO customer (id, email, phone, name, status, system_balance) "
            "VALUES ($1, $2, '13800000000', 'p9顾客', 'active', 0) RETURNING id",
            uuid7(),
            f"p9-{uuid7()}@test.dev",
        )
        order_id = await conn.fetchval(
            "INSERT INTO customer_order (id, customer_id, status, region_code, currency, "
            "shipping_cents, tax_cents, grand_total_cents) "
            "VALUES ($1, $2, $3, 'cn-east', 'cny', 0, 0, 990) RETURNING id",
            uuid7(),
            cust_id,
            order_status,
        )
        for bid in batch_ids:
            await conn.execute(
                "INSERT INTO order_line_item (id, order_id, batch_id, quantity, "
                "unit_price_cents, line_total_cents) VALUES ($1, $2, $3, 1, 990, 990)",
                uuid7(),
                order_id,
                bid,
            )
    return {
        "supplier_id": str(sup_id),
        "batch_ids": [str(b) for b in batch_ids],
        "customer_id": str(cust_id),
        "order_id": str(order_id),
    }


async def _submit(
    pool,
    *,
    user_id: str,
    order_id: str,
    text: str,
    image_url: str | None = None,
    batch_id: str | None = None,
    out_dir: Path,
) -> dict:
    from app.ext.omodul.submit_battle_report_workflow import (
        SubmitBattleReportWorkflowConfig,
        SubmitBattleReportWorkflowInput,
        submit_battle_report_workflow,
    )

    return await submit_battle_report_workflow(
        SubmitBattleReportWorkflowConfig(),
        SubmitBattleReportWorkflowInput(
            user_id=user_id,
            order_id=order_id,
            text=text,
            image_url=image_url,
            batch_id=batch_id,
        ),
        out_dir,
        pool=pool,
    )


@DB_TESTS
class TestSubmitBattleReportWorkflow:
    async def test_happy_path_with_image(self, cn_pool, tmp_path):
        """带图战报: 50 分算力金, 战报入账, 余额到账, 供应商不被误伤。"""
        s = await _seed_order(cn_pool)
        result = await _submit(
            cn_pool,
            user_id=s["customer_id"],
            order_id=s["order_id"],
            text="又脆又甜，个头大，特别满意",
            image_url="https://cdn.example/pic.jpg",
            out_dir=tmp_path,
        )
        assert result["status"] == "completed", result
        assert result["reward_granted"] == 50
        assert result["sentiment_polarity"] > 0
        assert result["supplier_penalized"] is False

        async with cn_pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT * FROM "batch_battle_report" WHERE order_id = $1',
                s["order_id"],
            )
            assert row is not None
            assert str(row["batch_id"]) == s["batch_ids"][0]
            assert row["reward_granted"] == 50
            assert row["status"] == "published"
            assert row["raw_image_url"] == "https://cdn.example/pic.jpg"
            balance = await conn.fetchval(
                "SELECT system_balance FROM customer WHERE id = $1", s["customer_id"]
            )
            assert balance == 50

    async def test_text_only_reward_is_base(self, cn_pool, tmp_path):
        s = await _seed_order(cn_pool)
        result = await _submit(
            cn_pool,
            user_id=s["customer_id"],
            order_id=s["order_id"],
            text="还行吧",
            out_dir=tmp_path,
        )
        assert result["status"] == "completed", result
        assert result["reward_granted"] == 20

    async def test_duplicate_report_rejected(self, cn_pool, tmp_path):
        """一单一报: 第二次提交同一订单 → already_reported, 余额不重复加。"""
        s = await _seed_order(cn_pool)
        first = await _submit(
            cn_pool,
            user_id=s["customer_id"],
            order_id=s["order_id"],
            text="不错",
            out_dir=tmp_path,
        )
        assert first["status"] == "completed"

        second = await _submit(
            cn_pool,
            user_id=s["customer_id"],
            order_id=s["order_id"],
            text="再评一次",
            out_dir=tmp_path,
        )
        assert second["status"] == "failed"
        assert second["reason"] == "already_reported"

        async with cn_pool.acquire() as conn:
            balance = await conn.fetchval(
                "SELECT system_balance FROM customer WHERE id = $1", s["customer_id"]
            )
            assert balance == 20  # 只发过一次

    async def test_order_not_settled_rejected(self, cn_pool, tmp_path):
        """订单未物理履约 (paid) → 拒绝评价。"""
        s = await _seed_order(cn_pool, order_status="paid")
        result = await _submit(
            cn_pool,
            user_id=s["customer_id"],
            order_id=s["order_id"],
            text="还没拿到货就评价?",
            out_dir=tmp_path,
        )
        assert result["status"] == "failed"
        assert result["reason"] == "invalid_or_incomplete_order"

    async def test_order_not_owned_rejected(self, cn_pool, tmp_path):
        """拿别人的 order_id 冒充评价 → 拒绝 (物理锁校验)。"""
        s = await _seed_order(cn_pool)
        result = await _submit(
            cn_pool,
            user_id="00000000-0000-0000-0000-000000000000",
            order_id=s["order_id"],
            text="我是别人",
            out_dir=tmp_path,
        )
        assert result["status"] == "failed"
        assert result["reason"] == "invalid_or_incomplete_order"

    async def test_reviewing_unpurchased_batch_rejected(self, cn_pool, tmp_path):
        """没买过的批次不允许评价 (batch_not_in_order)。"""
        from obase.uuid7 import uuid7

        s = await _seed_order(cn_pool)
        foreign_batch = str(uuid7())
        result = await _submit(
            cn_pool,
            user_id=s["customer_id"],
            order_id=s["order_id"],
            text="这个批次我没买",
            batch_id=foreign_batch,
            out_dir=tmp_path,
        )
        assert result["status"] == "failed"
        assert result["reason"] == "batch_not_in_order"

    async def test_negative_polarity_penalizes_supplier(self, cn_pool, tmp_path):
        """差评战报 (极性 < -0.5) → 追溯该批次供应商扣 2 分信誉。"""
        from obase.provider_registry import ProviderRegistry

        s = await _seed_order(cn_pool, supplier_trust=100)
        llm = ProviderRegistry.get().generic("llm", "manual")
        llm.set_response(
            system_prompt=_BATTLE_REPORT_SYSTEM_PROMPT,
            user_prompt="Text: 烂了，发酸，不新鲜\nImage: (无)",
            text=json.dumps(
                {
                    "freshness_index": 0.05,
                    "sentiment_polarity": -0.8,
                    "keywords": ["烂"],
                }
            ),
        )
        result = await _submit(
            cn_pool,
            user_id=s["customer_id"],
            order_id=s["order_id"],
            text="烂了，发酸，不新鲜",
            out_dir=tmp_path,
        )
        assert result["status"] == "completed", result
        assert result["supplier_penalized"] is True
        assert result["reward_granted"] == 20  # 差评也发基础奖励

        async with cn_pool.acquire() as conn:
            trust = await conn.fetchval(
                "SELECT trust_score FROM supplier WHERE id = $1", s["supplier_id"]
            )
            assert trust == 98

    async def test_mild_negative_does_not_penalize(self, cn_pool, tmp_path):
        """轻微不满 (-0.3) 不触发供应商处罚, 只有 < -0.5 才扣。"""
        from obase.provider_registry import ProviderRegistry

        s = await _seed_order(cn_pool, supplier_trust=100)
        llm = ProviderRegistry.get().generic("llm", "manual")
        llm.set_response(
            system_prompt=_BATTLE_REPORT_SYSTEM_PROMPT,
            user_prompt="Text: 一般\nImage: (无)",
            text=json.dumps(
                {
                    "freshness_index": 0.5,
                    "sentiment_polarity": -0.3,
                    "keywords": ["一般"],
                }
            ),
        )
        result = await _submit(
            cn_pool,
            user_id=s["customer_id"],
            order_id=s["order_id"],
            text="一般",
            out_dir=tmp_path,
        )
        assert result["status"] == "completed"
        assert result["supplier_penalized"] is False

        async with cn_pool.acquire() as conn:
            trust = await conn.fetchval(
                "SELECT trust_score FROM supplier WHERE id = $1", s["supplier_id"]
            )
            assert trust == 100


@DB_TESTS
class TestBattleReportReadSide:
    async def test_summary_aggregation(self, cn_pool, tmp_path):
        """多份战报 → 24h 计数 / 新鲜度拟合 / 关键词共现 / 最近实拍聚合正确。"""
        from app.queries import get_batch_battle_report_summary

        from obase.uuid7 import uuid7

        # 第一个订单自带批次 (batch_id = batch_ids[0])
        s = await _seed_order(cn_pool)
        batch_id = s["batch_ids"][0]

        # 另外两个订单直接挂在同一批次上 (不重复建批次)
        async def _order_on_batch() -> dict:
            async with cn_pool.acquire() as conn:
                cust_id = await conn.fetchval(
                    "INSERT INTO customer (id, email, phone, name, status, system_balance) "
                    "VALUES ($1, $2, '13800000001', 'p9顾客b', 'active', 0) RETURNING id",
                    uuid7(),
                    f"p9b-{uuid7()}@test.dev",
                )
                order_id = await conn.fetchval(
                    "INSERT INTO customer_order (id, customer_id, status, region_code, "
                    "currency, shipping_cents, tax_cents, grand_total_cents) "
                    "VALUES ($1, $2, 'delivered', 'cn-east', 'cny', 0, 0, 990) RETURNING id",
                    uuid7(),
                    cust_id,
                )
                await conn.execute(
                    "INSERT INTO order_line_item (id, order_id, batch_id, quantity, "
                    "unit_price_cents, line_total_cents) VALUES ($1, $2, $3, 1, 990, 990)",
                    uuid7(),
                    order_id,
                    batch_id,
                )
            return {"customer_id": str(cust_id), "order_id": str(order_id)}

        orders = [s, await _order_on_batch(), await _order_on_batch()]
        texts = ["又脆又甜，个头大", "很新鲜，回购", "脆甜，好吃"]
        for i, (o, t) in enumerate(zip(orders, texts)):
            await _submit(
                cn_pool,
                user_id=o["customer_id"],
                order_id=o["order_id"],
                text=t,
                image_url=f"https://cdn.example/pic{i}.jpg",
                out_dir=tmp_path,
            )

        summary = await get_batch_battle_report_summary(cn_pool, batch_id)
        assert summary is not None
        assert summary["report_count_total"] == 3
        assert summary["report_count_24h"] == 3
        assert (
            summary["freshness_fit_pct"] is not None
            and 0 <= summary["freshness_fit_pct"] <= 100
        )
        assert summary["dynamic_rating"] is not None
        assert len(summary["recent_images"]) == 3
        assert summary["recent_images"][0].startswith("https://cdn.example/")
        assert len(summary["top_keywords"]) >= 1

    async def test_summary_unknown_batch_returns_none(self, cn_pool):
        from app.queries import get_batch_battle_report_summary

        from obase.uuid7 import uuid7

        assert await get_batch_battle_report_summary(cn_pool, str(uuid7())) is None


@DB_TESTS
def test_http_endpoints_roundtrip():
    """HTTP 全链路: 注册路由 → 提交战报 → 读战报聚合 (真实 Postgres)。"""
    from fastapi.testclient import TestClient

    from app.deps import get_settings
    from app.main import create_app

    os.environ["HEMALL_PG_DSN"] = TEST_DSN
    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as client:
        db_health = client.get("/health/ready").json()["checks"]["database"]["status"]
        if db_health != "up":
            pytest.skip("database not reachable at TEST_PG_DSN")

        # 种数据: 独立池 (测试线程自己的 loop), 关掉后 app 用自己的池读
        seeded = asyncio.run(_seed_via_own_pool())
        batch_id = seeded["batch_ids"][0]

        # 1. 提交战报 (公开端点, 零登录)
        r = client.post(
            "/store/submit_battle_report_workflow",
            json={
                "user_id": seeded["customer_id"],
                "order_id": seeded["order_id"],
                "text": "又脆又甜，个头大，特别满意",
                "image_url": "https://cdn.example/http-pic.jpg",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "completed", body
        assert body["reward_granted"] == 50

        # 2. 重复提交 → 422 + already_reported (防刷闭环在 HTTP 层同样生效)
        r2 = client.post(
            "/store/submit_battle_report_workflow",
            json={
                "user_id": seeded["customer_id"],
                "order_id": seeded["order_id"],
                "text": "再评",
            },
        )
        assert r2.status_code == 422
        assert r2.json()["reason"] == "already_reported"

        # 3. 读战报聚合 (公开端点)
        r3 = client.get(f"/store/batches/{batch_id}/battle-reports")
        assert r3.status_code == 200, r3.text
        summary = r3.json()
        assert summary["report_count_total"] == 1
        assert summary["report_count_24h"] == 1
        assert summary["freshness_fit_pct"] is not None
        assert summary["recent_images"] == ["https://cdn.example/http-pic.jpg"]

        # 4. 未知批次 → 404
        import uuid as uuid_mod

        r4 = client.get(f"/store/batches/{uuid_mod.uuid4()}/battle-reports")
        assert r4.status_code == 404

    get_settings.cache_clear()


async def _seed_via_own_pool() -> dict:
    """HTTP 测试专用: 用自己的短命连接池种一笔已履约订单。"""
    from obase.persistence.pool import PgPool

    pool = await PgPool.create(
        name="hemall_phase9_http_seed", dsn=TEST_DSN, min_size=1, max_size=2
    )
    try:
        return await _seed_order(pool)
    finally:
        await pool.close()
