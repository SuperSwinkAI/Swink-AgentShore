"""Tests for the ESR builder and archive RPC helpers (DESIGN §5.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentshore.data.models import ArchiveRecord, PlayRecord, SessionRecord
from agentshore.data.store import DataStore
from agentshore.sidecar import archive_rpc
from agentshore.sidecar.archive_rpc import ArchiveError
from agentshore.sidecar.esr import build_esr_payload


async def _populated_store(db_path: Path) -> tuple[DataStore, str, str]:
    """Create a DataStore with one session, two plays, and two archive rows."""
    store = DataStore(db_path)
    await store.initialize()
    session_id = "session-test"
    started = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC).isoformat()
    ended = datetime(2026, 5, 1, 13, 0, 0, tzinfo=UTC).isoformat()
    await store.create_session(
        SessionRecord(
            session_id=session_id,
            project_path="/tmp/proj",
            started_at=started,
            ended_at=ended,
            status="ended",
            final_alignment=0.85,
        )
    )
    await store.record_play(
        PlayRecord(
            session_id=session_id,
            play_type="issue_pickup",
            started_at=started,
            ended_at=ended,
            success=True,
            dollar_cost=1.25,
        )
    )
    await store.record_play(
        PlayRecord(
            session_id=session_id,
            play_type="code_review",
            started_at=started,
            ended_at=ended,
            success=False,
            dollar_cost=0.75,
            error="boom",
        )
    )
    older_archive = "archive-old"
    newer_archive = "archive-new"
    await store.create_archive(
        ArchiveRecord(
            archive_id=older_archive,
            session_id=session_id,
            archive_path="/tmp/archive/old",
            total_cost=0.5,
            final_alignment=0.5,
            total_plays=3,
            created_at=datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC).isoformat(),
        )
    )
    await store.create_archive(
        ArchiveRecord(
            archive_id=newer_archive,
            session_id=session_id,
            archive_path="/tmp/archive/new",
            total_cost=2.0,
            final_alignment=0.85,
            total_plays=2,
            created_at=datetime(2026, 5, 1, 13, 0, 0, tzinfo=UTC).isoformat(),
        )
    )
    return store, session_id, newer_archive


@pytest.mark.asyncio
async def test_build_esr_payload_returns_full_wire_shape(tmp_path: Path) -> None:
    store, session_id, _ = await _populated_store(tmp_path / "db.sqlite")
    try:
        payload = await build_esr_payload(
            store,
            session_id,
            archive_path="/tmp/archive/new",
            report_path="/tmp/archive/new/report.html",
            log_path="/tmp/archive/new/session.log",
            exit_reason="user_stop",
            exit_code=0,
        )
    finally:
        await store.close()

    assert payload["session_id"] == session_id
    assert payload["exit_reason"] == "user_stop"
    assert payload["exit_code"] == 0
    assert payload["archive_path"] == "/tmp/archive/new"
    assert payload["report_path"] == "/tmp/archive/new/report.html"
    assert payload["log_path"] == "/tmp/archive/new/session.log"
    summary = payload["esr_summary"]
    assert "overview" in summary
    assert "play_stats" in summary
    assert "closed_issues" in summary
    assert "control_rejections" in summary
    assert "repo_url" in summary


@pytest.mark.asyncio
async def test_archive_list_returns_records(tmp_path: Path) -> None:
    store, _, _ = await _populated_store(tmp_path / "db.sqlite")
    try:
        rows = await archive_rpc.list_archives(store)
    finally:
        await store.close()
    assert len(rows) == 2
    # Newer archive first (ORDER BY created_at DESC)
    assert rows[0]["archive_id"] == "archive-new"
    assert rows[1]["archive_id"] == "archive-old"
    for row in rows:
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
async def test_archive_fetch_report_parses_sections(tmp_path: Path) -> None:
    store, _, archive_id = await _populated_store(tmp_path / "db.sqlite")
    html_file = tmp_path / "report.html"
    html_file.write_text(
        """
        <html><body>
          <section id="overview"><h2>Overview</h2><p>...</p></section>
          <section id="plays"><h2>Plays</h2><p>...</p></section>
          <section id="cost"><h2>Cost Breakdown</h2><p>...</p></section>
        </body></html>
        """,
        encoding="utf-8",
    )
    # Update the archive row to point at this report
    archive = await store.get_archive(archive_id)
    assert archive is not None
    # Adjust archive_path so fetch_report can locate the html
    try:
        result = await archive_rpc.fetch_report(
            store,
            archive_id,
            report_path_override=str(html_file),
        )
    finally:
        await store.close()
    assert result["html_path"] == str(html_file)
    assert result["sections"] == [
        {"id": "overview", "title": "Overview"},
        {"id": "plays", "title": "Plays"},
        {"id": "cost", "title": "Cost Breakdown"},
    ]


@pytest.mark.asyncio
async def test_archive_fetch_report_missing_archive_raises(tmp_path: Path) -> None:
    store, _, _ = await _populated_store(tmp_path / "db.sqlite")
    try:
        with pytest.raises(ArchiveError) as exc_info:
            await archive_rpc.fetch_report(store, "no-such-archive")
    finally:
        await store.close()
    assert exc_info.value.code == -32602


@pytest.mark.asyncio
async def test_archive_fetch_logs_returns_range(tmp_path: Path) -> None:
    store, _, archive_id = await _populated_store(tmp_path / "db.sqlite")
    log_file = tmp_path / "session.log"
    log_file.write_text("\n".join(f"line-{i}" for i in range(1, 601)) + "\n", encoding="utf-8")
    try:
        default = await archive_rpc.fetch_logs(
            store,
            archive_id,
            log_path_override=str(log_file),
        )
        assert len(default["lines"]) == 200
        assert default["lines"][0] == "line-1"
        assert default["lines"][-1] == "line-200"

        windowed = await archive_rpc.fetch_logs(
            store,
            archive_id,
            range_={"start": 201, "end": 400},
            log_path_override=str(log_file),
        )
        assert len(windowed["lines"]) == 200
        assert windowed["lines"][0] == "line-201"
        assert windowed["lines"][-1] == "line-400"

        with pytest.raises(ArchiveError) as exc:
            await archive_rpc.fetch_logs(
                store,
                archive_id,
                range_={"start": -1, "end": 50},
                log_path_override=str(log_file),
            )
    finally:
        await store.close()
    assert exc.value.code == -32602
