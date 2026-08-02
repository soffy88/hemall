"""hemall 扩展域 v6.0 集成测试 — 需真实 Postgres (TEST_PG_DSN)。

覆盖抖音拓客与数字领主：omodul.process_cloud_franchise_claim_workflow (云
加盟空间冲突检测) 、omodul.bind_digital_lord_contract_workflow (领主契约
册封/点火/排他) 、omodul.record_douyin_conversion_workflow (领主税+雇佣兵
悬赏双路径落账) 和 oservi.build_mercenary_routing_engine /
build_affiliate_settlement_engine (推流+夜间清算)。

统一改造后：地点/商品/批次/订单全部走共享表，douyin_uid 不再是任何本地
生成的 ID，只是一个任意的外部标识字符串；affiliate_contract/
douyin_conversion_log 的 id 是真 UUID。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

TEST_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration"
)


@pytest.fixture
async def cn_pool():
    from obase.payment_providers import ManualPaymentProvider
    from obase.persistence.pool import PgPool
    from obase.provider_registry import ProviderRegistry

    from app.ext.douyin_provider import ManualDouyinProvider
    from app.ext.payout_provider import ManualPayoutProvider
    from app.ext.schema import ensure_ext_schema

    reg = ProviderRegistry.get()
    reg.register_generic("payment", "manual", ManualPaymentProvider(), replace=True)
    reg.register_generic("douyin", "manual", ManualDouyinProvider(), replace=True)
    reg.register_generic("payout", "manual", ManualPayoutProvider(), replace=True)

    pool = await PgPool.create(
        name="hemall_phase7_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await ensure_ext_schema(pool)
    yield pool
    await pool.close()


def _random_coords() -> tuple[float, float]:
    import random

    # 共享的真实 DB 里到处都是历史测试残留的 stock_location，很多都扎堆在
    # (31.0, 121.0)/(31.5, 121.5) 这类"看起来像上海"的常见测试坐标——每个
    # 测试用随机坐标起跳，避免误撞进别的测试留下的 1 公里冲突圈。
    return random.uniform(-60.0, 60.0), random.uniform(-170.0, 170.0)


async def _dy_uid(prefix: str) -> str:
    from obase.uuid7 import uuid7

    return f"{prefix}_{uuid7()}"


async def _make_variant(pool, *, product_name: str, lat: float, lon: float):
    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, address, lat, lng, status) "
            "VALUES ($1,'p7_loc','cn-east','测试车库',$2,$3,'active') RETURNING id",
            uuid7(),
            lat,
            lon,
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            product_name,
            f"p7-slug-{uuid7()}",
        )
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"p7-sku-{uuid7()}",
        )
    return str(loc_id), str(prod_id), str(var_id)


async def _make_batch(
    pool,
    *,
    var_id: str,
    loc_id: str,
    stock_qty: int = 10,
    retail_price: int = 3900,
    cost_price: int = 2000,
    expiration_time=None,
) -> str:
    from obase.uuid7 import uuid7

    batch_id = uuid7()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inventory_batch "
            "(id, batch_no, variant_id, location_id, video_url, stock_qty, reserved_qty, "
            "cost_price_cents, retail_price_cents, status, inspection_status, expiration_time) "
            "VALUES ($1,$2,$3,$4,$5,$6,0,$7,$8,'active','passed',$9)",
            batch_id,
            f"p7-batch-{batch_id}",
            var_id,
            loc_id,
            "https://video.example/v.mp4",
            stock_qty,
            cost_price,
            retail_price,
            expiration_time,
        )
    return str(batch_id)


async def _make_order(pool, *, batch_id: str, qty: int = 1) -> str:
    from omodul.add_line_item_to_cart import (
        AddLineItemConfig,
        AddLineItemInput,
        add_line_item_to_cart,
    )
    from omodul.authorize_payment_for_cart import (
        AuthorizePaymentForCartConfig,
        AuthorizePaymentForCartInput,
        authorize_payment_for_cart,
    )
    from omodul.complete_checkout import (
        CompleteCheckoutConfig,
        CompleteCheckoutInput,
        complete_checkout,
    )
    from omodul.create_cart import CreateCartConfig, CreateCartInput, create_cart
    from omodul.create_payment_sessions import (
        CreatePaymentSessionsConfig,
        CreatePaymentSessionsInput,
        create_payment_sessions,
    )
    from omodul.set_payment_session import (
        SetPaymentSessionConfig,
        SetPaymentSessionInput,
        set_payment_session,
    )

    cart_result = await create_cart(
        CreateCartConfig(), CreateCartInput(), Path("/tmp"), pool=pool
    )
    assert cart_result["status"] == "completed", cart_result
    cart_id = cart_result["cart_id"]

    add_result = await add_line_item_to_cart(
        AddLineItemConfig(),
        AddLineItemInput(cart_id=cart_id, batch_id=batch_id, quantity=qty),
        Path("/tmp"),
        pool=pool,
    )
    assert add_result["status"] == "completed", add_result

    sessions_result = await create_payment_sessions(
        CreatePaymentSessionsConfig(),
        CreatePaymentSessionsInput(cart_id=cart_id, provider_names=["manual"]),
        Path("/tmp"),
        pool=pool,
    )
    assert sessions_result["status"] == "completed", sessions_result

    set_result = await set_payment_session(
        SetPaymentSessionConfig(),
        SetPaymentSessionInput(cart_id=cart_id, provider_name="manual"),
        Path("/tmp"),
        pool=pool,
    )
    assert set_result["status"] == "completed", set_result

    authorize_result = await authorize_payment_for_cart(
        AuthorizePaymentForCartConfig(),
        AuthorizePaymentForCartInput(cart_id=cart_id),
        Path("/tmp"),
        pool=pool,
    )
    assert authorize_result["status"] == "completed", authorize_result

    checkout_result = await complete_checkout(
        CompleteCheckoutConfig(),
        CompleteCheckoutInput(cart_id=cart_id),
        Path("/tmp"),
        pool=pool,
    )
    assert checkout_result["status"] == "completed", checkout_result
    return str(checkout_result["order_id"])


# ── process_cloud_franchise_claim_workflow ───────────────────────────────


@pytest.mark.asyncio
async def test_franchise_claim_approves_when_no_conflict(cn_pool, tmp_path):
    from app.ext.omodul.process_cloud_franchise_claim_workflow import (
        ProcessCloudFranchiseClaimWorkflowConfig,
        ProcessCloudFranchiseClaimWorkflowInput,
        process_cloud_franchise_claim_workflow,
    )

    lat, lon = _random_coords()
    douyin_uid = await _dy_uid("dyuid")

    result = await process_cloud_franchise_claim_workflow(
        ProcessCloudFranchiseClaimWorkflowConfig(),
        ProcessCloudFranchiseClaimWorkflowInput(
            douyin_uid=douyin_uid, address="测试地址", lat=lat, lon=lon
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed"
    assert result["decision"] == "approved"
    assert result["location_id"]

    async with cn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, douyin_host_uid, host_id FROM stock_location WHERE id=$1",
            result["location_id"],
        )
    assert row["status"] == "pending_hardware"
    assert row["douyin_host_uid"] == douyin_uid
    assert row["host_id"] is None


@pytest.mark.asyncio
async def test_franchise_claim_rejects_spatial_conflict(cn_pool, tmp_path):
    from app.ext.omodul.process_cloud_franchise_claim_workflow import (
        ProcessCloudFranchiseClaimWorkflowConfig,
        ProcessCloudFranchiseClaimWorkflowInput,
        process_cloud_franchise_claim_workflow,
    )

    lat, lon = _random_coords()
    first = await process_cloud_franchise_claim_workflow(
        ProcessCloudFranchiseClaimWorkflowConfig(),
        ProcessCloudFranchiseClaimWorkflowInput(
            douyin_uid=await _dy_uid("dyuid"), address="a", lat=lat, lon=lon
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert first["decision"] == "approved"

    # 500 米内，应判冲突
    second = await process_cloud_franchise_claim_workflow(
        ProcessCloudFranchiseClaimWorkflowConfig(),
        ProcessCloudFranchiseClaimWorkflowInput(
            douyin_uid=await _dy_uid("dyuid"),
            address="b",
            lat=lat + 0.003,
            lon=lon + 0.003,
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert second["status"] == "completed"
    assert second["decision"] == "rejected"
    assert second["reason"] == "spatial_conflict"


# ── bind_digital_lord_contract_workflow ──────────────────────────────────


@pytest.mark.asyncio
async def test_bind_lord_ignites_pending_node(cn_pool, tmp_path):
    from app.ext.omodul.bind_digital_lord_contract_workflow import (
        BindDigitalLordContractWorkflowConfig,
        BindDigitalLordContractWorkflowInput,
        bind_digital_lord_contract_workflow,
    )
    from app.ext.omodul.process_cloud_franchise_claim_workflow import (
        ProcessCloudFranchiseClaimWorkflowConfig,
        ProcessCloudFranchiseClaimWorkflowInput,
        process_cloud_franchise_claim_workflow,
    )

    lat, lon = _random_coords()
    douyin_uid = await _dy_uid("dyuid")
    claim = await process_cloud_franchise_claim_workflow(
        ProcessCloudFranchiseClaimWorkflowConfig(),
        ProcessCloudFranchiseClaimWorkflowInput(
            douyin_uid=douyin_uid, address="a", lat=lat, lon=lon
        ),
        tmp_path,
        pool=cn_pool,
    )
    location_id = claim["location_id"]

    result = await bind_digital_lord_contract_workflow(
        BindDigitalLordContractWorkflowConfig(),
        BindDigitalLordContractWorkflowInput(
            douyin_uid=douyin_uid, location_id=location_id
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed"
    assert result["action"] == "bound"

    async with cn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM stock_location WHERE id=$1", location_id
        )
    assert row["status"] == "active"


@pytest.mark.asyncio
async def test_bind_lord_idempotent_same_uid(cn_pool, tmp_path):
    from app.ext.omodul.bind_digital_lord_contract_workflow import (
        BindDigitalLordContractWorkflowConfig,
        BindDigitalLordContractWorkflowInput,
        bind_digital_lord_contract_workflow,
    )
    from app.ext.omodul.process_cloud_franchise_claim_workflow import (
        ProcessCloudFranchiseClaimWorkflowConfig,
        ProcessCloudFranchiseClaimWorkflowInput,
        process_cloud_franchise_claim_workflow,
    )

    lat, lon = _random_coords()
    douyin_uid = await _dy_uid("dyuid")
    claim = await process_cloud_franchise_claim_workflow(
        ProcessCloudFranchiseClaimWorkflowConfig(),
        ProcessCloudFranchiseClaimWorkflowInput(
            douyin_uid=douyin_uid, address="a", lat=lat, lon=lon
        ),
        tmp_path,
        pool=cn_pool,
    )
    location_id = claim["location_id"]
    config = BindDigitalLordContractWorkflowConfig()
    first = await bind_digital_lord_contract_workflow(
        config,
        BindDigitalLordContractWorkflowInput(
            douyin_uid=douyin_uid, location_id=location_id
        ),
        tmp_path,
        pool=cn_pool,
    )
    second = await bind_digital_lord_contract_workflow(
        config,
        BindDigitalLordContractWorkflowInput(
            douyin_uid=douyin_uid, location_id=location_id
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert second["status"] == "completed"
    assert second["action"] == "already_lord"
    assert second["contract_id"] == first["contract_id"]


@pytest.mark.asyncio
async def test_bind_lord_rejects_conflicting_claimant(cn_pool, tmp_path):
    from app.ext.omodul.bind_digital_lord_contract_workflow import (
        BindDigitalLordContractWorkflowConfig,
        BindDigitalLordContractWorkflowInput,
        bind_digital_lord_contract_workflow,
    )
    from app.ext.omodul.process_cloud_franchise_claim_workflow import (
        ProcessCloudFranchiseClaimWorkflowConfig,
        ProcessCloudFranchiseClaimWorkflowInput,
        process_cloud_franchise_claim_workflow,
    )

    lat, lon = _random_coords()
    claim = await process_cloud_franchise_claim_workflow(
        ProcessCloudFranchiseClaimWorkflowConfig(),
        ProcessCloudFranchiseClaimWorkflowInput(
            douyin_uid=await _dy_uid("dyuid"), address="a", lat=lat, lon=lon
        ),
        tmp_path,
        pool=cn_pool,
    )
    location_id = claim["location_id"]

    await bind_digital_lord_contract_workflow(
        BindDigitalLordContractWorkflowConfig(),
        BindDigitalLordContractWorkflowInput(
            douyin_uid=await _dy_uid("dyuid"), location_id=location_id
        ),
        tmp_path,
        pool=cn_pool,
    )

    challenger = await bind_digital_lord_contract_workflow(
        BindDigitalLordContractWorkflowConfig(),
        BindDigitalLordContractWorkflowInput(
            douyin_uid=await _dy_uid("dyuid"), location_id=location_id
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert challenger["status"] == "failed"


# ── record_douyin_conversion_workflow ────────────────────────────────────


@pytest.mark.asyncio
async def test_conversion_lord_tax_applies_without_referral(cn_pool, tmp_path):
    from app.ext.omodul.bind_digital_lord_contract_workflow import (
        BindDigitalLordContractWorkflowConfig,
        BindDigitalLordContractWorkflowInput,
        bind_digital_lord_contract_workflow,
    )
    from app.ext.omodul.record_douyin_conversion_workflow import (
        RecordDouyinConversionWorkflowConfig,
        RecordDouyinConversionWorkflowInput,
        record_douyin_conversion_workflow,
    )
    from obase.uuid7 import uuid7

    lat, lon = _random_coords()
    product_name = f"p7_{uuid7()}"
    loc_id, _, var_id = await _make_variant(
        cn_pool, product_name=product_name, lat=lat, lon=lon
    )
    lord_uid = await _dy_uid("dyuid")
    await bind_digital_lord_contract_workflow(
        BindDigitalLordContractWorkflowConfig(),
        BindDigitalLordContractWorkflowInput(douyin_uid=lord_uid, location_id=loc_id),
        tmp_path,
        pool=cn_pool,
    )

    batch_id = await _make_batch(
        cn_pool, var_id=var_id, loc_id=loc_id, retail_price=10000
    )
    order_id = await _make_order(cn_pool, batch_id=batch_id, qty=1)

    result = await record_douyin_conversion_workflow(
        RecordDouyinConversionWorkflowConfig(),
        RecordDouyinConversionWorkflowInput(order_id=order_id),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed"
    assert len(result["conversions"]) == 1
    conv = result["conversions"][0]
    assert conv["type"] == "digital_lord"
    assert conv["douyin_uid"] == lord_uid
    assert conv["dividend_amount"] == int(10000 * 0.002)


@pytest.mark.asyncio
async def test_conversion_mercenary_bounty_requires_referral_and_urgency(
    cn_pool, tmp_path
):
    from app.ext.omodul.record_douyin_conversion_workflow import (
        RecordDouyinConversionWorkflowConfig,
        RecordDouyinConversionWorkflowInput,
        record_douyin_conversion_workflow,
    )
    from obase.uuid7 import uuid7

    lat, lon = _random_coords()
    product_name = f"p7_{uuid7()}"
    loc_id, _, var_id = await _make_variant(
        cn_pool, product_name=product_name, lat=lat, lon=lon
    )
    # 批次 2 小时后过期 -> 恐慌区间 -> 80% 悬赏
    batch_id = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        retail_price=10000,
        expiration_time=datetime.now(UTC) + timedelta(hours=2),
    )
    order_id = await _make_order(cn_pool, batch_id=batch_id, qty=1)

    # 没有 douyin_uid -> 该节点没有领主也没有推荐人，不应产生任何转化
    no_referral = await record_douyin_conversion_workflow(
        RecordDouyinConversionWorkflowConfig(),
        RecordDouyinConversionWorkflowInput(order_id=order_id),
        tmp_path,
        pool=cn_pool,
    )
    assert no_referral["status"] == "completed"
    assert no_referral["conversions"] == []
    assert no_referral["total_dividend_cents"] == 0

    referrer_uid = await _dy_uid("dyref")
    order_id2 = await _make_order(cn_pool, batch_id=batch_id, qty=1)
    with_referral = await record_douyin_conversion_workflow(
        RecordDouyinConversionWorkflowConfig(),
        RecordDouyinConversionWorkflowInput(
            order_id=order_id2, douyin_uid=referrer_uid
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert with_referral["status"] == "completed"
    assert len(with_referral["conversions"]) == 1
    conv = with_referral["conversions"][0]
    assert conv["type"] == "mercenary"
    assert conv["douyin_uid"] == referrer_uid
    assert conv["bounty_rate"] == 0.80
    assert conv["dividend_amount"] == round(10000 * 0.80)

    async with cn_pool.acquire() as conn:
        contract = await conn.fetchrow(
            "SELECT douyin_uid, contract_type, bound_entity_id FROM affiliate_contract "
            "WHERE contract_type='mercenary' AND bound_entity_id=$1",
            batch_id,
        )
    assert contract["douyin_uid"] == referrer_uid


@pytest.mark.asyncio
async def test_conversion_mercenary_bounty_zero_when_batch_far_from_expiry(
    cn_pool, tmp_path
):
    from app.ext.omodul.record_douyin_conversion_workflow import (
        RecordDouyinConversionWorkflowConfig,
        RecordDouyinConversionWorkflowInput,
        record_douyin_conversion_workflow,
    )
    from obase.uuid7 import uuid7

    lat, lon = _random_coords()
    product_name = f"p7_{uuid7()}"
    loc_id, _, var_id = await _make_variant(
        cn_pool, product_name=product_name, lat=lat, lon=lon
    )
    # 30 天后才过期 -> 安全区间 -> 0% 悬赏，即使带了推荐人也不产生转化
    batch_id = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        expiration_time=datetime.now(UTC) + timedelta(days=30),
    )
    order_id = await _make_order(cn_pool, batch_id=batch_id, qty=1)

    result = await record_douyin_conversion_workflow(
        RecordDouyinConversionWorkflowConfig(),
        RecordDouyinConversionWorkflowInput(
            order_id=order_id, douyin_uid=await _dy_uid("dyref")
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed"
    assert result["conversions"] == []


# ── oservi engines ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mercenary_routing_engine_pushes_danger_batches(cn_pool, tmp_path):
    from app.ext.oservi import build_mercenary_routing_engine
    from obase.uuid7 import uuid7

    lat, lon = _random_coords()
    product_name = f"p7_{uuid7()}"
    loc_id, _, var_id = await _make_variant(
        cn_pool, product_name=product_name, lat=lat, lon=lon
    )
    danger_batch = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        retail_price=5000,
        expiration_time=datetime.now(UTC) + timedelta(hours=3),
    )
    safe_batch = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        retail_price=5000,
        expiration_time=datetime.now(UTC) + timedelta(days=10),
    )

    engine = build_mercenary_routing_engine(cn_pool)
    results = await engine.run_once()
    summary = results[0]

    pushed_ids = {p["batch_id"] for p in summary["pushed"]}
    assert danger_batch in pushed_ids
    assert safe_batch not in pushed_ids


@pytest.mark.asyncio
async def test_affiliate_settlement_engine_settles_above_threshold_only(
    cn_pool, tmp_path
):
    from app.ext.oservi import build_affiliate_settlement_engine
    from obase.uuid7 import uuid7

    rich_uid = await _dy_uid("dyrich")
    poor_uid = await _dy_uid("dypoor")
    order_id = uuid7()
    async with cn_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO customer_order (id, status) VALUES ($1,'paid')",
            order_id,
        )
        await conn.execute(
            "INSERT INTO douyin_conversion_log (id, order_id, douyin_uid, dividend_amount_cents) "
            "VALUES ($1,$2,$3,$4)",
            uuid7(),
            order_id,
            rich_uid,
            500,
        )
        await conn.execute(
            "INSERT INTO douyin_conversion_log (id, order_id, douyin_uid, dividend_amount_cents) "
            "VALUES ($1,$2,$3,$4)",
            uuid7(),
            order_id,
            poor_uid,
            50,
        )

    engine = build_affiliate_settlement_engine(cn_pool)
    results = await engine.run_once()
    summary = results[0]

    settled_uids = {s["douyin_uid"] for s in summary["settled"]}
    assert rich_uid in settled_uids
    assert poor_uid not in settled_uids

    async with cn_pool.acquire() as conn:
        rich_status = await conn.fetchval(
            "SELECT settlement_status FROM douyin_conversion_log WHERE douyin_uid=$1",
            rich_uid,
        )
        poor_status = await conn.fetchval(
            "SELECT settlement_status FROM douyin_conversion_log WHERE douyin_uid=$1",
            poor_uid,
        )
    assert rich_status == "settled"
    assert poor_status == "pending"
