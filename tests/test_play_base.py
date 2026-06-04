"""Tests for Play protocol, PlayParams, PlayExecutionContext."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentshore.plays.base import Play, PlayExecutionContext, PlayParams
from agentshore.state import OrchestratorState, PlayOutcome, PlayType

# ---------------------------------------------------------------------------
# PlayParams
# ---------------------------------------------------------------------------


def test_play_params_immutable_frozen() -> None:
    params = PlayParams(issue_number=42)
    with pytest.raises((AttributeError, TypeError)):
        params.issue_number = 99  # type: ignore[misc]


def test_play_params_default_extras_independent_per_instance() -> None:
    a = PlayParams()
    b = PlayParams()
    assert a.extras is not b.extras


# ---------------------------------------------------------------------------
# Play protocol — runtime_checkable
# ---------------------------------------------------------------------------


def _make_minimal_play() -> object:
    """Return a minimal object satisfying the Play protocol."""

    class _P:
        play_type = PlayType.ISSUE_PICKUP
        skill_name = "agentshore-issue-pickup"
        capability = "can_implement"

        def preconditions(self, state: OrchestratorState) -> list[str]:
            return []

        def estimated_cost(self, state: OrchestratorState) -> float:
            return 0.05

        async def execute(self, state, params, *, ctx):
            return PlayOutcome(
                play_type=PlayType.ISSUE_PICKUP,
                agent_id=None,
                success=True,
                partial=False,
                duration_seconds=1.0,
                token_cost=0,
                dollar_cost=0.0,
                artifacts=[],
                alignment_delta=0.0,
            )

    return _P()


def test_play_protocol_runtime_checkable_against_minimal_impl() -> None:
    play = _make_minimal_play()
    assert isinstance(play, Play)


def test_non_play_object_does_not_satisfy_protocol() -> None:
    assert not isinstance(object(), Play)


# ---------------------------------------------------------------------------
# PlayExecutionContext
# ---------------------------------------------------------------------------


def test_play_execution_context_carries_session_and_play_id() -> None:
    ctx = PlayExecutionContext(
        session_id="sess-123",
        play_id=42,
        manager=MagicMock(),
        store=MagicMock(),
        cfg=MagicMock(),
        project_path=Path("/tmp/project"),
    )
    assert ctx.session_id == "sess-123"
    assert ctx.play_id == 42
    assert ctx.state_provider is None
