"""DataStore provides async SQLite access through aiosqlite.

It inherits one mixin per domain table group. Lifecycle methods and schema
migration helpers live in this module."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import time
from typing import TYPE_CHECKING

import aiosqlite
import structlog

from agentshore.data.migrations import (
    migrate_v1_to_v2,
    migrate_v2_to_v3,
    migrate_v3_to_v4,
    migrate_v4_to_v5,
)
from agentshore.data.store.base import _DataStoreBase, _serialized
from agentshore.data.store.helpers import _load_schema_sql
from agentshore.data.store.mixins.agents import _AgentsMixin
from agentshore.data.store.mixins.archive import _ArchiveMixin
from agentshore.data.store.mixins.branch_activity import _BranchActivityMixin
from agentshore.data.store.mixins.external_mutations import _ExternalMutationsMixin
from agentshore.data.store.mixins.feedback import _FeedbackMixin
from agentshore.data.store.mixins.issues import _IssuesMixin
from agentshore.data.store.mixins.plays import _PlaysMixin
from agentshore.data.store.mixins.pull_requests import _PullRequestsMixin
from agentshore.data.store.mixins.review_patterns import _ReviewPatternsMixin
from agentshore.data.store.mixins.reviews import _ReviewsMixin
from agentshore.data.store.mixins.rl import _RLMixin
from agentshore.data.store.mixins.scope import _ScopeMixin
from agentshore.data.store.mixins.sessions import _SessionsMixin
from agentshore.data.store.mixins.trajectory import _TrajectoryMixin
from agentshore.data.store.mixins.work_claims import _WorkClaimsMixin
from agentshore.data.store.mixins.worktrees import _WorktreesMixin
from agentshore.errors import DatabaseError, DatabaseLockedError

if TYPE_CHECKING:
    from pathlib import Path


class DataStore(
    _SessionsMixin,
    _PlaysMixin,
    _AgentsMixin,
    _IssuesMixin,
    _PullRequestsMixin,
    _BranchActivityMixin,
    _ReviewsMixin,
    _WorkClaimsMixin,
    _ExternalMutationsMixin,
    _ScopeMixin,
    _FeedbackMixin,
    _TrajectoryMixin,
    _ReviewPatternsMixin,
    _ArchiveMixin,
    _RLMixin,
    _WorktreesMixin,
    _DataStoreBase,
):
    """Async SQLite data store for all AgentShore persistence.

    Usage::

        store = DataStore(db_path)
        await store.initialize()
        try:
            ...
        finally:
            await store.close()
    """

    def __init__(self, db_path: Path) -> None:
        _DataStoreBase.__init__(self, db_path)

    _EXPECTED_SCHEMA_NAMESPACE = "agentshore_dev_v1"

    async def initialize(self) -> None:
        """Open the database connection and apply the schema."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        init_ok = False
        try:
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA foreign_keys=ON")
            await self._db.execute("PRAGMA busy_timeout=5000")
            await self._db.execute("PRAGMA synchronous=FULL")
            await self._db.execute("PRAGMA wal_autocheckpoint=100")
            schema_sql = _load_schema_sql()
            await self._apply_schema_with_lock_retry(schema_sql)
            init_ok = True
        finally:
            if not init_ok:
                db = self._db
                self._db = None
                if db is not None:
                    with contextlib.suppress(Exception):
                        await db.close()

    _INIT_LOCK_RETRY_BUDGET_SECONDS = 45.0
    _INIT_LOCK_RETRY_BASE_DELAY = 0.5
    _INIT_LOCK_RETRY_MAX_DELAY = 4.0

    async def _apply_schema_with_lock_retry(self, schema_sql: str) -> None:
        """Apply schema + migrations, retrying a transient writer-lock (#4, #283).

        ``busy_timeout`` (set above) already waits up to 5s per statement for
        the WAL writer-lock held by an outgoing session's sidecar on a quick
        stop->start. But when that sidecar releases slowly (its process is
        still being reaped and has not yet closed its DB FDs), the lock can
        persist past 5s and the first write raises "database is locked",
        hard-failing ``session.start`` at the first-snapshot step (#4).

        This application-level retry re-attempts the whole write phase with
        exponential backoff, gating startup on lock availability instead of
        failing, until the wall-clock budget is exhausted. The schema script
        (``CREATE ... IF NOT EXISTS``) and the migrations are individually
        idempotent, so re-running is safe. Because ``initialize()`` runs before
        the first state snapshot, succeeding here also frees the later snapshot
        write. Only the "database is locked" OperationalError is retried; every
        other error propagates immediately. On budget exhaustion a typed
        :class:`DatabaseLockedError` is raised so the caller can render an
        actionable "database busy, retry" message (#283) rather than a bare
        ``OperationalError`` string.
        """
        assert self._db is not None
        delay = self._INIT_LOCK_RETRY_BASE_DELAY
        deadline = time.monotonic() + self._INIT_LOCK_RETRY_BUDGET_SECONDS
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._db.executescript(schema_sql)
                await self._validate_schema_namespace()
                await self._apply_migrations()
                await self._db.commit()
                return
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc).lower():
                    raise
                if time.monotonic() + delay >= deadline:
                    raise DatabaseLockedError(
                        "store init could not acquire the database lock within "
                        f"{self._INIT_LOCK_RETRY_BUDGET_SECONDS:.0f}s "
                        f"({attempt} attempts) — a previous session may still be "
                        "shutting down; retry in a moment"
                    ) from exc
                structlog.get_logger(__name__).warning(
                    "store_init_db_locked_retry",
                    attempt=attempt,
                    budget_seconds=self._INIT_LOCK_RETRY_BUDGET_SECONDS,
                    delay_seconds=delay,
                    db_path=str(self._db_path),
                    error=str(exc),
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._INIT_LOCK_RETRY_MAX_DELAY)

    async def _apply_migrations(self) -> None:
        """Apply forward-only schema migrations to an existing database.

        The baseline ``schema.sql`` script is idempotent (every statement
        is ``CREATE ... IF NOT EXISTS``) and reflects the current schema for
        a fresh database. Migrations carry pre-existing databases forward by
        applying the incremental DDL that the baseline no longer contains.
        Each migration is individually idempotent, so re-running
        ``initialize()`` against an already-migrated database is safe.
        """
        await migrate_v1_to_v2(self._conn)
        await migrate_v2_to_v3(self._conn)
        await migrate_v3_to_v4(self._conn)
        await migrate_v4_to_v5(self._conn)

    @_serialized
    async def wal_checkpoint(self) -> None:
        """Merge WAL frames into the main DB file (passive checkpoint)."""
        if self._db is not None:
            with contextlib.suppress(Exception):
                await self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                await self._db.commit()

    @_serialized
    async def integrity_check(self) -> tuple[bool, list[str]]:
        """Run ``PRAGMA quick_check`` against the live connection.

        Returns ``(True, [])`` when the connection's view is intact, or
        ``(False, [error_lines])`` when SQLite reports B-tree damage.
        See ``agentshore.data.integrity`` (desktop-jc7p) for the periodic
        canary that calls this and the auto-restore that triggers when
        a bad result lands at startup.
        """
        if self._db is None:
            return False, ["connection not initialized"]
        async with self._db.execute("PRAGMA quick_check") as cur:
            rows = await cur.fetchall()
        lines = [row[0] for row in rows if row and row[0]]
        if lines == ["ok"]:
            return True, []
        return False, lines

    @_serialized
    async def snapshot_to(self, dest: Path) -> None:
        """Write a fresh ``VACUUM INTO`` copy at *dest*.

        VACUUM INTO traverses the B-trees logically and writes a new
        file with a fresh page layout, so the snapshot reflects the
        running connection's consistent view even when the on-disk
        main file has stale pages. SQLite requires the destination
        not exist, so we unlink first.
        """
        if self._db is None:
            msg = "DataStore.snapshot_to called before initialize()"
            raise RuntimeError(msg)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        escaped_dest = str(dest).replace("'", "''")
        await self._db.execute(f"VACUUM INTO '{escaped_dest}'")

    _RESET_META_TABLES = frozenset({"schema_info", "schema_version"})
    _RESET_PRESERVED_TABLES = frozenset(
        {
            "sessions",
            "plays",
            "rl_experience",
            "session_archives",
            "review_feedback_patterns",
        }
    )

    @_serialized
    async def reset_session_scoped_tables(self) -> None:
        """Truncate all session/repo-state tables at the start of a new session.

        Repo + GitHub are the source of truth. Stale rows (especially
        pull_requests.author_agent_id stamps from prior sessions) cause
        code-review anti-confirmation dead-locks. The GH cache refresh during
        bootstrap repopulates these tables from live GitHub data.

        The delete set is *derived* — every base table in the schema that is
        neither a meta table (``_RESET_META_TABLES``) nor cross-session value
        (``_RESET_PRESERVED_TABLES``) is truncated. Deriving it (instead of
        hand-listing the DELETEs) means a newly added session-scoped table is
        wiped automatically rather than silently retaining stale rows across
        sessions — the exact drift that previously left ``dispatch_replay`` and
        ``worktrees`` un-reset.

        Preserved tables (cross-session value): sessions, plays, rl_experience,
        session_archives, review_feedback_patterns. Meta tables (untouched):
        schema_info, schema_version.
        """
        async with self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ) as cursor:
            all_tables = {row["name"] for row in await cursor.fetchall()}
        to_delete = all_tables - self._RESET_PRESERVED_TABLES - self._RESET_META_TABLES
        unknown_preserved = self._RESET_PRESERVED_TABLES - all_tables
        unknown_meta = self._RESET_META_TABLES - all_tables
        if unknown_preserved or unknown_meta:
            msg = (
                "reset_session_scoped_tables partition references tables absent "
                f"from the schema: preserved={sorted(unknown_preserved)}, "
                f"meta={sorted(unknown_meta)}"
            )
            raise DatabaseError(msg)
        reset_script = "".join(f"DELETE FROM {name};\n" for name in sorted(to_delete))
        await self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            await self._conn.executescript(reset_script)
            await self._conn.commit()
        finally:
            await self._conn.execute("PRAGMA foreign_keys=ON")

    @_serialized
    async def close(self) -> None:
        """Snapshot the live DB to a sibling file via the SQLite Online Backup
        API, atomically replace the main DB file with the snapshot, then close.

        Why not ``PRAGMA wal_checkpoint(TRUNCATE)``: a session running
        concurrently with an external ``sqlite3`` reader that issues its own
        TRUNCATE checkpoint can leave the main DB file truncated to 0 bytes
        even when the orchestrator's own close path runs cleanly. The Online
        Backup API produces a self-consistent snapshot regardless of WAL
        state, and the atomic ``os.replace`` ensures the main file is either
        the previous good copy or the new snapshot — never empty.
        """
        if self._db is None:
            return

        with contextlib.suppress(Exception):
            await self._db.rollback()

        tmp_path = self._db_path.with_suffix(self._db_path.suffix + ".tmp")
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()

        backup_ok = False
        backup_error: Exception | None = None
        replace_error: Exception | None = None
        try:
            try:
                target = await aiosqlite.connect(str(tmp_path))
                try:
                    await self._db.backup(target)
                finally:
                    with contextlib.suppress(Exception):
                        await target.close()
                backup_ok = True
            except (aiosqlite.Error, OSError) as exc:
                structlog.get_logger(__name__).warning(
                    "store_close_backup_failed",
                    error=str(exc),
                    tmp_path=str(tmp_path),
                )
                backup_error = exc
        finally:
            db = self._db
            self._db = None
            if db is not None:
                with contextlib.suppress(Exception):
                    await db.close()

        if backup_ok:
            try:
                os.replace(tmp_path, self._db_path)
            except OSError as exc:
                structlog.get_logger(__name__).warning(
                    "store_close_replace_failed",
                    error=str(exc),
                    tmp_path=str(tmp_path),
                    db_path=str(self._db_path),
                )
                replace_error = exc
        if backup_error is not None:
            raise DatabaseError(
                f"database backup failed during close: {backup_error}"
            ) from backup_error
        if replace_error is not None:
            raise DatabaseError(
                f"database backup replace failed during close: {replace_error}"
            ) from replace_error

    async def _validate_schema_namespace(self) -> None:
        """Verify the database belongs to the agentshore_dev_v1 schema generation."""
        async with self._conn.execute(
            "SELECT value FROM schema_info WHERE key = 'schema_namespace'"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise DatabaseError(
                "database is missing schema_info.schema_namespace — "
                "AgentShore requires a fresh database."
            )
        actual = row["value"]
        if actual != self._EXPECTED_SCHEMA_NAMESPACE:
            raise DatabaseError(
                f"schema namespace mismatch: expected "
                f"{self._EXPECTED_SCHEMA_NAMESPACE!r}, found {actual!r}"
            )
