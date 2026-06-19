"""IssuePickupPlay per-bead skip circuit-breaker.

When the live-graph bead-in-progress check rejects a dispatch, PPO has
historically taken many ticks to deprioritize the same issue, leading to
runs with 89+ consecutive $0 skips on the same bead. The circuit breaker
flips an env-state cooldown after `_SKIP_CIRCUIT_THRESHOLD` consecutive
skips so the policy's mask reflects the lock.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agentshore.beads import BeadStatus, GraphTask, ProjectGraph
from agentshore.plays.skill_backed.issue_pickup import (
    _SKIP_CIRCUIT_COOLDOWN_PLAYS,
    _SKIP_CIRCUIT_THRESHOLD,
    IssuePickupPlay,
)
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    IssueSnapshot,
    OrchestratorState,
    PlayOutcome,
    PlayType,
    SessionState,
)


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


def _make_issue(number: int) -> IssueSnapshot:
    return IssueSnapshot(
        issue_number=number,
        title=f"Issue #{number}",
        state="OPEN",
        priority=None,
        labels=[],
        source=None,
    )


def _make_state(
    issues: list[IssueSnapshot],
    *,
    total_plays: int = 0,
    graph: ProjectGraph | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=total_plays,
        total_cost=0.0,
        open_issues=issues,
        agents=[_make_agent()],
        graph=graph,
    )


def _ready_task(issue_number: int) -> GraphTask:
    """A bead that is OPEN with no live blockers → ``ready`` is True."""
    return GraphTask(
        bead_id=f"bd-{issue_number}",
        title=f"Task #{issue_number}",
        status=BeadStatus.OPEN,
        external_ref=f"gh-{issue_number}",
        issue_number=issue_number,
        ready=True,
        blocked_by_ids=frozenset(),
    )


def _blocked_task(issue_number: int) -> GraphTask:
    """A bead still blocked by an open dependency → ``ready`` is False."""
    return GraphTask(
        bead_id=f"bd-{issue_number}",
        title=f"Task #{issue_number}",
        status=BeadStatus.OPEN,
        external_ref=f"gh-{issue_number}",
        issue_number=issue_number,
        ready=False,
        blocked_by_ids=frozenset({"bd-dep"}),
    )


def test_under_threshold_skip_does_not_mask() -> None:
    """Below the consecutive-skip threshold the issue stays eligible."""
    play = IssuePickupPlay()
    issues = [_make_issue(101)]
    state = _make_state(issues, total_plays=0)

    for _ in range(_SKIP_CIRCUIT_THRESHOLD - 1):
        play._record_skip(101, total_plays=0)

    # Streak holds below threshold; not yet on cooldown; precondition still passes.
    assert play._skip_streaks.get(101) == _SKIP_CIRCUIT_THRESHOLD - 1
    assert 101 not in play._skip_until
    assert play.preconditions(state) == []


def test_threshold_skip_blacklists_issue() -> None:
    """Hitting the threshold moves the issue to cooldown and masks the play.

    With only the locked issue in open_issues, no other candidates remain →
    precondition emits the 'no eligible' reason.
    """
    play = IssuePickupPlay()
    issues = [_make_issue(101)]
    state = _make_state(issues, total_plays=5)

    for _ in range(_SKIP_CIRCUIT_THRESHOLD):
        play._record_skip(101, total_plays=5)

    assert play._skip_streaks.get(101) is None  # streak cleared on cooldown set
    assert play._skip_until[101] == 5 + _SKIP_CIRCUIT_COOLDOWN_PLAYS

    reasons = play.preconditions(state)
    assert any("no open issues eligible" in r.text for r in reasons)


def test_other_issues_still_eligible_during_cooldown() -> None:
    """A second open issue is still selectable while the first is on cooldown."""
    play = IssuePickupPlay()
    issues = [_make_issue(101), _make_issue(202)]
    state = _make_state(issues, total_plays=5)

    for _ in range(_SKIP_CIRCUIT_THRESHOLD):
        play._record_skip(101, total_plays=5)

    # Issue 202 is unaffected; precondition passes.
    assert play.preconditions(state) == []


def test_cooldown_expires_after_configured_plays() -> None:
    """Once total_plays advances past `_skip_until[N]`, the cooldown clears."""
    play = IssuePickupPlay()
    issues = [_make_issue(101)]

    for _ in range(_SKIP_CIRCUIT_THRESHOLD):
        play._record_skip(101, total_plays=5)

    # State at exactly the cooldown-clear boundary.
    state_at_expiry = _make_state(issues, total_plays=5 + _SKIP_CIRCUIT_COOLDOWN_PLAYS)
    reasons = play.preconditions(state_at_expiry)
    assert reasons == []
    assert 101 not in play._skip_until


def test_cooldown_rearms_immediately_when_blocker_clears() -> None:
    """An on-cooldown issue whose bead becomes ready is selectable that tick.

    The cooldown is a cost breaker, not a correctness gate. The moment the
    blocker clears (bead ready again), the issue must drop off cooldown
    rather than wait out ``_SKIP_CIRCUIT_COOLDOWN_PLAYS`` plays.
    """
    play = IssuePickupPlay()
    issues = [_make_issue(101)]

    # Trip the cooldown for #101.
    for _ in range(_SKIP_CIRCUIT_THRESHOLD):
        play._record_skip(101, total_plays=5)
    assert play._skip_until[101] == 5 + _SKIP_CIRCUIT_COOLDOWN_PLAYS

    # Still well inside the cooldown window, but the bead is now ready.
    graph = ProjectGraph(tasks=[_ready_task(101)], tasks_ready=1, tasks_total=1)
    state = _make_state(issues, total_plays=6, graph=graph)

    reasons = play.preconditions(state)

    # Re-armed: no mask, and both trackers cleared for #101.
    assert reasons == []
    assert 101 not in play._skip_until
    assert 101 not in play._skip_streaks


def test_cooldown_holds_when_blocker_persists() -> None:
    """A still-blocked bead does not re-arm — the cooldown keeps masking."""
    play = IssuePickupPlay()
    issues = [_make_issue(101)]

    for _ in range(_SKIP_CIRCUIT_THRESHOLD):
        play._record_skip(101, total_plays=5)

    # Bead still has a live blocker → ready is False, cooldown stands.
    graph = ProjectGraph(tasks=[_blocked_task(101)], tasks_ready=0, tasks_total=1)
    state = _make_state(issues, total_plays=6, graph=graph)

    reasons = play.preconditions(state)

    assert any("no open issues eligible" in r.text for r in reasons)
    assert play._skip_until[101] == 5 + _SKIP_CIRCUIT_COOLDOWN_PLAYS


def test_rearm_only_clears_ready_issue_not_others() -> None:
    """Re-arm is scoped per-issue: a ready bead clears only its own cooldown."""
    play = IssuePickupPlay()
    issues = [_make_issue(101), _make_issue(202)]

    for _ in range(_SKIP_CIRCUIT_THRESHOLD):
        play._record_skip(101, total_plays=5)
    for _ in range(_SKIP_CIRCUIT_THRESHOLD):
        play._record_skip(202, total_plays=5)

    # Only #101's bead is ready again.
    graph = ProjectGraph(
        tasks=[_ready_task(101), _blocked_task(202)],
        tasks_ready=1,
        tasks_total=2,
    )
    state = _make_state(issues, total_plays=6, graph=graph)

    play.preconditions(state)

    assert 101 not in play._skip_until
    assert play._skip_until[202] == 5 + _SKIP_CIRCUIT_COOLDOWN_PLAYS


def test_streak_resets_when_issue_closes() -> None:
    """If the issue is removed from open_issues, its tracking entry is purged."""
    play = IssuePickupPlay()
    play._record_skip(101, total_plays=0)
    play._record_skip(101, total_plays=0)
    assert play._skip_streaks.get(101) == 2

    # Issue 101 is no longer open (e.g. closed via PR merge).
    state = _make_state([_make_issue(202)], total_plays=0)
    play.preconditions(state)

    assert 101 not in play._skip_streaks
    assert 101 not in play._skip_until


def _failed_outcome(issue_number: int) -> PlayOutcome:
    return PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id="agent-1",
        success=False,
        partial=False,
        duration_seconds=0.0,
        token_cost=0,
        dollar_cost=0.1,
        artifacts=[],
        alignment_delta=0.0,
        error=f"blocked by open dependency: #{issue_number + 1}",
    )


def _success_outcome() -> PlayOutcome:
    return PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id="agent-1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=100,
        dollar_cost=0.1,
        artifacts=[],
        alignment_delta=0.0,
    )


@pytest.mark.asyncio
async def test_execution_failure_increments_skip_streak() -> None:
    """A failing ``execute()`` outcome counts toward the per-issue cooldown.

    Three consecutive failures on the same issue trip the same cooldown
    as three live-graph skips — the agent-reported "blocked by open
    dependency: #N" loop is the primary case this guards against.
    """
    from agentshore.plays.base import PlayParams

    play = IssuePickupPlay()
    params = PlayParams(issue_number=101)
    state = _make_state([_make_issue(101)], total_plays=5)
    ctx = object()  # never inspected; super().execute is patched out

    with patch(
        "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
        new=AsyncMock(return_value=_failed_outcome(101)),
    ):
        for _ in range(_SKIP_CIRCUIT_THRESHOLD):
            await play.execute(state, params, ctx=ctx)  # type: ignore[arg-type]

    assert 101 in play._skip_until
    assert play._skip_until[101] == 5 + _SKIP_CIRCUIT_COOLDOWN_PLAYS


@pytest.mark.asyncio
async def test_timeout_increments_skip_streak_as_non_rearmable() -> None:
    """#222: a dispatch timeout raises past the streak block and the executor
    converts it — so without the ``except`` it never counted. Now it does, as a
    NON-rearmable cooldown, and is re-raised so the executor still sees it."""
    from agentshore.errors import AgentTimeout
    from agentshore.plays.base import PlayParams

    play = IssuePickupPlay()
    params = PlayParams(issue_number=101)
    state = _make_state([_make_issue(101)], total_plays=5)
    ctx = object()

    with patch(
        "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
        new=AsyncMock(side_effect=AgentTimeout("agent timed out")),
    ):
        for _ in range(_SKIP_CIRCUIT_THRESHOLD):
            with pytest.raises(AgentTimeout):
                await play.execute(state, params, ctx=ctx)  # type: ignore[arg-type]

    assert play._skip_until[101] == 5 + _SKIP_CIRCUIT_COOLDOWN_PLAYS
    assert play._skip_rearmable[101] is False


@pytest.mark.asyncio
async def test_agent_crash_increments_skip_streak_as_non_rearmable() -> None:
    """#231: a -9-style agent crash feeds the same non-rearmable issue cooldown."""
    from agentshore.errors import AgentProcessCrashed
    from agentshore.plays.base import PlayParams

    play = IssuePickupPlay()
    params = PlayParams(issue_number=101)
    state = _make_state([_make_issue(101)], total_plays=5)
    ctx = object()

    with patch(
        "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
        new=AsyncMock(side_effect=AgentProcessCrashed("agent exited with code -9")),
    ):
        for _ in range(_SKIP_CIRCUIT_THRESHOLD):
            with pytest.raises(AgentProcessCrashed):
                await play.execute(state, params, ctx=ctx)  # type: ignore[arg-type]

    assert play._skip_until[101] == 5 + _SKIP_CIRCUIT_COOLDOWN_PLAYS
    assert play._skip_rearmable[101] is False


def test_timeout_cooldown_does_not_rearm_on_ready_bead() -> None:
    """#222: a timeout cooldown rides out its full window even when the bead is ready.

    Contrast ``test_cooldown_rearms_immediately_when_blocker_clears``: a
    dependency-block cooldown re-arms on readiness, but a timed-out issue's bead
    was never blocked, so readiness must NOT clear its cooldown — otherwise the
    timing-out issue is re-dispatched every tick with no backoff.
    """
    play = IssuePickupPlay()
    issues = [_make_issue(101)]

    for _ in range(_SKIP_CIRCUIT_THRESHOLD):
        play._record_skip(101, total_plays=5, rearmable=False)
    assert play._skip_until[101] == 5 + _SKIP_CIRCUIT_COOLDOWN_PLAYS

    # Bead is ready — a rearmable (dependency-block) cooldown would clear here,
    # but this timeout cooldown must hold.
    graph = ProjectGraph(tasks=[_ready_task(101)], tasks_ready=1, tasks_total=1)
    state = _make_state(issues, total_plays=6, graph=graph)

    reasons = play.preconditions(state)

    assert any("no open issues eligible" in r.text for r in reasons)
    assert play._skip_until[101] == 5 + _SKIP_CIRCUIT_COOLDOWN_PLAYS


@pytest.mark.asyncio
async def test_execution_success_clears_skip_streak_and_cooldown() -> None:
    """A successful pickup wipes any accumulated streak or cooldown."""
    from agentshore.plays.base import PlayParams

    play = IssuePickupPlay()
    # Seed prior failures.
    play._record_skip(101, total_plays=5)
    play._record_skip(101, total_plays=5)
    play._record_skip(101, total_plays=5)
    assert 101 in play._skip_until

    params = PlayParams(issue_number=101)
    state = _make_state([_make_issue(101)], total_plays=10)
    ctx = object()

    with patch(
        "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
        new=AsyncMock(return_value=_success_outcome()),
    ):
        await play.execute(state, params, ctx=ctx)  # type: ignore[arg-type]

    assert 101 not in play._skip_streaks
    assert 101 not in play._skip_until


@pytest.mark.asyncio
async def test_skipped_outcome_does_not_change_streak() -> None:
    """Skipped outcomes (e.g. internal short-circuits) carry no signal."""
    from agentshore.plays.base import PlayParams

    play = IssuePickupPlay()
    play._record_skip(101, total_plays=5)  # streak=1

    skipped = PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id=None,
        success=False,
        partial=True,
        duration_seconds=0.0,
        token_cost=0,
        dollar_cost=0.0,
        artifacts=[],
        alignment_delta=0.0,
        skipped=True,
    )
    params = PlayParams(issue_number=101)
    state = _make_state([_make_issue(101)], total_plays=5)
    ctx = object()

    with patch(
        "agentshore.plays.skill_backed.base.SkillBackedPlay.execute",
        new=AsyncMock(return_value=skipped),
    ):
        await play.execute(state, params, ctx=ctx)  # type: ignore[arg-type]

    # Streak unchanged at 1; cooldown not tripped.
    assert play._skip_streaks.get(101) == 1
    assert 101 not in play._skip_until
