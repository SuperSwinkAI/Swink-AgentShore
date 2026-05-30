"""Tests for SeedProjectPlay preconditions."""

from __future__ import annotations

from agentshore.beads import EpicStatus, ProjectGraph
from agentshore.plays.skill_backed.seed_project import (
    SeedProjectPlay,
    _validate_seed_audit_artifact,
)
from agentshore.state import IssueSnapshot, JsonArtifact, OrchestratorState, PlayType, SessionState


def _make_issue(n: int = 1) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=n,
        title=f"Issue {n}",
        state="open",
        priority=None,
        labels=[],
        source=None,
    )


def _make_epic() -> EpicStatus:
    return EpicStatus(
        bead_id="bd-001", title="E1", total_tasks=2, closed_tasks=0, closure_ratio=0.0
    )


def _make_state(
    *,
    open_issues: list[IssueSnapshot] | None = None,
    graph: ProjectGraph | None = None,
    plays_since_last_play_type: dict[PlayType, int] | None = None,
    last_play_success_by_type: dict[PlayType, bool] | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        open_issues=open_issues or [],
        graph=graph,
        plays_since_last_play_type=plays_since_last_play_type or {},
        last_play_success_by_type=last_play_success_by_type or {},
    )


_play = SeedProjectPlay()


def test_play_type() -> None:
    assert _play.play_type == PlayType.SEED_PROJECT


def test_skill_name() -> None:
    assert _play.skill_name == "agentshore-seed-project"


# ---------------------------------------------------------------------------
# desktop-hzgb: unified mid-session gate
# ---------------------------------------------------------------------------


def _seeded_graph() -> ProjectGraph:
    """ProjectGraph with at least one epic — i.e. a project that has already
    been seeded. Under the issue #566 fix the ceiling applies to these."""
    return ProjectGraph(epics=[_make_epic()])


def test_empty_graph_allowed_at_high_issue_count() -> None:
    """Project with no bd graph (never seeded) bypasses the ceiling.

    The carve-out exists to let seed_project run on truly-empty projects
    that need their initial graph built. The signal is the graph state,
    not the session history (issue #566 fix).
    """
    issues = [_make_issue(n) for n in range(200)]
    state = _make_state(open_issues=issues, graph=None)
    reasons = _play.preconditions(state)
    assert reasons == []


def test_seeded_project_masked_at_high_issue_count() -> None:
    """Issue #566 regression: existing project with epics + many issues = masked.

    Before the fix, the carve-out keyed on
    ``last_play_success_by_type.get(SEED_PROJECT) is None`` so a fresh session
    against an already-seeded project (e.g. example-project with 198 open
    issues and an established bd graph) bypassed the 10-issue ceiling.
    After the fix, ``state.graph.has_epics`` is the gate.
    """
    issues = [_make_issue(n) for n in range(198)]
    state = _make_state(open_issues=issues, graph=_seeded_graph())
    # No prior seed_project in this session — was the old carve-out's trigger.
    assert state.last_play_success_by_type == {}
    reasons = _play.preconditions(state)
    assert len(reasons) == 1
    reason_text = str(reasons[0])
    assert "198 open issues" in reason_text
    assert "10-issue ceiling" in reason_text


def test_post_success_allowed_below_threshold() -> None:
    """Seeded project, low issue count → allowed (the refresh path)."""
    issues = [_make_issue(n) for n in range(5)]
    state = _make_state(
        open_issues=issues,
        graph=_seeded_graph(),
        last_play_success_by_type={PlayType.SEED_PROJECT: True},
    )
    reasons = _play.preconditions(state)
    assert reasons == []


def test_post_success_masked_at_threshold() -> None:
    """Seeded project, open_issues == 10 (at the ceiling) → masked."""
    issues = [_make_issue(n) for n in range(10)]
    state = _make_state(
        open_issues=issues,
        graph=_seeded_graph(),
        last_play_success_by_type={PlayType.SEED_PROJECT: True},
    )
    reasons = _play.preconditions(state)
    assert len(reasons) == 1
    reason_text = str(reasons[0])
    assert "seed_project gated" in reason_text
    assert "10 open issues" in reason_text
    assert "10-issue ceiling" in reason_text


def test_post_failure_masked_at_threshold() -> None:
    """Seeded project, post-failure with open_issues >= 10 → masked.

    desktop-hzgb: the old post-failure carve-out (which allowed unlimited
    retries regardless of issue count) powered the 17-back-to-back-seeds
    incident. It has been removed; post-failure and post-success are now
    governed by the same threshold.
    """
    issues = [_make_issue(n) for n in range(10)]
    state = _make_state(
        open_issues=issues,
        graph=_seeded_graph(),
        last_play_success_by_type={PlayType.SEED_PROJECT: False},
    )
    reasons = _play.preconditions(state)
    assert len(reasons) == 1
    reason_text = str(reasons[0])
    assert "seed_project gated" in reason_text
    assert "10 open issues" in reason_text
    assert "10-issue ceiling" in reason_text


def test_post_failure_allowed_below_threshold() -> None:
    """Seeded project, post-failure with open_issues < ceiling → allowed."""
    issues = [_make_issue(n) for n in range(3)]
    state = _make_state(
        open_issues=issues,
        graph=_seeded_graph(),
        last_play_success_by_type={PlayType.SEED_PROJECT: False},
    )
    reasons = _play.preconditions(state)
    assert reasons == []


def test_threshold_is_tunable() -> None:
    """Overriding mid_session_issue_ceiling=5 masks at 5 issues, not 10."""
    play = SeedProjectPlay(mid_session_issue_ceiling=5)
    issues = [_make_issue(n) for n in range(7)]
    state = _make_state(
        open_issues=issues,
        graph=_seeded_graph(),
        last_play_success_by_type={PlayType.SEED_PROJECT: True},
    )
    reasons = play.preconditions(state)
    assert len(reasons) == 1
    reason_text = str(reasons[0])
    assert "7 open issues" in reason_text
    assert "5-issue ceiling" in reason_text


def test_mask_reason_contains_count_and_ceiling() -> None:
    """Mask reason text must surface both the current count and the ceiling."""
    issues = [_make_issue(n) for n in range(15)]
    state = _make_state(
        open_issues=issues,
        graph=_seeded_graph(),
        last_play_success_by_type={PlayType.SEED_PROJECT: True},
    )
    reasons = _play.preconditions(state)
    assert len(reasons) == 1
    reason_text = str(reasons[0])
    assert "15 open issues" in reason_text
    assert "10-issue ceiling" in reason_text


# ---------------------------------------------------------------------------
# Bootstrap / first-play invariants
# ---------------------------------------------------------------------------


def test_preconditions_passes_when_graph_is_none_no_issues() -> None:
    # Seed fires even with 0 open issues so it can bootstrap from design docs.
    state = _make_state(open_issues=[], graph=None)
    reasons = _play.preconditions(state)
    assert reasons == []


def test_preconditions_passes_when_graph_is_none_and_issues_exist() -> None:
    state = _make_state(open_issues=[_make_issue()], graph=None)
    reasons = _play.preconditions(state)
    assert reasons == []


def test_preconditions_passes_when_graph_empty_no_epics_and_issues_exist() -> None:
    graph = ProjectGraph()
    assert not graph.has_epics
    state = _make_state(open_issues=[_make_issue()], graph=graph)
    reasons = _play.preconditions(state)
    assert reasons == []


def test_preconditions_passes_when_empty_graph_no_issues() -> None:
    # Seed fires on an empty graph even with 0 open issues (design-doc-only bootstrap).
    graph = ProjectGraph()
    state = _make_state(open_issues=[], graph=graph)
    reasons = _play.preconditions(state)
    assert reasons == []


def test_estimated_cost_in_range() -> None:
    state = _make_state(open_issues=[_make_issue()])
    cost = _play.estimated_cost(state)
    assert 0.05 <= cost <= 0.15


def _audit_artifact(**overrides: object) -> list[JsonArtifact]:
    artifact = {
        "type": "seed_audit",
        "requirements_total": 3,
        "verified_requirements": 2,
        "represented_open_requirements": 1,
        "scope_gaps_found": 1,
        "unresolved_scope_gaps": 0,
        "unknown_requirements": 0,
        "scope_gap_issue_numbers": [101],
    }
    artifact.update(overrides)
    return [artifact]


def test_validate_seed_audit_artifact_accepts_full_coverage() -> None:
    assert _validate_seed_audit_artifact(_audit_artifact()) is None


def test_validate_seed_audit_artifact_rejects_missing_audit() -> None:
    assert "missing required seed_audit" in _validate_seed_audit_artifact([])


def test_validate_seed_audit_artifact_rejects_unresolved_gaps() -> None:
    error = _validate_seed_audit_artifact(_audit_artifact(unresolved_scope_gaps=1))
    assert error is not None
    assert "unresolved scope gaps" in error


def test_validate_seed_audit_artifact_rejects_uncovered_requirements() -> None:
    error = _validate_seed_audit_artifact(
        _audit_artifact(requirements_total=4, verified_requirements=2)
    )
    assert error is not None
    assert "did not account for all requirements" in error


def test_validate_seed_audit_artifact_rejects_scope_gap_without_issue() -> None:
    error = _validate_seed_audit_artifact(_audit_artifact(scope_gap_issue_numbers=[]))
    assert error is not None
    assert "did not create/link issues for all scope gaps" in error


def test_validate_seed_audit_artifact_rejects_runaway_scope_gaps() -> None:
    """Regression: 17 seed_project runs created dozens of phantom issues in
    v0.15.2. The Python validator now enforces a per-run cap of 25 new
    issues so a single misclassification-spiral run can't explode the
    backlog without human review."""
    runaway = _audit_artifact(
        requirements_total=30,
        verified_requirements=0,
        represented_open_requirements=0,
        scope_gaps_found=30,
        scope_gap_issue_numbers=list(range(200, 230)),
    )
    error = _validate_seed_audit_artifact(runaway)
    assert error is not None
    assert "too_many_scope_gaps_detected" in error


def test_validate_seed_audit_artifact_accepts_25_scope_gaps_at_the_cap() -> None:
    """25 is the boundary — at the cap should pass, 26+ fails. Per the
    artifact contract, scope_gaps_found is a subset of represented_open
    (gaps become represented_open once an issue is created), so for a
    50-requirement project where 25 were already done and 25 needed
    fresh issues: verified=25, represented_open=25, scope_gaps_found=25."""
    at_cap = _audit_artifact(
        requirements_total=50,
        verified_requirements=25,
        represented_open_requirements=25,
        scope_gaps_found=25,
        scope_gap_issue_numbers=list(range(200, 225)),
    )
    assert _validate_seed_audit_artifact(at_cap) is None
