"""仓主试点测试：纯计算单元测试 + DB 集成测试（需 TEST_PG_DSN）。

覆盖规则文件全部关键 math：10% 累积、退款比例冲回、20% 上限、互斥取最优、
自推拦截、余额记负回填；DB 部分覆盖绑定/结算/冲回/持有-核销-返还/冻结/
邀请礼/点货/账本。
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.cangzhu import (
    bind_referral,
    choose_discount,
    claim_invite_gift,
    create_purchase_request,
    ensure_cangzhu_schema,
    ensure_referral_code,
    finalize_redemption,
    freeze_accrual,
    generate_referral_code,
    get_voucher_balance,
    growth_ledger,
    hold_redemption,
    is_valid_referral_code,
    list_purchase_requests,
    member_discount_cents,
    purchase_request_leaderboard,
    referral_reward_cents,
    release_redemption,
    resolve_referrer_id,
    reversal_cents,
    reverse_for_refund,
    settle_accrual,
    unfreeze_accrual,
    update_purchase_request,
    validate_purchase_request_transition,
    voucher_redeemable_cents,
)

# ── 纯计算 ──────────────────────────────────────────────────────────────


def test_referral_reward_rounding() -> None:
    assert referral_reward_cents(10000) == 1000
    assert referral_reward_cents(105) == 11  # 10.5 → 11，四舍五入
    assert referral_reward_cents(0) == 0
    assert referral_reward_cents(-50) == 0


def test_member_discount() -> None:
    assert member_discount_cents(10000) == 1000
    assert member_discount_cents(0) == 0


def test_voucher_redeemable_cap_and_negative_balance() -> None:
    assert voucher_redeemable_cents(10000, 5000) == 2000  # 20% 上限
    assert voucher_redeemable_cents(10000, 500) == 500  # 余额更小
    assert voucher_redeemable_cents(10000, -100) == 0  # 记负余额不可用
    assert voucher_redeemable_cents(0, 5000) == 0


def test_choose_discount_prefers_member_on_tie() -> None:
    # 9 折 1000 vs 券 min(1000, 2000)=1000：平局优先 9 折，不动余额。
    assert choose_discount(10000, 1000, is_member=True) == ("member", 1000)


def test_choose_discount_voucher_wins_when_larger() -> None:
    assert choose_discount(10000, 5000, is_member=True) == ("voucher", 2000)
    assert choose_discount(10000, 5000, is_member=False) == ("voucher", 2000)


def test_choose_discount_none() -> None:
    assert choose_discount(10000, 0, is_member=False) == ("none", 0)
    assert choose_discount(0, 5000, is_member=True) == ("none", 0)


def test_reversal_proportional_and_capped() -> None:
    assert reversal_cents(1000, 5000, 10000) == 500
    assert reversal_cents(1000, 10000, 10000) == 1000
    assert reversal_cents(1000, 20000, 10000) == 1000  # 超额退款也只冲已发
    assert reversal_cents(1000, 5000, 0) == 0
    assert reversal_cents(0, 5000, 10000) == 0


def test_referral_code_format() -> None:
    assert is_valid_referral_code("HMABC123")
    assert not is_valid_referral_code("hmabc123")
    assert not is_valid_referral_code("HMABC12")
    assert not is_valid_referral_code("XXABC123")
    code = generate_referral_code()
    assert is_valid_referral_code(code)


def test_purchase_request_transitions() -> None:
    validate_purchase_request_transition("collected", "price_confirmed")
    validate_purchase_request_transition("arrived", "delivered")
    with pytest.raises(ValueError):
        validate_purchase_request_transition("collected", "delivered")
    with pytest.raises(ValueError):
        validate_purchase_request_transition("delivered", "failed")


# ── DB 集成 ─────────────────────────────────────────────────────────────

TEST_DSN = os.environ.get("TEST_PG_DSN")

needs_db = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set")


@pytest.fixture
async def cz_pool():
    from obase.persistence.pool import PgPool

    assert TEST_DSN is not None
    pool = await PgPool.create(name="hemall_cangzhu_test", dsn=TEST_DSN, min_size=1, max_size=5)
    await ensure_cangzhu_schema(pool)
    try:
        yield pool
    finally:
        await pool.close()


class Seed:
    """收集本测试种的行，teardown 时按 FK 安全顺序清掉。"""

    def __init__(self) -> None:
        self.customers: list[str] = []
        self.products: list[str] = []
        self.variants: list[str] = []
        self.orders: list[str] = []
        self.intents: list[str] = []

    def tag(self) -> str:
        return uuid.uuid4().hex[:8]


@pytest.fixture
async def seed(cz_pool):
    s = Seed()
    yield s
    async with cz_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM voucher_ledger WHERE customer_id = ANY($1)",
            s.customers,
        )
        await conn.execute("DELETE FROM invite_gift_claim WHERE customer_id = ANY($1)", s.customers)
        await conn.execute(
            "DELETE FROM member_purchase_request WHERE customer_id = ANY($1)",
            s.customers,
        )
        await conn.execute("DELETE FROM order_line_item WHERE order_id = ANY($1)", s.orders)
        await conn.execute("DELETE FROM refund_request WHERE order_id = ANY($1)", s.orders)
        await conn.execute("DELETE FROM payment_intent WHERE id = ANY($1)", s.intents)
        await conn.execute("DELETE FROM customer_order WHERE id = ANY($1)", s.orders)
        await conn.execute("DELETE FROM product_variant WHERE id = ANY($1)", s.variants)
        await conn.execute("DELETE FROM product WHERE id = ANY($1)", s.products)
        await conn.execute('DELETE FROM "customer" WHERE id = ANY($1)', s.customers)


async def make_customer(pool, seed: Seed, tag: str) -> str:
    cid = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO "customer" (id, email, status, created_at, updated_at)'
            " VALUES ($1, $2, 'active', NOW(), NOW())",
            cid,
            f"cz-{tag}@test.dev",
        )
    seed.customers.append(cid)
    return cid


async def make_variant(pool, seed: Seed, tag: str, *, excluded: bool = False) -> str:
    pid = str(uuid.uuid4())
    vid = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO product (id, title, slug, status, voucher_excluded,"
            " created_at, updated_at) VALUES ($1, $2, $3, 'published', $4, NOW(), NOW())",
            pid,
            f"cz product {tag}",
            f"cz-slug-{tag}",
            excluded,
        )
        await conn.execute(
            "INSERT INTO product_variant (id, product_id, sku_code, option_values,"
            " status, created_at, updated_at) VALUES ($1, $2, $3, '{}'::jsonb,"
            " 'active', NOW(), NOW())",
            vid,
            pid,
            f"CZ-{tag}",
        )
    seed.products.append(pid)
    seed.variants.append(vid)
    return vid


async def make_order(
    pool,
    seed: Seed,
    customer_id: str,
    lines: list[tuple[str, int, int]],
    *,
    status: str = "completed",
    shipping_cents: int = 0,
    voucher_redeemed_cents: int = 0,
) -> str:
    oid = str(uuid.uuid4())
    subtotal = sum(qty * price for _, qty, price in lines)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO customer_order (id, customer_id, currency, status,"
            " subtotal_cents, discount_cents, tax_cents, shipping_cents,"
            " grand_total_cents, refunded_cents, created_at, updated_at, version,"
            " voucher_redeemed_cents)"
            " VALUES ($1, $2, 'CNY', $3, $4, 0, 0, $5, $6, 0, NOW(), NOW(), 0, $7)",
            oid,
            customer_id,
            status,
            subtotal,
            shipping_cents,
            subtotal + shipping_cents,
            voucher_redeemed_cents,
        )
        for variant_id, qty, price in lines:
            await conn.execute(
                "INSERT INTO order_line_item (id, order_id, variant_id, quantity,"
                " unit_price_cents, line_total_cents, created_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, NOW())",
                str(uuid.uuid4()),
                oid,
                variant_id,
                qty,
                price,
                qty * price,
            )
    seed.orders.append(oid)
    return oid


@needs_db
async def test_referral_bind_and_self_referral_blocked(cz_pool, seed) -> None:
    tag = seed.tag()
    referrer = await make_customer(cz_pool, seed, f"ref-{tag}")
    buyer = await make_customer(cz_pool, seed, f"buy-{tag}")
    async with cz_pool.acquire() as conn:
        async with conn.transaction():
            code = await ensure_referral_code(conn, referrer)
        assert is_valid_referral_code(code)
        assert await resolve_referrer_id(conn, code) == referrer
        async with conn.transaction():
            await bind_referral(conn, customer_id=buyer, referrer_id=referrer)
        row = await conn.fetchrow('SELECT invited_by FROM "customer" WHERE id = $1', buyer)
        assert str(row["invited_by"]) == referrer
        # 重复绑定拒绝
        with pytest.raises(ValueError):
            async with conn.transaction():
                await bind_referral(conn, customer_id=buyer, referrer_id=referrer)
        # 自我推荐拒绝
        with pytest.raises(ValueError):
            async with conn.transaction():
                await bind_referral(conn, customer_id=buyer, referrer_id=buyer)
        # 无效码拒绝
        with pytest.raises(ValueError):
            await resolve_referrer_id(conn, "HMXXXXXX")


@needs_db
async def test_settle_accrual_and_idempotent_resettle(cz_pool, seed) -> None:
    tag = seed.tag()
    referrer = await make_customer(cz_pool, seed, f"ref-{tag}")
    buyer = await make_customer(cz_pool, seed, f"buy-{tag}")
    async with cz_pool.acquire() as conn:
        async with conn.transaction():
            await bind_referral(conn, customer_id=buyer, referrer_id=referrer)
    variant = await make_variant(cz_pool, seed, f"v-{tag}")
    order = await make_order(cz_pool, seed, buyer, [(variant, 2, 5000)])
    result = await settle_accrual(cz_pool, order)
    assert result["settled"] is True
    assert result["amount_cents"] == 1000  # 10000 的 10%
    async with cz_pool.acquire() as conn:
        assert await get_voucher_balance(conn, referrer) == 1000
    again = await settle_accrual(cz_pool, order)
    assert again["settled"] is False
    async with cz_pool.acquire() as conn:
        assert await get_voucher_balance(conn, referrer) == 1000


@needs_db
async def test_settle_skips_non_completed_and_unreferred(cz_pool, seed) -> None:
    tag = seed.tag()
    buyer = await make_customer(cz_pool, seed, f"buy-{tag}")
    variant = await make_variant(cz_pool, seed, f"v-{tag}")
    pending = await make_order(cz_pool, seed, buyer, [(variant, 1, 5000)], status="pending")
    assert (await settle_accrual(cz_pool, pending))["settled"] is False
    completed = await make_order(cz_pool, seed, buyer, [(variant, 1, 5000)])
    result = await settle_accrual(cz_pool, completed)
    assert result["reason"] == "no referrer"


@needs_db
async def test_excluded_product_not_counted(cz_pool, seed) -> None:
    tag = seed.tag()
    referrer = await make_customer(cz_pool, seed, f"ref-{tag}")
    buyer = await make_customer(cz_pool, seed, f"buy-{tag}")
    async with cz_pool.acquire() as conn:
        async with conn.transaction():
            await bind_referral(conn, customer_id=buyer, referrer_id=referrer)
    normal = await make_variant(cz_pool, seed, f"n-{tag}")
    excluded = await make_variant(cz_pool, seed, f"x-{tag}", excluded=True)
    order = await make_order(cz_pool, seed, buyer, [(normal, 1, 6000), (excluded, 1, 4000)])
    result = await settle_accrual(cz_pool, order)
    assert result["base_cents"] == 6000
    assert result["amount_cents"] == 600


@needs_db
async def test_reversal_proportional_and_negative_balance(cz_pool, seed) -> None:
    tag = seed.tag()
    referrer = await make_customer(cz_pool, seed, f"ref-{tag}")
    buyer = await make_customer(cz_pool, seed, f"buy-{tag}")
    async with cz_pool.acquire() as conn:
        async with conn.transaction():
            await bind_referral(conn, customer_id=buyer, referrer_id=referrer)
    variant = await make_variant(cz_pool, seed, f"v-{tag}")
    order = await make_order(cz_pool, seed, buyer, [(variant, 2, 5000)])
    await settle_accrual(cz_pool, order)
    # 推荐人先把 1000 券花掉 800（余额剩 200），再退一半货款：冲回 500 → 记负
    async with cz_pool.acquire() as conn:
        async with conn.transaction():
            await hold_redemption(
                conn, customer_id=referrer, order_id=str(uuid.uuid4()), amount_cents=800
            )
            assert await get_voucher_balance(conn, referrer) == 200
    reversed_result = await reverse_for_refund(
        cz_pool, refund_id=f"rf-{tag}", order_id=order, refund_cents=5000
    )
    assert reversed_result["reversed"] is True
    assert reversed_result["amount_cents"] == 500
    async with cz_pool.acquire() as conn:
        assert await get_voucher_balance(conn, referrer) == -300
    # 同一退款重放不重复冲
    again = await reverse_for_refund(
        cz_pool, refund_id=f"rf-{tag}", order_id=order, refund_cents=5000
    )
    assert again["reversed"] is False
    # 下一笔累积先填负：再完成一单 10000 → 1000 入账，余额 -300+1000=700
    order2 = await make_order(cz_pool, seed, buyer, [(variant, 2, 5000)])
    await settle_accrual(cz_pool, order2)
    async with cz_pool.acquire() as conn:
        assert await get_voucher_balance(conn, referrer) == 700


@needs_db
async def test_hold_finalize_release(cz_pool, seed) -> None:
    tag = seed.tag()
    owner = await make_customer(cz_pool, seed, f"own-{tag}")
    async with cz_pool.acquire() as conn:
        await conn.execute(
            'UPDATE "customer" SET voucher_balance_cents = 1500 WHERE id = $1', owner
        )
    order_a = str(uuid.uuid4())
    order_b = str(uuid.uuid4())
    async with cz_pool.acquire() as conn:
        async with conn.transaction():
            await hold_redemption(conn, customer_id=owner, order_id=order_a, amount_cents=1000)
            assert await get_voucher_balance(conn, owner) == 500
    assert await finalize_redemption(cz_pool, order_a) is True
    assert await finalize_redemption(cz_pool, order_a) is False  # 幂等
    async with cz_pool.acquire() as conn:
        async with conn.transaction():
            await hold_redemption(conn, customer_id=owner, order_id=order_b, amount_cents=400)
    assert await release_redemption(cz_pool, order_b) is True
    assert await release_redemption(cz_pool, order_b) is False  # 幂等
    async with cz_pool.acquire() as conn:
        assert await get_voucher_balance(conn, owner) == 500


@needs_db
async def test_freeze_and_unfreeze(cz_pool, seed) -> None:
    tag = seed.tag()
    referrer = await make_customer(cz_pool, seed, f"ref-{tag}")
    buyer = await make_customer(cz_pool, seed, f"buy-{tag}")
    async with cz_pool.acquire() as conn:
        async with conn.transaction():
            await bind_referral(conn, customer_id=buyer, referrer_id=referrer)
    variant = await make_variant(cz_pool, seed, f"v-{tag}")
    order = await make_order(cz_pool, seed, buyer, [(variant, 1, 5000)])
    assert await freeze_accrual(cz_pool, order) is True
    assert (await settle_accrual(cz_pool, order))["reason"] == "frozen for review"
    result = await unfreeze_accrual(cz_pool, order)
    assert result["settled"] is True
    assert result["amount_cents"] == 500


@needs_db
async def test_invite_gift_claim_once(cz_pool, seed) -> None:
    tag = seed.tag()
    buyer = await make_customer(cz_pool, seed, f"buy-{tag}")
    variant = await make_variant(cz_pool, seed, f"v-{tag}")
    order = await make_order(cz_pool, seed, buyer, [(variant, 1, 6000)], status="confirmed")
    intent_id = str(uuid.uuid4())
    async with cz_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO payment_intent (id, order_id, amount_cents, currency,"
            " provider, status) VALUES ($1, $2, 6000, 'CNY', 'manual', 'paid')",
            intent_id,
            str(order),
        )
    seed.intents.append(intent_id)
    claim = await claim_invite_gift(cz_pool, buyer, "egg")
    assert claim["status"] == "claimed"
    with pytest.raises(ValueError):
        await claim_invite_gift(cz_pool, buyer, "vegetable")
    poor = await make_customer(cz_pool, seed, f"poor-{tag}")
    with pytest.raises(ValueError):
        await claim_invite_gift(cz_pool, poor, "egg")


@needs_db
async def test_purchase_request_flow_and_leaderboard(cz_pool, seed) -> None:
    tag = seed.tag()
    buyer = await make_customer(cz_pool, seed, f"buy-{tag}")
    req_id = await create_purchase_request(
        cz_pool,
        customer_id=buyer,
        body={
            "product_name": "土鸡蛋",
            "brand": "农家",
            "spec_packaging": "30 枚装",
            "price_min_cents": 2000,
            "price_max_cents": 3000,
            "expected_qty": 2,
            "willing_preorder": True,
        },
    )
    mine = await list_purchase_requests(cz_pool, customer_id=buyer, status=None, limit=10, offset=0)
    assert len(mine) == 1
    assert mine[0]["status"] == "collected"
    updated = await update_purchase_request(
        cz_pool,
        request_id=req_id,
        to_status="price_confirmed",
        admin_note="已询价",
        failure_reason="",
    )
    assert updated["status"] == "price_confirmed"
    with pytest.raises(ValueError):
        await update_purchase_request(
            cz_pool,
            request_id=req_id,
            to_status="failed",
            admin_note="",
            failure_reason="",
        )
    board = await purchase_request_leaderboard(cz_pool, days=7, limit=10)
    assert any(row["product_name"] == "土鸡蛋" for row in board)


@needs_db
async def test_growth_ledger_runs(cz_pool, seed) -> None:
    from datetime import date

    ledger = await growth_ledger(cz_pool, date.today())
    assert ledger["day"] == date.today().isoformat()
    assert ledger["spoilage_rate"] is None
    assert any("spoilage" in gap for gap in ledger["gaps"])
    assert ledger["new_owners"] >= 0
