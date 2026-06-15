"""UnblockPrPlay -- resolve every blocker keeping an open PR from merging."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import aiosqlite
import structlog

from agentshore.plays.skill_backed._merge_reconcile import reconcile_merged_pr
from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import CapabilityGate
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.state import OrchestratorState, PlayOutcome

_logger = structlog.get_logger(__name__)


class UnblockPrPlay(SkillBackedPlay):
    """Resolve merge conflicts, review feedback, CI failures, and block labels on an open PR.

    Candidate validity ("is there a blocked, not-in-flight open PR to
    unblock?") lives in ``EligibilityAuthority._VALIDITY_FNS`` for
    ``UNBLOCK_PR`` and is appended by the base ``preconditions`` adapter. This
    play only declares the capability gate.

    The skill may also resolve a *stacked / mutually-blocking* sibling PR — when
    the target is blocked by another open PR it can merge that ready blocker in
    place (emitting a ``pr_merged`` artifact) and/or unblock it (emitting a
    ``pr_unblock_attempt`` for the sibling). ``execute`` reconciles those
    sibling effects into the local cache so it stays consistent before the next
    GitHub refresh.
    """

    gates = (CapabilityGate("can_implement"),)

    # PR-scoped: self-heal the PR base before unblocking so code_review/merge_pr
    # downstream see the right base.
    retarget_pr_base = True

    @property
    def play_type(self) -> PlayType:
        return PlayType.UNBLOCK_PR

    @property
    def skill_name(self) -> str:
        return "agentshore-unblock-pr"

    @property
    def capability(self) -> str | None:
        return "can_implement"

    async def execute(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
    ) -> PlayOutcome:
        outcome = await super().execute(state, params, ctx=ctx)

        # Reconcile any sibling PR the skill merged in place. This runs even on
        # an overall failure: a stacked blocker can be merged while the target
        # itself still carries an independent blocker (partial success), and the
        # merged sibling must be propagated either way.
        await self._reconcile_merged_blockers(outcome, ctx, state)

        if not outcome.success:
            return outcome

        # Record an AgentShore review PASS for every PR this dispatch unblocked
        # — the target plus any sibling whose branch we pushed commits to. A
        # successful unblock counts as AgentShore's code review for that PR.
        reviewed_pr_numbers: list[int] = []
        seen: set[int] = set()
        for artifact in outcome.artifacts:
            if not isinstance(artifact, dict):
                continue
            if artifact.get("type") not in {"stale_review_state", "pr_unblock_attempt"}:
                continue

            pr_number = artifact.get("pr", artifact.get("number", params.pr_number))
            if not isinstance(pr_number, int) or pr_number in seen:
                continue

            head_sha = artifact.get("head_sha")
            if not isinstance(head_sha, str):
                head_sha = _head_sha_from_state(state, pr_number)
            if not head_sha:
                continue

            await ctx.store.update_pr_last_reviewed_sha(
                pr_number, ctx.session_id, head_sha, status="PASS"
            )
            seen.add(pr_number)
            reviewed_pr_numbers.append(pr_number)

        if reviewed_pr_numbers:
            await self._complete_reviews(reviewed_pr_numbers, params, ctx)

        return outcome

    async def _reconcile_merged_blockers(
        self,
        outcome: PlayOutcome,
        ctx: PlayExecutionContext,
        state: OrchestratorState,
    ) -> None:
        """Propagate any ``pr_merged`` sibling artifacts into the local cache.

        Best-effort per artifact: a failure reconciling one merged blocker is
        logged and never aborts the play outcome or the remaining artifacts.
        """
        seen: set[int] = set()
        for artifact in outcome.artifacts:
            if not isinstance(artifact, dict) or artifact.get("type") != "pr_merged":
                continue
            pr_number = artifact.get("pr", artifact.get("number"))
            if not isinstance(pr_number, int) or pr_number in seen:
                continue
            seen.add(pr_number)
            try:
                await reconcile_merged_pr(pr_number, ctx=ctx, state=state)
            except (aiosqlite.Error, sqlite3.Error, RuntimeError, OSError) as exc:
                _logger.warning(
                    "unblock_pr_reconcile_merged_failed",
                    pr_number=pr_number,
                    error=str(exc),
                )

    async def _complete_reviews(
        self,
        pr_numbers: list[int],
        params: PlayParams,
        ctx: PlayExecutionContext,
    ) -> None:
        """Mark the review-queue rows for the unblocked PRs done.

        The target PR's queue id is carried on ``params.extras`` when the play
        was dispatched off the review queue; sibling PRs (and a target dispatched
        without a queue row) are matched against the pending-review list.
        """
        queue_id = params.extras.get("review_queue_id")
        pending = None
        for pr_number in pr_numbers:
            if pr_number == params.pr_number and isinstance(queue_id, int):
                await ctx.store.complete_review(queue_id)
                continue
            if pending is None:
                pending = await ctx.store.list_pending_reviews(ctx.session_id)
            for row in pending:
                if row.pr_number == pr_number and row.queue_id is not None:
                    await ctx.store.complete_review(row.queue_id)
                    break


def _head_sha_from_state(state: OrchestratorState, pr_number: int) -> str | None:
    for pr in state.pull_requests:
        if pr.pr_number == pr_number:
            return pr.head_sha
    return None
