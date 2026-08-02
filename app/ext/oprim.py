"""app.ext.oprim — hemall 扩展层元实现：原子操作。

严格扁平化，不包裹业务逻辑；oprim 之间禁止裸调。

通用 CRUD (db_insert/db_upsert/db_query_one/db_query_many) 直接复用
obase.persistence 的现成实现，只是按 SPEC §2.1 的命名包一层薄壳——
不重新发明轮子。db_lock_batch_inventory / db_unlock_batch_inventory /
generate_id_v7 是本域新增的原语，SPEC 未给现成实现。
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from obase.persistence import insert_one, write_one
from obase.persistence.pool import PgPool

# ── 通用 CRUD (对齐 SPEC §2.1，委托 obase.persistence 既有实现) ──────────


async def db_insert(
    pool: PgPool, *, table: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """单行插入，返回插入后的完整行 (id + 原 payload)。"""
    row_id = await insert_one(pool, table=table, data=payload, returning="id")
    return {"id": row_id, **payload}


async def db_upsert(
    pool: PgPool, *, table: str, payload: dict[str, Any], conflict_keys: list[str]
) -> dict[str, Any]:
    """单行 upsert (ON CONFLICT DO UPDATE)。"""
    await write_one(pool, table=table, data=payload, conflict_on=conflict_keys)
    return payload


async def db_query_one(
    pool: PgPool, *, sql: str, params: tuple[Any, ...] = ()
) -> dict[str, Any] | None:
    """任意参数化 SQL，取首行。"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    return dict(row) if row else None


async def db_query_many(
    pool: PgPool, *, sql: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    """任意参数化 SQL，取全部行。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


# ── 领域专属：批次库存硬锁 (新增，SPEC 未提供现成实现) ─────────────────────


async def db_lock_batch_inventory(
    pool: PgPool, *, batch_id: str, lock_qty: int
) -> dict[str, Any]:
    """PostgreSQL 原生防超卖锁：单条原子 UPDATE ... RETURNING。

    不借助应用层事务包裹或 Redis 分布式锁——WHERE 子句里的库存充足性判断
    (stock_qty - locked_qty >= lock_qty) 和 SET locked_qty = locked_qty +
    lock_qty 在同一条语句内原子完成，命中即锁定成功，未命中 (库存不足/批次
    不存在/非 active) 直接返回 failed，交由调用方 (omodul) 决定是否重试或
    补偿。这就是 SPEC 要的"高并发下的无锁追加"：不用 app 层加锁，让数据库
    的行级原子性自己保证正确性。

    Returns:
        completed: {"status", "batch_id", "locked_qty", "available_qty",
                     "retail_price", "variant_id", "location_id"}
        failed: {"status", "error": {"type", "message"}}
    """
    if lock_qty <= 0:
        return {
            "status": "failed",
            "error": {"type": "ValueError", "message": "lock_qty must be positive"},
        }

    sql = (
        'UPDATE "inventory_batch" '
        "SET reserved_qty = reserved_qty + $1 "
        "WHERE id = $2 AND status = 'active' "
        "AND stock_qty - reserved_qty >= $1 "
        "RETURNING id, stock_qty, reserved_qty, retail_price_cents, variant_id, location_id"
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, lock_qty, batch_id)

    if row is None:
        return {
            "status": "failed",
            "error": {
                "type": "InsufficientStock",
                "message": f"batch {batch_id!r} not found, inactive, or insufficient stock for qty={lock_qty}",
            },
        }

    return {
        "status": "completed",
        "batch_id": str(row["id"]),
        "locked_qty": row["reserved_qty"],
        "available_qty": row["stock_qty"] - row["reserved_qty"],
        "retail_price": row["retail_price_cents"],
        "variant_id": str(row["variant_id"]),
        "location_id": str(row["location_id"]),
    }


async def db_unlock_batch_inventory(
    pool: PgPool, *, batch_id: str, unlock_qty: int
) -> dict[str, Any]:
    """db_lock_batch_inventory 的补偿操作：结账中途失败/取消时释放已硬锁库存。

    SPEC 没有单独列这个函数，但 db_lock_batch_inventory 一旦被 omodul 多次
    调用编排 (逐行锁库存)，中途失败必须能补偿回滚——否则会有库存永久卡在
    locked_qty 里出不来。这是 db_lock_batch_inventory 的自然对称操作，不是
    脱离规范新造实体。
    """
    sql = (
        'UPDATE "inventory_batch" '
        "SET reserved_qty = GREATEST(reserved_qty - $1, 0) "
        "WHERE id = $2 "
        "RETURNING id, stock_qty, reserved_qty"
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, unlock_qty, batch_id)

    if row is None:
        return {
            "status": "failed",
            "error": {"type": "NotFound", "message": f"batch {batch_id!r} not found"},
        }

    return {
        "status": "completed",
        "batch_id": str(row["id"]),
        "locked_qty": row["reserved_qty"],
    }


async def db_confirm_batch_pick(
    pool: PgPool, *, batch_id: str, qty: int
) -> dict[str, Any]:
    """把硬锁的预留转成永久出库：stock_qty 和 locked_qty 同时扣减 qty。

    db_lock_batch_inventory 只是"占住"库存 (下单时)；confirm_batch_pick 这个
    omodul 要用的是它的对称收尾操作——大妈真的把货从架子上拿走之后，把这份
    预留变成真正的出库，而不是一直挂在 locked_qty 里。单条原子 UPDATE，
    同样不借助应用层事务/分布式锁。
    """
    if qty <= 0:
        return {
            "status": "failed",
            "error": {"type": "ValueError", "message": "qty must be positive"},
        }

    sql = (
        'UPDATE "inventory_batch" '
        "SET stock_qty = stock_qty - $1, reserved_qty = reserved_qty - $1 "
        "WHERE id = $2 AND reserved_qty >= $1 "
        "RETURNING id, stock_qty, reserved_qty"
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, qty, batch_id)

    if row is None:
        return {
            "status": "failed",
            "error": {
                "type": "InvalidPickState",
                "message": f"batch {batch_id!r} not found or reserved_qty < qty={qty} (nothing to confirm)",
            },
        }

    return {
        "status": "completed",
        "batch_id": str(row["id"]),
        "stock_qty": row["stock_qty"],
        "locked_qty": row["reserved_qty"],
    }


# ── 外部环境与传感器交互 (对齐 SPEC §2.2，async) ───────────────────────────
# 只实现本 phase 实际用到的：打款 + 天气 + 通知 + 多模态视觉判损 + 顶棚视觉
# 入库追踪 + 小票 OCR + 智能分账托管 + 大模型文案生成 + 微信视频号发版 +
# 竞对价格爬虫 + 抖音开放平台网关 (v2.0/v4.0/v5.0/v6.0 全部用到；
# ext_pay_authorize/cv_recognize_tote_action 目前没有 omodul/oservi 用到，
# 不预先造轮子)。全部委托给 obase.provider_registry 已注册的 provider
# (payout/weather/notification/vlm/cv/llm/wechat_channel/spider/douyin
# category)，不重新发明网关对接逻辑。v5.0 SPEC 自己的
# cv_parse_competitor_receipt 明确写了"之前推演已实现封装，此处复用接口"——
# 就是 v2.0 已有的 cv_parse_retail_receipt，不重复定义一个功能完全相同的新
# 函数；v6.0 SPEC 的 ext_pay_transfer_to_creator 同理，就是已有的
# ext_pay_transfer (同一个 "payout" provider category，同一个 transfer 语义)，
# 不重复定义。


async def ext_pay_transfer(
    provider: str, *, account: str, amount: int
) -> dict[str, Any]:
    """调用打款 provider (微信企业付款/银行对公) 给供应商/工人/宿主账户转账。"""
    from obase.provider_registry import ProviderRegistry

    p = ProviderRegistry.get().generic("payout", provider)
    return await p.transfer(account=account, amount=amount)


async def ext_weather_forecast(
    provider: str, *, lat: float, lon: float
) -> dict[str, Any]:
    """拉取指定坐标点未来 2 小时的降水概率与灾害级别，供 weather_arbitrage_engine 用。"""
    from obase.provider_registry import ProviderRegistry

    p = ProviderRegistry.get().generic("weather", provider)
    return await p.forecast(lat=lat, lon=lon)


async def vlm_assess_damage(
    provider: str, *, evidence_img: str, original_batch_video: str
) -> dict[str, Any]:
    """调用多模态大模型比对客诉证据图与原产地批次视频，判定损坏类型/严重度/造假概率。"""
    from obase.provider_registry import ProviderRegistry

    p = ProviderRegistry.get().generic("vlm", provider)
    return await p.assess_damage(
        evidence_img=evidence_img, original_batch_video=original_batch_video
    )


async def cv_parse_intake_stream(
    provider: str, *, video_stream: bytes
) -> dict[str, Any]:
    """接收微仓顶棚摄像头数据，执行 OCR 和物体追踪，定位批次货架位。"""
    from obase.provider_registry import ProviderRegistry

    p = ProviderRegistry.get().generic("cv", provider)
    return await p.parse_intake_stream(video_stream=video_stream)


async def cv_parse_retail_receipt(
    provider: str, *, image_bytes: bytes
) -> list[dict[str, Any]]:
    """OCR 解析竞争对手 (传统超市) 购物小票，用于比价狙击/众包价格基线核销
    (v5.0 SPEC 里的 cv_parse_competitor_receipt 就是这个函数，不重复定义)。
    """
    from obase.provider_registry import ProviderRegistry

    p = ProviderRegistry.get().generic("cv", provider)
    return await p.parse_retail_receipt(image_bytes=image_bytes)


async def spider_fetch_competitor_prices(
    provider: str, *, lat: float, lon: float, radius_km: int, keywords: list[str]
) -> list[dict[str, Any]]:
    """抓取坐标周围的商超 O2O 价格，用于构建全局竞对价格基线 (v5.0)。"""
    from obase.provider_registry import ProviderRegistry

    p = ProviderRegistry.get().generic("spider", provider)
    return await p.fetch_competitor_prices(
        lat=lat, lon=lon, radius_km=radius_km, keywords=keywords
    )


async def ext_douyin_sync_inventory(
    provider: str,
    *,
    sku_id: str,
    price: int,
    stock: int,
    commission_rate: float,
    access_token: str,
) -> dict[str, Any]:
    """向抖音精选联盟/本地生活开放平台强行推入或更新一个带高额佣金的临期 SKU (v6.0)。"""
    from obase.provider_registry import ProviderRegistry

    p = ProviderRegistry.get().generic("douyin", provider)
    return await p.sync_inventory(
        sku_id=sku_id,
        price=price,
        stock=stock,
        commission_rate=commission_rate,
        access_token=access_token,
    )


async def ext_trigger_smart_escrow(
    provider: str, *, account: str, amount: int, release_date: Any
) -> dict[str, Any]:
    """调用支付网关的高级分账功能，把资金锁定至 release_date (T+7)，随时准备斩仓。

    跟 ext_pay_transfer (立即转账) 走同一个 "payout" provider category，但是
    托管语义完全不同——委托给 provider 自己新增的 escrow() 方法而不是
    transfer()，两者在 ManualPayoutProvider 里是两套独立的内存状态机。
    """
    from obase.provider_registry import ProviderRegistry

    p = ProviderRegistry.get().generic("payout", provider)
    return await p.escrow(account=account, amount=amount, release_date=release_date)


async def ext_llm_generate_text(
    provider: str, *, system_prompt: str, user_prompt: str
) -> str:
    """调用外部大模型 (如 GPT-4o/Claude) 生成纯文本 (v4.0 用于播报文案)。"""
    from obase.provider_registry import ProviderRegistry

    p = ProviderRegistry.get().generic("llm", provider)
    return await p.generate_text(system_prompt=system_prompt, user_prompt=user_prompt)


async def ext_wechat_channel_publish(
    provider: str, *, video_url: str, copy_text: str, mp_path: str, access_token: str
) -> dict[str, Any]:
    """把视频源、文案与带参小程序路径打包推流到微信视频号 (v4.0)。"""
    from obase.provider_registry import ProviderRegistry

    p = ProviderRegistry.get().generic("wechat_channel", provider)
    return await p.channel_publish(
        video_url=video_url,
        copy_text=copy_text,
        mp_path=mp_path,
        access_token=access_token,
    )


async def ext_notify_send(
    provider: str, *, channel: str, template: str, data: dict[str, Any]
) -> bool:
    """发送通知 (取件码/降价提醒等)。channel="sms" 走 send_sms，其余走 send_email。

    SPEC 的 template/data 是"模板名 + 渲染变量"的组合；obase 现成的
    LogNotificationProvider 只认 subject/body 这类已渲染好的字段，这里用最
    朴素的方式把 template 当 subject/message、data 当 body 拼进去——真正的
    模板渲染是 notification provider 自己的职责，不在 oprim 这层重造。
    """
    from obase.provider_registry import ProviderRegistry

    p = ProviderRegistry.get().generic("notification", provider)
    if channel == "sms":
        result = await p.send_sms(
            to=str(data.get("to", "")), message=f"[{template}] {data}"
        )
    else:
        result = await p.send_email(
            to=str(data.get("to", "")), subject=template, body=str(data)
        )
    return result.get("status") == "sent"


# ── 纯计算 (对齐 SPEC §2.3，sync) ──────────────────────────────────────────


def generate_id_v7(prefix: str) -> str:
    """带前缀的时间序主键，如 "batch_xxx"。

    VARCHAR(32) 预算很紧：标准 UUIDv7 字符串 (含横线) 就有 36 字符，装不下
    前缀。这里手写一个去横线的紧凑时序 ID：48 位毫秒时间戳 + 随机位，取
    十六进制串的前若干位 (时间戳在最高位，截断保留头部不破坏字典序=时序序)，
    拼上前缀后总长度不超过 32。
    """
    ms = int(time.time() * 1000)
    rand_bits = secrets.randbits(40)
    raw = f"{ms:012x}{rand_bits:010x}"  # 12 hex (48-bit ts) + 10 hex (40-bit rand) = 22 chars

    budget = 32 - len(prefix) - 1  # -1 for the underscore separator
    if budget <= 0:
        raise ValueError(f"prefix {prefix!r} too long for VARCHAR(32) id budget")
    return f"{prefix}_{raw[:budget]}"


def math_haversine_distance(
    lat1: float, *, lon1: float, lat2: float, lon2: float
) -> float:
    """球面距离极速计算 (haversine 公式)，单位公里。

    Args:
        lat1, lon1: 第一个点的纬度/经度 (度)。
        lat2, lon2: 第二个点的纬度/经度 (度)。

    Returns:
        两点间的大圆距离 (公里)。
    """
    import math

    earth_radius_km = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return earth_radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def math_polygon_contains(polygon: dict[str, Any], point: tuple[float, float]) -> bool:
    """判断坐标点是否落在 GeoJSON 多边形内 (射线法/PNPoly)，纯数学算子。

    用于校验供应商 PWA 上传的视频 GPS 坐标是否严格落在其注册的原产地围栏内——
    防止供应商用别处的货冒充自己围栏内的产地拍摄。

    Args:
        polygon: GeoJSON Polygon 结构 ``{"type": "Polygon", "coordinates":
            [[[lon, lat], [lon, lat], ...]]}`` (GeoJSON 坐标序是 [经度, 纬度]，
            跟本域其余函数惯用的 (lat, lon) 参数序刻意不同——遵守 GeoJSON 标准，
            不是笔误)。只看第一个环 (外环)，不处理内环 (镂空) 场景。
        point: (lat, lon) 待判定坐标，跟 math_haversine_distance 等函数的
            参数序保持一致。

    Returns:
        True 表示点在多边形内部 (含边界上的判定采用射线法的标准近似)。

    Raises:
        ValueError: polygon 不是 Polygon 类型，或外环点数不足 3 个。
    """
    if polygon.get("type") != "Polygon":
        raise ValueError("math_polygon_contains: polygon must be a GeoJSON Polygon")
    rings = polygon.get("coordinates") or []
    if not rings or len(rings[0]) < 3:
        raise ValueError(
            "math_polygon_contains: outer ring must have at least 3 points"
        )

    lat, lon = point
    ring = rings[0]
    inside = False
    n = len(ring)
    x, y = lon, lat  # 统一成 (x=经度, y=纬度) 做射线法，跟 GeoJSON 坐标序一致
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside
