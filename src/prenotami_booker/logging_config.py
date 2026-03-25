"""Structured logging configuration using structlog."""

from __future__ import annotations

import uuid

import structlog


def setup_logging(*, json_output: bool = False) -> None:
    """Configure structlog for the application.

    Args:
        json_output: If True, output JSON formatted logs. Otherwise, use
            console-friendly formatting.
    """
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
    ]

    if json_output:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def generate_correlation_id() -> str:
    """Generate a unique correlation ID for request tracing.

    Returns:
        A short UUID string for use as correlation_id in log lines.
    """
    return uuid.uuid4().hex[:12]
