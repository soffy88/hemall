"""tests/test_phase95_agent_gateway.py — Phase 9.5: 智能体网关测试。

覆盖:
  - discover_tools (工具发现: 从 omodul registry 生成)
  - find_tool (工具查找)
  - route_command (自然语言命令路由 + 参数提取)
  - _clean_filename (视频文件名清洗)
"""

from __future__ import annotations

import pytest

from app.ext.agent_gateway import (
    _clean_filename,
    _extract_int,
    _extract_money,
    _extract_title,
    discover_tools,
    find_tool,
    route_command,
)


class TestDiscoverTools:
    def test_discovers_business_tools(self):
        tools = discover_tools()
        assert len(tools) > 20
        names = {t["tool"] for t in tools}
        # 上架链路工具必须存在
        assert any("create_product" in n for n in names)
        assert any("create_inventory_batch" in n for n in names)
        assert any("batch_settlement" in n for n in names)

    def test_tool_schema_fields(self):
        for t in discover_tools():
            assert t["tool"]
            assert t["domain"]
            assert t["path"].startswith("/")
            assert isinstance(t["parameters"], dict)
            assert isinstance(t["require_auth"], bool)


class TestFindTool:
    def test_find_by_slash_name(self):
        spec = find_tool("supply-chain/batch_settlement")
        assert spec is not None
        assert spec.name == "batch_settlement"

    def test_missing_tool_raises(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            find_tool("nonexistent/tool")
        assert ei.value.status_code == 404


class TestExtractors:
    def test_money_yuan_to_cents(self):
        assert _extract_money("上架 土豆 2.99元") == 299
        assert _extract_money("¥19.9") == 1990
        assert _extract_money("19块9") == 1990

    def test_money_missing(self):
        assert _extract_money("上架一批货") is None

    def test_int(self):
        assert _extract_int("30件") == 30
        assert _extract_int("没有数字") is None


class TestRouteCommand:
    def test_route_上架(self):
        r = route_command("上架 丹东草莓 19.9元 30件")
        assert r["kind"] == "list_product"
        assert r["tool"] == "supply-chain/create_inventory_batch"
        assert r["entities"]["money_cents"] == 1990
        assert r["entities"]["quantity"] == 30

    def test_route_上架_标题提取(self):
        r = route_command("上架 山东红富士 12.9元 60件")
        assert r["kind"] == "list_product"
        assert "红富士" in r["entities"]["title"]

    def test_route_调价(self):
        r = route_command("丹东草莓降价到 9.9")
        assert r["kind"] == "adjust_price"
        assert r["entities"]["money_cents"] == 990
        assert "丹东草莓" in r["entities"]["title"]

    def test_route_结算(self):
        r = route_command("给供应商结算")
        assert r["kind"] == "settle"

    def test_route_退款(self):
        r = route_command("订单 1ecc91d5-5f36-4cf3-bc79-5f5e3aa9b3fb 退款 50 元")
        assert r["kind"] == "refund"
        assert r["entities"]["money_cents"] == 5000
        assert r["entities"]["order_id"] == "1ecc91d5-5f36-4cf3-bc79-5f5e3aa9b3fb"

    def test_route_补货(self):
        r = route_command("牛奶补货 100 件")
        assert r["kind"] == "restock"
        assert r["entities"]["quantity"] == 100

    def test_unrouted(self):
        r = route_command("今天天气怎么样")
        assert r["kind"] is None
        assert r["confidence"] == 0.0


class TestTitleExtraction:
    def test_simple(self):
        assert "丹东草莓" in _extract_title("上架 丹东草莓 19.9元 30件")

    def test_price_only_no_title(self):
        assert _extract_title("上架 19.9元 30件") == "未命名商品"

    def test_trailing_verb(self):
        assert "牛奶" in _extract_title("这批牛奶降价到 9.9")

    def test_truncate(self):
        assert len(_extract_title("上架 " + "长" * 60 + " 19.9元")) <= 40


class TestExecuteCommand:
    """编排器: 不依赖真实 DB, 用假 pool 验证参数组装与链路。"""

    def test_list_product_needs_pool(self):
        """上架链路缺 pool 时返回 failed 而非崩溃。"""
        import asyncio
        import tempfile
        from pathlib import Path
        from app.ext.agent_gateway import execute_command

        async def run():
            with tempfile.TemporaryDirectory() as td:
                return await execute_command(
                    "上架 测试橙子 8.8元 20件",
                    pool=None,
                    out_root=Path(td),
                    principal={"user_id": "test-user"},
                )

        res = asyncio.run(run())
        # 无 DB 时发布步骤明确报错, 而不是崩溃
        assert res["status"] == "failed"
        err = str(res.get("error", {}))
        assert "pool is required" in err

    def test_adjust_price_without_amount_hints(self):
        import asyncio
        import tempfile
        from pathlib import Path
        from app.ext.agent_gateway import execute_command

        async def run():
            with tempfile.TemporaryDirectory() as td:
                return await execute_command(
                    "丹东草莓降价",
                    pool=None,
                    out_root=Path(td),
                    principal={"user_id": "test-user"},
                )

        res = asyncio.run(run())
        assert res["status"] == "needs_input"

    def test_rma_needs_structured_input(self):
        import asyncio
        import tempfile
        from pathlib import Path
        from app.ext.agent_gateway import execute_command

        async def run():
            with tempfile.TemporaryDirectory() as td:
                return await execute_command(
                    "给用户补天赔付",
                    pool=None,
                    out_root=Path(td),
                    principal={"user_id": "test-user"},
                )

        res = asyncio.run(run())
        assert res["status"] == "needs_input"
        assert "rma" in res["hint"]

    def test_unrouted(self):
        import asyncio
        import tempfile
        from pathlib import Path
        from app.ext.agent_gateway import execute_command

        async def run():
            with tempfile.TemporaryDirectory() as td:
                return await execute_command(
                    "今天天气怎么样",
                    pool=None,
                    out_root=Path(td),
                    principal={"user_id": "test-user"},
                )

        res = asyncio.run(run())
        assert res["status"] == "unrouted"


class TestCleanFilename:
    def test_video_name(self):
        assert _clean_filename("丹东草莓实拍.mp4") == "丹东草莓实拍"

    def test_strip_trailing_digits(self):
        assert _clean_filename("apple_20260803.mp4") == "apple"

    def test_underscore_to_space(self):
        assert _clean_filename("fresh_eggs_30.jpg") == "fresh eggs"

    def test_empty_fallback(self):
        assert _clean_filename("IMG_001.mp4") == "IMG"

    def test_truncate_long(self):
        assert len(_clean_filename("非常非常非常非常非常非常非常非常长的商品名称.mp4")) <= 40


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
