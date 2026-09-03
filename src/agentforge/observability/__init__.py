"""Observability wiring — structured logs, Prometheus metrics, OTLP traces.

One call, ``configure_observability()``, sets up all three; every entrypoint
(``agentforge api``, ``agentforge worker``, tests that need it) makes it. It is
idempotent.
"""

from __future__ import annotations

from typing import Any

from agentforge.logging import configure_logging, get_logger
from agentforge.observability import metrics
from agentforge.observability.tracing import (
    configure_tracing,
    get_tracer,
    set_span_attributes,
    span,
)

log = get_logger("observability")

__all__ = [
    "configure_observability",
    "get_tracer",
    "instrument_fastapi",
    "metrics",
    "set_span_attributes",
    "span",
]


def configure_observability() -> None:
    configure_logging()
    configure_tracing()


def instrument_fastapi(app: Any) -> None:
    """Attach OpenTelemetry's ASGI middleware to a FastAPI app (best effort)."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception:  # noqa: BLE001 - never block startup on instrumentation
        log.warning("fastapi_instrumentation_failed")
