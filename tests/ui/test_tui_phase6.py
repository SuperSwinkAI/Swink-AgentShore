"""Phase 6 W2 Agent 2C: TUI action stub implementations."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.state import PlayType
from agentshore.ui.app import OrchestratorApp
from agentshore.ui.play_labels import play_short_label


@pytest.mark.asyncio()
async def test_action_generate_report_no_orch() -> None:
    """action_generate_report returns early when no orchestrator is set."""
    app = OrchestratorApp()
    async with app.run_test():
        # Should return early without crash (no notify, no error)
        await app.action_generate_report()


@pytest.mark.asyncio()
async def test_action_generate_report_with_orch() -> None:
    """action_generate_report calls ReportGenerator when orchestrator exists."""
    app = OrchestratorApp()
    app._orch = MagicMock()
    app._orch._store = MagicMock()
    app._orch._repo_root = Path("/tmp")
    app._orch._session_id = "test"

    with patch("agentshore.reports.generator.ReportGenerator") as mock_gen_cls:
        mock_gen = AsyncMock()
        mock_gen_cls.return_value = mock_gen
        mock_gen.generate_session_summary = AsyncMock(return_value=Path("/tmp/report.html"))
        async with app.run_test():
            await app.action_generate_report()
            mock_gen.generate_session_summary.assert_called_once()


@pytest.mark.asyncio()
async def test_action_show_issues_no_state() -> None:
    """action_show_issues notifies when no open issues are available."""
    app = OrchestratorApp()
    async with app.run_test():
        await app.action_show_issues()
        # Should notify "No open issues" without crashing


# ---------------------------------------------------------------------------
# Play label mapping — #486
# ---------------------------------------------------------------------------


def test_short_play_label_known_values() -> None:
    """play_short_label returns expected short labels for key play types."""
    assert play_short_label(PlayType.ISSUE_PICKUP) == "Pickup"
    assert play_short_label(PlayType.CODE_REVIEW) == "Review"
    assert play_short_label(PlayType.RUN_QA) == "QA"
    assert play_short_label(PlayType.GROOM_BACKLOG) == "Groom"
    assert play_short_label(PlayType.SEED_PROJECT) == "Seed"
    assert play_short_label(PlayType.FUTURE_4) == "Reserved"
    assert play_short_label(PlayType.FUTURE_7) == "Reserved"
    assert play_short_label(PlayType.FUTURE_8) == "Reserved"


def test_short_play_label_all_members_non_empty() -> None:
    """Every PlayType member returns a non-empty string from play_short_label."""
    for pt in PlayType:
        label = play_short_label(pt)
        assert label, f"empty label for {pt}"
