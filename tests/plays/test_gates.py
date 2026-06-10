"""Unit tests for skill_backed/gates.py.

Covers each Gate class exhaustively — particularly ArmedByFailureGate's
state machine, which is the new logic shipped for RECONCILE_STATE.

Gates are pure functions of OrchestratorState, so tests construct minimal
OrchestratorState fixtures and assert on the returned MaskReason | None.
"""

from __future__ import annotations

from agentshore.beads import BeadStatus, EpicStatus, GraphTask, ProjectGraph
from agentshore.errors import ErrorClass
from agentshore.plays.skill_backed.gates import (
    ArmedByFailureGate,
    CapabilityGate,
    CooldownGate,
    DependenciesResolvedGate,
    InFlightGate,
    WarmupGate,
)
from agentshore.rl.mask_reason import MaskClassification, MaskSource
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    BudgetSnapshot,
    IssueSnapshot,
    OrchestratorState,
    PlayType,
    SessionState,
)


def _agent(
    *,
    agent_id: str = "a1",
    status: AgentStatus = AgentStatus.IDLE,
    agent_type: AgentType = AgentType.CLAUDE_CODE,
    last_error_class: ErrorClass | None = None,
    tasks_completed: int = 1,
    tasks_failed: int = 0,
    timeout_count: int = 0,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=agent_type,
        status=status,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=tasks_completed,
        tasks_failed=tasks_failed,
        last_error_class=last_error_class,
        timeout_count=timeout_count,
    )


def _state(
    *,
    agents: list[AgentSnapshot] | None = None,
    in_flight_plays: list[PlayType] | None = None,
    plays_since_last_play_type: dict[PlayType, int] | None = None,
    last_play_success_by_type: dict[PlayType, bool] | None = None,
    last_play_skipped_by_type: dict[PlayType, bool] | None = None,
    total_plays: int = 10,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess",
        session_state=SessionState.RUNNING,
        total_plays=total_plays,
        total_cost=0.0,
        agents=[_agent()] if agents is None else agents,
        budget=BudgetSnapshot(5.0, 0.0, 5.0, 0.1),
        in_flight_plays=[] if in_flight_plays is None else in_flight_plays,
        plays_since_last_play_type=plays_since_last_play_type or {},
        last_play_success_by_type=last_play_success_by_type or {},
        last_play_skipped_by_type=last_play_skipped_by_type or {},
    )


# --- CapabilityGate ---------------------------------------------------------


def test_capability_gate_passes_when_capable_idle_agent_present() -> None:
    # CLAUDE_CODE has can_run_skill=True per capabilities defaults.
    gate = CapabilityGate("can_run_skill")
    assert gate(_state()) is None


def test_capability_gate_masks_when_no_idle_agents() -> None:
    gate = CapabilityGate("can_run_skill")
    busy = _agent(status=AgentStatus.BUSY)
    reason = gate(_state(agents=[busy]))
    assert reason is not None
    assert "no IDLE agent with can_run_skill" in reason.text
    assert reason.classification == MaskClassification.TRANSIENT
    assert reason.source == MaskSource.ELIGIBILITY


def test_capability_gate_excludes_rate_limited_agent_type() -> None:
    """Idle agent whose type is rate-limited doesn't count as capable."""
    gate = CapabilityGate("can_run_skill")
    idle = _agent()
    rate_limited = _agent(
        status=AgentStatus.ERROR,
        last_error_class=ErrorClass.RATE_LIMIT,
    )
    reason = gate(_state(agents=[idle, rate_limited]))
    # idle is the same agent_type as rate_limited; entire type is excluded.
    assert reason is not None
    assert "no IDLE agent" in reason.text


def test_capability_gate_masks_circuit_broken_agent() -> None:
    """A circuit-broken agent (0 successes + a timeout) is not counted capable (#22)."""
    gate = CapabilityGate("can_run_skill")
    broken = _agent(tasks_completed=0, timeout_count=1)
    reason = gate(_state(agents=[broken]))
    assert reason is not None
    assert "no IDLE agent with can_run_skill" in reason.text


def test_capability_gate_prefers_healthy_over_circuit_broken() -> None:
    """With one dead and one healthy agent, the play stays available (#22)."""
    gate = CapabilityGate("can_run_skill")
    broken = _agent(agent_id="dead", tasks_completed=0, timeout_count=3)
    healthy = _agent(agent_id="ok", tasks_completed=2)
    assert gate(_state(agents=[broken, healthy])) is None


def test_capability_gate_repeated_failures_circuit_break() -> None:
    """0 successes + >= CIRCUIT_BREAKER_FAILURE_LIMIT failures also benches."""
    gate = CapabilityGate("can_run_skill")
    broken = _agent(tasks_completed=0, tasks_failed=2, timeout_count=0)
    assert gate(_state(agents=[broken])) is not None


def test_capability_gate_one_failure_not_circuit_broken() -> None:
    """A single non-timeout failure with 0 successes is below the limit — not benched."""
    gate = CapabilityGate("can_run_skill")
    agent = _agent(tasks_completed=0, tasks_failed=1, timeout_count=0)
    assert gate(_state(agents=[agent])) is None


# --- InFlightGate -----------------------------------------------------------


def test_in_flight_gate_passes_when_not_in_flight() -> None:
    gate = InFlightGate(PlayType.RECONCILE_STATE)
    assert gate(_state()) is None


def test_in_flight_gate_masks_when_in_flight() -> None:
    gate = InFlightGate(PlayType.RECONCILE_STATE)
    reason = gate(_state(in_flight_plays=[PlayType.RECONCILE_STATE]))
    assert reason is not None
    assert "reconcile_state already in flight" in reason.text
    assert reason.classification == MaskClassification.TRANSIENT
    assert reason.source == MaskSource.PRECONDITION


def test_in_flight_gate_ignores_other_play_types_in_flight() -> None:
    gate = InFlightGate(PlayType.RECONCILE_STATE)
    assert gate(_state(in_flight_plays=[PlayType.CLEANUP, PlayType.MERGE_PR])) is None


# --- CooldownGate -----------------------------------------------------------


def test_cooldown_gate_passes_when_never_run() -> None:
    gate = CooldownGate(PlayType.CLEANUP, plays=20)
    assert gate(_state()) is None


def test_cooldown_gate_masks_within_window() -> None:
    gate = CooldownGate(PlayType.CLEANUP, plays=20)
    reason = gate(_state(plays_since_last_play_type={PlayType.CLEANUP: 5}))
    assert reason is not None
    assert "cleanup cooldown (5/20" in reason.text
    assert reason.classification == MaskClassification.INDEFINITE_WAIT
    assert reason.source == MaskSource.PRECONDITION


def test_cooldown_gate_passes_at_window_edge() -> None:
    gate = CooldownGate(PlayType.CLEANUP, plays=20)
    # cooldown >= limit clears the gate
    assert gate(_state(plays_since_last_play_type={PlayType.CLEANUP: 20})) is None


def test_cooldown_gate_passes_past_window() -> None:
    gate = CooldownGate(PlayType.CLEANUP, plays=20)
    assert gate(_state(plays_since_last_play_type={PlayType.CLEANUP: 50})) is None


# --- ArmedByFailureGate (the new logic) -------------------------------------


def test_armed_gate_closed_when_no_plays_run() -> None:
    """Fresh session, no plays completed → not armed."""
    gate = ArmedByFailureGate(PlayType.RECONCILE_STATE)
    reason = gate(_state())
    assert reason is not None
    assert "no observable wedge since last reconcile_state" in reason.text
    assert reason.classification == MaskClassification.TRANSIENT
    assert reason.source == MaskSource.PRECONDITION


def test_armed_gate_closed_when_all_plays_succeeded() -> None:
    """Every play type's latest run = success → not armed."""
    gate = ArmedByFailureGate(PlayType.RECONCILE_STATE)
    state = _state(
        plays_since_last_play_type={PlayType.MERGE_PR: 1, PlayType.CODE_REVIEW: 3},
        last_play_success_by_type={PlayType.MERGE_PR: True, PlayType.CODE_REVIEW: True},
    )
    assert gate(state) is not None


def test_armed_gate_opens_on_first_failure() -> None:
    """One non-reconcile failure, reconcile never ran → armed."""
    gate = ArmedByFailureGate(PlayType.RECONCILE_STATE)
    state = _state(
        plays_since_last_play_type={PlayType.MERGE_PR: 1},
        last_play_success_by_type={PlayType.MERGE_PR: False},
    )
    assert gate(state) is None  # armed = gate passes


def test_armed_gate_stays_open_across_intervening_successes() -> None:
    """Any failure newer than the last reconcile arms — successes don't decay it."""
    gate = ArmedByFailureGate(PlayType.RECONCILE_STATE)
    state = _state(
        plays_since_last_play_type={
            PlayType.MERGE_PR: 5,
            PlayType.CODE_REVIEW: 2,
            PlayType.INSTANTIATE_AGENT: 1,
        },
        last_play_success_by_type={
            PlayType.MERGE_PR: False,
            PlayType.CODE_REVIEW: True,
            PlayType.INSTANTIATE_AGENT: True,
        },
    )
    assert gate(state) is None  # merge_pr failure arms regardless of newer successes


def test_armed_gate_consumed_when_reconcile_runs_after_failure() -> None:
    """Reconcile ran more recently than the failure → consumed."""
    gate = ArmedByFailureGate(PlayType.RECONCILE_STATE)
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
    reason = gate(state)
    assert reason is not None
    assert "no observable wedge" in reason.text


def test_armed_gate_rearmed_by_post_reconcile_failure() -> None:
    """Failure newer than reconcile (age < reconcile_age) → re-armed."""
    gate = ArmedByFailureGate(PlayType.RECONCILE_STATE)
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
    assert gate(state) is None


def test_armed_gate_self_failure_does_not_self_arm() -> None:
    """A prior reconcile failure (own type) must not self-arm — would loop forever."""
    gate = ArmedByFailureGate(PlayType.RECONCILE_STATE)
    state = _state(
        plays_since_last_play_type={PlayType.RECONCILE_STATE: 2},
        last_play_success_by_type={PlayType.RECONCILE_STATE: False},
    )
    reason = gate(state)
    assert reason is not None


def test_armed_gate_self_failure_plus_other_failure_arms() -> None:
    """A prior self-failure plus a newer non-self failure → armed (other failure wins)."""
    gate = ArmedByFailureGate(PlayType.RECONCILE_STATE)
    state = _state(
        plays_since_last_play_type={
            PlayType.RECONCILE_STATE: 4,
            PlayType.MERGE_PR: 1,
        },
        last_play_success_by_type={
            PlayType.RECONCILE_STATE: False,
            PlayType.MERGE_PR: False,
        },
    )
    assert gate(state) is None


def test_armed_gate_tie_age_treated_as_consumed() -> None:
    """If reconcile_age == failure_age the strict-less-than fails → masked.

    Edge case: in practice ages can't tie for distinct play types because
    plays_since_last_play_type counts whole plays, but the strict comparison
    keeps behavior unambiguous.
    """
    gate = ArmedByFailureGate(PlayType.RECONCILE_STATE)
    state = _state(
        plays_since_last_play_type={
            PlayType.RECONCILE_STATE: 3,
            PlayType.MERGE_PR: 3,
        },
        last_play_success_by_type={
            PlayType.RECONCILE_STATE: True,
            PlayType.MERGE_PR: False,
        },
    )
    assert gate(state) is not None


def test_armed_gate_not_armed_by_a_skip() -> None:
    """A ``skip:*`` outcome (success=False but skipped) must NOT arm the gate.

    This is the no-op-spin root: a write_impl skip is recorded success=False,
    which previously re-armed reconcile every tick, producing the
    write_impl-skip ↔ reconcile-run no-op loop. A skip is not a wedge.
    """
    gate = ArmedByFailureGate(PlayType.RECONCILE_STATE)
    state = _state(
        plays_since_last_play_type={PlayType.WRITE_IMPLEMENTATION_PLAN: 1},
        last_play_success_by_type={PlayType.WRITE_IMPLEMENTATION_PLAN: False},
        last_play_skipped_by_type={PlayType.WRITE_IMPLEMENTATION_PLAN: True},
    )
    assert gate(state) is not None  # masked — a skip does not arm self-heal


def test_armed_gate_still_arms_on_genuine_failure_amid_skips() -> None:
    """A genuine (non-skip) failure still arms even when a skip is also present."""
    gate = ArmedByFailureGate(PlayType.RECONCILE_STATE)
    state = _state(
        plays_since_last_play_type={
            PlayType.WRITE_IMPLEMENTATION_PLAN: 2,
            PlayType.MERGE_PR: 1,
        },
        last_play_success_by_type={
            PlayType.WRITE_IMPLEMENTATION_PLAN: False,
            PlayType.MERGE_PR: False,
        },
        last_play_skipped_by_type={
            PlayType.WRITE_IMPLEMENTATION_PLAN: True,  # skip — ignored
            PlayType.MERGE_PR: False,  # genuine failure — arms
        },
    )
    assert gate(state) is None  # armed by the real merge_pr failure


# --- WarmupGate -------------------------------------------------------------


def test_warmup_gate_masks_below_threshold_no_prereq() -> None:
    gate = WarmupGate(threshold=20)
    reason = gate(_state(total_plays=5))
    assert reason is not None
    assert "warmup floor (5/20 plays)" in reason.text
    assert reason.classification == MaskClassification.INDEFINITE_WAIT


def test_warmup_gate_passes_at_threshold() -> None:
    gate = WarmupGate(threshold=20)
    assert gate(_state(total_plays=20)) is None


def test_warmup_gate_passes_past_threshold() -> None:
    gate = WarmupGate(threshold=20)
    assert gate(_state(total_plays=50)) is None


def test_warmup_gate_with_prereq_passes_when_prereq_not_run() -> None:
    """Warmup floor is skipped if the prerequisite hasn't run this session."""
    gate = WarmupGate(threshold=20, prerequisite=PlayType.SEED_PROJECT)
    # SEED_PROJECT not in plays_since_last_play_type → prereq never ran
    assert gate(_state(total_plays=5)) is None


def test_warmup_gate_with_prereq_enforces_when_prereq_ran() -> None:
    """Warmup floor enforced once the prerequisite has run."""
    gate = WarmupGate(threshold=20, prerequisite=PlayType.SEED_PROJECT)
    state = _state(
        total_plays=5,
        plays_since_last_play_type={PlayType.SEED_PROJECT: 1},
    )
    reason = gate(state)
    assert reason is not None
    assert "warmup floor" in reason.text


# --- DependenciesResolvedGate (#96) -----------------------------------------


def _issue(number: int = 1, state: str = "OPEN") -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=number,
        title=f"Issue #{number}",
        state=state,
        priority=None,
        labels=[],
        source=None,
    )


def _epic() -> EpicStatus:
    return EpicStatus(
        bead_id="e1", title="Epic 1", total_tasks=2, closed_tasks=0, closure_ratio=0.0
    )


def _graph_task(
    *,
    bead_id: str = "t1",
    issue_number: int | None = 1,
    blocked_by_ids: frozenset[str] = frozenset(),
) -> GraphTask:
    return GraphTask(
        bead_id=bead_id,
        title="Task",
        status=BeadStatus.OPEN,
        issue_number=issue_number,
        ready=not bool(blocked_by_ids),
        blocked_by_ids=blocked_by_ids,
    )


def _dep_state(
    *,
    graph: ProjectGraph | None,
    open_issues: list[IssueSnapshot] | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess",
        session_state=SessionState.RUNNING,
        total_plays=10,
        total_cost=0.0,
        agents=[_agent()],
        budget=BudgetSnapshot(5.0, 0.0, 5.0, 0.1),
        in_flight_plays=[],
        plays_since_last_play_type={},
        last_play_success_by_type={},
        last_play_skipped_by_type={},
        graph=graph,
        open_issues=[_issue()] if open_issues is None else open_issues,
    )


def test_dep_gate_passes_when_no_graph() -> None:
    """Gate is silent when beads is not initialised (state.graph is None)."""
    gate = DependenciesResolvedGate()
    assert gate(_dep_state(graph=None)) is None


def test_dep_gate_passes_when_graph_has_no_epics() -> None:
    """Gate is silent when the graph exists but has no epics."""
    graph = ProjectGraph(epics=[], tasks=[], tasks_ready=0, tasks_total=0)
    assert DependenciesResolvedGate()(_dep_state(graph=graph)) is None


def test_dep_gate_passes_when_no_tasks_have_blocked_by_ids() -> None:
    """Gate is silent when no task in the graph has unresolved deps."""
    graph = ProjectGraph(
        epics=[_epic()],
        tasks=[_graph_task(blocked_by_ids=frozenset())],
        tasks_ready=1,
        tasks_total=1,
    )
    assert DependenciesResolvedGate()(_dep_state(graph=graph)) is None


def test_dep_gate_passes_when_some_issues_have_no_blocked_deps() -> None:
    """When at least one open issue is dep-free, the gate passes."""
    graph = ProjectGraph(
        epics=[_epic()],
        tasks=[
            _graph_task(bead_id="t1", issue_number=1, blocked_by_ids=frozenset({"dep-task-id"})),
            _graph_task(bead_id="t2", issue_number=2, blocked_by_ids=frozenset()),
        ],
        tasks_ready=1,
        tasks_total=2,
    )
    state = _dep_state(graph=graph, open_issues=[_issue(1), _issue(2)])
    assert DependenciesResolvedGate()(state) is None


def test_dep_gate_masks_when_all_open_issues_have_unresolved_deps() -> None:
    """Mask is applied when every open issue is linked to a dep-blocked task."""
    graph = ProjectGraph(
        epics=[_epic()],
        tasks=[
            _graph_task(bead_id="t1", issue_number=1, blocked_by_ids=frozenset({"dep-task-id"})),
        ],
        tasks_ready=0,
        tasks_total=1,
        tasks_blocked=1,
    )
    state = _dep_state(graph=graph, open_issues=[_issue(1)])
    reason = DependenciesResolvedGate()(state)
    assert reason is not None
    assert "blocked by unresolved beads dependencies" in reason.text
    assert reason.classification == MaskClassification.TRANSIENT
    assert reason.source == MaskSource.PRECONDITION


def test_dep_gate_masks_multiple_all_blocked_issues() -> None:
    """Mask fires when multiple open issues all have unresolved deps."""
    graph = ProjectGraph(
        epics=[_epic()],
        tasks=[
            _graph_task(bead_id="t1", issue_number=1, blocked_by_ids=frozenset({"dep-a"})),
            _graph_task(bead_id="t2", issue_number=2, blocked_by_ids=frozenset({"dep-b"})),
        ],
        tasks_ready=0,
        tasks_total=2,
        tasks_blocked=2,
    )
    state = _dep_state(graph=graph, open_issues=[_issue(1), _issue(2)])
    reason = DependenciesResolvedGate()(state)
    assert reason is not None
    assert "2" in reason.text  # count of blocked issues mentioned


def test_dep_gate_passes_when_no_open_issues() -> None:
    """Gate is silent when there are no open issues (other checks handle that)."""
    graph = ProjectGraph(
        epics=[_epic()],
        tasks=[_graph_task(blocked_by_ids=frozenset({"dep"}))],
        tasks_ready=0,
        tasks_total=1,
    )
    assert DependenciesResolvedGate()(_dep_state(graph=graph, open_issues=[])) is None


def test_dep_gate_ignores_issues_not_linked_to_blocked_tasks() -> None:
    """Open issues with no matching bead task are not considered blocked."""
    graph = ProjectGraph(
        epics=[_epic()],
        tasks=[
            # issue 2 is blocked, but issue 1 has no task entry at all
            _graph_task(bead_id="t2", issue_number=2, blocked_by_ids=frozenset({"dep"})),
        ],
        tasks_ready=0,
        tasks_total=1,
    )
    # Issue 1 is open and not in the blocked set → at least one dep-free issue
    state = _dep_state(graph=graph, open_issues=[_issue(1), _issue(2)])
    assert DependenciesResolvedGate()(state) is None
