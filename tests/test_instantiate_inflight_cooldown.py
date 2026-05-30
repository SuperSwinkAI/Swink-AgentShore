"""Tests for the instantiate in-flight cooldown gate (#3).

plays_since_last_instantiate only counts *completed* plays, so two instantiate
plays could dispatch back-to-back before the first landed in history — the
second slipped the 0/2 cooldown. An in-flight instantiate dispatch must mask the
next one.
"""

from __future__ import annotations

from agentshore.config import AgentSpawnConfig
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
        # Cooldown by completed-history count is satisfied (>= cooldown_plays).
        plays_since_last_instantiate=5,
    )


def _reasons(state: OrchestratorState) -> list[str]:
    play = InstantiateAgentPlay(AgentSpawnConfig(cooldown_plays=2))
    return [r.text for r in play.preconditions(state)]


def test_in_flight_instantiate_masks_next() -> None:
    reasons = _reasons(_state((PlayType.INSTANTIATE_AGENT,)))
    assert any("in flight" in r for r in reasons), reasons


def test_no_in_flight_instantiate_does_not_mask_on_that_reason() -> None:
    reasons = _reasons(_state((PlayType.ISSUE_PICKUP,)))
    assert not any("in flight" in r for r in reasons), reasons


def test_completed_cooldown_still_enforced_independently() -> None:
    """The completed-history cooldown still fires when count is below the floor,
    even with nothing in flight."""
    state = OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=10,
        total_cost=0.0,
        agents=[],
        in_flight_plays=[],
        plays_since_last_play_type={PlayType.SEED_PROJECT: 0},
        plays_since_last_instantiate=0,
    )
    reasons = _reasons(state)
    assert any("cooldown (0/2" in r for r in reasons), reasons
