"""缺口 6 二期测试：夜市阶梯价 + 拼单状态机。

纯计算部分冻结北京时间测时段边界；DB 部分（需 TEST_PG_DSN）覆盖
拼单发起/加入/成团/到期与夜市标记列。拼单不收款、不建单，故无
订单/券账本断言——结算语义以状态机为准。
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.cangzhu import (
    NIGHT_MARKET_TZ,
    choose_discount,
    night_discount_cents,
    night_discount_rate,
    night_price_cents,
)
from app.night_pintuan import (
    create_pintuan_group,
    ensure_night_pintuan_schema,
    get_pintuan_group,
    join_pintuan_group,
    list_pintuan_groups,
    sweep_expired_pintuan_groups,
)

# ── 纯计算：时段边界 ────────────────────────────────────────────────────


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 11, 3, hour, minute, tzinfo=NIGHT_MARKET_TZ)


def test_night_tier_boundaries() -> None:
    assert night_discount_rate(_at(19, 59)) == 0
    assert night_discount_rate(_at(20, 0)) == Decimal("0.20")
    assert night_discount_rate(_at(20, 59)) == Decimal("0.20")
    assert night_discount_rate(_at(21, 0)) == Decimal("0.30")
    assert night_discount_rate(_at(21, 59)) == Decimal("0.30")
    assert night_discount_rate(_at(22, 0)) == Decimal("0.40")
    assert night_discount_rate(_at(22, 59)) == Decimal("0.40")
    assert night_discount_rate(_at(23, 0)) == 0
    assert night_discount_rate(_at(23, 30)) == 0
    assert night_discount_rate(_at(2, 0)) == 0


def test_night_rate_utc_input_converted_to_beijing() -> None:
    # 12:00 UTC = 20:00 北京时间，进 8 折档。
    assert night_discount_rate(datetime(2026, 11, 3, 12, 0, tzinfo=UTC)) == (Decimal("0.20"))
    # naive 时间按北京时间墙上时间理解。
    assert night_discount_rate(datetime(2026, 11, 3, 21, 30)) == (Decimal("0.30"))


def test_night_discount_cents_rounding() -> None:
    assert night_discount_cents(10000, _at(20, 30)) == 2000
    assert night_discount_cents(10000, _at(21, 30)) == 3000
    assert night_discount_cents(10000, _at(22, 30)) == 4000
    assert night_discount_cents(10000, _at(19, 30)) == 0
    assert night_discount_cents(105, _at(20, 30)) == 21  # 21.0，四舍五入
    assert night_discount_cents(0, _at(20, 30)) == 0


def test_night_price_cents() -> None:
    assert night_price_cents(10000, _at(21, 30)) == 7000
    assert night_price_cents(10000, _at(19, 30)) == 10000


# ── 纯计算：三者互斥取最优 ─────────────────────────────────────────────


def test_choose_discount_night_wins_when_largest() -> None:
    assert choose_discount(10000, 5000, is_member=True, night_cents=3000) == (
        "night",
        3000,
    )


def test_choose_discount_night_wins_tie_with_member() -> None:
    assert choose_discount(10000, 1000, is_member=True, night_cents=1000) == (
        "night",
        1000,
    )


def test_choose_discount_member_wins_when_night_smaller() -> None:
    assert choose_discount(10000, 0, is_member=True, night_cents=500) == ("member", 1000)


def test_choose_discount_voucher_wins_when_largest() -> None:
    assert choose_discount(10000, 5000, is_member=True, night_cents=1500) == (
        "voucher",
        2000,
    )


def test_choose_discount_night_only_and_backward_compat() -> None:
    assert choose_discount(10000, 0, is_member=False, night_cents=2000) == (
        "night",
        2000,
    )
    # 不传 night_cents 时与旧版行为一致。
    assert choose_discount(10000, 1000, is_member=True) == ("member", 1000)
    assert choose_discount(10000, 5000, is_member=True) == ("voucher", 2000)


# ── DB 集成 ─────────────────────────────────────────────────────────────

TEST_DSN = os.environ.get("TEST_PG_DSN")

needs_db = pytest.mark.skipif(not TEST_DSN, reason="TEST_PG_DSN not set")


@pytest.fixture
async def np_pool():
    from obase.persistence.pool import PgPool

    assert TEST_DSN is not None
    pool = await PgPool.create(
        name="hemall_night_pintuan_test", dsn=TEST_DSN, min_size=1, max_size=5
    )
    await ensure_night_pintuan_schema(pool)
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
        self.groups: list[str] = []

    def tag(self) -> str:
        return uuid.uuid4().hex[:8]


@pytest.fixture
async def seed(np_pool):
    s = Seed()
    yield s
    async with np_pool.acquire() as conn:
        await conn.execute("DELETE FROM pintuan_participant WHERE group_id = ANY($1)", s.groups)
        await conn.execute("DELETE FROM pintuan_group WHERE id = ANY($1)", s.groups)
        await conn.execute("DELETE FROM product_variant WHERE id = ANY($1)", s.variants)
        await conn.execute("DELETE FROM product WHERE id = ANY($1)", s.products)
        await conn.execute('DELETE FROM "customer" WHERE id = ANY($1)', s.customers)


async def make_customer(pool, seed: Seed) -> str:
    cid = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO "customer" (id, email, status, created_at, updated_at)'
            " VALUES ($1, $2, 'active', NOW(), NOW())",
            cid,
            f"np-{seed.tag()}@test.dev",
        )
    seed.customers.append(cid)
    return cid


async def make_variant(pool, seed: Seed) -> str:
    pid = str(uuid.uuid4())
    vid = str(uuid.uuid4())
    tag = seed.tag()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO product (id, title, slug, status, created_at, updated_at)"
            " VALUES ($1, $2, $3, 'published', NOW(), NOW())",
            pid,
            f"np product {tag}",
            f"np-slug-{tag}",
        )
        await conn.execute(
            "INSERT INTO product_variant (id, product_id, sku_code, option_values,"
            " status, created_at, updated_at) VALUES ($1, $2, $3, '{}'::jsonb,"
            " 'active', NOW(), NOW())",
            vid,
            pid,
            f"NP-{tag}",
        )
    seed.products.append(pid)
    seed.variants.append(vid)
    return vid


async def expire_group(pool, group_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE pintuan_group SET expires_at = NOW() - INTERVAL '1 minute' WHERE id = $1",
            group_id,
        )


@needs_db
async def test_pintuan_forms_at_target_size(np_pool, seed) -> None:
    creator = await make_customer(np_pool, seed)
    joiner1 = await make_customer(np_pool, seed)
    joiner2 = await make_customer(np_pool, seed)
    variant = await make_variant(np_pool, seed)
    group = await create_pintuan_group(
        np_pool,
        creator_id=creator,
        variant_id=variant,
        pintuan_price_cents=8000,
        target_size=3,
    )
    seed.groups.append(group["group_id"])
    assert group["status"] == "open"
    assert group["participant_count"] == 1
    joined = await join_pintuan_group(np_pool, group_id=group["group_id"], customer_id=joiner1)
    assert joined["status"] == "open"
    assert joined["participant_count"] == 2
    formed = await join_pintuan_group(np_pool, group_id=group["group_id"], customer_id=joiner2)
    assert formed["status"] == "formed"
    assert formed["participant_count"] == 3
    detail = await get_pintuan_group(np_pool, group["group_id"])
    assert detail is not None and detail["status"] == "formed"
    assert len(detail["participants"]) == 3


@needs_db
async def test_pintuan_duplicate_and_full_rejected(np_pool, seed) -> None:
    creator = await make_customer(np_pool, seed)
    joiner = await make_customer(np_pool, seed)
    extra = await make_customer(np_pool, seed)
    variant = await make_variant(np_pool, seed)
    group = await create_pintuan_group(
        np_pool,
        creator_id=creator,
        variant_id=variant,
        pintuan_price_cents=8000,
        target_size=2,
    )
    seed.groups.append(group["group_id"])
    with pytest.raises(ValueError, match="already joined"):
        await join_pintuan_group(np_pool, group_id=group["group_id"], customer_id=creator)
    await join_pintuan_group(np_pool, group_id=group["group_id"], customer_id=joiner)
    with pytest.raises(ValueError, match="formed"):
        await join_pintuan_group(np_pool, group_id=group["group_id"], customer_id=extra)


@needs_db
async def test_pintuan_counts_heads_not_quantity(np_pool, seed) -> None:
    creator = await make_customer(np_pool, seed)
    joiner = await make_customer(np_pool, seed)
    variant = await make_variant(np_pool, seed)
    group = await create_pintuan_group(
        np_pool,
        creator_id=creator,
        variant_id=variant,
        pintuan_price_cents=8000,
        target_size=3,
        quantity=5,
    )
    seed.groups.append(group["group_id"])
    assert group["participant_count"] == 1
    joined = await join_pintuan_group(
        np_pool, group_id=group["group_id"], customer_id=joiner, quantity=5
    )
    assert joined["status"] == "open"  # 两人各 5 件，仍只有 2 个人头，未成团
    assert joined["participant_count"] == 2


@needs_db
async def test_pintuan_expired_group_rejects_join_and_lazy_expires(np_pool, seed) -> None:
    creator = await make_customer(np_pool, seed)
    joiner = await make_customer(np_pool, seed)
    variant = await make_variant(np_pool, seed)
    group = await create_pintuan_group(
        np_pool,
        creator_id=creator,
        variant_id=variant,
        pintuan_price_cents=8000,
        target_size=2,
    )
    seed.groups.append(group["group_id"])
    await expire_group(np_pool, group["group_id"])
    with pytest.raises(ValueError, match="expired"):
        await join_pintuan_group(np_pool, group_id=group["group_id"], customer_id=joiner)
    detail = await get_pintuan_group(np_pool, group["group_id"])
    assert detail is not None and detail["status"] == "expired"


@needs_db
async def test_pintuan_sweep_and_list_lazy_expiry(np_pool, seed) -> None:
    creator = await make_customer(np_pool, seed)
    variant = await make_variant(np_pool, seed)
    group = await create_pintuan_group(
        np_pool,
        creator_id=creator,
        variant_id=variant,
        pintuan_price_cents=8000,
        target_size=2,
    )
    seed.groups.append(group["group_id"])
    await expire_group(np_pool, group["group_id"])
    assert await sweep_expired_pintuan_groups(np_pool) == 1
    assert await sweep_expired_pintuan_groups(np_pool) == 0  # 幂等
    expired = await list_pintuan_groups(np_pool, status="expired", limit=50, offset=0)
    assert any(g["group_id"] == group["group_id"] for g in expired)
    open_groups = await list_pintuan_groups(np_pool, status="open", limit=50, offset=0)
    assert all(g["group_id"] != group["group_id"] for g in open_groups)


@needs_db
async def test_pintuan_create_validates(np_pool, seed) -> None:
    creator = await make_customer(np_pool, seed)
    variant = await make_variant(np_pool, seed)
    with pytest.raises(ValueError, match="positive"):
        await create_pintuan_group(
            np_pool,
            creator_id=creator,
            variant_id=variant,
            pintuan_price_cents=0,
            target_size=2,
        )
    with pytest.raises(ValueError, match="target_size"):
        await create_pintuan_group(
            np_pool,
            creator_id=creator,
            variant_id=variant,
            pintuan_price_cents=8000,
            target_size=1,
        )
    with pytest.raises(ValueError, match="variant not found"):
        await create_pintuan_group(
            np_pool,
            creator_id=creator,
            variant_id=str(uuid.uuid4()),
            pintuan_price_cents=8000,
            target_size=2,
        )


@needs_db
async def test_night_clearance_column_defaults_false(np_pool, seed) -> None:
    variant = await make_variant(np_pool, seed)
    async with np_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT p.night_clearance FROM product p "
            "JOIN product_variant pv ON pv.product_id = p.id WHERE pv.id = $1",
            variant,
        )
        assert row["night_clearance"] is False
        updated = await conn.fetchrow(
            "UPDATE product SET night_clearance = true, voucher_excluded = true "
            "WHERE id = (SELECT product_id FROM product_variant WHERE id = $1) "
            "RETURNING night_clearance, voucher_excluded",
            variant,
        )
        assert updated["night_clearance"] is True
        assert updated["voucher_excluded"] is True
