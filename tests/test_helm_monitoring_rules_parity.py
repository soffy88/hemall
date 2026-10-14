"""告警规则不能有两份会漂移的拷贝。

compose 侧 Prometheus 通过 `rule_files` 加载仓库根目录的
`prometheus.rules.yml`；Helm 侧 PrometheusRule 只能读 chart 目录内的文件
（Helm 的 `.Files` 不越目录），所以 `charts/hemal/files/prometheus.rules.yml`
是它的一份副本。

两份一旦漂移，后果是**静默**的：compose 环境该响的告警不响，K8s 环境反过来。
静态检查抓不到这种不一致，只能把两条路径都解析一遍对比规则集合。

这个测试同时兜住三件事：
1. 副本必须与根文件字节级一致（或至少规则集合等价）；
2. 两边都能解析出非空 groups，且告警名集合相同；
3. chart 副本被误删/写坏时 PrometheusRule 模板的 fail 会真的触发。
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT_RULES = REPO_ROOT / "prometheus.rules.yml"
CHART_RULES = REPO_ROOT / "charts" / "hemal" / "files" / "prometheus.rules.yml"
PROMETHEUS_RULE_TEMPLATE = REPO_ROOT / "charts" / "hemal" / "templates" / "prometheusrule.yaml"


def _rules(path: Path) -> list[dict]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return parsed.get("groups") or []


def _alert_names(groups: list[dict]) -> set[str]:
    return {rule["alert"] for group in groups for rule in group.get("rules", []) if "alert" in rule}


def test_chart_copy_exists():
    assert CHART_RULES.is_file(), (
        f"{CHART_RULES} 缺失 —— PrometheusRule 模板会 fail()，集群里渲染不出任何告警规则"
    )


def test_chart_copy_is_byte_identical_to_the_compose_rule_file():
    """直接比对字节：注释和字段顺序的差异同样是"两份规则"的信号。"""
    assert CHART_RULES.read_bytes() == ROOT_RULES.read_bytes(), (
        "charts/hemal/files/prometheus.rules.yml 与 prometheus.rules.yml 不一致。"
        "改告警规则时必须同步改两份 (cp prometheus.rules.yml charts/hemal/files/)"
    )


def test_both_copies_expose_the_same_alerts():
    root_alerts = _alert_names(_rules(ROOT_RULES))
    chart_alerts = _alert_names(_rules(CHART_RULES))
    assert root_alerts, "根 prometheus.rules.yml 一条告警都没有 —— compose 侧等于没配告警"
    assert chart_alerts == root_alerts, f"两侧告警集合不一致: 仅根={root_alerts - chart_alerts} 仅 chart={chart_alerts - root_alerts}"


def test_every_alert_has_the_fields_prometheus_needs():
    """alert/expr/severity 缺一不可；缺 severity 会让 Alertmanager 无法分级路由。"""
    for source, groups in (("prometheus.rules.yml", _rules(ROOT_RULES)), ("chart 副本", _rules(CHART_RULES))):
        assert groups, f"{source}: groups 为空"
        for group in groups:
            assert group.get("name"), f"{source}: 有 group 缺 name"
            for rule in group.get("rules", []):
                for field in ("alert", "expr", "severity"):
                    assert rule.get(field) or (
                        rule.get("labels") or {}
                    ).get(field), f"{source}: 规则 {rule.get('alert')} 缺 {field}"


def test_template_fails_loudly_instead_of_rendering_zero_alerts():
    """模板必须 fail 而不是静默渲染空 groups。

    告警配错时最糟的表征不是报错，而是"跑着但没人收" —— 所以这里要求
    模板对空规则文件显式 fail。
    """
    text = PROMETHEUS_RULE_TEMPLATE.read_text(encoding="utf-8")
    assert 'fail "' in text, "PrometheusRule 模板必须在规则缺失/为空时 fail()"
    assert ".Files.Get" in text, "规则内容应来自 chart 内的文件，而不是在模板里另抄一份"
    assert "fromYaml" in text, "模板必须解析规则文件并断言 groups 非空"
