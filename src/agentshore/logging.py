"""Structured logging configuration (structlog -> NDJSON)."""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from structlog.types import EventDict, WrappedLogger

import structlog

_session_id_var: ContextVar[str | None] = ContextVar("session_id", default=None)
_correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_LOG_LEVEL_MAP: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def _add_session_context(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    sid = _session_id_var.get()
    if sid is not None:
        event_dict.setdefault("session_id", sid)
    return event_dict


def _add_correlation_id(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    cid = _correlation_id_var.get()
    if cid is not None:
        event_dict.setdefault("correlation_id", cid)
    return event_dict


def setup_logging(
    level: str = "info",
    log_dir: Path | None = None,
    session_id: str | None = None,
) -> None:
    """Configure structlog and stdlib logging for AgentShore.

    Parameters
    ----------
    level:
        One of ``"debug"``, ``"info"``, ``"warning"``, ``"error"``.
    log_dir:
        Directory for log files.  When provided (together with *session_id*),
        a file handler is added that writes NDJSON to
        ``<log_dir>/agentshore-<session_id>.log``.
    session_id:
        Current session identifier.  Stored in a context var so every log
        entry automatically includes it.
    """
    if session_id is not None:
        _session_id_var.set(session_id)

    numeric_level = _LOG_LEVEL_MAP.get(level.lower(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        _add_session_context,
        _add_correlation_id,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(numeric_level)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)

    if log_dir is not None and session_id is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"agentshore-{session_id}.log"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for the given *name*."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]


@contextmanager
def with_correlation(correlation_id: str) -> Iterator[None]:
    """Context manager that binds *correlation_id* for the enclosed scope."""
    token = _correlation_id_var.set(correlation_id)
    try:
        yield
    finally:
        _correlation_id_var.reset(token)
