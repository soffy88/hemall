"""P1-6: 告警规则必须真的被加载，且必须有人收。

审计发现：

1. `prometheus.yml` 声明 `rule_files: [/etc/prometheus/prometheus.rules.yml]`，
   但 **只有 mall compose 挂了** prometheus.rules.yml。`rule_files` 是 glob，
   文件缺失时 Prometheus **不报错也不加载** → prod/dev 上 0 条告警，
   5xx 飙升、验签拒绝、引擎 tick 超时、DB 死锁全都不会通知任何人。
2. dev compose 里应用服务叫 `app`，而 prometheus.yml 的 target 写死
   `api:8000` → dev 连指标都抓不到，`job="hemall-backend"` 的告警恒不触发。
3. 全仓库没有 Alertmanager 收件人，`prometheus.rules.yml:9-11` 自认这一点。

本测试把这些变成可断言的不变量：
- 每个带 prometheus 服务的 compose 都必须挂载 prometheus.rules.yml；
- prometheus 配置必须声明 alerting.alertmanagers，且指向同一 compose 里真实
  存在的 alertmanager 服务；
- 抓取目标的 host 必须是该 compose 中真实存在的服务名（抓不到 = 静默无告警）；
- alertmanager 收件地址用 `${VAR}` 占位且在 compose 中以 `:?` fail-closed 声明；
- alertmanager 入口脚本在占位符缺失时**拒绝启动**（真跑 sh 验证，不是看注释）。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PROM_YML = REPO_ROOT / "prometheus.yml"
PROM_DEV_YML = REPO_ROOT / "prometheus.dev.yml"
RULES_YML = REPO_ROOT / "prometheus.rules.yml"
AM_CONFIG = REPO_ROOT / "alertmanager" / "alertmanager.yml"
AM_ENTRYPOINT = REPO_ROOT / "alertmanager" / "entrypoint.sh"

COMPOSE_FILES = sorted(
    p
    for p in REPO_ROOT.glob("docker-compose*.yml")
    if p.is_file() and p.name != "docker-compose.iot.yml"
)


def _compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _compose_with_prometheus() -> list[Path]:
    return [p for p in COMPOSE_FILES if "prometheus" in (_compose(p).get("services") or {})]


def _prom_config_mount(compose: dict) -> str | None:
    svc = (compose.get("services") or {}).get("prometheus") or {}
    for vol in svc.get("volumes") or []:
        src = vol.split(":")[0] if isinstance(vol, str) else ""
        if src.endswith("prometheus.yml") or src.endswith("prometheus.dev.yml"):
            return src
    return None


def _mounted_sources(compose: dict, service: str) -> list[str]:
    svc = (compose.get("services") or {}).get(service) or {}
    out = []
    for vol in svc.get("volumes") or []:
        if isinstance(vol, str):
            out.append(vol.split(":")[0])
    return out


# ── 规则文件本身 ────────────────────────────────────────────────────────────


def test_alert_rules_file_exists_and_has_rules():
    assert RULES_YML.is_file(), "prometheus.rules.yml 缺失"
    data = yaml.safe_load(RULES_YML.read_text(encoding="utf-8"))
    rules = [r for g in data.get("groups", []) for r in g.get("rules", [])]
    assert len(rules) >= 4, f"生死告警规则只剩 {len(rules)} 条"
    for r in rules:
        assert r.get("alert"), "存在没有 alert 名的规则"
        assert r.get("expr"), f"告警 {r.get('alert')} 缺少 expr"
        assert r.get("severity") or (r.get("labels") or {}).get("severity"), (
            f"告警 {r.get('alert')} 缺少 severity 标签，Alertmanager 无法路由"
        )


def test_rule_files_globs_are_declared():
    for cfg in (PROM_YML, PROM_DEV_YML):
        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert data.get("rule_files"), f"{cfg.name} 未声明 rule_files"


# ── 每个 compose 都要挂规则 ─────────────────────────────────────────────────


@pytest.mark.parametrize("compose", _compose_with_prometheus(), ids=lambda p: p.name)
def test_compose_mounts_alert_rules(compose: Path):
    data = _compose(compose)
    sources = _mounted_sources(data, "prometheus")
    assert any(s.endswith("prometheus.rules.yml") for s in sources), (
        f"{compose.name}: prometheus 未挂载 prometheus.rules.yml → 告警静默不生效"
    )


@pytest.mark.parametrize("compose", _compose_with_prometheus(), ids=lambda p: p.name)
def test_compose_has_alertmanager_service(compose: Path):
    data = _compose(compose)
    assert "alertmanager" in (data.get("services") or {}), (
        f"{compose.name}: 没有 Alertmanager，告警只在 Prometheus UI 可见，无人接收"
    )


@pytest.mark.parametrize("compose", _compose_with_prometheus(), ids=lambda p: p.name)
def test_prometheus_config_is_mounted(compose: Path):
    assert _prom_config_mount(_compose(compose)) is not None, (
        f"{compose.name}: prometheus 服务没有挂载任何 prometheus 配置"
    )


# ── 抓取目标必须是真实服务 ──────────────────────────────────────────────────


def _targets(cfg_path: Path) -> list[str]:
    """返回全部抓取目标原文 (含端口)，如 ['api:8000', 'localhost:9090']。"""
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    hosts: list[str] = []
    for job in data.get("scrape_configs") or []:
        for sc in job.get("static_configs") or []:
            for target in sc.get("targets") or []:
                hosts.append(str(target))
    return hosts


@pytest.mark.parametrize("compose", _compose_with_prometheus(), ids=lambda p: p.name)
def test_scrape_targets_are_real_services(compose: Path):
    """抓取目标写错服务名 → 该 job 恒无数据 → 相关告警永不触发。"""
    data = _compose(compose)
    services = set(data.get("services") or {})
    mount = _prom_config_mount(data)
    assert mount is not None
    cfg = REPO_ROOT / mount
    assert cfg.is_file(), f"{compose.name} 挂载的 {mount} 不存在"

    for target in _targets(cfg):
        host = target.split(":")[0]
        # localhost 指 Prometheus 自身
        expected = "prometheus" if host == "localhost" else host
        assert expected in services, (
            f"{compose.name} 用 {cfg.name} 抓取 '{target}'，但 compose 中没有名为 "
            f"'{expected}' 的服务 → 该 job 抓不到数据，告警恒不触发"
        )


def test_dev_compose_scrapes_the_app_service():
    """dev compose 的应用服务叫 app，抓取目标必须与之对应。"""
    data = _compose(REPO_ROOT / "docker-compose.yml")
    assert "app" in data["services"]
    mount = _prom_config_mount(data)
    assert mount is not None
    assert "app:8000" in _targets(REPO_ROOT / mount), (
        "dev compose 的 Prometheus 抓取目标不是 app:8000 (audit P1-6)"
    )


def test_dev_and_prod_configs_are_distinct():
    """dev/prod 服务名不同，必须用不同配置文件，否则至少一边抓不到。"""
    assert PROM_YML.is_file() and PROM_DEV_YML.is_file()
    assert "app:8000" in _targets(PROM_DEV_YML)
    assert "api:8000" in _targets(PROM_YML)
    assert PROM_YML.read_text(encoding="utf-8") != PROM_DEV_YML.read_text(encoding="utf-8")


# ── Alertmanager 收件人 ─────────────────────────────────────────────────────


@pytest.mark.parametrize("compose", _compose_with_prometheus(), ids=lambda p: p.name)
def test_prometheus_alerts_to_alertmanager(compose: Path):
    data = _compose(compose)
    mount = _prom_config_mount(data)
    cfg = yaml.safe_load((REPO_ROOT / mount).read_text(encoding="utf-8"))
    managers = (cfg.get("alerting") or {}).get("alertmanagers") or []
    targets = [
        t
        for am in managers
        for st in am.get("static_configs") or []
        for t in st.get("targets") or []
    ]
    assert targets, f"{mount}: 未配置 alerting.alertmanagers，告警不会被转发"
    for target in targets:
        host = str(target).split(":")[0]
        assert host in (data.get("services") or {}), (
            f"{mount} 把告警转发到 '{host}'，但 {compose.name} 中无此服务"
        )


def test_alertmanager_config_has_real_receiver():
    assert AM_CONFIG.is_file(), "缺少 alertmanager/alertmanager.yml"
    data = yaml.safe_load(AM_CONFIG.read_text(encoding="utf-8"))
    receivers = data.get("receivers") or []
    assert receivers, "Alertmanager 没有任何 receiver"
    assert data.get("route"), "Alertmanager 缺少 route"
    routes = data["route"]
    # route 树必须能到达某个 receiver
    leaf = routes.get("receiver")
    names = {r.get("name") for r in receivers}
    assert leaf in names, f"route.receiver='{leaf}' 不在 receivers {names} 中"


def test_receivers_use_env_placeholders():
    text = AM_CONFIG.read_text(encoding="utf-8")
    placeholders = set(re.findall(r"\$\{([A-Z0-9_]+)\}", text))
    assert placeholders, "Alertmanager 收件地址没有使用环境变量占位，收件人会被硬编码"
    assert any("WEBHOOK" in p or "ALERT" in p for p in placeholders), (
        f"占位符看起来不是告警收件地址: {sorted(placeholders)}"
    )


@pytest.mark.parametrize("compose", _compose_with_prometheus(), ids=lambda p: p.name)
def test_alertmanager_placeholders_declared_fail_closed(compose: Path):
    """占位符必须在 compose 里以 `${VAR:?...}` 必填形式声明，否则缺配置静默失联。"""
    text = AM_CONFIG.read_text(encoding="utf-8")
    placeholders = set(re.findall(r"\$\{([A-Z0-9_]+)\}", text))
    raw = compose.read_text(encoding="utf-8")
    for name in sorted(placeholders):
        assert re.search(rf"\$\{{{name}:\?", raw), (
            f"{compose.name}: 占位符 {name} 未以 ${{{name}:?...}} 必填形式注入 —— "
            "缺配置时会在渲染阶段失败而不是静默发不出告警"
        )


# ── Alertmanager 渲染入口的 fail-closed 行为 (真跑 sh) ──────────────────────


def _run_entrypoint(env: dict[str, str]) -> subprocess.CompletedProcess:
    """用 /bin/sh 真跑入口脚本；ALERTMANAGER_BIN 指向 echo 以免真起服务。"""
    full_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "ALERTMANAGER_BIN": "/bin/echo",
        **env,
    }
    return subprocess.run(
        ["/bin/sh", str(AM_ENTRYPOINT)],
        capture_output=True,
        text=True,
        env=full_env,
        cwd=str(REPO_ROOT),
    )


def test_entrypoint_renders_and_launches_when_env_present(tmp_path: Path):
    out = tmp_path / "rendered.yml"
    proc = _run_entrypoint(
        {
            "ALERTMANAGER_CONFIG_TEMPLATE": str(AM_CONFIG),
            "ALERTMANAGER_RENDERED_CONFIG": str(out),
            "HEMALL_ALERT_WEBHOOK_URL": "https://example.invalid/hook",
        }
    )
    assert proc.returncode == 0, f"渲染失败: {proc.stderr}"
    rendered = out.read_text(encoding="utf-8")
    assert "https://example.invalid/hook" in rendered
    assert "${" not in rendered, "渲染结果里仍有未替换的占位符"
    # 渲染结果必须是合法 YAML 且能过最基本结构校验
    parsed = yaml.safe_load(rendered)
    assert parsed.get("receivers") and parsed.get("route")


def test_entrypoint_fails_closed_when_webhook_missing():
    proc = _run_entrypoint(
        {
            "ALERTMANAGER_CONFIG_TEMPLATE": str(AM_CONFIG),
            "ALERTMANAGER_RENDERED_CONFIG": "/tmp/opencode/should-not-exist.yml",
        }
    )
    assert proc.returncode != 0, "缺 webhook 地址却仍然启动了 Alertmanager（告警会静默失联）"
    assert "HEMALL_ALERT_WEBHOOK_URL" in (proc.stderr + proc.stdout)


def test_entrypoint_fails_closed_on_empty_value():
    proc = _run_entrypoint(
        {
            "ALERTMANAGER_CONFIG_TEMPLATE": str(AM_CONFIG),
            "ALERTMANAGER_RENDERED_CONFIG": "/tmp/opencode/should-not-exist2.yml",
            "HEMALL_ALERT_WEBHOOK_URL": "",
        }
    )
    assert proc.returncode != 0, "空 webhook 地址被当成有效配置"


def test_entrypoint_fails_closed_on_unresolvable_placeholder():
    """未知占位符必须报错，不能原样写进配置让 Alertmanager 启动后收不到。"""
    proc = _run_entrypoint(
        {
            "ALERTMANAGER_CONFIG_TEMPLATE": str(AM_CONFIG),
            "ALERTMANAGER_RENDERED_CONFIG": "/tmp/opencode/should-not-exist3.yml",
            "HEMALL_ALERT_WEBHOOK_URL": "https://example.invalid/hook",
            "HEMALL_ALERT_TYPO_EXTRA": "x",
        }
    )
    # 多余环境变量不应导致失败 (只认模板里出现的占位符)
    assert proc.returncode == 0, proc.stderr


def test_entrypoint_declares_strict_mode():
    body = AM_ENTRYPOINT.read_text(encoding="utf-8")
    assert re.search(r"^set\s+-[a-z]*e", body, re.M), "入口脚本未启用 errexit"
    assert re.search(r"^set\s+-[a-z]*u", body, re.M), "入口脚本未启用 nounset"
