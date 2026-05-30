"""DataStore — async SQLite access via aiosqlite.

The class is composed by inheriting one mixin per domain table-group.
Lifecycle (``initialize``, ``close``, ``wal_checkpoint``,
``reset_session_scoped_tables``) and schema-migration helpers live here
because they're tightly coupled to ``initialize``.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from typing import TYPE_CHECKING

import aiosqlite

from agentshore.data.migrations import migrate_v1_to_v2
from agentshore.data.store.base import _DataStoreBase
from agentshore.data.store.helpers import _load_schema_sql
from agentshore.data.store.mixins.agents import _AgentsMixin
from agentshore.data.store.mixins.archive import _ArchiveMixin
from agentshore.data.store.mixins.branch_activity import _BranchActivityMixin
from agentshore.data.store.mixins.external_mutations import _ExternalMutationsMixin
from agentshore.data.store.mixins.feedback import _FeedbackMixin
from agentshore.data.store.mixins.issues import _IssuesMixin
from agentshore.data.store.mixins.learnings import _LearningsMixin
from agentshore.data.store.mixins.plays import _PlaysMixin
from agentshore.data.store.mixins.pull_requests import _PullRequestsMixin
from agentshore.data.store.mixins.review_patterns import _ReviewPatternsMixin
from agentshore.data.store.mixins.reviews import _ReviewsMixin
from agentshore.data.store.mixins.rl import _RLMixin
from agentshore.data.store.mixins.scope import _ScopeMixin
from agentshore.data.store.mixins.sessions import _SessionsMixin
from agentshore.data.store.mixins.trajectory import _TrajectoryMixin
from agentshore.data.store.mixins.work_claims import _WorkClaimsMixin
from agentshore.errors import DatabaseError

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
    _LearningsMixin,
    _TrajectoryMixin,
    _ReviewPatternsMixin,
    _ArchiveMixin,
    _RLMixin,
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

    # -- lifecycle -----------------------------------------------------------

    _EXPECTED_SCHEMA_NAMESPACE = "agentshore_dev_v1"

    async def initialize(self) -> None:
        """Open the database connection and apply the schema."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        # desktop-#4: tolerate a transient writer-lock held by an outgoing
        # session's sidecar on a quick stop→start. Without this, the new
        # orchestrator's first-snapshot write raised "database is locked" and
        # hard-failed session.start; the old sidecar releases the WAL lock within
        # seconds, so waiting up to 5s absorbs the race instead of failing.
        await self._db.execute("PRAGMA busy_timeout=5000")
        # desktop-gkku: durability hardening for environments that defer
        # fsyncs (macOS screen-lock I/O throttling — desktop-tvsb).
        #   - synchronous=FULL forces F_FULLFSYNC on macOS, which the OS
        #     cannot defer even under aggressive power management. The
        #     2-3x commit slowdown is acceptable at our write volume.
        #   - wal_autocheckpoint=100 (vs the 1000-page default) closes
        #     the freshly-checkpointed-vs-main desync window 10x faster,
        #     reducing the surface area for any deferred-write surprise.
        await self._db.execute("PRAGMA synchronous=FULL")
        await self._db.execute("PRAGMA wal_autocheckpoint=100")
        schema_sql = _load_schema_sql()
        await self._apply_schema_with_lock_retry(schema_sql)

    # Bounded retry for the writer-lock race on a quick stop->start (#4).
    _INIT_LOCK_RETRY_ATTEMPTS = 5
    _INIT_LOCK_RETRY_BASE_DELAY = 0.5
    _INIT_LOCK_RETRY_MAX_DELAY = 4.0

    async def _apply_schema_with_lock_retry(self, schema_sql: str) -> None:
        """Apply schema + migrations, retrying a transient writer-lock (#4).

        ``busy_timeout`` (set above) already waits up to 5s per statement for
        the WAL writer-lock held by an outgoing session's sidecar on a quick
        stop->start. But when that sidecar releases slowly (its process is
        still being reaped and has not yet closed its DB FDs), the lock can
        persist past 5s and the first write raises "database is locked",
        hard-failing ``session.start`` at the first-snapshot step (#4).

        This bounded application-level retry re-attempts the whole write
        phase with exponential backoff, gating startup on lock availability
        instead of failing. The schema script (``CREATE ... IF NOT EXISTS``)
        and the migrations are individually idempotent, so re-running is
        safe. Because ``initialize()`` runs before the first state snapshot,
        succeeding here also frees the later snapshot write. Only the
        "database is locked" OperationalError is retried; every other error
        propagates immediately.
        """
        assert self._db is not None
        delay = self._INIT_LOCK_RETRY_BASE_DELAY
        for attempt in range(1, self._INIT_LOCK_RETRY_ATTEMPTS + 1):
            try:
                await self._db.executescript(schema_sql)
                await self._validate_schema_namespace()
                await self._apply_migrations()
                await self._db.commit()
                return
            except sqlite3.OperationalError as exc:
                if (
                    "database is locked" not in str(exc).lower()
                    or attempt == self._INIT_LOCK_RETRY_ATTEMPTS
                ):
                    raise
                import structlog

                structlog.get_logger(__name__).warning(
                    "store_init_db_locked_retry",
                    attempt=attempt,
                    max_attempts=self._INIT_LOCK_RETRY_ATTEMPTS,
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

    async def wal_checkpoint(self) -> None:
        """Merge WAL frames into the main DB file (passive checkpoint)."""
        if self._db is not None:
            with contextlib.suppress(Exception):
                await self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                await self._db.commit()

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
        # VACUUM INTO does not accept parameters via the prepared-stmt
        # interface; the destination is part of the SQL text. The path
        # comes from agentshore code (snapshot ring), never from user
        # input, so string interpolation is safe here.
        await self._db.execute(f"VACUUM INTO '{dest}'")

    async def reset_session_scoped_tables(self) -> None:
        """Truncate all session/repo-state tables at the start of a new session.

        Repo + GitHub are the source of truth. Stale rows (especially
        pull_requests.author_agent_id stamps from prior sessions) cause
        code-review anti-confirmation dead-locks. The GH cache refresh during
        bootstrap repopulates these tables from live GitHub data.

        Preserved tables (cross-session value):
            schema_info, schema_version, sessions, plays, rl_experience,
            session_archives, review_feedback_patterns
        """
        # Single executescript drives 14 DELETEs in one round-trip instead
        # of 14 individual await self._conn.execute() calls (GH #507 /
        # desktop-bbl). The DELETEs run inside the implicit transaction
        # executescript opens, and aiosqlite still flushes via commit
        # below. PRAGMA foreign_keys=OFF/ON wraps the truncation so we
        # don't fight FK constraints between, e.g., agents and
        # agent_handoffs.
        reset_script = """
            DELETE FROM agents;
            DELETE FROM github_issues;
            DELETE FROM pull_requests;
            DELETE FROM branch_activity;
            DELETE FROM work_claims;
            DELETE FROM review_queue;
            DELETE FROM external_mutations;
            DELETE FROM scope_drift_log;
            DELETE FROM policy_checkpoints;
            DELETE FROM agent_handoffs;
            DELETE FROM trajectory_snapshots;
            DELETE FROM human_feedback;
            DELETE FROM session_learnings;
        """
        await self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            await self._conn.executescript(reset_script)
            await self._conn.commit()
        finally:
            await self._conn.execute("PRAGMA foreign_keys=ON")

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

        import os

        # Drain any open implicit transaction before backup. A failed write
        # (e.g., UNIQUE-constraint IntegrityError) can leave aiosqlite's
        # connection inside a transaction; sqlite3.Connection.backup() then
        # deadlocks waiting for the lock to clear. rollback() is a no-op when
        # autocommit is already in effect.
        with contextlib.suppress(Exception):
            await self._db.rollback()

        tmp_path = self._db_path.with_suffix(self._db_path.suffix + ".tmp")
        # Best-effort cleanup: a stale tmp from a prior crash is fine to clobber.
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()

        backup_ok = False
        backup_error: Exception | None = None
        replace_error: Exception | None = None
        try:
            target = await aiosqlite.connect(str(tmp_path))
            try:
                await self._db.backup(target)
            finally:
                await target.close()
            backup_ok = True
        except (aiosqlite.Error, OSError) as exc:
            # Don't suppress — the orchestrator's shutdown_step logger surfaces
            # this as ``store_close_failed`` so the operator knows the snapshot
            # didn't land. Existing main DB file is left untouched.
            import structlog

            structlog.get_logger(__name__).warning(
                "store_close_backup_failed",
                error=str(exc),
                tmp_path=str(tmp_path),
            )
            backup_error = exc

        await self._db.close()
        self._db = None

        if backup_ok:
            try:
                os.replace(tmp_path, self._db_path)
            except OSError as exc:
                import structlog

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

    # -- schema validation ---------------------------------------------------

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
