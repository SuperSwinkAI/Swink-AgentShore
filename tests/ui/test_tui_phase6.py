"""Phase 6 W2 Agent 2C: TUI action stub implementations."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
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


@pytest.mark.asyncio()
async def test_action_show_learnings_no_orch() -> None:
    """action_show_learnings notifies when no orchestrator is set."""
    app = OrchestratorApp()
    async with app.run_test():
        await app.action_show_learnings()
        # Should notify "No active session" without crashing


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


# ---------------------------------------------------------------------------
# Four-column learnings overlay — #486
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_action_show_learnings_four_columns(tmp_path: Path) -> None:
    """action_show_learnings renders header + four data columns in confidence-desc order."""
    # Write a learnings fixture with multiple categories and source play types.
    learnings_dir = tmp_path / ".agentshore"
    learnings_dir.mkdir(parents=True)
    learnings_path = learnings_dir / "learnings.json"
    fixture = [
        {
            "id": "l1",
            "pattern": "always sanitize sql params",
            "confidence": 0.9,
            "sessions_since_use": 0,
            "source_play_id": 47,
            "last_reinforced_play_id": 47,
            "created_at": "2026-01-01T00:00:00+00:00",
            "scope": "project",
            "category": "security",
        },
        {
            "id": "l2",
            "pattern": "mock external calls in tests",
            "confidence": 0.7,
            "sessions_since_use": 0,
            "source_play_id": 46,
            "last_reinforced_play_id": 46,
            "created_at": "2026-01-01T00:00:00+00:00",
            "scope": "project",
            "category": "testing",
        },
        {
            "id": "l3",
            "pattern": "use asyncio for agent dispatch",
            "confidence": 0.6,
            "sessions_since_use": 0,
            "source_play_id": 5,
            "last_reinforced_play_id": 5,
            "created_at": "2026-01-01T00:00:00+00:00",
            "scope": "project",
            "category": "agent",
        },
        {
            "id": "l4",
            "pattern": "prefer WAL mode for sqlite",
            "confidence": 0.5,
            "sessions_since_use": 0,
            "source_play_id": None,
            "last_reinforced_play_id": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "scope": "project",
            "category": "database",
        },
    ]
    learnings_path.write_text(json.dumps(fixture), encoding="utf-8")

    # Build mock play records for the three sourced play ids.
    def _play_record(play_id: int, play_type: str) -> SimpleNamespace:
        return SimpleNamespace(play_id=play_id, play_type=play_type)

    mock_store = MagicMock()
    mock_store.get_play_history = AsyncMock(
        return_value=[
            _play_record(47, "issue_pickup"),
            _play_record(46, "code_review"),
            _play_record(5, "run_qa"),
        ]
    )

    mock_orch = SimpleNamespace(
        _repo_root=tmp_path,
        _session_id="s1",
        _store=mock_store,
    )

    app = OrchestratorApp()
    app._orch = mock_orch  # type: ignore[assignment]

    async with app.run_test() as pilot:
        await app.action_show_learnings()
        await pilot.pause()

        # The modal should now be the top screen; query its Static widgets.
        assert len(app.screen_stack) == 2, "LearningsModal was not pushed"
        statics = app.screen.query("Static")
        # textual 8 removed Static.renderable; render() returns the Content,
        # whose str() is the plain text we assert against below.
        text = " ".join(str(w.render()) for w in statics)

    # Header columns present.
    assert "Pattern" in text
    assert "Conf." in text
    assert "Category" in text
    assert "Source" in text

    # Each entry's pattern is present.
    assert "always sanitize sql params" in text
    assert "mock external calls in tests" in text

    # Category values rendered.
    assert "security" in text
    assert "testing" in text
    assert "agent" in text
    assert "database" in text

    # Source values rendered.
    assert "Pickup #47" in text
    assert "Review #46" in text
    assert "QA #5" in text
    assert "—" in text  # entry with source_play_id=None

    # Order: confidence 0.9 → 0.7 → 0.6 → 0.5.
    pos_sql = text.find("always sanitize sql params")
    pos_mock = text.find("mock external calls in tests")
    pos_async = text.find("use asyncio for agent dispatch")
    assert pos_sql < pos_mock < pos_async, "entries not sorted by confidence descending"
