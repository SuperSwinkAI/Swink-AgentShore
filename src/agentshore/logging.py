"""Structured logging configuration (structlog -> NDJSON)."""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

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


def _build_formatter() -> structlog.stdlib.ProcessorFormatter:
    return structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            # Render exc_info into a structured ``exception`` field so exc_info=True
            # sites emit a real traceback in the NDJSON; without this only
            # ``"exc_info": true`` survived. Must precede JSONRenderer.
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
    )


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

    formatter = _build_formatter()

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


def attach_session_file_handler(log_dir: Path, session_id: str) -> None:
    """Attach the per-session NDJSON file handler early, before orchestrator boot.

    Unlike ``setup_logging``, this does NOT clear existing handlers — it only
    sets the session-id contextvar (so every subsequent log line carries
    ``session_id``) and adds the ``agentshore-<session_id>.log`` file handler
    if one for that exact path isn't already attached to the root logger.

    Exists to close a gap (issue #356): the sidecar's pre-bridge session-start
    phases (config_merge -> init_beads -> ...) run well before
    ``Orchestrator.bootstrap`` calls ``setup_logging`` with a file sink (that
    only happens in the later ``first_snapshot`` phase) — so log calls made
    during those early phases (e.g. ``reconcile_beads_schema``'s schema-drift
    warnings in ``init_beads``) previously had no durable sink and vanished.
    Calling this once ``session_id`` and the config-derived ``log_dir`` are
    known (end of phase 1, config_merge) gives them one. The later
    ``setup_logging`` call in ``orchestrator.py`` is idempotent against this:
    it recreates a handler for the identical path in the same process
    (embedded mode shares one root logger), so no duplication or loss occurs.
    """
    _session_id_var.set(session_id)

    log_path = log_dir / f"agentshore-{session_id}.log"
    resolved = log_path.resolve()

    root = logging.getLogger()
    for handler in root.handlers:
        if (
            isinstance(handler, logging.FileHandler)
            and Path(handler.baseFilename).resolve() == resolved
        ):
            return

    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(_build_formatter())
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
