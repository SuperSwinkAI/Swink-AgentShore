"""PrunePlay — run agentshore-prune to retire stale worktrees, branches, and beads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.errors import FailureKind
from agentshore.play_pacing import STANDARD_PLAY_COOLDOWN_PLAYS
from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import (
    CapabilityGate,
    CooldownGate,
    InFlightGate,
)
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.plays.base import PlayExecutionContext
    from agentshore.state import OrchestratorState, SkillResult


class PrunePlay(SkillBackedPlay):
    """Retire infrastructure debt the orchestrator can't clear mid-session.

    Three sweeps: orphan worktrees, dead local/remote branches, and beads
    whose linked GH issue is closed. Conservative on beads — unlinked
    decomposition residue is never touched.

    Gated only on the standard skill-backed stack (capability + in-flight +
    cooldown). There is deliberately no debt-threshold precondition: the four
    sweeps in the ``agentshore-prune`` skill are independent, and the only
    cheap state-derivable signal (stale linked beads) is orthogonal to the
    worktree/branch debt that actually accumulates — so a debt gate keyed off
    it left Prune permanently masked while worktrees and branches piled up.
    Worktree/branch/bead debt is discovered and cleared at execute time; the
    play must be reachable whenever the base gates pass.
    """

    def __init__(self, *, cooldown_plays: int = STANDARD_PLAY_COOLDOWN_PLAYS) -> None:
        self.gates = (
            CapabilityGate("can_implement"),
            InFlightGate(PlayType.PRUNE),
            CooldownGate(PlayType.PRUNE, plays=cooldown_plays),
        )

    @property
    def play_type(self) -> PlayType:
        return PlayType.PRUNE

    @property
    def skill_name(self) -> str:
        return "agentshore-prune"

    @property
    def capability(self) -> str | None:
        return "can_implement"

    def estimated_cost(self, state: OrchestratorState) -> float:
        return 0.10

    async def _extra_context(
        self,
        extra_context: dict[str, object],
        ctx: PlayExecutionContext,
        state: OrchestratorState,
    ) -> None:
        """Inject the set of currently-claimed / too-young worktrees so the
        skill skips them, even when they have no pushed branch yet. Without
        this, active pickup worktrees look like orphans (no open PR, no
        commits beyond target) and get deleted mid-play.
        """
        await self._inject_worktree_guards(extra_context, ctx)

    async def _post_process_result(
        self, ctx: PlayExecutionContext, skill_result: SkillResult
    ) -> tuple[SkillResult, FailureKind | None]:
        """Hard backstop for this destructive-sweep skill: even though
        ``_extra_context`` advised the skill which worktrees to keep, the LLM
        can still ignore it and remove one out from under a live dispatch
        (#311). Detect that post-hoc and refuse to let it read as a clean
        success.
        """
        skill_result, violated = await self._guard_against_protected_worktree_removal(
            ctx, skill_result
        )
        return skill_result, FailureKind.AGENT_ERROR if violated else None
