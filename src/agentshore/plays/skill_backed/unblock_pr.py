"""UnblockPrPlay -- resolve every blocker keeping an open PR from merging."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import CapabilityGate
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.state import OrchestratorState, PlayOutcome


class UnblockPrPlay(SkillBackedPlay):
    """Resolve merge conflicts, review feedback, CI failures, and block labels on an open PR.

    Candidate validity ("is there a blocked, not-in-flight open PR to
    unblock?") lives in ``EligibilityAuthority._VALIDITY_FNS`` for
    ``UNBLOCK_PR`` and is appended by the base ``preconditions`` adapter. This
    play only declares the capability gate.
    """

    gates = (CapabilityGate("can_implement"),)

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
        if not outcome.success:
            return outcome

        reviewed_pr_number: int | None = None
        for artifact in outcome.artifacts:
            if not isinstance(artifact, dict):
                continue
            if artifact.get("type") not in {"stale_review_state", "pr_unblock_attempt"}:
                continue

            pr_number = artifact.get("pr", artifact.get("number", params.pr_number))
            if not isinstance(pr_number, int):
                continue

            head_sha = artifact.get("head_sha")
            if not isinstance(head_sha, str):
                head_sha = _head_sha_from_state(state, pr_number)
            if not head_sha:
                continue

            await ctx.store.update_pr_last_reviewed_sha(
                pr_number, ctx.session_id, head_sha, status="PASS"
            )
            reviewed_pr_number = pr_number
            break

        if reviewed_pr_number is not None:
            queue_id = params.extras.get("review_queue_id")
            if isinstance(queue_id, int):
                await ctx.store.complete_review(queue_id)
            else:
                pending = await ctx.store.list_pending_reviews(ctx.session_id)
                for row in pending:
                    if row.pr_number == reviewed_pr_number and row.queue_id is not None:
                        await ctx.store.complete_review(row.queue_id)
                        break

        return outcome


def _head_sha_from_state(state: OrchestratorState, pr_number: int) -> str | None:
    for pr in state.pull_requests:
        if pr.pr_number == pr_number:
            return pr.head_sha
    return None
