"""Tests for the executor's agent-selection rejection path.

When agent selection rejects all candidates (e.g., the only IDLE agent is
the wrong tier), a play should return a staffing skip so PPO does not learn
from a dispatch that never actually started.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.config import AgentPreferencesConfig, RuntimeConfig, ScopeConfig
from agentshore.errors import AntiConfirmationViolation
from agentshore.plays.base import PlayParams
from agentshore.plays.executor import PlayExecutor
from agentshore.state import (
    AgentStatus,
    AgentType,
    OrchestratorState,
    PlayOutcome,
    PlayType,
    SessionState,
)


def _make_state() -> OrchestratorState:
    return OrchestratorState(
        session_id="sess-test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[],
    )


def _make_play(play_type: PlayType, skill_name: str) -> MagicMock:
    play = MagicMock()
    play.play_type = play_type
    play.skill_name = skill_name
    play.capability = None
    play.preconditions = MagicMock(return_value=[])
    play.estimated_cost = MagicMock(return_value=0.05)
    play.execute = AsyncMock()
    # Bare MagicMock yields truthy flags; pin real bools so selection rejection
    # is a staffing skip (these plays aren't observation/requeueable).
    play.authors_prs = False
    play.retarget_pr_base = False
    play.is_handoff = False
    play.is_observation = False
    play.requeue_on_anti_confirmation = False
    return play


def _make_store() -> AsyncMock:
    store = AsyncMock()
    store.record_play = AsyncMock(return_value=42)
    store.update_play = AsyncMock()
    store.get_pr_author = AsyncMock(return_value=None)
    store.get_pr_author_type = AsyncMock(return_value=None)
    store.get_pr_github_author = AsyncMock(return_value=None)
    store.get_last_implementer = AsyncMock(return_value=None)
    return store


def _make_manager() -> MagicMock:
    manager = MagicMock()
    handle = MagicMock()
    handle.agent_id = "agent-1"
    handle.agent_type = AgentType.CLAUDE_CODE
    handle.status = AgentStatus.IDLE
    handle.context_size = 50_000
    handle.model_tier = "large"
    manager.handles = {"agent-1": handle}
    manager.branch_exposure = {}
    return manager


def _make_executor(play: MagicMock) -> PlayExecutor:
    registry = MagicMock()
    registry.get = MagicMock(return_value=play)
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(return_value=PlayParams())
    return PlayExecutor(
        registry=registry,
        resolver=resolver,
        store=_make_store(),
        manager=_make_manager(),
        cfg=RuntimeConfig(scope=ScopeConfig(), agent_preferences=AgentPreferencesConfig()),
        project_path=Path("/tmp/project"),
        session_id="sess-test",
    )


@pytest.mark.asyncio
async def test_issue_pickup_skips_when_no_eligible_agent() -> None:
    """Issue pickup records staffing as a skip, not a failed play."""
    play = _make_play(PlayType.ISSUE_PICKUP, "agentshore-issue-pickup")
    executor = _make_executor(play)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.side_effect = AntiConfirmationViolation("All agents blocked for 'issue_pickup'")
        outcome = await executor.execute(PlayType.ISSUE_PICKUP, _make_state())

    assert outcome.success is True
    assert outcome.skipped is True
    assert outcome.skip_category == "staffing"
    play.execute.assert_not_called()


@pytest.mark.asyncio
async def test_calibrate_alignment_skips_when_no_eligible_agent() -> None:
    """calibrate_alignment records staffing as a skip, not a failed play."""
    play = _make_play(PlayType.CALIBRATE_ALIGNMENT, "agentshore-calibrate-alignment")
    executor = _make_executor(play)

    with patch("agentshore.plays.executor.select_agent_for") as mock_select:
        mock_select.side_effect = AntiConfirmationViolation(
            "All agents blocked for 'calibrate_alignment' — "
            "anti-confirmation, exclude, or tier-eligibility rules eliminated all candidates"
        )
        outcome: PlayOutcome = await executor.execute(PlayType.CALIBRATE_ALIGNMENT, _make_state())

    assert outcome.success is True
    assert outcome.skipped is True
    assert outcome.skip_category == "staffing"
    play.execute.assert_not_called()
