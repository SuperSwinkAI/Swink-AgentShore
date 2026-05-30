"""Atomic context-file writer for agent enrichment."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

_logger = structlog.get_logger(__name__)


def write_context_file(path: Path, payload: dict[str, object]) -> int:
    """Write *payload* as JSON to *path* atomically (temp + rename).

    Returns the byte size of the written file so callers can record
    context-payload telemetry without an extra ``stat()``.

    Atomicity prevents agents from reading a partially-written file.
    The parent directory is created if it does not exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".context_", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.flush()
            bytes_written = fh.tell()
        os.replace(tmp_path, path)
        return bytes_written
    except (OSError, TypeError, ValueError) as exc:
        _logger.warning("context_file_write_failed", path=str(path), error=str(exc))
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
