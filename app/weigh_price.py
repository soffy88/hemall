"""app.weigh_price — 线上商品称重定价：纯函数 + 幂等 DDL。

口径（2026-11-08 用户决定）：
- 商品/规格支持两种计价方式：按件（piece）/ 按重量（weight）。
- 前台统一按“斤”展示（1 斤 = 500 克），内部一律以 **克** 记重量、以 **分**
  记金额；按 kg 报价的供应商价格在入库时换算成元/斤，不把 kg 带进订单。
- 下单只锁定 **预估重量** 与预估金额；履约称重后按实际重量结算：
  差额在容差内自动多退少补，超出容差需顾客确认；manual 支付模式只记录
  待人工处理，绝不伪装成已退款/已扣款。
- 金额四舍五入规则：weight(克) × 单价(分/斤) ÷ 500，Decimal
  ROUND_HALF_UP 到整数分。测试固定这一规则。

本模块不 import 任何 app 内模块（与 app/weigh.py 同一纪律），供
ensure_p0_schema 与 storefront/结算服务共用。
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal

#: 1 斤 = 500 克。所有“元/斤”价格都以此换算。
JIN_GRAMS = 500

#: 线上称重单品允许的最大预估/实称重量（克）。超过视为录入错误。
MAX_WEIGHT_GRAMS = 15_000
MIN_WEIGHT_GRAMS = 1

#: 规格档位默认值（用户可在后台覆盖）。
DEFAULT_WEIGHT_OPTIONS = [
    {"label": "约0.5斤", "grams": 250},
    {"label": "约1斤", "grams": 500},
    {"label": "约2斤", "grams": 1000},
]

PRICING_MODES = ("piece", "weight")

WEIGH_PRICE_DDL = """
-- ── 线上称重定价：商品/规格计价方式 + 订单行预估/实称重量 + 结算记录 ──
ALTER TABLE product_variant
    ADD COLUMN IF NOT EXISTS pricing_mode TEXT NOT NULL DEFAULT 'piece';
ALTER TABLE product_variant
    ADD COLUMN IF NOT EXISTS unit_price_cents INTEGER;
ALTER TABLE product_variant
    ADD COLUMN IF NOT EXISTS price_basis_grams INTEGER NOT NULL DEFAULT 500;
ALTER TABLE product_variant
    ADD COLUMN IF NOT EXISTS weight_options JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE product_variant
    ADD COLUMN IF NOT EXISTS stock_unit TEXT NOT NULL DEFAULT 'piece';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'product_variant_pricing_mode_check') THEN
        ALTER TABLE product_variant ADD CONSTRAINT product_variant_pricing_mode_check
            CHECK (pricing_mode IN ('piece', 'weight'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'product_variant_stock_unit_check') THEN
        ALTER TABLE product_variant ADD CONSTRAINT product_variant_stock_unit_check
            CHECK (stock_unit IN ('piece', 'gram'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'product_variant_price_basis_check') THEN
        ALTER TABLE product_variant ADD CONSTRAINT product_variant_price_basis_check
            CHECK (price_basis_grams > 0);
    END IF;
END $$;

ALTER TABLE cart_line_item
    ADD COLUMN IF NOT EXISTS pricing_mode TEXT NOT NULL DEFAULT 'piece';
ALTER TABLE cart_line_item
    ADD COLUMN IF NOT EXISTS unit_price_cents_per_jin INTEGER;
ALTER TABLE cart_line_item
    ADD COLUMN IF NOT EXISTS estimated_weight_grams INTEGER;
ALTER TABLE cart_line_item
    ADD COLUMN IF NOT EXISTS weight_option_label TEXT;
ALTER TABLE cart_line_item
    ADD COLUMN IF NOT EXISTS weight_step_grams INTEGER;
ALTER TABLE cart_line_item
    ADD COLUMN IF NOT EXISTS price_basis_grams INTEGER NOT NULL DEFAULT 500;

-- 同一批次不同重量档要能并存：唯一键把重量档纳入（按件行仍为 0）。
DROP INDEX IF EXISTS uq_line_item_cart_batch;
CREATE UNIQUE INDEX IF NOT EXISTS uq_line_item_cart_batch_weight
    ON cart_line_item(cart_id, batch_id, COALESCE(weight_step_grams, 0))
    WHERE deleted_at IS NULL;

ALTER TABLE order_line_item
    ADD COLUMN IF NOT EXISTS pricing_mode TEXT NOT NULL DEFAULT 'piece';
ALTER TABLE order_line_item
    ADD COLUMN IF NOT EXISTS unit_price_cents_per_jin INTEGER;
ALTER TABLE order_line_item
    ADD COLUMN IF NOT EXISTS price_basis_grams INTEGER NOT NULL DEFAULT 500;
ALTER TABLE order_line_item
    ADD COLUMN IF NOT EXISTS estimated_weight_grams INTEGER;
ALTER TABLE order_line_item
    ADD COLUMN IF NOT EXISTS estimated_amount_cents INTEGER;
ALTER TABLE order_line_item
    ADD COLUMN IF NOT EXISTS actual_weight_grams INTEGER;
ALTER TABLE order_line_item
    ADD COLUMN IF NOT EXISTS actual_amount_cents INTEGER;
ALTER TABLE order_line_item
    ADD COLUMN IF NOT EXISTS weight_diff_cents INTEGER;
ALTER TABLE order_line_item
    ADD COLUMN IF NOT EXISTS settlement_status TEXT NOT NULL DEFAULT 'not_required';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'order_line_item_settlement_status_check') THEN
        ALTER TABLE order_line_item ADD CONSTRAINT order_line_item_settlement_status_check
            CHECK (settlement_status IN (
                'not_required', 'pending', 'auto_settled', 'needs_confirmation',
                'confirmed', 'refund_pending', 'refunded', 'charge_pending', 'disputed'
            ));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS weigh_settlement (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL,
    order_line_item_id UUID NOT NULL UNIQUE REFERENCES order_line_item(id),
    batch_id UUID,
    weight_grams INTEGER NOT NULL CHECK (weight_grams > 0),
    unit_price_cents_per_jin INTEGER NOT NULL CHECK (unit_price_cents_per_jin >= 0),
    price_basis_grams INTEGER NOT NULL DEFAULT 500 CHECK (price_basis_grams > 0),
    estimated_weight_grams INTEGER,
    estimated_amount_cents INTEGER,
    actual_amount_cents INTEGER NOT NULL CHECK (actual_amount_cents >= 0),
    diff_cents INTEGER NOT NULL,
    tolerance_percent INTEGER NOT NULL DEFAULT 10,
    within_tolerance BOOLEAN NOT NULL,
    status TEXT NOT NULL DEFAULT 'recorded'
        CHECK (status IN (
            'recorded', 'auto_refund', 'manual_refund_pending', 'charge_pending',
            'needs_customer_confirmation', 'customer_accepted', 'customer_rejected',
            'refunded', 'void'
        )),
    refund_id UUID,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_weigh_settlement_order
    ON weigh_settlement(order_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_weigh_settlement_status
    ON weigh_settlement(status, created_at DESC);

-- 每日进销存（进货/销售/剩余/报损/进价/售价/毛利），按克记重量。
CREATE TABLE IF NOT EXISTS store_daily_inventory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    biz_date DATE NOT NULL,
    variant_id UUID REFERENCES product_variant(id),
    product_name TEXT NOT NULL,
    purchase_qty_grams INTEGER NOT NULL DEFAULT 0 CHECK (purchase_qty_grams >= 0),
    sales_qty_grams INTEGER NOT NULL DEFAULT 0 CHECK (sales_qty_grams >= 0),
    waste_qty_grams INTEGER NOT NULL DEFAULT 0 CHECK (waste_qty_grams >= 0),
    remaining_qty_grams INTEGER NOT NULL DEFAULT 0 CHECK (remaining_qty_grams >= 0),
    purchase_price_cents INTEGER,
    sale_price_cents INTEGER,
    gross_margin_cents INTEGER,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (biz_date, variant_id)
);
CREATE INDEX IF NOT EXISTS idx_store_daily_inventory_date
    ON store_daily_inventory(biz_date DESC, product_name);
"""


def normalize_price_unit(unit: str | None) -> str:
    """把供应商报价单位归一成 ``jin`` / ``kg`` / ``piece``。"""
    text = (unit or "").strip().lower()
    if text in ("jin", "斤", "500g"):
        return "jin"
    if text in ("kg", "公斤", "千克", "1000g"):
        return "kg"
    if text in ("piece", "件", "个", "盒", "包", "瓶", "袋"):
        return "piece"
    raise ValueError(f"unsupported price unit: {unit!r}")


def unit_price_to_per_jin_cents(unit_price_cents: int, unit: str | None) -> int:
    """把“元/kg”或“元/斤”报价换算成“分/斤”。按件报价不适用，直接拒绝。"""
    if not isinstance(unit_price_cents, int) or isinstance(unit_price_cents, bool):
        raise ValueError("unit_price_cents must be an integer")
    if unit_price_cents < 0:
        raise ValueError("unit_price_cents must be non-negative")
    normalized = normalize_price_unit(unit)
    if normalized == "jin":
        return unit_price_cents
    if normalized == "kg":
        # 1 kg = 2 斤；分/斤 = 分/kg ÷ 2，四舍五入到分。
        return int(
            (Decimal(unit_price_cents) / Decimal(2)).to_integral_value(rounding=ROUND_HALF_UP)
        )
    raise ValueError("piece-priced item has no per-jin price")


def weigh_amount_cents(
    weight_grams: int,
    unit_price_cents_per_jin: int,
    *,
    price_basis_grams: int = JIN_GRAMS,
) -> int:
    """称重金额（分）：重量 × 单价 ÷ 基准，Decimal 四舍五入到分。

    基准默认 500 克（斤）；若后台按其它基准报价，显式传入。非法输入 raise ValueError。
    """
    if not isinstance(weight_grams, int) or isinstance(weight_grams, bool):
        raise ValueError("weight_grams must be an integer")
    if not isinstance(unit_price_cents_per_jin, int) or isinstance(unit_price_cents_per_jin, bool):
        raise ValueError("unit_price_cents_per_jin must be an integer")
    if not isinstance(price_basis_grams, int) or isinstance(price_basis_grams, bool):
        raise ValueError("price_basis_grams must be an integer")
    if weight_grams <= 0:
        raise ValueError("weight_grams must be positive")
    if unit_price_cents_per_jin < 0:
        raise ValueError("unit_price_cents_per_jin must be non-negative")
    if price_basis_grams <= 0:
        raise ValueError("price_basis_grams must be positive")
    amount = (Decimal(weight_grams) * Decimal(unit_price_cents_per_jin)) / Decimal(
        price_basis_grams
    )
    return int(amount.to_integral_value(rounding=ROUND_HALF_UP))


def validate_weight_grams(weight_grams: object, *, field: str = "weight_grams") -> int:
    """校验线上称重重量：1..15000 克的整数。返回 int，否则 raise ValueError。"""
    if not isinstance(weight_grams, int) or isinstance(weight_grams, bool):
        raise ValueError(f"{field} must be an integer")
    if weight_grams < MIN_WEIGHT_GRAMS or weight_grams > MAX_WEIGHT_GRAMS:
        raise ValueError(f"{field} must be between {MIN_WEIGHT_GRAMS} and {MAX_WEIGHT_GRAMS}")
    return weight_grams


def parse_weight_options(raw: object) -> list[dict[str, object]]:
    """把 JSONB/dict/list/JSON 串解析成规范档位列表；非法档位跳过。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        grams = item.get("grams")
        label = item.get("label")
        if not isinstance(grams, int) or isinstance(grams, bool):
            continue
        if grams < MIN_WEIGHT_GRAMS or grams > MAX_WEIGHT_GRAMS:
            continue
        out.append({"label": str(label or f"约{grams / JIN_GRAMS:g}斤"), "grams": grams})
    return out


def resolve_weight_options(raw: object) -> list[dict[str, object]]:
    """商品未配置档位时回落到默认三档，保证前台总有可选预估重量。"""
    options = parse_weight_options(raw)
    return options or [dict(item) for item in DEFAULT_WEIGHT_OPTIONS]


def diff_policy(
    estimated_amount_cents: int,
    actual_amount_cents: int,
    *,
    tolerance_percent: int = 10,
) -> dict[str, object]:
    """多退少补策略。

    返回：diff_cents（实际-预估，正=需补收，负=需退款）、within_tolerance、
    tolerance_cents、action（refund/charge/none/confirm_refund/confirm_charge）。
    容差按预估金额百分比计算，向下取整到分；超出容差必须顾客确认。
    """
    if not isinstance(estimated_amount_cents, int) or isinstance(estimated_amount_cents, bool):
        raise ValueError("estimated_amount_cents must be an integer")
    if not isinstance(actual_amount_cents, int) or isinstance(actual_amount_cents, bool):
        raise ValueError("actual_amount_cents must be an integer")
    if estimated_amount_cents < 0 or actual_amount_cents < 0:
        raise ValueError("amounts must be non-negative")
    if not isinstance(tolerance_percent, int) or isinstance(tolerance_percent, bool):
        raise ValueError("tolerance_percent must be an integer")
    if tolerance_percent < 0 or tolerance_percent > 100:
        raise ValueError("tolerance_percent must be 0..100")

    diff = actual_amount_cents - estimated_amount_cents
    tolerance_cents = estimated_amount_cents * tolerance_percent // 100
    within = abs(diff) <= tolerance_cents
    if diff == 0:
        action = "none"
    elif within:
        action = "refund" if diff < 0 else "charge"
    else:
        action = "confirm_refund" if diff < 0 else "confirm_charge"
    return {
        "diff_cents": diff,
        "within_tolerance": within,
        "tolerance_cents": tolerance_cents,
        "tolerance_percent": tolerance_percent,
        "action": action,
    }


def settlement_status_for(action: str) -> str:
    """把 diff_policy 的 action 映射成订单行的 settlement_status。"""
    return {
        "none": "auto_settled",
        "refund": "auto_settled",
        "charge": "charge_pending",
        "confirm_refund": "needs_confirmation",
        "confirm_charge": "needs_confirmation",
    }.get(action, "pending")
