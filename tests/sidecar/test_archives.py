"""Contract tests for ``archive.*`` JSON-RPC methods (DESIGN §5.1/§5.2).

These tests pin the wire shape and error codes promised by issue #228:

* ``archive.list`` ⇒ ``[{archive_id, session_id, archive_path, total_cost,
  final_alignment, total_plays, created_at}]`` sorted newest-first.
* ``archive.fetch_report`` ⇒ ``{html_path, sections}``; unknown archive_id
  raises ``-32602``, archive without a rendered report raises
  ``-32004 ERR_REPORT_NOT_FOUND``.
* ``archive.fetch_logs`` ⇒ ``{lines}`` for the requested line range;
  out-of-range slices return an empty list, unknown archive_id raises
  ``-32602``.
* The handshake capability list advertises all three methods.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentshore.data.models import ArchiveRecord, SessionRecord
from agentshore.data.store import DataStore
from agentshore.sidecar import archive_rpc
from agentshore.sidecar.archive_rpc import (
    ERR_REPORT_NOT_FOUND,
    INVALID_PARAMS,
    ArchiveError,
)
from agentshore.sidecar.handshake import capabilities


async def _seed_store(db_path: Path, archive_dir: Path) -> tuple[DataStore, str]:
    """Create a store with one session and one archive row pointing at ``archive_dir``."""
    store = DataStore(db_path)
    await store.initialize()
    session_id = "session-archives"
    started = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC).isoformat()
    ended = datetime(2026, 5, 10, 13, 0, 0, tzinfo=UTC).isoformat()
    await store.create_session(
        SessionRecord(
            session_id=session_id,
            project_path="/tmp/proj",
            started_at=started,
            ended_at=ended,
            status="ended",
            final_alignment=0.5,
        )
    )
    archive_id = "archive-1"
    await store.create_archive(
        ArchiveRecord(
            archive_id=archive_id,
            session_id=session_id,
            archive_path=str(archive_dir),
            total_cost=1.0,
            final_alignment=0.5,
            total_plays=4,
            created_at=ended,
        )
    )
    return store, archive_id


def test_handshake_capabilities_advertise_archive_methods() -> None:
    caps = capabilities()
    assert "archive.list" in caps
    assert "archive.fetch_report" in caps
    assert "archive.fetch_logs" in caps


@pytest.mark.asyncio
async def test_archive_list_round_trip_returns_required_keys(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    store, archive_id = await _seed_store(tmp_path / "db.sqlite", archive_dir)
    try:
        rows = await archive_rpc.list_archives(store)
    finally:
        await store.close()
    assert len(rows) == 1
    row = rows[0]
    assert row["archive_id"] == archive_id
    assert set(row.keys()) == {
        "archive_id",
        "session_id",
        "archive_path",
        "total_cost",
        "final_alignment",
        "total_plays",
        "created_at",
    }


@pytest.mark.asyncio
async def test_archive_fetch_report_unknown_archive_raises_invalid_params(
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    store, _ = await _seed_store(tmp_path / "db.sqlite", archive_dir)
    try:
        with pytest.raises(ArchiveError) as exc_info:
            await archive_rpc.fetch_report(store, "no-such-archive")
    finally:
        await store.close()
    assert exc_info.value.code == INVALID_PARAMS


@pytest.mark.asyncio
async def test_archive_fetch_report_missing_report_raises_err_report_not_found(
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    # Deliberately do not write report.html — archive row exists, report file does not.
    store, archive_id = await _seed_store(tmp_path / "db.sqlite", archive_dir)
    try:
        with pytest.raises(ArchiveError) as exc_info:
            await archive_rpc.fetch_report(store, archive_id)
    finally:
        await store.close()
    assert exc_info.value.code == ERR_REPORT_NOT_FOUND


@pytest.mark.asyncio
async def test_archive_fetch_logs_out_of_range_returns_empty_lines(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    log_file = archive_dir / "session.log"
    log_file.write_text("a\nb\nc\n", encoding="utf-8")
    store, archive_id = await _seed_store(tmp_path / "db.sqlite", archive_dir)
    try:
        result = await archive_rpc.fetch_logs(
            store,
            archive_id,
            range_={"start": 100, "end": 200},
        )
    finally:
        await store.close()
    assert result == {"lines": []}


@pytest.mark.asyncio
async def test_archive_fetch_logs_unknown_archive_raises_invalid_params(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    store, _ = await _seed_store(tmp_path / "db.sqlite", archive_dir)
    try:
        with pytest.raises(ArchiveError) as exc_info:
            await archive_rpc.fetch_logs(store, "no-such-archive")
    finally:
        await store.close()
    assert exc_info.value.code == INVALID_PARAMS
