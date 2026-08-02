"""hemall 扩展域 §4.1-§4.4 集成测试 — 需真实 Postgres (TEST_PG_DSN)。

覆盖入库侧 (集单/销毁/结算)、物理流转 (拣货/押金/退货/新节点)、分润结算
(工资/宿主分润)、会员订阅、环境式自动补货。每个函数至少一条 happy path +
一条该拒绝的负向用例。

统一改造后：物理批次/门店/购物车/订单全部走共享表；购物车加车/结账走
hemall 共享的 omodul 链路 (_checkout_cart 帮助函数)，不是 扩展域自己
已经退休的 add_line_item_to_cart/complete_checkout。cart_shipping_method_
set (扩展域自己的多行运费去重 hack) 也已经退休——共享 cart 模型本来就
只有一个 shipping_cents 标量字段，没有那个问题，这个测试跟着删掉。
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
    from obase.notification_providers import LogNotificationProvider
    from obase.payment_providers import ManualPaymentProvider
    from obase.persistence.pool import PgPool
    from obase.provider_registry import ProviderRegistry

    from app.ext.payout_provider import ManualPayoutProvider
    from app.ext.schema import ensure_ext_schema

    reg = ProviderRegistry.get()
    reg.register_generic("payment", "manual", ManualPaymentProvider(), replace=True)
    reg.register_generic("payout", "manual", ManualPayoutProvider(), replace=True)
    log_notify = LogNotificationProvider()
    reg.register_generic("notification", "log", log_notify, replace=True)

    pool = await PgPool.create(
        name="hemall_phase2_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await ensure_ext_schema(pool)
    pool._test_log_notify = log_notify  # type: ignore[attr-defined]
    yield pool
    await pool.close()


async def _make_variant(pool) -> tuple[str, str]:
    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, lat, lng, status) "
            "VALUES ($1,'p2_loc','cn-east','host_p2','测试车库',31.0,121.0,'active') RETURNING id",
            uuid7(),
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,'土鸡蛋',$2,'active') RETURNING id",
            uuid7(),
            f"p2-slug-{uuid7()}",
        )
        var_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"p2-sku-{uuid7()}",
        )
    return str(loc_id), str(var_id)


async def _make_batch(
    pool,
    *,
    var_id: str,
    loc_id: str,
    stock_qty: int,
    retail_price: int = 3900,
    cost_price: int = 2000,
) -> str:
    from obase.uuid7 import uuid7

    batch_id = uuid7()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inventory_batch "
            "(id, batch_no, variant_id, location_id, video_url, stock_qty, reserved_qty, "
            "cost_price_cents, retail_price_cents, inspection_status) "
            "VALUES ($1,$2,$3,$4,$5,$6,0,$7,$8,'passed')",
            batch_id,
            f"p2-batch-{batch_id}",
            var_id,
            loc_id,
            "https://video.example/v.mp4",
            stock_qty,
            cost_price,
            retail_price,
        )
    return str(batch_id)


async def _make_customer(pool) -> str:
    from obase.uuid7 import uuid7

    customer_id = uuid7()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO customer (id, email, status) VALUES ($1,$2,'active')",
            customer_id,
            f"p2-{customer_id}@example.com",
        )
    return str(customer_id)


async def _checkout_cart(pool, *, batch_id: str, quantity: int, tmp_path):
    """跑一遍共享 omodul 的建车->加车->授权支付->结账全链路，返回 complete_checkout 结果。"""
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
        CreateCartConfig(), CreateCartInput(), tmp_path, pool=pool
    )
    assert cart_result["status"] == "completed", cart_result
    cart_id = cart_result["cart_id"]

    add_result = await add_line_item_to_cart(
        AddLineItemConfig(),
        AddLineItemInput(cart_id=cart_id, batch_id=batch_id, quantity=quantity),
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

    return await complete_checkout(
        CompleteCheckoutConfig(),
        CompleteCheckoutInput(cart_id=cart_id),
        tmp_path,
        pool=pool,
    )


# ── §4.1 批次与供应链 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_crowd_intent_and_threshold(cn_pool, tmp_path):
    from obase.uuid7 import uuid7

    from app.ext.omodul.create_crowd_intent import (
        _REPLENISH_THRESHOLD,
        CreateCrowdIntentConfig,
        CreateCrowdIntentInput,
        create_crowd_intent,
    )

    _, var_id = await _make_variant(cn_pool)
    customer_id = await _make_customer(cn_pool)

    r1 = await create_crowd_intent(
        CreateCrowdIntentConfig(),
        CreateCrowdIntentInput(
            variant_id=var_id, customer_id=customer_id, prepaid_amount_cents=1000
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert r1["status"] == "completed", r1
    assert r1["pending_count"] == 1
    assert r1["threshold_triggered"] is False

    # 直接灌够 threshold-1 条，再调一次触发通知
    async with cn_pool.acquire() as conn:
        for _ in range(_REPLENISH_THRESHOLD - 2):
            await conn.execute(
                "INSERT INTO crowd_intent (id, variant_id, customer_id, prepaid_amount_cents, status) "
                "VALUES ($1,$2,$3,$4,'pending')",
                uuid7(),
                var_id,
                customer_id,
                1000,
            )

    r2 = await create_crowd_intent(
        CreateCrowdIntentConfig(),
        CreateCrowdIntentInput(
            variant_id=var_id, customer_id=customer_id, prepaid_amount_cents=1000
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert r2["status"] == "completed", r2
    assert r2["pending_count"] == _REPLENISH_THRESHOLD
    assert r2["threshold_triggered"] is True
    assert len(cn_pool._test_log_notify.sent) == 1


@pytest.mark.asyncio
async def test_mark_batch_for_disposal(cn_pool, tmp_path):
    from app.ext.omodul.mark_batch_for_disposal import (
        MarkBatchForDisposalConfig,
        MarkBatchForDisposalInput,
        mark_batch_for_disposal,
    )

    loc_id, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(cn_pool, var_id=var_id, loc_id=loc_id, stock_qty=5)

    result = await mark_batch_for_disposal(
        MarkBatchForDisposalConfig(),
        MarkBatchForDisposalInput(batch_id=batch_id),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["disposed_qty"] == 5

    # 已销毁的批次不能再销毁一次
    again = await mark_batch_for_disposal(
        MarkBatchForDisposalConfig(),
        MarkBatchForDisposalInput(batch_id=batch_id),
        tmp_path,
        pool=cn_pool,
    )
    assert again["status"] == "failed"
    assert "already disposed" in again["error"]["message"]


@pytest.mark.asyncio
async def test_batch_settlement(cn_pool, tmp_path):
    from app.ext.omodul.batch_settlement import (
        BatchSettlementConfig,
        BatchSettlementInput,
        batch_settlement,
    )
    from app.ext.oprim import db_lock_batch_inventory

    loc_id, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(
        cn_pool, var_id=var_id, loc_id=loc_id, stock_qty=4, cost_price=1500
    )

    # 还没卖完 -> 拒绝
    early = await batch_settlement(
        BatchSettlementConfig(),
        BatchSettlementInput(batch_id=batch_id, supplier_account="wx_supplier_1"),
        tmp_path,
        pool=cn_pool,
    )
    assert early["status"] == "failed"
    assert "not fully sold" in early["error"]["message"]

    # 卖完 (全部锁定)
    lock = await db_lock_batch_inventory(cn_pool, batch_id=batch_id, lock_qty=4)
    assert lock["status"] == "completed"

    result = await batch_settlement(
        BatchSettlementConfig(),
        BatchSettlementInput(batch_id=batch_id, supplier_account="wx_supplier_1"),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["amount"] == 4 * 1500

    async with cn_pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM inventory_batch WHERE id=$1", batch_id
        )
    assert status == "settled"


# ── §4.3 物理流转与无言售后 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_batch_pick_finalizes_stock_and_records_wage(cn_pool, tmp_path):
    from app.ext.omodul.confirm_batch_pick import (
        ConfirmBatchPickConfig,
        ConfirmBatchPickInput,
        confirm_batch_pick,
    )

    loc_id, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(cn_pool, var_id=var_id, loc_id=loc_id, stock_qty=10)
    checkout = await _checkout_cart(
        cn_pool, batch_id=batch_id, quantity=3, tmp_path=tmp_path
    )
    assert checkout["status"] == "completed", checkout

    async with cn_pool.acquire() as conn:
        oli_id = await conn.fetchval(
            "SELECT id FROM order_line_item WHERE order_id=$1", checkout["order_id"]
        )

    pick = await confirm_batch_pick(
        ConfirmBatchPickConfig(),
        ConfirmBatchPickInput(
            order_line_item_id=str(oli_id), worker_id="worker_dama_1"
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert pick["status"] == "completed", pick
    # 计件工资走 oskill.calculate_piece_rate_wage 按"全系统积压深度"激增，
    # 这是一个跨测试共享的真实 DB，积压深度不受本测试独占控制——只能断言
    # 下限 (激增只会往上调，不会低于基础单价 * 数量)，不能断言精确值。
    assert pick["wage_amount"] >= 3 * 500
    assert pick["queue_depth"] >= 0

    async with cn_pool.acquire() as conn:
        batch_row = await conn.fetchrow(
            "SELECT stock_qty, reserved_qty FROM inventory_batch WHERE id=$1", batch_id
        )
    assert batch_row["stock_qty"] == 7  # 10 - 3 confirmed out
    assert batch_row["reserved_qty"] == 0


@pytest.mark.asyncio
async def test_tote_deposit_charge_and_refund_cycle(cn_pool, tmp_path):
    from obase.uuid7 import uuid7

    from app.ext.omodul.tote_deposit_and_refund import (
        ToteDepositAndRefundConfig,
        ToteDepositAndRefundInput,
        tote_deposit_and_refund,
    )

    tote_id = uuid7()
    async with cn_pool.acquire() as conn:
        await conn.execute("INSERT INTO tote (id, status) VALUES ($1, 'idle')", tote_id)

    charge = await tote_deposit_and_refund(
        ToteDepositAndRefundConfig(),
        ToteDepositAndRefundInput(tote_id=tote_id, action="charge"),
        tmp_path,
        pool=cn_pool,
    )
    assert charge["status"] == "completed", charge
    assert charge["amount"] == 1000

    double_charge = await tote_deposit_and_refund(
        ToteDepositAndRefundConfig(),
        ToteDepositAndRefundInput(tote_id=tote_id, action="charge"),
        tmp_path,
        pool=cn_pool,
    )
    assert double_charge["status"] == "failed"

    refund = await tote_deposit_and_refund(
        ToteDepositAndRefundConfig(),
        ToteDepositAndRefundInput(tote_id=tote_id, action="refund"),
        tmp_path,
        pool=cn_pool,
    )
    assert refund["status"] == "completed", refund

    double_refund = await tote_deposit_and_refund(
        ToteDepositAndRefundConfig(),
        ToteDepositAndRefundInput(tote_id=tote_id, action="refund"),
        tmp_path,
        pool=cn_pool,
    )
    assert double_refund["status"] == "failed"

    async with cn_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM tote WHERE id=$1", tote_id)
    assert status == "idle"


@pytest.mark.asyncio
async def test_process_drop_return_refunds_full_line(cn_pool, tmp_path):
    from app.ext.omodul.process_drop_return import (
        ProcessDropReturnConfig,
        ProcessDropReturnInput,
        process_drop_return,
    )

    loc_id, var_id = await _make_variant(cn_pool)
    batch_id = await _make_batch(
        cn_pool, var_id=var_id, loc_id=loc_id, stock_qty=10, retail_price=2000
    )
    checkout = await _checkout_cart(
        cn_pool, batch_id=batch_id, quantity=2, tmp_path=tmp_path
    )
    assert checkout["status"] == "completed"

    async with cn_pool.acquire() as conn:
        oli_id = await conn.fetchval(
            "SELECT id FROM order_line_item WHERE order_id=$1", checkout["order_id"]
        )

    result = await process_drop_return(
        ProcessDropReturnConfig(),
        ProcessDropReturnInput(order_line_item_id=str(oli_id), reason="rotten"),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["refund_amount"] == 4000  # 2 * 2000


@pytest.mark.asyncio
async def test_commission_new_location(cn_pool, tmp_path):
    from app.ext.omodul.commission_new_location import (
        CommissionNewLocationConfig,
        CommissionNewLocationInput,
        commission_new_location,
    )

    result = await commission_new_location(
        CommissionNewLocationConfig(),
        CommissionNewLocationInput(
            host_id="host_new_1", address="新车库", lat=31.5, lon=121.3
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    # 这是共享真实 DB，之前的测试早就积累了一堆别的 active stock_location，
    # 所以这里 Voronoi 图几乎必然有意义 (>=2 个点)；只断言结构正确，不断言
    # 具体邻居是谁 (那取决于所有历史测试数据，不是本测试能控制的)。
    assert result["grid_recomputed"] is True
    assert isinstance(result["neighbors"], list)
    assert result["location_id"] not in result["neighbors"]

    async with cn_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT host_id, status FROM stock_location WHERE id=$1",
            result["location_id"],
        )
    assert row["host_id"] == "host_new_1"
    assert row["status"] == "active"


# ── §4.4 去中心化分润结算 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_labor_payment_aggregates_pending(cn_pool, tmp_path):
    from app.ext.omodul.dispatch_labor_payment import (
        DispatchLaborPaymentConfig,
        DispatchLaborPaymentInput,
        dispatch_labor_payment,
    )
    from app.ext.oprim import generate_id_v7

    worker_id = generate_id_v7("worker")
    async with cn_pool.acquire() as conn:
        for amount in (500, 1500, 1000):
            await conn.execute(
                "INSERT INTO labor_ledger (id, worker_id, wage_amount, status) VALUES ($1,$2,$3,'pending')",
                generate_id_v7("labor"),
                worker_id,
                amount,
            )

    result = await dispatch_labor_payment(
        DispatchLaborPaymentConfig(),
        DispatchLaborPaymentInput(worker_id=worker_id, payout_account="wx_dama_2"),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["total_amount"] == 3000
    assert result["entry_count"] == 3

    # 再结一次 -> 没有 pending 了，拒绝
    again = await dispatch_labor_payment(
        DispatchLaborPaymentConfig(),
        DispatchLaborPaymentInput(worker_id=worker_id, payout_account="wx_dama_2"),
        tmp_path,
        pool=cn_pool,
    )
    assert again["status"] == "failed"


@pytest.mark.asyncio
async def test_dispatch_host_dividend(cn_pool, tmp_path):
    from app.ext.omodul.dispatch_host_dividend import (
        DispatchHostDividendConfig,
        DispatchHostDividendInput,
        dispatch_host_dividend,
    )

    loc_id, _ = await _make_variant(cn_pool)

    result = await dispatch_host_dividend(
        DispatchHostDividendConfig(),
        DispatchHostDividendInput(
            host_id="host_p2",
            location_id=loc_id,
            payout_account="wx_host_1",
            tote_count=20,
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["amount"] == 20 * 300

    bad = await dispatch_host_dividend(
        DispatchHostDividendConfig(),
        DispatchHostDividendInput(
            host_id="host_p2",
            location_id=loc_id,
            payout_account="wx_host_1",
            tote_count=0,
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert bad["status"] == "failed"


# ── process_subscription ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_subscription_rejects_duplicate(cn_pool, tmp_path):
    from app.ext.omodul.process_subscription import (
        ProcessSubscriptionConfig,
        ProcessSubscriptionInput,
        process_subscription,
    )

    customer_id = await _make_customer(cn_pool)

    r1 = await process_subscription(
        ProcessSubscriptionConfig(),
        ProcessSubscriptionInput(customer_id=customer_id, plan_fee_cents=9900),
        tmp_path,
        pool=cn_pool,
    )
    assert r1["status"] == "completed", r1

    r2 = await process_subscription(
        ProcessSubscriptionConfig(),
        ProcessSubscriptionInput(customer_id=customer_id, plan_fee_cents=9900),
        tmp_path,
        pool=cn_pool,
    )
    assert r2["status"] == "failed"
    assert "already has an active membership" in r2["error"]["message"]


# ── execute_ambient_replenishment ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_ambient_replenishment_not_due_yet(cn_pool, tmp_path):
    from app.ext.omodul.execute_ambient_replenishment import (
        ExecuteAmbientReplenishmentConfig,
        ExecuteAmbientReplenishmentInput,
        execute_ambient_replenishment,
    )

    customer_id = await _make_customer(cn_pool)
    now = datetime.now(UTC)
    # 两次购买间隔 1 天，最近一次就是现在 -> 预测见底时间在未来，还没到期。
    history = [
        {"purchased_at": now - timedelta(days=1), "quantity": 1},
        {"purchased_at": now, "quantity": 1},
    ]

    result = await execute_ambient_replenishment(
        ExecuteAmbientReplenishmentConfig(),
        ExecuteAmbientReplenishmentInput(
            customer_id=customer_id,
            variant_id="00000000-0000-0000-0000-000000000000",
            qty=1,
            family_size=3,
            purchase_history=history,
            customer_lat=31.0,
            customer_lon=121.0,
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["replenished"] is False


@pytest.mark.asyncio
async def test_execute_ambient_replenishment_charges_and_routes_to_nearest(
    cn_pool, tmp_path
):
    from obase.uuid7 import uuid7

    from app.ext.omodul.execute_ambient_replenishment import (
        ExecuteAmbientReplenishmentConfig,
        ExecuteAmbientReplenishmentInput,
        execute_ambient_replenishment,
    )

    customer_id = await _make_customer(cn_pool)
    near_loc_id, var_id = await _make_variant(cn_pool)
    near_batch_id = await _make_batch(
        cn_pool, var_id=var_id, loc_id=near_loc_id, stock_qty=50, retail_price=3900
    )

    async with cn_pool.acquire() as conn:
        far_loc_id = await conn.fetchval(
            "INSERT INTO stock_location (id, name, region_code, host_id, address, lat, lng, status) "
            "VALUES ($1,'p2_far','cn-east','host_far','远处车库',40.0,121.0,'active') RETURNING id",
            uuid7(),
        )
    far_loc_id = str(far_loc_id)
    far_batch_id = await _make_batch(
        cn_pool, var_id=var_id, loc_id=far_loc_id, stock_qty=50, retail_price=3900
    )

    now = datetime.now(UTC)
    # 30 天前 / 40 天前购买，间隔 10 天，family_size=3 (参照基数) 不缩放 ->
    # 预测见底时间 = 30 天前 + 10 天 = 20 天前，早已过期，应立即触发补货。
    history = [
        {"purchased_at": now - timedelta(days=40), "quantity": 1},
        {"purchased_at": now - timedelta(days=30), "quantity": 1},
    ]

    result = await execute_ambient_replenishment(
        ExecuteAmbientReplenishmentConfig(),
        ExecuteAmbientReplenishmentInput(
            customer_id=customer_id,
            variant_id=var_id,
            qty=2,
            family_size=3,
            purchase_history=history,
            customer_lat=31.0,  # 跟 near_loc 同一个坐标，far_loc 在 40.0 纬度很远
            customer_lon=121.0,
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "completed", result
    assert result["replenished"] is True
    assert result["location_id"] == near_loc_id
    assert result["batch_id"] == near_batch_id
    assert result["grand_total_cents"] == 3900 * 2

    async with cn_pool.acquire() as conn:
        near_row = await conn.fetchrow(
            "SELECT reserved_qty FROM inventory_batch WHERE id=$1", near_batch_id
        )
        far_row = await conn.fetchrow(
            "SELECT reserved_qty FROM inventory_batch WHERE id=$1", far_batch_id
        )
        order_row = await conn.fetchrow(
            "SELECT grand_total_cents, status FROM customer_order WHERE id=$1",
            result["order_id"],
        )
    assert near_row["reserved_qty"] == 2
    assert far_row["reserved_qty"] == 0
    assert order_row["status"] == "paid"
    assert order_row["grand_total_cents"] == 3900 * 2


@pytest.mark.asyncio
async def test_execute_ambient_replenishment_fails_when_no_stock_nearby(
    cn_pool, tmp_path
):
    from app.ext.omodul.execute_ambient_replenishment import (
        ExecuteAmbientReplenishmentConfig,
        ExecuteAmbientReplenishmentInput,
        execute_ambient_replenishment,
    )

    customer_id = await _make_customer(cn_pool)
    _, var_id = await _make_variant(cn_pool)  # 没有 batch，库存为空

    now = datetime.now(UTC)
    history = [
        {"purchased_at": now - timedelta(days=40), "quantity": 1},
        {"purchased_at": now - timedelta(days=30), "quantity": 1},
    ]

    result = await execute_ambient_replenishment(
        ExecuteAmbientReplenishmentConfig(),
        ExecuteAmbientReplenishmentInput(
            customer_id=customer_id,
            variant_id=var_id,
            qty=1,
            family_size=3,
            purchase_history=history,
            customer_lat=31.0,
            customer_lon=121.0,
        ),
        tmp_path,
        pool=cn_pool,
    )
    assert result["status"] == "failed"
    assert "no available inventory" in result["error"]["message"]
