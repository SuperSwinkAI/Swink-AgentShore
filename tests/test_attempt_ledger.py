"""Shared no-progress cooldown for targeted plays (``plays.attempt_ledger``)."""

from __future__ import annotations

import dataclasses

from agentshore.errors import FailureKind
from agentshore.plays.attempt_ledger import (
    BASE_COOLDOWN_PLAYS,
    MAX_COOLDOWN_PLAYS,
    TargetAttemptLedger,
    target_key,
)
from agentshore.plays.base import PlayParams
from agentshore.plays.candidates import build_candidate_plan
from agentshore.state import (
    IssueSnapshot,
    OrchestratorState,
    PlayOutcome,
    PlayType,
    PullRequestSnapshot,
    SessionState,
)

_UNBLOCK_42 = (PlayType.UNBLOCK_PR.value, "pr:42")


def _pr(number: int = 42, **overrides: object) -> PullRequestSnapshot:
    data: dict[str, object] = dict(
        pr_number=number,
        title="t",
        state="open",
        branch=f"b-{number}",
        issue_number=None,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=True,
        blocked_reasons=["merge_conflicts"],
        head_sha="sha-1",
        mergeable="CONFLICTING",
    )
    data.update(overrides)
    return PullRequestSnapshot(**data)  # type: ignore[arg-type]


def _issue(number: int = 17, labels: list[str] | None = None) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=number,
        title="t",
        state="OPEN",
        priority=None,
        labels=labels or [],
        source=None,
    )


def _state(total_plays: int = 0, **kwargs: object) -> OrchestratorState:
    base: dict[str, object] = dict(
        session_id="s",
        session_state=SessionState.RUNNING,
        total_plays=total_plays,
        total_cost=0.0,
    )
    base.update(kwargs)
    return OrchestratorState(**base)  # type: ignore[arg-type]


def _failed(play_type: PlayType, **kwargs: object) -> PlayOutcome:
    return PlayOutcome.failed(play_type, "nope", agent_id="a1", **kwargs)  # type: ignore[arg-type]


def _unblock(ledger: TargetAttemptLedger, state: OrchestratorState, **kwargs: object) -> bool:
    return ledger.record(
        PlayType.UNBLOCK_PR,
        PlayParams(pr_number=42),
        state,
        _failed(PlayType.UNBLOCK_PR, **kwargs),
    )


def test_target_key_only_for_targeted_plays() -> None:
    assert target_key(PlayType.UNBLOCK_PR, PlayParams(pr_number=42)) == _UNBLOCK_42
    assert target_key(PlayType.ISSUE_PICKUP, PlayParams(issue_number=7)) == (
        "issue_pickup",
        "issue:7",
    )
    assert target_key(PlayType.RUN_QA, PlayParams(branch="main")) is None
    assert target_key(PlayType.ISSUE_PICKUP, PlayParams()) is None
    assert target_key(PlayType.GROOM_BACKLOG, PlayParams(issue_number=7)) is None


def test_one_attempt_keeps_the_target_available() -> None:
    ledger = TargetAttemptLedger()
    state = _state(pull_requests=[_pr()])
    assert _unblock(ledger, state) is False
    assert ledger.cooled_down(state) == frozenset()


def test_second_attempt_at_an_unchanged_target_arms_the_cooldown() -> None:
    ledger = TargetAttemptLedger()
    state = _state(total_plays=10, pull_requests=[_pr()])
    _unblock(ledger, state)
    assert _unblock(ledger, state) is True
    assert ledger.cooled_down(state) == frozenset({_UNBLOCK_42})
    late = _state(total_plays=10 + BASE_COOLDOWN_PLAYS - 1, pull_requests=[_pr()])
    assert ledger.cooled_down(late) == frozenset({_UNBLOCK_42})
    after = _state(total_plays=10 + BASE_COOLDOWN_PLAYS, pull_requests=[_pr()])
    assert ledger.cooled_down(after) == frozenset()


def test_a_reported_success_that_changes_nothing_still_counts() -> None:
    ledger = TargetAttemptLedger()
    state = _state(pull_requests=[_pr()])
    ok = PlayOutcome(
        play_type=PlayType.UNBLOCK_PR,
        agent_id="a1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=0,
        dollar_cost=0.0,
        artifacts=[],
        alignment_delta=0.0,
    )
    params = PlayParams(pr_number=42)
    assert ledger.record(PlayType.UNBLOCK_PR, params, state, ok) is False
    assert ledger.record(PlayType.UNBLOCK_PR, params, state, ok) is True


def test_a_changed_target_lifts_the_cooldown_immediately() -> None:
    ledger = TargetAttemptLedger()
    state = _state(pull_requests=[_pr()])
    _unblock(ledger, state)
    _unblock(ledger, state)
    rebased = _state(pull_requests=[_pr(head_sha="sha-2", mergeable="MERGEABLE")])
    assert ledger.cooled_down(rebased) == frozenset()
    assert _unblock(ledger, rebased) is False
    assert ledger.cooled_down(rebased) == frozenset()


def test_a_closed_target_is_forgotten() -> None:
    ledger = TargetAttemptLedger()
    state = _state(pull_requests=[_pr()])
    _unblock(ledger, state)
    _unblock(ledger, state)
    assert ledger.cooled_down(_state(pull_requests=[])) == frozenset()
    assert len(ledger) == 0


def test_cooldown_is_per_play_not_per_target() -> None:
    ledger = TargetAttemptLedger()
    state = _state(pull_requests=[_pr()])
    _unblock(ledger, state)
    _unblock(ledger, state)
    cooled = ledger.cooled_down(state)
    assert _UNBLOCK_42 in cooled
    assert (PlayType.CODE_REVIEW.value, "pr:42") not in cooled
    assert (PlayType.MERGE_PR.value, "pr:42") not in cooled


def test_attempts_that_never_exercised_the_target_are_ignored() -> None:
    ledger = TargetAttemptLedger()
    state = _state(pull_requests=[_pr()])
    for _ in range(4):
        assert _unblock(ledger, state, retry_requested=True) is False
        assert _unblock(ledger, state, failure_kind=FailureKind.AUTH) is False
        skipped = dataclasses.replace(_failed(PlayType.UNBLOCK_PR), skipped=True)
        assert ledger.record(PlayType.UNBLOCK_PR, PlayParams(pr_number=42), state, skipped) is False
    assert ledger.cooled_down(state) == frozenset()


def test_window_doubles_on_each_rearm_up_to_the_cap() -> None:
    ledger = TargetAttemptLedger()
    now = 0
    windows: list[int] = []
    for _ in range(6):
        state = _state(total_plays=now, pull_requests=[_pr()])
        _unblock(ledger, state)
        _unblock(ledger, state)
        window = 1
        while ledger.cooled_down(_state(total_plays=now + window, pull_requests=[_pr()])):
            window += 1
        windows.append(window)
        now += window
    assert windows[:4] == [
        BASE_COOLDOWN_PLAYS,
        BASE_COOLDOWN_PLAYS * 2,
        BASE_COOLDOWN_PLAYS * 4,
        MAX_COOLDOWN_PLAYS,
    ]
    assert windows[-1] == MAX_COOLDOWN_PLAYS


def test_issue_targets_rearm_when_labels_or_plan_state_change() -> None:
    ledger = TargetAttemptLedger()
    params = PlayParams(issue_number=17)
    state = _state(open_issues=[_issue()])
    for _ in range(2):
        ledger.record(PlayType.ISSUE_PICKUP, params, state, _failed(PlayType.ISSUE_PICKUP))
    assert ledger.cooled_down(state) == frozenset({("issue_pickup", "issue:17")})
    relabelled = _state(open_issues=[_issue(labels=["size/S"])])
    assert ledger.cooled_down(relabelled) == frozenset()

    for _ in range(2):
        ledger.record(PlayType.ISSUE_PICKUP, params, state, _failed(PlayType.ISSUE_PICKUP))
    planned = _state(open_issues=[_issue()], planned_issues=frozenset({17}))
    assert ledger.cooled_down(planned) == frozenset()


def test_candidate_plan_withholds_only_the_cooled_down_play() -> None:
    unblockable = _pr()
    clear = _state(pull_requests=[unblockable])
    assert [
        c.params.pr_number for c in build_candidate_plan(clear).candidates_for(PlayType.UNBLOCK_PR)
    ] == [42]

    cooled = _state(pull_requests=[unblockable], cooled_down_targets=frozenset({_UNBLOCK_42}))
    plan = build_candidate_plan(cooled)
    assert plan.candidates_for(PlayType.UNBLOCK_PR) == ()
    assert plan.blocked_reasons_by_play_type[PlayType.UNBLOCK_PR] == (
        "no-progress cooldown: pr:42",
    )
    assert [c.params.pr_number for c in plan.candidates_for(PlayType.CODE_REVIEW)] == [42]


def test_candidate_plan_withholds_a_cooled_down_issue_for_one_play_only() -> None:
    state = _state(
        open_issues=[_issue(17), _issue(18)],
        cooled_down_targets=frozenset({("write_implementation_plan", "issue:17")}),
    )
    plan = build_candidate_plan(state)
    assert [
        c.params.issue_number for c in plan.candidates_for(PlayType.WRITE_IMPLEMENTATION_PLAN)
    ] == [18]
    assert [c.params.issue_number for c in plan.candidates_for(PlayType.ISSUE_PICKUP)] == [17, 18]


async def test_resolver_store_path_honours_the_cooldown() -> None:
    """The store-backed discovery path bypasses the analyzer; the rule still applies."""
    from unittest.mock import AsyncMock, MagicMock

    from agentshore.config.models import RuntimeConfig
    from agentshore.plays.candidates import PlayCandidateService

    conflicting = _pr()
    store = MagicMock()
    store.list_open_pull_requests = AsyncMock(return_value=[conflicting])
    service = PlayCandidateService(store=store, cfg=RuntimeConfig(), github=None)

    before = await service.candidates_for(PlayType.UNBLOCK_PR, _state(pull_requests=[conflicting]))
    assert [c.params.pr_number for c in before] == [42]

    cooled = _state(pull_requests=[conflicting], cooled_down_targets=frozenset({_UNBLOCK_42}))
    assert await service.candidates_for(PlayType.UNBLOCK_PR, cooled) == []


def test_completion_hook_feeds_the_runtime_ledger() -> None:
    from types import SimpleNamespace

    from agentshore.core.mixins.completion import CompletionProcessor

    ledger = TargetAttemptLedger()
    state = _state(total_plays=5, pull_requests=[_pr()])
    stub = SimpleNamespace(_runtime=SimpleNamespace(target_attempts=ledger), _session_id="s")
    ctx = SimpleNamespace(params=PlayParams(pr_number=42), state_at_dispatch=state)

    for _ in range(2):
        CompletionProcessor._record_target_attempt(
            stub,  # type: ignore[arg-type]
            ctx,  # type: ignore[arg-type]
            _failed(PlayType.UNBLOCK_PR),
            PlayType.UNBLOCK_PR,
        )
    assert ledger.cooled_down(state) == frozenset({_UNBLOCK_42})
