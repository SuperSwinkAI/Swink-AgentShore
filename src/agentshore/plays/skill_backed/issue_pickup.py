"""IssuePickupPlay — pick up an open issue and implement it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from agentshore.errors import AgentProcessCrashed, AgentTimeout
from agentshore.github.labels import ISSUE_PICKUP_SKIP_LABELS
from agentshore.plays.candidates import MAX_OPEN_PRS
from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import DependenciesResolvedGate
from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.state import OrchestratorState, PlayOutcome

_logger = structlog.get_logger(__name__)

# Backpressure: when the open-PR queue reaches MAX_OPEN_PRS, mask issue_pickup so
# the policy clears review/merge work before opening more PRs. Past sessions have
# accumulated 20+ open PRs while every code_review either deduped or returned
# BLOCK; the queue grows without bound and the budget burns on context for PRs
# that never merge. The threshold lives in ``plays.candidates`` (single source of
# truth) so the END_SESSION human-jam escape hatch stays coupled to it.

# Per-issue circuit breaker. Failure classes that flip the same streak:
#   (1) The EligibilityAuthority's live ``confirm`` rejects the selected
#       issue when its bead dropped out of the live candidate set
#       (``EligibilityAuthority._live_target_reason``), which the selector
#       turns into a clean re-pick. Historical race-condition guard.
#   (2) ``execute()`` runs the agent and the returned outcome has
#       ``success=False`` — typically a body-declared dependency like
#       "blocked by open dependency: #N". Without this, PPO keeps
#       re-dispatching the same dep-blocked issue every couple of plays
#       and burns ~$0.10–0.20 per cycle.
#   (3) The dispatch times out or the agent crashes (``AgentTimeout`` /
#       ``AgentProcessCrashed``). These raise straight past the streak
#       accounting in ``execute()`` (the executor catches them), so #222
#       they were invisible to the circuit and a repeatedly-timing-out
#       issue was re-dispatched every tick with no backoff.
# After ``_SKIP_CIRCUIT_THRESHOLD`` consecutive failures the issue goes on
# cooldown for ``_SKIP_CIRCUIT_COOLDOWN_PLAYS`` plays; a successful pickup
# or the issue closing clears the streak. This is a *cost* breaker, not a
# correctness gate. A **dependency-block** cooldown re-arms the moment the
# blocker clears (its bead becomes ready — see ``_rearm_ready_issues``),
# never held for the full window. A **timeout/crash** cooldown is marked
# non-rearmable: bead-readiness is irrelevant to a timeout (the bead was
# never blocked), so re-arming on readiness would defeat the cooldown
# entirely (#222) — those ride out the full window instead.
_SKIP_CIRCUIT_THRESHOLD = 3
_SKIP_CIRCUIT_COOLDOWN_PLAYS = 20


class IssuePickupPlay(SkillBackedPlay):
    """Pick up the highest-priority open issue and implement it.

    Preconditions:
    - At least one open issue exists.
    - At least one IDLE agent with can_implement capability is available.
    - Fewer than ``MAX_OPEN_PRS`` open PRs in the queue (backpressure gate, inclusive).
    - The linked beads task (if any) is not already ``in_progress`` (double-pickup guard, C3).
    - At least one open issue is not currently on the per-bead skip cooldown
      (env-state circuit breaker; see ``_SKIP_CIRCUIT_THRESHOLD``). An issue
      whose blocker clears (its bead becomes ready) re-arms immediately and
      is no longer treated as on cooldown (see ``_rearm_ready_issues``).
    """

    # Only issue_pickup legitimately authors PRs; the executor stamps PR
    # authorship from its ``pr`` / ``pull_request`` artifacts.
    authors_prs = True

    def __init__(self) -> None:
        super().__init__()
        # issue_number → consecutive policy-skip count since last successful dispatch
        self._skip_streaks: dict[int, int] = {}
        # issue_number → state.total_plays count at which the issue becomes
        # eligible again. Set once `_skip_streaks` crosses the threshold.
        self._skip_until: dict[int, int] = {}
        # issue_number → whether the cooldown re-arms the moment the bead is
        # ready again. True for dependency-block failures (bead readiness is the
        # relevant signal); False for timeout / agent-crash failures, where
        # readiness is irrelevant and re-arming would defeat the cooldown (#222).
        self._skip_rearmable: dict[int, bool] = {}

    @property
    def play_type(self) -> PlayType:
        return PlayType.ISSUE_PICKUP

    @property
    def skill_name(self) -> str:
        return "agentshore-issue-pickup"

    @property
    def capability(self) -> str | None:
        return "can_implement"

    def _purge_closed_issues(self, open_numbers: set[int]) -> None:
        """Drop skip-tracking entries for issues that are no longer open."""
        for tracker in (self._skip_streaks, self._skip_until, self._skip_rearmable):
            for issue_number in list(tracker):
                if issue_number not in open_numbers:
                    del tracker[issue_number]

    def _rearm_ready_issues(self, state: OrchestratorState) -> None:
        """Clear the cooldown for any on-cooldown issue whose bead is now ready.

        The skip-circuit is a *cost* breaker, not a correctness gate: it
        exists to stop the policy re-dispatching a dep-blocked issue every
        couple of plays. The moment the blocker clears it must re-arm, or it
        would hold the PPO off genuinely-ready work for up to
        ``_SKIP_CIRCUIT_COOLDOWN_PLAYS`` plays.

        We reuse the per-tick beads snapshot already on ``state.graph`` —
        each ``GraphTask`` carries ``ready`` (``status == OPEN and not
        blocked_by``) and its GH ``issue_number`` — so this needs no extra
        async beads call. An issue that is on cooldown but whose bead is now
        ready is dropped from ``_skip_until`` and has its streak reset.
        """
        graph = state.graph
        if graph is None or not self._skip_until:
            return
        ready_issue_numbers = {
            task.issue_number
            for task in graph.tasks
            if task.ready and task.issue_number is not None
        }
        for issue_number in ready_issue_numbers & set(self._skip_until):
            if not self._skip_rearmable.get(issue_number, True):
                # Timeout/crash cooldown: bead-readiness never blocked it, so a
                # ready bead must not clear it — let it ride out the window (#222).
                continue
            del self._skip_until[issue_number]
            self._skip_streaks.pop(issue_number, None)
            self._skip_rearmable.pop(issue_number, None)

    def _issues_on_cooldown(self, total_plays: int) -> set[int]:
        """Return the set of issue_numbers still inside their skip cooldown."""
        expired = [n for n, until in self._skip_until.items() if total_plays >= until]
        for n in expired:
            del self._skip_until[n]
            self._skip_rearmable.pop(n, None)
        return set(self._skip_until.keys())

    def _record_skip(self, issue_number: int, total_plays: int, *, rearmable: bool = True) -> None:
        """Increment the per-issue skip streak, escalating to a cooldown at the threshold.

        Called from :meth:`execute` for every non-skipped failure — a
        cleanly-returned ``success=False`` outcome (``rearmable=True``,
        typically a body-declared dependency block) or a ``AgentTimeout`` /
        ``AgentProcessCrashed`` raised past the accounting block
        (``rearmable=False`` — bead-readiness is irrelevant to a timeout, so the
        cooldown must not re-arm on it, #222). Resets the streak counter once
        the cooldown fires and records the cooldown's rearmability.
        """
        streak = self._skip_streaks.get(issue_number, 0) + 1
        self._skip_streaks[issue_number] = streak
        if streak >= _SKIP_CIRCUIT_THRESHOLD:
            self._skip_until[issue_number] = total_plays + _SKIP_CIRCUIT_COOLDOWN_PLAYS
            self._skip_rearmable[issue_number] = rearmable
            del self._skip_streaks[issue_number]

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        issues: list[MaskReason] = []
        open_numbers = {issue.issue_number for issue in state.open_issues}
        self._purge_closed_issues(open_numbers)
        # Re-arm before reading the cooldown set: a cooled-down issue whose
        # blocker has cleared (its bead is ready again) must be selectable
        # this same tick, never held for the rest of the cooldown window.
        self._rearm_ready_issues(state)
        on_cooldown = self._issues_on_cooldown(state.total_plays)
        if not state.open_issues:
            issues.append(
                MaskReason(
                    text="no open issues available for pickup",
                    classification=MaskClassification.HARD,
                    source=MaskSource.CANDIDATE,
                )
            )
        elif not [
            issue
            for issue in state.open_issues
            if issue.state.upper() == "OPEN"
            and issue.issue_number not in on_cooldown
            and not (ISSUE_PICKUP_SKIP_LABELS & set(issue.labels))
        ]:
            issues.append(
                MaskReason(
                    text="no open issues eligible for pickup after AgentShore issue-label gates",
                    classification=MaskClassification.HARD,
                    source=MaskSource.CANDIDATE,
                )
            )
        dep_reason = DependenciesResolvedGate()(state)
        if dep_reason is not None:
            issues.append(dep_reason)
        issues += self._capability_check(state)
        open_prs = {pr.pr_number for pr in state.pull_requests if pr.state == "open"}
        open_pr_count = len(open_prs)
        if open_pr_count >= MAX_OPEN_PRS:
            issues.append(
                MaskReason(
                    text=(
                        f"too many open PRs ({open_pr_count} >= {MAX_OPEN_PRS}); "
                        "drain review/merge queue first"
                    ),
                    classification=MaskClassification.HARD,
                    source=MaskSource.PRECONDITION,
                )
            )
        if state.graph is not None and state.graph.has_epics and not state.graph.has_ready_tasks:
            # M8: Be explicit that the policy does not auto-promote groom_backlog.
            issues.append(
                MaskReason(
                    text=(
                        "beads graph has epics but no ready tasks — "
                        "groom_backlog must be manually eligible; "
                        "the policy does not auto-promote it"
                    ),
                    classification=MaskClassification.HARD,
                    source=MaskSource.PRECONDITION,
                )
            )
        return issues

    async def execute(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
    ) -> PlayOutcome:
        """Dispatch the agent and update the per-issue skip-circuit streak.

        Historical note (desktop-xi9d): this method used to do a final
        live-beads-graph check before dispatch and short-circuit with a
        partial-failure outcome whenever the bead was no longer OPEN. The
        check has moved into the ``EligibilityAuthority``'s one live
        ``confirm`` (``_live_target_reason``), which drops the selected
        issue from the live candidate set and triggers a clean re-pick in
        the selector instead. DB-backed work claims remain the actual
        dispatch lock; beads is an external progress mirror.

        Streak accounting: a successful outcome clears the per-issue
        streak and any cooldown; a failed outcome increments the streak
        via :meth:`_record_skip`, which trips the cooldown once it hits
        ``_SKIP_CIRCUIT_THRESHOLD``. Skipped outcomes are ignored — they
        carry no signal about the issue's workability. A timeout / crash
        raises out of ``super().execute()`` (the executor converts it to a
        failed outcome), so it is counted here too via the ``except`` — a
        **non-rearmable** skip, since a timed-out issue's bead stays ready
        and a rearmable cooldown would be cleared the next tick (#222).
        """
        try:
            outcome = await super().execute(state, params, ctx=ctx)
        except (AgentTimeout, AgentProcessCrashed):
            if params.issue_number is not None:
                self._record_skip(params.issue_number, state.total_plays, rearmable=False)
            raise
        if params.issue_number is not None and not outcome.skipped:
            if outcome.success:
                self._skip_streaks.pop(params.issue_number, None)
                self._skip_until.pop(params.issue_number, None)
                self._skip_rearmable.pop(params.issue_number, None)
            else:
                self._record_skip(params.issue_number, state.total_plays, rearmable=True)
        return outcome
