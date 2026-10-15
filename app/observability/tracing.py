"""OpenTelemetry 链路追踪配置。

Phase 0 交付：全链路 trace，支持 Jaeger/Tempo 可视化。

事故修复 (2026-10-15): 未部署 jaeger 时 OTEL 导出器持续报错刷日志。原因是
``OTLPSpanExporter`` 无条件构造, 端点缺失/格式错误也不拦, BatchSpanProcessor
在后台线程无限重试。现在改成**导出器配置解析与可用性判定**:
端点未配置或解析失败 → 不挂导出器 (tracing 仍然工作, 只是没有远端 span),
且只记一次 warning, 不刷屏。详见 :func:`resolve_exporter_config`。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping
from urllib.parse import urlparse

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

logger = logging.getLogger("hemall.tracing")

#: 导出器类型取值
EXPORTER_NONE = "none"
EXPORTER_CONSOLE = "console"
EXPORTER_OTLP = "otlp"

#: OTLP gRPC 默认端点——仅在用户显式选了 otlp 但没给端点时作为兜底
DEFAULT_OTLP_ENDPOINT = "http://localhost:4317"

#: 生产环境下未显式指定 OTEL_TRACES_EXPORTER 时的默认值 (不刷 stdout)
DEFAULT_PRODUCTION_EXPORTER = EXPORTER_NONE


def resolve_exporter_config(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str | None]:
    """把 OTEL_* 环境变量解析成 ``(exporter_type, endpoint_or_reason)``。

    不做任何网络探测, 纯字符串判定, 便于单测:

    - 显式 ``none`` → 关闭导出 (仍装 FastAPI/httpx 埋点, 只是不发出去)
    - production 且未显式指定 → ``none`` (避免 ConsoleSpanExporter 刷 stdout)
    - ``otlp`` 但端点为空/非 http(s)/无主机名 → 退回 ``none``, 第二个返回值是原因
    - ``otlp`` 端点合法 → ``("otlp", endpoint)``, 缺省端点用
      :data:`DEFAULT_OTLP_ENDPOINT`
    - 其他取值按 console 处理

    Returns
    -------
    (exporter_type, detail)
        ``exporter_type in {"none", "console", "otlp"}``；``detail`` 在 otlp 时
        是端点, 在降级为 none 时是降级原因 (供调用方记一次 warning)。
    """
    env = os.environ if environ is None else environ

    raw = (env.get("OTEL_TRACES_EXPORTER") or "").strip().lower()
    if raw == EXPORTER_NONE:
        return EXPORTER_NONE, "OTEL_TRACES_EXPORTER=none"

    if not raw:
        # 未显式指定: production 下不导出 (jaeger 常常没部署, 别刷日志)
        if (env.get("HEMALL_ENVIRONMENT") or "").strip().lower() == "production":
            return EXPORTER_NONE, "production without OTEL_TRACES_EXPORTER"
        return EXPORTER_CONSOLE, None

    if raw != EXPORTER_OTLP:
        return EXPORTER_CONSOLE, None

    endpoint = (env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    if not endpoint:
        endpoint = DEFAULT_OTLP_ENDPOINT
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return (
            EXPORTER_NONE,
            f"OTEL_EXPORTER_OTLP_ENDPOINT is not a valid http(s) URL: {endpoint!r}",
        )
    return EXPORTER_OTLP, endpoint


# ── 追踪器初始化 (进程启动期调用一次) ────────────────────────────────────


def setup_tracing(app: Any | None = None, service_name: str = "hemall-backend") -> None:
    """配置 OpenTelemetry 追踪。

    环境变量:
        OTEL_EXPORTER_OTLP_ENDPOINT: OTLP 端点 (缺省 http://localhost:4317)
        OTEL_TRACES_EXPORTER: 导出器类型 (none/console/otlp; 未设置时 development
            用 console、production 用 none)

    Usage:
        from app.observability.tracing import setup_tracing, get_tracer
        setup_tracing(app)  # 传入 FastAPI app 实例
        tracer = get_tracer(__name__)

        with tracer.start_as_current_span("custom_operation"):
            ...
    """
    # Resource 携带服务元数据
    resource = Resource.create({SERVICE_NAME: service_name})

    # TracerProvider
    provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(provider)

    # 导出器配置 (端点缺失/非法一律降级为"不导出", 不让后台线程无限重试刷日志)
    exporter_type, detail = resolve_exporter_config()

    if exporter_type == EXPORTER_OTLP:
        exporter = OTLPSpanExporter(endpoint=detail)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    elif exporter_type == EXPORTER_CONSOLE:
        # Console 导出器 (本地开发)
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    else:
        logger.info(
            "OTel span export disabled (%s); tracing instrumentation stays active",
            detail,
        )

    # 自动埋点：FastAPI
    if app is not None:
        FastAPIInstrumentor.instrument_app(app)
    else:
        FastAPIInstrumentor().instrument()

    # 自动埋点：HTTP 客户端 (httpx)
    HTTPXClientInstrumentor().instrument()


def get_tracer(name: str | None = None) -> trace.Tracer:
    """获取 tracer 实例。"""
    tracer_provider = trace.get_tracer_provider()
    return tracer_provider.get_tracer(name or "hemall")


# ── 手动埋点辅助 ────────────────────────────────────────────────────────


def set_span_attribute(key: str, value: Any) -> None:
    """为当前 span 添加属性。"""
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute(key, value)


def record_exception(exception: Exception) -> None:
    """记录异常到当前 span。"""
    span = trace.get_current_span()
    if span.is_recording():
        span.record_exception(exception)


def shutdown_tracing() -> None:
    """正确关闭 OpenTelemetry 追踪，flush 并 shutdown 所有 span processor。

    这防止了 pytest 退出时的 "I/O operation on closed file" 错误，
    也确保生产环境中 gunicorn worker 优雅退出时最后一批 span 不会丢失。
    """
    tracer_provider = trace.get_tracer_provider()
    if hasattr(tracer_provider, "shutdown"):
        tracer_provider.shutdown()
    elif hasattr(tracer_provider, "force_flush"):
        tracer_provider.force_flush()
