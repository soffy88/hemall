"""OpenTelemetry 链路追踪配置。

Phase 0 交付：全链路 trace，支持 Jaeger/Tempo 可视化。
"""

from __future__ import annotations

import os
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

# ── 追踪器初始化 (进程启动期调用一次) ────────────────────────────────────


def setup_tracing(app: Any | None = None, service_name: str = "hemall-backend") -> None:
    """配置 OpenTelemetry 追踪。

    环境变量:
        OTEL_EXPORTER_OTLP_ENDPOINT: OTLP 端点 (默认 http://localhost:4317)
        OTEL_TRACES_EXPORTER: 导出器类型 (console/otlp, 默认 console)

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

    # 导出器配置
    exporter_type = os.getenv("OTEL_TRACES_EXPORTER", "console").lower()

    if exporter_type == "otlp":
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        exporter = OTLPSpanExporter(endpoint=endpoint)
        processor = BatchSpanProcessor(exporter)
        provider.add_span_processor(processor)
    else:
        # Console 导出器 (开发环境)
        processor = BatchSpanProcessor(ConsoleSpanExporter())
        provider.add_span_processor(processor)

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
