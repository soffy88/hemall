"""仓库内 app/tests 之外的 Python 脚本必须语法正确、可编译 (audit P1-1)。

`scripts/chaos_test_doomsday.py` 曾经在 ``main()`` 里先读 ``BASE_URL`` 再写
``global BASE_URL``，整个文件根本无法编译 (SyntaxError: name 'BASE_URL' is used
prior to global declaration) —— lint 只报行长度和 import 问题，没人发现它其实
一运行就崩。这里用编译期锁住：任何一个仓库脚本出现语法错误，这个测试就红。

同时确认这些脚本不是空壳（至少有函数/类定义或模块级配置），防止"删掉内容也能
过编译"的假绿。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 仓库内被 CI 的 `ruff check .` / `ruff format --check .` 覆盖、但没有被
# app/tests 测试间接碰到的脚本目录。
SCRIPT_DIRS = [
    ROOT / "scripts",
    ROOT / "edge_bridge",
]

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _python_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for directory in SCRIPT_DIRS:
        if not directory.is_dir():
            continue
        files.extend(sorted(directory.rglob("*.py")))
    return files


@pytest.mark.parametrize(
    "path",
    [pytest.param(p, id=str(p.relative_to(ROOT))) for p in _python_files()],
)
def test_script_compiles(path: pathlib.Path) -> None:
    """每个脚本都要能通过 compile() —— 语法错误在此拦截。"""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    compile(tree, str(path), "exec")
    definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert definitions, f"{path.relative_to(ROOT)} 里没有任何函数/类定义（空壳脚本?）"


def test_gunicorn_conf_compiles() -> None:
    """gunicorn.conf.py 是部署侧唯一入口，语法与配置项都必须存在。"""
    path = ROOT / "gunicorn.conf.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    compile(tree, str(path), "exec")
    assert any(isinstance(node, ast.Assign) for node in tree.body), (
        "gunicorn.conf.py 没有任何配置项"
    )


def test_scripts_expose_main_guard() -> None:
    """可作为 `python -m` / 直接执行的脚本必须有 ``if __name__ == "__main__"`` 守卫。

    seed/benchmark/chaos 这三个脚本都是"直接运行"型，缺了守卫时 import 会
    触发副作用（连生产库 / 打满请求）。
    """
    expected = {
        "scripts/seed_grocery_catalog.py",
        "scripts/benchmark.py",
        "scripts/chaos_test_doomsday.py",
    }
    for rel in sorted(expected):
        path = ROOT / rel
        assert path.exists(), f"{rel} 不存在"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        has_guard = any(
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            for node in tree.body
        )
        assert has_guard, f"{rel} 缺少 if __name__ == '__main__' 守卫"
