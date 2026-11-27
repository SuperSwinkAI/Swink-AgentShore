"""Per-PR/issue repick suppression after a completed play (#6, #312, #458, #517)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.core.helpers import _logger, _SafeCallHost
from agentshore.core.terminal_park import (
    _UNBLOCK_MANUAL_REQUIRED_MARKERS,
    _WRITE_PLAN_UNPLANNABLE_MARKERS,
    TerminalParkPolicy,
)
from agentshore.github.labels import NEEDS_HUMAN_LABEL
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.core.context import _DispatchContext
    from agentshore.core.session_runtime import SessionRuntime
    from agentshore.plays.executor import PlayExecutor
    from agentshore.state import PlayOutcome


_UNBLOCK_PR_REPICK_COOLDOWN_MARKERS: tuple[str, ...] = ("merge conflict",)


def _outcome_blocked_by_sibling_pr(outcome: PlayOutcome) -> bool:
    """Return True when an unblock_pr outcome reports the target is gated on an
    unmerged sibling PR (a structured ``blocked_by_pr`` artifact).

    Such a failure is not the target PR's own fault — it is waiting on another
    open PR that this dispatch could not finish merging. It must therefore NOT
    tick the per-PR exhaustion counter or trip the ``manual-required`` park; the
    PPO will pick the blocker as its own candidate, after which the target
    becomes unblockable. ``PlayOutcome`` carries no structured ``blocked_by``
    field, so the artifact is the signal (an error-text marker would risk
    colliding with the ``_UNBLOCK_MANUAL_REQUIRED_MARKERS`` substring scan).
    """
    return any(
        isinstance(artifact, dict) and artifact.get("type") == "blocked_by_pr"
        for artifact in outcome.artifacts
    )


def _outcome_resolved_target_pr(outcome: PlayOutcome, pr_number: int) -> bool:
    """Return True when a *successful* unblock_pr outcome resolved the target PR.

    Resolution means the dispatch either merged the target (``pr_merged``) or
    dismissed the sole stale ``CHANGES_REQUESTED`` review and left the PR ready
    (``stale_review_state``). Both are definitive wins, not failed attempts, so
    they must NOT tick the per-PR exhaustion counter or trip the
    ``manual-required`` park — counting them parked a merge-ready PR after three
    no-op short-circuit successes (blocky PR #517). The artifact carries the PR
    number under ``pr`` or ``number``; an artifact with neither (legacy/loose
    shape) is treated as referring to the dispatch target.
    """
    if not outcome.success:
        return False
    for artifact in outcome.artifacts:
        if not isinstance(artifact, dict):
            continue
        if artifact.get("type") not in {"pr_merged", "stale_review_state"}:
            continue
        artifact_pr = artifact.get("pr", artifact.get("number", pr_number))
        if artifact_pr == pr_number:
            return True
    return False


class PrRepickTracker:
    """Suppresses re-selection of PRs/issues that a completion proved unfixable-for-now.

    Extracted from ``CompletionProcessor`` — all behaviour is verbatim.
    Constructed inside ``CompletionProcessor.__init__`` from the already-
    injected deps; ``CompletionProcessor._record_unblock_attempt_if_needed``,
    ``_record_merge_pr_repick_cooldown_if_needed``, and
    ``_park_unplannable_issue_if_needed`` delegate here via the same
    unbound-shim pattern ``IssueSyncer._mark_worktrees_stale_for_closed_prs``
    uses (``PrRepickTracker.<method>(self, ...)`` with the CompletionProcessor
    or a bare test stub as ``self``), so the pre-existing stub-harness tests —
    which bypass ``CompletionProcessor.__init__`` and set only a handful of
    attrs — keep working unmodified. ``self.mark_pr_manual_required`` /
    ``self.mark_issue_needs_human`` are therefore looked up dynamically on
    whatever ``self`` is passed, not on this class's own instance.
    """

    def __init__(
        self,
        *,
        host: _SafeCallHost,
        runtime: SessionRuntime,
        session_id: str,
        executor: PlayExecutor,
        terminal_park: TerminalParkPolicy,
    ) -> None:
        self._host = host
        self._runtime = runtime
        self._session_id = session_id
        self._executor = executor
        self._terminal_park = terminal_park

    async def mark_pr_manual_required(self, pr_number: int) -> None:
        """Persist a terminal manual gate after repeated unblock_pr failures."""
        await self._terminal_park.mark_pr_manual_required(pr_number)

    async def mark_issue_needs_human(self, issue_number: int) -> None:
        """Park an un-plannable issue behind NEEDS_HUMAN_LABEL (store + GitHub)."""
        await self._terminal_park.mark_issue_needs_human(issue_number)

    async def record_unblock_attempt_if_needed(
        self,
        ctx: _DispatchContext,
        outcome: PlayOutcome,
        completed_play_type: PlayType,
    ) -> None:
        if completed_play_type == PlayType.UNBLOCK_PR and ctx.params.pr_number is not None:
            if _outcome_blocked_by_sibling_pr(outcome):
                _logger.info(
                    "unblock_pr_blocked_by_sibling",
                    session_id=self._session_id,
                    pr_number=ctx.params.pr_number,
                )
                return
            if _outcome_resolved_target_pr(outcome, ctx.params.pr_number):
                self._executor._resolver.reset_unblock_pr_failures(ctx.params.pr_number)
                self._executor._resolver.clear_pr_repick_cooldown(ctx.params.pr_number)
                _logger.info(
                    "unblock_pr_resolved_target",
                    session_id=self._session_id,
                    pr_number=ctx.params.pr_number,
                )
                return
            exhausted = self._executor._resolver.record_unblock_pr_failure(ctx.params.pr_number)
            error_text = (outcome.error or "").lower()
            terminal = any(m in error_text for m in _UNBLOCK_MANUAL_REQUIRED_MARKERS)
            if exhausted or terminal:
                await self._host._safe_call(
                    self.mark_pr_manual_required(ctx.params.pr_number),
                    "mark_pr_manual_required",
                )
            elif any(m in error_text for m in _UNBLOCK_PR_REPICK_COOLDOWN_MARKERS):
                self._executor._resolver.record_pr_repick_cooldown(
                    ctx.params.pr_number,
                    ctx.state_at_dispatch.total_plays,
                    rearmable=True,
                )

    def record_merge_pr_repick_cooldown_if_needed(
        self,
        ctx: _DispatchContext,
        outcome: PlayOutcome,
        completed_play_type: PlayType,
    ) -> None:
        """Arm the fast per-PR repick cooldown on a merge_pr ``dirty_trunk`` failure (#312).

        Sibling to ``record_unblock_attempt_if_needed``'s merge_conflicts arm,
        and separate from ``TrunkWedgeEscalator.handle_merge_pr_outcome``'s
        SESSION-GLOBAL same-cause wedge counter (#330, untouched here — that
        mechanism only counts a specific root-untracked-path pathology toward
        unmasking END_SESSION, it carries no per-PR memory at all). This is
        per-PR: a ``dirty_trunk`` failure on PR #42 means re-picking #42
        immediately is wasted dispatch cost regardless of which untracked-path
        pathology caused it, so this matches on the same ``"dirty_trunk"``
        substring ``TrunkWedgeEscalator.handle_merge_pr_outcome`` checks but does
        not require the root-untracked refinement that guards the wedge
        counter's escalation.

        rearmable=False: unlike unblock_pr's merge_conflicts (whose live
        ``mergeable`` field is free to re-check every resolve), there is no
        equivalently cheap live "trunk is clean now" signal available to
        ``PlayCandidateService`` — it rides out the full cooldown window,
        mirroring issue_pickup's non-rearmable timeout/crash case (#222).
        """
        if (
            completed_play_type != PlayType.MERGE_PR
            or outcome.success
            or not isinstance(ctx.params.pr_number, int)
        ):
            return
        error_text = (outcome.error or "").lower()
        if "dirty_trunk" not in error_text:
            return
        self._executor._resolver.record_pr_repick_cooldown(
            ctx.params.pr_number,
            ctx.state_at_dispatch.total_plays,
            rearmable=False,
        )

    async def park_unplannable_issue_if_needed(
        self,
        ctx: _DispatchContext,
        outcome: PlayOutcome,
        completed_play_type: PlayType,
    ) -> None:
        if (
            completed_play_type != PlayType.WRITE_IMPLEMENTATION_PLAN
            or outcome.success
            or not isinstance(ctx.params.issue_number, int)
        ):
            return
        error_text = (outcome.error or "").lower()
        if not any(m in error_text for m in _WRITE_PLAN_UNPLANNABLE_MARKERS):
            return
        await self._host._safe_call(
            self.mark_issue_needs_human(ctx.params.issue_number),
            "mark_issue_needs_human",
        )
        self._runtime.recent_applied_labels.append((ctx.params.issue_number, NEEDS_HUMAN_LABEL))
