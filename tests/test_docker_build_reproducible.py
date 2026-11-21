"""P2-1: 移除 platform3o 死拷贝带来的构建硬依赖。

审计发现：`Dockerfile.prod` 中 10 行 `COPY --from=platform3o /<pkg> /platform/3O/<pkg>`
配合 compose `build.additional_contexts: {platform3o: ../platform/3O}`，
让"构建必须存在 sibling 目录 ../platform/3O"成为硬依赖。

但 `pyproject.toml [tool.uv.sources]` 已把 obase/oprim/oskill/omodul/oservi
全部钉成**固定 git rev**，相对路径 sources 根本不存在 —— 这些 COPY 是死拷贝。
注释宣称的"使相对路径 sources 生效"与 pyproject 自身的注释直接矛盾。

本测试保证该硬依赖不会回归。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

COMPOSE_FILES = sorted(p for p in REPO_ROOT.glob("docker-compose*.yml") if p.is_file())
DEPLOY_SCRIPTS = sorted(p for p in (REPO_ROOT / "scripts").glob("*.sh") if p.is_file())

P3O_PACKAGES = ("obase", "oprim", "oskill", "omodul", "oservi")


def test_dockerfile_prod_has_no_platform3o_copy():
    text = (REPO_ROOT / "Dockerfile.prod").read_text(encoding="utf-8")
    offenders = [
        ln for ln in text.splitlines() if "platform3o" in ln and not ln.strip().startswith("#")
    ]
    assert not offenders, (
        "Dockerfile.prod 仍有 platform3o 构建指令（死拷贝，且使 ../platform/3O "
        f"成为构建硬依赖）: {offenders}"
    )


@pytest.mark.parametrize("compose", COMPOSE_FILES, ids=lambda p: p.name)
def test_compose_has_no_additional_contexts(compose: Path):
    data = yaml.safe_load(compose.read_text(encoding="utf-8")) or {}
    services = data.get("services") or {}
    offenders = [
        name
        for name, svc in services.items()
        if isinstance(svc, dict) and (svc.get("build") or {}).get("additional_contexts")
    ]
    assert not offenders, f"{compose.name} 服务的 build 仍配了 additional_contexts: {offenders}"


@pytest.mark.parametrize("script", DEPLOY_SCRIPTS, ids=lambda p: p.name)
def test_deploy_scripts_pass_no_platform3o_build_context(script: Path):
    text = script.read_text(encoding="utf-8")
    offenders = [ln for ln in text.splitlines() if "--build-context" in ln and "platform3o" in ln]
    assert not offenders, f"{script.name} 仍传 --build-context platform3o: {offenders}"


def test_pyproject_sources_are_git_pinned():
    """前置事实：sources 已是固定 git rev，所以本地死拷贝确实无用。"""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^\[tool\.uv\.sources\]$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    assert block, "pyproject.toml 缺少 [tool.uv.sources] 段"
    body = block.group(1)
    for pkg in P3O_PACKAGES:
        line = next((ln for ln in body.splitlines() if ln.strip().startswith(pkg)), None)
        assert line is not None, f"{pkg} 未在 [tool.uv.sources] 中声明"
        assert "git = " in line and "rev = " in line, (
            f"{pkg} 的 source 未钉死 git rev，无法移除本地 platform3o 死拷贝: {line!r}"
        )
        assert "path = " not in line, f"{pkg} 仍是相对路径 source: {line!r}"
