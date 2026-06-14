"""SQLite integrity canary, hot snapshots, and auto-restore (desktop-jc7p).

Four-layer defense against silent corruption — independent of root cause.

Layer 1 — Canary
    Periodic ``PRAGMA quick_check`` against the running connection. On
    failure we log a ``db_integrity_failed`` event with the affected
    trees and trigger an immediate snapshot rotation so we have a
    pre-failure image to restore from.

Layer 2 — Hot snapshots
    Periodic ``VACUUM INTO`` writes a fresh, logically-traversed copy
    next to the main DB. The snapshots form a ring of ``ring_size``
    files. VACUUM INTO reads through the B-trees, so the snapshot
    captures the running connection's consistent view even when the
    on-disk main file is dirty.

Layer 3 — Snapshot auto-restore
    ``restore_from_snapshot_ring`` runs synchronously at bootstrap
    before the orchestrator opens its connection. If the main DB fails
    ``quick_check`` it walks the ring newest-to-oldest, picks the first
    snapshot that passes, atomically renames it into place, and
    preserves the corrupt file as ``agentshore.db.corrupt.<ts>`` for
    post-mortem.

Layer 4 — sqlite3 .recover salvage + fresh fallback
    When the snapshot ring is empty (or every snapshot is also corrupt)
    ``recover_via_sqlite_recover`` pipes ``sqlite3 corrupt .recover``
    through a fresh sqlite3 process to rebuild a DB from whatever rows
    still sit on readable pages. If recovery itself fails, the corrupt
    file is moved aside and the caller's ``store.initialize()`` creates
    a fresh empty DB so the session can boot. Session history is lost
    only in the worst case; weights, learnings, and project state live
    outside ``agentshore.db`` and are unaffected.

See desktop-tvsb for the canonical macOS screen-lock root cause and
desktop-gkku for the in-process power-assertion / synchronous=FULL
prevention that complements this safety net.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import subprocess
import time
from typing import TYPE_CHECKING, Protocol

from agentshore.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

_logger = get_logger(__name__)

_SNAPSHOT_PREFIX = "agentshore.db.snapshot."
_CORRUPT_PREFIX = "agentshore.db.corrupt."
_RECOVERED_SUFFIX = ".recovered"
_RECOVERY_TIMEOUT_SECONDS = 60.0


class _IntegrityCapable(Protocol):
    """The slice of DataStore that IntegrityMonitor depends on."""

    async def integrity_check(self) -> tuple[bool, list[str]]: ...

    async def snapshot_to(self, dest: Path) -> None: ...

    async def wal_checkpoint(self) -> None: ...


def _snapshot_path(snapshots_dir: Path, idx: int) -> Path:
    return snapshots_dir / f"{_SNAPSHOT_PREFIX}{idx}"


def _list_existing_snapshots(snapshots_dir: Path) -> list[Path]:
    """Return existing snapshot files sorted newest-first (by mtime)."""
    if not snapshots_dir.exists():
        return []
    candidates = [p for p in snapshots_dir.iterdir() if p.name.startswith(_SNAPSHOT_PREFIX)]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates


def _check_sqlite_file(path: Path) -> tuple[bool, list[str]]:
    """Run ``PRAGMA quick_check`` on a closed file via stdlib ``sqlite3``.

    Returns ``(ok, error_lines)``. Used at startup before the async
    DataStore connection exists.
    """
    if not path.exists() or path.stat().st_size == 0:
        return False, ["file missing or empty"]
    try:
        conn = sqlite3.connect(str(path))
    except sqlite3.Error as exc:
        return False, [f"sqlite3.connect failed: {exc}"]
    try:
        rows = conn.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as exc:
        return False, [f"quick_check raised: {exc}"]
    finally:
        conn.close()
    lines = [row[0] for row in rows if row and row[0]]
    if lines == ["ok"]:
        return True, []
    return False, lines


def recover_via_sqlite_recover(
    corrupt_path: Path,
    dest_path: Path,
    *,
    timeout_seconds: float = _RECOVERY_TIMEOUT_SECONDS,
) -> tuple[bool, list[str]]:
    """Pipe ``sqlite3 corrupt_path .recover`` into a fresh DB at ``dest_path``.

    Returns ``(ok, errors)`` where ``ok=True`` iff the resulting file
    passes ``PRAGMA quick_check``. ``.recover`` walks the B-trees defensively
    and emits a SQL dump of the salvageable rows; piping it into a new
    sqlite3 process rebuilds a coherent DB from those rows. Suitable when
    no clean snapshot exists but the corrupt file's pages are partially
    readable.

    Synchronous (stdlib subprocess) — runs at bootstrap before any async
    DataStore exists. Returns failure modes:
    - ``sqlite3`` CLI not on PATH
    - Either subprocess times out
    - ``.recover`` produces no output
    - Resulting DB still fails ``quick_check``

    The caller is responsible for swapping ``dest_path`` into the main
    slot if recovery succeeds.
    """
    if dest_path.exists():
        dest_path.unlink()

    try:
        dump = subprocess.run(  # noqa: S603, S607
            ["sqlite3", str(corrupt_path), ".recover"],
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            # Never inherit the sidecar's stdin (the live Tauri JSON-RPC pipe);
            # a subprocess probing it can wedge the caller (#155).
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False, ["sqlite3 CLI not found on PATH"]
    except subprocess.TimeoutExpired:
        return False, [f".recover dump timed out after {timeout_seconds}s"]
    except OSError as exc:
        return False, [f".recover dump OSError: {exc}"]

    if not dump.stdout:
        stderr_snippet = dump.stderr.decode("utf-8", errors="replace")[:300] if dump.stderr else ""
        return False, [
            f".recover produced no output (rc={dump.returncode}); stderr={stderr_snippet}"
        ]

    try:
        load = subprocess.run(  # noqa: S603, S607
            ["sqlite3", str(dest_path)],
            input=dump.stdout,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, [f".recover load timed out after {timeout_seconds}s"]
    except OSError as exc:
        return False, [f".recover load OSError: {exc}"]

    if load.returncode != 0:
        # Partial-failure load may still be useful — validate the result rather
        # than bailing on nonzero exit. SQLite returns nonzero on any INSERT
        # error but most rows can still land.
        _logger.warning(
            "db_recovery_load_returned_nonzero",
            returncode=load.returncode,
            stderr=load.stderr.decode("utf-8", errors="replace")[:300] if load.stderr else "",
        )

    return _check_sqlite_file(dest_path)


def restore_from_snapshot_ring(db_path: Path, snapshots_dir: Path) -> Path | None:
    """Synchronously check the main DB; recover if corrupt.

    Recovery ladder, tried in order:

    1. Main DB passes ``quick_check`` — no-op, returns ``None``.
    2. Newest-first snapshot from the ring that passes ``quick_check`` is
       copied into the main slot. Corrupt file preserved as
       ``agentshore.db.corrupt.<ts>``. Returns the snapshot path.
    3. No clean snapshot — ``sqlite3 .recover`` salvages rows from the
       corrupt file into a fresh DB which is swapped into the main slot.
       Corrupt file preserved. Returns the recovered path.
    4. Recovery fails — corrupt file is moved aside as
       ``agentshore.db.corrupt.<ts>`` so the caller's ``store.initialize()``
       creates a fresh empty DB with the current schema. Returns ``None``.

    Runs *before* the DataStore connection is opened, so it uses stdlib
    ``sqlite3`` (synchronous) rather than aiosqlite.
    """
    ok, errors = _check_sqlite_file(db_path)
    if ok:
        return None

    # File doesn't exist at all — first-run scenario, let store.initialize()
    # create the schema fresh. Don't waste cycles on recovery of a missing file.
    if not db_path.exists():
        return None

    # Capture corruption evidence before any recovery attempt — best-effort,
    # never raises. The dict is emitted as a single structured log event so
    # post-mortems can correlate downstream symptoms back to the moment of
    # detection via the embedded ``corruption_event_id``.
    try:
        from agentshore.data.corruption_evidence import capture_corruption_evidence

        evidence = capture_corruption_evidence(db_path)
        _logger.error(
            "db_corruption_evidence_captured",
            site="restore_from_snapshot_ring",
            **evidence,
        )
    except Exception as exc:  # noqa: BLE001 — evidence capture must not raise
        _logger.warning("db_corruption_evidence_capture_failed", error=str(exc))

    # 1. Try snapshot ring.
    candidates = _list_existing_snapshots(snapshots_dir)
    chosen: Path | None = None
    for snap in candidates:
        snap_ok, _ = _check_sqlite_file(snap)
        if snap_ok:
            chosen = snap
            break

    if chosen is not None:
        corrupt_dest = db_path.with_name(f"{_CORRUPT_PREFIX}{int(time.time())}")
        # Preserve the corrupt file via atomic rename so post-mortem tools
        # (``sqlite3 .recover``, page dumps) can still inspect it.
        if db_path.exists():
            os.replace(db_path, corrupt_dest)
        # Copy the snapshot into the main slot. We *copy* rather than rename
        # so the chosen snapshot stays in the ring as a known-good fallback
        # if the restore itself proves bad on first write.
        shutil.copy2(chosen, db_path)
        _logger.warning(
            "db_auto_restored",
            db_path=str(db_path),
            snapshot=chosen.name,
            corrupt_preserved_as=corrupt_dest.name,
            original_errors=errors,
        )
        return chosen

    # 2. Snapshot ring is empty or every snapshot is also corrupt — try
    #    ``sqlite3 .recover`` to salvage what we can from the live file.
    _logger.warning(
        "db_attempting_sqlite_recover",
        db_path=str(db_path),
        integrity_errors=errors,
        ring_size=len(candidates),
    )
    recovered_path = db_path.with_name(db_path.name + _RECOVERED_SUFFIX)
    recovery_ok, recovery_errors = recover_via_sqlite_recover(db_path, recovered_path)

    if recovery_ok:
        corrupt_dest = db_path.with_name(f"{_CORRUPT_PREFIX}{int(time.time())}")
        if db_path.exists():
            os.replace(db_path, corrupt_dest)
        os.replace(recovered_path, db_path)
        _logger.warning(
            "db_recovered_via_sqlite_recover",
            db_path=str(db_path),
            corrupt_preserved_as=corrupt_dest.name,
            original_errors=errors,
        )
        return db_path

    # 3. Recovery failed. Move the corrupt file aside so the caller's
    #    ``store.initialize()`` creates a fresh empty DB instead of crashing
    #    on the malformed pages. Session history is lost but the user gets a
    #    working session — weights, learnings.json, reports, contexts/, and
    #    project state (git, GitHub, beads) all live outside agentshore.db and
    #    are untouched.
    if recovered_path.exists():
        recovered_path.unlink()
    corrupt_dest = db_path.with_name(f"{_CORRUPT_PREFIX}{int(time.time())}")
    if db_path.exists():
        os.replace(db_path, corrupt_dest)
    _logger.error(
        "db_unrecoverable_starting_fresh",
        db_path=str(db_path),
        integrity_errors=errors,
        recovery_errors=recovery_errors,
        ring_size=len(candidates),
        corrupt_preserved_as=corrupt_dest.name,
    )
    return None


class IntegrityMonitor:
    """Background asyncio task running the canary + snapshot ring.

    Mirrors the ``HealthMonitor`` lifecycle: ``start()`` spawns the
    task, ``stop()`` cancels it, ``is_running`` reports state.
    """

    def __init__(
        self,
        store: _IntegrityCapable,
        snapshots_dir: Path,
        *,
        db_path: Path | None = None,
        canary_interval_seconds: float = 300.0,
        snapshot_interval_seconds: float = 300.0,
        snapshot_ring_size: int = 3,
        wal_checkpoint_interval_seconds: float = 30.0,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._snapshots_dir = snapshots_dir
        self._db_path = db_path
        self._canary_interval = canary_interval_seconds
        self._snapshot_interval = snapshot_interval_seconds
        self._ring_size = max(1, snapshot_ring_size)
        self._wal_checkpoint_interval = wal_checkpoint_interval_seconds
        self._sleep = sleep_fn or asyncio.sleep
        self._now = time_fn or time.monotonic
        self._task: asyncio.Task[None] | None = None
        self._next_snapshot_idx = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="agentshore.integrity_monitor"
            )

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        # Wake-up granularity = the smallest configured interval, so we
        # don't oversleep any of the three timers.
        tick = max(
            1.0,
            min(
                self._canary_interval,
                self._snapshot_interval,
                self._wal_checkpoint_interval,
            ),
        )
        last_canary = self._now()
        last_snapshot = self._now()
        last_checkpoint = self._now()
        _logger.info(
            "integrity_monitor_started",
            canary_interval_s=self._canary_interval,
            snapshot_interval_s=self._snapshot_interval,
            wal_checkpoint_interval_s=self._wal_checkpoint_interval,
            ring_size=self._ring_size,
        )
        try:
            while True:
                await self._sleep(tick)
                now = self._now()
                if now - last_canary >= self._canary_interval:
                    last_canary = now
                    await self._run_canary()
                if now - last_snapshot >= self._snapshot_interval:
                    last_snapshot = now
                    await self._rotate_snapshot()
                if now - last_checkpoint >= self._wal_checkpoint_interval:
                    last_checkpoint = now
                    await self._run_wal_checkpoint()
        except asyncio.CancelledError:
            _logger.info("integrity_monitor_stopped")
            raise

    async def _run_wal_checkpoint(self) -> None:
        try:
            await self._store.wal_checkpoint()
        except Exception as exc:
            _logger.warning(
                "wal_checkpoint_passive_failed",
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            return
        _logger.debug("wal_checkpoint_passive")

    async def _run_canary(self) -> None:
        try:
            ok, errors = await self._store.integrity_check()
        except Exception as exc:
            _logger.warning(
                "db_integrity_check_raised", error=str(exc), exc_type=type(exc).__name__
            )
            return
        if ok:
            _logger.debug("db_integrity_check", status="ok")
            return
        _logger.error("db_integrity_failed", errors=errors)
        # Capture surrounding system state so the next post-mortem can attribute
        # the corruption to its root cause. Best-effort; never raises.
        if self._db_path is not None:
            try:
                from agentshore.data.corruption_evidence import (
                    capture_corruption_evidence,
                )

                evidence = capture_corruption_evidence(self._db_path)
                _logger.error(
                    "db_corruption_evidence_captured",
                    site="canary",
                    **evidence,
                )
            except Exception as exc:  # noqa: BLE001 — evidence capture must not raise
                _logger.warning("db_corruption_evidence_capture_failed", error=str(exc))
        # Failure → take an immediate snapshot so the next startup has a
        # fresh known-good image to restore from.
        await self._rotate_snapshot()

    async def _rotate_snapshot(self) -> None:
        dest = _snapshot_path(self._snapshots_dir, self._next_snapshot_idx)
        try:
            await self._store.snapshot_to(dest)
        except Exception as exc:
            _logger.warning(
                "db_snapshot_failed",
                idx=self._next_snapshot_idx,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            return
        _logger.info(
            "db_snapshot_written",
            idx=self._next_snapshot_idx,
            path=str(dest),
            bytes=dest.stat().st_size if dest.exists() else 0,
        )
        self._next_snapshot_idx = (self._next_snapshot_idx + 1) % self._ring_size


__all__ = [
    "IntegrityMonitor",
    "recover_via_sqlite_recover",
    "restore_from_snapshot_ring",
]
