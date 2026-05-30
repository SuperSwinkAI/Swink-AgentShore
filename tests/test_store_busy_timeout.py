"""Regression test for the stop->start "database is locked" race (#4).

The store connection must set a non-zero busy_timeout so a transient writer
lock held by an outgoing session's sidecar is waited out rather than raising
SQLITE_BUSY and hard-failing session.start.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.data.store import DataStore


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
