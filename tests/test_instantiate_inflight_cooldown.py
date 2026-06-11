"""Tests for the instantiate in-flight overshoot guard.

Two instantiate plays could dispatch back-to-back before the first landed in
history. An in-flight instantiate dispatch must mask the next one so the fleet
doesn't overshoot its per-tier cap.
"""

from __future__ import annotations

from agentshore.plays.internal.instantiate_agent import InstantiateAgentPlay
from agentshore.state import OrchestratorState, PlayType, SessionState


def _state(in_flight: tuple[PlayType, ...]) -> OrchestratorState:
    return OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=10,
        total_cost=0.0,
        agents=[],
        in_flight_plays=list(in_flight),
        # A non-instantiate key satisfies the bootstrap-first-play gate so we
        # isolate the in-flight-instantiate reason.
        plays_since_last_play_type={PlayType.SEED_PROJECT: 0},
    )


def _reasons(state: OrchestratorState) -> list[str]:
    play = InstantiateAgentPlay()
    return [r.text for r in play.preconditions(state)]


def test_in_flight_instantiate_masks_next() -> None:
    reasons = _reasons(_state((PlayType.INSTANTIATE_AGENT,)))
    assert any("in flight" in r for r in reasons), reasons


def test_no_in_flight_instantiate_does_not_mask_on_that_reason() -> None:
    reasons = _reasons(_state((PlayType.ISSUE_PICKUP,)))
    assert not any("in flight" in r for r in reasons), reasons
