"""hemall 扩展域 v2.0 第二轮集成测试 — 需真实 Postgres (TEST_PG_DSN)。

覆盖 SPEC v2.0 §4 剩余 7 个 omodul (claim_origin_workflow /
execute_slashing_workflow / report_phantom_stock_workflow /
execute_ambient_intake_workflow / execute_peer_delivery_workflow /
process_credit_gated_rma_workflow / generate_crushing_offer_workflow) 和
§5 剩余 3 个 oservi 引擎 (market_maker_engine / tote_balancing_engine /
spatial_fomo_engine)。

统一改造后：物理批次/门店/购物车/订单/供应商全部走共享表 + 本地新表。
"""

from __future__ import annotations

import json
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
    from obase.notification_providers import LogNotificationProvider
    from obase.payment_providers import ManualPaymentProvider
    from obase.persistence.pool import PgPool
    from obase.provider_registry import ProviderRegistry

    from app.ext.cv_provider import ManualCVProvider
    from app.ext.payout_provider import ManualPayoutProvider
    from app.ext.schema import ensure_ext_schema

    reg = ProviderRegistry.get()
    reg.register_generic("payment", "manual", ManualPaymentProvider(), replace=True)
    reg.register_generic("payout", "manual", ManualPayoutProvider(), replace=True)
    reg.register_generic("notification", "log", LogNotificationProvider(), replace=True)
    cv = ManualCVProvider()
    reg.register_generic("cv", "manual", cv, replace=True)

    pool = await PgPool.create(
        name="hemall_phase4_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await ensure_ext_schema(pool)
    pool._test_cv = cv  # type: ignore[attr-defined]
    yield pool
    await pool.close()


async def _make_variant(
    pool, *, lat: float = 31.0, lon: float = 121.0
) -> tuple[str, str, str]:
    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, lat, lng, status) "
            "VALUES ($1,'p4_loc','cn-east','host_p4','测试车库',$2,$3,'active') RETURNING id",
            uuid7(),
            lat,
            lon,
        )
        # 商品名用不含常见词根的唯一 token (不能只是给 "土鸡蛋" 加后缀——
        # generate_crushing_offer_workflow 用双向子串匹配，"土鸡蛋" 本身仍会
        # 命中共享真实 DB 里历史测试留下的同名旧商品，选中错误的批次)。
        unique_tag = uuid7()
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            f"eggsku{unique_tag}",
            f"p4-slug-{unique_tag}",
        )
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"p4-sku-{unique_tag}",
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
    supplier_id: str | None = None,
    status: str = "active",
    intake_time: datetime | None = None,
    vwap_target_curve: dict | None = None,
) -> str:
    from obase.uuid7 import uuid7

    batch_id = uuid7()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inventory_batch "
            "(id, batch_no, variant_id, location_id, video_url, stock_qty, reserved_qty, "
            "cost_price_cents, retail_price_cents, supplier_id, status, created_at, vwap_target_curve, inspection_status) "
            "VALUES ($1,$2,$3,$4,$5,$6,0,$7,$8,$9,$10,COALESCE($11, NOW()),$12,'passed')",
            batch_id,
            f"p4-batch-{batch_id}",
            var_id,
            loc_id,
            "https://video.example/v.mp4",
            stock_qty,
            cost_price,
            retail_price,
            supplier_id,
            status,
            intake_time,
            json.dumps(vwap_target_curve) if vwap_target_curve else None,
        )
    return str(batch_id)


async def _make_order(pool, *, batch_id: str, qty: int = 1) -> tuple[str, str]:
    """建车+加车+授权支付+结账 (共享 omodul 链路)，拿到 (order_id, order_line_item_id)。"""
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
    order_id = str(checkout_result["order_id"])

    async with pool.acquire() as conn:
        oli_id = await conn.fetchval(
            "SELECT id FROM order_line_item WHERE order_id=$1", order_id
        )
    return order_id, str(oli_id)


async def _make_supplier(pool, *, escrow_balance: int = 100_000) -> str:
    from obase.uuid7 import uuid7

    supplier_id = uuid7()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO supplier (id, wallet_account, spatial_polygon, escrow_balance) "
            "VALUES ($1,$2,$3,$4)",
            supplier_id,
            "wallet_p4",
            '{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}',
            escrow_balance,
        )
    return str(supplier_id)


async def _make_customer(pool) -> str:
    from obase.uuid7 import uuid7

    customer_id = uuid7()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO customer (id, email, status) VALUES ($1,$2,'active')",
            customer_id,
            f"p4-{customer_id}@example.com",
        )
    return str(customer_id)


# ── claim_origin_workflow ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_origin_workflow_registers_supplier(cn_pool, tmp_path):
    from app.ext.omodul.claim_origin_workflow import (
        ClaimOriginWorkflowConfig,
        ClaimOriginWorkflowInput,
        claim_origin_workflow,
    )

    result = await claim_origin_workflow(
        ClaimOriginWorkflowConfig(),
        ClaimOriginWorkflowInput(
            wallet_account="wallet_claim_test",
            spatial_polygon={
                "type": "Polygon",
                "coordinates": [
                    [[121.0, 31.0], [121.1, 31.0], [121.1, 31.1], [121.0, 31.0]]
                ],
            },
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["status_value"] == "sandbox"

    async with cn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT trust_score, escrow_balance, status FROM supplier WHERE id=$1",
            result["supplier_id"],
        )
    assert row["trust_score"] == 100
    assert row["escrow_balance"] == 0
    assert row["status"] == "sandbox"


@pytest.mark.asyncio
async def test_claim_origin_workflow_rejects_bad_polygon(cn_pool, tmp_path):
    from app.ext.omodul.claim_origin_workflow import (
        ClaimOriginWorkflowConfig,
        ClaimOriginWorkflowInput,
        claim_origin_workflow,
    )

    result = await claim_origin_workflow(
        ClaimOriginWorkflowConfig(),
        ClaimOriginWorkflowInput(
            wallet_account="wallet_bad",
            spatial_polygon={"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]},
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "failed"


# ── execute_slashing_workflow ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_slashing_workflow_debits_and_freezes_below_red_line(
    cn_pool, tmp_path
):
    from app.ext.omodul.execute_slashing_workflow import (
        ExecuteSlashingWorkflowConfig,
        ExecuteSlashingWorkflowInput,
        execute_slashing_workflow,
    )

    supplier_id = await _make_supplier(cn_pool, escrow_balance=100_000)
    loc_id, _, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(
        cn_pool, var_id=var_id, loc_id=loc_id, supplier_id=supplier_id
    )
    order_id, _ = await _make_order(cn_pool, batch_id=batch_id)

    result = await execute_slashing_workflow(
        ExecuteSlashingWorkflowConfig(),
        ExecuteSlashingWorkflowInput(
            supplier_id=supplier_id,
            order_id=order_id,
            penalty_base_amount=1000,
            new_trust_score=40,
            reason="repeat_violation",
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["penalty_amount"] == 3000
    assert result["refund_amount"] == min(3000, 3900)
    assert result["frozen"] is True

    async with cn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT escrow_balance, status, trust_score FROM supplier WHERE id=$1",
            supplier_id,
        )
    assert row["escrow_balance"] == 100_000 - 3000
    assert row["status"] == "slashed"
    assert row["trust_score"] == 40


@pytest.mark.asyncio
async def test_execute_slashing_workflow_not_frozen_above_red_line(cn_pool, tmp_path):
    from app.ext.omodul.execute_slashing_workflow import (
        ExecuteSlashingWorkflowConfig,
        ExecuteSlashingWorkflowInput,
        execute_slashing_workflow,
    )

    supplier_id = await _make_supplier(cn_pool, escrow_balance=100_000)
    loc_id, _, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(
        cn_pool, var_id=var_id, loc_id=loc_id, supplier_id=supplier_id
    )
    order_id, _ = await _make_order(cn_pool, batch_id=batch_id)

    result = await execute_slashing_workflow(
        ExecuteSlashingWorkflowConfig(),
        ExecuteSlashingWorkflowInput(
            supplier_id=supplier_id,
            order_id=order_id,
            penalty_base_amount=500,
            new_trust_score=75,
            reason="minor_violation",
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["frozen"] is False

    async with cn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM supplier WHERE id=$1", supplier_id
        )
    assert row["status"] == "sandbox"


# ── report_phantom_stock_workflow ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_phantom_stock_workflow_zeroes_stock_and_refunds(
    cn_pool, tmp_path
):
    from app.ext.omodul.report_phantom_stock_workflow import (
        ReportPhantomStockWorkflowConfig,
        ReportPhantomStockWorkflowInput,
        report_phantom_stock_workflow,
    )

    loc_id, _, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(
        cn_pool, var_id=var_id, loc_id=loc_id, stock_qty=10, cost_price=2000
    )
    order_id, oli_id = await _make_order(cn_pool, batch_id=batch_id, qty=1)

    result = await report_phantom_stock_workflow(
        ReportPhantomStockWorkflowConfig(),
        ReportPhantomStockWorkflowInput(
            order_line_item_id=oli_id, worker_id="worker_p4"
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["refund_amount"] == 3900
    assert result["loss_amount"] == 2000
    assert result["location_id"] == loc_id

    async with cn_pool.acquire() as conn:
        batch_row = await conn.fetchrow(
            "SELECT stock_qty, reserved_qty FROM inventory_batch WHERE id=$1", batch_id
        )
        loss_row = await conn.fetchrow(
            "SELECT amount_cents, reason FROM stock_location_loss_ledger WHERE batch_id=$1",
            batch_id,
        )
    assert batch_row["stock_qty"] == 0
    assert batch_row["reserved_qty"] == 0
    assert loss_row["amount_cents"] == 2000
    assert loss_row["reason"] == "phantom_stock"


# ── execute_ambient_intake_workflow ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_ambient_intake_workflow_activates_pending_batch(
    cn_pool, tmp_path
):
    from app.ext.omodul.execute_ambient_intake_workflow import (
        ExecuteAmbientIntakeWorkflowConfig,
        ExecuteAmbientIntakeWorkflowInput,
        execute_ambient_intake_workflow,
    )

    loc_id, _, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(
        cn_pool, var_id=var_id, loc_id=loc_id, status="pending"
    )

    video_bytes = b"phase4-fake-video"
    cn_pool._test_cv.set_intake_result(
        video_stream=video_bytes,
        batch_id=batch_id,
        shelf_slot="B-7",
        snapshot_url="s3://phase4-snap.jpg",
    )

    result = await execute_ambient_intake_workflow(
        ExecuteAmbientIntakeWorkflowConfig(),
        ExecuteAmbientIntakeWorkflowInput(video_stream=video_bytes),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["batch_id"] == batch_id
    assert result["shelf_slot"] == "B-7"

    async with cn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, shelf_image_url, shelf_slot FROM inventory_batch WHERE id=$1",
            batch_id,
        )
    assert row["status"] == "active"
    assert row["shelf_image_url"] == "s3://phase4-snap.jpg"
    assert row["shelf_slot"] == "B-7"


@pytest.mark.asyncio
async def test_execute_ambient_intake_workflow_fails_when_cv_cannot_identify(
    cn_pool, tmp_path
):
    from app.ext.omodul.execute_ambient_intake_workflow import (
        ExecuteAmbientIntakeWorkflowConfig,
        ExecuteAmbientIntakeWorkflowInput,
        execute_ambient_intake_workflow,
    )

    result = await execute_ambient_intake_workflow(
        ExecuteAmbientIntakeWorkflowConfig(),
        ExecuteAmbientIntakeWorkflowInput(video_stream=b"unrecognized-noise"),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "failed"


# ── execute_peer_delivery_workflow ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_peer_delivery_workflow_releases_tote_and_pays_neighbor(
    cn_pool, tmp_path
):
    from obase.uuid7 import uuid7

    from app.ext.omodul.execute_peer_delivery_workflow import (
        ExecutePeerDeliveryWorkflowConfig,
        ExecutePeerDeliveryWorkflowInput,
        execute_peer_delivery_workflow,
    )

    loc_id, _, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(cn_pool, var_id=var_id, loc_id=loc_id)
    order_id, _ = await _make_order(cn_pool, batch_id=batch_id)

    tote_id = uuid7()
    async with cn_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tote (id, current_location_id, status) VALUES ($1,$2,'packed')",
            tote_id,
            loc_id,
        )

    result = await execute_peer_delivery_workflow(
        ExecutePeerDeliveryWorkflowConfig(),
        ExecutePeerDeliveryWorkflowInput(
            order_id=order_id,
            tote_id=tote_id,
            neighbor_id="neighbor_p4",
            bounty_amount=300,
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["bounty_amount"] == 300

    async with cn_pool.acquire() as conn:
        tote_row = await conn.fetchrow("SELECT status FROM tote WHERE id=$1", tote_id)
    assert tote_row["status"] == "in_transit"


# ── process_credit_gated_rma_workflow ────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_credit_gated_rma_workflow_instant_refunds_high_trust(
    cn_pool, tmp_path
):
    from app.ext.omodul.process_credit_gated_rma_workflow import (
        ProcessCreditGatedRmaWorkflowConfig,
        ProcessCreditGatedRmaWorkflowInput,
        process_credit_gated_rma_workflow,
    )

    loc_id, _, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(cn_pool, var_id=var_id, loc_id=loc_id)
    order_id, _ = await _make_order(cn_pool, batch_id=batch_id)
    user_id = await _make_customer(cn_pool)

    result = await process_credit_gated_rma_workflow(
        ProcessCreditGatedRmaWorkflowConfig(),
        ProcessCreditGatedRmaWorkflowInput(
            order_id=order_id,
            batch_id=batch_id,
            user_id=user_id,
            user_trust_score=100,
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["decision"] == "instant_refund"
    assert result["refund_amount"] == 3900


@pytest.mark.asyncio
async def test_process_credit_gated_rma_workflow_honeypot_low_trust(cn_pool, tmp_path):
    from app.ext.omodul.process_credit_gated_rma_workflow import (
        ProcessCreditGatedRmaWorkflowConfig,
        ProcessCreditGatedRmaWorkflowInput,
        process_credit_gated_rma_workflow,
    )

    loc_id, _, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(cn_pool, var_id=var_id, loc_id=loc_id)
    order_id, _ = await _make_order(cn_pool, batch_id=batch_id)
    user_id = await _make_customer(cn_pool)

    result = await process_credit_gated_rma_workflow(
        ProcessCreditGatedRmaWorkflowConfig(),
        ProcessCreditGatedRmaWorkflowInput(
            order_id=order_id,
            batch_id=batch_id,
            user_id=user_id,
            user_trust_score=10,
            batch_anomaly_rate=0.5,
            route_risk=0.5,
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["decision"] == "drop_to_bin"
    assert result["refund_amount"] == 0


# ── generate_crushing_offer_workflow ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_crushing_offer_workflow_matches_and_reports_savings(
    cn_pool, tmp_path
):
    from app.ext.omodul.generate_crushing_offer_workflow import (
        GenerateCrushingOfferWorkflowConfig,
        GenerateCrushingOfferWorkflowInput,
        generate_crushing_offer_workflow,
    )

    loc_id, prod_id, var_id = await _make_variant(cn_pool)
    customer_id = await _make_customer(cn_pool)
    # 给同一个 product 建两个批次，价格不同，期望命中价格更低的那个
    await _make_batch(cn_pool, var_id=var_id, loc_id=loc_id, retail_price=5000)
    cheap_batch_id = await _make_batch(
        cn_pool, var_id=var_id, loc_id=loc_id, retail_price=3000
    )

    async with cn_pool.acquire() as conn:
        product_name = await conn.fetchval(
            "SELECT title FROM product WHERE id=$1", prod_id
        )

    result = await generate_crushing_offer_workflow(
        GenerateCrushingOfferWorkflowConfig(),
        GenerateCrushingOfferWorkflowInput(
            customer_id=customer_id,
            receipt_items=[
                {"item": product_name, "qty": 1, "price": 6000},
                {"item": "某个完全不存在的商品名字XYZ", "qty": 1, "price": 100},
            ],
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert len(result["matched_items"]) == 1
    assert result["matched_items"][0]["batch_id"] == cheap_batch_id
    assert result["matched_items"][0]["savings"] == 6000 - 3000
    assert len(result["unmatched_items"]) == 1

    async with cn_pool.acquire() as conn:
        line_items = await conn.fetch(
            "SELECT batch_id FROM cart_line_item WHERE cart_id=$1", result["cart_id"]
        )
    assert len(line_items) == 1
    assert str(line_items[0]["batch_id"]) == cheap_batch_id


# ── oservi: market_maker_engine / tote_balancing_engine / spatial_fomo_engine ─


@pytest.mark.asyncio
async def test_market_maker_engine_marks_down_undersold_batch(cn_pool, tmp_path):
    from app.ext.oservi import (
        build_batch_broadcast_engine,
        build_market_maker_engine,
    )

    loc_id, _, var_id = await _make_variant(cn_pool)
    now = datetime.now(UTC)
    batch_id = await _make_batch(
        cn_pool,
        var_id=var_id,
        loc_id=loc_id,
        retail_price=3900,
        intake_time=now - timedelta(hours=2),
        vwap_target_curve={"expected_cumulative": [5, 10, 20, 40], "stddev": 5},
    )

    broadcast = build_batch_broadcast_engine()
    engine = build_market_maker_engine(cn_pool, broadcast_engine=broadcast)
    results = await engine.run_once()
    summary = results[0]

    repriced_ids = {r["batch_id"] for r in summary["repriced"]}
    assert batch_id in repriced_ids

    async with cn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT retail_price_cents FROM inventory_batch WHERE id=$1", batch_id
        )
    assert row["retail_price_cents"] < 3900


@pytest.mark.asyncio
async def test_tote_balancing_engine_flags_shortage_location(cn_pool, tmp_path):
    from obase.uuid7 import uuid7

    from app.ext.oservi import build_tote_balancing_engine

    surplus_loc, _, _ = await _make_variant(cn_pool)
    shortage_loc, _, _ = await _make_variant(cn_pool)

    async with cn_pool.acquire() as conn:
        for _ in range(20):
            await conn.execute(
                "INSERT INTO tote (id, current_location_id, status) VALUES ($1,$2,'idle')",
                uuid7(),
                surplus_loc,
            )
        await conn.execute(
            "INSERT INTO tote (id, current_location_id, status) VALUES ($1,$2,'idle')",
            uuid7(),
            shortage_loc,
        )

    engine = build_tote_balancing_engine(cn_pool)
    results = await engine.run_once()
    summary = results[0]

    shortage_ids = {s["location_id"] for s in summary["shortage_locations"]}
    assert shortage_loc in shortage_ids
    assert surplus_loc not in shortage_ids


@pytest.mark.asyncio
async def test_spatial_fomo_engine_promo_push_and_threshold_commission(
    cn_pool, tmp_path
):
    from app.ext.oservi import build_spatial_fomo_engine
    from app.config import Settings

    engine = build_spatial_fomo_engine(cn_pool, settings=Settings(output_root=tmp_path))

    push_result = await engine.dispatch(
        "spatial_fomo.promo_push",
        {
            "batch_id": "batch_x",
            "target_polygon": {"type": "Polygon", "coordinates": []},
        },
    )
    assert push_result["status"] == "completed"
    assert push_result["errors"] == []
    assert push_result["results"][0]["pushed"] is True

    threshold_result = await engine.dispatch(
        "spatial_fomo.threshold_reached",
        {
            "host_id": "host_fomo_test",
            "address": "fomo test addr",
            "lat": 30.5,
            "lon": 120.5,
        },
    )
    assert threshold_result["status"] == "completed"
    assert threshold_result["errors"] == []
    inner = threshold_result["results"][0]
    assert inner["status"] == "completed"
    assert inner["location_id"]
