"""事故回归 (2026-10-15): 未部署 jaeger 时 OTEL 导出器持续报错刷日志。

根因: ``setup_tracing`` 无条件构造 ``OTLPSpanExporter``——端点缺失或写成
``jaeger:4317`` 这种缺 scheme 的形式也照建, BatchSpanProcessor 在后台线程
无限重试并刷 ERROR。

本文件覆盖 :func:`app.observability.tracing.resolve_exporter_config` 的判定
矩阵, 以及「解析失败时不构造导出器、不注册 processor」的装配行为。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.observability.tracing import (
    DEFAULT_OTLP_ENDPOINT,
    EXPORTER_CONSOLE,
    EXPORTER_NONE,
    EXPORTER_OTLP,
    resolve_exporter_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── 配置解析矩阵 ────────────────────────────────────────────────────────


def test_explicit_none_disables_export() -> None:
    kind, detail = resolve_exporter_config(
        {"OTEL_TRACES_EXPORTER": "none", "HEMALL_ENVIRONMENT": "production"}
    )
    assert kind == EXPORTER_NONE
    assert detail == "OTEL_TRACES_EXPORTER=none"


def test_explicit_none_is_case_and_space_insensitive() -> None:
    kind, _ = resolve_exporter_config({"OTEL_TRACES_EXPORTER": "  NONE "})
    assert kind == EXPORTER_NONE


def test_production_without_exporter_env_does_not_export() -> None:
    """生产 (jaeger 常未部署) 未显式配置时不再刷 ConsoleSpanExporter。"""
    kind, detail = resolve_exporter_config({"HEMALL_ENVIRONMENT": "production"})
    assert kind == EXPORTER_NONE
    assert "production" in detail


def test_development_defaults_to_console() -> None:
    kind, _ = resolve_exporter_config({"HEMALL_ENVIRONMENT": "development"})
    assert kind == EXPORTER_CONSOLE
    kind, _ = resolve_exporter_config({})
    assert kind == EXPORTER_CONSOLE


def test_otlp_with_valid_endpoint_is_kept() -> None:
    kind, detail = resolve_exporter_config(
        {
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://jaeger:4317",
            "HEMALL_ENVIRONMENT": "production",
        }
    )
    assert kind == EXPORTER_OTLP
    assert detail == "http://jaeger:4317"


def test_otlp_without_endpoint_falls_back_to_default() -> None:
    kind, detail = resolve_exporter_config(
        {"OTEL_TRACES_EXPORTER": "otlp", "OTEL_EXPORTER_OTLP_ENDPOINT": ""}
    )
    assert kind == EXPORTER_OTLP
    assert detail == DEFAULT_OTLP_ENDPOINT


@pytest.mark.parametrize(
    "bad",
    [
        "jaeger:4317",  # 缺 scheme
        "localhost:4317",  # host:port 被当成 scheme
        "not a url",
        "://jaeger:4317",
        "http://",  # 无主机名
        "grpc://jaeger:4317",  # 非 http(s)
        "/var/run/otel.sock",
    ],
)
def test_unparsable_endpoint_disables_export_with_reason(bad: str) -> None:
    """端点解析失败 → 禁用导出, 并给出可诊断的原因。"""
    kind, detail = resolve_exporter_config(
        {
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_ENDPOINT": bad,
        }
    )
    assert kind == EXPORTER_NONE
    assert detail is not None and "not a valid http(s) URL" in detail
    assert repr(bad) in detail


def test_unknown_exporter_value_falls_back_to_console() -> None:
    kind, _ = resolve_exporter_config({"OTEL_TRACES_EXPORTER": "jaeger-thrift"})
    assert kind == EXPORTER_CONSOLE


# ── 装配行为: 禁用时绝不构造导出器 ───────────────────────────────────────


def test_setup_tracing_does_not_build_exporter_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """降级为 none 时 OTLPSpanExporter 一次都不该被构造 (否则后台线程仍会重试)。"""
    import app.observability.tracing as tracing

    built: list[str] = []

    class _Boom:
        def __init__(self, *args, **kwargs):  # pragma: no cover - 触发即失败
            built.append("otlp")
            raise AssertionError("OTLPSpanExporter must not be built")

    class _ConsoleBoom:
        def __init__(self, *args, **kwargs):  # pragma: no cover - 触发即失败
            built.append("console")
            raise AssertionError("ConsoleSpanExporter must not be built")

    monkeypatch.setattr(tracing, "OTLPSpanExporter", _Boom)
    monkeypatch.setattr(tracing, "ConsoleSpanExporter", _ConsoleBoom)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")

    tracing.setup_tracing(service_name="test-no-export")

    assert built == []
    tracing.shutdown_tracing()


def test_setup_tracing_builds_otlp_exporter_when_endpoint_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端点合法时才构造 OTLP 导出器 (线上 jaeger 已部署的路径不受影响)。"""
    import app.observability.tracing as tracing

    built: list[str] = []
    providers: list[object] = []

    class _FakeExporter:
        def __init__(self, endpoint: str | None = None, **kwargs: object) -> None:
            built.append(endpoint or "")

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(tracing, "OTLPSpanExporter", _FakeExporter)
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", providers.append)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4317")

    tracing.setup_tracing(service_name="test-otlp")

    assert built == ["http://jaeger:4317"]
    for provider in providers:
        provider.shutdown()  # type: ignore[attr-defined]


def test_setup_tracing_survives_broken_endpoint_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端点非法时 setup_tracing 不抛 (lifespan 里是非致命的一步)。"""
    import app.observability.tracing as tracing

    built: list[str] = []
    providers: list[Any] = []

    class _Boom:
        def __init__(self, *args, **kwargs):  # pragma: no cover - 触发即失败
            built.append("otlp")
            raise AssertionError("OTLPSpanExporter must not be built")

    monkeypatch.setattr(tracing, "OTLPSpanExporter", _Boom)
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", providers.append)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "jaeger:4317")

    tracing.setup_tracing(service_name="test-broken-endpoint")  # 不抛即达标

    assert built == [], "端点解析失败时不能构造导出器"
    assert len(providers) == 1
    # 降级后 provider 里没有任何 span processor (没有后台线程刷日志)
    assert providers[0]._active_span_processor._span_processors == ()


# ── 部署工件: 没有 jaeger 就不该配 OTLP ─────────────────────────────────


def test_prod_compose_does_not_force_otlp_exporter() -> None:
    """docker-compose.prod.yml 不应把导出器指向 jaeger 而不给端点 (或反之)。"""
    text = (REPO_ROOT / "docker-compose.prod.yml").read_text()
    api_block = text.split("\n  api:", 1)[1].split("\n  postgres:", 1)[0]
    if "OTEL_TRACES_EXPORTER" in api_block:
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in api_block, (
            "设了 OTLP 导出器就必须同时给端点, 否则会被降级为不导出"
        )
