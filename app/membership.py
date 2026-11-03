"""会员补齐（缺口 4）：等级 + 积分 + 储值。

规则依据：~/workspace/hemal-cangzhu-rules.md（规则 2 合资格口径）。
授权默认值（Soffy 可否决，见 docs/membership.md）：
1. 等级只作身份与统计，不给价格折扣（折扣仍只有 9 折，互斥规则一字不动）。
2. 积分按规则 2 合资格口径 1 元积 1 分；独立账本，永不过期、不可转让、
   无现金价值；本批只积不兑，不接 checkout、不发明兑换比例。
3. 储值独立账本 + 商家记账式充值（带外部支付凭证号，只记账，不调用任何
   支付网关）；余额可查，不可提现、不可转让。短信一律不做（无网关/凭证）。

与既有概念的区分（勿混淆）：
- membership 表是付费订阅（process_subscription 收算力费、给进场权限），
  与本模块的消费等级 member_tier 无关；本模块不读不写 membership 表。
- 绝不复用 customer.system_balance（SLA/UGC 算力金）与 voucher_ledger
  （购物券账本）；积分、储值各走自己的 append-only 账本。
- 积分累积不自动挂订单完成钩子：本批不碰 app/orders 与 checkout，结算
  触发走商家端点 POST /store/orders/{id}/points-accrue（幂等），待后续
  批次另行决定是否接完成钩子。退款冲回同理走商家端点显式触发。

本模块只含试点逻辑与幂等 DDL，不 import 任何 app 内模块，避免循环导入；
合资格口径复用 app.cangzhu.compute_eligible_base（函数内懒导入，与券账本
同一口径，排除标记 voucher_excluded 亦同一标记）。
"""

from __future__ import annotations

import json
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

logger = logging.getLogger("hemall.membership")

# ── 配置常量（改动须经 Soffy 拍板；不写死在 SQL 里）────────────────────
POINTS_CENTS_PER_POINT = 100  # 每满 100 分（1 元）合资格金额积 1 分，不足不计
TIER_REGULAR = "regular"
TIER_SILVER = "silver"
TIER_GOLD = "gold"
# 等级阈值：累计合资格金额（分，净额，退款冲回后扣减）。等级只作身份统计。
TIER_THRESHOLDS: tuple[tuple[str, int], ...] = (
    (TIER_REGULAR, 0),
    (TIER_SILVER, 50000),  # 累计合资格满 500 元
    (TIER_GOLD, 200000),  # 累计合资格满 2000 元
)
TIER_LABELS: dict[str, str] = {
    TIER_REGULAR: "普通会员",
    TIER_SILVER: "银卡会员",
    TIER_GOLD: "金卡会员",
}
MAX_TOPUP_NOTE_CHARS = 200

MEMBERSHIP_DDL = """
ALTER TABLE customer ADD COLUMN IF NOT EXISTS points_balance INT NOT NULL DEFAULT 0;
ALTER TABLE customer ADD COLUMN IF NOT EXISTS stored_value_balance_cents INT NOT NULL DEFAULT 0;
ALTER TABLE customer ADD COLUMN IF NOT EXISTS lifetime_eligible_cents INT NOT NULL DEFAULT 0;
ALTER TABLE customer ADD COLUMN IF NOT EXISTS member_tier TEXT NOT NULL DEFAULT 'regular';

CREATE TABLE IF NOT EXISTS points_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('accrual', 'reversal')),
    status TEXT NOT NULL CHECK (status IN ('posted', 'void')),
    order_id UUID,
    ref_id TEXT,
    points INT NOT NULL CHECK (points <> 0),
    base_cents INT,
    balance_after INT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_points_ledger_customer ON points_ledger(customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_points_ledger_order ON points_ledger(order_id, kind);

CREATE TABLE IF NOT EXISTS stored_value_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('topup')),
    status TEXT NOT NULL CHECK (status IN ('posted', 'void')),
    external_payment_ref TEXT NOT NULL CHECK (external_payment_ref <> ''),
    operator_id UUID,
    amount_cents INT NOT NULL CHECK (amount_cents > 0),
    balance_after_cents INT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_stored_value_ledger_customer
    ON stored_value_ledger(customer_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_stored_value_topup_ref
    ON stored_value_ledger(customer_id, external_payment_ref);
"""


async def ensure_membership_schema(pool: Any) -> None:
    """幂等创建会员补齐所需表、列、索引。"""

    async with pool.acquire() as conn:
        await conn.execute(MEMBERSHIP_DDL)


# ── 纯计算（无 DB，可单元测试）─────────────────────────────────────────


def points_for_eligible(eligible_cents: int) -> int:
    """合资格金额换算积分：每满 1 元 1 分，不足 1 元的部分不计。"""

    if eligible_cents <= 0:
        return 0
    return eligible_cents // POINTS_CENTS_PER_POINT


def tier_for_lifetime(lifetime_cents: int) -> str:
    """累计合资格净额对应的等级（取满足的最高阈值）。"""

    tier = TIER_REGULAR
    for name, threshold in TIER_THRESHOLDS:
        if lifetime_cents >= threshold:
            tier = name
    return tier


def tier_progress(lifetime_cents: int) -> dict[str, Any]:
    """等级进度：当前等级、下一等级与还差金额（已最高级则 next 为 None）。"""

    current = tier_for_lifetime(lifetime_cents)
    next_tier: str | None = None
    next_threshold: int | None = None
    for name, threshold in TIER_THRESHOLDS:
        if lifetime_cents < threshold:
            next_tier = name
            next_threshold = threshold
            break
    return {
        "current_tier": current,
        "next_tier": next_tier,
        "next_threshold_cents": next_threshold,
        "remaining_cents": (next_threshold - lifetime_cents) if next_threshold is not None else 0,
    }


def reversal_points(accrued_points: int, refund_cents: int, base_cents: int) -> int:
    """退款冲回积分：已发积分 x 退款占合资格基数比例，上限为已发积分。"""

    if accrued_points <= 0 or refund_cents <= 0 or base_cents <= 0:
        return 0
    raw = (Decimal(accrued_points) * Decimal(refund_cents) / Decimal(base_cents)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return max(0, min(int(raw), accrued_points))


# ── 账本写入辅助 ────────────────────────────────────────────────────────


async def _insert_points_row(
    conn: Any,
    *,
    customer_id: str,
    kind: str,
    points: int,
    order_id: str | None,
    ref_id: str | None,
    base_cents: int | None,
    idempotency_key: str,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    """插入积分账本行；幂等键冲突时返回 None（已记过，不重复记）。"""

    row = await conn.fetchrow(
        """
        INSERT INTO points_ledger
            (customer_id, kind, status, order_id, ref_id, points,
             base_cents, balance_after, idempotency_key, metadata)
        VALUES ($1, $2, 'posted', $3, $4, $5, $6, 0, $7, $8::jsonb)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        customer_id,
        kind,
        order_id,
        ref_id,
        points,
        base_cents,
        idempotency_key,
        json.dumps(metadata or {}, ensure_ascii=False),
    )
    return str(row["id"]) if row is not None else None


async def _refresh_tier(conn: Any, customer_id: str, lifetime_cents: int) -> str:
    tier = tier_for_lifetime(lifetime_cents)
    await conn.execute('UPDATE "customer" SET member_tier = $1 WHERE id = $2', tier, customer_id)
    return tier


# ── 积分累积与冲回 ──────────────────────────────────────────────────────


async def accrue_points(pool: Any, order_id: str) -> dict[str, Any]:
    """订单完成态后按合资格口径累积积分（买家本人得积分）。

    幂等（points-accrual:{order_id}）；订单未完成/基数为 0/不足 1 元时跳过
    不记账。须由商家端点显式触发，本批不挂订单完成钩子。
    """

    from app.cangzhu import compute_eligible_base

    async with pool.acquire() as conn:
        async with conn.transaction():
            order = await conn.fetchrow(
                "SELECT id, customer_id FROM customer_order WHERE id = $1 AND status = 'completed'",
                order_id,
            )
            if order is None:
                return {"accrued": False, "reason": "order not completed"}
            customer_id = str(order["customer_id"])
            base = await compute_eligible_base(conn, order_id)
            points = points_for_eligible(base)
            if points <= 0:
                return {"accrued": False, "reason": "zero eligible base", "base_cents": base}
            entry_id = await _insert_points_row(
                conn,
                customer_id=customer_id,
                kind="accrual",
                points=points,
                order_id=order_id,
                ref_id=None,
                base_cents=base,
                idempotency_key=f"points-accrual:{order_id}",
            )
            if entry_id is None:
                return {"accrued": False, "reason": "already accrued", "base_cents": base}
            updated = await conn.fetchrow(
                'UPDATE "customer" SET points_balance = points_balance + $1, '
                "lifetime_eligible_cents = lifetime_eligible_cents + $2 "
                "WHERE id = $3 RETURNING points_balance, lifetime_eligible_cents",
                points,
                base,
                customer_id,
            )
            if updated is None:
                raise ValueError(f"customer not found: {customer_id}")
            tier = await _refresh_tier(conn, customer_id, int(updated["lifetime_eligible_cents"]))
            await conn.execute(
                "UPDATE points_ledger SET balance_after = $1, updated_at = NOW() WHERE id = $2",
                int(updated["points_balance"]),
                entry_id,
            )
            return {
                "accrued": True,
                "customer_id": customer_id,
                "base_cents": base,
                "points": points,
                "points_balance": int(updated["points_balance"]),
                "lifetime_eligible_cents": int(updated["lifetime_eligible_cents"]),
                "member_tier": tier,
            }


async def reverse_points_for_refund(
    pool: Any, *, refund_id: str, order_id: str, refund_cents: int
) -> dict[str, Any]:
    """退款成功后按比例冲回已发积分。幂等（points-reversal:{refund_id}）。

    冲回上限为该单剩余未冲积分；余额与累计合资格净额同事务扣减（净额不为负），
    等级随净额回落。须由商家端点显式触发，本批不挂退款钩子。
    """

    if not refund_id.strip():
        raise ValueError("refund_id is required")
    if refund_cents <= 0:
        raise ValueError("refund_cents must be positive")
    async with pool.acquire() as conn:
        async with conn.transaction():
            accrual = await conn.fetchrow(
                "SELECT id, customer_id, points, base_cents FROM points_ledger "
                "WHERE order_id = $1 AND kind = 'accrual' AND status = 'posted' "
                "ORDER BY created_at LIMIT 1",
                order_id,
            )
            if accrual is None:
                return {"reversed": False, "reason": "no posted accrual"}
            already = await conn.fetchrow(
                "SELECT COALESCE(SUM(-points), 0) AS pts, "
                "COALESCE(SUM(base_cents), 0) AS base FROM points_ledger "
                "WHERE order_id = $1 AND kind = 'reversal' AND status = 'posted'",
                order_id,
            )
            remaining_points = int(accrual["points"]) - int(already["pts"] or 0)
            remaining_base = int(accrual["base_cents"] or 0) - int(already["base"] or 0)
            points = min(
                reversal_points(
                    int(accrual["points"]), refund_cents, int(accrual["base_cents"] or 0)
                ),
                max(0, remaining_points),
            )
            base_delta = min(refund_cents, max(0, remaining_base))
            if points <= 0:
                return {"reversed": False, "reason": "nothing to reverse"}
            entry_id = await _insert_points_row(
                conn,
                customer_id=str(accrual["customer_id"]),
                kind="reversal",
                points=-points,
                order_id=order_id,
                ref_id=refund_id,
                base_cents=base_delta,
                idempotency_key=f"points-reversal:{refund_id}",
            )
            if entry_id is None:
                return {"reversed": False, "reason": "already reversed"}
            updated = await conn.fetchrow(
                'UPDATE "customer" SET points_balance = points_balance - $1, '
                "lifetime_eligible_cents = GREATEST(0, lifetime_eligible_cents - $2) "
                "WHERE id = $3 AND points_balance >= $1 "
                "RETURNING points_balance, lifetime_eligible_cents",
                points,
                base_delta,
                str(accrual["customer_id"]),
            )
            if updated is None:
                raise ValueError("insufficient points balance for reversal")
            tier = await _refresh_tier(
                conn, str(accrual["customer_id"]), int(updated["lifetime_eligible_cents"])
            )
            await conn.execute(
                "UPDATE points_ledger SET balance_after = $1, updated_at = NOW() WHERE id = $2",
                int(updated["points_balance"]),
                entry_id,
            )
            return {
                "reversed": True,
                "points": points,
                "base_delta_cents": base_delta,
                "points_balance": int(updated["points_balance"]),
                "lifetime_eligible_cents": int(updated["lifetime_eligible_cents"]),
                "member_tier": tier,
            }


# ── 储值记账 ────────────────────────────────────────────────────────────


async def topup_stored_value(
    pool: Any,
    *,
    customer_id: str,
    amount_cents: int,
    external_payment_ref: str,
    operator_id: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    """商家记账式储值充值：只记账，不调用任何支付网关。

    外部支付凭证号（线下/外部渠道的付款凭证）必填且同一顾客下唯一，重复
    提交同一凭证号返回已记账、不重复加余额。余额不可提现、不可转让。
    """

    ref = external_payment_ref.strip()
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")
    if not ref:
        raise ValueError("external_payment_ref is required")
    if len(note) > MAX_TOPUP_NOTE_CHARS:
        raise ValueError("note is too long")
    idempotency_key = f"stored-topup:{customer_id}:{ref}"
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, amount_cents, balance_after_cents FROM stored_value_ledger "
                "WHERE idempotency_key = $1",
                idempotency_key,
            )
            if existing is not None:
                balance = await conn.fetchval(
                    'SELECT stored_value_balance_cents FROM "customer" WHERE id = $1',
                    customer_id,
                )
                return {
                    "recorded": False,
                    "reason": "already recorded",
                    "entry_id": str(existing["id"]),
                    "amount_cents": int(existing["amount_cents"]),
                    "stored_value_balance_cents": int(balance or 0),
                }
            row = await conn.fetchrow(
                """
                INSERT INTO stored_value_ledger
                    (customer_id, kind, status, external_payment_ref, operator_id,
                     amount_cents, balance_after_cents, idempotency_key, metadata)
                VALUES ($1, 'topup', 'posted', $2, $3, $4, 0, $5, $6::jsonb)
                RETURNING id
                """,
                customer_id,
                ref,
                operator_id,
                amount_cents,
                idempotency_key,
                json.dumps({"note": note}, ensure_ascii=False),
            )
            updated = await conn.fetchval(
                'UPDATE "customer" SET stored_value_balance_cents = '
                "stored_value_balance_cents + $1 WHERE id = $2 "
                "RETURNING stored_value_balance_cents",
                amount_cents,
                customer_id,
            )
            if updated is None:
                raise ValueError(f"customer not found: {customer_id}")
            await conn.execute(
                "UPDATE stored_value_ledger SET balance_after_cents = $1, "
                "updated_at = NOW() WHERE id = $2",
                int(updated),
                row["id"],
            )
            return {
                "recorded": True,
                "entry_id": str(row["id"]),
                "amount_cents": amount_cents,
                "stored_value_balance_cents": int(updated),
            }


# ── 查询 ────────────────────────────────────────────────────────────────


async def get_membership_summary(conn: Any, customer_id: str) -> dict[str, Any]:
    """顾客会员总览：等级、积分余额、储值余额、累计合资格净额、等级进度。"""

    row = await conn.fetchrow(
        "SELECT points_balance, stored_value_balance_cents, "
        'lifetime_eligible_cents, member_tier FROM "customer" WHERE id = $1',
        customer_id,
    )
    if row is None:
        raise ValueError(f"customer not found: {customer_id}")
    lifetime = int(row["lifetime_eligible_cents"] or 0)
    tier = str(row["member_tier"] or TIER_REGULAR)
    return {
        "customer_id": customer_id,
        "member_tier": tier,
        "member_tier_label": TIER_LABELS.get(tier, tier),
        "points_balance": int(row["points_balance"] or 0),
        "stored_value_balance_cents": int(row["stored_value_balance_cents"] or 0),
        "lifetime_eligible_cents": lifetime,
        "points_expire": None,
        "points_redeemable": False,
        "tier_thresholds": [
            {"tier": name, "threshold_cents": threshold} for name, threshold in TIER_THRESHOLDS
        ],
        **tier_progress(lifetime),
    }


async def list_points_entries(
    conn: Any, customer_id: str, *, limit: int, offset: int
) -> dict[str, Any]:
    """顾客自查积分账本明细（余额取自 customer 缓存列，与账本同事务维护）。"""

    balance = await conn.fetchval(
        'SELECT points_balance FROM "customer" WHERE id = $1', customer_id
    )
    if balance is None:
        raise ValueError(f"customer not found: {customer_id}")
    rows = await conn.fetch(
        "SELECT id, kind, status, order_id, ref_id, points, base_cents,"
        " balance_after, idempotency_key, created_at FROM points_ledger"
        " WHERE customer_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        customer_id,
        limit,
        offset,
    )
    return {"points_balance": int(balance or 0), "entries": [dict(r) for r in rows]}


async def list_stored_value_entries(
    conn: Any, customer_id: str, *, limit: int, offset: int
) -> dict[str, Any]:
    """顾客自查储值账本明细（余额取自 customer 缓存列，与账本同事务维护）。"""

    balance = await conn.fetchval(
        'SELECT stored_value_balance_cents FROM "customer" WHERE id = $1', customer_id
    )
    if balance is None:
        raise ValueError(f"customer not found: {customer_id}")
    rows = await conn.fetch(
        "SELECT id, kind, status, external_payment_ref, operator_id, amount_cents,"
        " balance_after_cents, idempotency_key, created_at FROM stored_value_ledger"
        " WHERE customer_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        customer_id,
        limit,
        offset,
    )
    return {"stored_value_balance_cents": int(balance or 0), "entries": [dict(r) for r in rows]}
