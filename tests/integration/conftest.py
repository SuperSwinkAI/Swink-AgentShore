"""Shared fixtures and helpers for integration tests."""

from __future__ import annotations

from agentshore.plays.base import PlayParams
from agentshore.state import PlayOutcome, PlayType


def make_outcome(
    play_type: PlayType = PlayType.ISSUE_PICKUP,
    success: bool = True,
    dollar_cost: float = 0.01,
    alignment_delta: float = 0.05,
    play_id: int | None = 1,
    agent_id: str | None = "agent-1",
) -> PlayOutcome:
    """Build a canned PlayOutcome for integration tests."""
    return PlayOutcome(
        play_type=play_type,
        agent_id=agent_id,
        success=success,
        partial=False,
        duration_seconds=1.0,
        token_cost=100,
        dollar_cost=dollar_cost,
        artifacts=[],
        alignment_delta=alignment_delta,
        play_id=play_id,
    )


def make_recording_executor(
    outcomes: list[PlayOutcome],
    store: object,
    session_id: str,
) -> tuple[object, list[PlayType]]:
    """Return a mock execute coroutine and a list that records called play_types.

    Each call pops the next outcome from *outcomes*, records a play row in the
    real DataStore so ``_build_state`` sees correct totals, and appends the
    play_type to *recorded*.
    """
    from agentshore.data.store import PlayRecord

    recorded: list[PlayType] = []
    idx = 0

    async def mock_execute(
        play_type: PlayType,
        state: object,
        *,
        override: PlayParams | None = None,
    ) -> PlayOutcome:
        nonlocal idx
        outcome = outcomes[idx]
        idx += 1
        recorded.append(play_type)

        await store.record_play(  # type: ignore[union-attr]
            PlayRecord(
                session_id=session_id,
                play_type=play_type.value,
                started_at="2026-01-01T00:00:00+00:00",
                success=outcome.success,
                agent_id=outcome.agent_id,
                dollar_cost=outcome.dollar_cost,
                token_cost=outcome.token_cost,
            )
        )
        return outcome

    return mock_execute, recorded
