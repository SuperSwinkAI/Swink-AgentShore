"""Phase 6 W2 Agent 2C: IPC command dispatch for report/archive commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.cli.runtime import _dispatch_command


@pytest.mark.asyncio()
async def test_dispatch_generate_report_summary() -> None:
    """generate_report with default type routes to Orchestrator.generate_report('summary')."""
    orch = MagicMock()
    orch.generate_report = AsyncMock()
    await _dispatch_command({"command": "generate_report"}, orch)
    orch.generate_report.assert_awaited_once_with("summary")


@pytest.mark.asyncio()
async def test_dispatch_generate_report_progress() -> None:
    """generate_report with report_type=progress routes the type through to the orchestrator."""
    orch = MagicMock()
    orch.generate_report = AsyncMock()
    await _dispatch_command({"command": "generate_report", "report_type": "progress"}, orch)
    orch.generate_report.assert_awaited_once_with("progress")


@pytest.mark.asyncio()
async def test_dispatch_archive_session() -> None:
    """archive_session routes to Orchestrator.archive_session()."""
    orch = MagicMock()
    orch.archive_session = AsyncMock()
    await _dispatch_command({"command": "archive_session"}, orch)
    orch.archive_session.assert_awaited_once_with()


@pytest.mark.asyncio()
async def test_dispatch_list_archives() -> None:
    """list_archives routes to Orchestrator.list_archives and logs the count."""
    orch = MagicMock()
    orch.list_archives = AsyncMock(return_value=["a1", "a2"])

    with patch("agentshore.cli.runtime._logger") as mock_logger:
        await _dispatch_command({"command": "list_archives"}, orch)
        orch.list_archives.assert_awaited_once()
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
    """rescan_issues routes to Orchestrator.refresh_issues()."""
    orch = MagicMock()
    orch.refresh_issues = AsyncMock()
    await _dispatch_command({"command": "rescan_issues"}, orch)
    orch.refresh_issues.assert_awaited_once()


@pytest.mark.asyncio()
async def test_dispatch_abort_play_routes_to_abort_in_flight() -> None:
    """abort_play logs the in-flight ids and routes to Orchestrator.abort_in_flight()."""
    orch = MagicMock()
    orch.in_flight_ids = MagicMock(return_value=["d1", "d2"])
    orch.abort_in_flight = AsyncMock()
    await _dispatch_command({"command": "abort_play"}, orch)
    orch.abort_in_flight.assert_awaited_once_with()


@pytest.mark.asyncio()
async def test_abort_in_flight_cancels_tasks() -> None:
    """Orchestrator.abort_in_flight cancels every in-flight play task."""
    from agentshore.core.orchestrator import Orchestrator

    task1 = MagicMock()
    task2 = MagicMock()
    orch = Orchestrator.__new__(Orchestrator)
    orch._in_flight = {"d1": task1, "d2": task2}
    await orch.abort_in_flight()
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
    with patch("agentshore.cli.runtime._logger"):
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
    orch.refresh_issues = AsyncMock()
    orch.resume = AsyncMock()
    await _dispatch_command({"command": "feedback_response", "action": "rescan_issues"}, orch)
    orch.refresh_issues.assert_awaited_once()
    orch.resume.assert_awaited_once()
