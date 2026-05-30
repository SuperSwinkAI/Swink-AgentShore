"""Tests for the session archiver."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentshore import __version__
from agentshore.archive import Archiver
from agentshore.data.store import DataStore, SessionRecord


@pytest.fixture
async def setup(tmp_path: Path):
    """Provision a DataStore with a completed session and return test harness objects."""
    db_path = tmp_path / ".agentshore" / "agentshore.db"
    db_path.parent.mkdir(parents=True)

    store = DataStore(db_path)
    await store.initialize()

    session = SessionRecord(
        session_id="test-session-abcdef01",
        project_path=str(tmp_path),
        started_at=datetime.now(UTC).isoformat(),
    )
    await store.create_session(session)
    await store.complete_session("test-session-abcdef01", final_alignment=0.75)

    archive_dir = tmp_path / ".agentshore" / "archives"
    archiver = Archiver(store, archive_dir)

    yield store, archiver, db_path, archive_dir

    await store.close()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_archive_creates_directory(setup: tuple) -> None:
    """Verify archive directory is created after archival."""
    _store, archiver, db_path, archive_dir = setup
    result = await archiver.create_archive("test-session-abcdef01", db_path=db_path)
    assert result.exists()
    assert result.is_dir()
    assert result.parent == archive_dir


@pytest.mark.asyncio
async def test_archive_copies_database(setup: tuple) -> None:
    """Verify session.sqlite exists in the archive and is non-empty."""
    _store, archiver, db_path, _archive_dir = setup
    result = await archiver.create_archive("test-session-abcdef01", db_path=db_path)
    copied_db = result / "session.sqlite"
    assert copied_db.exists()
    assert copied_db.stat().st_size > 0


@pytest.mark.asyncio
async def test_archive_copies_learnings(setup: tuple, tmp_path: Path) -> None:
    """Verify learnings.json is copied into the archive when present."""
    _store, archiver, db_path, _archive_dir = setup
    learnings_path = tmp_path / "learnings.json"
    learnings_path.write_text('{"patterns": []}', encoding="utf-8")

    result = await archiver.create_archive(
        "test-session-abcdef01",
        db_path=db_path,
        learnings_path=learnings_path,
    )
    copied = result / "learnings.json"
    assert copied.exists()
    assert json.loads(copied.read_text(encoding="utf-8")) == {"patterns": []}


@pytest.mark.asyncio
async def test_archive_copies_policy(setup: tuple, tmp_path: Path) -> None:
    """Verify policy.pt is copied into the archive when present."""
    _store, archiver, db_path, _archive_dir = setup
    policy_path = tmp_path / "policy.pt"
    policy_path.write_bytes(b"\x80\x02fake-tensor-data")

    result = await archiver.create_archive(
        "test-session-abcdef01",
        db_path=db_path,
        policy_path=policy_path,
    )
    copied = result / "policy.pt"
    assert copied.exists()
    assert copied.read_bytes() == b"\x80\x02fake-tensor-data"


@pytest.mark.asyncio
async def test_archive_manifest_contents(setup: tuple) -> None:
    """Parse manifest.json and verify all expected fields are present and correct."""
    _store, archiver, db_path, _archive_dir = setup
    result = await archiver.create_archive("test-session-abcdef01", db_path=db_path)

    manifest_path = result / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["session_id"] == "test-session-abcdef01"
    assert manifest["agentshore_version"] == __version__
    assert manifest["final_alignment"] == 0.75
    assert manifest["total_plays"] == 0
    assert manifest["total_cost"] == 0.0
    assert "started_at" in manifest
    assert "ended_at" in manifest
    assert "archived_at" in manifest


@pytest.mark.asyncio
async def test_archive_inserts_db_record(setup: tuple) -> None:
    """Verify list_archives() returns the new record after creation."""
    _store, archiver, db_path, _archive_dir = setup
    await archiver.create_archive("test-session-abcdef01", db_path=db_path)

    archives = await archiver.list_archives()
    assert len(archives) == 1
    rec = archives[0]
    assert rec.session_id == "test-session-abcdef01"
    assert rec.final_alignment == 0.75
    assert rec.total_plays == 0
    assert rec.total_cost == 0.0


@pytest.mark.asyncio
async def test_archive_list_multiple(setup: tuple) -> None:
    """Create 2 archives (different sessions) and verify both appear in list."""
    store, archiver, db_path, _archive_dir = setup

    # Create a second session
    session2 = SessionRecord(
        session_id="second-session-99887766",
        project_path="/tmp/proj2",
        started_at=datetime.now(UTC).isoformat(),
    )
    await store.create_session(session2)
    await store.complete_session("second-session-99887766", final_alignment=0.90)

    await archiver.create_archive("test-session-abcdef01", db_path=db_path)
    await archiver.create_archive("second-session-99887766", db_path=db_path)

    archives = await archiver.list_archives()
    assert len(archives) == 2
    session_ids = {a.session_id for a in archives}
    assert session_ids == {"test-session-abcdef01", "second-session-99887766"}


@pytest.mark.asyncio
async def test_auto_archive_enabled(setup: tuple) -> None:
    """auto_archive=True should create an archive and return its path."""
    _store, archiver, db_path, _archive_dir = setup
    result = await archiver.auto_archive_if_enabled(
        "test-session-abcdef01",
        auto_archive=True,
        db_path=db_path,
    )
    assert result is not None
    assert result.exists()
    assert (result / "manifest.json").exists()


@pytest.mark.asyncio
async def test_auto_archive_disabled(setup: tuple) -> None:
    """auto_archive=False should return None and create nothing."""
    _store, archiver, db_path, archive_dir = setup
    result = await archiver.auto_archive_if_enabled(
        "test-session-abcdef01",
        auto_archive=False,
        db_path=db_path,
    )
    assert result is None
    # Archive dir should not even exist
    assert not archive_dir.exists()


@pytest.mark.asyncio
async def test_archive_nonexistent_session(setup: tuple) -> None:
    """Archiving a non-existent session should raise ValueError."""
    _store, archiver, db_path, _archive_dir = setup
    with pytest.raises(ValueError, match="Session not found"):
        await archiver.create_archive("nonexistent-session", db_path=db_path)
