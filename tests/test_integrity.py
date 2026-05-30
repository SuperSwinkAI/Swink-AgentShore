"""Tests for the SQLite integrity canary, hot snapshots, and auto-restore.

Covers desktop-jc7p's acceptance criteria:
  - quick_check on a clean live connection returns ok
  - VACUUM INTO writes a usable snapshot file
  - Snapshot ring rotates through ring_size slots
  - Auto-restore picks the newest viable snapshot and preserves the
    corrupt original as agentshore.db.corrupt.<ts>
  - On schedule, the IntegrityMonitor fires canary + snapshot
  - Canary failure triggers an immediate extra snapshot
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from agentshore.data.integrity import (
    IntegrityMonitor,
    recover_via_sqlite_recover,
    restore_from_snapshot_ring,
)
from agentshore.data.store import DataStore, SessionRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_store(tmp_path: Path, name: str = "agentshore.db") -> DataStore:
    store = DataStore(tmp_path / name)
    await store.initialize()
    # Insert one row so we have actual user-data pages to corrupt later.
    await store.create_session(
        SessionRecord(
            session_id="s1",
            project_path=str(tmp_path),
            started_at="2026-01-01T00:00:00+00:00",
        )
    )
    return store


def _corrupt_file(path: Path, *, offset: int = 4096, n_bytes: int = 256) -> None:
    """Overwrite a span of bytes in a closed SQLite file.

    Default ``offset=4096`` lands inside the second page (page 1 is the
    DB header — corrupting it makes SQLite refuse to open at all, which
    also exercises the restore path but is less interesting than a
    structural-but-openable corruption).
    """
    with path.open("r+b") as fh:
        fh.seek(offset)
        fh.write(b"\xff" * n_bytes)


# ---------------------------------------------------------------------------
# DataStore methods
# ---------------------------------------------------------------------------


async def test_integrity_check_returns_ok_for_clean_db(tmp_path: Path) -> None:
    store = await _make_store(tmp_path)
    try:
        ok, errors = await store.integrity_check()
        assert ok is True
        assert errors == []
    finally:
        await store.close()


async def test_snapshot_to_writes_valid_file(tmp_path: Path) -> None:
    store = await _make_store(tmp_path)
    dest = tmp_path / "snap.db"
    try:
        await store.snapshot_to(dest)
    finally:
        await store.close()
    assert dest.exists() and dest.stat().st_size > 0
    # The snapshot is a real SQLite DB and quick_check passes on it.
    conn = sqlite3.connect(str(dest))
    try:
        rows = conn.execute("PRAGMA quick_check").fetchall()
        assert rows == [("ok",)]
        # The row we inserted is present in the snapshot.
        cur = conn.execute("SELECT session_id FROM sessions WHERE session_id=?", ("s1",))
        assert cur.fetchone() == ("s1",)
    finally:
        conn.close()


async def test_snapshot_to_overwrites_existing_dest(tmp_path: Path) -> None:
    store = await _make_store(tmp_path)
    dest = tmp_path / "snap.db"
    dest.write_bytes(b"stale-content")
    try:
        await store.snapshot_to(dest)
    finally:
        await store.close()
    assert dest.read_bytes()[:16].startswith(b"SQLite format 3")


# ---------------------------------------------------------------------------
# restore_from_snapshot_ring
# ---------------------------------------------------------------------------


async def test_restore_is_noop_when_main_db_is_clean(tmp_path: Path) -> None:
    store = await _make_store(tmp_path)
    await store.close()
    chosen = restore_from_snapshot_ring(tmp_path / "agentshore.db", tmp_path)
    assert chosen is None


async def test_restore_swaps_in_newest_clean_snapshot(tmp_path: Path) -> None:
    # 1. Build a clean DB and take a snapshot via the DataStore method.
    store = await _make_store(tmp_path)
    snap = tmp_path / "agentshore.db.snapshot.0"
    await store.snapshot_to(snap)
    await store.close()

    db_path = tmp_path / "agentshore.db"
    # 2. Corrupt the main file.
    _corrupt_file(db_path)
    # 3. Run restore.
    chosen = restore_from_snapshot_ring(db_path, tmp_path)
    assert chosen == snap
    # 4. The corrupt original was preserved alongside.
    corrupts = list(tmp_path.glob("agentshore.db.corrupt.*"))
    assert len(corrupts) == 1
    # 5. The new main file passes quick_check.
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    finally:
        conn.close()
    # 6. The snapshot itself is preserved as a fallback (we copy, not move).
    assert snap.exists()


async def test_restore_skips_corrupt_snapshots_and_picks_newer(tmp_path: Path) -> None:
    store = await _make_store(tmp_path)
    older = tmp_path / "agentshore.db.snapshot.0"
    newer = tmp_path / "agentshore.db.snapshot.1"
    await store.snapshot_to(older)
    await store.snapshot_to(newer)
    # Make the newer one a fresher mtime AND corrupt it; older is clean.
    _corrupt_file(newer)
    # Bump older's mtime above newer's so the sort puts older first.
    # (We actually want newer-first sort to pick newer, fall through to older.)
    # Make newer "newer" in mtime but corrupt → restore should fall through to older.
    now = time.time()
    import os

    os.utime(older, (now - 10, now - 10))
    os.utime(newer, (now, now))
    await store.close()

    db_path = tmp_path / "agentshore.db"
    _corrupt_file(db_path)
    chosen = restore_from_snapshot_ring(db_path, tmp_path)
    assert chosen == older  # newer was corrupt → fell through


async def test_restore_falls_through_to_recovery_when_no_clean_snapshot(
    tmp_path: Path,
) -> None:
    """When every snapshot is corrupt, ``.recover`` salvages the main DB."""
    store = await _make_store(tmp_path)
    snap = tmp_path / "agentshore.db.snapshot.0"
    await store.snapshot_to(snap)
    await store.close()

    db_path = tmp_path / "agentshore.db"
    _corrupt_file(db_path)
    _corrupt_file(snap)
    chosen = restore_from_snapshot_ring(db_path, tmp_path)
    # Recovery path returns the main db_path itself (not the snapshot) since
    # the recovered file is swapped in atomically.
    assert chosen == db_path
    # The DB at the main slot now passes quick_check.
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("PRAGMA quick_check").fetchall()
        assert [r[0] for r in rows] == ["ok"]
    finally:
        conn.close()
    # The corrupt original is preserved for post-mortem.
    corrupts = sorted(tmp_path.glob("agentshore.db.corrupt.*"))
    assert len(corrupts) == 1


# ---------------------------------------------------------------------------
# IntegrityMonitor scheduling
# ---------------------------------------------------------------------------


class _FakeStore:
    """Minimal store stub for monitor scheduling tests."""

    def __init__(self, *, fail_check: bool = False) -> None:
        self.check_calls = 0
        self.snapshot_calls: list[Path] = []
        self.wal_checkpoint_calls = 0
        self._fail_check = fail_check

    async def integrity_check(self) -> tuple[bool, list[str]]:
        self.check_calls += 1
        if self._fail_check:
            return False, ["row out of order"]
        return True, []

    async def snapshot_to(self, dest: Path) -> None:
        self.snapshot_calls.append(dest)
        # Make the file exist so the monitor's stat() doesn't blow up.
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"snap")

    async def wal_checkpoint(self) -> None:
        self.wal_checkpoint_calls += 1


async def test_monitor_fires_canary_and_snapshot_on_schedule(tmp_path: Path) -> None:
    fake = _FakeStore()
    now = [0.0]

    async def fake_sleep(s: float) -> None:
        now[0] += s
        # Yield so the test loop below regains control and can inspect
        # state / cancel; without this the monitor's tight loop would
        # starve the event loop.
        await asyncio.sleep(0)

    monitor = IntegrityMonitor(
        fake,
        tmp_path,
        canary_interval_seconds=10.0,
        snapshot_interval_seconds=20.0,
        snapshot_ring_size=3,
        sleep_fn=fake_sleep,
        time_fn=lambda: now[0],
    )
    monitor.start()
    # Wait until the schedule has reached canary fire #2 and snapshot #1.
    for _ in range(50):
        await asyncio.sleep(0)
        if fake.check_calls >= 2 and len(fake.snapshot_calls) >= 1:
            break
    monitor.stop()
    with pytest.raises(asyncio.CancelledError):
        await monitor._task  # type: ignore[union-attr]

    assert fake.check_calls >= 2
    assert len(fake.snapshot_calls) >= 1


async def test_monitor_takes_extra_snapshot_on_canary_failure(tmp_path: Path) -> None:
    fake = _FakeStore(fail_check=True)
    now = [0.0]

    async def fake_sleep(s: float) -> None:
        now[0] += s
        await asyncio.sleep(0)

    monitor = IntegrityMonitor(
        fake,
        tmp_path,
        canary_interval_seconds=10.0,
        snapshot_interval_seconds=1000.0,
        snapshot_ring_size=3,
        sleep_fn=fake_sleep,
        time_fn=lambda: now[0],
    )
    monitor.start()
    for _ in range(50):
        await asyncio.sleep(0)
        if len(fake.snapshot_calls) >= 1:
            break
    monitor.stop()
    with pytest.raises(asyncio.CancelledError):
        await monitor._task  # type: ignore[union-attr]

    # Canary failure forces an immediate snapshot even though the
    # snapshot timer hasn't elapsed.
    assert fake.check_calls >= 1
    assert len(fake.snapshot_calls) >= 1


async def test_monitor_fires_wal_checkpoint_on_schedule(tmp_path: Path) -> None:
    fake = _FakeStore()
    now = [0.0]

    async def fake_sleep(s: float) -> None:
        now[0] += s
        await asyncio.sleep(0)

    monitor = IntegrityMonitor(
        fake,
        tmp_path,
        canary_interval_seconds=1000.0,
        snapshot_interval_seconds=1000.0,
        wal_checkpoint_interval_seconds=5.0,
        snapshot_ring_size=3,
        sleep_fn=fake_sleep,
        time_fn=lambda: now[0],
    )
    monitor.start()
    for _ in range(50):
        await asyncio.sleep(0)
        if fake.wal_checkpoint_calls >= 2:
            break
    monitor.stop()
    with pytest.raises(asyncio.CancelledError):
        await monitor._task  # type: ignore[union-attr]

    assert fake.wal_checkpoint_calls >= 2
    # The other timers must NOT have fired (their intervals are 1000s, sim
    # time only advanced ~15s).
    assert fake.check_calls == 0
    assert fake.snapshot_calls == []


async def test_monitor_snapshot_ring_rotates_through_indices(tmp_path: Path) -> None:
    fake = _FakeStore()
    monitor = IntegrityMonitor(
        fake,
        tmp_path,
        canary_interval_seconds=1000.0,
        snapshot_interval_seconds=1.0,  # snapshot every "second"
        snapshot_ring_size=3,
    )
    # Call the rotate path directly to keep the test deterministic.
    for _ in range(7):
        await monitor._rotate_snapshot()  # type: ignore[reportPrivateUsage]
    names = [p.name for p in fake.snapshot_calls]
    # ring_size=3 → indices cycle 0,1,2,0,1,2,0
    assert names == [
        "agentshore.db.snapshot.0",
        "agentshore.db.snapshot.1",
        "agentshore.db.snapshot.2",
        "agentshore.db.snapshot.0",
        "agentshore.db.snapshot.1",
        "agentshore.db.snapshot.2",
        "agentshore.db.snapshot.0",
    ]


def test_restore_handles_missing_main_db(tmp_path: Path) -> None:
    # No main DB at all (first-run scenario after the user deleted .agentshore/
    # but the snapshots happen to still be present). The check should report
    # failure and try the ring; an empty ring returns None.
    chosen = restore_from_snapshot_ring(tmp_path / "agentshore.db", tmp_path)
    assert chosen is None


# ---------------------------------------------------------------------------
# recover_via_sqlite_recover + recovery fallback in restore_from_snapshot_ring
# ---------------------------------------------------------------------------


def _make_seeded_db_sync(path: Path) -> None:
    """Create a small SQLite DB synchronously (no async DataStore)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t(x INTEGER PRIMARY KEY, payload TEXT)")
        conn.executemany(
            "INSERT INTO t(x, payload) VALUES (?, ?)",
            [(i, "x" * 100) for i in range(200)],
        )
        conn.commit()
    finally:
        conn.close()


def _shred_file_header(path: Path) -> None:
    """Overwrite the SQLite header so the file is unrecoverable.

    The first 100 bytes are the SQLite header (``SQLite format 3\0``,
    page_size, change counter, etc.). Garbage here makes the file an
    unknown-format blob — ``sqlite3`` refuses to open it and ``.recover``
    produces no output.
    """
    with path.open("r+b") as fh:
        fh.write(b"\xff" * 100)


def test_recover_via_sqlite_recover_salvages_localized_corruption(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "src.db"
    _make_seeded_db_sync(db_path)
    _corrupt_file(db_path)  # mid-page corruption — header still intact

    dest = tmp_path / "recovered.db"
    ok, errors = recover_via_sqlite_recover(db_path, dest)
    assert ok, f"recovery should succeed for header-intact corruption; errors={errors}"
    assert dest.exists()
    # Recovered DB passes quick_check.
    rows = sqlite3.connect(str(dest)).execute("PRAGMA quick_check").fetchall()
    assert [r[0] for r in rows] == ["ok"]
    # Schema came over; row count varies with corruption location (B-tree
    # pages may be fully shredded). The recovery succeeding at all is the
    # interesting signal, not the exact salvage rate.
    conn = sqlite3.connect(str(dest))
    table_names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "t" in table_names


def test_recover_via_sqlite_recover_fails_on_shredded_header(tmp_path: Path) -> None:
    db_path = tmp_path / "src.db"
    _make_seeded_db_sync(db_path)
    _shred_file_header(db_path)

    dest = tmp_path / "recovered.db"
    ok, errors = recover_via_sqlite_recover(db_path, dest)
    assert ok is False
    assert errors  # at least one error line
    # The dest may or may not exist depending on which subprocess failed first;
    # but if it does, it should be empty / unhealthy. Either way, the function
    # must NOT report success on a shredded source.


def test_recover_via_sqlite_recover_overwrites_existing_dest(tmp_path: Path) -> None:
    """A stale ``dest_path`` from a prior attempt must be cleared before retry."""
    db_path = tmp_path / "src.db"
    _make_seeded_db_sync(db_path)
    _corrupt_file(db_path)

    dest = tmp_path / "recovered.db"
    dest.write_bytes(b"stale junk that is not a sqlite file")
    ok, _ = recover_via_sqlite_recover(db_path, dest)
    assert ok
    rows = sqlite3.connect(str(dest)).execute("PRAGMA quick_check").fetchall()
    assert [r[0] for r in rows] == ["ok"]


def test_restore_moves_corrupt_aside_when_recovery_fails(tmp_path: Path) -> None:
    """When ``.recover`` cannot salvage, the corrupt DB is moved aside.

    The caller's ``store.initialize()`` will then create a fresh empty DB at
    the main slot — session boots without history rather than crashing.
    """
    db_path = tmp_path / "agentshore.db"
    _make_seeded_db_sync(db_path)
    _shred_file_header(db_path)
    # Empty snapshot ring AND unrecoverable main → ladder falls through to
    # the final fallback.

    chosen = restore_from_snapshot_ring(db_path, tmp_path)
    assert chosen is None
    # Main slot is now empty; store.initialize() will create a fresh DB.
    assert not db_path.exists()
    # Corrupt file preserved for post-mortem.
    corrupts = sorted(tmp_path.glob("agentshore.db.corrupt.*"))
    assert len(corrupts) == 1


def test_restore_prefers_snapshot_over_recovery_when_both_viable(
    tmp_path: Path,
) -> None:
    """Snapshot restore wins over ``.recover`` when the ring has a clean image.

    Snapshot data is logically traversed (VACUUM INTO), so it's strictly
    higher fidelity than ``.recover`` output even if both would technically
    return a passable DB.
    """
    import asyncio

    async def _setup() -> None:
        store = await _make_store(tmp_path)
        snap = tmp_path / "agentshore.db.snapshot.0"
        await store.snapshot_to(snap)
        await store.close()
        # Corrupt only the main DB; snapshot remains clean.
        _corrupt_file(tmp_path / "agentshore.db")

    asyncio.run(_setup())
    chosen = restore_from_snapshot_ring(
        tmp_path / "agentshore.db", tmp_path
    )
    # Snapshot path is what's returned (not the main db_path that the
    # recovery branch would have returned).
    assert chosen == tmp_path / "agentshore.db.snapshot.0"
