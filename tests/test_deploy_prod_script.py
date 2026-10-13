"""P1-4: `scripts/deploy-prod.sh` 的健康检查必然失败 + 迁移步骤是假的。

三个叠加问题让一键生产部署**根本跑不完**：

1. **健康检查打在一个不存在的端口上**。脚本轮询
   `http://localhost:8000/health/live`，但 `docker-compose.prod.yml` 里
   `api` 服务**没有 ports**（注释写明"只经 nginx 暴露，不要把 worker 端口
   发布到宿主"）。端口 80/443 是 nginx 的。于是重试 30×3s 后必然
   `error "部署失败"` —— `set -e` 之下脚本死在第 4 步。

2. **"执行数据库 Schema 迁移"这一步是假的**。脚本执行

       docker compose exec -T api bash -c 'uv run python -c "
   from app.ext.schema import apply_schema_migrations; ..."' || warn "..."

   三重失效：最终镜像只在 builder 阶段装了 uv，运行时容器里**没有 uv**；
   `apply_schema_migrations` 在 `app/` 中**根本不存在**（真实入口是
   `app/bootstrap.py:init_db()`，由 lifespan 幂等执行 + advisory lock 串行化）；
   而 `|| warn` 把失败静默吞掉 —— 部署脚本报告成功，schema 却从未被检查过。

3. 收尾打印 `Grafana (admin/admin)`，但 compose 强制
   `GF_SECURITY_ADMIN_PASSWORD`，这段输出是错的；横幅还自称 "ClearNode v1.0.0"、
   硬编码早已过期的 tag `v1.0.0-ghost-ignition`。

本测试不靠读脚本文本猜测，**真跑脚本**：把假的 docker / git / uv 放到 PATH
前面，让脚本真的去调它们，从而控制"健康检查是否通过""schema 断言是否通过"，
验证脚本在这些输入下的退出码与输出。
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_PROD = REPO_ROOT / "scripts" / "deploy-prod.sh"
PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"

requires_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")


def _script_schema_tables() -> list[str]:
    """解析脚本里 SCHEMA_TABLES 数组 —— fake 工具要按真实表名回显。"""
    script = DEPLOY_PROD.read_text(encoding="utf-8")
    m = re.search(r"^SCHEMA_TABLES=\(([^)]*)\)", script, re.M)
    assert m, "脚本应声明 SCHEMA_TABLES（schema 断言要查的表）"
    return m.group(1).split()


SCHEMA_TABLES_FOR_TEST = _script_schema_tables()


def _compose() -> dict:
    return yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8")) or {}


# --------------------------------------------------------------------------
# 静态契约
# --------------------------------------------------------------------------


def _code_lines(script: str) -> list[tuple[int, str]]:
    """返回 (行号, 去掉注释的代码行)。

    脚本头部和步骤说明里会大量出现历史问题描述（`apply_schema_migrations`、
    "`|| warn` 吞掉"、"容器内 curl localhost:8000"）。那些是**解释**，不是
    **行为**。断言行为时必须先剥掉注释，否则会被自己的说明文字误伤 ——
    这类测试一旦误报，第一次就会被改成"把注释删掉"，防回归能力归零。
    """
    out: list[tuple[int, str]] = []
    in_heredoc = False
    for i, raw in enumerate(script.splitlines(), start=1):
        if raw.strip() == "PY" and in_heredoc is False:
            in_heredoc = True
            continue
        if raw.strip() == "PY" and in_heredoc:
            in_heredoc = False
            continue
        if in_heredoc:
            out.append((i, raw))
            continue
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append((i, raw))
    return out


def _script() -> str:
    return DEPLOY_PROD.read_text(encoding="utf-8")


def _echo_lines(script: str) -> str:
    """只取脚本**会打印**的内容（echo / printf / log / warn / error 的参数）。

    注释里为了解释历史问题会提到旧品牌名、旧口令名，那不算"脚本在打印
    它们"。断言输出内容时必须区分这两者，否则会被自己的注释误伤。
    """
    out: list[str] = []
    for ln in script.splitlines():
        stripped = ln.strip()
        if stripped.startswith("#"):
            continue
        if re.match(r"^(echo|printf|log|warn|error|step)\b", stripped) or re.search(r'"[^"]*\$\{?[A-Za-z_]', stripped):
            out.append(stripped)
    return "\n".join(out)


def test_api_port_is_not_published_so_health_check_must_not_use_it():
    """脚本不能把健康检查打在**宿主机**的 api 端口上。

    compose 刻意不发布 api 端口（只经 nginx 暴露）。脚本探
    `localhost:8000` 等于探一个永远没人监听的口 —— 必然超时失败。
    容器**内部**的 `exec -T api curl localhost:8000` 则是合法的，由下面
    两条测试分别约束。
    """
    assert not (_compose()["services"]["api"].get("ports")), (
        "前提变了：api 现在发布了端口。若确实如此，请同步更新本测试与脚本的探活方式"
    )
    host_probes = [
        f"{i}: {ln.strip()}"
        for i, ln in _code_lines(_script())
        if re.search(r"curl\b.*localhost:8000", ln) and "exec -T api" not in ln
    ]
    assert not host_probes, (
        "脚本在**宿主机**上探 localhost:8000 —— prod compose 不发布 api 端口，"
        f"该检查必然失败（audit P1-4）:\n" + "\n".join(host_probes)
    )


def test_health_check_goes_through_nginx_or_into_the_container():
    """健康检查必须走 nginx (80/443) 或进容器内打 localhost:8000。"""
    script = DEPLOY_PROD.read_text(encoding="utf-8")
    in_container = re.search(r"exec -T api curl[^\n]*localhost:8000/health/live", script)
    via_nginx = re.search(r"curl[^\n]*\$(?:\{)?API_SERVER_NAME|\$(?:\{)?API_SERVER_NAME[^\n]*https", script) or re.search(
        r"https://\$\{?API_SERVER_NAME", script
    )
    assert in_container or via_nginx, (
        "健康检查必须走 nginx 入口或进容器内打 8000；两者都没有说明还在探宿主机的 8000"
    )
    assert in_container, "至少要有一个容器内直连探活（权威判据：应用进程本身）"


def test_no_fake_migration_and_no_swallowed_failure():
    """假的迁移步骤要删掉，真实断言失败必须让部署失败。"""
    code = _code_lines(_script())
    assert all("apply_schema_migrations" not in ln for _, ln in code), (
        "apply_schema_migrations 在代码库中不存在；真实的 schema 入口是 "
        "app/bootstrap.py:init_db()（lifespan 幂等执行 + advisory lock）"
    )
    # 任何一步都不能用 `|| warn` / `|| true` 吞掉真实失败（原缺陷的核心）
    swallowing = [
        f"{i}: {ln.strip()}" for i, ln in code if re.search(r"\|\|\s*(warn|true)\b", ln)
    ]
    assert not swallowing, f"部署步骤的失败被静默吞掉: {swallowing}"


def test_schema_step_asserts_real_state():
    """替换掉的迁移步骤必须是**真断言**：调用真实函数并校验结果。"""
    script = _script()
    assert "init_db" in script, "schema 步骤应调用真实的 app.bootstrap.init_db（幂等 + advisory lock）"
    assert re.search(r"(to_regclass|information_schema|pg_tables)", script), (
        "schema 步骤必须真的查库确认表存在，而不是只跑一个函数就宣布成功"
    )


def test_no_stale_branding_or_credentials_in_output():
    """收尾输出不能自称 ClearNode，也不能打印与实际不符的默认口令。

    只检查脚本**会打印**的内容 —— 注释里为了说明历史问题提到这些词是允许的。
    """
    printed = _echo_lines(_script())
    assert "ClearNode" not in printed, "脚本横幅还自称 ClearNode v1.0.0（本项目是 Hemall）"
    assert "admin/admin" not in printed, (
        "不能打印 Grafana admin/admin —— prod compose 强制自定义 "
        "GF_SECURITY_ADMIN_PASSWORD，这行输出会让运维去用一个不存在的口令"
    )
    assert "ghost-ignition" not in printed, "硬编码的 tag 已过期（仓库早已到 v1.2.1）"


def test_tag_is_not_hardcoded_to_a_stale_value():
    """tag 应当可由调用方传入/环境变量覆盖，而不是钉死一个过期值。"""
    script = _script()
    m = re.search(r'^TAG="([^"]+)"', script, re.M)
    assert m, "脚本应有一个 TAG 变量"
    literal = m.group(1)
    assert "ghost-ignition" not in literal
    # 允许 `${VAR:-default}` 形式（可覆盖），不接受纯字面量
    assert "$" in literal, f"TAG={literal!r} 被硬编码，发布新版本时改不动；应允许环境变量/参数覆盖"


def test_script_uses_strict_shell_mode():
    assert "set -euo pipefail" in _script()


# --------------------------------------------------------------------------
# 行为: 真跑脚本
# --------------------------------------------------------------------------


def _fake_bin(tmp_path: Path, *, healthy: bool, schema_ok: bool) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()

    def write(name: str, body: str) -> None:
        p = bindir / name
        p.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    health_rc = "0" if healthy else "7"
    tables = str(len(SCHEMA_TABLES_FOR_TEST)) if schema_ok else "3"
    # 模拟 `docker compose exec -T api curl -sf .../health/live`
    write(
        "docker",
        # shellcheck disable=SC2016
        f"""
if [ "${{HEMALL_FAKE_LOG}}-" ]; then echo "$*" >> "${{HEMALL_FAKE_LOG}}"; fi
case "$*" in
  *exec*api*curl*health/live*) exit {health_rc} ;;
esac
case "$*" in
  *exec*api*) exit 0 ;;
esac
# postgres 里的 psql 查表断言
case "$*" in
  *exec*postgres*psql*) printf '%s\\n' '{tables}'; exit 0 ;;
esac
exit 0
""",
    )
    write("git", 'case "$*" in *"rev-parse --abbrev-ref"*) echo main ;; esac\nexit 0')
    write("uv", "exit 0")
    return bindir


def _run(tmp_path: Path, *, healthy: bool, schema_ok: bool, tag: str = "v1.2.1") -> tuple[int, str, str]:
    bindir = _fake_bin(tmp_path, healthy=healthy, schema_ok=schema_ok)
    log = tmp_path / "calls.log"
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["HEMALL_FAKE_LOG"] = str(log)
    env["HEMALL_DEPLOY_TAG"] = tag
    # 探活重试参数由脚本暴露为环境变量, 这里调小, 免得失败用例每次等 90 秒
    env["HEMALL_DEPLOY_HEALTH_RETRIES"] = "2"
    env["HEMALL_DEPLOY_HEALTH_INTERVAL"] = "0"
    proc = subprocess.run(
        ["bash", str(DEPLOY_PROD)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=300,
    )
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return proc.returncode, proc.stdout + proc.stderr, calls


@requires_bash
def test_unhealthy_api_must_fail_the_deployment(tmp_path):
    """API 起不来时脚本必须非零退出，绝不能打印"部署成功"。"""
    rc, out, _ = _run(tmp_path, healthy=False, schema_ok=True)
    assert rc != 0, f"API 一直不健康却返回成功:\n{out}"
    assert "部署成功" not in out, f"API 不健康却宣告部署成功:\n{out}"


@requires_bash
def test_healthy_api_and_passing_schema_succeeds(tmp_path):
    rc, out, calls = _run(tmp_path, healthy=True, schema_ok=True)
    assert rc == 0, f"期望部署成功:\n{out}"
    assert "exec -T api python" in calls, f"schema bootstrap 步骤根本没被执行:\n{calls}"
    assert "pg_tables" in calls, f"schema 表存在性断言根本没被执行:\n{calls}"


@requires_bash
def test_failing_schema_assertion_must_abort_the_deployment(tmp_path):
    """schema 断言失败必须让部署失败 —— 原来被 `|| warn` 吞掉了。"""
    rc, out, _ = _run(tmp_path, healthy=True, schema_ok=False)
    assert rc != 0, f"schema 断言失败却仍宣告部署成功:\n{out}"
    assert "部署成功" not in out


@requires_bash
def test_health_probe_never_targets_the_unpublished_api_port(tmp_path):
    """行为层面再钉一次：脚本发出的所有探活请求都不得指向 8000。"""
    _, _, calls = _run(tmp_path, healthy=True, schema_ok=True)
    health_calls = [ln for ln in calls.splitlines() if "health/live" in ln or "health/ready" in ln]
    assert health_calls, f"脚本没有发出任何探活请求:\n{calls}"
    for call in health_calls:
        # 容器内 curl localhost:8000 是允许的（那是在 api 容器内部），
        # 被禁止的是在宿主机上探 8000 —— 那种调用不会带 exec -T api。
        assert "exec -T api" in call or re.search(r":(80|443)\b", call), (
            f"探活请求既不在容器内也不走 nginx 80/443: {call}"
        )


@requires_bash
def test_tag_defaults_to_the_real_chart_app_version():
    """不设 HEMALL_DEPLOY_TAG 时，tag 必须真的取到 Chart.yaml 的 appVersion。

    提取规则写在脚本里（sed/tr），单测跑一遍真实的 Chart.yaml 才能确认它
    不是"看起来对、实际返回空串" —— 返回空串会让发布出去的 tag 变成空。
    """
    script = _script()
    lines = script.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith("_chart_appversion() {"))
    except StopIteration:  # pragma: no cover
        pytest.fail("脚本应有 _chart_appversion() 负责从 Chart.yaml 取 appVersion")
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    definition = "\n".join(lines[start : end + 1])
    assert lines[start + 1].strip(), "_chart_appversion() 函数体是空的"

    proc = subprocess.run(
        ["bash", "-c", f"{definition}\n_chart_appversion"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert proc.returncode == 0, f"提取 appVersion 失败:\n{proc.stderr}"
    got = proc.stdout.strip()
    expected = str((yaml.safe_load((REPO_ROOT / "charts" / "hemal" / "Chart.yaml").read_text(encoding="utf-8")) or {})["appVersion"])
    assert got == expected, f"取到 {got!r}，Chart.yaml 里是 {expected!r}"
    assert got, "appVersion 提取结果为空 —— 发布 tag 会变成空字符串"


def test_declared_tag_is_overridable_by_env():
    """tag 必须能被 HEMALL_DEPLOY_TAG 覆盖，否则发版仍要改脚本。"""
    assert "HEMALL_DEPLOY_TAG" in _script()
