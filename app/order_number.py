"""app.order_number — 柜台订单号：YYYYMMDD-NNNN（北京时间日期 + 当天序号）。

不 import 任何 app 内模块（与 app/counter_settlement_order.py 同一纪律），
供 ensure_p0_schema 与 storefront 柜台结算建单共用。

编号语义：
- 格式 ``YYYYMMDD-NNNN``，日期取北京时间（Asia/Shanghai）当天，序号从
  0001 起、每天重置（例：20261128-0001）。
- 只给柜台结算建单（_create_counter_order）编号；其它渠道的单 order_no
  留空（NULL），旧单不回填。
- 并发安全：序号存在 order_number_sequence（order_date 主键）里，同一条
  INSERT ... ON CONFLICT DO UPDATE 语句内行锁递增——单条语句原子执行，
  两笔结算同时进来也拿不到同一个号；customer_order.order_no 另有部分
  唯一索引作兜底（NULL 行不占位），撞号时调用方须转 409，绝不静默吞掉。

幂等：DDL 全是 IF NOT EXISTS，重复执行 ensure_p0_schema 不报错。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

#: 订单号的北京时间时区。
BEIJING_TZ = ZoneInfo("Asia/Shanghai")

#: 订单号格式：8 位日期 + 横线 + 4 位序号。
ORDER_NO_RE = re.compile(r"^\d{8}-\d{4}$")

ORDER_NUMBER_DDL = """
-- order-number: 柜台订单号——每天序号表 + 订单号列 + 部分唯一索引。
-- 幂等：表/列/索引已存在时跳过；NULL 订单号（非柜台单、旧单）不占唯一位。
CREATE TABLE IF NOT EXISTS order_number_sequence (
    order_date DATE PRIMARY KEY,
    last_seq INTEGER NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE customer_order ADD COLUMN IF NOT EXISTS order_no TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_order_order_no
    ON customer_order(order_no)
    WHERE order_no IS NOT NULL;
"""


def format_order_no(day: date, seq: int) -> str:
    """拼订单号：YYYYMMDD-NNNN。序号须 >= 1，否则 raise ValueError。"""
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise ValueError("format_order_no: seq must be a positive integer")
    return f"{day.strftime('%Y%m%d')}-{seq:04d}"


def is_valid_order_no(value: object) -> bool:
    """订单号格式校验：8 位合法日期 + 横线 + 4 位序号。"""
    if not isinstance(value, str) or not ORDER_NO_RE.match(value):
        return False
    try:
        datetime.strptime(value[:8], "%Y%m%d").date()
    except ValueError:
        return False
    return True


def beijing_today() -> date:
    """今天（北京时间）。"""
    return datetime.now(BEIJING_TZ).date()


async def next_order_no(conn, *, order_date: date | None = None) -> str:
    """在调用方事务内取下一个订单号（须已在事务内；与建单同一事务提交）。

    order_date 仅供测试注入（跨天重置）；生产调用一律不传，取北京时间今天。
    INSERT ... ON CONFLICT DO UPDATE 单语句原子递增：并发调用互不重号。
    """
    day = order_date if order_date is not None else beijing_today()
    seq = await conn.fetchval(
        "INSERT INTO order_number_sequence (order_date, last_seq) "
        "VALUES ($1, 1) "
        "ON CONFLICT (order_date) DO UPDATE SET "
        "last_seq = order_number_sequence.last_seq + 1, updated_at = NOW() "
        "RETURNING last_seq",
        day,
    )
    return format_order_no(day, int(seq))
