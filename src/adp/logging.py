"""Structured JSON logging correlated by run_id."""

import logging
import sys
import uuid

import structlog
from structlog.contextvars import bind_contextvars


def configure_logging(level: str = "INFO") -> None:
    """Render JSON logs on stdout; run_id comes from contextvars."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )


def bind_run_id(run_id: str | None = None) -> str:
    """Bind run_id to all subsequent logs (generates a uuid4 hex if absent)."""
    rid = run_id or uuid.uuid4().hex
    bind_contextvars(run_id=rid)
    return rid


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    """Return a structlog logger."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
