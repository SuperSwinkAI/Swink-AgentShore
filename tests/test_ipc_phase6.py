"""Phase 6 W2 Agent 2C: IPC command dispatch for report/archive commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.cli import _dispatch_command


@pytest.mark.asyncio()
async def test_dispatch_generate_report_summary() -> None:
    """generate_report with default type calls generate_session_summary."""
    orch = MagicMock()
    orch._store = MagicMock()
    orch._repo_root = Path("/tmp/test")
    orch._session_id = "test-session"

    with patch("agentshore.reports.generator.ReportGenerator") as mock_gen_cls:
        mock_gen = AsyncMock()
        mock_gen_cls.return_value = mock_gen
        await _dispatch_command({"command": "generate_report"}, orch)
        mock_gen.generate_session_summary.assert_called_once_with(
            "test-session",
            Path("/tmp/test/.agentshore/reports"),
        )


@pytest.mark.asyncio()
async def test_dispatch_generate_report_progress() -> None:
    """generate_report with report_type=progress calls generate_progress_report."""
    orch = MagicMock()
    orch._store = MagicMock()
    orch._repo_root = Path("/tmp/test")
    orch._session_id = "test-session"

    with patch("agentshore.reports.generator.ReportGenerator") as mock_gen_cls:
        mock_gen = AsyncMock()
        mock_gen_cls.return_value = mock_gen
        await _dispatch_command({"command": "generate_report", "report_type": "progress"}, orch)
        mock_gen.generate_progress_report.assert_called_once_with(
            "test-session",
            Path("/tmp/test/.agentshore/reports"),
        )


@pytest.mark.asyncio()
async def test_dispatch_archive_session() -> None:
    """archive_session creates an Archiver and calls create_archive."""
    orch = MagicMock()
    orch._store = MagicMock()
    orch._repo_root = Path("/tmp/test")
    orch._session_id = "test-session"

    with patch("agentshore.archive.Archiver") as mock_archiver_cls:
        mock_archiver = AsyncMock()
        mock_archiver_cls.return_value = mock_archiver
        await _dispatch_command({"command": "archive_session"}, orch)
        mock_archiver_cls.assert_called_once_with(
            orch._store,
            Path("/tmp/test/.agentshore/archives"),
        )
        mock_archiver.create_archive.assert_called_once_with(
            "test-session",
            db_path=Path("/tmp/test/.agentshore/agentshore.db"),
        )


@pytest.mark.asyncio()
async def test_dispatch_list_archives() -> None:
    """list_archives calls store.list_archives and logs the count."""
    orch = MagicMock()
    orch._store = MagicMock()
    orch._store.list_archives = AsyncMock(return_value=["a1", "a2"])

    with patch("agentshore.cli._logger") as mock_logger:
        await _dispatch_command({"command": "list_archives"}, orch)
        orch._store.list_archives.assert_awaited_once()
        mock_logger.info.assert_called_once_with("ipc.archives_listed", count=2)


@pytest.mark.asyncio()
async def test_dispatch_unknown_command_ignored() -> None:
    """Unknown commands don't crash."""
    orch = MagicMock()
    # Should not raise
    await _dispatch_command({"command": "unknown_future_command"}, orch)


# ---------------------------------------------------------------------------
# Tests for previously-silent commands (issue #6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_dispatch_rescan_issues() -> None:
    """rescan_issues calls _refresh_issues on the orchestrator."""
    orch = MagicMock()
    orch._completion.refresh_issues = AsyncMock()
    await _dispatch_command({"command": "rescan_issues"}, orch)
    orch._completion.refresh_issues.assert_awaited_once()


@pytest.mark.asyncio()
async def test_dispatch_abort_play_cancels_in_flight() -> None:
    """abort_play cancels all in-flight play tasks."""
    task1 = MagicMock()
    task2 = MagicMock()
    orch = MagicMock()
    orch._in_flight = {"d1": task1, "d2": task2}
    await _dispatch_command({"command": "abort_play"}, orch)
    task1.cancel.assert_called_once()
    task2.cancel.assert_called_once()


@pytest.mark.asyncio()
async def test_dispatch_verification_response_passed_resumes() -> None:
    """verification_response with passed=True resumes the orchestrator."""
    orch = MagicMock()
    orch.resume = AsyncMock()
    await _dispatch_command(
        {
            "command": "verification_response",
            "checkpoint_id": "cp-1",
            "result": "ok",
            "passed": True,
        },
        orch,
    )
    orch.resume.assert_awaited_once()


@pytest.mark.asyncio()
async def test_dispatch_verification_response_failed_stays_paused() -> None:
    """verification_response with passed=False does NOT resume the orchestrator."""
    orch = MagicMock()
    orch.resume = AsyncMock()
    with patch("agentshore.cli._logger"):
        await _dispatch_command(
            {
                "command": "verification_response",
                "checkpoint_id": "cp-1",
                "result": "ok",
                "passed": False,
                "notes": "Failed E2E",
            },
            orch,
        )
    orch.resume.assert_not_called()


@pytest.mark.asyncio()
async def test_dispatch_start_is_noop() -> None:
    """start command is a no-op at dispatch time (orchestrator already running)."""
    orch = MagicMock()
    # Should not raise or call any orchestrator method
    await _dispatch_command({"command": "start"}, orch)


@pytest.mark.asyncio()
async def test_dispatch_feedback_response_rescan_issues() -> None:
    """feedback_response with action=rescan_issues refreshes issues and resumes."""
    orch = MagicMock()
    orch._completion.refresh_issues = AsyncMock()
    orch.resume = AsyncMock()
    await _dispatch_command({"command": "feedback_response", "action": "rescan_issues"}, orch)
    orch._completion.refresh_issues.assert_awaited_once()
    orch.resume.assert_awaited_once()
