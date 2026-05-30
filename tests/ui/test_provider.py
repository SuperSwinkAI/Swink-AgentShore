"""Tests for TuiStateProvider and OrchestratorApp shell (W1.5)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentshore.config.models import PolicyMode
from agentshore.plays.base import PlayParams
from agentshore.state import (
    AgentStatus,
    OrchestratorState,
    PlayOutcome,
    PlayType,
    SessionState,
    StateProvider,
)
from agentshore.ui.app import AppWiring, OrchestratorApp
from agentshore.ui.provider import TuiStateProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state() -> OrchestratorState:
    return OrchestratorState(
        session_id="s",
        session_state=SessionState.RUNNING,
        total_plays=1,
        total_cost=0.0,
        agents=[],
        open_issues=[],
        budget=None,
        trajectory=None,
        active_play=None,
        same_type_failure_streak=0,
    )


def _make_outcome() -> PlayOutcome:
    return PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.01,
        artifacts=[],
        alignment_delta=0.0,
    )


def _make_provider() -> tuple[TuiStateProvider, MagicMock]:
    mock_app = MagicMock()
    mock_app.post_message = MagicMock()
    return TuiStateProvider(mock_app), mock_app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_isinstance_state_provider() -> None:
    """TuiStateProvider must satisfy the StateProvider runtime-checkable protocol."""
    mock_app = MagicMock()
    provider = TuiStateProvider(mock_app)
    assert isinstance(provider, StateProvider)


@pytest.mark.asyncio
async def test_on_state_update_posts_state_updated() -> None:
    provider, mock_app = _make_provider()
    state = _make_state()
    await provider.on_state_update(state)
    mock_app.post_message.assert_called_once()
    msg = mock_app.post_message.call_args[0][0]
    assert isinstance(msg, OrchestratorApp.StateUpdated)
    assert msg.state is state


@pytest.mark.asyncio
async def test_on_play_started_posts_play_started() -> None:
    provider, mock_app = _make_provider()
    params = PlayParams(agent_id="agent-1", issue_number=42)
    await provider.on_play_started(PlayType.ISSUE_PICKUP, params)
    mock_app.post_message.assert_called_once()
    msg = mock_app.post_message.call_args[0][0]
    assert isinstance(msg, OrchestratorApp.PlayStarted)
    assert msg.play_type is PlayType.ISSUE_PICKUP
    assert msg.params is params


@pytest.mark.asyncio
async def test_on_play_completed_posts_play_completed() -> None:
    provider, mock_app = _make_provider()
    outcome = _make_outcome()
    await provider.on_play_completed(outcome)
    mock_app.post_message.assert_called_once()
    msg = mock_app.post_message.call_args[0][0]
    assert isinstance(msg, OrchestratorApp.PlayCompleted)
    assert msg.outcome is outcome


@pytest.mark.asyncio
async def test_on_agent_changed_posts_agent_changed() -> None:
    provider, mock_app = _make_provider()
    await provider.on_agent_changed("agent-x", AgentStatus.BUSY)
    mock_app.post_message.assert_called_once()
    msg = mock_app.post_message.call_args[0][0]
    assert isinstance(msg, OrchestratorApp.AgentChanged)
    assert msg.agent_id == "agent-x"
    assert msg.status is AgentStatus.BUSY


@pytest.mark.asyncio
async def test_on_feedback_requested_posts_feedback_requested() -> None:
    provider, mock_app = _make_provider()
    await provider.on_feedback_requested("need help")
    mock_app.post_message.assert_called_once()
    msg = mock_app.post_message.call_args[0][0]
    assert isinstance(msg, OrchestratorApp.FeedbackRequested)
    assert msg.reason == "need help"


@pytest.mark.asyncio
async def test_on_session_paused_posts_session_paused() -> None:
    provider, mock_app = _make_provider()
    await provider.on_session_paused("budget exhausted")
    mock_app.post_message.assert_called_once()
    msg = mock_app.post_message.call_args[0][0]
    assert isinstance(msg, OrchestratorApp.SessionPaused)
    assert msg.reason == "budget exhausted"


def test_orchestratorapp_bindings_have_expected_set() -> None:
    """OrchestratorApp declares the documented keybindings.

    The original test asserted a 'p' (pause) binding; pause was intentionally
    removed in commit c4f276b ("TUI footer cleanup — remove Manual Revert,
    Quit, Pause, Learnings, Approvals"). Updated to assert the surviving set.
    """
    keys = {b[0] for b in OrchestratorApp.BINDINGS}
    expected = {"ctrl+q", "ctrl+shift+q", "question_mark", "g", "d", "i"}
    assert expected.issubset(keys), f"missing bindings: {expected - keys}"


def test_phase6_import_smoke() -> None:
    """Importing from the agentshore.ui package must succeed."""
    from agentshore.ui import OrchestratorApp, TuiStateProvider

    assert OrchestratorApp is not None
    assert TuiStateProvider is not None


def test_orchestratorapp_title_includes_replay_badge_for_audit_replay_mode() -> None:
    app = OrchestratorApp(
        wiring=AppWiring(
            cfg=MagicMock(),
            repo_root=Path("."),
            policy_mode=PolicyMode.AUDIT_REPLAY,
        )
    )
    assert app.title == "AgentShore [REPLAY]"


def test_orchestratorapp_title_omits_replay_badge_with_default_wiring() -> None:
    assert OrchestratorApp().title == "AgentShore"


def test_orchestratorapp_title_omits_replay_badge_in_learning_mode() -> None:
    app = OrchestratorApp(
        wiring=AppWiring(
            cfg=MagicMock(),
            repo_root=Path("."),
            policy_mode=PolicyMode.LEARNING,
        )
    )
    assert app.title == "AgentShore"
