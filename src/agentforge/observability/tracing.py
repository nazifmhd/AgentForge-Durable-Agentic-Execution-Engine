"""OpenTelemetry tracing setup.

``configure_tracing()`` is idempotent and safe to call from every entrypoint
(API, worker, CLI). With ``AGENTFORGE_OTEL_EXPORTER_OTLP_ENDPOINT`` set it ships
spans over OTLP; without it, tracing is a no-op (spans are still created so
context propagation and the structlog trace-id binding work, they just are not
exported).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from agentforge.config import settings
from agentforge.logging import get_logger

log = get_logger("tracing")

_configured = False


def configure_tracing(*, extra_processor: SpanProcessor | None = None) -> None:
    """Idempotent. Installs an SDK ``TracerProvider`` the first time (OpenTelemetry
    forbids replacing it later), then only ever adds span processors."""
    global _configured

    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        resource = Resource.create(
            {
                SERVICE_NAME: settings.otel_service_name,
                "deployment.environment": settings.environment,
            }
        )
        provider = TracerProvider(resource=resource)
        trace.set_tracer_provider(provider)

    if not _configured:
        if settings.otel_exporter_otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
            )
            log.info("tracing_otlp_enabled", endpoint=settings.otel_exporter_otlp_endpoint)
        _configured = True

    if extra_processor is not None:
        provider.add_span_processor(extra_processor)


def get_tracer(name: str = "agentforge") -> trace.Tracer:
    return trace.get_tracer(name)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Start a span; record any raised exception before re-raising."""
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as current:
        _set_attrs(current, attributes)
        try:
            yield current
        except Exception as exc:
            current.record_exception(exc)
            current.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            raise


def set_span_attributes(attributes: Mapping[str, Any]) -> None:
    _set_attrs(trace.get_current_span(), attributes)


def _set_attrs(target: trace.Span, attributes: Mapping[str, Any]) -> None:
    for key, value in attributes.items():
        if value is None:
            continue
        clean = value if isinstance(value, str | bool | int | float) else str(value)
        target.set_attribute(key, clean)


def _reset_for_tests() -> None:
    """Test hook: forget that tracing was configured so a fresh provider can be set."""
    global _configured
    _configured = False
