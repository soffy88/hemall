"""P1-3: `scripts/deploy.sh` 的 tag 键写错 → 新版本永远没上线，还报成功。

真实的键路径是 `api.image.tag`（charts/hemal/values.yaml），脚本写的是
`--set image.tag=$tag`。Helm 对未知键只**告警并忽略**，不报错；紧随其后的
`kubectl rollout status` 又因为 Deployment spec 没变化而"立即成功"。两层
假信号叠加成**假上线**：脚本打印"部署成功"，集群里跑的还是旧版本。

这里不靠读脚本文本来断言，而是**真跑**脚本：把假的 `helm` / `kubectl` /
`docker` 放到 PATH 前面，脚本会真的去调它们。fake 工具会记录收到的参数，
并按测试给的镜像回显 —— 于是"请求的 tag"与"实际运行的镜像"可以被独立控制，
模拟集群跑错版本的现场。
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
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
CHART_VALUES = REPO_ROOT / "charts" / "hemal" / "values.yaml"

_TAG_PATH = "api.image.tag"


def _fake_bin(tmp_path: Path, *, cluster_image: str) -> Path:
    """造一套 fake helm/kubectl/docker，并把调用记录到 args.log。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "args.log"

    def write(name: str, body: str) -> None:
        p = bindir / name
        p.write_text(f"#!/bin/sh\necho \"{name} $*\" >> {log}\n{body}\n", encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # helm: 记录参数后成功返回
    write("helm", 'echo "$*" | grep -q -- "--namespace" || exit 0\nexit 0')
    # kubectl: 只有 `get deploy ... -o jsonpath=...` 返回集群里真实的镜像，
    # 其余子命令一律成功 (rollout status / wait / create 都过)。
    write(
        "kubectl",
        "case \"$*\" in\n"
        f"  *jsonpath*) printf '%s' '{cluster_image}' ;;\n"
        "esac\nexit 0",
    )
    write("docker", "exit 0")
    # git 也被 check_prerequisites 检查
    write("git", "exit 0")
    return bindir


def _run_deploy(tmp_path: Path, cluster_image: str, *args: str) -> tuple[int, str, str]:
    bindir = _fake_bin(tmp_path, cluster_image=cluster_image)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    proc = subprocess.run(
        ["bash", str(DEPLOY_SH), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=180,
    )
    calls = (tmp_path / "args.log").read_text(encoding="utf-8") if (tmp_path / "args.log").exists() else ""
    return proc.returncode, proc.stdout + proc.stderr, calls


def test_deploy_script_exists_and_is_executable():
    assert DEPLOY_SH.is_file()
    assert os.access(DEPLOY_SH, os.X_OK), "scripts/deploy.sh 必须可执行"


# --------------------------------------------------------------------------
# 静态契约: --set 的键必须和 chart 真实值路径一致
# --------------------------------------------------------------------------


def test_helm_set_key_matches_the_real_values_path():
    """`--set` 的键必须能在 chart 的 values 里找到对应层级。

    这是本次缺陷的根因：键写错时 Helm 静默忽略。用真实 values.yaml 反查
    值路径，再确认脚本里 `IMAGE_TAG_KEY` 变量正是这条路径 —— 不硬编码一份
    键名清单。真正"键有没有真的传出去"由下面的行为测试用 fake helm 断言。
    """
    script = DEPLOY_SH.read_text(encoding="utf-8")

    m = re.search(r'^IMAGE_TAG_KEY="([^"]+)"', script, re.M)
    assert m, "脚本应把镜像 tag 的值路径集中到一个变量上，避免散落的字面量"
    key = m.group(1)
    assert key == _TAG_PATH, f"IMAGE_TAG_KEY={key!r}，chart 里的真实路径是 {_TAG_PATH!r}"

    assert re.search(r'--set\s+"\$\{IMAGE_TAG_KEY\}', script), "helm 命令必须引用该变量"

    # 键路径必须真实存在于 values（父层是映射，叶子是 tag 的值）
    node: object = yaml.safe_load(CHART_VALUES.read_text(encoding="utf-8"))
    parts = key.split(".")
    for part in parts[:-1]:
        assert isinstance(node, dict) and part in node, f"values.yaml 里没有 {key} 这一层"
        node = node[part]
    assert isinstance(node, dict) and parts[-1] in node, (
        f"values.yaml 里 {key} 不是已声明的键 —— --set 写进去也没地方可覆盖"
    )

    assert "--set image.tag=" not in script, "旧的无前缀键 `--set image.tag` 又回来了"


def test_deploy_script_fails_on_unknown_helm_set_keys():
    """脚本必须让 Helm 对未知键硬失败，而不是打一行 warning 继续。

    `helm upgrade --strict` 会对未知 `--set` 键直接报错。少了这个开关，
    任何拼错的键都会退化成"部署成功但没生效"。
    """
    script = DEPLOY_SH.read_text(encoding="utf-8")
    assert "--strict" in script, "helm 命令必须带 --strict，让拼错的 --set 键硬失败而不是静默忽略"


def test_deploy_script_requires_the_external_secret():
    """chart 的 secret.yaml 对空 existingSecret 直接 fail，脚本必须提供。

    否则 `bash scripts/deploy.sh production` 在 helm 那一步就失败，
    部署根本没开始。
    """
    script = DEPLOY_SH.read_text(encoding="utf-8")
    assert "secrets.existingSecret" in script, (
        "脚本必须给 chart 传 secrets.existingSecret —— templates/secret.yaml 对空值 fail-closed"
    )


# --------------------------------------------------------------------------
# 行为: 真跑脚本，验证"假上线"被堵死
# --------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")
def test_correct_tag_passes_and_reaches_the_deployment(tmp_path):
    """请求的 tag 与集群里实际运行的镜像一致 → 成功，且 helm 收到正确的键。"""
    rc, out, calls = _run_deploy(tmp_path, "registry.example.com/hemall/backend:v1.2.3", "production", "v1.2.3")

    assert "--set api.image.tag=v1.2.3" in calls, f"helm 收到的参数不对: {calls}"
    assert "--set image.tag=" not in calls
    assert rc == 0, f"期望部署成功:\n{out}"
    assert "部署成功" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")
def test_cluster_running_another_tag_fails_loudly_instead_of_claiming_success(tmp_path):
    """集群里跑的不是请求的 tag → 必须非零退出，且不得打印"部署成功"。

    这就是原缺陷的现场：`--set image.tag` 被忽略，Deployment spec 没变，
    rollout 立即"成功"，脚本报告部署完成，而线上仍是旧版本。
    """
    rc, out, calls = _run_deploy(
        tmp_path,
        "registry.example.com/hemall/backend:0.3.0",  # 集群还跑着旧版本
        "production",
        "v1.2.3",
    )

    assert rc != 0, f"实际运行的镜像与请求不符却返回成功 —— 假上线:\n{out}"
    assert "部署成功" not in out, f"校验失败却仍宣告成功:\n{out}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")
def test_failed_verification_is_not_swallowed(tmp_path):
    """校验失败必须让脚本以非零码退出，且不能被 `|| true` / `|| warn` 吞掉。"""
    rc, out, _ = _run_deploy(
        tmp_path, "registry.example.com/hemall/backend:0.3.0", "production", "v1.2.3"
    )
    assert rc != 0
    assert out.strip(), "失败时必须有可操作的说明，不能静默退出"


def test_health_check_actually_asserts_something():
    """`run_health_check` 不能只是打印 kubectl get —— 那不是检查。

    审计记录："只 kubectl get pods/svc/deployments（无断言）"。至少要有
    就绪副本数、Ingress 后端服务名这类能失败的断言。
    """
    script = DEPLOY_SH.read_text(encoding="utf-8")
    body = script[script.index("run_health_check()") : script.index("# 主部署函数")]
    asserts = [
        ln
        for ln in body.splitlines()
        if ln.strip().startswith(("[ ", "if ", "test ", "[[")) or "!=" in ln
    ]
    assert asserts, "run_health_check 里没有任何断言，只是打印状态"
    assert "kubectl get" in body, "健康检查应当仍会展示集群状态"


def test_script_uses_strict_shell_mode_and_quotes():
    """`set -euo pipefail` + 变量引用加引号，否则 unset 变量/管道失败会静默继续。"""
    script = DEPLOY_SH.read_text(encoding="utf-8")
    assert "set -euo pipefail" in script, "deploy.sh 必须开 set -euo pipefail"
    assert 'set -e  #' not in script, "旧的 `set -e`（无 -u/-o pipefail）还在"

    unquoted = [
        f"{i}: {ln.strip()}"
        for i, ln in enumerate(script.splitlines(), start=1)
        if any(tok in ln for tok in ("$namespace", "$tag", "$values_file", "$environment"))
        and not ln.strip().startswith("#")
        and '"${' not in ln
        and "$@" not in ln
        and ("$namespace" in ln or "$tag" in ln or "$values_file" in ln or "$environment" in ln)
    ]
    assert not unquoted, f"变量引用未加引号:\n" + "\n".join(unquoted)


def test_help_text_does_not_advertise_latest():
    """P2-8 之后 chart 已钉死 tag，帮助文本还教人传 latest 是自相矛盾的。

    只查**示例命令行**里有没有把 latest 当参数传；正文里"不要传 latest"
    这类告诫是允许的（甚至鼓励的）。
    """
    script = DEPLOY_SH.read_text(encoding="utf-8")
    help_text = script[script.index("show_help()") : script.index("# 参数处理")]

    examples = re.findall(r'\$0\s+([^\n"]*)', help_text)
    assert examples, "帮助文本里应当有示例命令行"
    for args in examples:
        assert not re.search(r"(^|\s)latest(\s|$)", args), (
            f"帮助示例仍在传 latest: `$0 {args}` —— chart 已钉死 tag，浮动 tag 无法复现也无法回滚"
        )
