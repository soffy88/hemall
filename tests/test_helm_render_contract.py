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


