"""Structured logging setup (structlog over stdlib).

Emits JSON in every non-local environment so logs are ingestible by Loki /
CloudWatch / Datadog without a parsing layer. Trace/span ids are bound by the
OpenTelemetry integration when a span is active.
"""

from __future__ import annotations

import logging
import sys

import structlog

from agentforge.config import settings

_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        timestamper,
    ]

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=settings.log_level,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
