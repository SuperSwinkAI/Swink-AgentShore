"""Tests for ReconcileStatePlay (agentshore-reconcile-state skill).

Covers (a) registry wiring and slot placement, (b) the declarative gate
configuration on the play, (c) representative scenarios of the gate behavior
exercised against ReconcileStatePlay.preconditions(), and (d) reward shaping.
Exhaustive gate-level semantics are tested in tests/plays/test_gates.py.
"""

from __future__ import annotations

from agentshore.config.models import RewardConfig
from agentshore.plays.registry import build_default_registry
from agentshore.plays.skill_backed.gates import (
    ArmedByFailureGate,
    CapabilityGate,
    InFlightGate,
)
from agentshore.plays.skill_backed.reconcile_state import ReconcileStatePlay
from agentshore.rl.action_space import PLAY_TO_INDEX
from agentshore.rl.mask_reason import MaskClassification, MaskSource
from agentshore.rl.reward import _RECONCILE_STATE_SUCCESS_BONUS, RewardBreakdown, compute_reward
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    BudgetSnapshot,
    OrchestratorState,
    PlayType,
    SessionState,
)


def _idle_agent(agent_type: AgentType = AgentType.CLAUDE_CODE) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id="a1",
        agent_type=agent_type,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=1,
        tasks_failed=0,
    )


def _state(
    *,
    agents: list[AgentSnapshot] | None = None,
    in_flight_plays: list[PlayType] | None = None,
    plays_since_last_play_type: dict[PlayType, int] | None = None,
    last_play_success_by_type: dict[PlayType, bool] | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess",
        session_state=SessionState.RUNNING,
        total_plays=10,
        total_cost=0.0,
        agents=[_idle_agent()] if agents is None else agents,
        budget=BudgetSnapshot(5.0, 0.0, 5.0, 0.1),
        in_flight_plays=[] if in_flight_plays is None else in_flight_plays,
        plays_since_last_play_type=plays_since_last_play_type or {},
        last_play_success_by_type=last_play_success_by_type or {},
    )


# --- registry + slot --------------------------------------------------------


def test_play_registered_under_reconcile_state() -> None:
    play = build_default_registry().get(PlayType.RECONCILE_STATE)
    assert isinstance(play, ReconcileStatePlay)
    assert play.skill_name == "agentshore-reconcile-state"
    assert play.capability == "can_run_skill"


def test_slot_11_is_reconcile_state() -> None:
    assert PLAY_TO_INDEX[PlayType.RECONCILE_STATE] == 11


# --- declared gates ---------------------------------------------------------


def test_play_declares_expected_gate_set() -> None:
    """The play composes capability + in-flight + armed-by-failure gates."""
    gates = ReconcileStatePlay.gates
    assert len(gates) == 3
    assert isinstance(gates[0], CapabilityGate)
    assert gates[0].capability == "can_run_skill"
    assert isinstance(gates[1], InFlightGate)
    assert gates[1].play_type == PlayType.RECONCILE_STATE
    assert isinstance(gates[2], ArmedByFailureGate)
    assert gates[2].play_type == PlayType.RECONCILE_STATE


# --- preconditions wiring (gate behaviors covered in test_gates.py) ---------


def test_masked_when_no_failures_yet() -> None:
    """Fresh session, no observable wedge → masked by ArmedByFailureGate."""
    reasons = ReconcileStatePlay().preconditions(_state())
    assert len(reasons) == 1
    assert "no observable wedge since last reconcile_state" in reasons[0].text
    assert reasons[0].classification == MaskClassification.TRANSIENT
    assert reasons[0].source == MaskSource.PRECONDITION


def test_unmasked_after_first_non_self_failure() -> None:
    """A single merge_pr failure with no prior reconcile → eligible."""
    state = _state(
        plays_since_last_play_type={PlayType.MERGE_PR: 1},
        last_play_success_by_type={PlayType.MERGE_PR: False},
    )
    assert ReconcileStatePlay().preconditions(state) == []


def test_masked_after_reconcile_completes() -> None:
    """Reconcile ran after the failure → gate consumed."""
    state = _state(
        plays_since_last_play_type={
            PlayType.MERGE_PR: 5,
            PlayType.RECONCILE_STATE: 2,
        },
        last_play_success_by_type={
            PlayType.MERGE_PR: False,
            PlayType.RECONCILE_STATE: True,
        },
    )
    reasons = ReconcileStatePlay().preconditions(state)
    assert len(reasons) == 1
    assert "no observable wedge" in reasons[0].text


def test_rearmed_by_post_reconcile_failure() -> None:
    """Failure newer than reconcile → re-armed."""
    state = _state(
        plays_since_last_play_type={
            PlayType.RECONCILE_STATE: 4,
            PlayType.MERGE_PR: 1,
        },
        last_play_success_by_type={
            PlayType.RECONCILE_STATE: True,
            PlayType.MERGE_PR: False,
        },
    )
    assert ReconcileStatePlay().preconditions(state) == []


def test_self_failure_does_not_self_arm() -> None:
    """A prior reconcile failure must not self-arm — would loop forever."""
    state = _state(
        plays_since_last_play_type={PlayType.RECONCILE_STATE: 2},
        last_play_success_by_type={PlayType.RECONCILE_STATE: False},
    )
    reasons = ReconcileStatePlay().preconditions(state)
    assert len(reasons) == 1
    assert "no observable wedge" in reasons[0].text


# --- capability + in-flight precedence --------------------------------------


def test_capability_gate_fires_when_no_idle_agent() -> None:
    """No idle agent → ELIGIBILITY mask appears in the reason list."""
    busy = AgentSnapshot(
        agent_id="a1",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.BUSY,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=1,
        tasks_failed=0,
    )
    state = _state(
        agents=[busy],
        plays_since_last_play_type={PlayType.MERGE_PR: 1},
        last_play_success_by_type={PlayType.MERGE_PR: False},
    )
    reasons = ReconcileStatePlay().preconditions(state)
    assert any(r.source == MaskSource.ELIGIBILITY for r in reasons)


def test_in_flight_gate_fires_during_dispatch() -> None:
    """An in-flight reconcile masks subsequent dispatches."""
    state = _state(
        in_flight_plays=[PlayType.RECONCILE_STATE],
        plays_since_last_play_type={PlayType.MERGE_PR: 1},
        last_play_success_by_type={PlayType.MERGE_PR: False},
    )
    reasons = ReconcileStatePlay().preconditions(state)
    assert any("already in flight" in r.text for r in reasons)


# --- reward shaping ---------------------------------------------------------


def test_reward_bonus_applied_on_success() -> None:
    """A successful RECONCILE_STATE play earns the flat success bonus."""
    from agentshore.rl.reward import RewardSignals

    signals = RewardSignals(
        success=True,
        play_type=PlayType.RECONCILE_STATE,
        dollar_cost=0.05,
        duration_seconds=30.0,
    )
    _total, bd = compute_reward(signals, RewardConfig())
    assert bd.reconcile_state_success_bonus == _RECONCILE_STATE_SUCCESS_BONUS


def test_reward_bonus_not_applied_on_failure() -> None:
    """A failed RECONCILE_STATE play does not earn the bonus."""
    from agentshore.rl.reward import RewardSignals

    signals = RewardSignals(
        success=False,
        play_type=PlayType.RECONCILE_STATE,
        dollar_cost=0.05,
        duration_seconds=30.0,
    )
    _total, bd = compute_reward(signals, RewardConfig())
    assert bd.reconcile_state_success_bonus == 0.0


def test_reward_bonus_not_applied_to_other_play_types() -> None:
    """The bonus is gated on RECONCILE_STATE specifically, not all successes."""
    from agentshore.rl.reward import RewardSignals

    signals = RewardSignals(
        success=True,
        play_type=PlayType.CODE_REVIEW,
        dollar_cost=0.05,
        duration_seconds=30.0,
    )
    _total, bd = compute_reward(signals, RewardConfig())
    assert bd.reconcile_state_success_bonus == 0.0


def test_reward_breakdown_field_default_zero() -> None:
    """RewardBreakdown initializes the new field to 0.0 so legacy callers stay sound."""
    bd = RewardBreakdown()
    assert bd.reconcile_state_success_bonus == 0.0
