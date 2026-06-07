"""File-backed transport for engine → dashboard state.

The orchestrator's :class:`~agentshore.ipc.provider.IpcStateProvider` writes
the latest :class:`~agentshore.state.OrchestratorState` snapshot to a JSON file in
the session directory after every play and appends every event (play
lifecycle, agent status, session lifecycle) to an NDJSON log.

The dashboard sidecar tails both files and fans new state/events out to
its connected browser WebSockets. This replaces the previous engine-side
streaming socket (`_ClientStream` + `_drain_stream`) that aborted slow
consumers after a 10-second drain timeout — see
``docs/design/ipc-decouple-coalesce`` for the original streaming design
and the dashboard freeze incident on 2026-05-16 for why the engine-side
push was replaced with a pull-and-tail file model.

Two files in the session dir:

- ``dashboard_state.json`` — current full state snapshot. Atomically
  replaced (tmp-write + rename) so a reader never sees a half-written
  file. Coalesced: only the latest snapshot is ever kept.

- ``dashboard_events.ndjson`` — line-appended event log. Bounded growth
  via a tail-and-truncate rotation once the file exceeds
  :data:`_EVENTS_ROTATE_BYTES`; tail size kept is
  :data:`_EVENTS_ROTATE_KEEP_BYTES`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
import time
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

_logger = structlog.get_logger(__name__)

STATE_FILENAME = "dashboard_state.json"
EVENTS_FILENAME = "dashboard_events.ndjson"

# Windows file-locking tolerance. The dashboard bridge opens these files
# read-only and closes them again on every poll; CPython's open() grants no
# FILE_SHARE_DELETE, so while that momentary handle is live an ``os.replace``
# or ``unlink`` of the target raises ``PermissionError`` (WinError 5/32) — a
# POSIX rename-over-open has no such problem. The bridge's hold is sub-poll, so
# a short bounded retry clears it; an orphaned prior-session bridge is the worst
# case and must never crash the orchestrator. POSIX takes the single-shot path.
_WIN_LOCK_RETRIES = 10
_WIN_LOCK_BACKOFF_S = 0.02


def _replace_with_retry(src: str, dst: Path) -> None:
    """``os.replace`` that tolerates a momentary Windows reader lock on *dst*."""
    if not sys.platform.startswith("win"):
        os.replace(src, dst)
        return
    last: OSError | None = None
    for _ in range(_WIN_LOCK_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(_WIN_LOCK_BACKOFF_S)
    assert last is not None
    raise last


# Rotation: when the events file exceeds ``_EVENTS_ROTATE_BYTES``, keep
# only the trailing ``_EVENTS_ROTATE_KEEP_BYTES`` (rounded to a line
# boundary). Historical events are recoverable from agentshore.db; the file
# is a tail consumed by the dashboard, so unbounded retention is wasted.
_EVENTS_ROTATE_BYTES = 5 * 1024 * 1024
_EVENTS_ROTATE_KEEP_BYTES = 1 * 1024 * 1024


def _unlink_best_effort(path: Path) -> None:
    """Remove a stale prior-session file without ever crashing boot.

    On Windows an orphaned prior-session dashboard bridge can still hold the
    file (no FILE_SHARE_DELETE), so ``unlink`` raises ``PermissionError`` rather
    than ``FileNotFoundError`` — which a bare ``suppress`` would not catch,
    taking the orchestrator down on startup. Retry briefly, then give up
    quietly: the fresh session coalesces the latest state on its first write, so
    a lingering stale file is self-correcting.
    """
    retries = _WIN_LOCK_RETRIES if sys.platform.startswith("win") else 1
    for attempt in range(retries):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt + 1 < retries:
                time.sleep(_WIN_LOCK_BACKOFF_S)
                continue
            _logger.warning("state_writer.reset_unlink_failed", path=str(path))
            return


def reset_session_files(session_dir: Path) -> None:
    """Remove a prior session's dashboard state/event files (best-effort).

    Exposed at module scope so the reset-before-prime ordering can run from the
    sidecar's start_bridge phase — before the embedded bridge primes — without
    prematurely constructing a :class:`StateWriter`. The ``StateWriter``
    constructor calls this too, so the orchestrator path stays self-resetting.
    """
    _unlink_best_effort(session_dir / STATE_FILENAME)
    _unlink_best_effort(session_dir / EVENTS_FILENAME)


class StateWriter:
    """Atomically write the latest state snapshot and append events.

    Both methods are coroutines but defer the blocking file I/O to a
    thread via :func:`asyncio.to_thread`, satisfying the project rule of
    no blocking calls in the core loop.

    The writer is process-safe for the single-producer use case (one
    orchestrator per session). It is not designed for concurrent
    writers; the session directory is owned by the engine.
    """

    def __init__(self, session_dir: Path) -> None:
        self._dir = session_dir
        self._state_path = session_dir / STATE_FILENAME
        self._events_path = session_dir / EVENTS_FILENAME
        # Single in-process lock around the file operations so async tasks
        # cannot interleave a tmp-write/rename with an append.
        self._lock = asyncio.Lock()
        # The session directory is keyed by project_key (stable path hash),
        # so prior sessions for the same project leave events behind. The
        # bridge's prime-from-disk would otherwise replay a prior session's
        # `session_ended` and trigger uvicorn `should_exit` on startup.
        reset_session_files(session_dir)

    @property
    def state_path(self) -> Path:
        return self._state_path

    @property
    def events_path(self) -> Path:
        return self._events_path

    async def write_state(self, message: str) -> None:
        """Replace the state snapshot atomically.

        *message* must be a single-line JSON string (typically produced by
        :func:`~agentshore.ipc.serializer.make_message`). The trailing newline,
        if any, is stripped — state snapshots are not line-delimited.
        Readers either see the old contents or the new contents.
        """
        blob = message.rstrip("\n")
        async with self._lock:
            await asyncio.to_thread(self._sync_write_state, blob)

    async def append_event(self, message: str) -> None:
        """Append *message* as a single NDJSON line and maybe rotate.

        *message* must be a single-line JSON string. Ensures exactly one
        trailing newline so concatenated lines remain valid NDJSON.
        """
        line = message if message.endswith("\n") else message + "\n"
        async with self._lock:
            await asyncio.to_thread(self._sync_append_event, line)

    def _sync_write_state(self, blob: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        # Use NamedTemporaryFile + os.replace so a crash mid-write leaves
        # the previous snapshot intact.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".dashboard_state-",
            suffix=".json.tmp",
            dir=self._dir,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(blob)
            _replace_with_retry(tmp_path, self._state_path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            if sys.platform.startswith("win"):
                # A dropped snapshot is recoverable — state is coalesced, so the
                # next write carries the latest. Never crash the orchestrator
                # over a transient dashboard-reader lock on Windows.
                _logger.warning("state_writer.write_state_failed", error=str(exc))
                return
            raise

    def _sync_append_event(self, line: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        with self._events_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        self._maybe_rotate_events()

    def _maybe_rotate_events(self) -> None:
        """Truncate the events file from the head when it grows past the cap.

        Keeps the trailing ``_EVENTS_ROTATE_KEEP_BYTES`` (rounded up to the
        next newline), so tailing consumers see a contiguous suffix even
        across rotations. Historical events remain available in
        ``agentshore.db`` and the engine log.
        """
        try:
            size = self._events_path.stat().st_size
        except OSError:
            return
        if size <= _EVENTS_ROTATE_BYTES:
            return

        try:
            with self._events_path.open("rb") as fh:
                fh.seek(-_EVENTS_ROTATE_KEEP_BYTES, os.SEEK_END)
                tail = fh.read()
        except OSError as exc:
            _logger.warning("state_writer.rotate_read_failed", error=str(exc))
            return

        # Trim to the first newline so we don't keep a partial line.
        newline = tail.find(b"\n")
        if newline != -1:
            tail = tail[newline + 1 :]

        # Rewrite via the same tmp+rename dance for atomicity.
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=".dashboard_events-",
                suffix=".ndjson.tmp",
                dir=self._dir,
            )
            with os.fdopen(fd, "wb") as fh:
                fh.write(tail)
            _replace_with_retry(tmp_path, self._events_path)
        except OSError as exc:
            _logger.warning("state_writer.rotate_write_failed", error=str(exc))


class NullStateWriter:
    """No-op writer for tests / providers that don't need file output."""

    async def write_state(self, message: str) -> None:
        return None

    async def append_event(self, message: str) -> None:
        return None
