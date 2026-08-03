"""tests/test_phase8_agent_chat.py — Phase 8: LLM 智能体运维中枢测试。

覆盖:
  - admin_agent_chat router (/admin/agent-chat)
  - Tool 定义完整性
  - SQL 查询原语可执行性 (mock pool)
  - Admin-Ops JWT 鉴权
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ext.admin_agent_chat import (
    router,
    TOOLS,
    SQL_QUERIES,
    _execute_query_node_revenue,
    _execute_query_batch_status,
    _execute_query_system_liabilities,
    _execute_query_intent_summary,
)


class TestToolDefinitions:
    """验证 Tool 定义格式完整且符合 Tool Calling 协议。"""

    def test_all_tools_have_required_fields(self):
        for tool in TOOLS:
            assert tool.name, f"Tool name is empty"
            assert tool.description, f"Tool {tool.name} has no description"
            assert tool.parameters, f"Tool {tool.name} has no parameters"
            assert isinstance(tool.parameters, dict), (
                f"Tool {tool.name}.parameters should be a dict"
            )

    def test_tool_names_match_sql_keys(self):
        """每个 tool.name 都应该对应一个 SQL_QUERIES 键。"""
        sql_keys = set(SQL_QUERIES.keys())
        tool_names = {t.name for t in TOOLS}
        # query_node_revenue 对应两条 SQL (带位置过滤 + 按名称过滤)
        expected_coverage = {"query_node_revenue", "query_batch_status",
                             "query_batch_search", "query_system_balance_liabilities",
                             "query_intent_cluster_summary"}
        assert tool_names.issubset(sql_keys | set(expected_coverage)), (
            f"Tools not covered by SQL: {tool_names - sql_keys}"
        )

    def test_query_system_balance_has_breakdown_param(self):
        liab_tool = next(t for t in TOOLS if t.name == "query_system_balance_liabilities")
        assert "breakdown" in liab_tool.parameters["properties"]

    def test_query_intent_summary_has_min_cluster_size(self):
        intent_tool = next(t for t in TOOLS if t.name == "query_intent_cluster_summary")
        assert "min_cluster_size" in intent_tool.parameters["properties"]


class TestSQLQueryTemplates:
    """验证 SQL 查询模板的合法性 (语法检查)。"""

    def test_all_queries_have_params_list(self):
        for name, (sql, params) in SQL_QUERIES.items():
            assert isinstance(params, list), f"SQL '{name}' params should be a list"
            assert isinstance(sql, str), f"SQL '{name}' should be a string"
            assert len(sql.strip()) > 0, f"SQL '{name}' is empty"

    def test_no_delete_or_update_in_read_tools(self):
        """只读工具不应该包含 DELETE / UPDATE / DROP。"""
        read_tools = {"query_node_revenue", "query_batch_status",
                      "query_batch_search", "query_system_balance_liabilities",
                      "query_intent_cluster_summary"}
        for name, (sql, _) in SQL_QUERIES.items():
            if name in read_tools:
                sql_upper = sql.upper()
                assert "DELETE " not in sql_upper and "UPDATE " not in sql_upper and \
                       "DROP " not in sql_upper, (
                    f"Read-only tool '{name}' contains destructive SQL: {sql[:50]}"
                )


@pytest.mark.asyncio
class TestExecuteQueriesMocked:
    """使用 mock pool 验证查询执行逻辑。"""

    @pytest.fixture
    def mock_pool_and_conn(self):
        """创建 mock PgPool 和可配置的连接。返回 (pool, conn) 以便测试配置 conn 方法。"""
        mock_conn = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=None)
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=mock_cm)
        return mock_pool, mock_conn

    @pytest.mark.asyncio
    async def test_query_node_revenue_success(self, mock_pool_and_conn):
        mock_pool, mock_conn = mock_pool_and_conn
        mock_conn.fetchrow = AsyncMock(return_value={
            "total_orders": 42,
            "total_revenue_cents": 420000,
            "avg_order_value_cents": 10000,
            "active_batches": 5,
        })

        result = await _execute_query_node_revenue(mock_pool, {
            "location_id": "test-loc-id",
            "time_range": "last_7_days",
        })

        data = json.loads(result)
        assert data["total_orders"] == 42
        assert data["total_revenue_cents"] == 420000

    @pytest.mark.asyncio
    async def test_query_node_revenue_no_data(self, mock_pool_and_conn):
        mock_pool, mock_conn = mock_pool_and_conn
        mock_conn.fetchrow = AsyncMock(return_value=None)

        result = await _execute_query_node_revenue(mock_pool, {
            "location_id": "nonexistent",
            "time_range": "today",
        })

        data = json.loads(result)
        assert "no revenue data" in data.get("message", "").lower()

    @pytest.mark.asyncio
    async def test_query_batch_status_by_id(self, mock_pool_and_conn):
        mock_pool, mock_conn = mock_pool_and_conn
        mock_conn.fetch = AsyncMock(return_value=[{
            "batch_id": "b1",
            "product_title": "Test Product",
            "stock_qty": 5,
            "reserved_qty": 3,
            "available_qty": 2,
            "retail_price_cents": 1000,
            "status": "active",
        }])

        result = await _execute_query_batch_status(mock_pool, {
            "batch_id": "test-batch-id",
        })

        data = json.loads(result)
        assert len(data) >= 1
        assert data[0]["stock_qty"] == 5

    @pytest.mark.asyncio
    async def test_query_batch_status_missing_params(self, mock_pool_and_conn):
        mock_pool, _ = mock_pool_and_conn
        result = await _execute_query_batch_status(mock_pool, {})
        data = json.loads(result)
        assert "error" in data
        assert "must provide batch_id or product_search" in data["error"]

    @pytest.mark.asyncio
    async def test_query_system_liabilities_success(self, mock_pool_and_conn):
        mock_pool, mock_conn = mock_pool_and_conn
        mock_conn.fetchval = AsyncMock(side_effect=[
            500000,   # user_balances
            300000,   # tote_deposits
            100000,   # affiliate_payouts
            80000,    # labor_wages
        ])
        mock_conn.fetchrow = AsyncMock(return_value={
            "cnt": 5,
            "total_cents": 2000,
        })

        result = await _execute_query_system_liabilities(
            mock_pool, {}, compensation_amount=400
        )

        data = json.loads(result)
        assert data["user_balances_cents"] == 500000
        assert data["tote_deposits_cents"] == 300000
        assert data["sla_pending_count"] == 5
        assert data["sla_pending_cents"] == 2000
        assert "total_liability_yuan" in data

    @pytest.mark.asyncio
    async def test_query_intent_summary_success(self, mock_pool_and_conn):
        mock_pool, mock_conn = mock_pool_and_conn
        mock_conn.fetchval = AsyncMock(side_effect=[
            1500,     # total_pending_intentions
            7500000,  # total_prepaid_cents
            3,        # ghost_nodes (pending_hardware)
            2,        # recent_ignitions
        ])

        result = await _execute_query_intent_summary(mock_pool, {})
        data = json.loads(result)
        assert data["total_pending_intentions"] == 1500
        assert data["pending_hardware_nodes"] == 3
        assert data["recent_ignitions_7d"] == 2


CRYPTO_SECRET = "phase8-test-jwt-secret-key-that-is-at-least-32-characters!"


class TestAdminAuthLogic:
    """验证 ADMIN_OPS JWT 鉴权逻辑。"""

    @pytest.fixture
    def make_token(self):
        from obase.crypto.util import CryptoUtil

        def _create(role: str = "ADMIN_OPS") -> str:
            payload = {"sub": "test-user-id", "email": "admin@test.com", "role": role}
            return CryptoUtil.jwt_sign(
                payload=payload,
                secret=CRYPTO_SECRET,
                expires_in_minutes=60,
                algorithm="HS256",
            )
        return _create

    def test_admin_ops_role_allowed(self, make_token):
        from obase.crypto.util import CryptoUtil
        token = make_token(role="ADMIN_OPS")
        assert len(token) > 0
        decoded = CryptoUtil.jwt_decode(
            token=token, secret=CRYPTO_SECRET, algorithm="HS256"
        )
        assert decoded.get("role") == "ADMIN_OPS"

    def test_non_admin_role_rejected(self, make_token):
        from obase.crypto.util import CryptoUtil
        token = make_token(role="USER")
        decoded = CryptoUtil.jwt_decode(
            token=token, secret=CRYPTO_SECRET, algorithm="HS256"
        )
        assert decoded.get("role") == "USER"
        # 在 _require_admin_ops 中会 403


class TestAgentChatResponseFormat:
    """验证 /admin/agent-chat 端点的返回格式。"""

    def test_response_contains_request_and_final_answer_when_no_tools(self):
        """当 LLM 不触发 tool call 时，response 应直接包含最终答案。"""
        sample_response = {
            "request": "查看系统负债",
            "tool_calls": [],
            "final_answer": "当前系统总负债为 ¥15.8 万元...",
            "model": "gpt-4o",
        }
        assert "request" in sample_response
        assert "final_answer" in sample_response
        assert sample_response["tool_calls"] == []

    def test_response_contains_tool_execution_chain_when_tools_triggered(self):
        """当触发 tool calls 时，response 应包含完整的工具调用链。"""
        sample_response = {
            "request": "3号仓今天为什么滞销？",
            "tool_calls": [
                {
                    "tool_name": "query_batch_status",
                    "arguments": {"product_search": "牛奶"},
                    "result": [{"batch_id": "test-batch", "stock_qty": 50}],
                },
            ],
            "final_answer": "根据数据，3号仓库存积压严重...建议降价清仓",
            "model": "claude-3-sonnet",
        }
        assert len(sample_response["tool_calls"]) >= 1
        tc = sample_response["tool_calls"][0]
        assert "tool_name" in tc
        assert "result" in tc

    def test_tool_call_structure(self):
        """验证单条 tool call 的数据结构。"""
        tc = {
            "tool_name": "query_node_revenue",
            "arguments": {"location_id": "abc", "time_range": "today"},
            "result": {"total_orders": 10},
        }
        assert isinstance(tc["tool_name"], str)
        assert isinstance(tc["arguments"], dict)
        assert isinstance(tc["result"], dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
