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
import tempfile
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

_logger = structlog.get_logger(__name__)

STATE_FILENAME = "dashboard_state.json"
EVENTS_FILENAME = "dashboard_events.ndjson"

# Rotation: when the events file exceeds ``_EVENTS_ROTATE_BYTES``, keep
# only the trailing ``_EVENTS_ROTATE_KEEP_BYTES`` (rounded to a line
# boundary). Historical events are recoverable from agentshore.db; the file
# is a tail consumed by the dashboard, so unbounded retention is wasted.
_EVENTS_ROTATE_BYTES = 5 * 1024 * 1024
_EVENTS_ROTATE_KEEP_BYTES = 1 * 1024 * 1024


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
        self._reset_session_files()

    def _reset_session_files(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._state_path.unlink()
        with contextlib.suppress(FileNotFoundError):
            self._events_path.unlink()

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
            os.replace(tmp_path, self._state_path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
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
            os.replace(tmp_path, self._events_path)
        except OSError as exc:
            _logger.warning("state_writer.rotate_write_failed", error=str(exc))


class NullStateWriter:
    """No-op writer for tests / providers that don't need file output."""

    async def write_state(self, message: str) -> None:
        return None

    async def append_event(self, message: str) -> None:
        return None
