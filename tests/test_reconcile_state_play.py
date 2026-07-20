"""Tests for ReconcileStatePlay (agentshore-reconcile-state skill).

Covers (a) registry wiring and slot placement, (b) the declarative gate
configuration on the play, (c) representative scenarios of the gate behavior
exercised against ReconcileStatePlay.preconditions(), (d) reward shaping,
and (e) the active-play cross-check that prevents reconcile_state from
classifying live agents as zombies (#93).
Exhaustive gate-level semantics are tested in tests/plays/test_gates.py.
"""

from __future__ import annotations

from pathlib import Path

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


# --- active-play cross-check (#93) ------------------------------------------


def _agent_snapshot_with_play(
    agent_id: str = "agent-busy",
    play_id: int = 762,
    play_type: PlayType = PlayType.CLEANUP,
) -> AgentSnapshot:
    """Return an agent snapshot that has an active in-flight play."""
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CODEX,
        status=AgentStatus.BUSY,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=3,
        tasks_failed=0,
        current_play_type=play_type,
        current_play_id=play_id,
        current_play_started_at="2026-06-09T10:00:00+00:00",
    )


def test_wedge_signals_active_agents_in_flight_populated(tmp_path: Path) -> None:
    """build_recent_wedge_signals includes agents with active plays in active_agents_in_flight.

    This is the data the reconcile skill reads to cross-check zombie candidates (#93).
    """
    from agentshore.core.wedge_signals import build_recent_wedge_signals

    busy_agent = _agent_snapshot_with_play(agent_id="agent-busy", play_id=759)
    idle_agent = AgentSnapshot(
        agent_id="agent-idle",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=5,
        tasks_failed=0,
    )
    state = OrchestratorState(
        session_id="sess-93",
        session_state=SessionState.RUNNING,
        total_plays=20,
        total_cost=1.0,
        agents=[busy_agent, idle_agent],
    )

    signals = build_recent_wedge_signals(state, tmp_path, session_id="sess-93")

    in_flight = signals["active_agents_in_flight"]
    assert isinstance(in_flight, list)
    assert len(in_flight) == 1
    entry = in_flight[0]
    assert entry["agent_id"] == "agent-busy"
    assert entry["current_play_id"] == 759
    assert entry["current_play_type"] == "cleanup"


def test_wedge_signals_active_agents_empty_when_all_idle(tmp_path: Path) -> None:
    """active_agents_in_flight is empty when no agent has an active play."""
    from agentshore.core.wedge_signals import build_recent_wedge_signals

    idle_agent = _idle_agent()
    state = OrchestratorState(
        session_id="sess-idle",
        session_state=SessionState.RUNNING,
        total_plays=5,
        total_cost=0.5,
        agents=[idle_agent],
    )

    signals = build_recent_wedge_signals(state, tmp_path, session_id="sess-idle")

    assert signals["active_agents_in_flight"] == []


# --- mid-session orphan sweep (#330) -----------------------------------------


class _FakeCtx:
    """Minimal PlayExecutionContext stand-in — mirrors test_trunk_artifacts.py's."""

    def __init__(self, store: object, project_path: Path, *, session_id: str = "sess") -> None:
        self.store = store
        self.project_path = project_path
        self.session_id = session_id


async def _record_closed_cleanup_play(store: object, *, session_id: str) -> int:
    from agentshore.data.store import PlayRecord

    return await store.record_play(  # type: ignore[attr-defined]
        PlayRecord(
            session_id=session_id,
            play_type=PlayType.CLEANUP.value,
            started_at="2026-06-12T00:01:00+00:00",
            success=False,  # killed mid-flight: never stamped ended_at
        )
    )


async def test_sweep_reclaims_prior_killed_play_orphan(tmp_path: Path) -> None:
    """A file orphaned by a previously-killed trunk-scoped play is reclaimed."""
    import os

    from agentshore.core.trunk_artifacts import snapshot_untracked_root_artifacts
    from agentshore.data.store import DataStore, SessionRecord
    from agentshore.plays.skill_backed.reconcile_state import _sweep_mid_session_orphans
    from tests.test_trunk_artifacts import _init_repo

    repo = _init_repo(tmp_path)
    (repo / ".agentshore").mkdir(exist_ok=True)
    store = DataStore(repo / ".agentshore" / "agentshore.db")
    await store.initialize()
    try:
        await store.create_session(
            SessionRecord(
                session_id="sess", project_path=str(repo), started_at="2026-06-12T00:00:00+00:00"
            )
        )
        owner = await _record_closed_cleanup_play(store, session_id="sess")
        orphan = repo / "orphan.json"
        orphan.write_text("{}")
        os.utime(orphan, (1900000000.0, 1900000000.0))

        ctx = _FakeCtx(store, repo, session_id="sess")
        state = _state(agents=[_idle_agent()])  # nobody in flight

        await _sweep_mid_session_orphans(state, ctx)

        assert not orphan.exists()
        quarantined = repo / ".agentshore" / "reclaimed" / str(owner) / "orphan.json"
        assert quarantined.exists()
        mutation = await store.get_external_mutation("sess", f"reclaim:{owner}:orphan.json")
        assert mutation is not None
        assert mutation.status == "reclaimed_reconcile"
        assert snapshot_untracked_root_artifacts(repo) == set()
    finally:
        await store.close()


async def test_sweep_protects_file_owned_by_active_trunk_play(tmp_path: Path) -> None:
    """A file bracketed by a currently-running trunk-scoped agent is left alone."""
    import os

    from agentshore.data.store import DataStore, SessionRecord
    from agentshore.plays.skill_backed.reconcile_state import _sweep_mid_session_orphans
    from tests.test_trunk_artifacts import _init_repo

    repo = _init_repo(tmp_path)
    (repo / ".agentshore").mkdir(exist_ok=True)
    store = DataStore(repo / ".agentshore" / "agentshore.db")
    await store.initialize()
    try:
        await store.create_session(
            SessionRecord(
                session_id="sess", project_path=str(repo), started_at="2026-06-12T00:00:00+00:00"
            )
        )
        owner = await _record_closed_cleanup_play(store, session_id="sess")
        in_flight = repo / "in_flight.json"
        in_flight.write_text("{}")
        os.utime(in_flight, (1900000200.0, 1900000200.0))  # after the busy agent started

        busy_agent = AgentSnapshot(
            agent_id="agent-busy",
            agent_type=AgentType.CODEX,
            status=AgentStatus.BUSY,
            context_size=0,
            total_cost=0.0,
            total_tokens=0,
            tasks_completed=0,
            tasks_failed=0,
            current_play_type=PlayType.CLEANUP,
            current_play_id=owner + 1,
            current_play_started_at="2030-03-17T17:46:40+00:00",  # epoch 1900000000
        )
        state = _state(agents=[busy_agent])

        await _sweep_mid_session_orphans(state, ctx=_FakeCtx(store, repo, session_id="sess"))

        assert in_flight.exists()
        mutation = await store.get_external_mutation("sess", f"reclaim:{owner}:in_flight.json")
        assert mutation is None
    finally:
        await store.close()


async def test_sweep_exception_is_swallowed_and_does_not_raise(tmp_path: Path) -> None:
    """A sweep failure (e.g. a DB error) never raises out of the reconcile play."""

    class _ExplodingStore:
        async def list_trunk_play_windows(self, *, play_types: list[str]) -> list[object]:
            raise RuntimeError("db exploded")

    from agentshore.plays.skill_backed.reconcile_state import _sweep_mid_session_orphans

    ctx = _FakeCtx(_ExplodingStore(), tmp_path, session_id="sess")
    state = _state(agents=[_idle_agent()])

    # Must not raise.
    await _sweep_mid_session_orphans(state, ctx)
