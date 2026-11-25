"""IssuePickupPlay — pick up an open issue and implement it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from agentshore.cooldown import Clock, Cooldown, CooldownSpec
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

    authors_prs = True

    def __init__(self) -> None:
        super().__init__()
        self._skip: Cooldown[int] = Cooldown(
            CooldownSpec(
                threshold=_SKIP_CIRCUIT_THRESHOLD,
                cooldown=_SKIP_CIRCUIT_COOLDOWN_PLAYS,
                clock=Clock.PLAYS,
            )
        )
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
        for issue_number in self._skip.tracked_keys():
            if issue_number not in open_numbers:
                self._skip.clear(issue_number)
        for issue_number in list(self._skip_rearmable):
            if issue_number not in open_numbers:
                del self._skip_rearmable[issue_number]

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
        ready is dropped from the cooldown (streak + armed window cleared).
        """
        graph = state.graph
        on_cooldown = self._skip.armed_keys(now=state.total_plays)
        if graph is None or not on_cooldown:
            return
        ready_issue_numbers = {
            task.issue_number
            for task in graph.tasks
            if task.ready and task.issue_number is not None
        }
        for issue_number in ready_issue_numbers & on_cooldown:
            if not self._skip_rearmable.get(issue_number, True):
                continue
            self._skip.clear(issue_number)
            self._skip_rearmable.pop(issue_number, None)

    def _issues_on_cooldown(self, total_plays: int) -> set[int]:
        """Return the set of issue_numbers still inside their skip cooldown."""
        on_cooldown = self._skip.armed_keys(now=total_plays)
        for issue_number in set(self._skip_rearmable) - on_cooldown:
            del self._skip_rearmable[issue_number]
        return on_cooldown

    def _record_skip(self, issue_number: int, total_plays: int, *, rearmable: bool = True) -> None:
        """Increment the per-issue skip streak, escalating to a cooldown at the threshold.

        Called from :meth:`execute` for every non-skipped failure — a
        cleanly-returned ``success=False`` outcome (``rearmable=True``,
        typically a body-declared dependency block) or a ``AgentTimeout`` /
        ``AgentProcessCrashed`` raised past the accounting block
        (``rearmable=False`` — bead-readiness is irrelevant to a timeout, so the
        cooldown must not re-arm on it, #222). The ``Cooldown`` resets the streak
        counter once the cooldown fires; record the cooldown's rearmability when
        it trips.
        """
        if self._skip.record_failure(issue_number, now=total_plays) == 0:
            self._skip_rearmable[issue_number] = rearmable

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        issues: list[MaskReason] = []
        open_numbers = {issue.issue_number for issue in state.open_issues}
        self._purge_closed_issues(open_numbers)
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
                self._skip.clear(params.issue_number)
                self._skip_rearmable.pop(params.issue_number, None)
            else:
                self._record_skip(params.issue_number, state.total_plays, rearmable=True)
        return outcome
