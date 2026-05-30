"""Phase 6 Wave 2: CLI commands for report generation and session archival."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentshore.cli import main
from agentshore.data.store import DataStore, PlayRecord, SessionRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SID = "test-session-abcdef01"
NOW = datetime.now(UTC).isoformat()


def _ts(offset_seconds: float = 0.0) -> str:
    """Return an ISO-8601 timestamp shifted by ``offset_seconds`` from now."""
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).isoformat(timespec="seconds")


def _setup_db(tmp_path: Path) -> Path:
    """Create a DB with a session and plays inside tmp_path/.agentshore/."""

    async def _create() -> Path:
        db_path = tmp_path / ".agentshore" / "agentshore.db"
        db_path.parent.mkdir(parents=True)
        store = DataStore(db_path)
        await store.initialize()
        await store.create_session(
            SessionRecord(
                session_id=SID,
                project_path=str(tmp_path),
                started_at=NOW,
                ended_at=NOW,
                status="completed",
                total_cost=0.05,
                total_plays=1,
                final_alignment=0.75,
            )
        )
        await store.record_play(
            PlayRecord(
                session_id=SID,
                play_type="issue_pickup",
                agent_id="agent-1",
                started_at=NOW,
                success=True,
                dollar_cost=0.05,
                token_cost=100,
                duration_ms=5000,
            )
        )
        await store.close()
        return db_path

    return asyncio.run(_create())


def _setup_two_sessions(tmp_path: Path) -> Path:
    """Create a DB with two completed sessions for comparison tests."""

    async def _create() -> Path:
        db_path = tmp_path / ".agentshore" / "agentshore.db"
        db_path.parent.mkdir(parents=True)
        store = DataStore(db_path)
        await store.initialize()

        for sid, cost, alignment in [
            ("session-aaa-11111", 1.0, 0.6),
            ("session-bbb-22222", 2.0, 0.8),
        ]:
            await store.create_session(
                SessionRecord(
                    session_id=sid,
                    project_path=str(tmp_path),
                    started_at=NOW,
                    ended_at=NOW,
                    status="completed",
                    total_cost=cost,
                    total_plays=1,
                    final_alignment=alignment,
                )
            )
            await store.record_play(
                PlayRecord(
                    session_id=sid,
                    play_type="issue_pickup",
                    agent_id="agent-1",
                    started_at=NOW,
                    success=True,
                    dollar_cost=cost,
                    duration_ms=3000,
                )
            )

        await store.close()
        return db_path

    return asyncio.run(_create())


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# report --type summary
# ---------------------------------------------------------------------------


def test_report_summary_generates_file(runner: CliRunner, tmp_path: Path) -> None:
    _setup_db(tmp_path)
    result = runner.invoke(
        main,
        ["report", "--session", SID, "--project", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "Report saved to:" in result.output
    # Verify the HTML file was actually created
    reports_dir = tmp_path / ".agentshore" / "reports"
    html_files = list(reports_dir.glob("*.html"))
    assert len(html_files) == 1
    assert "summary" in html_files[0].name


# ---------------------------------------------------------------------------
# report --type progress
# ---------------------------------------------------------------------------


def test_report_progress_generates_file(runner: CliRunner, tmp_path: Path) -> None:
    _setup_db(tmp_path)
    result = runner.invoke(
        main,
        ["report", "--session", SID, "--type", "progress", "--project", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "Report saved to:" in result.output
    reports_dir = tmp_path / ".agentshore" / "reports"
    html_files = list(reports_dir.glob("*.html"))
    assert len(html_files) == 1
    assert "progress" in html_files[0].name


# ---------------------------------------------------------------------------
# report — no database
# ---------------------------------------------------------------------------


def test_report_no_db(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        main,
        ["report", "--project", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "No database found" in result.output


# ---------------------------------------------------------------------------
# report — DB exists, no sessions
# ---------------------------------------------------------------------------


def test_report_no_sessions(runner: CliRunner, tmp_path: Path) -> None:
    """DB exists but has no sessions -> exit with 'No sessions found'."""

    async def _create_empty_db() -> None:
        db_path = tmp_path / ".agentshore" / "agentshore.db"
        db_path.parent.mkdir(parents=True)
        store = DataStore(db_path)
        await store.initialize()
        await store.close()

    asyncio.run(_create_empty_db())

    result = runner.invoke(
        main,
        ["report", "--project", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "No sessions found" in result.output


# ---------------------------------------------------------------------------
# report — uses most recent session by default
# ---------------------------------------------------------------------------


def test_report_default_session(runner: CliRunner, tmp_path: Path) -> None:
    _setup_db(tmp_path)
    # No --session flag; should pick the latest automatically
    result = runner.invoke(
        main,
        ["report", "--project", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "Report saved to:" in result.output


# ---------------------------------------------------------------------------
# archive list — empty
# ---------------------------------------------------------------------------


def test_archive_list_empty(runner: CliRunner, tmp_path: Path) -> None:
    _setup_db(tmp_path)
    result = runner.invoke(
        main,
        ["archive", "list", "--project", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "No archived sessions" in result.output


# ---------------------------------------------------------------------------
# archive list — with data
# ---------------------------------------------------------------------------


def test_archive_list_with_data(runner: CliRunner, tmp_path: Path) -> None:
    """Create an archive record and verify formatted table output."""

    async def _create() -> None:
        from agentshore.data.store import ArchiveRecord

        db_path = tmp_path / ".agentshore" / "agentshore.db"
        db_path.parent.mkdir(parents=True)
        store = DataStore(db_path)
        await store.initialize()
        # FK requires a session to exist first
        await store.create_session(
            SessionRecord(
                session_id="sess-archive-test",
                project_path=str(tmp_path),
                started_at=NOW,
                status="completed",
                total_cost=1.50,
                total_plays=10,
                final_alignment=0.82,
            )
        )
        await store.create_archive(
            ArchiveRecord(
                archive_id="arc-001",
                session_id="sess-archive-test",
                archive_path=str(tmp_path / "archives" / "test"),
                total_cost=1.50,
                final_alignment=0.82,
                total_plays=10,
                created_at=_ts(),
            )
        )
        await store.close()

    asyncio.run(_create())

    result = runner.invoke(
        main,
        ["archive", "list", "--project", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "sess-archi" in result.output  # truncated session_id
    assert "$" in result.output
    assert "0.82" in result.output


# ---------------------------------------------------------------------------
# archive create — creates archive directory
# ---------------------------------------------------------------------------


def test_archive_create_creates_directory(runner: CliRunner, tmp_path: Path) -> None:
    _setup_db(tmp_path)
    result = runner.invoke(
        main,
        ["archive", "create", "--session", SID, "--project", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "Session archived to:" in result.output
    archives_dir = tmp_path / ".agentshore" / "archives"
    assert archives_dir.exists()
    archive_dirs = list(archives_dir.iterdir())
    assert len(archive_dirs) >= 1


# ---------------------------------------------------------------------------
# archive compare — generates HTML
# ---------------------------------------------------------------------------


def test_archive_compare_generates_html(runner: CliRunner, tmp_path: Path) -> None:
    _setup_two_sessions(tmp_path)
    result = runner.invoke(
        main,
        [
            "archive",
            "compare",
            "session-aaa-11111",
            "session-bbb-22222",
            "--project",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Comparison report:" in result.output
    reports_dir = tmp_path / ".agentshore" / "reports"
    html_files = list(reports_dir.glob("*.html"))
    assert len(html_files) == 1
    assert "comparison" in html_files[0].name


# ---------------------------------------------------------------------------
# archive create — no database
# ---------------------------------------------------------------------------


def test_archive_create_no_db(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        main,
        ["archive", "create", "--project", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "No database found" in result.output
