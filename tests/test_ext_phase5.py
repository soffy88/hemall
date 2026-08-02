"""透仓 (ClearNode) v4.0 集成测试 — 需真实 Postgres (TEST_PG_DSN)。

覆盖微信视频号社交播报引擎：omodul.execute_channel_broadcast_workflow (正向
发布/唯一索引防重发/库存耗尽拒绝/微信 API 拒绝) 和
oservi.social_broadcast_engine (上新+清仓候选扫描/优先级排序/跨 tick 防重复)。

统一改造后：物理批次/门店/供应商全部走共享表 + 本地新表，播报日志表改名为
channel_broadcast_log (单数)。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

TEST_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration"
)


@pytest.fixture
async def cn_pool():
    from obase.persistence.pool import PgPool
    from obase.provider_registry import ProviderRegistry

    from app.ext.llm_provider import ManualLLMProvider
    from app.ext.schema import ensure_ext_schema
    from app.ext.wechat_provider import ManualWeChatChannelProvider

    reg = ProviderRegistry.get()
    llm = ManualLLMProvider()
    wx = ManualWeChatChannelProvider()
    reg.register_generic("llm", "manual", llm, replace=True)
    reg.register_generic("wechat_channel", "manual", wx, replace=True)

    pool = await PgPool.create(
        name="clearnode_phase5_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await ensure_ext_schema(pool)
    pool._test_llm = llm  # type: ignore[attr-defined]
    pool._test_wx = wx  # type: ignore[attr-defined]
    yield pool
    await pool.close()


async def _make_variant(pool) -> tuple[str, str, str]:
    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, lat, lng, status) "
            "VALUES ($1,'p5_loc','cn-east','host_p5','测试车库',31.0,121.0,'active') RETURNING id",
            uuid7(),
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,'土鸡蛋p5',$2,'active') RETURNING id",
            uuid7(),
            f"p5-slug-{uuid7()}",
        )
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"p5-sku-{uuid7()}",
        )
    return str(loc_id), str(prod_id), str(var_id)


async def _make_batch(
    pool,
    *,
    var_id: str,
    loc_id: str,
    stock_qty: int = 30,
    retail_price: int = 3900,
    cost_price: int = 2000,
    supplier_id: str | None = None,
    status: str = "active",
    intake_time: datetime | None = None,
    expiration_time: datetime | None = None,
) -> str:
    from obase.uuid7 import uuid7

    batch_id = uuid7()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inventory_batch "
            "(id, batch_no, variant_id, location_id, video_url, stock_qty, reserved_qty, "
            "cost_price_cents, retail_price_cents, supplier_id, status, created_at, expiration_time, inspection_status) "
            "VALUES ($1,$2,$3,$4,$5,$6,0,$7,$8,$9,$10,COALESCE($11, NOW()),$12,'passed')",
            batch_id,
            f"p5-batch-{batch_id}",
            var_id,
            loc_id,
            "https://video.example/v.mp4",
            stock_qty,
            cost_price,
            retail_price,
            supplier_id,
            status,
            intake_time,
            expiration_time,
        )
    return str(batch_id)


async def _make_supplier(pool, *, polygon_name: str = "测试农场") -> str:
    from obase.uuid7 import uuid7

    supplier_id = uuid7()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO supplier (id, wallet_account, spatial_polygon, polygon_name) "
            "VALUES ($1,$2,$3,$4)",
            supplier_id,
            "wallet_p5",
            '{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}',
            polygon_name,
        )
    return str(supplier_id)


# ── execute_channel_broadcast_workflow ───────────────────────────────────────


@pytest.mark.asyncio
async def test_broadcast_workflow_publishes_and_records_copywriting(cn_pool, tmp_path):
    from app.ext.omodul.execute_channel_broadcast_workflow import (
        ExecuteChannelBroadcastWorkflowConfig,
        ExecuteChannelBroadcastWorkflowInput,
        execute_channel_broadcast_workflow,
    )

    supplier_id = await _make_supplier(cn_pool)
    loc_id, _, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        stock_qty=30,
        retail_price=3900,
        supplier_id=supplier_id,
    )

    result = await execute_channel_broadcast_workflow(
        ExecuteChannelBroadcastWorkflowConfig(),
        ExecuteChannelBroadcastWorkflowInput(
            batch_id=batch_id,
            broadcast_type="fresh_arrival",
            market_price=7800,
            access_token="TEST_TOKEN",
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["feed_id"]
    assert "batch_x" not in result["copywriting"]  # 不是原样抄模板，是真的拼过

    async with cn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, wechat_feed_id, mini_program_path, llm_copywriting "
            "FROM channel_broadcast_log WHERE id=$1",
            result["log_id"],
        )
    assert row["status"] == "published"
    assert row["mini_program_path"] == f"pages/checkout/direct?batch_id={batch_id}"
    assert len(row["llm_copywriting"]) > 0


@pytest.mark.asyncio
async def test_broadcast_workflow_dedup_via_unique_index(cn_pool, tmp_path):
    from app.ext.omodul.execute_channel_broadcast_workflow import (
        ExecuteChannelBroadcastWorkflowConfig,
        ExecuteChannelBroadcastWorkflowInput,
        execute_channel_broadcast_workflow,
    )

    loc_id, _, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(cn_pool, var_id=var_id, loc_id=loc_id)

    input_data = ExecuteChannelBroadcastWorkflowInput(
        batch_id=batch_id,
        broadcast_type="fresh_arrival",
        market_price=7800,
        access_token="TEST_TOKEN",
    )
    first = await execute_channel_broadcast_workflow(
        ExecuteChannelBroadcastWorkflowConfig(), input_data, tmp_path, pool=cn_pool
    )
    assert first["status"] == "completed", first

    second = await execute_channel_broadcast_workflow(
        ExecuteChannelBroadcastWorkflowConfig(), input_data, tmp_path, pool=cn_pool
    )
    assert second["status"] == "failed"
    assert "already broadcasted" in second["error"]["message"]

    # 换一个 broadcast_type，唯一索引是 (batch_id, broadcast_type) 复合键，应该能过
    clearance_input = ExecuteChannelBroadcastWorkflowInput(
        batch_id=batch_id,
        broadcast_type="clearance",
        market_price=11700,
        access_token="TEST_TOKEN",
    )
    third = await execute_channel_broadcast_workflow(
        ExecuteChannelBroadcastWorkflowConfig(), clearance_input, tmp_path, pool=cn_pool
    )
    assert third["status"] == "completed", third


@pytest.mark.asyncio
async def test_broadcast_workflow_rejects_when_stock_depleted(cn_pool, tmp_path):
    from app.ext.omodul.execute_channel_broadcast_workflow import (
        ExecuteChannelBroadcastWorkflowConfig,
        ExecuteChannelBroadcastWorkflowInput,
        execute_channel_broadcast_workflow,
    )

    loc_id, _, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(cn_pool, var_id=var_id, loc_id=loc_id, stock_qty=0)

    result = await execute_channel_broadcast_workflow(
        ExecuteChannelBroadcastWorkflowConfig(),
        ExecuteChannelBroadcastWorkflowInput(
            batch_id=batch_id,
            broadcast_type="fresh_arrival",
            market_price=7800,
            access_token="TEST_TOKEN",
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "failed"

    async with cn_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM channel_broadcast_log WHERE batch_id=$1", batch_id
        )
    assert count == 0  # 没走到 LLM/INSERT 那一步，压根没留下审计行


@pytest.mark.asyncio
async def test_broadcast_workflow_marks_log_failed_on_wechat_rejection(
    cn_pool, tmp_path
):
    from app.ext.omodul.execute_channel_broadcast_workflow import (
        ExecuteChannelBroadcastWorkflowConfig,
        ExecuteChannelBroadcastWorkflowInput,
        execute_channel_broadcast_workflow,
    )

    loc_id, _, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(cn_pool, var_id=var_id, loc_id=loc_id)
    mp_path = f"pages/checkout/direct?batch_id={batch_id}"
    cn_pool._test_wx.set_response(
        mp_path=mp_path, response={"errcode": 61023, "errmsg": "risk control rejected"}
    )

    result = await execute_channel_broadcast_workflow(
        ExecuteChannelBroadcastWorkflowConfig(),
        ExecuteChannelBroadcastWorkflowInput(
            batch_id=batch_id,
            broadcast_type="fresh_arrival",
            market_price=7800,
            access_token="TEST_TOKEN",
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "failed"
    assert "wechat api rejected" in result["error"]["message"]

    async with cn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, error_message FROM channel_broadcast_log WHERE batch_id=$1",
            batch_id,
        )
    assert row["status"] == "failed"
    assert "61023" in row["error_message"]


# ── social_broadcast_engine ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_social_broadcast_engine_publishes_fresh_and_clearance_with_priority(
    cn_pool, tmp_path
):
    from app.ext.oservi import build_social_broadcast_engine
    from app.config import Settings

    loc_id, _, var_id = await _make_variant(cn_pool)
    now = datetime.now(UTC)

    fresh_batch_id = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        stock_qty=25,
        intake_time=now - timedelta(minutes=30),
    )
    urgent_batch_id = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        stock_qty=50,
        intake_time=now - timedelta(days=1),
        expiration_time=now + timedelta(hours=1),
    )
    relaxed_batch_id = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        stock_qty=10,
        intake_time=now - timedelta(days=1),
        expiration_time=now + timedelta(hours=10),
    )

    engine = build_social_broadcast_engine(
        cn_pool, settings=Settings(output_root=tmp_path), stagger_seconds=0
    )
    results = await engine.run_once()
    summary = results[0]

    published = {(p["batch_id"], p["broadcast_type"]) for p in summary["published"]}
    assert (fresh_batch_id, "fresh_arrival") in published
    assert (urgent_batch_id, "clearance") in published
    assert (relaxed_batch_id, "clearance") in published

    # 优先级排序：urgent (1 小时后过期) 应该排在 relaxed (10 小时后过期) 前面
    clearance_order = [
        p["batch_id"]
        for p in summary["published"]
        if p["broadcast_type"] == "clearance"
    ]
    assert clearance_order.index(urgent_batch_id) < clearance_order.index(
        relaxed_batch_id
    )

    # 第二次 tick：三个批次都已经播报过，不应该再出现在 published 里
    results2 = await engine.run_once()
    summary2 = results2[0]
    published2 = {(p["batch_id"], p["broadcast_type"]) for p in summary2["published"]}
    assert (fresh_batch_id, "fresh_arrival") not in published2
    assert (urgent_batch_id, "clearance") not in published2
    assert (relaxed_batch_id, "clearance") not in published2


@pytest.mark.asyncio
async def test_social_broadcast_engine_ignores_low_stock_and_old_batches(
    cn_pool, tmp_path
):
    from app.ext.oservi import build_social_broadcast_engine
    from app.config import Settings

    loc_id, _, var_id = await _make_variant(cn_pool)
    now = datetime.now(UTC)

    # stock_qty 太低 (<=20)，不该被当成 fresh_arrival 候选
    low_stock_batch_id = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        stock_qty=5,
        intake_time=now - timedelta(minutes=10),
    )
    # 入库太久 (>1 小时)，不该被当成 fresh_arrival 候选
    old_batch_id = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        stock_qty=100,
        intake_time=now - timedelta(hours=5),
    )

    engine = build_social_broadcast_engine(
        cn_pool, settings=Settings(output_root=tmp_path), stagger_seconds=0
    )
    results = await engine.run_once()
    published = {(p["batch_id"], p["broadcast_type"]) for p in results[0]["published"]}
    assert (low_stock_batch_id, "fresh_arrival") not in published
    assert (old_batch_id, "fresh_arrival") not in published
