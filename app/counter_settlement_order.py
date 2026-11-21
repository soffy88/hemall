"""app.counter_settlement_order — 柜台现金结算建单：纯函数 + 幂等 DDL。

不 import 任何 app 内模块（与 app/weigh.py、app/counter_closeout.py 同一纪律），
供 ensure_p0_schema 与 storefront 柜台结算端点共用。

背景：app/counter_closeout.py 的模块注释记过一条建单链路缺口——customer_order
只能从购物车快照创建、库存扣减只扣已有预留，柜台称重行项两者皆无。本模块
闭合该缺口：结算事务内直接建单并直接扣库存，不经过购物车、不建预留等待。

建单语义（授权默认值，见交付报告，供 Soffy 否决）：
- 渠道记 counter（customer_order.channel），散客无顾客关联：customer_id 列
  在共享 DDL 里可空（UUID，无 NOT NULL），单据 customer_id 为空时订单
  customer_id 即为空，绝不造假顾客；单据若带有 customer_id 则原样带入。
- 不碰券/9 折：discount_cents=0、discount_kind='none'、voucher_redeemed_cents=0；
  券与 9 折只走线上建单链路，互斥规则一字不动；不写 voucher_ledger。
- 货物离柜即扣库存，不建预留等待：同一事务内先按预留账本协议写
  inventory_reservation（随即标记 consumed），再写 shipment stock_movement，
  批次 stock_qty 直接扣减；后续发货/取消路径看到已 consumed 的预留即拒绝
  二次处理（见 app/orders/service.py 的 _ship/_release 守卫），不会重扣。
- 不伪造支付单据：不建 payment_intent，payment_provider_name /
  payment_intent_id 留空；现金结算记录（counter_settlement）即付款凭证，
  订单经 counter_ticket_id 链回单据。订单状态直接落 'completed'：现金已收、
  货物已离柜，交易当场完成，不经过 pending/确认/发货状态机。

库存折算语义（称重行按重量/计价基准折算库存单位）：
- 库存以“份”为单位计数：stock_qty 记的是变体规格份数，一份 = 计价基准
  克数（如规格 "500g/份" → 基准 500 克，一份即 500 克）；代码里没有任何
  以克为单位记 stock_qty 的口径，绝不能把克数直接当 quantity 扣。
- 称重行销量 = ceil(weight_grams / price_basis_grams) 份，向上取整：不足
  一份按一份扣。理由：quantity / reservation quantity / stock_qty 全是
  INTEGER，分数份无法落账；向下取整会在小重量时扣 0（违反 quantity > 0），
  四舍五入同样会在小于半份时扣 0；只有向上取整保证“卖了必扣、永不超卖”。
  代价是如实承认的近似：重量不是基准整数倍时，多扣的部分是随份售出的
  余量（开包零卖的损耗口径），订单行上以 weight_grams 原样留痕，审计时
  quantity × unit_price 与 line_total 的差额可解释、不可抹平。
- 行单价取称重计价时的 unit_price_cents（每基准单价），行合计取称重计价
  时的 amount_cents（重量 × 单价 / 基准，四舍五入到分），订单合计 = 各行
  合计之和，必须等于结算重算的单据合计，否则整笔结算回滚。

幂等：
- 一笔结算最多建一单：主守卫是单据状态（open/pending_settlement 才能结算，
  结算事务内对单据行 FOR UPDATE 串行化并发重结）；兜底是
  customer_order.counter_ticket_id 的 UNIQUE 约束与 counter_settlement
  的 UNIQUE(ticket_id)，撞上唯一约束时调用方须转 409，绝不静默吞掉。
- 无计价行项的单据（合计 0、没有 processed 行）不建单：空订单是假订单，
  结算只落结算记录；unpriced 行（只记重量、未挂商品）不参与建单，沿用
  既有口径留在未结算态。
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

#: 柜台订单渠道值（customer_order.channel）。
ORDER_CHANNEL_COUNTER = "counter"

#: 柜台订单落单即完成态（现金已收、货物已离柜）。
COUNTER_ORDER_STATUS = "completed"

COUNTER_SETTLEMENT_ORDER_DDL = """
-- counter-settlement-order: 柜台结算建单——订单渠道 + 单据↔订单唯一链 + 行项实称重量。
-- 幂等：列已存在时跳过；counter_ticket_id 的 UNIQUE 是一单一单的数据库兜底。
ALTER TABLE customer_order ADD COLUMN IF NOT EXISTS channel TEXT;
ALTER TABLE customer_order
    ADD COLUMN IF NOT EXISTS counter_ticket_id UUID UNIQUE REFERENCES counter_ticket(id);
ALTER TABLE order_line_item ADD COLUMN IF NOT EXISTS weight_grams INT;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'order_line_item_weight_grams_check'
    ) THEN
        ALTER TABLE order_line_item
            ADD CONSTRAINT order_line_item_weight_grams_check
            CHECK (weight_grams IS NULL OR weight_grams > 0);
    END IF;
END $$;
"""


def weigh_stock_units(weight_grams: int, price_basis_grams: int) -> int:
    """称重销量折算成库存份数：ceil(重量 / 计价基准)，至少 1 份。

    一份 = price_basis_grams 克（规格文本里的 g/kg 基准）。向上取整是唯一
    不扣 0、不超卖的整数折算（见模块注释）；非法输入 raise ValueError。
    """
    if not isinstance(weight_grams, int) or isinstance(weight_grams, bool):
        raise ValueError("weigh_stock_units: weight_grams must be an integer")
    if not isinstance(price_basis_grams, int) or isinstance(price_basis_grams, bool):
        raise ValueError("weigh_stock_units: price_basis_grams must be an integer")
    if weight_grams <= 0:
        raise ValueError("weigh_stock_units: weight_grams must be positive")
    if price_basis_grams <= 0:
        raise ValueError("weigh_stock_units: price_basis_grams must be positive")
    units = (Decimal(weight_grams) / Decimal(price_basis_grams)).to_integral_value(
        rounding=ROUND_CEILING
    )
    return int(units)


def require_weigh_line_pricing(
    *,
    batch_id: object,
    weight_grams: object,
    unit_price_cents: object,
    price_basis_grams: object,
    amount_cents: object,
) -> None:
    """建单前校验一条已计价称重行：缺批次/缺重量/缺单价/缺基准/缺金额时
    raise ValueError（调用方转 409，整笔结算回滚，绝不拿残缺行估算建单）。
    """
    if batch_id is None:
        raise ValueError("weigh line has no batch: cannot build an order line from it")
    if not isinstance(weight_grams, int) or isinstance(weight_grams, bool) or weight_grams <= 0:
        raise ValueError("weigh line has invalid weight_grams")
    if (
        not isinstance(unit_price_cents, int)
        or isinstance(unit_price_cents, bool)
        or unit_price_cents < 0
    ):
        raise ValueError("weigh line has invalid unit_price_cents")
    if (
        not isinstance(price_basis_grams, int)
        or isinstance(price_basis_grams, bool)
        or price_basis_grams <= 0
    ):
        raise ValueError("weigh line has invalid price_basis_grams")
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents < 0:
        raise ValueError("weigh line has invalid amount_cents")
