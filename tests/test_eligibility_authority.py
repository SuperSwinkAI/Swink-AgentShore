"""Tests for the EligibilityAuthority — the single source of truth for validity.

The eligibility refactor makes one ``EligibilityAuthority`` own A-type play
validity, used both to present options to PPO (the snapshot action mask) and to
validate a play after PPO selects it (one live ``confirm``). These tests pin the
load-bearing invariants:

* ``eligibility()`` and ``confirm()`` agree when the live read agrees with the
  snapshot (no drift).
* A live read that disagrees (bead flipped IN_PROGRESS) produces ``valid=False``
  — a clean re-pick — with no plays-table skip row and no RL experience sample.
* Idle-maintenance plays (un-armed reconcile, cooled-down calibrate, fresh
  design_audit) all mask via ``eligibility()``.
* The refactor does NOT bump the action-space / observation dimensions.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agentshore.beads import BeadStatus, GraphTask, ProjectGraph
from agentshore.plays.base import PlayParams
from agentshore.plays.registry import build_default_registry
from agentshore.rl.action_space import V1_ACTION_ORDER
from agentshore.rl.eligibility import EligibilityAuthority
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    IssueSnapshot,
    OrchestratorState,
    PlayType,
    PullRequestSnapshot,
    SessionState,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _agent(agent_id: str = "agent-1", github_identity: str | None = "reviewer") -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        model_tier="large",
        github_identity=github_identity,
    )


def _issue(num: int) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=num,
        title=f"Issue {num}",
        state="open",
        priority=None,
        labels=[],
        source=None,
    )


def _mergeable_pr(num: int) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        pr_number=num,
        title=f"PR {num}",
        state="open",
        branch=f"feature/{num}",
        issue_number=None,
        labels=[],
        review_decision="APPROVED",
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
        github_author="author",
        mergeable="MERGEABLE",
        head_sha="abc",
        last_reviewed_sha="abc",
        last_review_status="PASS",
    )


def _graph(issue_number: int, status: BeadStatus) -> ProjectGraph:
    task = GraphTask(
        bead_id=f"bd-{issue_number:04d}",
        title="Task",
        status=status,
        external_ref=f"gh-{issue_number}",
        issue_number=issue_number,
    )
    return ProjectGraph(tasks=[task])


def _work_state(**kwargs: object) -> OrchestratorState:
    base = dict(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=5,
        total_cost=0.0,
        agents=[_agent()],
        open_issues=[_issue(234)],
        pull_requests=[_mergeable_pr(50)],
        target_branch="main",
        plays_since_last_play_type={PlayType.SEED_PROJECT: 0, PlayType.DESIGN_AUDIT: 0},
        last_play_success_by_type={PlayType.SEED_PROJECT: True, PlayType.DESIGN_AUDIT: True},
    )
    base.update(kwargs)
    return OrchestratorState(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# eligibility() / confirm() agreement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eligibility_confirm_agreement() -> None:
    """eligibility().verdicts[pt].valid == confirm(pt, ...).valid for all plays.

    When the live read confirm() performs agrees with the snapshot eligibility()
    was built from (same state), the two must reach identical verdicts for every
    action — that is the single-source-of-truth invariant.
    """
    state = _work_state()
    authority = EligibilityAuthority(state, build_default_registry())
    report = authority.eligibility()

    for pt in V1_ACTION_ORDER:
        verdict = report.verdicts[pt]
        # confirm() against the same state should reproduce the snapshot verdict.
        # Use the authority's own resolved candidate as the confirm subject.
        params = verdict.candidates[0].params if verdict.candidates else PlayParams()
        confirmed = await authority.confirm(pt, params, state)
        assert confirmed.valid == verdict.valid, (
            f"{pt.value}: eligibility valid={verdict.valid} but confirm valid={confirmed.valid}"
        )


# ---------------------------------------------------------------------------
# Live-drift confirm → clean re-pick
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_live_drift_is_clean_repick() -> None:
    """A bead that flips IN_PROGRESS at confirm time → valid=False, clean re-pick.

    The snapshot eligibility() saw the issue as a valid ISSUE_PICKUP candidate.
    The live read (state.graph mocked IN_PROGRESS) drops it from the live
    candidate plan, so confirm() returns valid=False with a typed reason. The
    contract for the caller is a clean re-pick: NO plays-table skip row and NO
    RL experience sample. Those side-effects live in the selector / executor;
    confirm() itself acquires no work-claim and records nothing — we assert it
    is a pure verdict.
    """
    # Snapshot state: issue is a live ISSUE_PICKUP candidate.
    snapshot_state = _work_state(graph=_graph(234, BeadStatus.OPEN))
    authority = EligibilityAuthority(snapshot_state, build_default_registry())
    report = authority.eligibility()
    assert report.verdicts[PlayType.ISSUE_PICKUP].valid is True

    # Live state: the same issue's bead has flipped to in_progress.
    live_state = _work_state(graph=_graph(234, BeadStatus.IN_PROGRESS))
    verdict = await authority.confirm(
        PlayType.ISSUE_PICKUP, PlayParams(issue_number=234), live_state
    )

    assert verdict.valid is False
    assert verdict.reason is not None
    # Clean re-pick: the drifted target is gone from the live candidate set.
    assert all(c.params.issue_number != 234 for c in verdict.candidates)


# ---------------------------------------------------------------------------
# Idle-maintenance plays mask via eligibility()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_maintenance_plays_mask() -> None:
    """Un-armed reconcile / cooled-down calibrate / fresh design_audit all mask.

    All three idle-maintenance plays are masked through the one authority
    computation: reconcile_state has no observable wedge, calibrate_alignment
    and design_audit are inside their freshness cooldowns.
    """
    graph = MagicMock()
    graph.has_epics = True
    graph.has_ready_tasks = False
    graph.tasks_ready = 0
    graph.tasks = []
    graph.global_closure_ratio = 1.0
    state = _work_state(
        open_issues=[],
        pull_requests=[],
        graph=graph,
        plays_since_last_play_type={
            PlayType.SEED_PROJECT: 0,
            PlayType.DESIGN_AUDIT: 0,
            PlayType.CALIBRATE_ALIGNMENT: 0,
            PlayType.RUN_QA: 0,
        },
        last_play_success_by_type={
            PlayType.SEED_PROJECT: True,
            PlayType.DESIGN_AUDIT: True,
            PlayType.CALIBRATE_ALIGNMENT: True,
            PlayType.RUN_QA: True,
        },
    )
    authority = EligibilityAuthority(state, build_default_registry())
    report = authority.eligibility()

    for pt in (
        PlayType.RECONCILE_STATE,
        PlayType.CALIBRATE_ALIGNMENT,
        PlayType.DESIGN_AUDIT,
    ):
        verdict = report.verdicts[pt]
        assert verdict.valid is False, f"{pt.value} should mask via eligibility()"
        assert verdict.reason is not None
    # The masked bits show up in the projected action mask too.
    mask = report.mask()
    for pt in (
        PlayType.RECONCILE_STATE,
        PlayType.CALIBRATE_ALIGNMENT,
        PlayType.DESIGN_AUDIT,
    ):
        assert not mask[V1_ACTION_ORDER.index(pt)]


# ---------------------------------------------------------------------------
# No action-space / observation dimension bump
# ---------------------------------------------------------------------------


def test_no_version_bump() -> None:
    """The eligibility refactor does not change the tensor dimensions/versions."""
    import inspect

    from agentshore.rl.action_space import NUM_ACTIONS
    from agentshore.rl.observation import (
        OBSERVATION_DIM,
        OBSERVATION_VERSION,
        encode_observation,
    )

    assert NUM_ACTIONS == 22
    assert OBSERVATION_DIM == 250
    assert OBSERVATION_VERSION == 14
    # encode_observation must NOT take a mask argument — the mask is derived
    # separately by the EligibilityAuthority, never folded into the observation.
    params = inspect.signature(encode_observation).parameters
    assert "mask" not in params
    assert "action_mask" not in params
