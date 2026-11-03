"""仓主试点：推荐绑定、10% 购物券账本、9 折互斥、点货、裂变账本。

规则依据：~/workspace/hemal-cangzhu-rules.md（Soffy 2026-11-02 全部通过）。
本模块只含试点逻辑与幂等 DDL，不 import 任何 app 内模块，避免循环导入。

账本语义（voucher_ledger，append-only，状态机在行内）：
- accrual（累积）：订单完成态后，按合资格金额 10% 发给直接推荐人。
  status=posted 计入余额；status=frozen 为异常冻结待人工审，不计入。
- redemption（核销）：下单时按合资格金额 20% 上限持有，status=held 已扣
  余额；支付确认后 status=posted；订单取消则 status=released 并返还余额。
- reversal（冲回）：退款成功后按退款占合资格基数比例冲回已发奖励。
余额缓存列 customer.voucher_balance_cents 与账本同事务维护；余额可为负，
下期累积先填负。绝不复用 customer.system_balance（SLA/UGC 算力金）。
"""

from __future__ import annotations

import logging
import secrets
import string
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from asyncpg.exceptions import UniqueViolationError

logger = logging.getLogger("hemall.cangzhu")

# ── 规则常量（规则文件默认值；改动须经 Soffy 拍板）─────────────────────
REFERRAL_REWARD_RATE = Decimal("0.10")  # 被推荐人合资格消费 10% 返推荐人
MEMBER_DISCOUNT_RATE = Decimal("0.10")  # 会员 9 折
VOUCHER_REDEEM_CAP_RATE = Decimal("0.20")  # 每单券抵扣上限：合资格金额 20%
# ── 夜市阶梯价（规则 1，缺口 6 二期）────────────────────────────────
# 时段语义：北京时间（UTC+8 固定偏移，无夏令时，等价 Asia/Shanghai）
# 当日 20:00（含）起 8 折、21:00（含）起 7 折、22:00（含）起 6 折，
# 23:00 起恢复原价（与门店 23:00 打烊一致）。仅 night_clearance 标记的
# 当日临期清货品参与；时段外、未标记均无夜市价。
NIGHT_MARKET_TZ = timezone(timedelta(hours=8), name="UTC+08:00")
NIGHT_MARKET_START_HOUR = 20
NIGHT_MARKET_END_HOUR = 23
NIGHT_MARKET_TIERS: tuple[tuple[int, Decimal], ...] = (
    (20, Decimal("0.20")),  # 20:00–20:59：8 折，折扣额 20%
    (21, Decimal("0.30")),  # 21:00–21:59：7 折，折扣额 30%
    (22, Decimal("0.40")),  # 22:00–22:59：6 折，折扣额 40%
)
# 邀请礼默认值（规则 6）：待当地采购价锁定后复核。
INVITE_GIFT_THRESHOLD_CENTS = 5000  # 首单实付满 50 元
INVITE_GIFT_COST_CAP_CENTS = 800  # 单份成本上限 8 元
REFERRAL_CODE_PREFIX = "HM"
REFERRAL_CODE_LEN = 8

# 视为已付款的订单状态（首单/复购口径用）：pending/draft/cancelled/failed 除外。
PAID_ORDER_STATUSES = (
    "confirmed",
    "processing",
    "packed",
    "shipped",
    "delivered",
    "completed",
    "returning",
    "refunded",
)

CANGZHU_DDL = """
ALTER TABLE customer ADD COLUMN IF NOT EXISTS referral_code TEXT;
ALTER TABLE customer ADD COLUMN IF NOT EXISTS voucher_balance_cents INT NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_referral_code
    ON customer(referral_code) WHERE referral_code IS NOT NULL;
ALTER TABLE customer_order ADD COLUMN IF NOT EXISTS discount_kind TEXT NOT NULL DEFAULT 'none';
ALTER TABLE customer_order ADD COLUMN IF NOT EXISTS voucher_redeemed_cents INT NOT NULL DEFAULT 0;
ALTER TABLE customer_order ADD COLUMN IF NOT EXISTS voucher_accrual_frozen
    BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE product ADD COLUMN IF NOT EXISTS voucher_excluded BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS voucher_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('accrual', 'reversal', 'redemption')),
    status TEXT NOT NULL CHECK (status IN ('held', 'posted', 'released', 'frozen', 'void')),
    order_id UUID,
    ref_id TEXT,
    amount_cents INT NOT NULL CHECK (amount_cents <> 0),
    base_cents INT,
    balance_after_cents INT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_voucher_ledger_customer ON voucher_ledger(customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_voucher_ledger_order ON voucher_ledger(order_id, kind);

CREATE TABLE IF NOT EXISTS member_purchase_request (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL,
    product_name TEXT NOT NULL CHECK (product_name <> ''),
    brand TEXT NOT NULL DEFAULT '',
    spec_packaging TEXT NOT NULL DEFAULT '',
    price_min_cents INT CHECK (price_min_cents IS NULL OR price_min_cents >= 0),
    price_max_cents INT CHECK (price_max_cents IS NULL OR price_max_cents >= 0),
    expected_qty INT NOT NULL DEFAULT 1 CHECK (expected_qty > 0),
    willing_preorder BOOLEAN NOT NULL DEFAULT false,
    status TEXT NOT NULL DEFAULT 'collected'
        CHECK (status IN ('collected', 'price_confirmed', 'purchasing',
                          'arrived', 'delivered', 'failed')),
    admin_note TEXT NOT NULL DEFAULT '',
    failure_reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_purchase_request_customer
    ON member_purchase_request(customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_purchase_request_status
    ON member_purchase_request(status, created_at);

CREATE TABLE IF NOT EXISTS invite_gift_claim (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL UNIQUE,
    order_id UUID NOT NULL,
    gift_kind TEXT NOT NULL CHECK (gift_kind IN ('egg', 'vegetable')),
    status TEXT NOT NULL DEFAULT 'claimed'
        CHECK (status IN ('claimed', 'confirmed', 'void')),
    cost_cap_cents INT NOT NULL DEFAULT 800,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def ensure_cangzhu_schema(pool: Any) -> None:
    """幂等创建仓主试点所需表、列、索引。"""

    async with pool.acquire() as conn:
        await conn.execute(CANGZHU_DDL)


# ── 纯计算（无 DB，可单元测试）─────────────────────────────────────────


def _round_cents(amount: Decimal) -> int:
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def referral_reward_cents(eligible_cents: int) -> int:
    """合资格金额 10% 的推荐奖励（分，四舍五入）。"""

    if eligible_cents <= 0:
        return 0
    return _round_cents(Decimal(eligible_cents) * REFERRAL_REWARD_RATE)


def member_discount_cents(eligible_cents: int) -> int:
    """会员 9 折的折扣额（分）。"""

    if eligible_cents <= 0:
        return 0
    return _round_cents(Decimal(eligible_cents) * MEMBER_DISCOUNT_RATE)


def voucher_redeemable_cents(eligible_cents: int, balance_cents: int) -> int:
    """本单可用的券抵扣额：余额与合资格金额 20% 上限取小，余额为负则 0。"""

    if eligible_cents <= 0 or balance_cents <= 0:
        return 0
    cap = _round_cents(Decimal(eligible_cents) * VOUCHER_REDEEM_CAP_RATE)
    return min(balance_cents, cap)


def night_now() -> datetime:
    """当前北京时间（UTC+8 固定偏移的 aware 时间）。"""

    return datetime.now(NIGHT_MARKET_TZ)


def night_discount_rate(now: datetime | None = None) -> Decimal:
    """当前时段的夜市折扣率（0 / 0.20 / 0.30 / 0.40）。

    now 为 None 取当前北京时间；naive 时间按北京时间墙上时间理解；
    aware 时间先换算到北京时间再取小时。边界：20:00 整进 8 折档，
    21:00 整切 7 折档，22:00 整切 6 折档，23:00 整出夜市。
    """

    current = now if now is not None else night_now()
    if current.tzinfo is None:
        local_hour = current.hour
    else:
        local_hour = current.astimezone(NIGHT_MARKET_TZ).hour
    if local_hour < NIGHT_MARKET_START_HOUR or local_hour >= NIGHT_MARKET_END_HOUR:
        return Decimal("0")
    for start_hour, rate in NIGHT_MARKET_TIERS:
        if local_hour == start_hour:
            return rate
    return Decimal("0")


def night_discount_cents(line_total_cents: int, now: datetime | None = None) -> int:
    """夜市行合计的折扣额（分，四舍五入）；非夜市时段为 0。"""

    if line_total_cents <= 0:
        return 0
    return _round_cents(Decimal(line_total_cents) * night_discount_rate(now))


def night_price_cents(unit_price_cents: int, now: datetime | None = None) -> int:
    """单件夜市价（分）；非夜市时段等于原价。"""

    if unit_price_cents <= 0:
        return 0
    return max(0, unit_price_cents - night_discount_cents(unit_price_cents, now))


def choose_discount(
    eligible_cents: int,
    balance_cents: int,
    *,
    is_member: bool,
    night_cents: int = 0,
) -> tuple[str, int]:
    """互斥取最优：夜市价、9 折、券抵扣三选一，取折扣额最大者。

    night_cents 是调用方按夜市行合计算好的夜市折扣额（夜市标记行、
    当前时段）；night_cents 缺省 0 时行为与旧版完全一致。平局优先
    顺序：夜市 > 9 折 > 券（前两者不动券余额）。夜市品不可用券由
    调用方保证：夜市行不计入 eligible_cents，券抵扣额只按合资格行算。

    返回 (discount_kind, discount_cents)，kind ∈ none/member/voucher/night。
    """

    member = member_discount_cents(eligible_cents) if is_member else 0
    voucher = voucher_redeemable_cents(eligible_cents, balance_cents)
    night = max(0, night_cents)
    if member <= 0 and voucher <= 0 and night <= 0:
        return "none", 0
    if night >= member and night >= voucher:
        return "night", night
    if voucher > member:
        return "voucher", voucher
    return "member", member


def reversal_cents(accrued_cents: int, refund_cents: int, base_cents: int) -> int:
    """退款冲回额：已发奖励 × 退款占合资格基数比例，上限为已发奖励。"""

    if accrued_cents <= 0 or refund_cents <= 0 or base_cents <= 0:
        return 0
    raw = _round_cents(Decimal(accrued_cents) * Decimal(refund_cents) / Decimal(base_cents))
    return max(0, min(raw, accrued_cents))


def is_valid_referral_code(code: str) -> bool:
    """推荐码格式：HM + 6 位大写字母/数字。"""

    if len(code) != REFERRAL_CODE_LEN or not code.startswith(REFERRAL_CODE_PREFIX):
        return False
    alphabet = set(string.ascii_uppercase + string.digits)
    return all(ch in alphabet for ch in code[2:])


def generate_referral_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return REFERRAL_CODE_PREFIX + "".join(secrets.choice(alphabet) for _ in range(6))


# ── 推荐绑定 ────────────────────────────────────────────────────────────


async def ensure_referral_code(conn: Any, customer_id: str) -> str:
    """取顾客推荐码；没有则生成并落库（WHERE 守卫 + 唯一冲突重试）。"""

    row = await conn.fetchrow(
        'SELECT referral_code FROM "customer" WHERE id = $1 FOR UPDATE',
        customer_id,
    )
    if row is None:
        raise ValueError(f"customer not found: {customer_id}")
    if row["referral_code"]:
        return str(row["referral_code"])
    for _ in range(5):
        code = generate_referral_code()
        try:
            await conn.execute(
                'UPDATE "customer" SET referral_code = $1 WHERE id = $2 AND referral_code IS NULL',
                code,
                customer_id,
            )
        except UniqueViolationError:
            continue
        check = await conn.fetchrow(
            'SELECT referral_code FROM "customer" WHERE id = $1', customer_id
        )
        if check and check["referral_code"]:
            return str(check["referral_code"])
    raise ValueError("could not allocate a unique referral code")


async def resolve_referrer_id(conn: Any, referral_code: str) -> str:
    """推荐码 → 推荐人 id；码无效/账号不可用则 ValueError。"""

    code = referral_code.strip().upper()
    if not is_valid_referral_code(code):
        raise ValueError(f"invalid referral code: {referral_code}")
    row = await conn.fetchrow(
        'SELECT id FROM "customer" WHERE referral_code = $1 AND deleted_at IS NULL '
        "AND status = 'active'",
        code,
    )
    if row is None:
        raise ValueError(f"referral code not found: {code}")
    return str(row["id"])


async def bind_referral(conn: Any, *, customer_id: str, referrer_id: str) -> None:
    """写入 invited_by：仅当未绑定时；自我推荐与重复绑定拒绝。

    调用方须在同一事务内持有顾客行锁（或接受 WHERE 守卫的单次语义）。
    """

    if customer_id == referrer_id:
        raise ValueError("self referral is not allowed")
    updated = await conn.fetchval(
        'UPDATE "customer" SET invited_by = $1 '
        "WHERE id = $2 AND invited_by IS NULL AND deleted_at IS NULL "
        "RETURNING id",
        referrer_id,
        customer_id,
    )
    if updated is None:
        row = await conn.fetchrow('SELECT invited_by FROM "customer" WHERE id = $1', customer_id)
        if row is None:
            raise ValueError(f"customer not found: {customer_id}")
        if row["invited_by"] is not None:
            raise ValueError("referral already bound and cannot be changed")
        raise ValueError(f"customer not found: {customer_id}")


# ── 券账本 ──────────────────────────────────────────────────────────────


async def get_voucher_balance(conn: Any, customer_id: str) -> int:
    row = await conn.fetchrow(
        'SELECT voucher_balance_cents FROM "customer" WHERE id = $1', customer_id
    )
    if row is None:
        raise ValueError(f"customer not found: {customer_id}")
    return int(row["voucher_balance_cents"] or 0)


async def _insert_ledger_row(
    conn: Any,
    *,
    customer_id: str,
    kind: str,
    status: str,
    amount_cents: int,
    order_id: str | None,
    ref_id: str | None,
    base_cents: int | None,
    idempotency_key: str,
    balance_after_cents: int,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """插入账本行；幂等键冲突时返回 False（已记过，不重复记）。"""

    import json as _json

    row = await conn.fetchrow(
        """
        INSERT INTO voucher_ledger
            (customer_id, kind, status, order_id, ref_id, amount_cents,
             base_cents, balance_after_cents, idempotency_key, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        customer_id,
        kind,
        status,
        order_id,
        ref_id,
        amount_cents,
        base_cents,
        balance_after_cents,
        idempotency_key,
        _json.dumps(metadata or {}, ensure_ascii=False),
    )
    return row is not None


async def hold_redemption(conn: Any, *, customer_id: str, order_id: str, amount_cents: int) -> None:
    """下单时持有券抵扣：先记账本行（幂等），再扣余额（余额不足则失败）。

    须在订单创建的同一事务内调用，事务回滚时持有同步撤销。
    """

    if amount_cents <= 0:
        return
    inserted = await _insert_ledger_row(
        conn,
        customer_id=customer_id,
        kind="redemption",
        status="held",
        amount_cents=-amount_cents,
        order_id=order_id,
        ref_id=None,
        base_cents=None,
        idempotency_key=f"redeem:{order_id}",
        balance_after_cents=0,  # 占位：余额更新后回填
    )
    if not inserted:
        return  # 同一订单重试：已持有，不重复扣
    updated = await conn.fetchval(
        'UPDATE "customer" SET voucher_balance_cents = voucher_balance_cents - $1 '
        "WHERE id = $2 AND voucher_balance_cents >= $1 "
        "RETURNING voucher_balance_cents",
        amount_cents,
        customer_id,
    )
    if updated is None:
        raise ValueError("insufficient voucher balance")
    await conn.execute(
        "UPDATE voucher_ledger SET balance_after_cents = $1, updated_at = NOW() "
        "WHERE idempotency_key = $2",
        int(updated),
        f"redeem:{order_id}",
    )


async def finalize_redemption(pool: Any, order_id: str) -> bool:
    """支付确认后：持有券转为已核销。幂等（只动 held 行）。"""

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE voucher_ledger SET status = 'posted', updated_at = NOW() "
            "WHERE order_id = $1 AND kind = 'redemption' AND status = 'held' "
            "RETURNING id",
            order_id,
        )
        return row is not None


async def release_redemption(pool: Any, order_id: str) -> bool:
    """订单取消：返还持有的券。幂等（只动 held 行）。"""

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, customer_id, amount_cents FROM voucher_ledger "
                "WHERE order_id = $1 AND kind = 'redemption' AND status = 'held' "
                "FOR UPDATE",
                order_id,
            )
            if row is None:
                return False
            await conn.execute(
                "UPDATE voucher_ledger SET status = 'released', updated_at = NOW() WHERE id = $1",
                row["id"],
            )
            await conn.execute(
                'UPDATE "customer" SET voucher_balance_cents = voucher_balance_cents - $1 '
                "WHERE id = $2",
                int(row["amount_cents"]),  # amount 为负，减负即返还
                row["customer_id"],
            )
            return True


async def compute_eligible_base(conn: Any, order_id: str) -> int:
    """订单合资格基数：非排除商品行合计 − 券抵扣 − 运费 − 已成功退款。

    排除口径：product.voucher_excluded（特价/清仓/礼品卡/限促等由管理端标记；
    夜市价商品以 product.night_clearance 标记，置标时同步 voucher_excluded=true，
    故按 voucher_excluded 排除即含夜市品）。预订行（batch 为空）按行合计
    计入，后续退款冲回时再扣减。
    """

    lines_total = await conn.fetchval(
        """
        SELECT COALESCE(SUM(oli.line_total_cents), 0)
        FROM order_line_item oli
        JOIN product_variant pv ON pv.id = oli.variant_id
        JOIN product p ON p.id = pv.product_id
        WHERE oli.order_id = $1 AND NOT COALESCE(p.voucher_excluded, false)
        """,
        order_id,
    )
    order = await conn.fetchrow(
        "SELECT shipping_cents, voucher_redeemed_cents FROM customer_order WHERE id = $1",
        order_id,
    )
    if order is None:
        raise ValueError(f"order not found: {order_id}")
    refunded = await conn.fetchval(
        "SELECT COALESCE(SUM(actual_refunded_amount), 0) FROM refund_request "
        "WHERE order_id = $1 AND status = 'succeeded'",
        order_id,
    )
    base = (
        int(lines_total or 0)
        - int(order["voucher_redeemed_cents"] or 0)
        - int(order["shipping_cents"] or 0)
        - int(refunded or 0)
    )
    return max(0, base)


async def settle_accrual(pool: Any, order_id: str) -> dict[str, Any]:
    """订单完成态后结算推荐奖励：合资格基数 10% 发给直接推荐人。

    幂等（accrual:{order_id}）；无推荐人/基数为 0/已冻结时跳过不记账。
    余额可为负：先填负再累积由同一 UPDATE 语义保证。
    """

    async with pool.acquire() as conn:
        async with conn.transaction():
            order = await conn.fetchrow(
                "SELECT id, customer_id, voucher_accrual_frozen FROM customer_order "
                "WHERE id = $1 AND status = 'completed'",
                order_id,
            )
            if order is None:
                return {"settled": False, "reason": "order not completed"}
            if order["voucher_accrual_frozen"]:
                return {"settled": False, "reason": "frozen for review"}
            buyer = await conn.fetchrow(
                'SELECT invited_by FROM "customer" WHERE id = $1 AND deleted_at IS NULL',
                order["customer_id"],
            )
            referrer_id = str(buyer["invited_by"]) if buyer and buyer["invited_by"] else None
            if not referrer_id:
                return {"settled": False, "reason": "no referrer"}
            base = await compute_eligible_base(conn, order_id)
            amount = referral_reward_cents(base)
            if amount <= 0:
                return {"settled": False, "reason": "zero eligible base", "base_cents": base}
            new_balance = await conn.fetchval(
                'UPDATE "customer" SET voucher_balance_cents = voucher_balance_cents + $1 '
                "WHERE id = $2 RETURNING voucher_balance_cents",
                amount,
                referrer_id,
            )
            inserted = await _insert_ledger_row(
                conn,
                customer_id=referrer_id,
                kind="accrual",
                status="posted",
                amount_cents=amount,
                order_id=order_id,
                ref_id=None,
                base_cents=base,
                idempotency_key=f"accrual:{order_id}",
                balance_after_cents=int(new_balance),
            )
            if not inserted:
                # 并发重入：账本行已存在，本次多加的余额须回滚——但本事务内
                # 无法单独撤销 UPDATE，改为直接补偿扣回，保持“记账一次、余额一次”。
                await conn.execute(
                    'UPDATE "customer" SET voucher_balance_cents = '
                    "voucher_balance_cents - $1 WHERE id = $2",
                    amount,
                    referrer_id,
                )
                return {"settled": False, "reason": "already settled"}
            return {
                "settled": True,
                "referrer_id": referrer_id,
                "base_cents": base,
                "amount_cents": amount,
            }


async def reverse_for_refund(
    pool: Any, *, refund_id: str, order_id: str, refund_cents: int
) -> dict[str, Any]:
    """退款成功后按比例冲回已发奖励。幂等（reversal:{refund_id}）。"""

    async with pool.acquire() as conn:
        async with conn.transaction():
            accrual = await conn.fetchrow(
                "SELECT id, customer_id, amount_cents, base_cents FROM voucher_ledger "
                "WHERE order_id = $1 AND kind = 'accrual' AND status = 'posted' "
                "ORDER BY created_at LIMIT 1",
                order_id,
            )
            if accrual is None:
                return {"reversed": False, "reason": "no posted accrual"}
            already = await conn.fetchval(
                "SELECT COALESCE(SUM(-amount_cents), 0) FROM voucher_ledger "
                "WHERE order_id = $1 AND kind = 'reversal' AND status = 'posted'",
                order_id,
            )
            remaining = int(accrual["amount_cents"]) - int(already or 0)
            amount = min(
                reversal_cents(
                    int(accrual["amount_cents"]), refund_cents, int(accrual["base_cents"] or 0)
                ),
                max(0, remaining),
            )
            if amount <= 0:
                return {"reversed": False, "reason": "nothing to reverse"}
            new_balance = await conn.fetchval(
                'UPDATE "customer" SET voucher_balance_cents = voucher_balance_cents - $1 '
                "WHERE id = $2 RETURNING voucher_balance_cents",
                amount,
                accrual["customer_id"],
            )
            inserted = await _insert_ledger_row(
                conn,
                customer_id=str(accrual["customer_id"]),
                kind="reversal",
                status="posted",
                amount_cents=-amount,
                order_id=order_id,
                ref_id=refund_id,
                base_cents=int(accrual["base_cents"] or 0),
                idempotency_key=f"reversal:{refund_id}",
                balance_after_cents=int(new_balance),
            )
            if not inserted:
                await conn.execute(
                    'UPDATE "customer" SET voucher_balance_cents = '
                    "voucher_balance_cents + $1 WHERE id = $2",
                    amount,
                    accrual["customer_id"],
                )
                return {"reversed": False, "reason": "already reversed"}
            return {"reversed": True, "amount_cents": amount}


async def freeze_accrual(pool: Any, order_id: str) -> bool:
    """异常订单冻结：后续完成结算跳过，待人工审。"""

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE customer_order SET voucher_accrual_frozen = true "
            "WHERE id = $1 AND voucher_accrual_frozen = false RETURNING id",
            order_id,
        )
        return row is not None


async def unfreeze_accrual(pool: Any, order_id: str) -> dict[str, Any]:
    """解冻并重新尝试结算（人工审通过后调用）。"""

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE customer_order SET voucher_accrual_frozen = false WHERE id = $1",
            order_id,
        )
    return await settle_accrual(pool, order_id)


# ── 邀请礼 ──────────────────────────────────────────────────────────────


async def check_invite_gift_eligibility(pool: Any, customer_id: str) -> dict[str, Any]:
    """首单实付满阈值且未领过礼，返回 eligible。

    首单 = 最早进入已付款状态的订单；实付 = 该单 payment_intent 已付合计 −
    成功退款合计。口径如实记录：以 payment_intent 为准，不含券抵扣口径争议
    （券抵扣已反映在 grand_total 中，intent 金额即实付）。
    """

    async with pool.acquire() as conn:
        claimed = await conn.fetchval(
            "SELECT id FROM invite_gift_claim WHERE customer_id = $1", customer_id
        )
        if claimed is not None:
            return {"eligible": False, "reason": "already claimed"}
        # $1 是 customer_id，状态占位从 $2 起。
        placeholders = ",".join(f"${i + 2}" for i in range(len(PAID_ORDER_STATUSES)))
        first = await conn.fetchrow(
            f"SELECT id, created_at FROM customer_order WHERE customer_id = $1 "
            f"AND status IN ({placeholders}) ORDER BY created_at ASC LIMIT 1",
            customer_id,
            *PAID_ORDER_STATUSES,
        )
        if first is None:
            return {"eligible": False, "reason": "no paid order"}
        # payment_intent.order_id 是 TEXT 列，UUID 对象须转 str。
        first_order_id = str(first["id"])
        paid = await conn.fetchval(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM payment_intent "
            "WHERE order_id = $1 AND status = 'paid'",
            first_order_id,
        )
        refunded = await conn.fetchval(
            "SELECT COALESCE(SUM(actual_refunded_amount), 0) FROM refund_request "
            "WHERE order_id = $1 AND status = 'succeeded'",
            first_order_id,
        )
        net_paid = int(paid or 0) - int(refunded or 0)
        return {
            "eligible": net_paid >= INVITE_GIFT_THRESHOLD_CENTS,
            "order_id": str(first["id"]),
            "net_paid_cents": net_paid,
            "threshold_cents": INVITE_GIFT_THRESHOLD_CENTS,
            "reason": None
            if net_paid >= INVITE_GIFT_THRESHOLD_CENTS
            else "first order below threshold",
        }


async def claim_invite_gift(pool: Any, customer_id: str, gift_kind: str) -> dict[str, Any]:
    """领取邀请礼：资格复核 + 一人一次（UNIQUE 守卫）。"""

    if gift_kind not in ("egg", "vegetable"):
        raise ValueError("gift_kind must be 'egg' or 'vegetable'")
    check = await check_invite_gift_eligibility(pool, customer_id)
    if not check["eligible"]:
        raise ValueError(f"not eligible for invite gift: {check.get('reason')}")
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO invite_gift_claim "
                "(customer_id, order_id, gift_kind, cost_cap_cents) "
                "VALUES ($1, $2, $3, $4) RETURNING id, status",
                customer_id,
                check["order_id"],
                gift_kind,
                INVITE_GIFT_COST_CAP_CENTS,
            )
        except UniqueViolationError as exc:
            raise ValueError("invite gift already claimed") from exc
    return {"claim_id": str(row["id"]), "status": row["status"], "gift_kind": gift_kind}


# ── 仓主点货 ────────────────────────────────────────────────────────────

PURCHASE_REQUEST_TRANSITIONS: dict[str, set[str]] = {
    "collected": {"price_confirmed", "failed"},
    "price_confirmed": {"purchasing", "failed"},
    "purchasing": {"arrived", "failed"},
    "arrived": {"delivered", "failed"},
    "delivered": set(),
    "failed": set(),
}


def validate_purchase_request_transition(from_status: str, to_status: str) -> None:
    allowed = PURCHASE_REQUEST_TRANSITIONS.get(from_status)
    if allowed is None or to_status not in allowed:
        raise ValueError(f"invalid purchase request transition: {from_status} -> {to_status}")


async def create_purchase_request(pool: Any, *, customer_id: str, body: dict[str, Any]) -> str:
    name = str(body.get("product_name") or "").strip()
    if not name:
        raise ValueError("product_name is required")
    price_min = body.get("price_min_cents")
    price_max = body.get("price_max_cents")
    if price_min is not None and price_max is not None and int(price_min) > int(price_max):
        raise ValueError("price_min_cents must not exceed price_max_cents")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO member_purchase_request
                (customer_id, product_name, brand, spec_packaging,
                 price_min_cents, price_max_cents, expected_qty, willing_preorder)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
            """,
            customer_id,
            name,
            str(body.get("brand") or "").strip(),
            str(body.get("spec_packaging") or "").strip(),
            price_min,
            price_max,
            max(1, int(body.get("expected_qty") or 1)),
            bool(body.get("willing_preorder") or False),
        )
        return str(row["id"])


async def list_purchase_requests(
    pool: Any, *, customer_id: str | None, status: str | None, limit: int, offset: int
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if customer_id is not None:
        conditions.append(f"customer_id = ${len(params) + 1}")
        params.append(customer_id)
    if status is not None:
        conditions.append(f"status = ${len(params) + 1}")
        params.append(status)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT id, customer_id, product_name, brand, spec_packaging, "
            "price_min_cents, price_max_cents, expected_qty, willing_preorder, "
            "status, admin_note, failure_reason, created_at, updated_at "
            f"FROM member_purchase_request {where} "
            "ORDER BY created_at DESC LIMIT $"
            f"{len(params) + 1} OFFSET ${len(params) + 2}",
            *params,
            limit,
            offset,
        )
        return [dict(r) for r in rows]


async def update_purchase_request(
    pool: Any, *, request_id: str, to_status: str, admin_note: str, failure_reason: str
) -> dict[str, Any]:
    if to_status == "failed" and not failure_reason.strip():
        raise ValueError("failure_reason is required when marking failed")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT status FROM member_purchase_request WHERE id = $1 FOR UPDATE",
                request_id,
            )
            if row is None:
                raise ValueError(f"purchase request not found: {request_id}")
            validate_purchase_request_transition(row["status"], to_status)
            updated = await conn.fetchrow(
                "UPDATE member_purchase_request SET status = $1, admin_note = $2, "
                "failure_reason = $3, updated_at = NOW() WHERE id = $4 "
                "RETURNING id, status, admin_note, failure_reason",
                to_status,
                admin_note,
                failure_reason,
                request_id,
            )
            return dict(updated)


async def purchase_request_leaderboard(
    pool: Any, *, days: int = 7, limit: int = 20
) -> list[dict[str, Any]]:
    """每周点货榜：近 N 天按商品名汇总需求条数与总期望量。"""

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT product_name, COUNT(*)::int AS request_count, "
            "COALESCE(SUM(expected_qty), 0)::int AS total_qty "
            "FROM member_purchase_request "
            "WHERE created_at >= NOW() - $1 * INTERVAL '1 day' "
            "GROUP BY product_name ORDER BY request_count DESC, total_qty DESC "
            "LIMIT $2",
            days,
            limit,
        )
        return [dict(r) for r in rows]


# ── 每日裂变账本 ────────────────────────────────────────────────────────


async def growth_ledger(pool: Any, day: date) -> dict[str, Any]:
    """指定自然日的裂变账本。口径逐项标注；无数据源的项返回 null 并记入 gaps。"""

    gaps: list[str] = []
    async with pool.acquire() as conn:
        new_owners = await conn.fetchval(
            'SELECT COUNT(*) FROM "customer" WHERE created_at::date = $1 AND deleted_at IS NULL',
            day,
        )
        referred = await conn.fetchval(
            'SELECT COUNT(*) FROM "customer" WHERE created_at::date = $1 '
            "AND invited_by IS NOT NULL AND deleted_at IS NULL",
            day,
        )
        first_time = await conn.fetchval(
            """
            SELECT COUNT(*) FROM (
                SELECT customer_id, MIN(h.created_at)::date AS first_paid_day
                FROM order_status_history h
                JOIN customer_order o ON o.id = h.order_id
                WHERE h.to_status = 'confirmed' AND o.customer_id IS NOT NULL
                GROUP BY customer_id
            ) firsts WHERE first_paid_day = $1
            """,
            day,
        )
        repeat_30d = await conn.fetchval(
            """
            SELECT COUNT(*) FROM (
                SELECT customer_id
                FROM customer_order
                WHERE status = ANY($2::text[])
                AND created_at >= $1::timestamptz - INTERVAL '30 days'
                GROUP BY customer_id HAVING COUNT(*) >= 2
            ) t
            """,
            datetime(day.year, day.month, day.day),
            list(PAID_ORDER_STATUSES),
        )
        spend = await conn.fetchrow(
            """
            SELECT COUNT(DISTINCT customer_id)::int AS buyers,
                   COALESCE(SUM(grand_total_cents), 0)::int AS total_cents
            FROM customer_order
            WHERE status = ANY($2::text[]) AND created_at::date = $1
            """,
            day,
            list(PAID_ORDER_STATUSES),
        )
        issued = await conn.fetchval(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM voucher_ledger "
            "WHERE kind = 'accrual' AND status = 'posted' AND created_at::date = $1",
            day,
        )
        redeemed = await conn.fetchval(
            "SELECT COALESCE(SUM(-amount_cents), 0) FROM voucher_ledger "
            "WHERE kind = 'redemption' AND status = 'posted' AND created_at::date = $1",
            day,
        )
        margin = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(oli.line_total_cents), 0)::int AS revenue_cents,
                   COALESCE(SUM(oli.quantity * ib.cost_price_cents), 0)::int AS cost_cents
            FROM order_line_item oli
            JOIN customer_order o ON o.id = oli.order_id
            JOIN inventory_batch ib ON ib.id = oli.batch_id
            WHERE o.status = 'completed' AND o.created_at::date = $1
            """,
            day,
        )
        requests = await conn.fetchrow(
            "SELECT COUNT(*)::int AS total, "
            "COUNT(*) FILTER (WHERE status = 'delivered')::int AS delivered, "
            "COUNT(*) FILTER (WHERE status = 'failed')::int AS failed "
            "FROM member_purchase_request WHERE created_at::date = $1",
            day,
        )
        preorder = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(quantity), 0)::int AS qty,
                   COALESCE(SUM(preorder_qty), 0)::int AS preorder_qty
            FROM order_line_item oli JOIN customer_order o ON o.id = oli.order_id
            WHERE o.created_at::date = $1
            """,
            day,
        )
    buyers = int(spend["buyers"] or 0)
    total_cents = int(spend["total_cents"] or 0)
    revenue = int(margin["revenue_cents"] or 0)
    cost = int(margin["cost_cents"] or 0)
    decided = int(requests["delivered"] or 0) + int(requests["failed"] or 0)
    gaps.append("spoilage_rate: stock_movement 无存量行，无损耗口径")
    gaps.append("margin: 仅已完成订单中有批次成本的行计入，预订未配货行无成本，口径偏高")
    return {
        "day": day.isoformat(),
        "new_owners": int(new_owners or 0),
        "referred_registrations": int(referred or 0),
        "first_time_buyers": int(first_time or 0),
        "repeat_buyers_30d": int(repeat_30d or 0),
        "avg_spend_cents": (total_cents // buyers) if buyers else 0,
        "voucher_issued_cents": int(issued or 0),
        "voucher_redeemed_cents": int(redeemed or 0),
        "margin_cents": revenue - cost,
        "margin_rate": round((revenue - cost) / revenue, 4) if revenue else None,
        "request_fulfill_rate": round(int(requests["delivered"] or 0) / decided, 4)
        if decided
        else None,
        "preorder_unfulfilled_rate": round(
            int(preorder["preorder_qty"] or 0) / int(preorder["qty"] or 1), 4
        )
        if int(preorder["qty"] or 0)
        else None,
        "spoilage_rate": None,
        "gaps": gaps,
    }
