"""app.ext.admin_agent_chat — LLM 智能体运维中枢。

Phase 8 Task 2: 用大模型直接对话底层数据，彻底取代 BI 看板和数据分析师。

端点 `/admin/agent-chat` 受 `ADMIN_OPS` JWT 严格保护，接收自然语言查询，
通过 tool calling (function calling) 能力让 LLM 自动选择并执行底层查库原语：
  - query_node_revenue: 查询微仓营收数据
  - query_batch_status: 查询批次状态和库存流转
  - query_system_balance_liabilities: 查询系统总负债 (用户余额 + 押金 + 待赔付)
  - query_intent_cluster_summary: 查询意向金集单聚类统计

LLM 自行拼装参数 → 调用工具 → 拿到原始数据 → 用自然语言返回诊断报告。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..ext.oskill_ghost_clustering import IGNITION_THRESHOLD

logger = logging.getLogger("hemall.admin_agent")

router = APIRouter(prefix="/admin", tags=["admin-agent"])


# ── Tool 注册定义 ──────────────────────────────────────────────────────────────

class ToolDefinition(BaseModel):
    """Tool Calling 的函数定义 (兼容 OpenAI / Anthropic 格式)。"""
    name: str
    description: str
    parameters: dict[str, Any]


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="query_node_revenue",
        description=(
            "查询指定微仓节点 (location_id 或 location_name) 在给定时间范围内的"
            "营收数据。返回该节点的订单数、总收入、平均客单价、活跃批次数。"
            "如果未指定 time_range，默认查最近 7 天。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "location_id": {
                    "type": "string",
                    "description": "微仓节点 UUID，可选。与 location_name 二选一。",
                },
                "location_name": {
                    "type": "string",
                    "description": "微仓节点名称 (模糊匹配)，可选。与 location_id 二选一。",
                },
                "time_range": {
                    "type": "string",
                    "enum": ["today", "yesterday", "last_7_days", "last_30_days", "custom"],
                    "description": "时间范围。如 custom 需在 extra_params 中传入 start_date/end_date。",
                },
            },
            "required": ["time_range"],
        },
    ),
    ToolDefinition(
        name="query_batch_status",
        description=(
            "查询指定批次 (batch_id 或 product/title 模糊搜索) 的当前状态、"
            "库存数量、预留数量、售价、过期时间。用于诊断批次为什么滞销或是否该降价。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "batch_id": {
                    "type": "string",
                    "description": "批次 UUID，精确查询。",
                },
                "product_search": {
                    "type": "string",
                    "description": "商品名关键词，模糊匹配多个批次。",
                },
                "location_id": {
                    "type": "string",
                    "description": "限定某个微仓节点，可选。",
                },
            },
            "required": [],
        },
    ),
    ToolDefinition(
        name="query_system_balance_liabilities",
        description=(
            "查询系统总负债 —— 所有用户的 system_balance (算力金余额)、"
            "tote_deposit 押金总额、pending 状态的 SLA 赔付义务、"
            "pending 分润/工资支出。给出汇总数字和按类别拆分。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "breakdown": {
                    "type": "boolean",
                    "description": "是否返回分类明细 (默认 true)。",
                },
            },
            "required": [],
        },
    ),
    ToolDefinition(
        name="query_intent_cluster_summary",
        description=(
            "查询近期 C2B 意向金集单的聚类统计：总意向单数、活跃聚类数、"
            "已点火幽灵节点数、各 cluster 规模分布。用于评估 ghost ignition 引擎效果。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "min_cluster_size": {
                    "type": "integer",
                    "description": f"最小聚类的单量过滤 (默认 {IGNITION_THRESHOLD})。",
                },
            },
            "required": [],
        },
    ),
]

# ── SQL 查询模板 (只读原语) ────────────────────────────────────────────────────

SQL_QUERIES: dict[str, tuple[str, list[str]]] = {
    "query_node_revenue": (
        # 带位置过滤的营收查询
        """
        SELECT
            COUNT(DISTINCT o.id) AS total_orders,
            COALESCE(SUM(o.grand_total_cents), 0) AS total_revenue_cents,
            CASE WHEN COUNT(DISTINCT o.id) > 0
                 THEN ROUND(COALESCE(SUM(o.grand_total_cents), 0)::numeric / COUNT(DISTINCT o.id))
                 ELSE 0 END AS avg_order_value_cents,
            COUNT(DISTINCT b.id) FILTER (WHERE b.status = 'active') AS active_batches
        FROM "customer_order" o
        JOIN "order_line_item" oli ON oli.order_id = o.id
        JOIN "inventory_batch" b ON b.id = oli.batch_id
        WHERE o.status IN ('confirmed', 'processing', 'packed', 'shipped', 'delivered', 'completed')
          AND b.location_id = :location_id
          AND o.created_at >= :start_time
        """,
        ["location_id", "start_time"],
    ),
    "query_batch_status": (
        # 批次状态查询
        """
        SELECT
            b.id AS batch_id,
            p.title AS product_title,
            v.sku_code,
            b.stock_qty,
            b.reserved_qty,
            (b.stock_qty - b.reserved_qty) AS available_qty,
            b.retail_price_cents,
            b.cost_price_cents,
            b.expiration_time,
            b.status,
            b.created_at AS intake_time,
            sl.name AS location_name
        FROM "inventory_batch" b
        JOIN "product_variant" v ON v.id = b.variant_id
        JOIN "product" p ON p.id = v.product_id
        JOIN "stock_location" sl ON sl.id = b.location_id
        WHERE b.id = :batch_id
        """,
        ["batch_id"],
    ),
    "query_batch_search": (
        # 批次搜索查询
        """
        SELECT
            b.id AS batch_id,
            p.title AS product_title,
            v.sku_code,
            b.stock_qty,
            b.reserved_qty,
            (b.stock_qty - b.reserved_qty) AS available_qty,
            b.retail_price_cents,
            b.cost_price_cents,
            b.expiration_time,
            b.status,
            b.created_at AS intake_time,
            sl.name AS location_name
        FROM "inventory_batch" b
        JOIN "product_variant" v ON v.id = b.variant_id
        JOIN "product" p ON p.id = v.product_id
        JOIN "stock_location" sl ON sl.id = b.location_id
        WHERE p.title ILIKE :search_pattern
          AND b.status = 'active'
          AND b.stock_qty - b.reserved_qty > 0
        ORDER BY b.expiration_time ASC NULLS LAST
        LIMIT 50
        """,
        ["search_pattern"],
    ),
    "query_system_balance_liabilities": (
        # 系统总负债查询
        """
        -- 用户余额负债
        SELECT 'user_balances' AS category,
               SUM(system_balance) AS total_cents,
               COUNT(*) AS user_count
        FROM "customer"
        WHERE deleted_at IS NULL

        UNION ALL

        -- 押金负债
        SELECT 'tote_deposits' AS category,
               SUM(amount_cents) FILTER (WHERE status = 'charged'),
               COUNT(*) FILTER (WHERE status = 'charged')
        FROM "tote_deposit"

        UNION ALL

        -- SLA 待赔付
        SELECT 'sla_competnsations_pending' AS category,
               SUM(:compensation_amount) AS total_cents,
               COUNT(*) AS pending_count
        FROM "customer_order"
        WHERE promised_delivery_at < NOW()
          AND sla_compensated_at IS NULL
          AND status NOT IN ('canceled', 'refunded')

        UNION ALL

        -- 待结分润 (抖音达人)
        SELECT 'affiliate_payouts_pending' AS category,
               SUM(dividend_amount_cents),
               COUNT(*)
        FROM "douyin_conversion_log"
        WHERE settlement_status = 'pending'

        UNION ALL

        -- 待结计件工资
        SELECT 'labor_wages_pending' AS category,
               SUM(wage_amount),
               COUNT(*)
        FROM "labor_ledger"
        WHERE status = 'pending'
        """,
        [],
    ),
    "query_intent_cluster_summary": (
        # 意向金聚类统计
        f"""
        SELECT
            COUNT(*) AS total_pending_intentions,
            COUNT(*) FILTER (WHERE id::text LIKE 'ghost_%') AS ghost_cluster_intentions,
            SUM(prepaid_amount_cents) AS total_prepaid_cents
        FROM "crowd_intent"
        WHERE status = 'pending'
        """,
        [],
    ),
}

# ── 补偿查询：SLA pending count (因 UNIONS 的 LIMIT 限制) ──────────────────────

COMPLEMENTARY_SQL: dict[str, str] = {
    "sla_pending_count": (
        "SELECT COUNT(*) AS cnt, COALESCE(SUM($1), 0) AS total_cents "
        'FROM "customer_order" '
        "WHERE promised_delivery_at < NOW() "
        "AND sla_compensated_at IS NULL "
        "AND status NOT IN ('canceled', 'refunded')"
    ),
}


# ── Admin Auth ─────────────────────────────────────────────────────────────────


def _require_admin_ops(request: Request) -> None:
    """Admin Ops 级别鉴权——不仅要有 token，还要验证 role = ADMIN_OPS。"""
    from obase.crypto.util import CryptoUtil

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing token")
    token = auth_header.removeprefix("Bearer ")
    cfg = request.app.state.config
    try:
        decoded = CryptoUtil.jwt_decode(
            token=token, secret=cfg.jwt_secret, algorithm=cfg.jwt_algorithm
        )
    except Exception:
        raise HTTPException(status_code=401, detail="invalid token")

    # 检查 role 字段是否存在且为 ADMIN_OPS
    claims_role = decoded.get("role")
    if claims_role != "ADMIN_OPS":
        raise HTTPException(
            status_code=403,
            detail=f"insufficient privileges: required ADMIN_OPS, got {claims_role!r}",
        )


# ── Chat Request/Response Models ──────────────────────────────────────────────


class AgentChatRequest(BaseModel):
    """Agent Chat 请求体。"""
    message: str = Field(..., description="管理员的自然语言查询。")
    model: str | None = Field(None, description="覆盖默认模型的模型名，可选。")
    temperature: float = Field(0.3, ge=0, le=1, description="LLM 温度参数。")


class ToolCallResult(BaseModel):
    """工具调用执行结果。"""
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


class AgentChatResponse(BaseModel):
    """Agent Chat 响应体 (流式时逐条 SSE event)。"""
    role: str = "assistant"  # always assistant
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    is_final: bool = False  # last chunk


# ── Core Chat Handler ─────────────────────────────────────────────────────────


@router.post("/agent-chat")
async def admin_agent_chat(
    body: AgentChatRequest,
    request: Request,
    _=Depends(_require_admin_ops),
) -> dict[str, Any]:
    """LLM 智能体运维中枢入口。

    流程:
      1. 把用户的自然语言消息喂给 LLM，附带 TOOLS 定义作为 function calling 上下文。
      2. LLM 决定调用哪个/哪些工具及参数。
      3. 后端按工具名执行对应的 SQL 查询，把结果以 text/plain 形式返回。
      4. 再次把 (消息 + 工具结果) 喂给 LLM，让它生成最终的自然语言诊断报告。

    返回值是完整对话链 (messages)，包含用户的输入、LLM 的工具调用、
    工具的返回结果、以及最终的回答。生产环境建议走 stream=True (SSE)。
    """
    pool = getattr(request.app.state, "pool", None)
    settings = getattr(request.app.state, "config", None)

    if pool is None:
        raise HTTPException(status_code=503, detail="database not ready")
    if settings is None:
        raise HTTPException(status_code=503, detail="settings not ready")

    ai_service = getattr(request.app.state, "ai_service", None)
    if ai_service is None or not ai_service._llm:
        raise HTTPException(status_code=503, detail="LLM provider not configured")

    llm = ai_service._llm
    from app.ai_assistant.llm_provider import LLMMessage

    compensation_amount = settings.sla_compensation_cents if hasattr(settings, 'sla_compensation_cents') else 400

    async def execute_tool(tool_name: str, args: dict[str, Any]) -> str:
        """根据工具名执行对应的 SQL 查询，返回 JSON 字符串。"""
        try:
            if tool_name == "query_node_revenue":
                return await _execute_query_node_revenue(pool, args)
            elif tool_name == "query_batch_status":
                return await _execute_query_batch_status(pool, args)
            elif tool_name == "query_system_balance_liabilities":
                return await _execute_query_system_liabilities(pool, args, compensation_amount)
            elif tool_name == "query_intent_cluster_summary":
                return await _execute_query_intent_summary(pool, args)
            else:
                return json.dumps({"error": f"unknown tool: {tool_name}"}, ensure_ascii=False)
        except Exception as exc:
            logger.error("tool execution failed: %s(%s): %s", tool_name, args, exc, exc_info=True)
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    # Step 1: 构建系统提示词 (含 Tools 定义)
    tools_json = json.dumps([t.model_dump() for t in TOOLS], ensure_ascii=False, indent=2)
    system_prompt = (
        "你是一个专业的电商系统运维 AI 助手，代号 '透仓智脑 (ClearNode Oracle)'。"
        "你具备 SQL 查询能力和生鲜电商全链路业务知识。\n\n"
        "你可以使用以下工具来查询数据库。每个工具对应一个只读的 SQL 查询原语："
        "\n\n## 可用工具:\n"
        f"{tools_json}\n\n"
        "重要规则:\n"
        "- 你必须先调用工具获取真实数据，再用自然语言分析结果。\n"
        "- 不要编造任何数据！没有查到数据就说没有。\n"
        "- 回答要简洁专业，像资深运营总监的口吻。\n"
        "- 对于营收/库存问题，主动给出业务建议（如降价、补货、清仓等）。\n"
        "- 对于负债查询，指出最大风险项。\n"
        "- 对于意向金问题，分析是否需要加速点火建仓。\n"
        "- 回复使用中文。\n"
        "- 你的工具调用必须严格按 JSON schema 传参。"
    )

    messages: list[LLMMessage] = [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=body.message),
    ]

    # Step 2: 第一轮 LLM 调用 —— 可能触发 tool calls
    from app.ai_assistant.models import AIConfig

    model_name = body.model or ai_service._config.model_name
    temperature = body.temperature

    first_response = await llm.chat_with_tools(
        messages,
        tools=[t.model_dump() for t in TOOLS],
        model=model_name,
        temperature=temperature,
    )

    tool_calls = first_response.tool_calls or []
    if not tool_calls:
        # 没触发 tool call，直接返回答案
        return {
            "request": body.message,
            "response": first_response.content,
            "tool_calls": [],
            "model": first_response.model,
        }

    # Step 3: 执行所有工具调用
    tool_results: list[ToolCallResult] = []
    for tc in tool_calls:
        tool_result = ToolCallResult(
            tool_name=tc["function"]["name"],
            arguments=json.loads(tc["function"].get("arguments", "{}")),
        )
        raw_result = await execute_tool(tool_result.tool_name, tool_result.arguments)
        tool_result.result = json.loads(raw_result) if raw_result.strip().startswith("{") else {"raw_text": raw_result}
        tool_results.append(tool_result)

    # Step 4: 组装第二轮对话 —— 把工具结果喂给 LLM
    messages.append(LLMMessage(role="assistant", content=first_response.content))
    for tr in tool_results:
        messages.append(
            LLMMessage(
                role="tool",
                content=json.dumps(tr.result, ensure_ascii=False, default=str),
            )
        )

    # Step 5: 最终回答 —— LLM 基于真实数据生成诊断报告
    final_response = await llm.chat(messages, model=model_name, temperature=0.3)

    return {
        "request": body.message,
        "tool_calls": [
            {
                "tool_name": tr.tool_name,
                "arguments": tr.arguments,
                "result": tr.result,
            }
            for tr in tool_results
        ],
        "final_answer": final_response.content,
        "model": final_response.model,
    }


# ── Tool 执行器实现 ────────────────────────────────────────────────────────────


async def _execute_query_node_revenue(
    pool: Any, args: dict[str, Any]
) -> str:
    """执行微仓营收查询。"""
    from datetime import UTC, datetime, timedelta

    location_id = args.get("location_id")
    location_name = args.get("location_name")
    time_range = args.get("time_range", "last_7_days")

    now = datetime.now(UTC)
    if time_range == "today":
        start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_range == "yesterday":
        start_time = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_range == "last_7_days":
        start_time = now - timedelta(days=7)
    elif time_range == "last_30_days":
        start_time = now - timedelta(days=30)
    else:
        start_time = now - timedelta(days=7)

    sql, params_list = SQL_QUERIES["query_node_revenue"]
    safe_sql = sql.replace(":location_id", "$%d").replace(":start_time", "$%d") % (
        len(params_list) + 1,
        len(params_list) + 2,
    )
    params = list(params_list)
    params.append(location_id or "")
    params.append(start_time.isoformat())

    if location_name:
        safe_sql = safe_sql.replace("b.location_id = :location_id", 'sl.name ILIKE :loc_name')
        safe_sql = safe_sql.replace("$%d" % (len(params)-1), ":loc_name")
        params.append(f"%{location_name}%")
        # Reconstruct with named params
        safe_sql = sql.replace(":location_id", ":loc_name").replace(":start_time", ":start_time")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(safe_sql[:500], *params[:3])  # truncation safety
        if row:
            return json.dumps({
                "total_orders": row.get("total_orders", 0),
                "total_revenue_cents": row.get("total_revenue_cents", 0),
                "avg_order_value_cents": row.get("avg_order_value_cents", 0),
                "active_batches": row.get("active_batches", 0),
            }, ensure_ascii=False)
        return json.dumps({"message": "no revenue data found"}, ensure_ascii=False)


async def _execute_query_batch_status(
    pool: Any, args: dict[str, Any]
) -> str:
    """执行批次状态查询。"""
    batch_id = args.get("batch_id")
    product_search = args.get("product_search")

    if batch_id:
        sql, param_names = SQL_QUERIES["query_batch_status"]
        safe_sql = sql.replace(":batch_id", "$1")
        async with pool.acquire() as conn:
            rows = await conn.fetch(safe_sql, batch_id)
    elif product_search:
        sql, param_names = SQL_QUERIES["query_batch_search"]
        search_pattern = f"%{product_search}%"
        safe_sql = sql.replace(":search_pattern", "$1")
        async with pool.acquire() as conn:
            rows = await conn.fetch(safe_sql, search_pattern)
    else:
        return json.dumps({"error": "must provide batch_id or product_search"})

    results = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
        results.append(d)

    if not results:
        return json.dumps({"message": "no batches found matching criteria"}, ensure_ascii=False)
    return json.dumps(results, ensure_ascii=False, default=str)


async def _execute_query_system_liabilities(
    pool: Any, args: dict[str, Any], compensation_amount: int
) -> str:
    """执行系统总负债查询。"""
    liability_data = {}

    async with pool.acquire() as conn:
        # 用户余额
        row = await conn.fetchval(
            'SELECT SUM(system_balance) FROM "customer" WHERE deleted_at IS NULL'
        )
        liability_data["user_balances_cents"] = int(row or 0)

        # 押金
        row = await conn.fetchval(
            'SELECT SUM(amount_cents) FROM "tote_deposit" WHERE status = $1',
            "charged",
        )
        liability_data["tote_deposits_cents"] = int(row or 0)

        # SLA 待赔付计数
        sla_row = await conn.fetchrow(
            COMPLEMENTARY_SQL["sla_pending_count"],
            compensation_amount,
        )
        liability_data["sla_pending_count"] = int(sla_row["cnt"] if sla_row else 0)
        liability_data["sla_pending_cents"] = int(sla_row["total_cents"] if sla_row else 0)

        # 待结分润
        row = await conn.fetchval(
            'SELECT SUM(dividend_amount_cents) FROM "douyin_conversion_log" WHERE settlement_status = $1',
            "pending",
        )
        liability_data["affiliate_payouts_pending_cents"] = int(row or 0)

        # 待结工资
        row = await conn.fetchval(
            'SELECT SUM(wage_amount) FROM "labor_ledger" WHERE status = $1',
            "pending",
        )
        liability_data["labor_wages_pending_cents"] = int(row or 0)

    total_liability = sum(liability_data.values())
    liability_data["total_liability_cents"] = total_liability
    liability_data["total_liability_yuan"] = round(total_liability / 100, 2)

    return json.dumps(liability_data, ensure_ascii=False, default=str)


async def _execute_query_intent_summary(
    pool: Any, args: dict[str, Any]
) -> str:
    """执行意向金聚类统计查询。"""
    min_size = args.get("min_cluster_size", IGNITION_THRESHOLD)

    async with pool.acquire() as conn:
        total_pending = await conn.fetchval(
            'SELECT COUNT(*) FROM "crowd_intent" WHERE status = $1',
            "pending",
        )
        total_prepaid = await conn.fetchval(
            'SELECT COALESCE(SUM(prepaid_amount_cents), 0) FROM "crowd_intent" WHERE status = $1',
            "pending",
        )

        # 幽灵节点点火数
        ghost_nodes = await conn.fetchval(
            'SELECT COUNT(*) FROM "stock_location" WHERE status = $1',
            "pending_hardware",
        )

        # 近期点火成功的簇
        recent_ignitions = await conn.fetchval(
            "SELECT COUNT(*) FROM \"stock_location\" "
            "WHERE status = $1 AND created_at > NOW() - INTERVAL '7 days'",
            "pending_hardware",
        )

    return json.dumps({
        "total_pending_intentions": int(total_pending or 0),
        "total_prepaid_cents": int(total_prepaid or 0),
        "pending_hardware_nodes": int(ghost_nodes or 0),
        "recent_ignitions_7d": int(recent_ignitions or 0),
        "threshold_for_ignition": min_size,
    }, ensure_ascii=False)
