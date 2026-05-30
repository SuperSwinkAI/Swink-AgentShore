"""Tests for PlaySelector implementations."""

from __future__ import annotations

import pytest

from agentshore.plays.base import PlayParams
from agentshore.plays.selector import FixedPlanSelector, PlaySelector
from agentshore.state import OrchestratorState, PlayType, SessionState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _state() -> OrchestratorState:
    return OrchestratorState(
        session_id="sess",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
    )


# ---------------------------------------------------------------------------
# PlaySelector is a runtime-checkable Protocol
# ---------------------------------------------------------------------------


def test_fixed_plan_selector_satisfies_protocol() -> None:
    selector = FixedPlanSelector([])
    assert isinstance(selector, PlaySelector)


# ---------------------------------------------------------------------------
# FixedPlanSelector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fixed_yields_plays_in_order() -> None:
    plan = [
        (PlayType.INSTANTIATE_AGENT, PlayParams()),
        (PlayType.ISSUE_PICKUP, PlayParams(issue_number=1)),
        (PlayType.END_SESSION, PlayParams()),
    ]
    selector = FixedPlanSelector(plan)

    results = []
    for _ in range(3):
        r = await selector.select(_state())
        assert r is not None
        results.append(r[0])

    assert results == [PlayType.INSTANTIATE_AGENT, PlayType.ISSUE_PICKUP, PlayType.END_SESSION]


@pytest.mark.asyncio
async def test_fixed_returns_none_after_exhaustion() -> None:
    selector = FixedPlanSelector([(PlayType.END_SESSION, PlayParams())])

    await selector.select(_state())  # consume the one item
    result = await selector.select(_state())
    assert result is None


@pytest.mark.asyncio
async def test_fixed_empty_plan_returns_none_immediately() -> None:
    selector = FixedPlanSelector([])
    assert await selector.select(_state()) is None
