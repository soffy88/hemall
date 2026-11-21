"""售后/退款状态机 — 单元 + PG 集成测试 (需真实 Postgres TEST_PG_DSN)。

覆盖核验要求:
  - 所有合法/非法状态转换 (纯单元)
  - 部分退款累计不超过实付 (PG)
  - 并发两次退款只有一笔成功 (PG, 行锁串行化)
  - 退货回补库存 + 重复收货不双倍回补 (PG)
  - 幂等: provider_refund_no 唯一 + 回调重放不动状态 (PG)
  - 网关退款回调验签: 自生成密钥, 伪造签名被拒 (PG)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.payments.refund_models import (
    InvalidRefundTransitionError,
    RefundReason,
    RefundRequest,
    RefundStatus,
    RefundType,
    validate_refund_transition,
)

TEST_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set; skipping DB integration")

# reviewer_id/operator_id 是 uuid 列, 测试也得用真 UUID
REVIEWER = str(uuid.uuid4())
REVIEWER_A = str(uuid.uuid4())
REVIEWER_B = str(uuid.uuid4())


# ── 纯单元: 状态转换矩阵 ────────────────────────────────────────────────


class TestRefundTransitionMatrix:
    """VALID_REFUND_TRANSITIONS 全矩阵: 合法通过, 非法抛错。"""

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (RefundStatus.SUBMITTED, RefundStatus.APPROVED),
            (RefundStatus.SUBMITTED, RefundStatus.REJECTED),
            (RefundStatus.APPROVED, RefundStatus.REFUNDING),
            (RefundStatus.REFUNDING, RefundStatus.SUCCEEDED),
            (RefundStatus.REFUNDING, RefundStatus.FAILED),
            (RefundStatus.FAILED, RefundStatus.REFUNDING),  # 失败可重试
        ],
    )
    def test_legal_transitions(self, from_status, to_status):
        refund = RefundRequest(
            id=str(uuid.uuid4()),
            order_id=str(uuid.uuid4()),
            customer_id=str(uuid.uuid4()),
            requested_amount=100,
            status=from_status,
        )
        refund.transition_to(to_status)  # 不抛错即通过
        assert refund.status == to_status

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (RefundStatus.SUBMITTED, RefundStatus.REFUNDING),
            (RefundStatus.SUBMITTED, RefundStatus.SUCCEEDED),
            (RefundStatus.SUBMITTED, RefundStatus.FAILED),
            (RefundStatus.APPROVED, RefundStatus.SUCCEEDED),
            (RefundStatus.APPROVED, RefundStatus.REJECTED),
            (RefundStatus.REFUNDING, RefundStatus.APPROVED),
            (RefundStatus.REJECTED, RefundStatus.APPROVED),
            (RefundStatus.REJECTED, RefundStatus.SUBMITTED),
            (RefundStatus.SUCCEEDED, RefundStatus.REFUNDING),
            (RefundStatus.FAILED, RefundStatus.SUCCEEDED),
        ],
    )
    def test_illegal_transitions_raise(self, from_status, to_status):
        refund = RefundRequest(
            id=str(uuid.uuid4()),
            order_id=str(uuid.uuid4()),
            customer_id=str(uuid.uuid4()),
            requested_amount=100,
            status=from_status,
        )
        with pytest.raises(InvalidRefundTransitionError):
            refund.transition_to(to_status)
        assert refund.status == from_status  # 失败后状态不变

    def test_terminal_states(self):
        for status in (RefundStatus.REJECTED, RefundStatus.SUCCEEDED):
            refund = RefundRequest(
                id=str(uuid.uuid4()),
                order_id=str(uuid.uuid4()),
                customer_id=str(uuid.uuid4()),
                requested_amount=100,
                status=status,
            )
            assert refund.is_terminal()
            assert not refund.can_retry_refund()

    def test_failed_is_not_terminal_and_can_retry(self):
        refund = RefundRequest(
            id=str(uuid.uuid4()),
            order_id=str(uuid.uuid4()),
            customer_id=str(uuid.uuid4()),
            requested_amount=100,
            status=RefundStatus.FAILED,
        )
        assert not refund.is_terminal()
        assert refund.can_retry_refund()

    def test_failed_can_retry_but_others_cannot(self):
        refund = RefundRequest(
            id=str(uuid.uuid4()),
            order_id=str(uuid.uuid4()),
            customer_id=str(uuid.uuid4()),
            requested_amount=100,
            status=RefundStatus.FAILED,
        )
        assert refund.can_retry_refund()
        assert not RefundRequest(
            id=str(uuid.uuid4()),
            order_id=str(uuid.uuid4()),
            customer_id=str(uuid.uuid4()),
            requested_amount=100,
        ).can_retry_refund()

    def test_validate_function_directly(self):
        rid = str(uuid.uuid4())
        validate_refund_transition(rid, RefundStatus.SUBMITTED, RefundStatus.APPROVED)
        with pytest.raises(InvalidRefundTransitionError):
            validate_refund_transition(rid, RefundStatus.SUCCEEDED, RefundStatus.APPROVED)


# ── PG 集成 fixture ─────────────────────────────────────────────────────


@pytest.fixture
async def cn_pool():
    from db_test_utils import cleanup_db_test_rows
    from obase.persistence.pool import PgPool

    pool = await PgPool.create(name="hemall_refund_test", dsn=TEST_DSN, min_size=1, max_size=5)
    # 先清售后 (refund_request FK 引用 order_line_item, 顺序反了 cleanup 会炸)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM refund_audit_log")
        await conn.execute("DELETE FROM refund_request")
    await cleanup_db_test_rows(pool)
    yield pool
    await pool.close()


async def _seed_paid_order(pool, *, paid_cents: int = 10_000, grand_total_cents: int = 10_000):
    """种一个已支付订单 (含顾客/商品/批次/订单行/payment_session), 返回 ids。"""
    from obase.uuid7 import uuid7

    async with pool.acquire() as conn:
        cust_id = await conn.fetchval(
            "INSERT INTO customer (id, email, name, status) "
            "VALUES ($1,$2,'rf','active') RETURNING id",
            uuid7(),
            f"rf-{uuid7()}@test.dev",
        )
        loc_id = await conn.fetchval(
            "INSERT INTO stock_location "
            "(id, name, region_code, host_id, address, lat, lng, status) "
            "VALUES ($1,'rf_loc','cn-east','host_rf','测试车库',31.0,121.0,'active') RETURNING id",
            uuid7(),
        )
        prod_id = await conn.fetchval(
            "INSERT INTO product (id, title, slug, status) VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            "售后测试商品",
            f"rf-slug-{uuid7()}",
        )
        variant_id = await conn.fetchval(
            "INSERT INTO product_variant (id, product_id, sku_code, status) "
            "VALUES ($1,$2,$3,'active') RETURNING id",
            uuid7(),
            prod_id,
            f"SKU-{uuid7()}",
        )
        batch_id = await conn.fetchval(
            "INSERT INTO inventory_batch (id, variant_id, location_id, supplier_id, "
            "batch_no, video_url, cost_price_cents, stock_qty, reserved_qty, "
            "retail_price_cents, expiration_time, status) "
            "VALUES ($1,$2,$3,NULL,$4,'https://video.example/rf.mp4',500,10,0,$5,$6,'active') "
            "RETURNING id",
            uuid7(),
            variant_id,
            loc_id,
            f"BATCH-{uuid7()}",
            paid_cents,
            datetime.now(UTC) + timedelta(days=30),
        )
        order_id = await conn.fetchval(
            "INSERT INTO customer_order (id, customer_id, status, currency, region_code, "
            "shipping_cents, tax_cents, grand_total_cents) "
            "VALUES ($1,$2,'paid','cny','cn-east',0,0,$3) RETURNING id",
            uuid7(),
            cust_id,
            grand_total_cents,
        )
        line_id = await conn.fetchval(
            "INSERT INTO order_line_item (id, order_id, batch_id, quantity, "
            "unit_price_cents, line_total_cents) "
            "VALUES ($1,$2,$3,1,$4,$5) RETURNING id",
            uuid7(),
            order_id,
            batch_id,
            paid_cents,
            paid_cents,
        )
        # 实付记录落在 payment_intent (app 层支付意图, 回调成功置 paid)。
        # 共享 schema 的 payment_session 状态机不含 'paid', 不能用它种"已支付"。
        await conn.execute(
            "INSERT INTO payment_intent (id, order_id, amount_cents, currency, "
            "provider, status, provider_trade_no, refund_amount_cents, metadata) "
            "VALUES ($1,$2,$3,'cny','manual','paid',$4,0,'{}')",
            uuid7(),
            str(order_id),
            paid_cents,
            f"trade-{uuid7()}",
        )
        return {
            "customer_id": str(cust_id),
            "order_id": str(order_id),
            "line_id": str(line_id),
            "batch_id": str(batch_id),
        }


async def _create(pool, ids, amount, *, line_id=None, type_=None, reason=None, reason_detail=None):
    from app.payments.refund_service import create_refund_request

    return await create_refund_request(
        pool,
        order_id=ids["order_id"],
        customer_id=ids["customer_id"],
        type=type_ or RefundType.REFUND_ONLY,
        reason=reason or RefundReason.OTHER,
        reason_detail=reason_detail,
        requested_amount=amount,
        order_line_id=line_id,
    )


# ── PG 集成: 申请与金额约束 ────────────────────────────────────────────


class TestRefundCreation:
    async def test_create_refund_happy_path(self, cn_pool):
        from app.payments.refund_service import create_refund_request

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        refund = await create_refund_request(
            cn_pool,
            order_id=ids["order_id"],
            customer_id=ids["customer_id"],
            type=RefundType.REFUND_ONLY,
            reason=RefundReason.PRODUCT_ISSUE,
            reason_detail="商品破损",
            requested_amount=4_000,
        )
        assert refund.status == RefundStatus.SUBMITTED
        assert refund.requested_amount == 4_000
        assert refund.provider == "manual"
        assert refund.provider_refund_no and refund.provider_refund_no.startswith("refund_")

    async def test_create_refund_rejects_amount_over_paid(self, cn_pool):
        from app.payments.refund_service import (
            RefundAmountExceedsError,
            create_refund_request,
        )

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        with pytest.raises(RefundAmountExceedsError):
            await create_refund_request(
                cn_pool,
                order_id=ids["order_id"],
                customer_id=ids["customer_id"],
                type=RefundType.REFUND_ONLY,
                reason=RefundReason.OTHER,
                reason_detail=None,
                requested_amount=10_001,
            )

    async def test_partial_refunds_cumulative_not_exceeding_paid(self, cn_pool):
        """两笔部分退款 4000+4000 可以; 第三笔 3000 被拒 (累计 11000 > 10000)。"""
        from app.payments.refund_service import (
            RefundAmountExceedsError,
            approve_refund,
            create_refund_request,
        )

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        for amount in (4_000, 4_000):
            r = await create_refund_request(
                cn_pool,
                order_id=ids["order_id"],
                customer_id=ids["customer_id"],
                type=RefundType.REFUND_ONLY,
                reason=RefundReason.OTHER,
                reason_detail=None,
                requested_amount=amount,
            )
            # 逐笔审批完成, 释放"进行中"互斥, 但累计额度继续生效
            await approve_refund(cn_pool, r.id, REVIEWER)
        with pytest.raises(RefundAmountExceedsError):
            await create_refund_request(
                cn_pool,
                order_id=ids["order_id"],
                customer_id=ids["customer_id"],
                type=RefundType.REFUND_ONLY,
                reason=RefundReason.OTHER,
                reason_detail=None,
                requested_amount=3_000,
            )
        # 剩余额度 2000 仍可退
        refund = await create_refund_request(
            cn_pool,
            order_id=ids["order_id"],
            customer_id=ids["customer_id"],
            type=RefundType.REFUND_ONLY,
            reason=RefundReason.OTHER,
            reason_detail=None,
            requested_amount=2_000,
        )
        assert refund.status == RefundStatus.SUBMITTED

    async def test_create_refund_rejects_foreign_order(self, cn_pool):
        from app.payments.refund_service import (
            RefundNotEligibleError,
            create_refund_request,
        )

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        with pytest.raises(RefundNotEligibleError):
            await create_refund_request(
                cn_pool,
                order_id=ids["order_id"],
                customer_id=str(uuid.uuid4()),  # 别人的订单
                type=RefundType.REFUND_ONLY,
                reason=RefundReason.OTHER,
                reason_detail=None,
                requested_amount=1_000,
            )

    async def test_duplicate_in_progress_refund_rejected(self, cn_pool):
        from app.payments.refund_service import RefundError, create_refund_request

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        await create_refund_request(
            cn_pool,
            order_id=ids["order_id"],
            customer_id=ids["customer_id"],
            type=RefundType.REFUND_ONLY,
            reason=RefundReason.OTHER,
            reason_detail=None,
            requested_amount=1_000,
        )
        with pytest.raises(RefundError):
            await create_refund_request(
                cn_pool,
                order_id=ids["order_id"],
                customer_id=ids["customer_id"],
                type=RefundType.REFUND_ONLY,
                reason=RefundReason.OTHER,
                reason_detail=None,
                requested_amount=1_000,
            )


# ── PG 集成: 审核/执行/状态推进 ─────────────────────────────────────────


class TestRefundApprovalFlow:
    async def test_approve_triggers_gateway_refund_to_succeeded(self, cn_pool):
        """approve → 自动 execute_refund → manual 网关成功 → succeeded。"""
        from app.payments.refund_service import (
            _get_refund_request,
            approve_refund,
            create_refund_request,
            get_refund_audit_logs,
        )

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        refund = await create_refund_request(
            cn_pool,
            order_id=ids["order_id"],
            customer_id=ids["customer_id"],
            type=RefundType.REFUND_ONLY,
            reason=RefundReason.OTHER,
            reason_detail=None,
            requested_amount=4_000,
        )
        approved = await approve_refund(cn_pool, refund.id, REVIEWER)
        assert approved.status == RefundStatus.SUCCEEDED
        assert approved.approved_amount == 4_000
        assert approved.actual_refunded_amount == 4_000
        assert approved.reviewer_id == REVIEWER

        final = await _get_refund_request(cn_pool, refund.id)
        assert final.status == RefundStatus.SUCCEEDED
        assert final.actual_refunded_amount == 4_000

        logs = await get_refund_audit_logs(cn_pool, refund.id)
        transitions = [(x.from_status, x.to_status) for x in logs]
        assert (RefundStatus.SUBMITTED, RefundStatus.APPROVED) in transitions
        assert (RefundStatus.APPROVED, RefundStatus.REFUNDING) in transitions
        assert (RefundStatus.REFUNDING, RefundStatus.SUCCEEDED) in transitions

    async def test_approve_with_partial_amount(self, cn_pool):
        from app.payments.refund_service import approve_refund, create_refund_request

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        refund = await create_refund_request(
            cn_pool,
            order_id=ids["order_id"],
            customer_id=ids["customer_id"],
            type=RefundType.REFUND_ONLY,
            reason=RefundReason.OTHER,
            reason_detail=None,
            requested_amount=8_000,
        )
        approved = await approve_refund(cn_pool, refund.id, REVIEWER, approved_amount=5_000)
        assert approved.status == RefundStatus.SUCCEEDED
        assert approved.approved_amount == 5_000
        assert approved.actual_refunded_amount == 5_000

    async def test_approve_rejects_amount_over_requested(self, cn_pool):
        from app.payments.refund_service import (
            RefundAmountExceedsError,
            approve_refund,
            create_refund_request,
        )

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        refund = await create_refund_request(
            cn_pool,
            order_id=ids["order_id"],
            customer_id=ids["customer_id"],
            type=RefundType.REFUND_ONLY,
            reason=RefundReason.OTHER,
            reason_detail=None,
            requested_amount=3_000,
        )
        with pytest.raises(RefundAmountExceedsError):
            await approve_refund(cn_pool, refund.id, REVIEWER, approved_amount=3_001)

    async def test_failed_refund_can_be_retried(self, cn_pool):
        """网关退款失败 → failed; 后台重试 → 网关恢复 → succeeded。终态不可重试。"""
        from app.payments.refund_service import (
            GatewayRefundError,
            RefundError,
            _get_refund_request,
            approve_refund,
            create_refund_request,
            retry_refund,
        )

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        refund = await create_refund_request(
            cn_pool,
            order_id=ids["order_id"],
            customer_id=ids["customer_id"],
            type=RefundType.REFUND_ONLY,
            reason=RefundReason.OTHER,
            reason_detail=None,
            requested_amount=3_000,
        )

        class FailingGateway:
            async def refund(self, **kwargs):
                return {"status": "FAILED"}

        with patch(
            "app.payments.refund_service.build_payment_gateway",
            return_value=("manual", FailingGateway()),
        ):
            with pytest.raises(GatewayRefundError):
                await approve_refund(cn_pool, refund.id, REVIEWER)
        final = await _get_refund_request(cn_pool, refund.id)
        assert final.status == RefundStatus.FAILED

        class OkGateway:
            async def refund(self, **kwargs):
                return {"status": "success", "refund_id": "retry-ok"}

        with patch(
            "app.payments.refund_service.build_payment_gateway",
            return_value=("manual", OkGateway()),
        ):
            retried = await retry_refund(cn_pool, refund.id)
        assert retried.status == RefundStatus.SUCCEEDED
        assert retried.actual_refunded_amount == 3_000

        with pytest.raises(RefundError):
            await retry_refund(cn_pool, refund.id)

    async def test_reject_flow(self, cn_pool):
        from app.payments.refund_service import (
            RefundError,
            approve_refund,
            create_refund_request,
            reject_refund,
        )

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        refund = await create_refund_request(
            cn_pool,
            order_id=ids["order_id"],
            customer_id=ids["customer_id"],
            type=RefundType.REFUND_ONLY,
            reason=RefundReason.OTHER,
            reason_detail=None,
            requested_amount=2_000,
        )
        rejected = await reject_refund(cn_pool, refund.id, REVIEWER, "商品无问题")
        assert rejected.status == RefundStatus.REJECTED
        assert rejected.reject_reason == "商品无问题"

        # 驳回是终态, 不能再 approve
        with pytest.raises(RefundError):
            await approve_refund(cn_pool, refund.id, REVIEWER)

    async def test_second_in_progress_refund_allowed_on_different_line(self, cn_pool):
        """两笔不同订单行的部分退款可以并存 (不同 order_line_id)。"""
        from obase.uuid7 import uuid7

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        async with cn_pool.acquire() as conn:
            line2 = await conn.fetchval(
                "INSERT INTO order_line_item (id, order_id, batch_id, quantity, "
                "unit_price_cents, line_total_cents) VALUES ($1,$2,$3,1,5000,5000) RETURNING id",
                uuid7(),
                ids["order_id"],
                ids["batch_id"],
            )
        r1 = await _create(cn_pool, ids, ids["line_id"], 4_000)
        r2 = await _create(cn_pool, ids, str(line2), 4_000)
        assert r1.status == RefundStatus.SUBMITTED
        assert r2.status == RefundStatus.SUBMITTED


async def _create(pool, ids, line_id, amount):
    from app.payments.refund_service import create_refund_request

    return await create_refund_request(
        pool,
        order_id=ids["order_id"],
        customer_id=ids["customer_id"],
        type=RefundType.REFUND_ONLY,
        reason=RefundReason.OTHER,
        reason_detail=None,
        order_line_id=line_id,
        requested_amount=amount,
    )


# ── PG 集成: 并发 ───────────────────────────────────────────────────────


class TestRefundConcurrency:
    async def test_concurrent_approvals_only_one_succeeds(self, cn_pool):
        """并发两次 approve 同一售后单: 行锁串行化, 只有一笔成功。"""
        from app.payments.refund_service import (
            RefundError,
            _get_refund_request,
            approve_refund,
            create_refund_request,
        )

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        refund = await create_refund_request(
            cn_pool,
            order_id=ids["order_id"],
            customer_id=ids["customer_id"],
            type=RefundType.REFUND_ONLY,
            reason=RefundReason.OTHER,
            reason_detail=None,
            requested_amount=5_000,
        )

        results = await asyncio.gather(
            approve_refund(cn_pool, refund.id, REVIEWER_A),
            approve_refund(cn_pool, refund.id, REVIEWER_B),
            return_exceptions=True,
        )
        successes = [r for r in results if not isinstance(r, BaseException)]
        failures = [r for r in results if isinstance(r, RefundError)]
        assert len(successes) == 1
        assert len(failures) == 1
        final = await _get_refund_request(cn_pool, refund.id)
        assert final.status == RefundStatus.SUCCEEDED
        assert final.actual_refunded_amount == 5_000  # 只退了一笔

    async def test_concurrent_creations_only_one_exceeds_quota(self, cn_pool):
        """并发创建两笔各 6000 的申请 (实付 10000): 订单行 FOR UPDATE 串行化,
        reserved 检查后只有一笔能成功。"""
        from app.payments.refund_service import (
            RefundError,
            create_refund_request,
        )

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        results = await asyncio.gather(
            create_refund_request(
                cn_pool,
                order_id=ids["order_id"],
                customer_id=ids["customer_id"],
                type=RefundType.REFUND_ONLY,
                reason=RefundReason.OTHER,
                reason_detail=None,
                requested_amount=6_000,
            ),
            create_refund_request(
                cn_pool,
                order_id=ids["order_id"],
                customer_id=ids["customer_id"],
                type=RefundType.REFUND_ONLY,
                reason=RefundReason.OTHER,
                reason_detail=None,
                requested_amount=6_000,
            ),
            return_exceptions=True,
        )
        successes = [r for r in results if not isinstance(r, BaseException)]
        failures = [r for r in results if isinstance(r, RefundError)]
        assert len(successes) == 1
        assert len(failures) == 1


# ── PG 集成: 退货回补库存 ───────────────────────────────────────────────


class TestReturnRestock:
    async def test_return_received_restocks_inventory(self, cn_pool):
        from app.payments.refund_service import (
            approve_refund,
            create_refund_request,
            mark_refund_returned,
        )

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        async with cn_pool.acquire() as conn:
            stock_before = await conn.fetchval(
                "SELECT stock_qty FROM inventory_batch WHERE id = $1", ids["batch_id"]
            )
        refund = await create_refund_request(
            cn_pool,
            order_id=ids["order_id"],
            customer_id=ids["customer_id"],
            reason=RefundReason.OTHER,
            reason_detail=None,
            type=RefundType.RETURN_REFUND,
            requested_amount=3_000,
        )
        await approve_refund(cn_pool, refund.id, REVIEWER)
        marked = await mark_refund_returned(
            cn_pool, refund.id, "SF123456", ids["batch_id"], REVIEWER
        )
        assert marked.return_batch_id == ids["batch_id"]
        assert marked.metadata.get("restocked") is True
        async with cn_pool.acquire() as conn:
            stock_after = await conn.fetchval(
                "SELECT stock_qty FROM inventory_batch WHERE id = $1", ids["batch_id"]
            )
        assert stock_after == stock_before + 1

    async def test_double_return_received_does_not_double_restock(self, cn_pool):
        from app.payments.refund_service import (
            approve_refund,
            create_refund_request,
            mark_refund_returned,
        )

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        refund = await create_refund_request(
            cn_pool,
            order_id=ids["order_id"],
            customer_id=ids["customer_id"],
            reason=RefundReason.OTHER,
            reason_detail=None,
            type=RefundType.RETURN_REFUND,
            requested_amount=3_000,
        )
        await approve_refund(cn_pool, refund.id, REVIEWER)
        await mark_refund_returned(cn_pool, refund.id, "SF123456", ids["batch_id"], REVIEWER)
        await mark_refund_returned(cn_pool, refund.id, "SF123456", ids["batch_id"], REVIEWER)
        async with cn_pool.acquire() as conn:
            stock_after = await conn.fetchval(
                "SELECT stock_qty FROM inventory_batch WHERE id = $1", ids["batch_id"]
            )
        assert stock_after == 11  # 初始 10, 只回补一次


# ── PG 集成: 网关回调验签 ───────────────────────────────────────────────


def _make_keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return priv_pem, pub_pem


def _wechat_callback_envelope(
    *, platform_priv_pem: str, api_v3_key: str, plaintext: dict
) -> tuple[dict[str, str], bytes]:
    """构造一个验签合法的微信退款回调 (自生成平台密钥签名)。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from app.ext.payment_gateways import load_private_key_pem, rsa_sha256_sign

    nonce = "abcdef123456"
    ct = AESGCM(api_v3_key.encode()).encrypt(
        nonce.encode(), json.dumps(plaintext).encode(), b"transaction"
    )
    body = json.dumps(
        {
            "id": "evt-test",
            "create_time": datetime.now(UTC).isoformat(),
            "event_type": "TRANSACTION.REFUND.SUCCESS",
            "resource": {
                "algorithm": "AEAD_AES_256_GCM",
                "associated_data": "transaction",
                "ciphertext": base64.b64encode(ct).decode(),
                "nonce": nonce,
                "original_type": "transaction",
            },
        }
    ).encode()
    ts = str(int(time.time()))
    msg = f"{ts}\nwechatpay-nonce\n{body.decode()}\n"
    signature = rsa_sha256_sign(load_private_key_pem(platform_priv_pem), msg)
    headers = {
        "wechatpay-timestamp": ts,
        "wechatpay-nonce": "wechatpay-nonce",
        "wechatpay-signature": signature,
        "wechatpay-serial": "serial-test",
    }
    return headers, body


class TestRefundCallbackVerification:
    async def _approved_refund(self, cn_pool):
        from app.payments.refund_service import create_refund_request

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        refund = await create_refund_request(
            cn_pool,
            order_id=ids["order_id"],
            customer_id=ids["customer_id"],
            type=RefundType.REFUND_ONLY,
            reason=RefundReason.OTHER,
            reason_detail=None,
            requested_amount=3_000,
        )
        # 直接推到 refunding (跳过网关执行, 用手工 update 模拟在途)
        async with cn_pool.acquire() as conn:
            await conn.execute(
                "UPDATE refund_request SET status='refunding' WHERE id=$1", refund.id
            )
        return refund

    async def test_wechat_callback_valid_signature_processed(self, cn_pool):
        from app.ext.payment_gateways import WechatPayNativeGateway
        from app.payments.refund_service import (
            _get_refund_request,
            process_refund_callback,
        )

        refund = await self._approved_refund(cn_pool)
        platform_priv, platform_pub = _make_keypair()
        api_v3_key = "0" * 32
        gateway = WechatPayNativeGateway(
            mchid="mch-test",
            serial_no="serial-test",
            platform_cert=platform_pub,
            api_v3_key=api_v3_key,
        )
        plaintext = {
            "mchid": "mch-test",
            "out_trade_no": refund.order_id,
            "out_refund_no": refund.provider_refund_no,
            "refund_status": "SUCCESS",
            "amount": {"refund": 3000, "total": 10000},
        }
        headers, body = _wechat_callback_envelope(
            platform_priv_pem=platform_priv,
            api_v3_key=api_v3_key,
            plaintext=plaintext,
        )
        with patch(
            "app.payments.refund_service.build_payment_gateway",
            return_value=("wechat", gateway),
        ):
            result = await process_refund_callback(
                cn_pool, provider="wechat", headers=headers, body=body
            )
        assert result["status"] == "processed"
        final = await _get_refund_request(cn_pool, refund.id)
        assert final.status == RefundStatus.SUCCEEDED
        assert final.actual_refunded_amount == 3_000

    async def test_wechat_callback_forged_signature_rejected(self, cn_pool):
        from app.ext.payment_gateways import WechatPayNativeGateway
        from app.payments.refund_service import (
            GatewayRefundError,
            _get_refund_request,
            process_refund_callback,
        )

        refund = await self._approved_refund(cn_pool)
        _, platform_pub = _make_keypair()
        gateway = WechatPayNativeGateway(
            mchid="mch-test",
            serial_no="serial-test",
            platform_cert=platform_pub,
            api_v3_key="0" * 32,
        )
        # 伪造签名: 用不相干的私钥签名
        other_priv, _ = _make_keypair()
        headers, body = _wechat_callback_envelope(
            platform_priv_pem=other_priv,
            api_v3_key="0" * 32,
            plaintext={"out_refund_no": refund.provider_refund_no, "refund_status": "SUCCESS"},
        )
        with patch(
            "app.payments.refund_service.build_payment_gateway",
            return_value=("wechat", gateway),
        ):
            with pytest.raises(GatewayRefundError):
                await process_refund_callback(
                    cn_pool, provider="wechat", headers=headers, body=body
                )
        final = await _get_refund_request(cn_pool, refund.id)
        assert final.status == RefundStatus.REFUNDING  # 伪造回调不动状态

    async def test_wechat_callback_tampered_body_rejected(self, cn_pool):
        """改一个字节 (金额) 后签名失效 → 拒绝, 状态不动。"""
        from app.ext.payment_gateways import WechatPayNativeGateway
        from app.payments.refund_service import (
            GatewayRefundError,
            _get_refund_request,
            process_refund_callback,
        )

        refund = await self._approved_refund(cn_pool)
        platform_priv, platform_pub = _make_keypair()
        gateway = WechatPayNativeGateway(
            mchid="mch-test",
            serial_no="serial-test",
            platform_cert=platform_pub,
            api_v3_key="0" * 32,
        )
        plaintext = {"out_refund_no": refund.provider_refund_no, "refund_status": "SUCCESS"}
        headers, body = _wechat_callback_envelope(
            platform_priv_pem=platform_priv, api_v3_key="0" * 32, plaintext=plaintext
        )
        # 篡改 body (签名是对原 body 的, 验签必然失败)
        tampered = body.replace(b'"SUCCESS"', b"CUCCESS") if b'"SUCCESS"' in body else body + b" "
        with patch(
            "app.payments.refund_service.build_payment_gateway",
            return_value=("wechat", gateway),
        ):
            with pytest.raises(GatewayRefundError):
                await process_refund_callback(
                    cn_pool, provider="wechat", headers=headers, body=tampered
                )
        final = await _get_refund_request(cn_pool, refund.id)
        assert final.status == RefundStatus.REFUNDING

    async def test_wechat_callback_duplicate_is_idempotent(self, cn_pool):
        """同一回调重放: 第二次返回 duplicate, 状态不变。"""
        from app.ext.payment_gateways import WechatPayNativeGateway
        from app.payments.refund_service import _get_refund_request, process_refund_callback

        refund = await self._approved_refund(cn_pool)
        platform_priv, platform_pub = _make_keypair()
        gateway = WechatPayNativeGateway(
            mchid="mch-test",
            serial_no="serial-test",
            platform_cert=platform_pub,
            api_v3_key="0" * 32,
        )
        plaintext = {"out_refund_no": refund.provider_refund_no, "refund_status": "SUCCESS"}
        headers, body = _wechat_callback_envelope(
            platform_priv_pem=platform_priv, api_v3_key="0" * 32, plaintext=plaintext
        )
        with patch(
            "app.payments.refund_service.build_payment_gateway",
            return_value=("wechat", gateway),
        ):
            first = await process_refund_callback(
                cn_pool, provider="wechat", headers=headers, body=body
            )
            second = await process_refund_callback(
                cn_pool, provider="wechat", headers=headers, body=body
            )
        assert first["status"] == "processed"
        assert second["status"] == "duplicate"
        final = await _get_refund_request(cn_pool, refund.id)
        assert final.status == RefundStatus.SUCCEEDED
        assert final.actual_refunded_amount == 3_000  # 没有翻倍

    async def test_alipay_callback_valid_signature_processed(self, cn_pool):
        from app.ext.payment_gateways import (
            AlipayPaymentGateway,
            _alipay_build_sign_str,
            _rsa_sha256_sign_alipay,
            load_private_key_pem,
        )
        from app.payments.refund_service import _get_refund_request, process_refund_callback

        refund = await self._approved_refund(cn_pool)
        # 平台密钥对: 私钥签名, 公钥交给网关验签
        platform_priv, platform_pub = _make_keypair()
        gateway = AlipayPaymentGateway(
            app_id="app-test", private_key="", alipay_public_key=platform_pub
        )
        params = {
            "code": "10000",
            "out_trade_no": refund.order_id,
            "out_request_no": refund.provider_refund_no,
            "refund_status": "SUCCESS",
            "app_id": "app-test",
            "sign_type": "RSA2",
        }
        # 支付宝规范: 签名串不含 sign / sign_type, body 需 URL 编码
        from urllib.parse import urlencode

        sign_params = {k: v for k, v in params.items() if k not in ("sign", "sign_type")}
        sign_str = _alipay_build_sign_str(sign_params)
        params["sign"] = _rsa_sha256_sign_alipay(load_private_key_pem(platform_priv), sign_str)
        body = urlencode(params).encode()
        with patch(
            "app.payments.refund_service.build_payment_gateway",
            return_value=("alipay", gateway),
        ):
            result = await process_refund_callback(
                cn_pool, provider="alipay", headers={}, body=body
            )
        assert result["status"] == "processed"
        final = await _get_refund_request(cn_pool, refund.id)
        assert final.status == RefundStatus.SUCCEEDED


# ── HTTP 端点: 伪造签名回调 400 ─────────────────────────────────────────


class TestRefundGatewayNotifyEndpoint:
    async def test_forged_signature_notify_returns_400(self, cn_pool, monkeypatch):
        from fastapi.testclient import TestClient

        from app.deps import get_pool
        from app.ext.payment_gateways import WechatPayNativeGateway
        from app.main import create_app
        from app.payments.refund_service import create_refund_request

        monkeypatch.setenv("HEMALL_PAYMENT_GATEWAY_PROVIDER", "wechat")
        from app.deps import get_settings

        get_settings.cache_clear()

        ids = await _seed_paid_order(cn_pool, paid_cents=10_000)
        refund = await create_refund_request(
            cn_pool,
            order_id=ids["order_id"],
            customer_id=ids["customer_id"],
            type=RefundType.REFUND_ONLY,
            reason=RefundReason.OTHER,
            reason_detail=None,
            requested_amount=3_000,
        )
        async with cn_pool.acquire() as conn:
            await conn.execute(
                "UPDATE refund_request SET status='refunding' WHERE id=$1", refund.id
            )

        _, platform_pub = _make_keypair()
        gateway = WechatPayNativeGateway(
            mchid="mch-test", serial_no="s", platform_cert=platform_pub, api_v3_key="0" * 32
        )
        body = json.dumps(
            {
                "resource": {
                    "algorithm": "AEAD_AES_256_GCM",
                    "associated_data": "transaction",
                    "ciphertext": base64.b64encode(b"x" * 32).decode(),
                    "nonce": "abcdef123456",
                    "original_type": "transaction",
                }
            }
        ).encode()
        headers = {
            "wechatpay-timestamp": str(int(time.time())),
            "wechatpay-nonce": "n",
            "wechatpay-signature": base64.b64encode(b"forged").decode(),
            "wechatpay-serial": "s",
        }
        with patch(
            "app.payments.refund_service.build_payment_gateway",
            return_value=("wechat", gateway),
        ):
            app = create_app()
            app.dependency_overrides[get_pool] = lambda: cn_pool
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.post(
                    "/aftersales/refunds/gateway/notify", content=body, headers=headers
                )
        assert resp.status_code == 400, resp.text
        assert "signature" in resp.json()["detail"].lower()
