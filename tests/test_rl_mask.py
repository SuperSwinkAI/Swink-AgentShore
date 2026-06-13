"""Tests for rl/mask.py — compute_action_mask correctness."""

from __future__ import annotations

from unittest.mock import MagicMock

from agentshore.errors import ErrorClass
from agentshore.plays.registry import build_default_registry
from agentshore.rl.action_space import NUM_ACTIONS, PLAY_TO_INDEX, V1_ACTION_ORDER
from agentshore.rl.mask import (
    compute_action_mask,
    compute_agent_eligibility_mask,
    compute_mask_reasons,
    compute_terminal_no_work_decision,
)
from agentshore.rl.mask_reason import MaskSource
from agentshore.state import (
    OrchestratorState,
    PlayType,
    PullRequestSnapshot,
    SessionState,
)


def _state(**kwargs: object) -> OrchestratorState:
    plays_since = kwargs.pop("plays_since_last_play_type", None)
    default_success = {PlayType.SEED_PROJECT: True, PlayType.DESIGN_AUDIT: True}
    last_success = kwargs.get("last_play_success_by_type", default_success)
    if plays_since is None:
        plays_since = {PlayType.SEED_PROJECT: 0, PlayType.DESIGN_AUDIT: 0}
    elif (
        isinstance(plays_since, dict)
        and isinstance(last_success, dict)
        and last_success.get(PlayType.SEED_PROJECT) is True
        and PlayType.SEED_PROJECT not in plays_since
    ):
        plays_since = {PlayType.SEED_PROJECT: 0, **plays_since}
    if (
        isinstance(plays_since, dict)
        and isinstance(last_success, dict)
        and PlayType.RUN_QA in plays_since
        and PlayType.RUN_QA not in last_success
    ):
        last_success = {**last_success, PlayType.RUN_QA: True}
    if (
        isinstance(plays_since, dict)
        and isinstance(last_success, dict)
        and last_success.get(PlayType.DESIGN_AUDIT) is True
        and PlayType.DESIGN_AUDIT not in plays_since
    ):
        plays_since = {PlayType.DESIGN_AUDIT: 0, **plays_since}
    base = dict(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        plays_since_last_play_type=plays_since,
        last_play_success_by_type=last_success,
    )
    base.update(kwargs)
    return OrchestratorState(**base)  # type: ignore[arg-type]


def _make_registry(precondition_pred) -> MagicMock:
    """Build a registry mock the EligibilityAuthority can read.

    The authority resolves validity via ``registry.get(pt).preconditions(state)``
    (an empty list == met) and ``play.capability`` (None == internal, no
    agent-eligibility gate). ``preconditions_met`` is also wired (some callers
    still query it directly) and kept consistent via ``precondition_pred(pt) ->
    bool``.

    ``capability`` is None on every stub so the agent-eligibility stage is a
    no-op; these helpers are only used without a ``cfg`` (the cfg-bearing tests
    use a real ``build_default_registry()``), so that stage never runs anyway.
    """
    from agentshore.rl.mask_reason import NOT_AVAILABLE

    reg = MagicMock()

    def _get(pt: PlayType) -> MagicMock:
        play = MagicMock()
        play.capability = None
        play.preconditions.return_value = [] if precondition_pred(pt) else [NOT_AVAILABLE]
        return play

    reg.get.side_effect = _get
    reg.preconditions_met.side_effect = lambda pt, _s: precondition_pred(pt)
    return reg


def _registry_all_true() -> MagicMock:
    return _make_registry(lambda _pt: True)


def _registry_all_false() -> MagicMock:
    return _make_registry(lambda _pt: False)


def test_mask_shape():
    mask = compute_action_mask(_state(), _registry_all_true())
    assert mask.shape == (NUM_ACTIONS,)


def test_mask_dtype():
    mask = compute_action_mask(_state(), _registry_all_true())
    assert mask.dtype == bool


def test_all_preconditions_met_applies_global_masks():
    mask = compute_action_mask(_state(), _registry_all_true())
    expected_masked = {
        PlayType.UNBLOCK_PR,
        PlayType.WRITE_IMPLEMENTATION_PLAN,
        PlayType.ISSUE_PICKUP,
        PlayType.CODE_REVIEW,
        PlayType.MERGE_PR,
        PlayType.SYSTEMATIC_DEBUGGING,
        PlayType.END_SESSION,
        PlayType.FUTURE_4,
        PlayType.FUTURE_7,
        PlayType.FUTURE_8,
        PlayType.REFINE_TASK_BREAKDOWN,
        PlayType.TAKE_BREAK,
        PlayType.GROOM_BACKLOG,
        # Empty fleet + no remaining work + not terminal → don't spawn an agent
        # with nothing to do (would otherwise spin the loop). See Item 7.
        PlayType.INSTANTIATE_AGENT,
    }
    assert mask.sum() == NUM_ACTIONS - len(expected_masked)
    for play_type in expected_masked:
        assert not mask[PLAY_TO_INDEX[play_type]]


def test_end_session_requires_successful_terminal_audits_even_at_full_closure():
    graph = MagicMock()
    graph.has_epics = True
    graph.global_closure_ratio = 1.0
    mask = compute_action_mask(
        _state(graph=graph, last_play_success_by_type={}),
        _registry_all_true(),
    )

    assert not mask[PLAY_TO_INDEX[PlayType.END_SESSION]]


def _manual_required_pr(number: int) -> PullRequestSnapshot:
    from agentshore.github.labels import MANUAL_REQUIRED_LABEL

    return PullRequestSnapshot(
        pr_number=number,
        title=f"PR {number}",
        state="open",
        branch=f"branch-{number}",
        issue_number=None,
        labels=[MANUAL_REQUIRED_LABEL],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
    )


def _plain_open_pr(number: int) -> PullRequestSnapshot:
    """An open PR with no manual-required label (keeps the queue drainable)."""
    return PullRequestSnapshot(
        pr_number=number,
        title=f"PR {number}",
        state="open",
        branch=f"branch-{number}",
        issue_number=None,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
    )


def _graph_with_ready_tasks() -> MagicMock:
    from types import SimpleNamespace

    graph = MagicMock()
    graph.has_epics = True
    graph.global_closure_ratio = 0.5
    graph.has_ready_tasks = True
    graph.tasks_ready = 1
    graph.tasks = [SimpleNamespace(issue_number=99, ready=True)]
    return graph


def test_end_session_masked_with_ready_tasks_when_queue_not_human_blocked():
    """Baseline for the escape hatch: ready beads tasks mask END_SESSION (point 6)
    while the PR queue is NOT human-blocked. 8 manual-required PRs (< MAX_OPEN_PRS
    - 1) plus one plain open PR keep the queue drainable (not every open PR is
    parked), so neither hatch path fires and the ready tasks keep END_SESSION
    masked."""
    from agentshore.plays.candidates import MAX_OPEN_PRS

    prs = [_manual_required_pr(100 + i) for i in range(MAX_OPEN_PRS - 2)]
    prs.append(_plain_open_pr(900))
    mask = compute_action_mask(
        _state(graph=_graph_with_ready_tasks(), pull_requests=prs),
        _registry_all_true(),
    )

    assert not mask[PLAY_TO_INDEX[PlayType.END_SESSION]]


def test_end_session_unmasked_when_all_open_prs_manual_required_and_no_work():
    """End-session-wedge fix: when *every* open PR is manual-required and there is
    no other actionable work, the queue cannot drain without a human even below
    the cap. pr_queue_human_blocked fires, so END_SESSION is no longer masked by
    the phantom ``ready_task_count > 0`` signal (the exact state that stranded the
    loop: 4-of-4 manual-required PRs + ready tasks all covered by them)."""
    prs = [_manual_required_pr(100 + i) for i in range(4)]
    mask = compute_action_mask(
        _state(graph=_graph_with_ready_tasks(), pull_requests=prs),
        _registry_all_true(),
    )

    assert mask[PLAY_TO_INDEX[PlayType.END_SESSION]]


def test_end_session_unmasked_when_pr_queue_human_blocked_despite_ready_tasks():
    """#166 escape hatch: with >= MAX_OPEN_PRS - 1 manual-required PRs the queue is
    wedged on a human, so END_SESSION becomes valid even though ready beads tasks
    would otherwise mask it via point 6."""
    from agentshore.plays.candidates import MAX_OPEN_PRS

    prs = [_manual_required_pr(100 + i) for i in range(MAX_OPEN_PRS - 1)]
    mask = compute_action_mask(
        _state(graph=_graph_with_ready_tasks(), pull_requests=prs),
        _registry_all_true(),
    )

    assert mask[PLAY_TO_INDEX[PlayType.END_SESSION]]


def test_end_session_requires_design_audit_after_successful_seed_audit():
    graph = MagicMock()
    graph.has_epics = True
    graph.global_closure_ratio = 1.0
    mask = compute_action_mask(
        _state(
            graph=graph,
            last_play_success_by_type={PlayType.SEED_PROJECT: True},
            plays_since_last_play_type={PlayType.SEED_PROJECT: 0},
        ),
        _registry_all_true(),
    )

    assert not mask[PLAY_TO_INDEX[PlayType.END_SESSION]]


def test_end_session_no_longer_requires_recent_qa():
    """Item 3: END_SESSION gates only on 'no actionable work remains'.

    Recent RUN_QA used to be a hard gate (and full closure ratio). Both are
    gone: once terminal audits are fresh and nothing actionable is left, the
    PPO is free to end the session regardless of how long ago QA ran. Ending
    is a directional decision the policy owns, not a deterministic threshold.
    """
    graph = MagicMock()
    graph.has_epics = True
    graph.global_closure_ratio = 0.5  # below the old 1.0 gate — must not matter now
    mask = compute_action_mask(_state(graph=graph), _registry_all_true())

    assert mask[PLAY_TO_INDEX[PlayType.END_SESSION]]


def test_end_session_allows_full_closure_after_successful_terminal_audits_and_qa():
    graph = MagicMock()
    graph.has_epics = True
    graph.global_closure_ratio = 1.0
    mask = compute_action_mask(
        _state(graph=graph, plays_since_last_play_type={PlayType.RUN_QA: 0}),
        _registry_all_true(),
    )

    assert mask[PLAY_TO_INDEX[PlayType.END_SESSION]]


def test_end_session_unmasks_in_open_start_when_design_audit_is_fresh():
    """Open-start sessions never run SEED_PROJECT — fall back to design_audit alone.

    Production session 08a948ed-2026-05-28 ran 150 plays / $43 with end_session
    permanently masked because no seed audit ever fired. Open-start mode must
    accept a recent successful design_audit as terminal-audit evidence.
    """
    graph = MagicMock()
    graph.has_epics = True
    graph.global_closure_ratio = 1.0
    mask = compute_action_mask(
        _state(
            graph=graph,
            last_play_success_by_type={
                PlayType.DESIGN_AUDIT: True,
                PlayType.RUN_QA: True,
            },
            plays_since_last_play_type={
                PlayType.DESIGN_AUDIT: 0,
                PlayType.RUN_QA: 0,
            },
        ),
        _registry_all_true(),
    )

    assert mask[PLAY_TO_INDEX[PlayType.END_SESSION]]


def test_end_session_still_masked_in_open_start_without_design_audit():
    """Without ANY successful audit, end_session stays masked (no evidence)."""
    graph = MagicMock()
    graph.has_epics = True
    graph.global_closure_ratio = 1.0
    mask = compute_action_mask(
        _state(
            graph=graph,
            last_play_success_by_type={PlayType.RUN_QA: True},
            plays_since_last_play_type={PlayType.RUN_QA: 0},
        ),
        _registry_all_true(),
    )

    assert not mask[PLAY_TO_INDEX[PlayType.END_SESSION]]


def test_no_preconditions_met_returns_all_false():
    mask = compute_action_mask(_state(), _registry_all_false())
    assert not mask.any()


def test_mask_queries_each_action_once():
    """The authority reads validity via registry.get(pt).preconditions(state).

    Source of truth moved from registry.preconditions_met to the
    EligibilityAuthority, which calls get(pt) once per action and reads the
    play's own preconditions() — so we count those instead.
    """
    called_types: list[PlayType] = []

    reg = MagicMock()

    def _get(pt: PlayType) -> MagicMock:
        called_types.append(pt)
        play = MagicMock()
        play.capability = None
        play.preconditions.return_value = []
        return play

    reg.get.side_effect = _get
    compute_action_mask(_state(), reg)
    assert len(called_types) == NUM_ACTIONS


def test_mask_queries_in_v1_order():
    called_types: list[PlayType] = []

    reg = MagicMock()

    def _get(pt: PlayType) -> MagicMock:
        called_types.append(pt)
        play = MagicMock()
        play.capability = None
        play.preconditions.return_value = []
        return play

    reg.get.side_effect = _get
    compute_action_mask(_state(), reg)
    assert called_types == list(V1_ACTION_ORDER)


def test_mask_exception_in_preconditions_treated_as_false():
    reg = MagicMock()

    def _get(pt: PlayType) -> MagicMock:
        play = MagicMock()
        play.capability = None
        play.preconditions.side_effect = RuntimeError("boom")
        return play

    reg.get.side_effect = _get
    mask = compute_action_mask(_state(), reg)
    assert not mask.any()


def test_selective_mask():
    allowed = {PlayType.ISSUE_PICKUP, PlayType.SEED_PROJECT}

    reg = _make_registry(lambda pt: pt in allowed)

    mask = compute_action_mask(_state(open_issues=[_issue_snapshot(234)]), reg)
    assert mask.sum() == 2
    assert mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]
    assert mask[PLAY_TO_INDEX[PlayType.SEED_PROJECT]]
    assert not mask[PLAY_TO_INDEX[PlayType.CODE_REVIEW]]


def _breaker_state(strikes: int, plays_since: int) -> OrchestratorState:
    """ISSUE_PICKUP candidate-available, with a given strike count / cooldown age."""
    return _state(
        open_issues=[_issue_snapshot(501)],
        plays_since_last_play_type={
            PlayType.SEED_PROJECT: 0,
            PlayType.ISSUE_PICKUP: plays_since,
        },
        consecutive_nonproductive_by_type={PlayType.ISSUE_PICKUP: strikes},
    )


def test_circuit_breaker_benches_play_after_three_strikes():
    """3 consecutive fail/skip within the cooldown window → the play is masked."""
    mask = compute_action_mask(_breaker_state(strikes=3, plays_since=0), _registry_all_true())
    assert not mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]


def test_circuit_breaker_below_threshold_not_benched():
    mask = compute_action_mask(_breaker_state(strikes=2, plays_since=0), _registry_all_true())
    assert mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]


def test_circuit_breaker_lifts_after_cooldown():
    """Once 20 plays elapse since the last attempt the mask lifts for a retry."""
    mask = compute_action_mask(_breaker_state(strikes=3, plays_since=20), _registry_all_true())
    assert mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]


def test_circuit_breaker_excludes_reconcile_state():
    """Self-heal (reconcile_state) is never benched — it must stay available."""
    state = _state(
        plays_since_last_play_type={
            PlayType.SEED_PROJECT: 0,
            PlayType.RECONCILE_STATE: 0,
        },
        consecutive_nonproductive_by_type={PlayType.RECONCILE_STATE: 5},
    )
    mask = compute_action_mask(state, _registry_all_true())
    assert mask[PLAY_TO_INDEX[PlayType.RECONCILE_STATE]]


def test_circuit_breaker_emits_reason():
    reasons = compute_mask_reasons(_breaker_state(strikes=3, plays_since=0), _registry_all_true())
    assert reasons[PlayType.ISSUE_PICKUP].source == MaskSource.CIRCUIT_BREAKER


def test_mask_blocks_seed_project_when_precondition_fails():
    reg = MagicMock()
    reg.preconditions_met.side_effect = lambda pt, _s: pt != PlayType.SEED_PROJECT

    mask = compute_action_mask(_state(), reg)

    assert not mask[PLAY_TO_INDEX[PlayType.SEED_PROJECT]]


def test_real_mask_blocks_seed_project_when_in_flight():
    state = _state(in_flight_plays=[PlayType.SEED_PROJECT])
    mask = compute_action_mask(state, build_default_registry())

    assert not mask[PLAY_TO_INDEX[PlayType.SEED_PROJECT]]


def _instantiate_scaling_state(in_flight_plays):
    """State where INSTANTIATE_AGENT is otherwise valid for fleet scaling.

    One active idle agent + first-play evidence (so the bootstrap-first-play
    gate is satisfied), remaining work present (so the empty-fleet mask does not
    fire), and the completed-history cooldown already satisfied — leaving the
    in-flight gate as the only thing that can block the next instantiate.
    """
    from agentshore.state import AgentType

    graph = MagicMock()
    graph.has_epics = True
    graph.global_closure_ratio = 0.0
    return _state(
        agents=[_agent_snapshot("codex-1", AgentType.CODEX)],
        open_issues=[_issue_snapshot(101)],
        graph=graph,
        in_flight_plays=list(in_flight_plays),
        plays_since_last_play_type={PlayType.ISSUE_PICKUP: 0},
        last_play_success_by_type={PlayType.ISSUE_PICKUP: True},
        plays_since_last_instantiate=5,
    )


def test_real_mask_blocks_instantiate_when_instantiate_in_flight():
    """#3: an in-flight (dispatched-but-not-completed) instantiate must hold the
    cooldown so a second instantiate cannot dispatch back-to-back before the
    first lands in completion history."""
    state = _instantiate_scaling_state([PlayType.INSTANTIATE_AGENT])
    mask = compute_action_mask(state, build_default_registry())

    assert not mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]]


def test_real_mask_allows_instantiate_when_no_instantiate_in_flight():
    """Control: with nothing in flight and the completed-history cooldown
    satisfied, the same scaling state leaves INSTANTIATE_AGENT selectable."""
    state = _instantiate_scaling_state([])
    mask = compute_action_mask(state, build_default_registry())

    assert mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]]


def test_real_mask_allows_seed_project_for_existing_graph_before_audit():
    """seed_project can audit an existing beads graph before any prior run."""
    graph = MagicMock()
    graph.has_epics = True
    graph.global_closure_ratio = 0.0
    state = _state(graph=graph, plays_since_last_play_type={}, last_play_success_by_type={})
    mask = compute_action_mask(state, build_default_registry())

    assert mask[PLAY_TO_INDEX[PlayType.SEED_PROJECT]]


def test_real_mask_blocks_seed_project_when_existing_graph_already_audited():
    """desktop-hzgb: mid-session seed_project is masked when open_issues >= 10."""
    graph = MagicMock()
    graph.has_epics = True
    graph.global_closure_ratio = 0.0
    # 10 open issues meets the default ceiling — seed_project must be masked.
    issues = [_issue_snapshot(n) for n in range(10)]
    state = _state(
        graph=graph,
        open_issues=issues,
        plays_since_last_play_type={PlayType.SEED_PROJECT: 0},
    )
    mask = compute_action_mask(state, build_default_registry())

    assert not mask[PLAY_TO_INDEX[PlayType.SEED_PROJECT]]


def test_real_mask_allows_seed_project_when_backlog_empty_after_success():
    """desktop-f8d: post-success, seed_project re-runs only when backlog is fully empty.

    The previous semantic was a 50-play cooldown gate; that allowed PPO to
    re-select seed_project mid-session against a fresh issue backlog. Now
    the gate is the backlog itself.
    """
    graph = MagicMock()
    graph.has_epics = True
    graph.has_ready_tasks = False
    graph.global_closure_ratio = 0.0
    state = _state(
        graph=graph,
        open_issues=[],
        last_play_success_by_type={PlayType.SEED_PROJECT: True},
    )
    mask = compute_action_mask(state, build_default_registry())

    assert mask[PLAY_TO_INDEX[PlayType.SEED_PROJECT]]


def test_real_mask_allows_seed_project_with_empty_open_issues():
    """seed_project fires even when open_issues is empty (e.g. project with design docs but no GH issues yet)."""
    state = _state(open_issues=[], graph=None)
    mask = compute_action_mask(state, build_default_registry())

    assert mask[PLAY_TO_INDEX[PlayType.SEED_PROJECT]]


def test_real_mask_blocks_calibrate_alignment_when_no_graph():
    """calibrate_alignment is blocked when beads graph is not initialised."""
    state = _state()
    mask = compute_action_mask(state, build_default_registry())

    # state.graph is None → precondition fails → masked off
    assert not mask[PLAY_TO_INDEX[PlayType.CALIBRATE_ALIGNMENT]]


# ---------------------------------------------------------------------------
# compute_config_mask
# ---------------------------------------------------------------------------


def _make_cfg(
    *,
    enabled: tuple[str, ...] = ("claude_code", "codex"),
    max_per_config: int = 5,
):
    from agentshore.config.models import (
        AgentConfig,
        ModelTierConfig,
        RuntimeConfig,
    )

    agents = {}
    for name in ("claude_code", "codex", "gemini"):
        agents[name] = AgentConfig(
            enabled=name in enabled,
            model_tiers={"medium": ModelTierConfig(model="m", enabled=True, max=max_per_config)},
        )
    return RuntimeConfig(agents=agents)


def _make_large_qa_cfg():
    from agentshore.config.models import (
        AgentConfig,
        AgentPreferencesConfig,
        ModelTierConfig,
        RuntimeConfig,
    )

    return RuntimeConfig(
        agents={
            "codex": AgentConfig(
                enabled=True,
                model_tiers={"large": ModelTierConfig(model="m", enabled=True)},
            )
        },
        agent_preferences=AgentPreferencesConfig(),
    )


def _agent_snapshot(
    agent_id: str,
    agent_type,
    model_tier: str = "medium",
    github_identity: str | None = None,
    status=None,
    current_play_type: PlayType | None = None,
):
    from agentshore.state import AgentSnapshot, AgentStatus

    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=agent_type,
        status=status or AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        model_tier=model_tier,
        github_identity=github_identity,
        current_play_type=current_play_type,
    )


def _issue_snapshot(num: int, labels: list[str] | None = None, state: str = "open"):
    from agentshore.state import IssueSnapshot

    return IssueSnapshot(
        issue_number=num,
        title=f"Issue {num}",
        state=state,
        priority=None,
        labels=labels or [],
        source=None,
    )


def _pr_snapshot(
    pr_number: int,
    github_author: str | None = None,
    is_draft: bool = False,
    issue_number: int | None = None,
    blocked: bool = False,
    mergeable: str | None = None,
    review_decision: str | None = None,
    head_sha: str | None = None,
    last_reviewed_sha: str | None = None,
    last_review_status: str | None = None,
):
    from agentshore.state import PullRequestSnapshot

    return PullRequestSnapshot(
        pr_number=pr_number,
        title=f"PR {pr_number}",
        state="open",
        branch=f"feature/{pr_number}",
        issue_number=issue_number,
        labels=[],
        review_decision=review_decision,
        status_check_summary=None,
        is_draft=is_draft,
        blocked=blocked,
        blocked_reasons=["blocked"] if blocked else [],
        github_author=github_author,
        mergeable=mergeable,
        head_sha=head_sha,
        last_reviewed_sha=last_reviewed_sha,
        last_review_status=last_review_status,
    )


def _seeded_graph():
    graph = MagicMock()
    graph.has_epics = True
    graph.has_ready_tasks = False
    graph.tasks_ready = 0
    graph.tasks = []
    graph.global_closure_ratio = 0.0
    return graph


def test_reverse_failsafe_disabled_by_default_when_open_issue_and_idle_agent():
    from agentshore.state import AgentType

    state = _state(
        open_issues=[_issue_snapshot(234)],
        agents=[_agent_snapshot("codex-1", AgentType.CODEX)],
    )

    mask = compute_action_mask(state, _registry_all_false())

    assert not mask.any()


def test_reverse_failsafe_unmasks_work_when_enabled_with_open_issue_and_idle_agent():
    from agentshore.state import AgentType

    state = _state(
        open_issues=[_issue_snapshot(234)],
        agents=[_agent_snapshot("codex-1", AgentType.CODEX)],
    )

    mask = compute_action_mask(state, _registry_all_false(), apply_reverse_failsafe=True)

    assert mask.any()
    # With an idle agent already present the failsafe lifts WORK plays but no
    # longer grows the fleet — INSTANTIATE_AGENT stays masked (#163); spawning
    # more idle agents cannot unblock work, the bottleneck is masked dispatch.
    assert not mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]]
    assert mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]
    assert mask[PLAY_TO_INDEX[PlayType.WRITE_IMPLEMENTATION_PLAN]]
    assert mask[PLAY_TO_INDEX[PlayType.RUN_QA]]
    for hard_masked in (
        PlayType.SEED_PROJECT,
        PlayType.END_AGENT,
        PlayType.END_SESSION,
        PlayType.TAKE_BREAK,
        PlayType.FUTURE_7,
        PlayType.FUTURE_8,
    ):
        assert not mask[PLAY_TO_INDEX[hard_masked]]


def test_reverse_failsafe_masks_write_plan_when_all_open_issues_are_planned():
    """Reverse failsafe must not conjure candidates that don't exist.

    Even when the action menu is opened, write_implementation_plan stays masked
    if every open issue already carries ``agentshore/planned``. Without this gate,
    PPO can pick WIP under reverse failsafe, the resolver returns params from
    a slightly fresher candidate set, and the executor rejects on the
    precondition recheck — the noisy loop seen in desktop-wwr.
    """
    from agentshore.rl.mask import compute_reverse_failsafe_mask
    from agentshore.state import AgentType

    state = _state(
        open_issues=[
            _issue_snapshot(234, labels=["agentshore/planned"]),
            _issue_snapshot(235, labels=["agentshore/planned"]),
        ],
        agents=[_agent_snapshot("codex-1", AgentType.CODEX)],
    )

    mask = compute_reverse_failsafe_mask(state)

    assert not mask[PLAY_TO_INDEX[PlayType.WRITE_IMPLEMENTATION_PLAN]]
    # INSTANTIATE_AGENT is no longer lifted under the failsafe when an idle agent
    # already exists — spawning more cannot unblock work (#163).
    assert not mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]]


# ---------------------------------------------------------------------------
# #159 config-index fail-closed + #163 lifecycle-churn breaker
# ---------------------------------------------------------------------------


def _churn_state(**kwargs):
    """Idle fleet + open work + a stale work play → lifecycle-churn conditions (#163)."""
    from agentshore.state import AgentType

    base = dict(
        open_issues=[_issue_snapshot(901)],
        agents=[
            _agent_snapshot("a0", AgentType.CLAUDE_CODE),
            _agent_snapshot("a1", AgentType.CODEX),
        ],
        # Every work play that has run is now stale (>= 6 plays ago); only the
        # lifecycle plays are fresh — the churn signature from session 8cbc74cb.
        plays_since_last_play_type={
            PlayType.ISSUE_PICKUP: 7,
            PlayType.SEED_PROJECT: 9,
            PlayType.DESIGN_AUDIT: 8,
            PlayType.INSTANTIATE_AGENT: 0,
            PlayType.END_AGENT: 1,
        },
    )
    base.update(kwargs)
    return _state(**base)


def test_lifecycle_churn_active_fires_when_work_stale_with_idle_capacity():
    from agentshore.rl.mask import _lifecycle_churn_active

    assert _lifecycle_churn_active(_churn_state())


def test_lifecycle_churn_inactive_during_cold_start():
    """No work play has ever run → cold-start bootstrap, not churn (guard 2)."""
    from agentshore.rl.mask import _lifecycle_churn_active
    from agentshore.state import AgentType

    state = _state(
        open_issues=[_issue_snapshot(902)],
        agents=[_agent_snapshot("a0", AgentType.CLAUDE_CODE)],
        plays_since_last_play_type={PlayType.INSTANTIATE_AGENT: 0},
    )
    assert not _lifecycle_churn_active(state)


def test_lifecycle_churn_inactive_when_no_idle_agent():
    """All agents BUSY → instantiate may legitimately grow the fleet (guard 1)."""
    from agentshore.rl.mask import _lifecycle_churn_active
    from agentshore.state import AgentStatus, AgentType

    state = _churn_state(
        agents=[
            _agent_snapshot("a0", AgentType.CLAUDE_CODE, status=AgentStatus.BUSY),
            _agent_snapshot("a1", AgentType.CODEX, status=AgentStatus.BUSY),
        ],
    )
    assert not _lifecycle_churn_active(state)


def test_lifecycle_churn_inactive_when_work_recent():
    """A work play ran within the threshold → healthy interleaving, not churn (guard 3)."""
    from agentshore.rl.mask import _lifecycle_churn_active

    state = _churn_state(
        plays_since_last_play_type={
            PlayType.SEED_PROJECT: 0,
            PlayType.DESIGN_AUDIT: 0,
            PlayType.ISSUE_PICKUP: 2,  # fresh < 6
            PlayType.INSTANTIATE_AGENT: 0,
        },
    )
    assert not _lifecycle_churn_active(state)


def test_lifecycle_churn_breaker_masks_instantiate():
    """Under churn, compute_action_mask masks INSTANTIATE_AGENT even with a free cell (#163)."""
    from agentshore.state import AgentType

    cfg = _make_cfg()  # claude_code + codex, medium max=5
    config_index = (("claude_code", "medium"), ("codex", "medium"))
    # One idle codex agent occupies codex/medium (masked as idle); claude_code/medium
    # is free so instantiate is otherwise base-valid — the churn breaker masks it.
    state = _churn_state(agents=[_agent_snapshot("c0", AgentType.CODEX)])
    mask = compute_action_mask(state, build_default_registry(), cfg=cfg, config_index=config_index)
    assert not mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]]


def test_lifecycle_churn_breaker_leaves_end_agent_for_wedged_agent():
    """END_AGENT stays available under churn when an agent needs reaping (#163)."""
    cfg = _make_cfg()
    config_index = (("claude_code", "medium"), ("codex", "medium"))
    state = _churn_state(recovery_exhausted_agent_ids=frozenset({"a0"}))
    mask = compute_action_mask(state, build_default_registry(), cfg=cfg, config_index=config_index)
    assert not mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]]
    assert mask[PLAY_TO_INDEX[PlayType.END_AGENT]]


def test_lifecycle_churn_breaker_masks_both_when_no_reaping_needed():
    """Under churn with no agent needing reaping, BOTH lifecycle plays are masked
    so the selector idles instead of spinning instantiate <-> end_agent (#163).

    This is the anti-spin terminal state: with the trigger removed (WS1 trunk
    artifacts / WS2 allocation) and no wedged agent to retire, the orchestrator
    quiesces rather than churning lifecycle plays with zero dispatches."""
    cfg = _make_cfg()
    config_index = (("claude_code", "medium"), ("codex", "medium"))
    # Default _churn_state has two healthy IDLE agents and no exhausted/terminal
    # agent → _agent_needs_reaping is False.
    state = _churn_state()
    mask = compute_action_mask(state, build_default_registry(), cfg=cfg, config_index=config_index)
    assert not mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]]
    assert not mask[PLAY_TO_INDEX[PlayType.END_AGENT]]


def test_empty_config_index_hard_masks_instantiate_base():
    """An empty config_index must HARD-mask INSTANTIATE_AGENT, not bypass the gate (#159)."""
    from agentshore.state import AgentType

    cfg = _make_cfg()
    state = _state(
        open_issues=[_issue_snapshot(903)],
        agents=[_agent_snapshot("a0", AgentType.CLAUDE_CODE)],
    )
    mask = compute_action_mask(state, build_default_registry(), cfg=cfg, config_index=())
    assert not mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]]


def test_empty_config_index_masks_instantiate_reverse_failsafe():
    """The reverse failsafe must not lift INSTANTIATE_AGENT on an empty config (#159)."""
    from agentshore.rl.mask import compute_reverse_failsafe_mask
    from agentshore.state import AgentType

    cfg = _make_cfg()
    state = _state(
        open_issues=[_issue_snapshot(904)],
        agents=[_agent_snapshot("a0", AgentType.CLAUDE_CODE)],
    )
    mask = compute_reverse_failsafe_mask(state, cfg=cfg, config_index=())
    assert not mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]]


def test_reverse_failsafe_keeps_write_plan_open_when_unplanned_issue_exists():
    """Sanity check the candidate-required gate doesn't over-mask."""
    from agentshore.rl.mask import compute_reverse_failsafe_mask
    from agentshore.state import AgentType

    state = _state(
        open_issues=[_issue_snapshot(234)],  # no agentshore/planned label
        agents=[_agent_snapshot("codex-1", AgentType.CODEX)],
    )

    mask = compute_reverse_failsafe_mask(state)

    assert mask[PLAY_TO_INDEX[PlayType.WRITE_IMPLEMENTATION_PLAN]]


def test_reverse_failsafe_dead_end_controls_can_be_opened():
    from agentshore.state import AgentType

    state = _state(
        open_issues=[_issue_snapshot(234)],
        agents=[_agent_snapshot("codex-1", AgentType.CODEX)],
    )

    from agentshore.rl.mask import compute_reverse_failsafe_mask

    mask = compute_reverse_failsafe_mask(state, allow_control_plays=True)

    assert mask[PLAY_TO_INDEX[PlayType.END_AGENT]]
    assert not mask[PLAY_TO_INDEX[PlayType.END_SESSION]]


def _error_agent_snapshot(agent_id: str, error_class: ErrorClass | None):
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.ERROR,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=1,
        model_tier="medium",
        last_error_class=error_class,
    )


def test_end_agent_unmasked_for_terminal_error_agent():
    """#20: a non-recoverable ERROR agent (auth) makes END_AGENT valid even when
    its registry precondition would mask it — the PPO gets the retire option so
    the agent isn't leaked until end_session."""
    state = _state(agents=[_error_agent_snapshot("broken", ErrorClass.AUTH)])
    mask = compute_action_mask(state, _registry_all_false())
    assert mask[PLAY_TO_INDEX[PlayType.END_AGENT]]


def test_end_agent_stays_masked_for_recoverable_error_agent():
    """A rate_limit ERROR agent still has the TAKE_BREAK recovery path and isn't
    recovery-exhausted yet, so END_AGENT stays masked (recovery-first)."""
    state = _state(agents=[_error_agent_snapshot("throttled", ErrorClass.RATE_LIMIT)])
    mask = compute_action_mask(state, _registry_all_false())
    assert not mask[PLAY_TO_INDEX[PlayType.END_AGENT]]


def test_reverse_failsafe_end_session_requires_recent_terminal_evidence():
    from agentshore.state import AgentType

    state = _state(
        open_issues=[_issue_snapshot(234)],
        agents=[_agent_snapshot("codex-1", AgentType.CODEX)],
        plays_since_last_play_type={PlayType.RUN_QA: 49},
    )

    from agentshore.rl.mask import compute_reverse_failsafe_mask

    mask = compute_reverse_failsafe_mask(state, allow_control_plays=True)

    assert mask[PLAY_TO_INDEX[PlayType.END_SESSION]]


def test_candidate_gates_mask_write_plan_when_only_stale_closed_snapshots_are_unplanned():
    state = _state(
        open_issues=[
            _issue_snapshot(1, state="closed"),
            _issue_snapshot(362, labels=["agentshore/planned", "agentshore/cleanup"]),
            _issue_snapshot(381, labels=["agentshore/planned", "agentshore/slop"]),
        ]
    )

    mask = compute_action_mask(state, _registry_all_true())

    assert not mask[PLAY_TO_INDEX[PlayType.WRITE_IMPLEMENTATION_PLAN]]


def test_reverse_failsafe_stays_off_without_open_issue():
    from agentshore.state import AgentType

    state = _state(agents=[_agent_snapshot("codex-1", AgentType.CODEX)])

    mask = compute_action_mask(state, _registry_all_false(), apply_reverse_failsafe=True)

    assert not mask.any()


def test_reverse_failsafe_stays_off_without_idle_agent():
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    busy = AgentSnapshot(
        agent_id="codex-1",
        agent_type=AgentType.CODEX,
        status=AgentStatus.BUSY,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )
    state = _state(open_issues=[_issue_snapshot(234)], agents=[busy])

    mask = compute_action_mask(state, _registry_all_false(), apply_reverse_failsafe=True)

    assert not mask.any()


def test_terminal_no_work_allows_final_qa_when_qa_stale():
    from agentshore.state import AgentType

    cfg = _make_large_qa_cfg()
    state = _state(
        graph=_seeded_graph(),
        total_plays=5,
        agents=[_agent_snapshot("qa", AgentType.CODEX, "large")],
        open_issues=[_issue_snapshot(209, ["agentshore/blocked", "agentshore/disallowed"])],
        plays_since_last_play_type={PlayType.RUN_QA: 50},
    )

    mask = compute_action_mask(
        state,
        build_default_registry(),
        cfg=cfg,
        config_index=(("codex", "large"),),
    )

    # Item 1: terminal no-work no longer one-hot-forces a play. With an idle QA
    # agent and no actionable work, both RUN_QA and END_SESSION are valid and
    # the PPO chooses between them — nothing is forced.
    assert mask.sum() >= 2
    assert mask[PLAY_TO_INDEX[PlayType.RUN_QA]]
    assert mask[PLAY_TO_INDEX[PlayType.END_SESSION]]


def test_terminal_no_work_empty_fleet_offers_instantiate_or_end():
    cfg = _make_large_qa_cfg()
    state = _state(
        graph=_seeded_graph(),
        total_plays=30,
        agents=[],
        open_issues=[_issue_snapshot(209, ["agentshore/blocked", "agentshore/disallowed"])],
    )

    mask = compute_action_mask(
        state,
        build_default_registry(),
        cfg=cfg,
        config_index=(("codex", "large"),),
    )

    # Item 7: empty fleet under terminal no-work — INSTANTIATE_AGENT and
    # END_SESSION are both valid; nothing is forced. RUN_QA stays masked (no
    # idle agent can run it). The PPO opens an agent or ends the session.
    assert mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]]
    assert mask[PLAY_TO_INDEX[PlayType.END_SESSION]]
    assert not mask[PLAY_TO_INDEX[PlayType.RUN_QA]]


def test_terminal_no_work_config_mask_filters_to_large_qa_configs():
    from agentshore.config.models import (
        AgentConfig,
        AgentPreferencesConfig,
        ModelTierConfig,
        RuntimeConfig,
    )
    from agentshore.rl.mask import compute_terminal_no_work_config_mask

    cfg = RuntimeConfig(
        agents={
            "codex": AgentConfig(
                enabled=True,
                model_tiers={
                    "medium": ModelTierConfig(model="m", enabled=True),
                    "large": ModelTierConfig(model="m", enabled=True),
                },
            ),
            "unknown_agent": AgentConfig(
                enabled=True,
                model_tiers={"large": ModelTierConfig(model="m", enabled=True)},
            ),
        },
        agent_preferences=AgentPreferencesConfig(),
    )
    config_index = (("codex", "medium"), ("codex", "large"), ("unknown_agent", "large"))

    mask = compute_terminal_no_work_config_mask(_state(), cfg, config_index)

    assert mask.tolist() == [False, True, False]


def test_terminal_no_work_offers_end_and_qa_regardless_of_recency():
    from agentshore.state import AgentType

    cfg = _make_large_qa_cfg()
    state = _state(
        graph=_seeded_graph(),
        total_plays=30,
        agents=[_agent_snapshot("qa", AgentType.CODEX, "large")],
        open_issues=[_issue_snapshot(209, ["agentshore/blocked", "agentshore/disallowed"])],
        plays_since_last_play_type={PlayType.RUN_QA: 49},
    )

    mask = compute_action_mask(
        state,
        build_default_registry(),
        cfg=cfg,
        config_index=(("codex", "large"),),
    )

    # Item 1/3: a recent QA no longer one-hot-forces END_SESSION. END_SESSION
    # and RUN_QA are both valid; the policy is free to end or re-run QA.
    assert mask.sum() >= 2
    assert mask[PLAY_TO_INDEX[PlayType.END_SESSION]]
    assert mask[PLAY_TO_INDEX[PlayType.RUN_QA]]


def test_terminal_no_work_qa_recency_does_not_change_mask():
    """QA recency (49 vs 50 plays) no longer gates the terminal mask — Item 1."""
    from agentshore.state import AgentType

    cfg = _make_large_qa_cfg()

    def _mask_for(qa_plays_ago: int) -> object:
        state = _state(
            graph=_seeded_graph(),
            total_plays=30,
            agents=[_agent_snapshot("qa", AgentType.CODEX, "large")],
            open_issues=[_issue_snapshot(209, ["agentshore/blocked", "agentshore/disallowed"])],
            plays_since_last_play_type={PlayType.RUN_QA: qa_plays_ago},
        )
        return compute_action_mask(
            state,
            build_default_registry(),
            cfg=cfg,
            config_index=(("codex", "large"),),
        )

    recent = _mask_for(49)
    stale = _mask_for(50)
    # Item 1: the RUN_QA / END_SESSION terminal directional choices are
    # recency-independent. The idle-audit plays (CALIBRATE_ALIGNMENT,
    # DESIGN_AUDIT) are deliberately excepted: the eligibility refactor added an
    # idle-audit freshness gate that masks them in terminal no-work once their
    # own audit window is fresh — and QA recency feeds terminal-audit freshness,
    # so those two bits legitimately flip. Compare every other action.
    idle_audit_idx = {
        PLAY_TO_INDEX[PlayType.CALIBRATE_ALIGNMENT],
        PLAY_TO_INDEX[PlayType.DESIGN_AUDIT],
    }
    recent_rest = [b for i, b in enumerate(recent.tolist()) if i not in idle_audit_idx]
    stale_rest = [b for i, b in enumerate(stale.tolist()) if i not in idle_audit_idx]
    assert recent_rest == stale_rest
    assert recent[PLAY_TO_INDEX[PlayType.RUN_QA]]
    assert recent[PLAY_TO_INDEX[PlayType.END_SESSION]]


def test_open_start_first_tick_has_valid_action():
    """Cold open-start (zero agents, no prior plays) must never deadlock — Item 7/2.

    With the forced bootstrap removed the PPO opens the fleet itself, so the
    very first tick must leave INSTANTIATE_AGENT selectable even though no
    first-play has run yet and there are no agents. Guards the empty-fleet seam
    between the CORE mask and the open-start bootstrap removal: an all-False
    first-tick mask would hang the session (reverse failsafe needs an idle
    agent, which a cold fleet lacks).
    """
    cfg = _make_large_qa_cfg()
    state = _state(
        agents=[],
        open_issues=[_issue_snapshot(234)],
        plays_since_last_play_type={},
        plays_since_last_instantiate=None,
        total_plays=0,
    )
    mask = compute_action_mask(
        state,
        build_default_registry(),
        cfg=cfg,
        config_index=(("codex", "large"),),
    )
    assert mask.any()
    assert mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]]


def test_terminal_no_work_stays_off_when_pr_work_exists():
    from agentshore.state import AgentType

    state = _state(
        graph=_seeded_graph(),
        total_plays=30,
        agents=[_agent_snapshot("reviewer", AgentType.CODEX, "large")],
        pull_requests=[_pr_snapshot(12, github_author="someone-else")],
    )

    assert compute_terminal_no_work_decision(state, build_default_registry()) is None


def test_terminal_no_work_blocked_when_beads_ready_tasks_nonzero():
    """end_session must stay masked when beads still has open tasks (gh#35).

    GitHub workable-issue counts can lag calibrate_alignment; beads ready_task_count
    is authoritative — a non-empty beads backlog must prevent the terminal decision.
    """
    from agentshore.state import AgentType

    graph = MagicMock()
    graph.has_epics = True
    graph.has_ready_tasks = True
    graph.tasks_ready = 25  # beads says: 25 tasks still open
    graph.tasks = []
    graph.global_closure_ratio = 0.0

    state = _state(
        graph=graph,
        total_plays=30,
        agents=[_agent_snapshot("qa", AgentType.CODEX, "large")],
        # All GitHub issues blocked/disallowed → workable_issues == 0 from GitHub view
        open_issues=[_issue_snapshot(209, ["agentshore/blocked", "agentshore/disallowed"])],
        plays_since_last_play_type={
            PlayType.RUN_QA: 13
        },  # recent QA → would normally unlock end_session
    )

    decision = compute_terminal_no_work_decision(state, build_default_registry())
    assert decision is None, "end_session must be blocked when ready_task_count > 0"

    mask = compute_action_mask(state, build_default_registry())
    assert not mask[PLAY_TO_INDEX[PlayType.END_SESSION]], (
        "end_session must be masked when beads backlog non-empty"
    )


def test_open_planned_issue_unreviewed_pr_and_groom_needed_are_not_terminal_no_work():
    from agentshore.state import AgentType

    graph = MagicMock()
    graph.has_epics = True
    graph.has_ready_tasks = False
    graph.tasks_ready = 0
    graph.tasks = []
    graph.global_closure_ratio = 0.0
    state = _state(
        graph=graph,
        total_plays=30,
        agents=[_agent_snapshot("codex-1", AgentType.CODEX, "medium")],
        open_issues=[_issue_snapshot(12, ["agentshore/planned", "agentshore/ai-slop"])],
        pull_requests=[_pr_snapshot(350, github_author="trusted", mergeable="MERGEABLE")],
    )

    mask = compute_action_mask(state, build_default_registry())

    assert compute_terminal_no_work_decision(state, build_default_registry()) is None
    assert mask[PLAY_TO_INDEX[PlayType.GROOM_BACKLOG]]
    assert not mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]
    assert not mask[PLAY_TO_INDEX[PlayType.MERGE_PR]]


def test_compute_config_mask_empty_index_returns_empty():
    from agentshore.rl.mask import compute_config_mask

    mask = compute_config_mask(_state(), _make_cfg(), ())
    assert mask.shape == (0,)


def test_compute_config_mask_disabled_agent_is_masked():
    from agentshore.rl.mask import compute_config_mask

    cfg = _make_cfg(enabled=("claude_code",))  # codex disabled
    mask = compute_config_mask(
        _state(),
        cfg,
        (("claude_code", "medium"), ("codex", "medium")),
    )
    assert mask[0]  # claude_code enabled
    assert not mask[1]  # codex disabled


def test_compute_config_mask_max_per_config_blocks():
    from agentshore.rl.mask import compute_config_mask
    from agentshore.state import AgentType

    cfg = _make_cfg(max_per_config=2)
    state = _state(
        agents=[
            _agent_snapshot("a", AgentType.CLAUDE_CODE, "medium"),
            _agent_snapshot("b", AgentType.CLAUDE_CODE, "medium"),
        ]
    )
    mask = compute_config_mask(state, cfg, (("claude_code", "medium"), ("codex", "medium")))
    assert not mask[0]  # at cap
    assert mask[1]  # codex still has room


def test_compute_config_mask_blocks_only_idle_same_config():
    from agentshore.rl.mask import compute_config_mask
    from agentshore.state import AgentStatus, AgentType

    cfg = _make_cfg(max_per_config=3)
    state = _state(
        agents=[
            _agent_snapshot("idle-claude", AgentType.CLAUDE_CODE, "medium"),
            _agent_snapshot("busy-codex", AgentType.CODEX, "medium", status=AgentStatus.BUSY),
        ]
    )

    mask = compute_config_mask(state, cfg, (("claude_code", "medium"), ("codex", "medium")))

    assert not mask[0]  # idle same type/tier can take work
    assert mask[1]  # busy same type/tier is valid expansion pressure


def test_compute_config_mask_per_type_cap_blocks_fully_saturated_cells():
    """When every cell in ``config_index`` is at ``max_per_config``, the mask is empty.

    Previously this test exercised the global ``max_total`` ceiling.
    That ceiling was removed in desktop-ty04; the equivalent zero-mask
    case is now "every per-(type, tier) cell saturated."
    """
    from agentshore.rl.mask import compute_config_mask
    from agentshore.state import AgentType

    cfg = _make_cfg(max_per_config=1)
    state = _state(
        agents=[
            _agent_snapshot("a", AgentType.CLAUDE_CODE, "medium"),
            _agent_snapshot("b", AgentType.CODEX, "medium"),
        ]
    )
    mask = compute_config_mask(state, cfg, (("claude_code", "medium"), ("codex", "medium")))
    assert not mask.any()


def test_compute_config_mask_rate_limit_error_blocks_respawn():
    """A rate_limit ERROR agent counts toward per-config cap to prevent re-spawn loops."""
    from agentshore.rl.mask import compute_config_mask
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    cfg = _make_cfg(max_per_config=1, enabled=("claude_code", "gemini"))
    rate_limited = AgentSnapshot(
        agent_id="gem-1",
        agent_type=AgentType.GEMINI,
        status=AgentStatus.ERROR,
        last_error_class=ErrorClass.RATE_LIMIT,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=1,
        model_tier="medium",
    )
    state = _state(agents=[rate_limited])
    mask = compute_config_mask(state, cfg, (("gemini", "medium"), ("claude_code", "medium")))
    assert not mask[0]  # gemini blocked — rate_limit counts toward cap
    assert mask[1]  # claude_code unaffected


def test_compute_config_mask_non_rate_limit_error_counts_toward_cap():
    """Any non-terminated ERROR agent occupies its per-config slot."""
    from agentshore.rl.mask import compute_config_mask
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    cfg = _make_cfg(max_per_config=1, enabled=("claude_code",))
    unknown_error = AgentSnapshot(
        agent_id="cc-1",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.ERROR,
        last_error_class=ErrorClass.UNKNOWN,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=1,
        model_tier="medium",
    )
    state = _state(agents=[unknown_error])
    mask = compute_config_mask(state, cfg, (("claude_code", "medium"),))
    assert not mask[0]


def test_compute_config_mask_terminated_agent_frees_slot():
    from agentshore.rl.mask import compute_config_mask
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    cfg = _make_cfg(max_per_config=1, enabled=("claude_code",))
    terminated = AgentSnapshot(
        agent_id="cc-1",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.TERMINATED,
        last_error_class=ErrorClass.UNKNOWN,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=1,
        model_tier="medium",
    )
    state = _state(agents=[terminated])
    mask = compute_config_mask(state, cfg, (("claude_code", "medium"),))
    assert mask[0]


def test_compute_config_mask_provider_in_take_break_blocks_only_that_provider():
    from agentshore.rl.mask import compute_config_mask
    from agentshore.state import AgentStatus, AgentType

    cfg = _make_cfg(max_per_config=3, enabled=("claude_code", "codex"))
    state = _state(
        agents=[
            _agent_snapshot(
                "cooling-claude",
                AgentType.CLAUDE_CODE,
                "large",
                status=AgentStatus.ERROR,
                current_play_type=PlayType.TAKE_BREAK,
            )
        ]
    )

    mask = compute_config_mask(state, cfg, (("claude_code", "medium"), ("codex", "medium")))

    assert not mask[0]
    assert mask[1]


def test_compute_config_mask_invalid_model_blocks_same_config():
    """invalid_model ERROR agents block that exact model tier from respawning."""
    from agentshore.rl.mask import compute_config_mask
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    cfg = _make_cfg(max_per_config=5, enabled=("gemini", "claude_code"))
    bad_medium = AgentSnapshot(
        agent_id="gem-1",
        agent_type=AgentType.GEMINI,
        status=AgentStatus.ERROR,
        last_error_class=ErrorClass.INVALID_MODEL,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=1,
        model_tier="medium",
    )
    state = _state(agents=[bad_medium])
    mask = compute_config_mask(
        state,
        cfg,
        (("gemini", "medium"), ("gemini", "small"), ("claude_code", "medium")),
    )
    assert not mask[0]  # bad Gemini medium tier should not respawn
    assert mask[1]  # other Gemini tiers remain available
    assert mask[2]  # other agent types remain available


def test_compute_action_mask_with_no_eligible_config_zeros_instantiate_agent():
    """When every (type, tier) cell in config_index is saturated, instantiate is masked."""
    from agentshore.state import AgentType

    cfg = _make_cfg(max_per_config=1)
    state = _state(agents=[_agent_snapshot("a", AgentType.CLAUDE_CODE, "medium")])
    mask = compute_action_mask(
        state,
        build_default_registry(cfg),
        cfg=cfg,
        config_index=(("claude_code", "medium"),),
    )
    assert not mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]]


def test_compute_action_mask_all_busy_allows_instantiate_agent():
    from agentshore.state import AgentStatus, AgentType

    cfg = _make_cfg(max_per_config=3)
    state = _state(
        agents=[
            _agent_snapshot(
                "busy-claude",
                AgentType.CLAUDE_CODE,
                "medium",
                status=AgentStatus.BUSY,
            )
        ]
    )

    mask = compute_action_mask(
        state,
        build_default_registry(cfg),
        cfg=cfg,
        config_index=(("claude_code", "medium"),),
    )

    assert mask[PLAY_TO_INDEX[PlayType.INSTANTIATE_AGENT]]


# ---------------------------------------------------------------------------
# compute_agent_eligibility_mask
# ---------------------------------------------------------------------------


def _make_cfg_with_prefs(
    *,
    exclude: dict[str, list[str]] | None = None,
):
    from agentshore.config.models import (
        AgentConfig,
        AgentPreferencesConfig,
        ModelTierConfig,
        RuntimeConfig,
    )

    agents = {}
    for name in ("claude_code", "codex"):
        agents[name] = AgentConfig(
            enabled=True,
            model_tiers={"medium": ModelTierConfig(model="m", enabled=True)},
        )
    return RuntimeConfig(
        agents=agents,
        agent_preferences=AgentPreferencesConfig(exclude=exclude or {}),
    )


def test_mask_zeros_issue_pickup_when_only_small_tier_idle():
    """ISSUE_PICKUP requires medium|large; small-only fleet should mask it off."""
    from agentshore.rl.mask import compute_agent_eligibility_mask
    from agentshore.state import AgentType

    cfg = _make_cfg_with_prefs()
    state = _state(agents=[_agent_snapshot("a", AgentType.CLAUDE_CODE, "small")])
    mask = compute_agent_eligibility_mask(state, build_default_registry(), cfg=cfg)
    assert not mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]


def test_mask_allows_issue_pickup_with_medium_tier_agent():
    """Medium-tier agent satisfies ISSUE_PICKUP tier requirement."""
    from agentshore.rl.mask import compute_agent_eligibility_mask
    from agentshore.state import AgentType

    cfg = _make_cfg_with_prefs()
    state = _state(agents=[_agent_snapshot("a", AgentType.CLAUDE_CODE, "medium")])
    mask = compute_agent_eligibility_mask(state, build_default_registry(), cfg=cfg)
    assert mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]


def test_mask_zeros_run_qa_with_medium_tier_agent():
    """RUN_QA is large-only."""
    from agentshore.state import AgentType

    cfg = _make_large_qa_cfg()
    state = _state(agents=[_agent_snapshot("a", AgentType.CLAUDE_CODE, "medium")])
    mask = compute_agent_eligibility_mask(state, build_default_registry(), cfg=cfg)
    assert not mask[PLAY_TO_INDEX[PlayType.RUN_QA]]


def test_mask_zeros_calibrate_alignment_with_medium_tier_agent():
    """CALIBRATE_ALIGNMENT is large-only."""
    from agentshore.state import AgentType

    cfg = _make_large_qa_cfg()
    state = _state(agents=[_agent_snapshot("a", AgentType.CLAUDE_CODE, "medium")])
    mask = compute_agent_eligibility_mask(state, build_default_registry(), cfg=cfg)
    assert not mask[PLAY_TO_INDEX[PlayType.CALIBRATE_ALIGNMENT]]


def test_mask_zeros_code_review_when_agent_type_excluded():
    """Exclude codex from code_review; only codex IDLE → mask off CODE_REVIEW."""
    from agentshore.rl.mask import compute_agent_eligibility_mask
    from agentshore.state import AgentType

    cfg = _make_cfg_with_prefs(exclude={"code_review": ["codex"]})
    state = _state(agents=[_agent_snapshot("a", AgentType.CODEX, "medium")])
    mask = compute_agent_eligibility_mask(state, build_default_registry(), cfg=cfg)
    assert not mask[PLAY_TO_INDEX[PlayType.CODE_REVIEW]]


def test_mask_allows_code_review_when_other_type_available():
    """Pending review authored by codex + IDLE claude → CODE_REVIEW eligible."""
    from agentshore.rl.mask import compute_agent_eligibility_mask
    from agentshore.state import AgentType, PendingReviewSnapshot

    pending = PendingReviewSnapshot(
        queue_id=1,
        pr_number=1,
        author_label="codex",
        enqueued_at="2026-01-01T00:00:00Z",
    )
    cfg = _make_cfg_with_prefs()
    state = _state(
        agents=[_agent_snapshot("r", AgentType.CLAUDE_CODE, "medium")],
        pending_review_queue=[pending],
    )
    mask = compute_agent_eligibility_mask(state, build_default_registry(), cfg=cfg)
    assert mask[PLAY_TO_INDEX[PlayType.CODE_REVIEW]]


def test_mask_zeros_code_review_when_only_author_identity_idle():
    """Pending review whose author identity matches every IDLE agent → masked off.

    Identity is the deconfliction key now (not agent_type): two claudes with
    DIFFERENT GH logins are mutually eligible reviewers; two agents (any type)
    sharing one login are not.
    """
    from agentshore.rl.mask import compute_agent_eligibility_mask
    from agentshore.state import AgentType, PendingReviewSnapshot

    pending = PendingReviewSnapshot(
        queue_id=1,
        pr_number=1,
        author_label=None,
        enqueued_at="2026-01-01T00:00:00Z",
    )
    cfg = _make_cfg_with_prefs()
    state = _state(
        agents=[
            _agent_snapshot("c1", AgentType.CLAUDE_CODE, "medium", github_identity="user_a"),
            _agent_snapshot("c2", AgentType.CLAUDE_CODE, "large", github_identity="user_a"),
        ],
        pull_requests=[_pr_snapshot(1, github_author="user_a")],
        pending_review_queue=[pending],
    )
    mask = compute_agent_eligibility_mask(state, build_default_registry(), cfg=cfg)
    assert not mask[PLAY_TO_INDEX[PlayType.CODE_REVIEW]]


def test_code_review_masked_when_no_pending_reviews():
    """Empty pending_review_queue → CODE_REVIEW masked off."""
    from agentshore.rl.mask import compute_agent_eligibility_mask
    from agentshore.state import AgentType

    cfg = _make_cfg_with_prefs()
    state = _state(
        agents=[_agent_snapshot("r", AgentType.CODEX, "medium")],
        pending_review_queue=[],
    )
    mask = compute_agent_eligibility_mask(state, build_default_registry(), cfg=cfg)
    assert not mask[PLAY_TO_INDEX[PlayType.CODE_REVIEW]]


def test_code_review_unmasked_for_unreviewed_open_pr():
    """No queue row is required when an open PR still needs review."""
    from agentshore.state import AgentType

    cfg = _make_cfg_with_prefs()
    state = _state(
        agents=[_agent_snapshot("r", AgentType.CODEX, "medium", github_identity="reviewer")],
        pull_requests=[_pr_snapshot(1, github_author="author")],
    )
    mask = compute_action_mask(state, build_default_registry(), cfg=cfg)
    assert mask[PLAY_TO_INDEX[PlayType.CODE_REVIEW]]


def test_code_review_masked_for_current_reviewed_pr_without_queue():
    """Already reviewed current heads should not keep code_review PPO-visible."""
    from agentshore.state import AgentType

    cfg = _make_cfg_with_prefs()
    state = _state(
        agents=[_agent_snapshot("r", AgentType.CODEX, "medium", github_identity="reviewer")],
        pull_requests=[
            _pr_snapshot(
                1,
                github_author="author",
                head_sha="abc",
                last_reviewed_sha="abc",
                last_review_status="PASS",
            )
        ],
    )
    mask = compute_action_mask(state, build_default_registry(), cfg=cfg)
    assert not mask[PLAY_TO_INDEX[PlayType.CODE_REVIEW]]


def test_code_review_unmasked_null_author_label():
    """Pending review with author_label=None → unmasked for any IDLE reviewer."""
    from agentshore.rl.mask import compute_agent_eligibility_mask
    from agentshore.state import AgentType, PendingReviewSnapshot

    pending = PendingReviewSnapshot(
        queue_id=1,
        pr_number=1,
        author_label=None,
        enqueued_at="2026-01-01T00:00:00Z",
    )
    cfg = _make_cfg_with_prefs()
    state = _state(
        agents=[_agent_snapshot("r", AgentType.CODEX, "medium")],
        pending_review_queue=[pending],
    )
    mask = compute_agent_eligibility_mask(state, build_default_registry(), cfg=cfg)
    assert mask[PLAY_TO_INDEX[PlayType.CODE_REVIEW]]


def test_compute_mask_reasons_emits_tier_eligibility_string():
    """compute_mask_reasons surfaces 'No IDLE agent of allowed tier' for tier-blocked plays."""
    from agentshore.rl.mask import compute_mask_reasons
    from agentshore.state import AgentType

    cfg = _make_cfg_with_prefs()
    # An open issue is required so the ISSUE_PICKUP precondition is satisfied;
    # then the small-tier-only fleet is the genuine blocker and the
    # agent-eligibility stage surfaces the tier reason (the authority evaluates
    # preconditions before agent eligibility, so without work the precondition
    # reason would win first).
    state = _state(
        agents=[_agent_snapshot("a", AgentType.CLAUDE_CODE, "small")],
        open_issues=[_issue_snapshot(234)],
    )
    reasons = compute_mask_reasons(state, build_default_registry(), cfg=cfg)
    assert PlayType.ISSUE_PICKUP in reasons
    assert "tier" in reasons[PlayType.ISSUE_PICKUP].text


def test_compute_mask_reasons_explains_idle_same_config_for_instantiate():
    """INSTANTIATE_AGENT is masked when every eligible config is unspawnable.

    Eligibility refactor: the authority masks INSTANTIATE_AGENT via its config
    viability gate (``compute_config_mask`` empty → no spawnable config). With
    an idle claude_code/medium already present, that config cell is filtered
    out, leaving no eligible configuration — the authority surfaces the typed
    CONFIG reason rather than the legacy mask pipeline's free-text string.
    """
    from agentshore.rl.mask import compute_mask_reasons
    from agentshore.rl.mask_reason import MaskSource
    from agentshore.state import AgentType

    cfg = _make_cfg(enabled=("claude_code",), max_per_config=5)
    state = _state(agents=[_agent_snapshot("idle-claude", AgentType.CLAUDE_CODE, "medium")])

    reasons = compute_mask_reasons(
        state,
        build_default_registry(),
        cfg=cfg,
        config_index=(("claude_code", "medium"),),
    )

    assert reasons[PlayType.INSTANTIATE_AGENT].source == MaskSource.CONFIG
    assert reasons[PlayType.INSTANTIATE_AGENT].text == "No eligible agent configuration"


def test_code_review_masked_all_same_identity():
    """All pending reviews authored by the only IDLE agent's identity → masked off."""
    from agentshore.rl.mask import compute_agent_eligibility_mask
    from agentshore.state import AgentType, PendingReviewSnapshot

    pending_a = PendingReviewSnapshot(
        queue_id=1,
        pr_number=1,
        author_label=None,
        enqueued_at="2026-01-01T00:00:00Z",
    )
    pending_b = PendingReviewSnapshot(
        queue_id=2,
        pr_number=2,
        author_label=None,
        enqueued_at="2026-01-01T00:01:00Z",
    )
    cfg = _make_cfg_with_prefs()
    state = _state(
        agents=[_agent_snapshot("r", AgentType.CODEX, "medium", github_identity="user_a")],
        pull_requests=[
            _pr_snapshot(1, github_author="user_a"),
            _pr_snapshot(2, github_author="user_a"),
        ],
        pending_review_queue=[pending_a, pending_b],
    )
    mask = compute_agent_eligibility_mask(state, build_default_registry(), cfg=cfg)
    assert not mask[PLAY_TO_INDEX[PlayType.CODE_REVIEW]]


def test_pr_lifecycle_actions_remain_visible_with_unresolved_pre_session_prs():
    """The old-PR gate masks issue pickup, not the PR lifecycle actions."""
    from agentshore.state import AgentType

    cfg = _make_cfg_with_prefs()
    # unblock_pr allows {"large", "medium"}; include a large-tier agent so the
    # lifecycle-actions-visible assertion still holds even though medium agents
    # are now eligible too.
    agents = [
        _agent_snapshot("impl", AgentType.CLAUDE_CODE, "medium", github_identity="impl"),
        _agent_snapshot("review", AgentType.CODEX, "medium", github_identity="reviewer"),
        _agent_snapshot("senior", AgentType.CLAUDE_CODE, "large", github_identity="senior"),
    ]
    state = _state(
        agents=agents,
        open_issues=[_issue_snapshot(99)],
        pull_requests=[
            _pr_snapshot(229, review_decision="APPROVED", mergeable="MERGEABLE"),
            _pr_snapshot(230, blocked=True, mergeable="CONFLICTING"),
            _pr_snapshot(231, github_author="author"),
        ],
    )

    mask = compute_action_mask(state, build_default_registry(), cfg=cfg)

    assert mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]
    assert mask[PLAY_TO_INDEX[PlayType.CODE_REVIEW]]
    assert mask[PLAY_TO_INDEX[PlayType.MERGE_PR]]
    assert mask[PLAY_TO_INDEX[PlayType.UNBLOCK_PR]]


def test_systematic_debugging_masked_for_review_bug_root_cause_labels_during_pr_drain():
    from agentshore.state import AgentType

    cfg = _make_cfg_with_prefs()
    state = _state(
        agents=[_agent_snapshot("impl", AgentType.CLAUDE_CODE, "medium")],
        open_issues=[
            _issue_snapshot(
                222, ["agentshore/review", "agentshore/planned", "agentshore/root-cause-found"]
            ),
            _issue_snapshot(243, ["bug", "agentshore/root-cause-found"]),
        ],
        pull_requests=[_pr_snapshot(229, review_decision="APPROVED", mergeable="MERGEABLE")],
    )

    mask = compute_action_mask(state, build_default_registry(), cfg=cfg)

    assert not mask[PLAY_TO_INDEX[PlayType.SYSTEMATIC_DEBUGGING]]


def test_systematic_debugging_unmasked_for_independent_debug_issue_during_pr_drain():
    from agentshore.state import AgentType

    cfg = _make_cfg_with_prefs()
    state = _state(
        agents=[_agent_snapshot("impl", AgentType.CLAUDE_CODE, "medium")],
        open_issues=[_issue_snapshot(244, ["agentshore/debug-needed"])],
        pull_requests=[_pr_snapshot(229, review_decision="APPROVED", mergeable="MERGEABLE")],
    )

    mask = compute_action_mask(state, build_default_registry(), cfg=cfg)

    assert mask[PLAY_TO_INDEX[PlayType.SYSTEMATIC_DEBUGGING]]


# ---------------------------------------------------------------------------
# TAKE_BREAK trigger conditions
# ---------------------------------------------------------------------------


def test_take_break_enabled_for_rate_limit_error():
    """rate_limit is the primary TAKE_BREAK trigger."""
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    agent = AgentSnapshot(
        agent_id="gem-1",
        agent_type=AgentType.GEMINI,
        status=AgentStatus.ERROR,
        last_error_class=ErrorClass.RATE_LIMIT,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=1,
    )
    mask = compute_action_mask(_state(agents=[agent]), _registry_all_true())
    assert mask[PLAY_TO_INDEX[PlayType.TAKE_BREAK]]


def test_take_break_enabled_for_unknown_error():
    """unknown error is the secondary TAKE_BREAK trigger."""
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    agent = AgentSnapshot(
        agent_id="cc-1",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.ERROR,
        last_error_class=ErrorClass.UNKNOWN,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=1,
    )
    mask = compute_action_mask(_state(agents=[agent]), _registry_all_true())
    assert mask[PLAY_TO_INDEX[PlayType.TAKE_BREAK]]


def test_take_break_masked_for_other_error_classes():
    """auth/timeout errors do NOT trigger TAKE_BREAK."""
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    for ec in (ErrorClass.AUTH, ErrorClass.TIMEOUT, ErrorClass.INVALID_MODEL):
        agent = AgentSnapshot(
            agent_id="cc-1",
            agent_type=AgentType.CLAUDE_CODE,
            status=AgentStatus.ERROR,
            last_error_class=ec,
            context_size=0,
            total_cost=0.0,
            total_tokens=0,
            tasks_completed=0,
            tasks_failed=1,
        )
        mask = compute_action_mask(_state(agents=[agent]), _registry_all_true())
        assert not mask[PLAY_TO_INDEX[PlayType.TAKE_BREAK]], f"TAKE_BREAK should be masked for {ec}"


def test_take_break_does_not_force_global_session_pause_when_available():
    """A single agent cooldown must not mask unrelated plays for healthy agents."""
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    agent = AgentSnapshot(
        agent_id="gem-1",
        agent_type=AgentType.GEMINI,
        status=AgentStatus.ERROR,
        last_error_class=ErrorClass.RATE_LIMIT,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=1,
    )
    # Include an IDLE Claude so other plays would otherwise be eligible
    idle = AgentSnapshot(
        agent_id="cc-1",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=5,
        tasks_failed=0,
    )
    mask = compute_action_mask(
        _state(agents=[agent, idle], open_issues=[_issue_snapshot(234)]),
        _registry_all_true(),
    )
    assert mask[PLAY_TO_INDEX[PlayType.TAKE_BREAK]]
    assert mask.sum() > 1
    assert mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]


def test_take_break_masked_when_trigger_agent_already_cooling_down():
    """Do not dispatch duplicate TAKE_BREAK plays for the same cooling agent."""
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    agent = AgentSnapshot(
        agent_id="gem-1",
        agent_type=AgentType.GEMINI,
        status=AgentStatus.ERROR,
        last_error_class=ErrorClass.RATE_LIMIT,
        current_play_type=PlayType.TAKE_BREAK,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=1,
    )
    mask = compute_action_mask(_state(agents=[agent]), _registry_all_true())
    assert not mask[PLAY_TO_INDEX[PlayType.TAKE_BREAK]]


def test_rate_limited_type_blocks_idle_same_type_agent():
    """An IDLE Gemini agent is blocked from dispatch when another Gemini is rate-limited."""
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    rate_limited = AgentSnapshot(
        agent_id="gem-1",
        agent_type=AgentType.GEMINI,
        status=AgentStatus.ERROR,
        last_error_class=ErrorClass.RATE_LIMIT,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=1,
    )
    idle_gemini = AgentSnapshot(
        agent_id="gem-2",
        agent_type=AgentType.GEMINI,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )
    cfg = _make_cfg(enabled=("gemini",))
    state = _state(agents=[rate_limited, idle_gemini])
    elig = compute_agent_eligibility_mask(state, build_default_registry(), cfg=cfg)
    # Every non-internal play should be blocked since the only IDLE agent type
    # (gemini) is rate-limited.
    from agentshore.rl.action_space import V1_ACTION_ORDER

    for i, pt in enumerate(V1_ACTION_ORDER):
        play = build_default_registry().get(pt)
        if play.capability is not None:  # skip internal plays
            assert not elig[i], f"Expected {pt.value} to be blocked for rate-limited gemini"


# ===========================================================================
# v0.15 Phase 7 — eligibility-authority / mask-builder gate tests
#
# The legacy ``_stage_*`` free functions were deleted (their logic lives in the
# single :class:`EligibilityAuthority` + :class:`ActionMaskBuilder` pipeline).
# These tests pin the same gate behaviour through the live surface so a future
# refactor that moves a gate can't silently change the overall mask.
# ===========================================================================


def test_eligibility_precondition_seeds_from_registry():
    """Met registry preconditions make non-candidate, non-gated plays valid."""
    import numpy as np

    from agentshore.rl.eligibility import EligibilityAuthority

    mask = EligibilityAuthority(_state(), _registry_all_true()).eligibility().mask()
    assert mask.shape == (NUM_ACTIONS,)
    assert mask.dtype == np.bool_
    # Plays with no candidate / terminal gate ride straight on the precondition
    # verdict, so a met precondition leaves them valid.
    assert mask[PLAY_TO_INDEX[PlayType.RECONCILE_STATE]]
    assert mask[PLAY_TO_INDEX[PlayType.CLEANUP]]


def test_eligibility_precondition_zeros_when_registry_false():
    """The authority mask is all-False when no precondition is met."""
    from agentshore.rl.eligibility import EligibilityAuthority

    mask = EligibilityAuthority(_state(), _registry_all_false()).eligibility().mask()
    assert mask.shape == (NUM_ACTIONS,)
    assert not mask.any()


def test_eligibility_wedged_end_agent_reenables_end_agent():
    """A recovery-exhausted agent re-enables END_AGENT even when its precondition is unmet."""
    from agentshore.rl.eligibility import EligibilityAuthority

    state = _state(recovery_exhausted_agent_ids=frozenset({"a1"}))
    mask = EligibilityAuthority(state, _registry_all_false()).eligibility().mask()
    assert mask[PLAY_TO_INDEX[PlayType.END_AGENT]]


def test_eligibility_wedged_end_agent_noop_without_flag():
    """With no recovery-exhausted agent, END_AGENT stays masked (the precondition gate stands)."""
    from agentshore.rl.eligibility import EligibilityAuthority

    mask = EligibilityAuthority(_state(), _registry_all_false()).eligibility().mask()
    assert not mask[PLAY_TO_INDEX[PlayType.END_AGENT]]


def test_drain_mask_suppresses_wedged_end_agent_via_builder():
    """Drain owns END_AGENT via the short-circuit; reaching it through the builder yields
    an END_AGENT-only mask regardless of the recovery-exhausted re-enable."""
    from agentshore.rl.mask import ActionMaskBuilder

    state = _state(
        session_state=SessionState.DRAINING,
        recovery_exhausted_agent_ids=frozenset({"a1"}),
    )
    mask = ActionMaskBuilder(state, _registry_all_true()).build()
    assert mask[PLAY_TO_INDEX[PlayType.END_AGENT]]
    others = [i for i in range(NUM_ACTIONS) if i != PLAY_TO_INDEX[PlayType.END_AGENT]]
    for i in others:
        assert not mask[i], f"draining mask leaked play index {i}"


def test_builder_reserved_slots_zeroed():
    """The reserved-slot overlay zeros FUTURE_4/7/8 while leaving active slots untouched."""
    from agentshore.rl.mask import ActionMaskBuilder

    mask = ActionMaskBuilder(_state(), _registry_all_true()).build()
    # FUTURE_5 was filled in place by RECONCILE_STATE (AgentShore #593) and
    # FUTURE_6 by PRUNE — both are active slots now. FUTURE_4 (slot 14, formerly
    # browser_verification) plus FUTURE_7/8 remain reserved and stay zeroed.
    assert not mask[PLAY_TO_INDEX[PlayType.FUTURE_4]]
    assert not mask[PLAY_TO_INDEX[PlayType.FUTURE_7]]
    assert not mask[PLAY_TO_INDEX[PlayType.FUTURE_8]]
    # Active slots untouched — including the newly-active slots 11 and 19.
    assert mask[PLAY_TO_INDEX[PlayType.RECONCILE_STATE]]
    assert mask[PLAY_TO_INDEX[PlayType.PRUNE]]
    assert mask[PLAY_TO_INDEX[PlayType.SEED_PROJECT]]
    assert mask[PLAY_TO_INDEX[PlayType.CLEANUP]]


def test_eligibility_take_break_masked_when_no_rate_limit():
    """TAKE_BREAK stays masked unless an agent reports rate_limit/unknown error."""
    from agentshore.rl.eligibility import EligibilityAuthority

    mask = EligibilityAuthority(_state(agents=[]), _registry_all_true()).eligibility().mask()
    assert not mask[PLAY_TO_INDEX[PlayType.TAKE_BREAK]]


def test_builder_drain_short_circuits_to_end_agent():
    """When draining, the builder returns a mask with only END_AGENT enabled."""
    from agentshore.rl.mask import ActionMaskBuilder

    state = _state(session_state=SessionState.DRAINING)
    mask = ActionMaskBuilder(state, _registry_all_true()).build()
    assert mask[PLAY_TO_INDEX[PlayType.END_AGENT]]
    # All other slots must be off.
    others = [i for i in range(NUM_ACTIONS) if i != PLAY_TO_INDEX[PlayType.END_AGENT]]
    for i in others:
        assert not mask[i], f"draining mask leaked play index {i}"


def test_builder_no_drain_short_circuit_when_not_draining():
    """When not draining, the builder does not collapse to an END_AGENT-only mask."""
    from agentshore.rl.mask import ActionMaskBuilder

    mask = ActionMaskBuilder(_state(), _registry_all_true()).build()
    # The drain short-circuit did not fire: other plays remain valid alongside
    # (or instead of) END_AGENT, so the mask is not the END_AGENT-only collapse.
    other_valid = [
        i for i in range(NUM_ACTIONS) if mask[i] and i != PLAY_TO_INDEX[PlayType.END_AGENT]
    ]
    assert other_valid


def test_pipeline_order_invariant_short_circuit_after_zero_only():
    """The full compute_action_mask: draining must short-circuit regardless of preconditions."""
    state = _state(session_state=SessionState.DRAINING)
    mask = compute_action_mask(state, _registry_all_true())
    assert mask[PLAY_TO_INDEX[PlayType.END_AGENT]]
    # Active plays would be allowed by preconditions=True but drain short-circuit
    # zeros everything except END_AGENT.
    assert not mask[PLAY_TO_INDEX[PlayType.SEED_PROJECT]]
    assert not mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]]


# ===========================================================================
# v0.15 Phase 3 — reverse-failsafe overlay structural-superset contract
# ===========================================================================


def test_reverse_failsafe_overlay_is_structural_superset_of_base_mask():
    """compute_reverse_failsafe_mask(base_mask=...) never zeros a bit set in base_mask.

    The Phase 3 contract: reverse failsafe is an overlay that can ADD selectable
    actions, never REMOVE them. This is enforced by ``lifted | base_mask``.
    """
    import numpy as np

    from agentshore.rl.mask import compute_reverse_failsafe_mask

    state = _state(open_issues=[])
    # Construct an arbitrary base mask with some bits set, including some that
    # the reverse-failsafe hard-mask set would normally zero (FUTURE_7).
    base = np.zeros(NUM_ACTIONS, dtype=bool)
    base[PLAY_TO_INDEX[PlayType.SEED_PROJECT]] = True
    base[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]] = True
    base[PLAY_TO_INDEX[PlayType.FUTURE_7]] = True  # would be hard-masked by RF alone

    overlay = compute_reverse_failsafe_mask(state, base_mask=base)

    # Structural superset: every True bit in base is True in overlay.
    for i in range(NUM_ACTIONS):
        if base[i]:
            assert overlay[i], f"overlay zeroed base-mask bit {i}"


def test_reverse_failsafe_overlay_without_base_mask_falls_back_to_v0_14_4():
    """Calling without base_mask returns the lifted-only mask (v0.14.4 behavior)."""
    from agentshore.rl.mask import compute_reverse_failsafe_mask

    state = _state()
    no_base = compute_reverse_failsafe_mask(state)
    # Reserved future slots are always hard-masked by the lift gate. Slot 11
    # is no longer reserved (RECONCILE_STATE) and slot 19 is no longer reserved
    # (PRUNE) — FUTURE_4 (slot 14) plus FUTURE_7/8 remain in the reserved set.
    assert not no_base[PLAY_TO_INDEX[PlayType.FUTURE_4]]
    assert not no_base[PLAY_TO_INDEX[PlayType.FUTURE_7]]
    assert not no_base[PLAY_TO_INDEX[PlayType.FUTURE_8]]
