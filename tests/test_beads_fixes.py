"""Tests for beads integration fixes: C5, C2, C3, H4.

Coverage:
1. (C5)  _BD_LOCK serialises concurrent bd subprocess calls.
2. (C2)  merge_pr post-success calls ``bd update --status closed`` for each issue.
3. (C3)  issue_pickup pre-dispatch marks the beads task ``in_progress``.
4. (C3)  A duplicate pickup is excluded from issue-pickup candidates.
5. (H4)  _validated_issue_set rejects a hallucinated issue number.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.beads import BeadStatus, EpicStatus, GraphTask, ProjectGraph
from agentshore.plays.base import PlayParams
from agentshore.plays.candidates import build_candidate_plan
from agentshore.plays.skill_backed.issue_pickup import IssuePickupPlay
from agentshore.plays.skill_backed.merge_pr import MergePRPlay, _validated_issue_set
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    IssueSnapshot,
    PlayOutcome,
    PlayType,
    SkillResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(*, project_path: Path | None = None) -> Any:
    ctx = MagicMock()
    ctx.session_id = "test-session"
    ctx.play_id = 1
    ctx.project_path = project_path or Path("/fake/repo")
    ctx.store = AsyncMock()
    ctx.store.mark_pr_merged = AsyncMock()
    ctx.store.complete_reviews_for_pr = AsyncMock()
    ctx.store.update_issue_state = AsyncMock()
    ctx.cfg = MagicMock()
    # Real empty identities so the post-merge ff-sync's fetch-overlay resolver
    # (resolve_ff_fetch_overlay → select_default_git_identity) returns None
    # cleanly instead of choking on a MagicMock's empty-iterable .items() (#178).
    ctx.cfg.identities = {}
    ctx.manager = MagicMock()
    return ctx


def _make_agent() -> AgentSnapshot:
    return AgentSnapshot(
        agent_id="agent-1",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )


def _make_issue(number: int = 1) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=number,
        title=f"Issue #{number}",
        state="OPEN",
        priority=None,
        labels=[],
        source=None,
    )


def _make_graph_task(
    *,
    bead_id: str = "bd-001",
    issue_number: int = 1,
    status: BeadStatus = BeadStatus.OPEN,
) -> GraphTask:
    return GraphTask(
        bead_id=bead_id,
        title="Some task",
        status=status,
        issue_number=issue_number,
        ready=(status == BeadStatus.OPEN),
    )


def _make_state(
    *, graph: ProjectGraph | None = None, issue_numbers: list[int] | None = None
) -> Any:
    state = MagicMock()
    state.graph = graph
    issue_numbers = issue_numbers or [1]
    state.open_issues = [_make_issue(n) for n in issue_numbers]
    state.agents = [_make_agent()]
    state.pull_requests = []
    # Issue-author trust gating is off here (a truthy MagicMock default would
    # otherwise spuriously exclude every author-less issue).
    state.restrict_issues_to_trusted_authors = False
    state.trusted_issue_authors = frozenset()
    return state


def _success_outcome(pr_number: int = 42) -> PlayOutcome:
    return PlayOutcome(
        play_type=PlayType.MERGE_PR,
        agent_id="agent-1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=100,
        dollar_cost=0.05,
        artifacts=[{"type": "merge", "pr": pr_number, "merge_method": "squash"}],
        alignment_delta=0.1,
    )


# ---------------------------------------------------------------------------
# C5 — _BD_LOCK serialises concurrent bd calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bd_lock_serialises_concurrent_calls() -> None:
    """Two concurrent bd coroutines must not overlap — the lock ensures sequential execution.

    Fully hermetic: both binary resolution and the subprocess spawn are mocked, so
    the test exercises only ``_BD_LOCK`` ordering and needs no real bd on PATH.
    """
    timeline: list[str] = []

    async def _fake_bd(*args: str, cwd: object, stdin_data: object = None) -> str:
        # This inner coroutine holds the lock while "running" — a second
        # concurrent caller must wait.  We use a short sleep to create
        # observable overlap if the lock were absent.
        timeline.append("start")
        await asyncio.sleep(0.02)
        timeline.append("end")
        return ""

    # Patch the subprocess path inside bd() to our fake that captures ordering.
    # resolve_bd_binary is pinned so the test does not depend on bd being installed.
    with (
        patch("agentshore.beads.resolve_bd_binary", return_value="bd"),
        patch("agentshore.beads.asyncio.create_subprocess_exec") as mock_exec,
    ):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        async def _slow_exec(*args: object, **kwargs: object) -> AsyncMock:
            timeline.append("start")
            await asyncio.sleep(0.02)
            timeline.append("end")
            return mock_proc

        mock_exec.side_effect = _slow_exec

        from agentshore.beads import bd

        # Fire two bd calls concurrently.
        await asyncio.gather(
            bd("query", "type=task", cwd=Path("/tmp")),
            bd("query", "type=task", cwd=Path("/tmp")),
        )

    # If the lock is working, the sequence must be start→end→start→end (no interleaving).
    assert timeline == ["start", "end", "start", "end"], (
        f"Concurrent bd calls interleaved — lock not working: {timeline}"
    )


# ---------------------------------------------------------------------------
# C2 — merge_pr calls bd update --status closed for each beads task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_pr_closes_beads_tasks() -> None:
    """Successful merge → bd update --status closed called for each linked task."""
    play = MergePRPlay()
    ctx = _make_ctx()

    task17 = _make_graph_task(bead_id="bd-017", issue_number=17, status=BeadStatus.OPEN)
    task23 = _make_graph_task(bead_id="bd-023", issue_number=23, status=BeadStatus.IN_PROGRESS)
    graph = ProjectGraph(tasks=[task17, task23])
    state = _make_state(graph=graph, issue_numbers=[17, 23])

    bd_calls: list[tuple[str, ...]] = []

    async def _fake_bd(*args: str, cwd: object, stdin_data: object = None) -> str:
        bd_calls.append(args)
        return ""

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=True, issues_closed=[17, 23])
        return _success_outcome()

    # PR body confirms both issues.
    async def _fake_fetch_body(pr_number: int, project_path: object) -> str:
        return "Closes #17\nFixes #23"

    with (
        patch("agentshore.plays.skill_backed.base.SkillBackedPlay.execute", new=_super_execute),
        patch("agentshore.plays.skill_backed.merge_pr.bd", new=_fake_bd),
        patch("agentshore.plays.skill_backed.merge_pr._fetch_pr_body", new=_fake_fetch_body),
    ):
        outcome = await play.execute(state, PlayParams(agent_id="agent-1", pr_number=42), ctx=ctx)

    assert outcome.success
    # Both beads tasks should have been closed.
    closed_bead_ids = {call[1] for call in bd_calls if "closed" in call}
    assert "bd-017" in closed_bead_ids, f"bd-017 not closed; bd_calls={bd_calls}"
    assert "bd-023" in closed_bead_ids, f"bd-023 not closed; bd_calls={bd_calls}"
    # bd calls use --status closed (not bd set-state).
    for call in bd_calls:
        assert call[0] == "update", f"expected 'update' subcommand, got: {call}"
        assert "--status" in call and "closed" in call


@pytest.mark.asyncio
async def test_merge_pr_skips_already_closed_bead() -> None:
    """A task already CLOSED is not passed to bd update."""
    play = MergePRPlay()
    ctx = _make_ctx()

    task = _make_graph_task(bead_id="bd-007", issue_number=7, status=BeadStatus.CLOSED)
    graph = ProjectGraph(tasks=[task])
    state = _make_state(graph=graph, issue_numbers=[7])

    bd_calls: list[tuple[str, ...]] = []

    async def _fake_bd(*args: str, cwd: object, stdin_data: object = None) -> str:
        bd_calls.append(args)
        return ""

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=True, issues_closed=[7])
        return _success_outcome(pr_number=99)

    async def _fake_fetch_body(pr_number: int, project_path: object) -> str:
        return "Closes #7"

    with (
        patch("agentshore.plays.skill_backed.base.SkillBackedPlay.execute", new=_super_execute),
        patch("agentshore.plays.skill_backed.merge_pr.bd", new=_fake_bd),
        patch("agentshore.plays.skill_backed.merge_pr._fetch_pr_body", new=_fake_fetch_body),
    ):
        await play.execute(state, PlayParams(agent_id="agent-1", pr_number=99), ctx=ctx)

    assert bd_calls == [], f"Expected no bd calls for already-closed task, got: {bd_calls}"


@pytest.mark.asyncio
async def test_merge_pr_bead_close_failure_does_not_abort() -> None:
    """If bd update fails, the merge outcome is still returned successfully."""
    play = MergePRPlay()
    ctx = _make_ctx()

    task = _make_graph_task(bead_id="bd-001", issue_number=1, status=BeadStatus.OPEN)
    graph = ProjectGraph(tasks=[task])
    state = _make_state(graph=graph, issue_numbers=[1])

    async def _failing_bd(*args: str, cwd: object, stdin_data: object = None) -> str:
        from agentshore.beads import BdError

        raise BdError("bd not available")

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=True, issues_closed=[1])
        return _success_outcome(pr_number=5)

    async def _fake_fetch_body(pr_number: int, project_path: object) -> str:
        return "Closes #1"

    with (
        patch("agentshore.plays.skill_backed.base.SkillBackedPlay.execute", new=_super_execute),
        patch("agentshore.plays.skill_backed.merge_pr.bd", new=_failing_bd),
        patch("agentshore.plays.skill_backed.merge_pr._fetch_pr_body", new=_fake_fetch_body),
    ):
        outcome = await play.execute(state, PlayParams(agent_id="agent-1", pr_number=5), ctx=ctx)

    # The play must still succeed despite the bd error.
    assert outcome.success


# ---------------------------------------------------------------------------
# C3 — issue_pickup leaves beads status to the dispatched skill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_pickup_does_not_mark_bead_in_progress_before_dispatch() -> None:
    """execute() dispatches without first making its own beads lock look external."""
    play = IssuePickupPlay()
    ctx = _make_ctx()

    task = _make_graph_task(bead_id="bd-010", issue_number=5, status=BeadStatus.OPEN)
    graph = ProjectGraph(tasks=[task])
    state = _make_state(graph=graph, issue_numbers=[5])

    issue_pickup_outcome = PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id="agent-1",
        success=True,
        partial=False,
        duration_seconds=2.0,
        token_cost=200,
        dollar_cost=0.10,
        artifacts=[],
        alignment_delta=0.0,
    )

    async def _super_execute(*args: object, **kwargs: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=True)
        return issue_pickup_outcome

    with patch("agentshore.plays.skill_backed.base.SkillBackedPlay.execute", new=_super_execute):
        outcome = await play.execute(
            state,
            PlayParams(agent_id="agent-1", issue_number=5),
            ctx=ctx,
        )

    assert outcome.success


# ---------------------------------------------------------------------------
# C3 — duplicate pickup excluded by candidate selection
# ---------------------------------------------------------------------------


def test_issue_pickup_excludes_duplicate_pickup_candidate() -> None:
    """If a linked beads task is already in_progress, only that issue is excluded."""
    play = IssuePickupPlay()

    task = _make_graph_task(bead_id="bd-010", issue_number=3, status=BeadStatus.IN_PROGRESS)
    graph = ProjectGraph(tasks=[task])
    state = _make_state(graph=graph, issue_numbers=[3, 4])

    assert play.preconditions(state) == []
    plan = build_candidate_plan(state)
    assert [c.params.issue_number for c in plan.candidates_for(PlayType.ISSUE_PICKUP)] == [4]
    assert plan.work_availability.bead_in_progress_issue_count == 1


def test_issue_pickup_does_not_block_when_task_open() -> None:
    """An OPEN beads task does not trigger the duplicate-pickup guard."""
    play = IssuePickupPlay()

    task = _make_graph_task(bead_id="bd-010", issue_number=3, status=BeadStatus.OPEN)
    graph = ProjectGraph(tasks=[task])
    state = _make_state(graph=graph, issue_numbers=[3])

    failures = play.preconditions(state)
    assert not any("already in_progress" in f.text for f in failures), (
        f"Open task should not trigger duplicate guard; got: {failures}"
    )


@pytest.mark.asyncio
async def test_issue_pickup_execute_no_longer_blocks_on_live_graph() -> None:
    """desktop-xi9d regression: the live-graph check moved to param-resolve time.

    Prior to desktop-xi9d, ``IssuePickupPlay.execute`` re-checked the live
    beads graph and returned a partial-failure outcome whenever the bead
    was no longer OPEN. That burned a full PPO action on every race.
    The check now lives in
    ``_DispatchMixin._dispatch_revalidation_reason`` (verified by
    ``tests/test_issue_pickup_live_graph_race.py``), so ``execute`` is
    expected to dispatch unconditionally — the param-resolve-time hook
    is the one that emits ``dispatch_revalidation_blocked``.
    """
    play = IssuePickupPlay()
    ctx = _make_ctx()
    task = _make_graph_task(bead_id="bd-010", issue_number=5, status=BeadStatus.OPEN)
    graph = ProjectGraph(tasks=[task])
    state = _make_state(graph=graph, issue_numbers=[5])

    super_execute_called = False

    async def _super_execute(*args: object, **kwargs: object) -> Any:
        nonlocal super_execute_called
        super_execute_called = True
        return PlayOutcome(
            play_type=PlayType.ISSUE_PICKUP,
            agent_id="agent-1",
            success=True,
            partial=False,
            duration_seconds=2.0,
            token_cost=200,
            dollar_cost=0.10,
            artifacts=[],
            alignment_delta=0.0,
        )

    with patch("agentshore.plays.skill_backed.base.SkillBackedPlay.execute", new=_super_execute):
        outcome = await play.execute(
            state,
            PlayParams(agent_id="agent-1", issue_number=5),
            ctx=ctx,
        )

    # Execute is now a thin wrapper over super().execute — no live-graph
    # check, no early-return, no action consumed inside the race window.
    assert outcome.success, "execute() should not short-circuit on its own"
    assert super_execute_called, "Agent dispatch must proceed; revalidation lives at param-resolve"


# ---------------------------------------------------------------------------
# H4 — _validated_issue_set rejects hallucinated issues
# ---------------------------------------------------------------------------


def test_validated_issue_set_rejects_hallucinated() -> None:
    """An issue in skill_issues but not in PR body is dropped with a warning."""
    # Issue 99 is hallucinated — not in the PR body.
    result = _validated_issue_set(
        skill_issues=[17, 99],
        pr_body="Closes #17",
        pr_number=42,
    )
    assert 17 in result
    assert 99 not in result, "Hallucinated issue 99 must be excluded"


def test_validated_issue_set_adds_missed_issues() -> None:
    """An issue in the PR body but missing from skill_issues is added."""
    result = _validated_issue_set(
        skill_issues=[17],
        pr_body="Closes #17\nFixes #23",
        pr_number=42,
    )
    assert 17 in result
    assert 23 in result, "Missed issue 23 from PR body must be included"


def test_validated_issue_set_none_body_passthrough() -> None:
    """When pr_body is None (fetch failed), skill_issues are returned unchanged."""
    result = _validated_issue_set(
        skill_issues=[5, 6, 7],
        pr_body=None,
        pr_number=99,
    )
    assert sorted(result) == [5, 6, 7]


def test_validated_issue_set_empty_body() -> None:
    """An empty PR body causes all skill_issues to be treated as hallucinated."""
    result = _validated_issue_set(
        skill_issues=[1, 2],
        pr_body="No issue references here.",
        pr_number=10,
    )
    assert result == [], f"No body refs → all skill issues are hallucinated; got: {result}"


def test_validated_issue_set_both_empty() -> None:
    """Empty skill_issues and empty body → empty result."""
    result = _validated_issue_set(
        skill_issues=[],
        pr_body="No issue references here.",
        pr_number=10,
    )
    assert result == []


def test_validated_issue_set_comma_separated_two() -> None:
    """'Closes #123, #456' on one line must include both issue numbers."""
    result = _validated_issue_set(
        skill_issues=[123, 456],
        pr_body="Closes #123, #456",
        pr_number=42,
    )
    assert 123 in result, "Issue 123 must be included"
    assert 456 in result, "Issue 456 must be included from comma-separated list"


def test_validated_issue_set_comma_separated_three_with_and() -> None:
    """'Fixes #1, #2, and #3' on one line must include all three issue numbers."""
    result = _validated_issue_set(
        skill_issues=[1, 2, 3],
        pr_body="Fixes #1, #2, and #3",
        pr_number=99,
    )
    assert 1 in result
    assert 2 in result
    assert 3 in result, "Issue 3 must be included from 'and #3' suffix"


def test_validated_issue_set_comma_separated_missed_in_skill() -> None:
    """If skill only reports the first issue in 'Closes #10, #11', the second is added."""
    result = _validated_issue_set(
        skill_issues=[10],
        pr_body="Closes #10, #11",
        pr_number=5,
    )
    assert 10 in result
    assert 11 in result, "Missed issue 11 from comma-separated list must be added"


# ---------------------------------------------------------------------------
# M8 — precondition message clarity
# ---------------------------------------------------------------------------


def test_issue_pickup_groom_backlog_message_mentions_policy() -> None:
    """The groom_backlog precondition message explicitly mentions policy non-promotion."""
    graph = ProjectGraph(
        epics=[
            EpicStatus(
                bead_id="e1", title="Epic 1", total_tasks=3, closed_tasks=0, closure_ratio=0.0
            )
        ],
        tasks_ready=0,
        tasks_total=3,
        global_closure_ratio=0.0,
    )
    state = _make_state(graph=graph)
    play = IssuePickupPlay()
    failures = play.preconditions(state)
    groom_msgs = [f for f in failures if "groom_backlog" in f.text]
    assert groom_msgs, "Expected a groom_backlog message"
    msg = groom_msgs[0]
    assert "policy does not auto-promote" in msg.text, (
        f"Message should clarify policy non-promotion; got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Duplicate-bead routing — pick_bead_for_issue helper
# ---------------------------------------------------------------------------


def test_pick_bead_for_issue_single_open_bead() -> None:
    """A single OPEN bead is returned as-is."""
    from agentshore.beads import pick_bead_for_issue

    task = _make_graph_task(bead_id="bd-100", issue_number=42, status=BeadStatus.OPEN)
    assert pick_bead_for_issue([task], 42) is task


def test_pick_bead_for_issue_prefers_open_over_closed_duplicate() -> None:
    """When duplicates exist, the OPEN bead wins regardless of insertion order."""
    from agentshore.beads import pick_bead_for_issue

    closed = _make_graph_task(bead_id="dup-closed", issue_number=265, status=BeadStatus.CLOSED)
    live = _make_graph_task(bead_id="live-open", issue_number=265, status=BeadStatus.OPEN)

    # CLOSED-first ordering reproduces the gh-265 production bug; the helper
    # must still pick the OPEN bead.
    assert pick_bead_for_issue([closed, live], 265) is live
    # And the reverse ordering still picks OPEN — stable result.
    assert pick_bead_for_issue([live, closed], 265) is live


def test_pick_bead_for_issue_returns_none_when_no_match() -> None:
    """Caller (state hydration / dispatch) gets None when no bead pairs with the issue."""
    from agentshore.beads import pick_bead_for_issue

    other = _make_graph_task(bead_id="bd-200", issue_number=99, status=BeadStatus.OPEN)
    assert pick_bead_for_issue([other], 42) is None
    assert pick_bead_for_issue([], 42) is None


def test_pick_bead_for_issue_falls_back_to_least_bad_status() -> None:
    """If every match is non-OPEN, the helper returns the most-actionable one (CLOSED last)."""
    from agentshore.beads import pick_bead_for_issue

    closed = _make_graph_task(bead_id="bd-c", issue_number=7, status=BeadStatus.CLOSED)
    in_progress = _make_graph_task(bead_id="bd-ip", issue_number=7, status=BeadStatus.IN_PROGRESS)
    blocked = _make_graph_task(bead_id="bd-b", issue_number=7, status=BeadStatus.BLOCKED)

    # IN_PROGRESS beats BLOCKED beats CLOSED.
    assert pick_bead_for_issue([closed, blocked, in_progress], 7) is in_progress
    assert pick_bead_for_issue([closed, blocked], 7) is blocked


# ---------------------------------------------------------------------------
# Duplicate-bead routing — issue_pickup.execute regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_pickup_execute_clears_skip_streak_on_dispatch() -> None:
    """desktop-xi9d: execute() clears the per-issue skip streak on dispatch.

    Previously the streak counter was also bumped/cleared by the
    in-execute live-graph check; that check moved to param-resolve time.
    What remains is the streak-clear-on-real-dispatch invariant —
    important because the dispatch-mixin's revalidation hook calls
    ``_record_skip`` to drive the cooldown, and we want the counter to
    reset cleanly once a real (non-skip) dispatch reaches execute().
    """
    play = IssuePickupPlay()
    ctx = _make_ctx()
    state = _make_state(graph=ProjectGraph(), issue_numbers=[265])

    # Pretend the dispatch-mixin recorded two skips before this attempt.
    play._record_skip(265, total_plays=10)
    play._record_skip(265, total_plays=10)
    assert play._skip_streaks.get(265) == 2

    async def _super_execute(*args: object, **kwargs: object) -> Any:
        return PlayOutcome(
            play_type=PlayType.ISSUE_PICKUP,
            agent_id="agent-1",
            success=True,
            partial=False,
            duration_seconds=1.0,
            token_cost=100,
            dollar_cost=0.05,
            artifacts=[],
            alignment_delta=0.0,
        )

    with patch("agentshore.plays.skill_backed.base.SkillBackedPlay.execute", new=_super_execute):
        outcome = await play.execute(
            state,
            PlayParams(agent_id="agent-1", issue_number=265),
            ctx=ctx,
        )

    assert outcome.success
    # Streak must be cleared on a real dispatch so the cooldown counter
    # doesn't carry stale skips across successful runs.
    assert 265 not in play._skip_streaks


# ---------------------------------------------------------------------------
# Duplicate-bead routing — _project_open_issues state hydration
# ---------------------------------------------------------------------------


def test_project_open_issues_prefers_open_bead_for_duplicate() -> None:
    """state.open_issues must reflect the OPEN duplicate, not whichever bead sorts last."""
    from agentshore.core.mixins.snapshots import SnapshotProjector
    from agentshore.data.models import GitHubIssueRecord

    closed_dup = _make_graph_task(
        bead_id="desktop-tfq.1.3", issue_number=265, status=BeadStatus.CLOSED
    )
    live_open = _make_graph_task(
        bead_id="desktop-tfq.1.2", issue_number=265, status=BeadStatus.OPEN
    )
    graph = ProjectGraph(tasks=[closed_dup, live_open])
    record = GitHubIssueRecord(
        issue_number=265,
        session_id="test",
        title="Issue #265",
        state="open",
        created_at="2026-05-16T22:00:00Z",
        priority=None,
        labels=[],
        source="github",
        url=None,
        closed_at=None,
    )

    snapshots = SnapshotProjector.project_open_issues([record], graph)

    assert len(snapshots) == 1
    snap = snapshots[0]
    # Open dup wins regardless of dict-insertion order in the input list.
    assert snap.bead_id == "desktop-tfq.1.2"
    assert snap.bead_status == "open"
