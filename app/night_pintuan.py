"""缺口 6 二期：夜市清货标记 DDL + 拼单（意向成团登记）。

规则依据：~/workspace/hemal-cangzhu-rules.md（Soffy 2026-11-02 全部通过）。
增长方案原文未在 ~/workspace 找到，以规则文件为准，未发明语义。

夜市标记（时段价纯函数在 app/cangzhu.py，本模块只含 DDL）：
- product.night_clearance：true 表示当日临期清货品，可享夜市阶梯价；
  false（默认）表示普通商品。标记本身不含价格，价格只由时段决定。
- 标记即清货品：置标时须同步 voucher_excluded=true（规则 2/3：夜市品
  不计推荐奖励合资格基数、不可用券）；该不变式由管理端点保证，见
  app/storefront.py。voucher_ledger 账本本体与券核销逻辑本模块不碰。

拼单（最小诚实版；与众筹意向 crowd_intent、缺货预订 preorder 均无关）：
- crowd_intent 是“预付意向集单、500 单触发采购通知”，无成团、无拼单价、
  无到期语义；order_line_item.preorder_qty 是缺货预订量。两者均未复用，
  本模块新建 pintuan_group / pintuan_participant 两张表。
- 拼单不收钱：发起与加入只登记成团意向，不创建订单、不扣款、不碰库存
  与券账本。成团后由商家按拼单价手动履约，本线不自动建单、不接结账。
- 成团条件：参与人头数达到 target_size（按人头，不按件数）；达到即
  formed。人数上限即 target_size，满团后拒绝加入；一人一团内只能参加一次。
- 到期：expires_at 之后未成团即 expired（读/加入时懒过期 + 商家扫过期
  端点批量过期）。过期不产生任何结算：未收款，故无退款、无冲回。
- 拼单价与 9 折/券：不叠加。拼单价是成团意向价，参与本身不产生折扣；
  商家手动履约走正常下单时，该单仍按原互斥规则取最优一种（规则 1）。
  拼单价只校验为正，不与标价比价（无授权口径，不编造）。

本模块只含拼单逻辑与幂等 DDL，不 import 任何 app 内模块，避免循环导入。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from asyncpg.exceptions import UniqueViolationError

# 拼单默认值（授权默认值，见模块 docstring；改动须经 Soffy 拍板）：
PINTUAN_DEFAULT_DURATION_MINUTES = 1440  # 未指定时长时，成团窗口 24 小时
PINTUAN_MIN_TARGET_SIZE = 2  # 成团最少 2 人；1 人不成团
PINTUAN_MAX_TARGET_SIZE = 1000

NIGHT_PINTUAN_DDL = """
-- 缺口 6（夜市+拼单）：当日临期清货标记（默认 false，普通商品）。
ALTER TABLE product ADD COLUMN IF NOT EXISTS night_clearance BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS pintuan_group (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    variant_id UUID NOT NULL,
    creator_id UUID NOT NULL,
    pintuan_price_cents INT NOT NULL CHECK (pintuan_price_cents > 0),
    target_size INT NOT NULL CHECK (target_size >= 2),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'formed', 'expired')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pintuan_group_variant_status
    ON pintuan_group(variant_id, status);
CREATE INDEX IF NOT EXISTS idx_pintuan_group_open_expiry
    ON pintuan_group(expires_at) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS pintuan_participant (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id UUID NOT NULL REFERENCES pintuan_group(id),
    customer_id UUID NOT NULL,
    quantity INT NOT NULL DEFAULT 1 CHECK (quantity > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (group_id, customer_id)
);
CREATE INDEX IF NOT EXISTS idx_pintuan_participant_group
    ON pintuan_participant(group_id);
"""


async def ensure_night_pintuan_schema(pool: Any) -> None:
    """幂等创建缺口 6 所需列与表。"""

    async with pool.acquire() as conn:
        await conn.execute(NIGHT_PINTUAN_DDL)


def _group_dict(row: Any, participant_count: int) -> dict[str, Any]:
    return {
        "group_id": str(row["id"]),
        "variant_id": str(row["variant_id"]),
        "creator_id": str(row["creator_id"]),
        "pintuan_price_cents": int(row["pintuan_price_cents"]),
        "target_size": int(row["target_size"]),
        "status": row["status"],
        "participant_count": participant_count,
        "expires_at": row["expires_at"].isoformat(),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


async def _participant_count(conn: Any, group_id: str) -> int:
    count = await conn.fetchval(
        "SELECT COUNT(*) FROM pintuan_participant WHERE group_id = $1", group_id
    )
    return int(count or 0)


async def _expire_open_group_if_past(conn: Any, row: Any) -> Any:
    """行锁内的懒过期：open 且已过期的团当场转为 expired，返回最新行。"""

    if row["status"] == "open" and row["expires_at"] <= datetime.now(UTC):
        row = await conn.fetchrow(
            "UPDATE pintuan_group SET status = 'expired', updated_at = NOW() "
            "WHERE id = $1 RETURNING *",
            row["id"],
        )
    return row


async def create_pintuan_group(
    pool: Any,
    *,
    creator_id: str,
    variant_id: str,
    pintuan_price_cents: int,
    target_size: int,
    duration_minutes: int = PINTUAN_DEFAULT_DURATION_MINUTES,
    quantity: int = 1,
) -> dict[str, Any]:
    """发起拼单：校验后建团，发起人自动计为第 1 名参与者。"""

    if pintuan_price_cents <= 0:
        raise ValueError("pintuan_price_cents must be positive")
    if target_size < PINTUAN_MIN_TARGET_SIZE or target_size > PINTUAN_MAX_TARGET_SIZE:
        raise ValueError(
            f"target_size must be between {PINTUAN_MIN_TARGET_SIZE} and {PINTUAN_MAX_TARGET_SIZE}"
        )
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be positive")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    expires_at = datetime.now(UTC) + timedelta(minutes=duration_minutes)
    async with pool.acquire() as conn:
        async with conn.transaction():
            variant = await conn.fetchrow(
                "SELECT id FROM product_variant WHERE id = $1 AND deleted_at IS NULL",
                variant_id,
            )
            if variant is None:
                raise ValueError(f"variant not found: {variant_id}")
            row = await conn.fetchrow(
                "INSERT INTO pintuan_group "
                "(variant_id, creator_id, pintuan_price_cents, target_size, expires_at) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING *",
                variant_id,
                creator_id,
                pintuan_price_cents,
                target_size,
                expires_at,
            )
            await conn.execute(
                "INSERT INTO pintuan_participant (group_id, customer_id, quantity) "
                "VALUES ($1, $2, $3)",
                row["id"],
                creator_id,
                quantity,
            )
            return _group_dict(row, 1)


async def join_pintuan_group(
    pool: Any, *, group_id: str, customer_id: str, quantity: int = 1
) -> dict[str, Any]:
    """加入拼单：人头数达到 target_size 的同一事务内转为 formed。

    满团、已成团、已过期、重复参加均拒绝；过期团先懒过期再拒绝。
    """

    if quantity <= 0:
        raise ValueError("quantity must be positive")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM pintuan_group WHERE id = $1 FOR UPDATE", group_id
            )
            if row is None:
                raise ValueError(f"pintuan group not found: {group_id}")
            row = await _expire_open_group_if_past(conn, row)
            if row["status"] != "open":
                raise ValueError(f"pintuan group is {row['status']}, cannot join")
            try:
                await conn.execute(
                    "INSERT INTO pintuan_participant (group_id, customer_id, quantity) "
                    "VALUES ($1, $2, $3)",
                    group_id,
                    customer_id,
                    quantity,
                )
            except UniqueViolationError as exc:
                raise ValueError("already joined this pintuan group") from exc
            count = await _participant_count(conn, group_id)
            if count > int(row["target_size"]):
                # 唯一约束 + 行锁保证正常路径到不了这里；防御性回滚语义。
                raise ValueError("pintuan group is full")
            if count >= int(row["target_size"]):
                row = await conn.fetchrow(
                    "UPDATE pintuan_group SET status = 'formed', updated_at = NOW() "
                    "WHERE id = $1 RETURNING *",
                    group_id,
                )
            return _group_dict(row, count)


async def get_pintuan_group(pool: Any, group_id: str) -> dict[str, Any] | None:
    """团详情：读时懒过期，附参与人头数与参与清单。"""

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM pintuan_group WHERE id = $1 FOR UPDATE", group_id
            )
            if row is None:
                return None
            row = await _expire_open_group_if_past(conn, row)
            count = await _participant_count(conn, group_id)
            participants = await conn.fetch(
                "SELECT customer_id, quantity, created_at FROM pintuan_participant "
                "WHERE group_id = $1 ORDER BY created_at ASC",
                group_id,
            )
            result = _group_dict(row, count)
            result["participants"] = [
                {
                    "customer_id": str(p["customer_id"]),
                    "quantity": int(p["quantity"]),
                    "created_at": p["created_at"].isoformat(),
                }
                for p in participants
            ]
            return result


async def list_pintuan_groups(
    pool: Any, *, status: str | None, limit: int, offset: int
) -> list[dict[str, Any]]:
    """团列表：先批量过期已到期未成团的团，再按创建时间倒序返回。"""

    if status is not None and status not in ("open", "formed", "expired"):
        raise ValueError(f"invalid pintuan status: {status}")
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE pintuan_group SET status = 'expired', updated_at = NOW() "
                "WHERE status = 'open' AND expires_at <= NOW()"
            )
            conditions: list[str] = []
            params: list[Any] = []
            if status is not None:
                conditions.append(f"status = ${len(params) + 1}")
                params.append(status)
            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            rows = await conn.fetch(
                f"SELECT * FROM pintuan_group {where} "
                f"ORDER BY created_at DESC LIMIT ${len(params) + 1} "
                f"OFFSET ${len(params) + 2}",
                *params,
                limit,
                offset,
            )
            result = []
            for row in rows:
                count = await _participant_count(conn, str(row["id"]))
                result.append(_group_dict(row, count))
            return result


async def sweep_expired_pintuan_groups(pool: Any) -> int:
    """批量过期：所有 open 且已到期的团转为 expired，返回过期团数。"""

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "UPDATE pintuan_group SET status = 'expired', updated_at = NOW() "
            "WHERE status = 'open' AND expires_at <= NOW() RETURNING id"
        )
        return len(rows)
