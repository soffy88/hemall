"""Helm chart 渲染契约: 渲染结果必须是能真跑起来的部署。

审计发现 (charts/hemal):

- **P1-2** `templates/ingress.yaml` 的后端 Service 名写成 `{{ .Release.Name }}-api-service`，
  而 `templates/service.yaml` 建的实名是 `{{ .Release.Name }}-api` → ingress-nginx
  上游解析失败，`helm install` 后**所有过 Ingress 的流量 503**，而 Helm 自身
  不会报错 (`helm template` 照样渲染成功)。
- **P1-1** pod `runAsUser: 1000` / `runAsGroup: 3000`，与镜像里 `hemall` 用户不符；
  `gunicorn.conf.py` 的 `user = "hemall"` 会让 master 对非 root worker 调
  `setgid/setuid` → EPERM → CrashLoopBackOff。同时 `command: ["gunicorn"]`
  覆盖了镜像 ENTRYPOINT，entrypoint.sh 的数据卷属主修复被跳过。
- **P2-5** NetworkPolicy 默认开启但 egress 不全 (无同 ns、无 Jaeger OTLP 4317)，
  DB/Redis/tracing 会被静默丢弃，只在 lifespan 打 warning。
- **P2-6** `api.env` 里的 5 个键没有 `HEMALL_` 前缀，而 `Settings` 是
  `env_prefix="HEMALL_"` + `extra="ignore"` → **全部被静默忽略**；
  `monitoring.serviceMonitor/prometheusRule` 在 `templates/` 里没有对应文件。
- **P2-8** `values.mall-sxueji.yaml` 用 `tag: latest` + `pullPolicy: Always`
  → 部署不可复现、回滚无意义。

这些全是"helm template 能过、集群里必炸"的类型，静态 YAML 检查看不出，
所以这里的断言全部建立在 **真实 `helm template` 渲染产物** 上。
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "charts" / "hemal"

# 渲染时必须提供的最小 values：chart 的 secret.yaml 对空 existingSecret 直接
# fail（这是刻意设计的 fail-closed，见 templates/secret.yaml）。
_RENDER_SET = ["--set", "secrets.existingSecret=hemall-test-secret"]

VALUES_FILES = ["values.yaml", "values.staging.yaml", "values.production.yaml", "values.mall-sxueji.yaml"]


def _helm_binary() -> tuple[list[str], str] | None:
    """找到可用的 helm 调用方式: PATH 上的 helm, 否则退到 docker。

    返回 (命令前缀, chart 路径前缀) —— 走 docker 时仓库被挂在 /repo 下,
    chart 路径必须跟着换算, 否则 helm 报 "path not found"。
    """
    if shutil.which("helm"):
        return ["helm"], str(CHART_DIR)
    if shutil.which("docker"):
        probe = subprocess.run(
            ["docker", "image", "inspect", "alpine/helm:3.16.4"],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            return (
                ["docker", "run", "--rm", "-v", f"{REPO_ROOT}:/repo", "-w", "/repo", "alpine/helm:3.16.4"],
                "/repo/charts/hemal",
            )
    return None


_HELM = _helm_binary()


@functools.lru_cache(maxsize=None)
def helm_template(release: str, values_file: str | None = None) -> list[dict]:
    """渲染 chart 并返回 List[dict]。values_file 为 chart 目录下的文件名。"""
    if _HELM is None:
        pytest.skip("helm 不可用 (PATH 与 docker 都没有)")
    prefix, chart_path = _HELM
    cmd = [*prefix, "template", release, chart_path, *_RENDER_SET]
    if values_file:
        cmd += ["-f", f"{chart_path}/{values_file}"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"helm template 失败:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


@functools.lru_cache(maxsize=None)
def _rendered(values_file: str) -> dict:
    """把所有 values 变体渲染一次, 按 kind+name 索引, 供参数化断言复用。"""
    docs = helm_template("hemall", values_file)
    return {(d.get("kind"), d.get("metadata", {}).get("name")): d for d in docs}


def _of(values_file: str, kind: str, name: str) -> dict:
    index = _rendered(values_file)
    assert (kind, name) in index, (
        f"{values_file}: 渲染结果里没有 {kind}/{name}；实际有 "
        f"{sorted(k for k in index if k[0] == kind)}"
    )
    return index[(kind, name)]


ALL_VALUES = pytest.mark.parametrize("values_file", VALUES_FILES, ids=lambda v: v.replace("values", ""))


# --------------------------------------------------------------------------
# P1-2 — Ingress 后端必须指向真实存在的 Service
# --------------------------------------------------------------------------


@ALL_VALUES
def test_ingress_backend_service_exists(values_file: str):
    """Ingress 引用的后端 Service 名必须与 service.yaml 渲染出的实名一致。

    名字写错时 Helm 不会报错, 但 ingress-nginx 上游解析失败 → 全站 503。
    """
    ingress = _of(values_file, "Ingress", "hemall-api-ingress")
    service_names = {d["metadata"]["name"] for d in _rendered(values_file).values() if d.get("kind") == "Service"}

    referenced = {
        path["backend"]["service"]["name"]
        for rule in ingress["spec"].get("rules", [])
        for http in rule.get("http", {}).get("paths", [])
        for path in [http]
        if "backend" in path
    }
    assert referenced, "Ingress 没有任何 http backend, 流量无处可去"
    missing = referenced - service_names
    assert not missing, (
        f"{values_file}: Ingress 指向 chart 内不存在的 Service {sorted(missing)}; "
        f"实际渲染出的 Service 只有 {sorted(service_names)} → 过 Ingress 的流量全部 503"
    )


def test_ingress_service_name_is_not_the_stale_api_service():
    """回归钉子: 历史上 chart 用过 `<release>-api-service` 这个名字。

    只看 `name:` 这一行, 跳过注释 —— 注释里需要解释这个历史坑。
    """
    name_lines = [
        ln.split("#", 1)[0]
        for ln in (CHART_DIR / "templates" / "ingress.yaml").read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith("name:") and "Release.Name" in ln
    ]
    assert name_lines, "ingress.yaml 里没有带 Release.Name 的 Service 引用"
    for line in name_lines:
        assert "-api-service" not in line, f"ingress 又引回了不存在的 `{line.strip()}`"


# --------------------------------------------------------------------------
# P1-1 — pod securityContext 必须和镜像里的 hemall 用户对得上
# --------------------------------------------------------------------------


def _dockerfile_runtime_stage() -> str:
    """取 Dockerfile.prod 的运行阶段 (最后一个 FROM 之后)。"""
    text = (REPO_ROOT / "Dockerfile.prod").read_text(encoding="utf-8")
    stages = text.split("\nFROM ")
    return "\nFROM " + stages[-1] if len(stages) > 1 else text


def _declared_runtime_user() -> tuple[int, int] | None:
    """从 Dockerfile.prod 解析出 hemall 的 uid/gid; 必须是显式写死的。

    解析不出固定数字意味着 uid 取决于构建时已存在的系统用户 (Debian 的
    `useradd -r` 会挑一个空闲的 SYS_UID_MAX 以内的号), 换个基础镜像就变,
    Helm 里写死的 runAsUser 必然漂移 → CrashLoopBackOff。
    """
    stage = _dockerfile_runtime_stage()
    gid_match = __import__("re").search(r"groupadd[^\n]*?-g\s+(\d+)", stage)
    uid_match = __import__("re").search(r"useradd[^\n]*?-u\s+(\d+)", stage)
    if not (gid_match and uid_match):
        return None
    return int(uid_match.group(1)), int(gid_match.group(1))


@ALL_VALUES
def test_pod_runs_as_the_uid_that_exists_in_the_image(values_file: str):
    """runAsUser/runAsGroup 必须等于 Dockerfile.prod 里 hemall 的 uid/gid。"""
    declared = _declared_runtime_user()
    assert declared is not None, (
        "Dockerfile.prod 运行阶段必须用 `useradd -u <uid>` / `groupadd -g <gid>` "
        "把 hemall 的 uid/gid 写死, 否则 Helm 侧无从对齐"
    )
    uid, gid = declared

    pod_sc = _of(values_file, "Deployment", "hemall-api")["spec"]["template"]["spec"]["securityContext"]
    assert pod_sc.get("runAsNonRoot") is True, "runAsNonRoot 必须为 true (安全基线)"
    assert int(pod_sc["runAsUser"]) == uid, (
        f"{values_file}: runAsUser={pod_sc['runAsUser']} 但镜像里 hemall 是 uid={uid} → "
        f"gunicorn 对 worker 调 setuid(uid) 抛 EPERM → CrashLoopBackOff"
    )
    assert int(pod_sc["runAsGroup"]) == gid, (
        f"{values_file}: runAsGroup={pod_sc['runAsGroup']} 但镜像里 hemall 是 gid={gid}"
    )
    assert int(pod_sc["fsGroup"]) == gid, "fsGroup 必须与运行 gid 一致, 否则挂载卷不可写"


@ALL_VALUES
def test_container_does_not_override_image_entrypoint(values_file: str):
    """`command:` 会覆盖镜像 ENTRYPOINT, 让 entrypoint.sh 的属主修复被跳过。

    这里要求: Deployment 不覆盖 command; 若确实需要传 gunicorn 参数, 只能走
    `args:` (ENTRYPOINT 会被保留)。
    """
    container = _of(values_file, "Deployment", "hemall-api")["spec"]["template"]["spec"]["containers"][0]
    assert "command" not in container, (
        "Deployment 不能用 command: 覆盖镜像 ENTRYPOINT —— entrypoint.sh 要先修数据卷属主再 exec"
    )


def test_gunicorn_user_matches_the_image_user():
    """gunicorn.conf.py 的降权目标必须是镜像里真实存在的用户。"""
    text = (REPO_ROOT / "Dockerfile.prod").read_text(encoding="utf-8")
    assert "useradd" in text and "hemall" in text
    gunicorn = (REPO_ROOT / "gunicorn.conf.py").read_text(encoding="utf-8")
    assert 'user = "hemall"' in gunicorn and 'group = "hemall"' in gunicorn


def test_entrypoint_tolerates_already_unprivileged_start():
    """非 root 启动 (Helm 路径) 时 entrypoint 不能因为 chown 失败而退出。"""
    body = (REPO_ROOT / "deploy-tools" / "entrypoint.sh").read_text(encoding="utf-8")
    chown_lines = [ln for ln in body.splitlines() if ln.strip().startswith("chown")]
    assert chown_lines, "entrypoint 应当仍然负责修属主 (compose 以 root 启动时需要)"
    assert all("|| true" in ln for ln in chown_lines), (
        "非 root 启动时 chown 必然 EPERM; entrypoint 必须把 chown 当作 best-effort, "
        "否则 runAsNonRoot 的 pod 会在 chown 处直接退出"
    )


def _entrypoint_branches(body: str) -> tuple[list[str], list[str]]:
    """拆出 entrypoint.sh 的 (root 分支, 非 root 分支) 命令行。

    刻意只看**命令行**: 日志文案里出现 "chown" 这个词是合理的
    ("skipping chown (needs root)")，把它当成 chown 命令会误判。
    """
    lines = [ln.strip() for ln in body.splitlines()]
    start = next(i for i, ln in enumerate(lines) if ln == 'if [ "$(id -u)" = "0" ]; then')
    else_idx = next(i for i in range(start + 1, len(lines)) if lines[i] == "else")
    fi_idx = next(i for i in range(else_idx + 1, len(lines)) if lines[i] == "fi")
    root = [ln for ln in lines[start + 1 : else_idx] if ln]
    non_root = [ln for ln in lines[else_idx + 1 : fi_idx] if ln]
    return root, non_root


def test_entrypoint_reports_which_start_path_it_took():
    """两条启动路径必须各说各话，别再无脑打 "fixing data dir ownership"。

    非 root 路径下 chown 根本不会执行，日志却声称修了 —— 排查 K8s 写权限
    问题的人会被这句日志直接带偏。
    """
    body = (REPO_ROOT / "deploy-tools" / "entrypoint.sh").read_text(encoding="utf-8")
    root_branch, non_root_branch = _entrypoint_branches(body)

    root_chown = [ln for ln in root_branch if ln.startswith("chown ")]
    assert root_chown, "root 分支必须真的修数据卷属主 (compose 路径靠它)"

    non_root_chown = [ln for ln in non_root_branch if ln.startswith("chown ")]
    assert not non_root_chown, f"非 root 分支里不该有 chown 命令: {non_root_chown}"

    # 分支要各说各话, 否则排查 K8s 写权限时会被 root 路径的日志误导
    assert any("root" in ln for ln in root_branch if ln.startswith("echo"))
    assert any("uid" in ln for ln in non_root_branch if ln.startswith("echo"))


def test_uid_alignment_is_what_makes_gunicorn_deprivilege_a_noop():
    """钉死 P1-1 的失效机理: uid 对齐后 gunicorn 的降权调用才会变成 no-op。

    `gunicorn.util.set_owner_process` 是这么写的（无 root 检查）::

        elif gid != os.getgid():
            os.setgid(gid)
        if uid and uid != os.getuid():
            os.setuid(uid)

    容器以 uid 1000 启动时，两行都会执行 → 非 root 下 EPERM → master 退出 →
    CrashLoopBackOff。只有当容器 uid/gid **就是** hemall 的 uid/gid 时，
    两个分支都被短路，降权退化成 no-op。

    所以"Helm 里写什么 uid"不是风格问题，而是崩不崩的问题 —— 这里直接读
    gunicorn 的真实源码来确认这个短路条件还在，不靠记忆。
    """
    try:
        import inspect

        import gunicorn.util
    except ImportError:  # pragma: no cover - gunicorn 是 dev 依赖
        pytest.skip("gunicorn 未安装")

    src = inspect.getsource(gunicorn.util.set_owner_process)
    assert "os.setuid(uid)" in src, "gunicorn 换了实现, 本测试的前提需重新确认"
    assert "if uid and uid != os.getuid():" in src, (
        "gunicorn 不再按 uid 短路 setuid —— 若它变成无条件降权, 非 root 启动将再次崩溃"
    )
    assert "gid != os.getgid()" in src, "同上: setgid 也必须按 gid 短路"

    declared = _declared_runtime_user()
    assert declared is not None
    uid, gid = declared
    pod_sc = _of("values.yaml", "Deployment", "hemall-api")["spec"]["template"]["spec"]["securityContext"]
    assert int(pod_sc["runAsUser"]) == uid and int(pod_sc["runAsGroup"]) == gid, (
        "runAsUser/runAsGroup 必须等于 hemall 的 uid/gid, 两个 setX 才会短路成 no-op"
    )


# --------------------------------------------------------------------------
# P2-6 — 声明了等于没有: 无前缀的 env 键 + 渲染不出东西的 monitoring 开关
# --------------------------------------------------------------------------


def _settings_field_names() -> set[str]:
    """从**真实** Settings 类取字段名，而不是抄一份清单。

    Settings 是 `env_prefix="HEMALL_"` + `extra="ignore"`：环境变量名必须
    是 `HEMALL_<字段名>`，少前缀、少字段都**静默丢弃**，不报错、不告警。
    所以这里直接反射 Settings 模型，而不是维护一份会过期的名单。
    """
    from app.config import Settings

    return {name for name in Settings.model_fields}


def _deployment_env_names(values_file: str) -> list[str]:
    container = _of(values_file, "Deployment", "hemall-api")["spec"]["template"]["spec"]["containers"][0]
    return [e["name"] for e in container.get("env") or []]


@ALL_VALUES
def test_deployment_env_keys_are_real_hemall_settings(values_file: str):
    """Deployment 注入的每个 env 键都必须是 Settings 真实认识的键。

    无前缀的键 (`LOG_LEVEL`) 与不存在的键 (`DATABASE_POOL_SIZE`) 会被
    `extra="ignore"` 静默吞掉 —— 运维在 values 里改半天，容器里根本没生效，
    而且没有任何报错。
    """
    fields = _settings_field_names()
    # 这两个由 gunicorn.conf.py 自己读，不走 Settings
    gunicorn_managed = {"HEMALL_GUNICORN_WORKERS", "HEMALL_GUNICORN_TIMEOUT"}

    for name in _deployment_env_names(values_file):
        if name in gunicorn_managed:
            continue
        assert name.startswith("HEMALL_"), (
            f"{values_file}: env 键 {name!r} 没有 HEMALL_ 前缀 —— "
            f"Settings(env_prefix='HEMALL_', extra='ignore') 会静默丢弃它"
        )
        field = name[len("HEMALL_") :].lower()
        assert field in fields, (
            f"{values_file}: env 键 {name!r} 对应不到任何 Settings 字段 "
            f"(hemall.{field} 不存在) → 被静默忽略"
        )


def test_gunicorn_conf_actually_reads_the_injected_env():
    """GUNICORN_WORKERS / GUNICORN_TIMEOUT 必须真被 gunicorn.conf.py 读到。

    修复前 conf 是 `workers = 4` 硬编码，values 里的 GUNICORN_WORKERS
    改多少遍都没用。这里**真跑** conf 文件并断言取值跟着环境变量走，
    顺带确认缺省值仍然合理。
    """
    import runpy

    conf = REPO_ROOT / "gunicorn.conf.py"

    def load(env: dict[str, str]) -> dict:
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            return runpy.run_path(str(conf))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    defaults = load({})
    assert defaults["workers"] >= 1
    assert defaults["timeout"] >= 1

    tuned = load({"HEMALL_GUNICORN_WORKERS": "7", "HEMALL_GUNICORN_TIMEOUT": "45"})
    assert tuned["workers"] == 7, "gunicorn.conf.py 没有读 HEMALL_GUNICORN_WORKERS"
    assert tuned["timeout"] == 45, "gunicorn.conf.py 没有读 HEMALL_GUNICORN_TIMEOUT"

    # 非法值不能把服务搞崩 —— 回落默认值并留下可见信号
    bad = load({"HEMALL_GUNICORN_WORKERS": "not-a-number"})
    assert bad["workers"] == defaults["workers"], "非法 worker 数应回落默认值"


@ALL_VALUES
def test_monitoring_switches_render_real_resources(values_file: str):
    """values 里的 monitoring.serviceMonitor/prometheusRule 开关必须渲染出资源。

    修复前 `templates/` 下根本没有对应文件，开了等于没开。
    """
    index = _rendered(values_file)
    values = yaml.safe_load((CHART_DIR / values_file).read_text(encoding="utf-8"))
    monitoring = values.get("monitoring") or {}

    if monitoring.get("serviceMonitor", {}).get("enabled"):
        key = ("ServiceMonitor", "hemall-api")
        assert key in index, f"{values_file}: serviceMonitor.enabled=true 但没渲染出 ServiceMonitor"
        sm = index[key]
        assert sm["spec"]["selector"]["matchLabels"], "ServiceMonitor 必须有 selector 才能选到 pod"
        assert sm["spec"]["endpoints"], "ServiceMonitor 必须声明抓取端点"

    if monitoring.get("prometheusRule", {}).get("enabled"):
        key = ("PrometheusRule", "hemall-api")
        assert key in index, f"{values_file}: prometheusRule.enabled=true 但没渲染出 PrometheusRule"
        rule = index[key]
        assert rule["spec"]["groups"], "PrometheusRule 必须带 groups，否则没有告警规则"
        alerts = [r["alert"] for g in rule["spec"]["groups"] for r in g.get("rules", [])]
        assert alerts, "PrometheusRule 的 groups 里没有 alert 规则"


@ALL_VALUES
def test_monitoring_selectors_match_the_deployment_pod_labels(values_file: str):
    """ServiceMonitor 的 selector 必须真的能选中 API pod，否则抓不到指标。"""
    index = _rendered(values_file)
    sm = index.get(("ServiceMonitor", "hemall-api"))
    if sm is None:
        pytest.skip(f"{values_file}: 未启用 ServiceMonitor")
    pod_labels = _of(values_file, "Deployment", "hemall-api")["spec"]["template"]["metadata"]["labels"]
    for key, value in sm["spec"]["selector"]["matchLabels"].items():
        assert pod_labels.get(key) == value, (
            f"{values_file}: ServiceMonitor selector {key}={value} 与 pod labels {pod_labels} 不匹配 → 抓不到指标"
        )


@ALL_VALUES
def test_values_env_keys_are_real_hemall_settings(values_file: str):
    """values 里的 `api.env` 键必须也是真实 Settings 字段。

    values 是运维唯一会去改的地方。里面的死键比模板里的死键更隐蔽：改完
    没报错、没告警、指标一切正常，只是配置从来没生效过。
    """
    values = yaml.safe_load((CHART_DIR / values_file).read_text(encoding="utf-8"))
    declared = (values.get("api") or {}).get("env") or {}
    if not declared:
        pytest.skip(f"{values_file}: 没有自定义 api.env")

    fields = _settings_field_names()
    gunicorn_managed = {"HEMALL_GUNICORN_WORKERS", "HEMALL_GUNICORN_TIMEOUT"}

    for key in declared:
        if key in gunicorn_managed:
            continue
        assert key.startswith("HEMALL_"), (
            f"{values_file}: api.env.{key} 没有 HEMALL_ 前缀 → Settings 会静默忽略它"
        )
        assert key[len("HEMALL_") :].lower() in fields, (
            f"{values_file}: api.env.{key} 对应不到任何 Settings 字段 → 静默忽略"
        )
