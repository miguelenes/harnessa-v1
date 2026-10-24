"""OpenTelemetry setup and safe trace-context helpers for API and worker processes."""

from __future__ import annotations

import os
from collections.abc import Callable, Coroutine, Mapping
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

_configured = False
P = ParamSpec("P")
R = TypeVar("R")


def configure_tracing(service_name: str) -> TracerProvider:
    """Configure one process-local provider; export only when an OTLP endpoint is set."""
    global _configured
    provider = trace.get_tracer_provider()
    if _configured:
        return provider  # type: ignore[return-value]

    traces_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    base_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    configured_provider = _create_tracer_provider(
        service_name, base_endpoint=base_endpoint, traces_endpoint=traces_endpoint
    )
    trace.set_tracer_provider(configured_provider)
    _configured = True
    return configured_provider


def _create_tracer_provider(
    service_name: str, *, base_endpoint: str = "", traces_endpoint: str = ""
) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    endpoint = traces_endpoint or (
        base_endpoint.rstrip("/") + "/v1/traces" if base_endpoint else ""
    )
    if endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=5))
        )
    return provider


def tracer_for(module_name: str) -> trace.Tracer:
    return trace.get_tracer(module_name)


def shutdown_tracing() -> None:
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.force_flush(timeout_millis=5_000)
        provider.shutdown()


def extract_trace_context(carrier: Mapping[str, str] | None = None) -> Context:
    """Extract only the standard W3C trace context; baggage is intentionally ignored."""
    if not carrier:
        return Context()
    normalized = {key.lower(): value for key, value in carrier.items()}
    return extract(carrier=normalized)


def inject_trace_context(carrier: dict[str, str]) -> None:
    inject(carrier=carrier)


def current_traceparent() -> str | None:
    carrier: dict[str, str] = {}
    inject_trace_context(carrier)
    return carrier.get("traceparent")


def current_trace_id() -> str | None:
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return f"{span_context.trace_id:032x}"


def traced_async(
    name: str,
    *,
    attributes: Callable[[Mapping[str, Any]], Mapping[str, str | int | bool]] | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
) -> Callable[[Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]]:
    """Add a bounded, redaction-safe span to an async operation."""

    def decorate(function: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, Coroutine[Any, Any, R]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            safe_attributes = attributes(kwargs) if attributes is not None else {}
            with tracer_for(function.__module__).start_as_current_span(
                name, kind=kind, attributes=safe_attributes
            ) as span:
                try:
                    return await function(*args, **kwargs)
                except Exception as exc:
                    span.set_status(Status(StatusCode.ERROR))
                    span.set_attribute("error.type", type(exc).__name__)
                    raise

        return wrapped

    return decorate


def trace_context_from_parent(traceparent: str | None) -> Context:
    if not traceparent or len(traceparent) != 55:
        return Context()
    return extract_trace_context({"traceparent": traceparent})
