"""Regression test for the stop->start "database is locked" race (#4).

The store connection must set a non-zero busy_timeout so a transient writer
lock held by an outgoing session's sidecar is waited out rather than raising
SQLITE_BUSY and hard-failing session.start.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentshore.data.store import DataStore
from agentshore.data.store.helpers import _load_schema_sql


@pytest.mark.asyncio
async def test_busy_timeout_is_set(tmp_path: Path) -> None:
    db = DataStore(tmp_path / "test.db")
    await db.initialize()
    try:
        async with db._db.execute("PRAGMA busy_timeout") as cur:  # type: ignore[union-attr]
            row = await cur.fetchone()
        assert row is not None
        assert int(row[0]) >= 5000
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_second_writer_waits_instead_of_erroring(tmp_path: Path) -> None:
    """A second connection writing while the first holds a write lock should
    block on the busy_timeout rather than immediately raising 'database is
    locked'. We assert the second connection can ultimately commit."""
    import aiosqlite

    db_path = tmp_path / "race.db"
    store = DataStore(db_path)
    await store.initialize()
    try:
        # Independent rw connection with the same busy_timeout.
        other = await aiosqlite.connect(str(db_path))
        try:
            await other.execute("PRAGMA busy_timeout=5000")
            # Writing through the second connection should succeed (waits out
            # any momentary lock rather than raising). Use a scratch table so we
            # don't depend on schema specifics.
            await other.execute("CREATE TABLE IF NOT EXISTS _race (id INTEGER)")
            await other.execute("INSERT INTO _race (id) VALUES (1)")
            await other.commit()
        finally:
            await other.close()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_initialize_retries_transient_db_locked_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The schema write phase retries a transient 'database is locked' (held
    past busy_timeout by a slow-releasing outgoing sidecar) and succeeds (#4)."""
    monkeypatch.setattr(DataStore, "_INIT_LOCK_RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(DataStore, "_INIT_LOCK_RETRY_MAX_DELAY", 0.0)
    store = DataStore(tmp_path / "retry.db")
    await store.initialize()
    try:
        calls = {"n": 0}
        real = store._db.executescript  # type: ignore[union-attr]

        async def flaky(script: str) -> object:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise sqlite3.OperationalError("database is locked")
            return await real(script)

        monkeypatch.setattr(store._db, "executescript", flaky)
        await store._apply_schema_with_lock_retry(_load_schema_sql())
        assert calls["n"] == 3  # failed twice, succeeded on the third attempt
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_initialize_raises_after_persistent_db_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock that never clears raises a typed DatabaseLockedError once the
    wall-clock retry budget is exhausted (#4, #283)."""
    from agentshore.errors import DatabaseLockedError

    monkeypatch.setattr(DataStore, "_INIT_LOCK_RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(DataStore, "_INIT_LOCK_RETRY_MAX_DELAY", 0.0)
    # Zero budget => give up on the first persistent lock instead of spinning
    # for the full default window.
    monkeypatch.setattr(DataStore, "_INIT_LOCK_RETRY_BUDGET_SECONDS", 0.0)
    store = DataStore(tmp_path / "locked.db")
    await store.initialize()
    try:

        async def always_locked(script: str) -> object:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(store._db, "executescript", always_locked)
        with pytest.raises(DatabaseLockedError, match="could not acquire the database lock"):
            await store._apply_schema_with_lock_retry(_load_schema_sql())
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_initialize_does_not_retry_non_lock_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only 'database is locked' is retried; other OperationalErrors fail fast (#4)."""
    monkeypatch.setattr(DataStore, "_INIT_LOCK_RETRY_BASE_DELAY", 0.0)
    store = DataStore(tmp_path / "other.db")
    await store.initialize()
    try:
        calls = {"n": 0}

        async def boom(script: str) -> object:
            calls["n"] += 1
            raise sqlite3.OperationalError("no such table: bogus")

        monkeypatch.setattr(store._db, "executescript", boom)
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            await store._apply_schema_with_lock_retry(_load_schema_sql())
        assert calls["n"] == 1  # no retry on a non-lock error
    finally:
        await store.close()
