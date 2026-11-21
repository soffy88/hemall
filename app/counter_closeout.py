"""app.counter_closeout — 柜台收尾件：现金结算 + 交接班 + 日结报表的纯函数与幂等 DDL。

不 import 任何 app 内模块（与 app/weigh.py 同一纪律），供 ensure_p0_schema
与 storefront 柜台端点共用。

记账铁律：
- 结算只认服务端重算的单据合计，绝不信任客户端传来的应收；实收小于应收
  拒单，找零 = 实收 - 应收（Decimal，不用 float）。
- 交班差异 = 实盘现金 - 系统应有现金，正负都如实落账，绝不抹平。
- 日结毛利只计有批次成本的行：成本 = 重量(克) × 批次成本价 / 计价基准(克)，
  无成本价或无基准的行排除在外并计数，绝不估算（口径与裂变账本一致）。

建单链路缺口（2026-11-03 实查）：customer_order 只能从购物车快照创建
（_create_order_from_cart，含预留/预订拆分与券折扣持有），库存扣减
（deduct_stock）只扣已有预留；柜台称重行项无购物车、无预留、无订单，
干净复用不可能。结算因此只记结算事实，不创建 customer_order、不扣库存、
不碰券账本与 9 折逻辑；建单与库存联动缺口记在结算端点注释与交付报告里。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

#: 结算方式：首期只收现金；其它方式调用方必须 422 拒绝，绝不静默记成现金。
SETTLE_METHOD_CASH = "cash"

COUNTER_CLOSEOUT_DDL = """
-- ── 柜台收尾件：现金结算记录（一单一结算，UNIQUE 防重结）──
CREATE TABLE IF NOT EXISTS counter_settlement (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id UUID NOT NULL UNIQUE REFERENCES counter_ticket(id),
    cashier_id TEXT NOT NULL,
    method VARCHAR(20) NOT NULL DEFAULT 'cash' CHECK (method = 'cash'),
    amount_due_cents INT NOT NULL CHECK (amount_due_cents >= 0),
    amount_received_cents INT NOT NULL CHECK (amount_received_cents >= 0),
    change_cents INT NOT NULL CHECK (change_cents >= 0),
    settled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_counter_settlement_cashier
    ON counter_settlement(cashier_id, settled_at);
CREATE INDEX IF NOT EXISTS idx_counter_settlement_day
    ON counter_settlement(settled_at);

-- ── 柜台收尾件：交接班账本 ──
CREATE TABLE IF NOT EXISTS counter_shift (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cashier_id TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'closed')),
    opening_float_cents INT NOT NULL CHECK (opening_float_cents >= 0),
    opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ,
    counted_cash_cents INT CHECK (counted_cash_cents >= 0),
    expected_cash_cents INT,
    variance_cents INT,
    handover_to TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_counter_shift_cashier
    ON counter_shift(cashier_id, opened_at);
"""


def compute_change_cents(amount_due_cents: int, amount_received_cents: int) -> int:
    """找零 = 实收 - 应收；实收不足 raise ValueError（调用方转 422）。"""
    if amount_due_cents < 0:
        raise ValueError("compute_change_cents: amount_due_cents must be non-negative")
    if amount_received_cents < 0:
        raise ValueError("compute_change_cents: amount_received_cents must be non-negative")
    change = amount_received_cents - amount_due_cents
    if change < 0:
        raise ValueError(
            f"amount received {amount_received_cents} is less than amount due {amount_due_cents}"
        )
    return change


def compute_shift_expected_cents(opening_float_cents: int, settled_due_cents: int) -> int:
    """交班时系统应有现金 = 备用金 + 本班现金结算的应收合计（实收 - 找零 = 应收）。"""
    if opening_float_cents < 0:
        raise ValueError("compute_shift_expected_cents: opening_float_cents must be non-negative")
    if settled_due_cents < 0:
        raise ValueError("compute_shift_expected_cents: settled_due_cents must be non-negative")
    return opening_float_cents + settled_due_cents


def compute_shift_variance_cents(counted_cash_cents: int, expected_cash_cents: int) -> int:
    """差异 = 实盘 - 应有；正为多、负为少，原样记录，不抹平。"""
    if counted_cash_cents < 0:
        raise ValueError("compute_shift_variance_cents: counted_cash_cents must be non-negative")
    return counted_cash_cents - expected_cash_cents


def weigh_line_cost_cents(
    weight_grams: int | None,
    price_basis_grams: int | None,
    cost_price_cents: int | None,
) -> int | None:
    """称重行项的成本（分）：重量 × 批次成本价 / 计价基准，四舍五入到分。

    成本价或基准缺失时返回 None（调用方把该行排除在毛利外并计数，绝不估算）。
    """
    if weight_grams is None or price_basis_grams is None or cost_price_cents is None:
        return None
    if weight_grams <= 0 or price_basis_grams <= 0 or cost_price_cents < 0:
        return None
    cost = (
        Decimal(weight_grams) * Decimal(cost_price_cents) / Decimal(price_basis_grams)
    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cost)
