"""P0 交易服务的真实 PostgreSQL invariant tests.

这些用例故意不使用内存库存或 mock transaction。provider 只模拟外部网络边界，
数据库中的 intent/outbox/reservation/订单状态仍由生产 service 和 PostgreSQL
锁、唯一约束、事务真实驱动。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

TEST_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_PG_DSN not set; skipping P0 PostgreSQL tests"
)


@dataclass(frozen=True)
class StockSeed:
    product_id: str
    variant_id: str
    location_id: str
    batch_ids: tuple[str, ...]
    quantities: tuple[int, ...]


@pytest.fixture
async def p0_pool():
    from obase.persistence.pool import PgPool

    pool = await PgPool.create(
        name=f"hemall_p0_{uuid4().hex}",
        dsn=TEST_DSN,
        min_size=1,
        max_size=20,
    )
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_stock(pool: Any, quantities: tuple[int, ...] = (10,)) -> StockSeed:
    suffix = uuid4().hex
    async with pool.acquire() as conn:
        location_id = await conn.fetchval(
            """
            INSERT INTO stock_location (id, name, region_code, host_id, address, lat, lng)
            VALUES ($1, $2, 'p0-test', $3, 'P0 test location', 31.23, 121.47)
            RETURNING id
            """,
            uuid4(),
            f"p0-location-{suffix}",
            f"p0-host-{suffix[:20]}",
        )
        product_id = await conn.fetchval(
            """
            INSERT INTO product (id, title, slug, status)
            VALUES ($1, $2, $3, 'active')
            RETURNING id
            """,
            uuid4(),
            f"P0 product {suffix}",
            f"p0-product-{suffix}",
        )
        variant_id = await conn.fetchval(
            """
            INSERT INTO product_variant (id, product_id, sku_code, status)
            VALUES ($1, $2, $3, 'active')
            RETURNING id
            """,
            uuid4(),
            product_id,
            f"P0-SKU-{suffix}",
        )
        batch_ids: list[str] = []
        for index, quantity in enumerate(quantities):
            batch_id = await conn.fetchval(
                """
                INSERT INTO inventory_batch (
                    id, variant_id, location_id, supplier_id, batch_no, video_url,
                    cost_price_cents, stock_qty, reserved_qty, retail_price_cents,
                    inspection_status, expiration_time, status
                )
                VALUES ($1, $2, $3, NULL, $4, 'https://p0.test/batch.mp4',
                        100, $5, 0, 250, 'passed', $6, 'active')
                RETURNING id
                """,
                uuid4(),
                variant_id,
                location_id,
                f"P0-BATCH-{suffix}-{index}",
                quantity,
                datetime.now(UTC) + timedelta(days=30),
            )
            batch_ids.append(str(batch_id))
    return StockSeed(
        product_id=str(product_id),
        variant_id=str(variant_id),
        location_id=str(location_id),
        batch_ids=tuple(batch_ids),
        quantities=quantities,
    )


async def _insert_order(
    pool: Any,
    *,
    status: str = "pending",
    total_cents: int = 1000,
    currency: str = "CNY",
    customer_id: str | None = None,
) -> str:
    order_id = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO customer_order (
                id, customer_id, region_code, currency, status,
                subtotal_cents, discount_cents, tax_cents, shipping_cents,
                grand_total_cents
            )
            VALUES ($1, $2, 'p0-test', $3, $4, $5, 0, 0, 0, $5)
            """,
            order_id,
            customer_id,
            currency,
            status,
            total_cents,
        )
    return str(order_id)


async def _insert_line(
    pool: Any,
    *,
    order_id: str,
    batch_id: str,
    quantity: int,
    unit_price_cents: int = 250,
) -> str:
    line_id = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO order_line_item
                (id, order_id, batch_id, quantity, unit_price_cents, line_total_cents)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            line_id,
            order_id,
            batch_id,
            quantity,
            unit_price_cents,
            quantity * unit_price_cents,
        )
    return str(line_id)


async def _wire_line_reservation(pool: Any, *, order_id: str, line_id: str, batch_id: str) -> str:
    async with pool.acquire() as conn:
        reservation_id = await conn.fetchval(
            """
            SELECT id FROM inventory_reservation
            WHERE order_id = $1 AND batch_id = $2
            ORDER BY created_at, id
            LIMIT 1
            """,
            order_id,
            batch_id,
        )
        assert reservation_id is not None
        await conn.execute(
            """
            UPDATE inventory_reservation
            SET order_line_item_id = $1, updated_at = NOW()
            WHERE id = $2
            """,
            line_id,
            reservation_id,
        )
        await conn.execute(
            "UPDATE order_line_item SET reservation_id = $1 WHERE id = $2",
            reservation_id,
            line_id,
        )
    return str(reservation_id)


async def _prepare_reserved_order(
    pool: Any,
    *,
    quantity: int = 2,
    status: str = "pending",
    total_cents: int = 1000,
) -> tuple[StockSeed, str, str, str]:
    from app.inventory.service import InventoryService

    seed = await _seed_stock(pool, (quantity + 5,))
    order_id = await _insert_order(pool, status=status, total_cents=total_cents)
    line_id = await _insert_line(
        pool, order_id=order_id, batch_id=seed.batch_ids[0], quantity=quantity
    )
    allocations = await InventoryService(pool).reserve_stock_allocations(
        order_id=order_id,
        product_id=seed.product_id,
        variant_id=seed.variant_id,
        location_id=seed.location_id,
        quantity=quantity,
        reservation_key=f"p0-order-reservation:{order_id}",
    )
    assert len(allocations) == 1
    reservation_id = await _wire_line_reservation(
        pool, order_id=order_id, line_id=line_id, batch_id=seed.batch_ids[0]
    )
    return seed, order_id, line_id, reservation_id


class RecordingGateway:
    """外部 provider 的可控边界；所有本地状态仍落真实 PostgreSQL。"""

    def __init__(self) -> None:
        self.prepay_calls: list[dict[str, Any]] = []
        self.refund_calls: list[dict[str, Any]] = []
        self.prepays: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.refund_status = "success"
        self.webhook_event: dict[str, Any] | None = None

    async def prepay(
        self,
        *,
        out_trade_no: str,
        total_fee_cents: int,
        description: str,
        notify_url: str,
    ) -> dict[str, Any]:
        self.prepay_calls.append(
            {
                "out_trade_no": out_trade_no,
                "total_fee_cents": total_fee_cents,
                "description": description,
                "notify_url": notify_url,
            }
        )
        return self.prepays.setdefault(
            out_trade_no,
            {
                "out_trade_no": out_trade_no,
                "total_fee_cents": total_fee_cents,
                "status": "pending",
                "prepay_id": f"prepay-{out_trade_no}",
                "payment_intent_id": f"provider-intent-{out_trade_no}",
            },
        )

    async def stripe_prepay(
        self,
        *,
        out_trade_no: str,
        total_fee_cents: int,
        description: str,
        currency: str = "CNY",
    ) -> dict[str, Any]:
        result = await self.prepay(
            out_trade_no=out_trade_no,
            total_fee_cents=total_fee_cents,
            description=description,
            notify_url="stripe",
        )
        return {**result, "currency": currency.upper(), "client_secret": "secret"}

    async def refund(
        self,
        *,
        out_trade_no: str,
        out_refund_no: str,
        refund_fee_cents: int,
        reason: str = "",
        total_fee_cents: int | None = None,
        payment_intent_id: str | None = None,
    ) -> dict[str, Any]:
        self.refund_calls.append(
            {
                "out_trade_no": out_trade_no,
                "out_refund_no": out_refund_no,
                "refund_fee_cents": refund_fee_cents,
                "reason": reason,
                "total_fee_cents": total_fee_cents,
                "payment_intent_id": payment_intent_id,
            }
        )
        result = self.refunds.setdefault(
            out_refund_no,
            {
                "status": self.refund_status,
                "refund_id": f"provider-refund-{out_refund_no}",
            },
        )
        # A provider may return PROCESSING first and SUCCESS on a later query
        # while retaining the same idempotency key.
        result["status"] = self.refund_status
        return result

    async def verify_callback(self, headers: dict[str, str], body: bytes) -> dict[str, Any] | None:
        del headers, body
        return self.webhook_event


@pytest.fixture
async def payment_env(p0_pool: Any):
    from obase.provider_registry import ProviderRegistry

    registry = ProviderRegistry.get()
    gateway = RecordingGateway()
    old_values: dict[str, Any] = {}
    for name in ("manual", "stripe", "wechat", "alipay"):
        with contextlib.suppress(Exception):
            old_values[name] = registry.generic("payment_gateway", name)
        registry.register_generic("payment_gateway", name, gateway, replace=True)
    settings = SimpleNamespace(wechat_pay_notify_url="https://p0.test/payments/notify")
    try:
        yield p0_pool, gateway, settings
    finally:
        from app.ext.payout_provider import ManualPaymentGateway

        registry.register_generic(
            "payment_gateway",
            "manual",
            old_values.get("manual", ManualPaymentGateway()),
            replace=True,
        )
        for name in ("stripe", "wechat", "alipay"):
            if name in old_values:
                registry.register_generic("payment_gateway", name, old_values[name], replace=True)


async def _make_intent(
    pool: Any,
    settings: Any,
    *,
    provider: str = "manual",
    total_cents: int = 1000,
    currency: str = "CNY",
) -> tuple[str, Any, Any]:
    from app.payments.service import PaymentService

    order_id = await _insert_order(
        pool, total_cents=total_cents, currency=currency, status="pending"
    )
    service = PaymentService(pool, settings)
    intent = await service.create_intent(
        order_id=order_id,
        provider=provider,
        description="P0 transaction test",
        idempotency_key=f"p0-client:{uuid4()}",
    )
    return order_id, service, intent


async def _make_paid_intent(
    pool: Any,
    gateway: RecordingGateway,
    settings: Any,
    *,
    total_cents: int = 1000,
) -> tuple[str, Any, Any]:
    from app.payments.router import _settle_payment_and_order

    order_id, service, intent = await _make_intent(pool, settings, total_cents=total_cents)
    await service.process_intent(intent.intent_id)
    await _settle_payment_and_order(
        pool,
        intent.intent_id,
        f"transaction-p0-{uuid4().hex}",
        total_cents,
        expected_provider="manual",
        paid_currency="CNY",
    )
    assert gateway.prepay_calls
    return order_id, service, intent


async def _install_raising_trigger(
    pool: Any, *, table: str, row_id: str, prefix: str
) -> tuple[str, str]:
    function_name = f"{prefix}_fn_{uuid4().hex[:12]}"
    trigger_name = f"{prefix}_trg_{uuid4().hex[:12]}"
    async with pool.acquire() as conn:
        await conn.execute(
            f'''
            CREATE FUNCTION "{function_name}"() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'P0 test persistence failure';
            END;
            $$;
            CREATE TRIGGER "{trigger_name}"
            BEFORE UPDATE ON "{table}"
            FOR EACH ROW WHEN (OLD.id = '{row_id}'::uuid)
            EXECUTE FUNCTION "{function_name}"();
            '''
        )
    return function_name, trigger_name


async def _install_skip_update_trigger(
    pool: Any, *, table: str, row_id: str, prefix: str
) -> tuple[str, str]:
    function_name = f"{prefix}_fn_{uuid4().hex[:12]}"
    trigger_name = f"{prefix}_trg_{uuid4().hex[:12]}"
    async with pool.acquire() as conn:
        await conn.execute(
            f'''
            CREATE FUNCTION "{function_name}"() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RETURN NULL;
            END;
            $$;
            CREATE TRIGGER "{trigger_name}"
            BEFORE UPDATE ON "{table}"
            FOR EACH ROW WHEN (OLD.id = '{row_id}'::uuid)
            EXECUTE FUNCTION "{function_name}"();
            '''
        )
    return function_name, trigger_name


async def _drop_trigger(pool: Any, *, table: str, function_name: str, trigger_name: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}" ON "{table}"')
        await conn.execute(f'DROP FUNCTION IF EXISTS "{function_name}"()')


class _AlipayStub:
    def __init__(self, *, success: bool = True, refund_code: str | None = None) -> None:
        self.success = success
        self.refund_code = refund_code

    async def create_order(self, request: Any) -> Any:
        from app.payments.alipay import AlipayOrderResponse

        del request
        return AlipayOrderResponse(
            qr_code="alipay://p0" if self.success else None,
            err_code=None if self.success else "TRADE_ERROR",
            err_msg=None if self.success else "rejected",
        )

    async def refund(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"code": self.refund_code, "msg": "rejected" if self.refund_code else "ok"}


class _WechatStub:
    def __init__(self, *, success: bool = True) -> None:
        self.success = success

    async def create_order(self, request: Any) -> Any:
        from app.payments.wechat import WeChatPayOrderResponse

        del request
        return WeChatPayOrderResponse(
            prepay_id="wechat-prepay" if self.success else None,
            code_url="weixin://p0" if self.success else None,
            err_code=None if self.success else "ORDER_ERROR",
            err_msg=None if self.success else "rejected",
        )


class _LegacyRefundStub:
    def __init__(self) -> None:
        self.calls = 0

    async def refund(
        self, *, out_trade_no: str, out_refund_no: str, refund_amount: int
    ) -> dict[str, Any]:
        self.calls += 1
        return {
            "status": "success",
            "out_trade_no": out_trade_no,
            "out_refund_no": out_refund_no,
            "refund_amount": refund_amount,
        }


@pytest.mark.asyncio
async def test_payment_authority_and_concurrent_intent_idempotency(payment_env: Any):
    from app.payments.service import (
        PaymentAmountMismatchError,
        PaymentNotFoundError,
        PaymentService,
        PaymentServiceError,
    )

    pool, _gateway, settings = payment_env
    order_id = await _insert_order(pool, total_cents=1234, currency="cny")
    service = PaymentService(pool, settings)

    results = await asyncio.gather(
        *(
            service.create_intent(
                order_id=order_id,
                provider="manual",
                requested_amount=Decimal("12.34"),
                idempotency_key=f"client-retry-{index}",
            )
            for index in range(8)
        )
    )
    assert {result.intent_id for result in results} == {results[0].intent_id}
    assert results[0].amount_cents == 1234
    assert results[0].currency == "CNY"

    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT COUNT(*) FROM payment_intent WHERE order_id = $1", order_id)
            == 1
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM payment_outbox WHERE payment_intent_id = $1",
                results[0].intent_id,
            )
            == 1
        )

    with pytest.raises(PaymentAmountMismatchError):
        await service.create_intent(
            order_id=order_id,
            provider="manual",
            requested_amount=Decimal("99.99"),
        )
    with pytest.raises(PaymentAmountMismatchError):
        await service.create_intent(
            order_id=order_id,
            provider="manual",
            requested_amount=Decimal("12.345"),
        )
    with pytest.raises(PaymentServiceError):
        await service.create_intent(order_id=order_id, provider="not-a-provider")
    with pytest.raises(PaymentNotFoundError):
        await service.create_intent(order_id=str(uuid4()), provider="manual")


def test_payment_amount_conversion_is_strict():
    from app.payments.service import PaymentAmountMismatchError, yuan_to_cents

    assert yuan_to_cents(Decimal("0.01")) == 1
    assert yuan_to_cents("10.50") == 1050
    assert yuan_to_cents(2) == 200
    for value in (0, -1, "nan", "1.001"):
        with pytest.raises(PaymentAmountMismatchError):
            yuan_to_cents(value)


@pytest.mark.asyncio
async def test_payment_provider_success_db_failure_is_recoverable(payment_env: Any):
    pool, gateway, settings = payment_env
    _order_id, service, intent = await _make_intent(pool, settings, total_cents=2780)
    function_name, trigger_name = await _install_raising_trigger(
        pool, table="payment_intent", row_id=intent.intent_id, prefix="p0_payment"
    )
    try:
        first = await service.process_intent(intent.intent_id)
        assert first.status == "pending"
        assert len(gateway.prepay_calls) == 1
    finally:
        await _drop_trigger(
            pool,
            table="payment_intent",
            function_name=function_name,
            trigger_name=trigger_name,
        )

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE payment_outbox
            SET locked_at = NOW() - INTERVAL '10 minutes'
            WHERE payment_intent_id = $1 AND kind = 'prepay'
            """,
            intent.intent_id,
        )
    assert await service.reconcile() >= 1

    recovered = await service._read_intent(intent.intent_id)
    assert recovered.status == "provider_pending"
    target_calls = [
        call for call in gateway.prepay_calls if call["out_trade_no"] == intent.intent_id
    ]
    assert len(target_calls) == 2
    assert {call["out_trade_no"] for call in target_calls} == {intent.intent_id}
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, attempts FROM payment_outbox WHERE payment_intent_id = $1",
            intent.intent_id,
        )
    assert row["status"] == "completed"
    assert row["attempts"] == 2


@pytest.mark.asyncio
async def test_payment_callback_wrong_amount_currency_and_order_fail_closed(payment_env: Any):
    from app.payments.router import _settle_payment_and_order

    pool, _gateway, settings = payment_env
    order_id, _service, intent = await _make_intent(pool, settings, total_cents=1500)

    await _settle_payment_and_order(
        pool,
        intent.intent_id,
        "wrong-amount",
        1499,
        expected_provider="manual",
        paid_currency="CNY",
    )
    await _settle_payment_and_order(
        pool,
        intent.intent_id,
        "wrong-currency",
        1500,
        expected_provider="manual",
        paid_currency="USD",
    )
    await _settle_payment_and_order(
        pool,
        intent.intent_id,
        "wrong-provider",
        1500,
        expected_provider="stripe",
        paid_currency="CNY",
    )
    await _settle_payment_and_order(
        pool,
        str(uuid4()),
        "unknown-order",
        1500,
        expected_provider="manual",
        paid_currency="CNY",
    )
    await _settle_payment_and_order(pool, intent.intent_id, "missing-amount", None)
    await _settle_payment_and_order(None, intent.intent_id, "no-db", 1500)

    async with pool.acquire() as conn:
        intent_row = await conn.fetchrow(
            "SELECT status FROM payment_intent WHERE id = $1", intent.intent_id
        )
        order_row = await conn.fetchrow("SELECT status FROM customer_order WHERE id = $1", order_id)
        assert intent_row["status"] == "pending"
        assert order_row["status"] == "pending"
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM order_status_history WHERE order_id = $1", order_id
            )
            == 0
        )


@pytest.mark.asyncio
async def test_duplicate_concurrent_payment_callbacks_have_one_effect(payment_env: Any):
    from app.payments.router import _settle_payment_and_order

    pool, gateway, settings = payment_env
    seed = await _seed_stock(pool, (10,))
    order_id, service, intent = await _make_intent(
        pool, settings, total_cents=750, provider="manual"
    )
    line_id = await _insert_line(
        pool, order_id=order_id, batch_id=seed.batch_ids[0], quantity=3, unit_price_cents=250
    )
    await service.process_intent(intent.intent_id)

    await asyncio.gather(
        *(
            _settle_payment_and_order(
                pool,
                intent.intent_id,
                f"transaction-{intent.intent_id}-{index}",
                750,
                expected_provider="manual",
                paid_currency="CNY",
            )
            for index in range(10)
        )
    )
    assert gateway.prepay_calls

    async with pool.acquire() as conn:
        payment = await conn.fetchrow(
            "SELECT status, provider_trade_no, version FROM payment_intent WHERE id = $1",
            intent.intent_id,
        )
        order = await conn.fetchrow(
            "SELECT status, version FROM customer_order WHERE id = $1", order_id
        )
        assert payment["status"] == "paid"
        assert payment["provider_trade_no"] is not None
        assert order["status"] == "confirmed"
        assert order["version"] == 1
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM inventory_reservation WHERE order_id = $1", order_id
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM order_status_history WHERE order_id = $1", order_id
            )
            == 1
        )
        assert (
            await conn.fetchval("SELECT reservation_id FROM order_line_item WHERE id = $1", line_id)
            is not None
        )


@pytest.mark.asyncio
async def test_duplicate_stripe_webhook_replay_has_one_business_effect(payment_env: Any):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from obase.provider_registry import ProviderRegistry

    from app.payments.router import router

    pool, gateway, _settings = payment_env
    seed = await _seed_stock(pool, (8,))
    order_id = await _insert_order(pool, total_cents=500)
    line_id = await _insert_line(
        pool, order_id=order_id, batch_id=seed.batch_ids[0], quantity=2, unit_price_cents=250
    )
    intent_id = uuid4()
    provider_intent_id = f"pi-p0-{uuid4().hex}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO payment_intent
                (id, order_id, amount_cents, currency, provider, status, provider_intent_id)
            VALUES ($1, $2, 500, 'CNY', 'stripe', 'provider_pending', $3)
            """,
            intent_id,
            order_id,
            provider_intent_id,
        )

    gateway.webhook_event = {
        "id": f"evt-p0-{uuid4().hex}",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": provider_intent_id,
                "amount_received": 500,
                "currency": "cny",
                "metadata": {"out_trade_no": str(intent_id)},
            }
        },
    }
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.pool = pool
    ProviderRegistry.get().register_generic("payment_gateway", "stripe", gateway, replace=True)
    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://p0.test"
    ) as client:
        responses = await asyncio.gather(
            *(client.post("/payments/stripe/webhook", content=b"signed") for _ in range(6))
        )
    assert [response.status_code for response in responses] == [200] * 6

    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT status FROM payment_intent WHERE id = $1", intent_id)
            == "paid"
        )
        assert (
            await conn.fetchval("SELECT status FROM customer_order WHERE id = $1", order_id)
            == "confirmed"
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM order_status_history WHERE order_id = $1", order_id
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM inventory_reservation WHERE order_id = $1", order_id
            )
            == 1
        )
        assert (
            await conn.fetchval("SELECT reservation_id FROM order_line_item WHERE id = $1", line_id)
            is not None
        )


@pytest.mark.asyncio
async def test_refund_idempotency_partial_and_cumulative_limit(payment_env: Any):
    pool, gateway, settings = payment_env
    _order_id, service, intent = await _make_paid_intent(pool, gateway, settings, total_cents=1000)

    first = await service.request_refund(
        payment_id=intent.intent_id,
        refund_amount=Decimal("7.00"),
        reason="partial",
        idempotency_key="p0-refund-a",
    )
    assert first["status"] == "succeeded"
    assert first["refund_amount_cents"] == 700
    assert len(gateway.refund_calls) == 1

    duplicate = await service.request_refund(
        payment_id=intent.intent_id,
        refund_amount=Decimal("7.00"),
        reason="retry",
        idempotency_key="p0-refund-a",
    )
    assert duplicate["refund_id"] == first["refund_id"]
    assert len(gateway.refund_calls) == 1

    from app.payments.service import RefundConflictError

    with pytest.raises(RefundConflictError, match="exceeds refundable balance"):
        await service.request_refund(
            payment_id=intent.intent_id,
            refund_amount=Decimal("4.00"),
            reason="too much",
            idempotency_key="p0-refund-bad",
        )

    second = await service.request_refund(
        payment_id=intent.intent_id,
        refund_amount=None,
        reason="remaining",
        idempotency_key="p0-refund-full",
    )
    assert second["status"] == "succeeded"
    assert second["refund_amount_cents"] == 300
    assert len(gateway.refund_calls) == 2
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT status FROM payment_intent WHERE id = $1", intent.intent_id)
            == "refunded"
        )
        assert (
            await conn.fetchval(
                "SELECT refund_amount_cents FROM payment_intent WHERE id = $1", intent.intent_id
            )
            == 1000
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM payment_refund WHERE payment_intent_id = $1",
                intent.intent_id,
            )
            == 2
        )


@pytest.mark.asyncio
async def test_duplicate_provider_refund_event_and_refund_db_failure_reconcile(payment_env: Any):
    pool, gateway, settings = payment_env
    _order_id, service, intent = await _make_paid_intent(pool, gateway, settings, total_cents=900)
    refund_id: str | None = None
    # Install the trigger after the refund row exists so only the provider-result
    # persistence phase fails; the provider call itself remains real and durable.
    original_refund = service.request_refund
    # Create the row without processing by temporarily making the provider report
    # an accepted/processing result; the later reconcile changes it to success.
    gateway.refund_status = "pending"
    pending = await original_refund(
        payment_id=intent.intent_id,
        refund_amount=Decimal("9.00"),
        reason="db-failure-reconcile",
        idempotency_key="p0-refund-reconcile",
    )
    refund_id = pending["refund_id"]
    assert pending["status"] == "processing"
    gateway.refund_status = "success"
    function_name, trigger_name = await _install_raising_trigger(
        pool, table="payment_refund", row_id=refund_id, prefix="p0_refund"
    )
    try:
        # Move the retryable outbox back to ready and invoke the provider once;
        # the trigger makes the local success persistence fail and roll back.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE payment_outbox SET next_attempt_at = NOW() WHERE refund_id = $1",
                refund_id,
            )
        failed_persist = await service.reconcile()
        assert failed_persist >= 1
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval("SELECT status FROM payment_refund WHERE id = $1", refund_id)
                == "processing"
            )
            await conn.execute(
                "UPDATE payment_outbox SET locked_at = NOW() - INTERVAL '10 minutes' "
                "WHERE refund_id = $1",
                refund_id,
            )
    finally:
        await _drop_trigger(
            pool,
            table="payment_refund",
            function_name=function_name,
            trigger_name=trigger_name,
        )

    assert await service.reconcile() >= 1
    recovered = await service._read_refund(refund_id)
    assert recovered["status"] == "succeeded"
    target_refund_calls = [
        call
        for call in gateway.refund_calls
        if call["out_refund_no"].endswith(refund_id.replace("-", ""))
    ]
    assert len(target_refund_calls) == 3
    assert len({call["out_refund_no"] for call in target_refund_calls}) == 1

    duplicate = await service.process_refund(refund_id)
    assert duplicate["refund_id"] == refund_id
    target_calls_after_replay = [
        call
        for call in gateway.refund_calls
        if call["out_refund_no"].endswith(refund_id.replace("-", ""))
    ]
    assert len(target_calls_after_replay) == len(target_refund_calls)


@pytest.mark.asyncio
async def test_payment_provider_adapters_fail_closed_and_preserve_amounts(payment_env: Any):
    from app.payments.service import PaymentService, PaymentServiceError

    pool, gateway, settings = payment_env
    service = PaymentService(pool, settings)
    service._gateway = lambda provider: gateway  # type: ignore[method-assign]

    manual = await service._provider_prepay(
        provider="manual",
        out_trade_no="manual-p0",
        total_fee_cents=321,
        currency="CNY",
        description="manual",
        payer_openid=None,
    )
    stripe = await service._provider_prepay(
        provider="stripe",
        out_trade_no="stripe-p0",
        total_fee_cents=654,
        currency="usd",
        description="stripe",
        payer_openid=None,
    )
    assert manual["total_fee_cents"] == 321
    assert stripe["total_fee_cents"] == 654
    assert stripe["currency"] == "USD"

    alipay = _AlipayStub()
    service._gateway = lambda provider: alipay  # type: ignore[method-assign]
    alipay_result = await service._provider_prepay(
        provider="alipay",
        out_trade_no="alipay-p0",
        total_fee_cents=888,
        currency="CNY",
        description="alipay",
        payer_openid=None,
    )
    assert alipay_result == {"qr_code": "alipay://p0", "gateway": "alipay"}

    service._gateway = lambda provider: _AlipayStub(success=False)  # type: ignore[method-assign]
    with pytest.raises(PaymentServiceError, match="rejected"):
        await service._provider_prepay(
            provider="alipay",
            out_trade_no="alipay-bad",
            total_fee_cents=888,
            currency="CNY",
            description="alipay",
            payer_openid=None,
        )

    wechat = _WechatStub()
    service._gateway = lambda provider: wechat  # type: ignore[method-assign]
    wechat_result = await service._provider_prepay(
        provider="wechat",
        out_trade_no="wechat-p0",
        total_fee_cents=999,
        currency="CNY",
        description="wechat",
        payer_openid="openid",
    )
    assert wechat_result["prepay_id"] == "wechat-prepay"
    service._gateway = lambda provider: _WechatStub(success=False)  # type: ignore[method-assign]
    with pytest.raises(PaymentServiceError, match="rejected"):
        await service._provider_prepay(
            provider="wechat",
            out_trade_no="wechat-bad",
            total_fee_cents=999,
            currency="CNY",
            description="wechat",
            payer_openid=None,
        )

    service._gateway = lambda provider: gateway  # type: ignore[method-assign]
    assert (
        await service._provider_refund(
            provider="manual",
            out_trade_no="manual-p0",
            provider_intent_id=None,
            out_refund_no="manual-refund",
            refund_fee_cents=100,
            reason="test",
            total_fee_cents=321,
        )
    )["status"] == "success"
    assert (
        await service._provider_refund(
            provider="stripe",
            out_trade_no="stripe-p0",
            provider_intent_id="pi-p0",
            out_refund_no="stripe-refund",
            refund_fee_cents=200,
            reason="test",
            total_fee_cents=654,
        )
    )["status"] == "success"

    service._gateway = lambda provider: alipay  # type: ignore[method-assign]
    assert (
        await service._provider_refund(
            provider="alipay",
            out_trade_no="alipay-p0",
            provider_intent_id=None,
            out_refund_no="alipay-refund",
            refund_fee_cents=300,
            reason="test",
            total_fee_cents=888,
        )
    )["code"] is None
    service._gateway = lambda provider: _AlipayStub(refund_code="ERR")  # type: ignore[method-assign]
    with pytest.raises(PaymentServiceError, match="rejected"):
        await service._provider_refund(
            provider="alipay",
            out_trade_no="alipay-p0",
            provider_intent_id=None,
            out_refund_no="alipay-bad",
            refund_fee_cents=300,
            reason="test",
            total_fee_cents=888,
        )

    legacy = _LegacyRefundStub()
    service._gateway = lambda provider: legacy  # type: ignore[method-assign]
    legacy_result = await service._provider_refund(
        provider="wechat",
        out_trade_no="wechat-p0",
        provider_intent_id=None,
        out_refund_no="legacy-refund",
        refund_fee_cents=400,
        reason="test",
        total_fee_cents=999,
    )
    assert legacy_result["refund_amount"] == 400
    assert legacy.calls == 1


@pytest.mark.asyncio
async def test_inventory_fifo_exact_allocation_release_deduct_and_invariants(p0_pool: Any):
    from app.inventory.service import InventoryService

    seed = await _seed_stock(p0_pool, (2, 3, 4))
    service = InventoryService(p0_pool)
    order_id = await _insert_order(p0_pool, total_cents=1750)
    allocations = await service.reserve_stock_allocations(
        order_id=order_id,
        product_id=seed.product_id,
        variant_id=seed.variant_id,
        location_id=seed.location_id,
        quantity=7,
        reservation_key=f"p0-fifo:{order_id}",
    )
    assert [allocation["quantity"] for allocation in allocations] == [2, 3, 2]
    assert [allocation["batch_id"] for allocation in allocations] == list(seed.batch_ids)
    assert sum(allocation["quantity"] for allocation in allocations) == 7

    snapshots = await service.get_stock(
        seed.product_id, seed.location_id, variant_id=seed.variant_id
    )
    assert [snapshot.available_qty for snapshot in snapshots] == [0, 0, 2]
    assert (
        await service.get_available_stock(seed.product_id, seed.location_id, seed.variant_id) == 2
    )
    assert await service.check_availability(seed.product_id, seed.location_id, 2, seed.variant_id)
    assert not await service.check_availability(
        seed.product_id, seed.location_id, 3, seed.variant_id
    )

    await service.set_safety_threshold(
        seed.product_id, seed.location_id, 2, variant_id=seed.variant_id
    )
    summary = await service.get_stock_summary(
        product_id=seed.product_id, location_id=seed.location_id, low_stock_only=True
    )
    assert len(summary) == 1
    assert summary[0]["total_qty"] == 9
    assert summary[0]["reserved_qty"] == 7
    assert summary[0]["available_qty"] == 2
    alerts = await service.check_all_safety_stock()
    assert any(alert.product_id == seed.product_id for alert in alerts)

    await service.release_reservation(
        order_id=order_id,
        product_id=seed.product_id,
        variant_id=seed.variant_id,
        location_id=seed.location_id,
        quantity=4,
    )
    # Replaying the same release cannot consume another four units.
    await service.release_reservation(
        order_id=order_id,
        product_id=seed.product_id,
        variant_id=seed.variant_id,
        location_id=seed.location_id,
        quantity=4,
    )
    async with p0_pool.acquire() as conn:
        after_release = await conn.fetch(
            "SELECT batch_id, quantity, consumed_qty, released_qty "
            "FROM inventory_reservation WHERE order_id = $1",
            order_id,
        )
        reserved = await conn.fetch(
            "SELECT id, stock_qty, reserved_qty FROM inventory_batch WHERE id = ANY($1::uuid[])",
            list(seed.batch_ids),
        )
    reservations_by_batch = {str(row["batch_id"]): row for row in after_release}
    batches_by_id = {str(row["id"]): row for row in reserved}
    assert sum(row["released_qty"] for row in after_release) == 4
    for allocation in allocations:
        reservation = reservations_by_batch[allocation["batch_id"]]
        batch = batches_by_id[allocation["batch_id"]]
        assert batch["reserved_qty"] == (
            allocation["quantity"] - reservation["consumed_qty"] - reservation["released_qty"]
        )

    await service.deduct_stock(
        order_id=order_id,
        product_id=seed.product_id,
        variant_id=seed.variant_id,
        location_id=seed.location_id,
        quantity=3,
    )
    # Replaying shipment is also a no-op, not a second physical deduction.
    await service.deduct_stock(
        order_id=order_id,
        product_id=seed.product_id,
        variant_id=seed.variant_id,
        location_id=seed.location_id,
        quantity=3,
    )
    async with p0_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, stock_qty, reserved_qty FROM inventory_batch WHERE id = ANY($1::uuid[])",
            list(seed.batch_ids),
        )
        reservations = await conn.fetch(
            "SELECT batch_id, quantity, consumed_qty, released_qty "
            "FROM inventory_reservation WHERE order_id = $1",
            order_id,
        )
        movements = await conn.fetch(
            "SELECT movement_type, batch_id, quantity FROM stock_movement "
            "WHERE reference_id = $1 ORDER BY created_at, id",
            order_id,
        )
    final_batches = {str(row["id"]): row for row in rows}
    for allocation in allocations:
        reservation = next(
            row for row in reservations if str(row["batch_id"]) == allocation["batch_id"]
        )
        batch = final_batches[allocation["batch_id"]]
        assert (
            batch["stock_qty"]
            == seed.quantities[allocations.index(allocation)] - reservation["consumed_qty"]
        )
        assert batch["reserved_qty"] == (
            allocation["quantity"] - reservation["consumed_qty"] - reservation["released_qty"]
        )
    assert sum(row["consumed_qty"] for row in reservations) == 3
    assert sum(row["reserved_qty"] for row in rows) == 0
    assert sum(row["quantity"] for row in movements if row["movement_type"] == "reserve") == 7
    assert sum(row["quantity"] for row in movements if row["movement_type"] == "unreserve") == 4
    assert sum(row["quantity"] for row in movements if row["movement_type"] == "shipment") == 3
    history = await service.get_movement_history(
        seed.product_id, location_id=seed.location_id, variant_id=seed.variant_id
    )
    assert {row["movement_type"] for row in history} >= {"reserve", "unreserve", "shipment"}

    for row in rows:
        assert row["stock_qty"] >= 0
        assert row["reserved_qty"] >= 0
        assert row["stock_qty"] - row["reserved_qty"] >= 0


@pytest.mark.asyncio
async def test_inventory_idempotency_and_conditional_update_zero_is_failure(p0_pool: Any):
    from app.inventory.service import InventoryService, ReservationConflictError

    seed = await _seed_stock(p0_pool, (5,))
    service = InventoryService(p0_pool)
    order_id = await _insert_order(p0_pool)
    key = f"p0-same-reservation:{order_id}"
    first = await service.reserve_stock_allocations(
        order_id=order_id,
        product_id=seed.product_id,
        variant_id=seed.variant_id,
        location_id=seed.location_id,
        quantity=3,
        reservation_key=key,
    )
    second = await service.reserve_stock_allocations(
        order_id=order_id,
        product_id=seed.product_id,
        variant_id=seed.variant_id,
        location_id=seed.location_id,
        quantity=3,
        reservation_key=key,
    )
    assert second == first
    with pytest.raises(ReservationConflictError, match="already used"):
        await service.reserve_stock_allocations(
            order_id=order_id,
            product_id=seed.product_id,
            variant_id=seed.variant_id,
            location_id=seed.location_id,
            quantity=2,
            reservation_key=key,
        )
    with pytest.raises(ValueError, match="positive"):
        await service.reserve_stock_allocations(
            order_id=order_id,
            product_id=seed.product_id,
            variant_id=seed.variant_id,
            location_id=seed.location_id,
            quantity=0,
        )

    skip_seed = await _seed_stock(p0_pool, (5,))
    function_name, trigger_name = await _install_skip_update_trigger(
        p0_pool,
        table="inventory_batch",
        row_id=skip_seed.batch_ids[0],
        prefix="p0_inventory_zero",
    )
    try:
        with pytest.raises(ReservationConflictError, match="changed while reserving"):
            await service.reserve_stock_allocations(
                order_id=await _insert_order(p0_pool),
                product_id=skip_seed.product_id,
                variant_id=skip_seed.variant_id,
                location_id=skip_seed.location_id,
                quantity=1,
                reservation_key=f"p0-row-zero:{uuid4()}",
            )
    finally:
        await _drop_trigger(
            p0_pool,
            table="inventory_batch",
            function_name=function_name,
            trigger_name=trigger_name,
        )
    async with p0_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT stock_qty, reserved_qty FROM inventory_batch WHERE id = $1",
            skip_seed.batch_ids[0],
        )
        assert row["stock_qty"] == 5
        assert row["reserved_qty"] == 0
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM inventory_reservation WHERE batch_id = $1",
                skip_seed.batch_ids[0],
            )
            == 0
        )


@pytest.mark.asyncio
async def test_inventory_concurrent_120_reservations_cannot_oversell(p0_pool: Any):
    from app.inventory.service import InsufficientStockError, InventoryService

    seed = await _seed_stock(p0_pool, (37,))
    service = InventoryService(p0_pool)
    order_ids = [await _insert_order(p0_pool) for _ in range(120)]

    async def reserve(order_id: str) -> Any:
        try:
            return await service.reserve_stock_allocations(
                order_id=order_id,
                product_id=seed.product_id,
                variant_id=seed.variant_id,
                location_id=seed.location_id,
                quantity=1,
                reservation_key=f"p0-stress:{order_id}",
            )
        except Exception as exc:  # gather preserves every concurrent outcome
            return exc

    results = await asyncio.gather(*(reserve(order_id) for order_id in order_ids))
    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(successes) == 37
    assert len(failures) == 83
    assert all(isinstance(result, InsufficientStockError) for result in failures)

    async with p0_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT stock_qty, reserved_qty FROM inventory_batch WHERE id = $1",
            seed.batch_ids[0],
        )
        assert row["stock_qty"] == 37
        assert row["reserved_qty"] == 37
        assert row["stock_qty"] - row["reserved_qty"] == 0
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM inventory_reservation WHERE batch_id = $1",
                seed.batch_ids[0],
            )
            == 37
        )


@pytest.mark.asyncio
async def test_inventory_receive_and_query_filters_preserve_ledger(p0_pool: Any):
    from app.inventory.models import StockMovementType
    from app.inventory.service import InventoryService

    seed = await _seed_stock(p0_pool, (4,))
    service = InventoryService(p0_pool)
    movement_id = await service.receive_stock(
        product_id=seed.product_id,
        variant_id=seed.variant_id,
        location_id=seed.location_id,
        batch_id=seed.batch_ids[0],
        quantity=3,
        movement_type=StockMovementType.RETURN_IN,
        reference_type="return",
        reference_id=f"p0-return-{uuid4()}",
        reason="test return",
    )
    assert movement_id
    snapshots = await service.get_stock(seed.product_id, seed.location_id)
    assert len(snapshots) == 1
    assert snapshots[0].total_qty == 7
    summary = await service.get_stock_summary(product_id=seed.product_id)
    assert summary[0]["batch_count"] == 1
    all_history = await service.get_movement_history(seed.product_id, limit=100)
    assert any(str(row["id"]) == movement_id for row in all_history)
    assert await service.check_availability(seed.product_id, seed.location_id, 7)


@pytest.mark.asyncio
async def test_order_history_and_ship_use_exact_reservation_allocation(p0_pool: Any):
    from app.orders.models import OrderStatus
    from app.orders.service import OrderService

    seed, order_id, line_id, reservation_id = await _prepare_reserved_order(
        p0_pool, quantity=3, status="pending", total_cents=750
    )
    service = OrderService(p0_pool)
    assert (await service.get_order(order_id)).status == OrderStatus.PENDING
    assert str((await service.get_order_items(order_id))[0]["batch_id"]) == seed.batch_ids[0]

    confirmed = await service.confirm_order(order_id, payment_intent_id="p0-payment")
    assert confirmed.status == OrderStatus.CONFIRMED
    await service.transition_status(order_id, OrderStatus.PROCESSING)
    await service.transition_status(order_id, OrderStatus.PACKED)
    shipped = await service.ship_order(order_id, tracking_number="TRACK-P0", carrier="P0-carrier")
    assert shipped.status == OrderStatus.SHIPPED
    delivered = await service.deliver_order(order_id)
    completed = await service.complete_order(order_id)
    assert delivered.status == OrderStatus.DELIVERED
    assert completed.status == OrderStatus.COMPLETED
    assert completed.version == 6

    history = await service.get_status_history(order_id)
    assert [(entry.from_status, entry.to_status) for entry in history] == [
        ("pending", "confirmed"),
        ("confirmed", "processing"),
        ("processing", "packed"),
        ("packed", "shipped"),
        ("shipped", "delivered"),
        ("delivered", "completed"),
    ]
    assert history[3].metadata == {"tracking_number": "TRACK-P0", "carrier": "P0-carrier"}

    async with p0_pool.acquire() as conn:
        batch = await conn.fetchrow(
            "SELECT stock_qty, reserved_qty FROM inventory_batch WHERE id = $1",
            seed.batch_ids[0],
        )
        reservation = await conn.fetchrow(
            "SELECT status, quantity, consumed_qty, released_qty "
            "FROM inventory_reservation WHERE id = $1",
            reservation_id,
        )
        line = await conn.fetchrow(
            "SELECT fulfilled_qty, reservation_id, location_id FROM order_line_item WHERE id = $1",
            line_id,
        )
        assert batch["stock_qty"] == 5
        assert batch["reserved_qty"] == 0
        assert dict(reservation) == {
            "status": "consumed",
            "quantity": 3,
            "consumed_qty": 3,
            "released_qty": 0,
        }
        assert line["fulfilled_qty"] == 3
        assert str(line["reservation_id"]) == reservation_id
        assert str(line["location_id"]) == seed.location_id
        assert (
            await conn.fetchval(
                "SELECT SUM(quantity) FROM stock_movement "
                "WHERE reference_id = $1 AND movement_type = 'shipment'",
                order_id,
            )
            == 3
        )

    assert await service.count_orders(status=OrderStatus.COMPLETED) >= 1
    assert (await service.get_order_stats())["completed"] >= 1


@pytest.mark.asyncio
async def test_confirm_cancel_concurrency_allows_one_legal_transition(p0_pool: Any):
    from app.orders.models import ConcurrentOrderTransitionError, OrderStatus
    from app.orders.service import OrderService

    _seed, order_id, _line_id, _reservation_id = await _prepare_reserved_order(
        p0_pool, quantity=2, status="pending", total_cents=500
    )
    service = OrderService(p0_pool)
    results = await asyncio.gather(
        service.transition_status(order_id, OrderStatus.CONFIRMED, expected_version=0),
        service.transition_status(
            order_id, OrderStatus.CANCELLED, reason="race", expected_version=0
        ),
        return_exceptions=True,
    )
    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ConcurrentOrderTransitionError)

    async with p0_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, version FROM customer_order WHERE id = $1", order_id
        )
        assert row["status"] in {"confirmed", "cancelled"}
        assert row["version"] == 1
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM order_status_history WHERE order_id = $1", order_id
            )
            == 1
        )
        movement_types = await conn.fetch(
            "SELECT movement_type FROM stock_movement WHERE reference_id = $1", order_id
        )
    if row["status"] == "cancelled":
        assert [record["movement_type"] for record in movement_types].count("unreserve") == 1
    else:
        assert [record["movement_type"] for record in movement_types].count("unreserve") == 0


@pytest.mark.asyncio
async def test_ship_cancel_concurrency_has_one_terminal_inventory_side_effect(p0_pool: Any):
    from app.orders.service import OrderService

    seed, order_id, _line_id, reservation_id = await _prepare_reserved_order(
        p0_pool, quantity=2, status="packed", total_cents=500
    )
    service = OrderService(p0_pool)
    results = await asyncio.gather(
        service.ship_order(order_id, tracking_number="race-ship"),
        service.cancel_order(order_id, reason="race-cancel"),
        return_exceptions=True,
    )
    successes = [result for result in results if not isinstance(result, Exception)]
    assert len(successes) == 1
    assert sum(not isinstance(result, Exception) for result in results) == 1
    final_status = (await service.get_order(order_id)).status.value
    assert final_status in {"shipped", "cancelled"}

    async with p0_pool.acquire() as conn:
        batch = await conn.fetchrow(
            "SELECT stock_qty, reserved_qty FROM inventory_batch WHERE id = $1",
            seed.batch_ids[0],
        )
        reservation = await conn.fetchrow(
            "SELECT status, consumed_qty, released_qty FROM inventory_reservation WHERE id = $1",
            reservation_id,
        )
        movements = await conn.fetch(
            "SELECT movement_type, quantity FROM stock_movement WHERE reference_id = $1",
            order_id,
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM order_status_history WHERE order_id = $1", order_id
            )
            == 1
        )
    if final_status == "shipped":
        assert (batch["stock_qty"], batch["reserved_qty"]) == (5, 0)
        assert dict(reservation) == {"status": "consumed", "consumed_qty": 2, "released_qty": 0}
        assert [row["movement_type"] for row in movements].count("shipment") == 1
        assert [row["movement_type"] for row in movements].count("unreserve") == 0
    else:
        assert (batch["stock_qty"], batch["reserved_qty"]) == (7, 0)
        assert dict(reservation) == {"status": "released", "consumed_qty": 0, "released_qty": 2}
        assert [row["movement_type"] for row in movements].count("shipment") == 0
        assert [row["movement_type"] for row in movements].count("unreserve") == 1


@pytest.mark.asyncio
async def test_order_transition_rolls_back_inventory_when_history_persist_fails(p0_pool: Any):
    from app.orders.service import OrderService

    seed, order_id, _line_id, reservation_id = await _prepare_reserved_order(
        p0_pool, quantity=2, status="packed", total_cents=500
    )
    function_name, trigger_name = await _install_raising_trigger(
        p0_pool,
        table="order_status_history",
        row_id=str(uuid4()),
        prefix="p0_order_history_unused",
    )
    await _drop_trigger(
        p0_pool,
        table="order_status_history",
        function_name=function_name,
        trigger_name=trigger_name,
    )
    # The trigger is installed after the order id is known and matches every
    # history insert; this makes the final write fail after inventory mutation.
    function_name = f"p0_order_fail_fn_{uuid4().hex[:12]}"
    trigger_name = f"p0_order_fail_trg_{uuid4().hex[:12]}"
    async with p0_pool.acquire() as conn:
        await conn.execute(
            f'''
            CREATE FUNCTION "{function_name}"() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'P0 order history persistence failure';
            END;
            $$;
            CREATE TRIGGER "{trigger_name}"
            BEFORE INSERT ON order_status_history
            FOR EACH ROW EXECUTE FUNCTION "{function_name}"();
            '''
        )
    try:
        with pytest.raises(Exception, match="P0 order history persistence failure"):
            await OrderService(p0_pool).ship_order(order_id)
    finally:
        await _drop_trigger(
            p0_pool,
            table="order_status_history",
            function_name=function_name,
            trigger_name=trigger_name,
        )

    async with p0_pool.acquire() as conn:
        order = await conn.fetchrow(
            "SELECT status, version FROM customer_order WHERE id = $1", order_id
        )
        batch = await conn.fetchrow(
            "SELECT stock_qty, reserved_qty FROM inventory_batch WHERE id = $1",
            seed.batch_ids[0],
        )
        reservation = await conn.fetchrow(
            "SELECT status, consumed_qty, released_qty FROM inventory_reservation WHERE id = $1",
            reservation_id,
        )
        assert order["status"] == "packed"
        assert order["version"] == 0
        assert (batch["stock_qty"], batch["reserved_qty"]) == (7, 2)
        assert dict(reservation) == {"status": "reserved", "consumed_qty": 0, "released_qty": 0}
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM stock_movement WHERE reference_id = $1 "
                "AND movement_type = 'shipment'",
                order_id,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_order_ship_fails_closed_without_reservation(p0_pool: Any):
    from app.orders.service import OrderInventoryConflictError, OrderService

    seed = await _seed_stock(p0_pool, (5,))
    order_id = await _insert_order(p0_pool, status="packed", total_cents=250)
    await _insert_line(p0_pool, order_id=order_id, batch_id=seed.batch_ids[0], quantity=1)
    with pytest.raises(OrderInventoryConflictError, match="no inventory reservations"):
        await OrderService(p0_pool).ship_order(order_id)
    async with p0_pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT status FROM customer_order WHERE id = $1", order_id)
            == "packed"
        )
        assert (
            await conn.fetchval(
                "SELECT COUNT(*) FROM order_status_history WHERE order_id = $1", order_id
            )
            == 0
        )


@pytest.mark.asyncio
async def test_order_expected_version_cas_rejects_stale_transition(p0_pool: Any):
    from app.orders.models import ConcurrentOrderTransitionError, OrderStatus
    from app.orders.service import OrderService

    order_id = await _insert_order(p0_pool, status="pending", total_cents=250)
    service = OrderService(p0_pool)
    await service.transition_status(order_id, OrderStatus.CONFIRMED, expected_version=0)
    with pytest.raises(ConcurrentOrderTransitionError):
        await service.transition_status(order_id, OrderStatus.CANCELLED, expected_version=0)
    with pytest.raises(Exception, match="invalid order transition"):
        await service.transition_status(order_id, OrderStatus.PENDING)


@pytest.mark.asyncio
async def test_order_queries_and_cancel_release_exact_reservation(p0_pool: Any):
    from app.orders.models import OrderStatus
    from app.orders.service import OrderNotFoundError, OrderService

    seed, order_id, line_id, reservation_id = await _prepare_reserved_order(
        p0_pool, quantity=2, status="pending", total_cents=500
    )
    service = OrderService(p0_pool)
    assert await service.get_order(str(uuid4())) is None
    order = await service.get_order(order_id)
    assert order is not None
    assert order.status == OrderStatus.PENDING
    assert order.grand_total_cents == 500
    assert len(await service.get_order_items(order_id)) == 1
    assert any(item.id == order_id for item in await service.list_orders())
    filtered_customer = str(uuid4())
    filtered_order = await _insert_order(
        p0_pool, customer_id=filtered_customer, status="pending", total_cents=125
    )
    filtered = await service.list_orders(customer_id=filtered_customer, status=OrderStatus.PENDING)
    assert [item.id for item in filtered] == [filtered_order]
    missing_customer = str(uuid4())
    assert await service.count_orders(customer_id=missing_customer) == 0
    assert await service.count_orders(status=OrderStatus.PENDING) >= 1
    assert await service.get_order_stats()
    assert await service.get_order_stats(customer_id=missing_customer) == {}

    with pytest.raises(OrderNotFoundError):
        await service.transition_status(str(uuid4()), OrderStatus.CANCELLED)

    cancelled = await service.cancel_order(order_id, reason="p0 exact release")
    assert cancelled.status == OrderStatus.CANCELLED
    history = await service.get_status_history(order_id)
    assert [(entry.from_status, entry.to_status) for entry in history] == [("pending", "cancelled")]
    async with p0_pool.acquire() as conn:
        batch = await conn.fetchrow(
            "SELECT stock_qty, reserved_qty FROM inventory_batch WHERE id = $1",
            seed.batch_ids[0],
        )
        reservation = await conn.fetchrow(
            "SELECT status, quantity, consumed_qty, released_qty "
            "FROM inventory_reservation WHERE id = $1",
            reservation_id,
        )
        line = await conn.fetchrow(
            "SELECT reservation_id, location_id FROM order_line_item WHERE id = $1",
            line_id,
        )
        assert (batch["stock_qty"], batch["reserved_qty"]) == (7, 0)
        assert dict(reservation) == {
            "status": "released",
            "quantity": 2,
            "consumed_qty": 0,
            "released_qty": 2,
        }
        assert str(line["reservation_id"]) == reservation_id
        assert str(line["location_id"]) == seed.location_id
        assert (
            await conn.fetchval(
                "SELECT SUM(quantity) FROM stock_movement "
                "WHERE reference_id = $1 AND movement_type = 'unreserve'",
                order_id,
            )
            == 2
        )
