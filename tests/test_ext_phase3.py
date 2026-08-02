"""hemall 扩展域 v2.0 Phase 2/3 集成测试 — 需真实 Postgres (TEST_PG_DSN)。

覆盖全自动纠纷仲裁链路：oskill.evaluate_claim_credibility 已在
test_ext_oskill.py 单测过 (纯函数)；这里测 omodul.
execute_liability_routing_workflow (instant 退款+判责 / honeypot 挂起 /
rejected 拒赔) 和 oservi.autonomous_triage_engine 的端到端信号驱动全链路。

统一改造后：仲裁记录并入共享 claim 表 (不再是 扩展域自己的 rma_claims)，
供应商并入本地新表 supplier (不再是 suppliers)，订单走共享 omodul 的建车->
加车->授权支付->结账链路。
"""

from __future__ import annotations

import os

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

    from app.ext.schema import ensure_ext_schema
    from app.ext.vlm_provider import ManualVLMProvider

    reg = ProviderRegistry.get()
    reg.register_generic("payment", "manual", ManualPaymentProvider(), replace=True)
    vlm = ManualVLMProvider()
    reg.register_generic("vlm", "manual", vlm, replace=True)

    pool = await PgPool.create(
        name="hemall_phase3_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await ensure_ext_schema(pool)
    pool._test_vlm = vlm  # type: ignore[attr-defined]
    yield pool
    await pool.close()


async def _make_order(
    pool, *, supplier_id: str | None = None, retail_price: int = 3900
) -> tuple[str, str, str]:
    """建齐门店/商品/批次，走共享 omodul 的建车->加车->授权支付->结账链路拿到一笔真实订单。

    Returns:
        (order_id, batch_id, video_url)
    """
    from pathlib import Path

    from obase.uuid7 import uuid7
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

    video_url = "https://video.example/origin.mp4"
    async with pool.acquire() as conn:
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, lat, lng, status) "
            "VALUES ($1,'p3_loc','cn-east','host_p3','测试车库',31.0,121.0,'active') RETURNING id",
            uuid7(),
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,'土鸡蛋',$2,'active') RETURNING id",
            uuid7(),
            f"p3-slug-{uuid7()}",
        )
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"p3-sku-{uuid7()}",
        )
        batch_id = await conn.fetchval(
            "INSERT INTO inventory_batch "
            "(id, batch_no, variant_id, location_id, video_url, stock_qty, reserved_qty, "
            "cost_price_cents, retail_price_cents, inspection_status, supplier_id) "
            "VALUES ($1,$2,$3,$4,$5,10,0,2000,$6,'passed',$7) RETURNING id",
            uuid7(),
            f"p3-batch-{uuid7()}",
            var_id,
            loc_id,
            video_url,
            retail_price,
            supplier_id,
        )
    batch_id = str(batch_id)

    tmp_path = Path("/tmp")
    cart_result = await create_cart(
        CreateCartConfig(), CreateCartInput(), tmp_path, pool=pool
    )
    assert cart_result["status"] == "completed", cart_result
    cart_id = cart_result["cart_id"]

    add_result = await add_line_item_to_cart(
        AddLineItemConfig(),
        AddLineItemInput(cart_id=cart_id, batch_id=batch_id, quantity=1),
        tmp_path,
        pool=pool,
    )
    assert add_result["status"] == "completed", add_result

    sessions_result = await create_payment_sessions(
        CreatePaymentSessionsConfig(),
        CreatePaymentSessionsInput(cart_id=cart_id, provider_names=["manual"]),
        tmp_path,
        pool=pool,
    )
    assert sessions_result["status"] == "completed", sessions_result

    set_result = await set_payment_session(
        SetPaymentSessionConfig(),
        SetPaymentSessionInput(cart_id=cart_id, provider_name="manual"),
        tmp_path,
        pool=pool,
    )
    assert set_result["status"] == "completed", set_result

    authorize_result = await authorize_payment_for_cart(
        AuthorizePaymentForCartConfig(),
        AuthorizePaymentForCartInput(cart_id=cart_id),
        tmp_path,
        pool=pool,
    )
    assert authorize_result["status"] == "completed", authorize_result

    checkout_result = await complete_checkout(
        CompleteCheckoutConfig(),
        CompleteCheckoutInput(cart_id=cart_id),
        tmp_path,
        pool=pool,
    )
    assert checkout_result["status"] == "completed", checkout_result
    return str(checkout_result["order_id"]), batch_id, video_url


async def _make_supplier(pool, *, escrow_balance: int = 100_000) -> str:
    from obase.uuid7 import uuid7

    supplier_id = uuid7()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO supplier (id, wallet_account, spatial_polygon, escrow_balance) "
            "VALUES ($1,$2,$3,$4)",
            supplier_id,
            "wallet_demo",
            '{"type":"Polygon","coordinates":[]}',
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
            f"p3-{customer_id}@example.com",
        )
    return str(customer_id)


# ── execute_liability_routing_workflow ───────────────────────────────────────


@pytest.mark.asyncio
async def test_liability_routing_instant_refund_supplier_liable(cn_pool, tmp_path):
    from app.ext.omodul.execute_liability_routing_workflow import (
        ExecuteLiabilityRoutingWorkflowConfig,
        ExecuteLiabilityRoutingWorkflowInput,
        execute_liability_routing_workflow,
    )

    supplier_id = await _make_supplier(cn_pool, escrow_balance=100_000)
    order_id, batch_id, _ = await _make_order(cn_pool, supplier_id=supplier_id)
    user_id = await _make_customer(cn_pool)

    result = await execute_liability_routing_workflow(
        ExecuteLiabilityRoutingWorkflowConfig(),
        ExecuteLiabilityRoutingWorkflowInput(
            order_id=order_id,
            batch_id=batch_id,
            user_id=user_id,
            evidence_image_url="https://evidence.example/1.jpg",
            vlm_damage_type="spoiled",
            vlm_severity=0.8,
            fraud_probability=0.1,
            credibility_decision="instant",
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["decision"] == "instant_refund"
    assert result["liable_party"] == "supplier"
    assert result["refund_amount"] == 3900

    async with cn_pool.acquire() as conn:
        supplier_row = await conn.fetchrow(
            "SELECT escrow_balance FROM supplier WHERE id=$1", supplier_id
        )
        claim_row = await conn.fetchrow(
            "SELECT decision, liable_party, vlm_damage_type FROM claim WHERE id=$1",
            result["claim_id"],
        )
    assert supplier_row["escrow_balance"] == 100_000 - 3900
    assert claim_row["decision"] == "instant_refund"
    assert claim_row["liable_party"] == "supplier"
    assert claim_row["vlm_damage_type"] == "spoiled"


@pytest.mark.asyncio
async def test_liability_routing_instant_refund_location_liable_no_supplier_debit(
    cn_pool, tmp_path
):
    from app.ext.omodul.execute_liability_routing_workflow import (
        ExecuteLiabilityRoutingWorkflowConfig,
        ExecuteLiabilityRoutingWorkflowInput,
        execute_liability_routing_workflow,
    )

    supplier_id = await _make_supplier(cn_pool, escrow_balance=100_000)
    order_id, batch_id, _ = await _make_order(cn_pool, supplier_id=supplier_id)
    user_id = await _make_customer(cn_pool)

    result = await execute_liability_routing_workflow(
        ExecuteLiabilityRoutingWorkflowConfig(),
        ExecuteLiabilityRoutingWorkflowInput(
            order_id=order_id,
            batch_id=batch_id,
            user_id=user_id,
            evidence_image_url="https://evidence.example/2.jpg",
            vlm_damage_type="crushed",  # 不在供应商责任类型集合里
            vlm_severity=0.6,
            fraud_probability=0.0,
            credibility_decision="instant",
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["liable_party"] == "location"
    assert result["refund_amount"] == 3900

    async with cn_pool.acquire() as conn:
        supplier_row = await conn.fetchrow(
            "SELECT escrow_balance FROM supplier WHERE id=$1", supplier_id
        )
    assert supplier_row["escrow_balance"] == 100_000  # 未扣款


@pytest.mark.asyncio
async def test_liability_routing_honeypot_pending_no_refund(cn_pool, tmp_path):
    from app.ext.omodul.execute_liability_routing_workflow import (
        ExecuteLiabilityRoutingWorkflowConfig,
        ExecuteLiabilityRoutingWorkflowInput,
        execute_liability_routing_workflow,
    )

    order_id, batch_id, _ = await _make_order(cn_pool)
    user_id = await _make_customer(cn_pool)

    result = await execute_liability_routing_workflow(
        ExecuteLiabilityRoutingWorkflowConfig(),
        ExecuteLiabilityRoutingWorkflowInput(
            order_id=order_id,
            batch_id=batch_id,
            user_id=user_id,
            evidence_image_url="https://evidence.example/3.jpg",
            vlm_damage_type="crushed",
            vlm_severity=0.5,
            fraud_probability=0.1,
            credibility_decision="honeypot",
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["decision"] == "drop_to_bin"
    assert result["liable_party"] is None
    assert result["refund_amount"] == 0


@pytest.mark.asyncio
async def test_liability_routing_rejected_fraud_suspected(cn_pool, tmp_path):
    from app.ext.omodul.execute_liability_routing_workflow import (
        ExecuteLiabilityRoutingWorkflowConfig,
        ExecuteLiabilityRoutingWorkflowInput,
        execute_liability_routing_workflow,
    )

    order_id, batch_id, _ = await _make_order(cn_pool)
    user_id = await _make_customer(cn_pool)

    result = await execute_liability_routing_workflow(
        ExecuteLiabilityRoutingWorkflowConfig(),
        ExecuteLiabilityRoutingWorkflowInput(
            order_id=order_id,
            batch_id=batch_id,
            user_id=user_id,
            evidence_image_url="https://evidence.example/4.jpg",
            vlm_damage_type="crushed",
            vlm_severity=0.9,
            fraud_probability=0.95,  # 造假嫌疑极高
            credibility_decision="instant",  # 就算信誉裁决说 instant 也应该被覆盖
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["decision"] == "rejected"
    assert result["liable_party"] is None
    assert result["refund_amount"] == 0


@pytest.mark.asyncio
async def test_liability_routing_rejected_no_material_damage(cn_pool, tmp_path):
    from app.ext.omodul.execute_liability_routing_workflow import (
        ExecuteLiabilityRoutingWorkflowConfig,
        ExecuteLiabilityRoutingWorkflowInput,
        execute_liability_routing_workflow,
    )

    order_id, batch_id, _ = await _make_order(cn_pool)
    user_id = await _make_customer(cn_pool)

    result = await execute_liability_routing_workflow(
        ExecuteLiabilityRoutingWorkflowConfig(),
        ExecuteLiabilityRoutingWorkflowInput(
            order_id=order_id,
            batch_id=batch_id,
            user_id=user_id,
            evidence_image_url="https://evidence.example/5.jpg",
            vlm_damage_type="none",
            vlm_severity=0.0,
            fraud_probability=0.0,
            credibility_decision="instant",
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["decision"] == "rejected"


# ── autonomous_triage_engine (端到端信号驱动) ─────────────────────────────────


@pytest.mark.asyncio
async def test_autonomous_triage_engine_full_chain(cn_pool, tmp_path):
    from app.ext.oservi import build_autonomous_triage_engine
    from app.config import Settings

    supplier_id = await _make_supplier(cn_pool, escrow_balance=100_000)
    order_id, batch_id, video_url = await _make_order(cn_pool, supplier_id=supplier_id)
    user_id = await _make_customer(cn_pool)

    evidence_url = "https://evidence.example/triage.jpg"
    cn_pool._test_vlm.set_assessment(
        evidence_img=evidence_url,
        damage_type="spoiled",
        severity=0.9,
        fraud_probability=0.05,
    )

    engine = build_autonomous_triage_engine(
        cn_pool, settings=Settings(output_root=tmp_path)
    )
    dispatch_result = await engine.dispatch(
        "rma.claim_submitted",
        {
            "order_id": order_id,
            "batch_id": batch_id,
            "user_id": user_id,
            "evidence_image_url": evidence_url,
            "user_trust_score": 95,
            "route_risk": 0.0,
        },
    )

    assert dispatch_result["status"] == "completed", dispatch_result
    assert dispatch_result["errors"] == []
    inner = dispatch_result["results"][0]
    assert inner["status"] == "completed", inner
    assert inner["decision"] == "instant_refund"
    assert inner["liable_party"] == "supplier"

    async with cn_pool.acquire() as conn:
        supplier_row = await conn.fetchrow(
            "SELECT escrow_balance FROM supplier WHERE id=$1", supplier_id
        )
    assert supplier_row["escrow_balance"] == 100_000 - 3900
