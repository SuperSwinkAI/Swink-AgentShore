"""#458: a failed write_implementation_plan on an un-plannable issue parks it.

When the planner reports an issue is too ambiguous/large to turn into a plan by
re-running an agent, the completion handler applies ``agentshore/needs-human``
(store + GitHub) and shadows it so the very next state build drops the issue from
the candidate set. Without this the deterministic priority sort re-selects the
same issue every tick, spamming comments and burning budget with no progress.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.core.mixins.completion import CompletionProcessor
from agentshore.core.session_runtime import SessionRuntime
from agentshore.github.labels import NEEDS_HUMAN_LABEL
from agentshore.plays.base import PlayParams
from agentshore.state import PlayOutcome, PlayType


class _Harness(CompletionProcessor):
    """Minimal CompletionProcessor stand-in for _park_unplannable_issue_if_needed."""

    def __init__(self) -> None:
        self._session_id = "s1"
        self._store = AsyncMock()
        self._executor = MagicMock()
        self._executor._github = AsyncMock()
        self._runtime = SessionRuntime()
        self._host = self

    async def _safe_call(self, coro: object, label: str) -> None:
        await coro  # type: ignore[misc]


def _ctx(issue_number: int | None) -> MagicMock:
    ctx = MagicMock()
    ctx.params = PlayParams(issue_number=issue_number)
    return ctx


def _outcome(*, success: bool, error: str | None) -> PlayOutcome:
    return PlayOutcome(
        play_type=PlayType.WRITE_IMPLEMENTATION_PLAN,
        agent_id="a1",
        success=success,
        partial=False,
        duration_seconds=0.0,
        token_cost=0,
        dollar_cost=0.0,
        artifacts=[],
        alignment_delta=0.0,
        error=error,
    )


@pytest.mark.asyncio
async def test_unplannable_failure_parks_issue() -> None:
    h = _Harness()
    await h._park_unplannable_issue_if_needed(
        _ctx(458),
        _outcome(success=False, error="Issue #458 is too ambiguous to plan — needs decomposition"),
        PlayType.WRITE_IMPLEMENTATION_PLAN,
    )

    h._store.add_issue_labels.assert_awaited_once_with(458, "s1", [NEEDS_HUMAN_LABEL])
    h._executor._github.label_issue.assert_awaited_once()
    assert (458, NEEDS_HUMAN_LABEL) in h._runtime.recent_applied_labels


@pytest.mark.asyncio
async def test_successful_plan_does_not_park() -> None:
    h = _Harness()
    await h._park_unplannable_issue_if_needed(
        _ctx(458),
        _outcome(success=True, error=None),
        PlayType.WRITE_IMPLEMENTATION_PLAN,
    )
    h._store.add_issue_labels.assert_not_awaited()
    assert not h._runtime.recent_applied_labels


@pytest.mark.asyncio
async def test_transient_failure_does_not_park() -> None:
    """A failure with no un-plannable marker stays retryable — not parked."""
    h = _Harness()
    await h._park_unplannable_issue_if_needed(
        _ctx(458),
        _outcome(success=False, error="agent timed out fetching context"),
        PlayType.WRITE_IMPLEMENTATION_PLAN,
    )
    h._store.add_issue_labels.assert_not_awaited()
    assert not h._runtime.recent_applied_labels


@pytest.mark.asyncio
async def test_other_play_type_does_not_park() -> None:
    h = _Harness()
    await h._park_unplannable_issue_if_needed(
        _ctx(458),
        PlayOutcome(
            play_type=PlayType.ISSUE_PICKUP,
            agent_id="a1",
            success=False,
            partial=False,
            duration_seconds=0.0,
            token_cost=0,
            dollar_cost=0.0,
            artifacts=[],
            alignment_delta=0.0,
            error="too ambiguous",
        ),
        PlayType.ISSUE_PICKUP,
    )
    h._store.add_issue_labels.assert_not_awaited()
    assert not h._runtime.recent_applied_labels
