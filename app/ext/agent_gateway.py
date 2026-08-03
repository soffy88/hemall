"""agent_gateway — 智能体网关 (Hermes / Cindy / 任意 Agent 接管系统的标准入口)。

把整个 ClearNode 系统暴露成一个标准的工具协议后端：

  GET  /agent/tools    → 工具发现 (从 omodul registry 自动生成, 含参数 schema)
  POST /agent/execute  → 工具执行 (JSON-RPC 风格: {tool, args})
  POST /agent/command  → 自然语言命令 (规则意图匹配 → 工具执行)
  POST /agent/ingest   → 视频/图片传货 (multipart → 商品识别 → 上架草稿)

设计要点:
  - 一切业务能力都是 omodul 端点 (POST /<domain>/<name>)，Agent Gateway 只是
    把"工具发现 + 工具执行"标准化——任何 agent (Hermes/Cindy/Claude/自研)
    拿到 /agent/tools 清单就能接管系统，不需要改业务代码。
  - 危险操作 (上架/改价/结算) 通过 ADMIN_OPS JWT 鉴权，手机指挥台先登录。
  - 视频传货: 上传实景视频 → 存盘 → 提取文件元信息 → 生成商品草稿 →
    自动创建 product + variant + inventory_batch (真实库存/真实图片)。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile

from .registry import all_endpoint_specs as ext_all_endpoint_specs


# ── 工具发现 ──────────────────────────────────────────────────────────


def _input_schema(input_cls: type) -> dict[str, Any]:
    """把 omodul 的 pydantic Input 模型转成 JSON Schema (供 agent 读参数)。"""
    try:
        return input_cls.model_json_schema() if hasattr(input_cls, "model_json_schema") else {}
    except Exception:  # noqa: BLE001
        return {}


def discover_tools() -> list[dict[str, Any]]:
    """从 omodul registry 发现全部可执行工具 (含参数 schema)。

    这是给 Hermes/Cindy 等智能体的标准工具清单：
      [{tool, domain, description, auth, parameters}, ...]
    """
    from ..registry import all_endpoint_specs as std_all_endpoint_specs

    tools: list[dict[str, Any]] = []
    seen: set[str] = set()

    for spec in list(std_all_endpoint_specs()) + list(ext_all_endpoint_specs()):
        tool_name = f"{spec.domain}/{spec.name}"
        if tool_name in seen:
            continue
        seen.add(tool_name)
        tools.append(
            {
                "tool": tool_name,
                "domain": spec.domain,
                "name": spec.name,
                "path": spec.path,
                "require_auth": spec.require_auth,
                "parameters": _input_schema(spec.input_cls),
            }
        )
    return sorted(tools, key=lambda t: t["tool"])


def find_tool(tool: str) -> Any:
    """按 "domain/name" 查找 omodul spec，找不到抛 404。"""
    from ..registry import all_endpoint_specs as std_all_endpoint_specs

    for spec in list(std_all_endpoint_specs()) + list(ext_all_endpoint_specs()):
        if f"{spec.domain}/{spec.name}" == tool or spec.path == f"/{tool}":
            return spec
    raise HTTPException(404, f"tool not found: {tool}")


# ── 工具执行 ──────────────────────────────────────────────────────────


async def execute_tool(
    tool: str,
    args: dict[str, Any],
    pool: Any,
    out_root: Path,
    principal: dict[str, Any],
) -> dict[str, Any]:
    """执行一个工具 (omodul 端点)。

    直接调用 omodul 的 (fn, config_cls, input_cls)，与 HTTP 端点同一套装配
    (respond.omodul_endpoint 也是这么接的)，保证行为一致。
    """
    spec = find_tool(tool)

    # 需要鉴权的工具必须来自已登录管理员 (get_current_user 已保证非匿名)
    if spec.require_auth and not principal.get("user_id"):
        raise HTTPException(401, "admin token required for this tool")

    cfg = spec.config_cls()
    # 注入 redis_url (与 app/storefront.py _cfg 同一约定): 容器内 localhost 是
    # 自身而非 redis 服务, 必须用应用配置的正确地址覆盖 omodul 默认值。
    if hasattr(cfg, "redis_url"):
        import os

        cfg.redis_url = os.environ.get("HEMALL_REDIS_URL") or "redis://localhost:6379/0"
    try:
        inp = spec.input_cls(**args)
    except Exception as exc:  # noqa: BLE001 — pydantic 校验错误转成 400
        raise HTTPException(400, f"invalid args for {tool}: {exc}") from exc

    # 输出目录: <out_root>/<tool>/<匿名引用>
    user_ref = str(principal.get("user_id") or "anonymous")
    out_dir = out_root / tool.replace("/", "__") / user_ref[:8]
    out_dir.mkdir(parents=True, exist_ok=True)

    result = await spec.fn(cfg, inp, out_dir, pool=pool)
    return dict(result)


# ── 自然语言命令 (规则意图匹配, 可插拔 LLM) ──────────────────────────
#
# 手机指挥台 / Hermes/Cindy 的命令入口。规则引擎负责两件事:
#   1. 意图识别 (关键词 → 命令种类)
#   2. 实体提取 (商品名 / 金额 / 数量 / UUID)
#
# 然后由 execute_command 编排器按各 omodul 的真实 Input schema 组装参数
# (缺 ID 的按商品名/最新记录从库解析), 避免"路由到工具但参数对不上"的断链。
# LLM tool-calling 就绪后, 同一份 schema 可由 Hermes/Cindy 直接驱动。


#: 命令意图表: 关键词 → 命令种类
_COMMAND_KINDS: list[tuple[list[str], str]] = [
    (["上架", "新品", "传货", "进货"], "list_product"),
    (["补货", "加库存"], "restock"),
    (["降价", "调价", "改价", "折扣"], "adjust_price"),
    (["报废", "下架", "销毁"], "dispose"),
    (["结算", "结款"], "settle"),
    (["佣金", "分润", "提成"], "dividend"),
    (["工资", "劳务"], "labor"),
    (["意向", "集单", "预售"], "crowd_intent"),
    (["退款", "退货"], "refund"),
    (["轮播", "视频号", "广播", "推广"], "broadcast"),
    (["行情", "基准价", "比价", "竞品"], "benchmark"),
    (["补天", "赔付", "补偿"], "rma"),
]

#: 每种命令对应的落库工具 (供 Hermes/Cindy 直接 function-calling 时参考)
_KIND_TOOL: dict[str, str] = {
    "list_product": "supply-chain/create_inventory_batch",  # 编排器内部走 create_product → variant → batch
    "restock": "inventory/adjust_inventory_level",
    "adjust_price": "catalog/update_product_variant",  # 编排器内部直改批次零售价
    "dispose": "supply-chain/mark_batch_for_disposal",
    "settle": "supply-chain/batch_settlement",
    "dividend": "settlement/dispatch_host_dividend",
    "labor": "settlement/dispatch_labor_payment",
    "crowd_intent": "community/create_crowd_intent",
    "refund": "aftersales/refund_payment",
    "broadcast": "marketing/execute_channel_broadcast_workflow",
    "rma": "aftersales/process_credit_gated_rma_workflow",
}

#: 商品名里的干扰词 (动词/量词/语气词, 从标题里剔除)
_TITLE_NOISE = [
    "上架", "新品", "传货", "进货", "创建商品", "新建商品", "建档",
    "补货", "加库存", "降价", "调价", "改价", "折扣", "报废", "下架", "销毁",
    "结算", "结款", "佣金", "分润", "提成", "工资", "劳务", "意向", "集单",
    "预售", "退款", "退货", "轮播", "视频号", "广播", "推广", "行情", "基准价",
    "比价", "竞品", "补天", "赔付", "补偿", "这批", "那批", "刚刚", "的", "到",
    "至", "吧", "啊", "请", "麻烦", "帮", "我", "给", "把", ",", "，", ".",
    "。", "!", "！", "?", "？", " ", "　",
    "件", "个", "斤", "箱", "包", "份", "袋", "瓶", "盒", "公斤", "千克", "克", "元",
]

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _extract_money(text: str) -> int | None:
    """从文本提取金额 (元 → 分)。"19.9元"/"¥19.9"/"19块9"/"19块9毛"/"9.9"。

    优先带货币单位 (元/块/毛/RMB/¥) 的模式; 裸数字只在前后都是非字母数字时
    才接受 (避免把订单 UUID 里的数字误当金额)。
    """
    # 口语: 19块9 / 19块9毛 → 19.9 元
    m = re.search(r"(?:¥|￥)?\s*(\d+(?:\.\d{1,2})?)\s*块\s*(\d{1,2})?\s*(?:毛|角)?", text)
    if m:
        yuan = float(m.group(1))
        jiao = int(m.group(2) or 0)
        return int(round((yuan + jiao / 10) * 100))
    # 带单位: 19.9元 / ¥19.9 / 50 元 / 9.9RMB
    m = re.search(r"(?:¥|￥)?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|RMB|毛|角)", text)
    if m:
        return int(round(float(m.group(1)) * 100))
    # 裸数字兜底: 前后都不能是字母数字 ("降价到 9.9" / "19.9")
    m = re.search(r"(?<![0-9a-zA-Z-])(\d+(?:\.\d{1,2})?)(?![0-9a-zA-Z])", text)
    if m:
        return int(round(float(m.group(1)) * 100))
    return None


def _extract_int(text: str) -> int | None:
    """从文本提取数量/个数。优先带量词的 ("30件"/"2斤"/"100箱"), 否则第一个整数。"""
    m = re.search(r"(\d+)\s*(?:件|个|斤|箱|包|份|袋|瓶|盒|公斤|千克|克)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def _extract_title(text: str) -> str:
    """从命令文本提取商品名。"上架 丹东草莓 19.9元 30件" → "丹东草莓"。"""
    t = text
    # 先剔动词/干扰词 (按长度降序, 避免"上架"吃掉"下架"之类)
    for noise in sorted(_TITLE_NOISE, key=len, reverse=True):
        t = t.replace(noise, " ")
    # 剔金额 ("19.9元"/"¥19.9"/"19块9")
    t = re.sub(r"(?:¥|￥)?\s*\d+(?:\.\d{1,2})?\s*块\s*\d{1,2}?\s*(?:毛|角)?", " ", t)
    t = re.sub(r"(?:¥|￥)?\s*\d+(?:\.\d{1,2})?\s*(?:元|RMB)?", " ", t)
    # 剔数量 ("30件"/"50个"/"2斤"/"10箱")
    t = re.sub(r"\d+\s*(?:件|个|斤|箱|包|份|袋|瓶|盒|公斤|千克|克)", " ", t)
    t = re.sub(r"\d+", " ", t)  # 残余数字
    t = " ".join(t.split())
    return (t.strip() or "未命名商品")[:40]


def route_command(text: str) -> dict[str, Any]:
    """自然语言命令 → 意图 + 实体 (规则引擎, 可替换为 LLM tool calling)。

    Returns:
        {kind, tool, entities, confidence, matched_keyword}
        entities: {title, money_cents, quantity, order_id, batch_id}
    """
    for keywords, kind in _COMMAND_KINDS:
        for kw in keywords:
            if kw in text:
                uuid = _UUID_RE.search(text)
                return {
                    "kind": kind,
                    "tool": _KIND_TOOL[kind],
                    "entities": {
                        "title": _extract_title(text),
                        "money_cents": _extract_money(text),
                        "quantity": _extract_int(text),
                        "order_id": uuid.group(0) if uuid else None,
                        "batch_id": uuid.group(0) if uuid else None,
                    },
                    "confidence": 0.8,
                    "matched_keyword": kw,
                }
    return {"kind": None, "tool": None, "entities": {}, "confidence": 0.0, "matched_keyword": None}


# ── 编排器: 实体 → 真实 omodul 参数 (缺 ID 从库解析) ─────────────────


async def _resolve_default_location(pool: Any) -> str | None:
    """默认仓位: 第一个 active 的 stock_location。"""
    if pool is None:
        return None
    async with pool.acquire() as conn:
        loc = await conn.fetchval(
            'SELECT id FROM "stock_location" WHERE status = $1 ORDER BY created_at LIMIT 1',
            "active",
        )
        return str(loc) if loc else None


async def _resolve_batch_by_title(
    pool: Any, title: str, *, default_to_latest: bool = True
) -> dict[str, str] | None:
    """按商品名找最新 active 批次 (商品名模糊匹配), 取 batch/variant/location。

    Returns:
        {batch_id, variant_id, location_id} 或 None
    """
    if pool is None:
        return None
    async with pool.acquire() as conn:
        if title and title != "未命名商品":
            row = await conn.fetchrow(
                'SELECT b.id AS batch_id, b.variant_id, b.location_id '
                'FROM "inventory_batch" b '
                'JOIN "product_variant" v ON v.id = b.variant_id '
                'JOIN "product" p ON p.id = v.product_id '
                'WHERE b.status = $1 AND p.deleted_at IS NULL AND p.title ILIKE $2 '
                'ORDER BY b.created_at DESC LIMIT 1',
                "active",
                f"%{title}%",
            )
        else:
            row = None
        if row is None and default_to_latest:
            row = await conn.fetchrow(
                'SELECT id AS batch_id, variant_id, location_id FROM "inventory_batch" '
                "WHERE status = $1 ORDER BY created_at DESC LIMIT 1",
                "active",
            )
    if not row:
        return None
    return {"batch_id": str(row["batch_id"]), "variant_id": str(row["variant_id"]), "location_id": str(row["location_id"])}


async def _resolve_order(pool: Any, order_id: str | None) -> str | None:
    """订单: 指定 ID 或最新一笔。"""
    if pool is None:
        return order_id
    async with pool.acquire() as conn:
        if order_id:
            row = await conn.fetchval('SELECT id FROM "customer_order" WHERE id = $1', order_id)
        else:
            row = await conn.fetchval('SELECT id FROM "customer_order" ORDER BY created_at DESC LIMIT 1')
        return str(row) if row else None


async def _resolve_default_category(pool: Any) -> str | None:
    """默认分类: 日用百货, 缺失则最早一个。"""
    if pool is None:
        return None
    async with pool.acquire() as conn:
        cat = await conn.fetchval(
            'SELECT id FROM "product_category" WHERE slug = $1 AND deleted_at IS NULL', "daily"
        )
        if cat is None:
            cat = await conn.fetchval(
                'SELECT id FROM "product_category" ORDER BY created_at LIMIT 1'
            )
        return str(cat) if cat else None


def _placeholder_media(title: str) -> str:
    """命令上架无实拍媒体时, 生成自包含 SVG data-URI 占位图 (商品名居中)。

    商城卡片/信息流把它当 <img src> 渲染, 无需服务器额外文件。
    """
    import urllib.parse

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400">'
        '<rect width="100%" height="100%" fill="#e8f5e9"/>'
        '<circle cx="200" cy="150" r="60" fill="#a5d6a7"/>'
        '<text x="50%" y="270" font-size="30" text-anchor="middle" '
        'fill="#2e7d32" font-family="sans-serif">' + title + "</text></svg>"
    )
    return "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg)


async def execute_command(
    text: str,
    *,
    pool: Any,
    out_root: Path,
    principal: dict[str, Any],
) -> dict[str, Any]:
    """自然语言命令执行编排器 (手机指挥台 / Hermes/Cindy 命令入口)。

    按命令种类组装真实 omodul 参数: 能提取的实体直接用, 缺的 ID 按
    商品名模糊匹配/最新记录从库解析; 上架走 create_product →
    create_product_variant → create_inventory_batch 三步链路。
    """
    routed = route_command(text)
    if not routed["kind"]:
        return {
            "status": "unrouted",
            "text": text,
            "hint": "未能理解命令，试试: 上架 / 调价 / 补货 / 结算 / 退款 / 下架 / 广播",
        }

    kind = routed["kind"]
    ent = routed["entities"]
    title = ent.get("title") or ""
    money = ent.get("money_cents")
    qty = ent.get("quantity") or 30
    matched = routed["matched_keyword"]

    # ── 上架: 三步链路 create_product → variant → batch ──
    if kind == "list_product":
        retail = money or 1299
        cost = int(retail * 0.6)
        category_id = await _resolve_default_category(pool)
        product_args: dict[str, Any] = {
            "title": title,
            "slug": f"{title}-{datetime.now(timezone.utc).strftime('%H%M%S')}",
            "description": f"手机命令上架 ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')})。",
        }
        if category_id:
            product_args["category_id"] = category_id
        step1 = await execute_tool(
            "catalog/create_product",
            product_args,
            pool=pool,
            out_root=out_root,
            principal=principal,
        )
        if step1.get("status") != "completed":
            return {"status": "failed", "text": text, "matched_keyword": matched, "kind": kind, "error": step1}
        product_id = step1["product_id"]
        # 上架即发布 (共享 create_product 默认 draft, 商城只展示 published)
        pub = await execute_tool(
            "catalog/update_product",
            {**product_args, "product_id": product_id, "status": "published"},
            pool=pool,
            out_root=out_root,
            principal=principal,
        )
        if pub.get("status") != "completed":
            return {"status": "failed", "text": text, "matched_keyword": matched, "kind": kind, "error": pub}
        step2 = await execute_tool(
            "catalog/create_product_variant",
            {
                "product_id": product_id,
                "sku_code": f"CMD-{datetime.now(timezone.utc).strftime('%H%M%S')}",
                "option_values": {"来源": "手机命令"},
                "reference_price_cents": int(retail * 1.2),
            },
            pool=pool,
            out_root=out_root,
            principal=principal,
        )
        if step2.get("status") != "completed":
            return {"status": "failed", "text": text, "matched_keyword": matched, "kind": kind, "error": step2}
        variant_id = step2["variant_id"]
        location_id = await _resolve_default_location(pool)
        if not location_id:
            return {
                "status": "needs_input",
                "text": text,
                "matched_keyword": matched,
                "kind": kind,
                "hint": "系统没有可用仓位 (stock_location)，请先创建仓位再上架",
            }
        step3 = await execute_tool(
            "supply-chain/create_inventory_batch",
            {
                "variant_id": variant_id,
                "location_id": location_id,
                "video_url": _placeholder_media(title),
                "stock_qty": qty,
                "cost_price": cost,
                "retail_price": retail,
            },
            pool=pool,
            out_root=out_root,
            principal=principal,
        )
        if step3.get("status") != "completed":
            return {"status": "failed", "text": text, "matched_keyword": matched, "kind": kind, "error": step3}
        return {
            "status": "executed",
            "text": text,
            "matched_keyword": matched,
            "kind": kind,
            "result": {
                "title": title,
                "product_id": product_id,
                "variant_id": variant_id,
                "batch_id": step3.get("batch_id"),
                "retail_price_cents": retail,
                "stock_qty": qty,
            },
        }

    # ── 调价: 按商品名找批次, 直接改批次零售价 (同步参考价) ──
    if kind == "adjust_price":
        if money is None:
            return {"status": "needs_input", "text": text, "matched_keyword": matched, "hint": "请给出新价格, 如: 丹东草莓降价到 9.9"}
        batch = await _resolve_batch_by_title(pool, title)
        if not batch:
            return {"status": "needs_input", "text": text, "matched_keyword": matched, "hint": "没有找到可调价的 active 批次 (先上架或传货)"}
        async with pool.acquire() as conn:
            await conn.execute(
                'UPDATE "inventory_batch" SET retail_price_cents = $1 WHERE id = $2',
                money,
                batch["batch_id"],
            )
        # 同步 variant 参考价
        try:
            await execute_tool(
                "catalog/update_product_variant",
                {"variant_id": batch["variant_id"], "reference_price_cents": int(money * 1.2)},
                pool=pool,
                out_root=out_root,
                principal=principal,
            )
        except HTTPException:
            pass  # 参考价同步失败不阻塞主流程
        return {
            "status": "executed",
            "text": text,
            "matched_keyword": matched,
            "kind": kind,
            "result": {"batch_id": batch["batch_id"], "retail_price_cents": money},
        }

    # ── 补货: adjust_inventory_level (delta=+数量) ──
    if kind == "restock":
        delta = max(qty or 1, 1)
        batch = await _resolve_batch_by_title(pool, title)
        if not batch:
            return {"status": "needs_input", "text": text, "matched_keyword": matched, "hint": "没有找到可补货的 active 批次 (先上架或传货)"}
        result = await execute_tool(
            "inventory/adjust_inventory_level",
            {"batch_id": batch["batch_id"], "delta": delta, "reason": "手机补货指令"},
            pool=pool,
            out_root=out_root,
            principal=principal,
        )
        return {"status": "executed" if result.get("status") == "completed" else "failed", "text": text, "matched_keyword": matched, "kind": kind, "result": {**result, "batch_id": batch["batch_id"], "delta": delta}}

    # ── 下架/报废: mark_batch_for_disposal ──
    if kind == "dispose":
        batch = await _resolve_batch_by_title(pool, title)
        if not batch:
            return {"status": "needs_input", "text": text, "matched_keyword": matched, "hint": "没有找到可下架的 active 批次"}
        result = await execute_tool(
            "supply-chain/mark_batch_for_disposal",
            {"batch_id": batch["batch_id"], "reason": "手机下架指令"},
            pool=pool,
            out_root=out_root,
            principal=principal,
        )
        return {"status": "executed" if result.get("status") == "completed" else "failed", "text": text, "matched_keyword": matched, "kind": kind, "result": {**result, "batch_id": batch["batch_id"]}}

    # ── 结算: batch_settlement (缺批次用最新 active) ──
    if kind == "settle":
        batch = await _resolve_batch_by_title(pool, title, default_to_latest=True)
        if not batch:
            return {"status": "needs_input", "text": text, "matched_keyword": matched, "hint": "没有可结算的批次"}
        result = await execute_tool(
            "supply-chain/batch_settlement",
            {"batch_id": batch["batch_id"], "supplier_account": "supplier@hemall.local"},
            pool=pool,
            out_root=out_root,
            principal=principal,
        )
        return {"status": "executed" if result.get("status") == "completed" else "failed", "text": text, "matched_keyword": matched, "kind": kind, "result": {**result, "batch_id": batch["batch_id"]}}

    # ── 退款: refund_payment (order_id + amount_cents) ──
    if kind == "refund":
        if money is None:
            return {"status": "needs_input", "text": text, "matched_keyword": matched, "hint": "请给出退款金额, 如: 订单 <id> 退款 50 元"}
        order_id = await _resolve_order(pool, ent.get("order_id"))
        if not order_id:
            return {"status": "needs_input", "text": text, "matched_keyword": matched, "hint": "没有找到可退款的订单"}
        result = await execute_tool(
            "aftersales/refund_payment",
            {"order_id": order_id, "amount_cents": money},
            pool=pool,
            out_root=out_root,
            principal=principal,
        )
        return {"status": "executed" if result.get("status") == "completed" else "failed", "text": text, "matched_keyword": matched, "kind": kind, "result": {**result, "order_id": order_id, "amount_cents": money}}

    # ── 广播: execute_channel_broadcast_workflow ──
    if kind == "broadcast":
        batch = await _resolve_batch_by_title(pool, title)
        if not batch:
            return {"status": "needs_input", "text": text, "matched_keyword": matched, "hint": "没有找到可推广的批次"}
        result = await execute_tool(
            "marketing/execute_channel_broadcast_workflow",
            {
                "batch_id": batch["batch_id"],
                "broadcast_type": "video",
                "market_price": money or 0,
                "access_token": "",
            },
            pool=pool,
            out_root=out_root,
            principal=principal,
        )
        return {"status": "executed" if result.get("status") == "completed" else "failed", "text": text, "matched_keyword": matched, "kind": kind, "result": {**result, "batch_id": batch["batch_id"]}}

    # ── 需要实体 ID 但规则引擎无法可靠提取的 (留给 Hermes/Cindy LLM 编排) ──
    return {
        "status": "needs_input",
        "text": text,
        "matched_keyword": matched,
        "kind": kind,
        "tool": routed["tool"],
        "hint": (
            f"命令「{kind}」需要结构化参数, 请让 Hermes/Cindy 通过 /agent/execute "
            f"调用 {routed['tool']} (参数 schema 见 /agent/tools), 或补全: 佣金需 host_id, "
            "工资需 worker_id, 意向需 variant_id+customer_id, 补天需 order_id+batch_id"
        ),
    }


# ── 视频传货 (实景视频 → 商品草稿 → 上架) ─────────────────────────────


def _clean_filename(name: str) -> str:
    """从上传文件名清洗出商品名 (去扩展名/下划线/数字串)。"""
    stem = Path(name).stem
    stem = re.sub(r"[\-_]+\d+$", "", stem)  # 去尾部数字
    stem = re.sub(r"[_\-]+", " ", stem)
    return stem.strip()[:40] or "未命名商品"


async def ingest_media(
    file: UploadFile,
    *,
    location_id: str,
    pool: Any,
    out_root: Path,
    principal: dict[str, Any],
    retail_price_cents: int | None = None,
    stock_qty: int = 30,
    category_slug: str = "daily",
) -> dict[str, Any]:
    """视频/图片传货: 保存实景文件 → 生成商品草稿 → 自动上架。

    流程 (MVP, LLM/VLM 就绪后可升级为自动识别):
      1. 保存上传文件到 <out_root>/agent_ingest/<user>/<ts>_<filename>
      2. 商品名 = 文件名清洗 (如 "丹东草莓实拍.mp4" → "丹东草莓")
      3. 自动创建 product (published) + variant + inventory_batch
         - 图片 = 媒体文件本身 (video_url 填上传文件, 前端可播放)
         - 售价 = 参数或默认 (成本 + 毛利)
      4. 返回商品 ID + 草稿信息

    幂等: 文件名重复会 SKU 冲突, 自动加时间戳后缀避免。
    """
    if not file.filename:
        raise HTTPException(400, "empty upload")

    # 1. 存盘
    user_ref = str(principal.get("user_id") or "anonymous")[:8]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    ingest_dir = out_root / "agent_ingest" / user_ref
    ingest_dir.mkdir(parents=True, exist_ok=True)
    dest = ingest_dir / f"{ts}_{file.filename}"
    content = await file.read()
    if not content:
        raise HTTPException(400, "empty file")
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(413, "file too large (max 50MB)")
    dest.write_bytes(content)

    # 2. 商品名 + SKU
    title = _clean_filename(file.filename)
    sku = f"AGT-{abs(hash(dest.name)) % 900000 + 100000}"

    # 3. 上架 (直插 SQL — 与 seed_grocery_catalog 同一套装配)
    is_video = (file.content_type or "").startswith("video/")
    media_url = f"/media/agent_ingest/{user_ref}/{dest.name}"
    cost = int((retail_price_cents or 1000) * 0.6)
    retail = retail_price_cents or 1299

    async with pool.acquire() as conn:
        cat_id = await conn.fetchval(
            'SELECT id FROM "product_category" WHERE slug = $1 AND deleted_at IS NULL',
            category_slug,
        )
        # 分类缺失时归入日用百货
        if cat_id is None:
            cat_id = await conn.fetchval(
                'SELECT id FROM "product_category" ORDER BY created_at LIMIT 1'
            )

        pid = await conn.fetchval(
            'INSERT INTO "product" (id, title, slug, description, category_id, status) '
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, 'published') RETURNING id",
            title,
            f"{title}-{ts[-6:]}",
            f"手机实景传货 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} 上传, 实拍直供。",
            cat_id,
        )
        vid = await conn.fetchval(
            'INSERT INTO "product_variant" (id, product_id, sku_code, option_values, '
            "reference_price_cents, status) "
            "VALUES (gen_random_uuid(), $1, $2, $3::jsonb, $4, 'active') RETURNING id",
            pid,
            sku,
            '{"来源": "手机实拍"}',
            int(retail * 1.2),
        )
        # 找 location (指定或最近 active)
        if not location_id:
            location_id = await conn.fetchval(
                'SELECT id FROM "stock_location" WHERE status = $1 ORDER BY created_at LIMIT 1',
                "active",
            )
        await conn.fetchval(
            'INSERT INTO "inventory_batch" (id, variant_id, location_id, batch_no, video_url, '
            "cost_price_cents, retail_price_cents, stock_qty, currency, media_assets, "
            "shelf_image_url, status) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, 'CNY', $8::jsonb, $8::jsonb, 'active') "
            "RETURNING id",
            vid,
            location_id,
            f"AGT-{ts}",
            media_url if is_video else "",
            cost,
            retail,
            stock_qty,
            f'["{media_url}"]',
        )

    return {
        "status": "created",
        "title": title,
        "product_id": str(pid),
        "variant_id": str(vid),
        "location_id": str(location_id),
        "retail_price_cents": retail,
        "cost_price_cents": cost,
        "stock_qty": stock_qty,
        "media_url": media_url,
        "media_type": "video" if is_video else "image",
        "saved_path": str(dest),
        "hint": "LLM/VLM 就绪后可自动识别商品名/分类/价格; 当前使用文件名推导, 可在后台修改。",
    }
