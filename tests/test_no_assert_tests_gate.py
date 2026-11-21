"""门禁：仓库里不许再出现"零断言测试"（audit P2-1）。

审计统计到 13 个测试函数一条 assert / pytest.raises / mock assert 都没有，
其中 tests/test_phase1_realtime.py 的 test_websocket_requires_token 更是
`TestClient(app=None)` 走个过场——名字承诺验证 WS 鉴权，实际什么都没做。
最坏的一类：把被测代码整段删掉它照样绿。

本文件把 AST 扫描固化成测试，防止新增。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _has_verification(node: ast.AST) -> bool:
    """函数体里是否有真正的验证动作。"""
    for n in ast.walk(node):
        if isinstance(n, ast.Assert):
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            # pytest.raises / pytest.warns / unittest mock 的 assert_* 系列
            if n.func.attr in ("raises", "warns") or n.func.attr.startswith("assert"):
                return True
    return False


def _assert_less_tests() -> list[str]:
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            if not _has_verification(node):
                offenders.append(f"{path.name}:{node.lineno} {node.name}")
    return offenders


def test_no_assert_less_tests_in_test_suite():
    offenders = _assert_less_tests()
    assert offenders == [], "这些测试没有任何断言，把被测代码删掉它们也会绿:\n  - " + "\n  - ".join(
        offenders
    )


def test_no_op_websocket_token_test_is_not_reintroduced():
    """曾经的 no-op: `TestClient(app=None)` 走个过场。

    用 AST 判定而不是全文搜索——说明文字里提到这个写法是合理的，只要代码里
    真的没有这么调用。
    """
    source = (TESTS_DIR / "test_phase1_realtime.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "TestClient":
            continue
        for kw in node.keywords:
            if kw.arg == "app" and isinstance(kw.value, ast.Constant) and kw.value.value is None:
                pytest.fail(f"line {node.lineno}: 又出现了 TestClient(app=None) no-op")

    # 真实的 WS 鉴权覆盖：必须真的断言 4001 关闭码
    assert "4001" in source, "WS 鉴权测试必须真的断言 4001 关闭码"
