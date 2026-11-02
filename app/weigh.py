"""app.weigh — 柜台称重试点：纯函数 + 幂等 DDL，不 import 任何 app 内模块。

依据：IoT 盘点报告 §4.2（称重台端点：硬件 POST 重量 + 商品/批次 + tote，
行项按重量 × 单价计价；pick 与称重同记 hardware_event 一本账）。

计价铁律：
- 单价取批次现价（retail_price_cents，缺失时退变体 reference_price_cents），
  取不到价 → 记账失败并返回明确错误，绝不编价格。
- 计价基准取自变体规格文本（如 "500g/份" → 500 克）；基准无法解析 → 记账
  失败，绝不猜“按公斤”还是“按件”。
- 金额 = 重量(克) × 单价(分) / 基准(克)，Decimal 四舍五入到分。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

#: 秤最大称重 15KG（商祺一体秤规格）：单次称重超过即拒绝，防误触/漂移脏数据。
MAX_WEIGHT_GRAMS = 15000

#: 称重事件来源：秤直发 / 店员手工 / 模拟器注入（演示与验证专用）。
WEIGH_SOURCES = ("scale", "manual", "simulator")

WEIGH_DDL = """
-- ── 柜台称重试点：hardware_event 账本列（IoT 报告 §4.2，同记一本账）──
ALTER TABLE hardware_event ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE hardware_event ADD COLUMN IF NOT EXISTS settlement_status
  VARCHAR(20) NOT NULL DEFAULT 'unsettled';
ALTER TABLE hardware_event ADD COLUMN IF NOT EXISTS weight_grams INT;
ALTER TABLE hardware_event ADD COLUMN IF NOT EXISTS unit_price_cents INT;
ALTER TABLE hardware_event ADD COLUMN IF NOT EXISTS price_basis_grams INT;
ALTER TABLE hardware_event ADD COLUMN IF NOT EXISTS amount_cents INT;
ALTER TABLE hardware_event ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'scale';
ALTER TABLE hardware_event ADD COLUMN IF NOT EXISTS ticket_id UUID;
CREATE INDEX IF NOT EXISTS idx_hardware_event_ticket ON hardware_event(ticket_id, created_at);
CREATE INDEX IF NOT EXISTS idx_hardware_event_session ON hardware_event(session_id, created_at);

-- ── 柜台单据头：行项即 hardware_event(event_type='weigh' 且 ticket_id 指向本表) ──
CREATE TABLE IF NOT EXISTS counter_ticket (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status VARCHAR(20) NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'pending_settlement', 'settled', 'void')),
    tote_id TEXT,
    customer_id UUID,
    created_by TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_counter_ticket_status ON counter_ticket(status, created_at);
"""

_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g)", re.IGNORECASE)


def extract_unit_texts(option_values: object) -> list[str]:
    """从变体 option_values 里取出全部规格文本（dict / JSON 串 / 纯文本都容）。"""
    if option_values is None:
        return []
    if isinstance(option_values, dict):
        return [str(v) for v in option_values.values() if v is not None]
    if isinstance(option_values, str):
        text = option_values.strip()
        if not text:
            return []
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                return [text]
            return extract_unit_texts(parsed)
        return [text]
    if isinstance(option_values, (list, tuple)):
        out: list[str] = []
        for item in option_values:
            out.extend(extract_unit_texts(item))
        return out
    return [str(option_values)]


def parse_basis_grams(unit_texts: Iterable[str]) -> int | None:
    """从规格文本解析计价基准（克）。如 "500g/份" → 500，"1kg" → 1000。

    找不到 g/kg 数量时返回 None（调用方必须记账失败，绝不猜基准）。
    """
    for text in unit_texts:
        if not text:
            continue
        match = _UNIT_RE.search(str(text).lower())
        if not match:
            continue
        qty = Decimal(match.group(1))
        if match.group(2).lower() == "kg":
            qty *= 1000
        grams = int(qty.to_integral_value(rounding=ROUND_HALF_UP))
        if grams > 0:
            return grams
    return None


def weigh_amount_cents(weight_grams: int, unit_price_cents: int, basis_grams: int) -> int:
    """金额(分) = 重量(克) × 单价(分) / 基准(克)，四舍五入到分。"""
    if weight_grams <= 0:
        raise ValueError("weigh_amount_cents: weight_grams must be positive")
    if unit_price_cents < 0:
        raise ValueError("weigh_amount_cents: unit_price_cents must be non-negative")
    if basis_grams <= 0:
        raise ValueError("weigh_amount_cents: basis_grams must be positive")
    amount = (Decimal(weight_grams) * Decimal(unit_price_cents) / Decimal(basis_grams)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(amount)
