"""PlayRegistry — register, freeze, and look up Play implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentshore.config import RuntimeConfig
    from agentshore.plays.base import Play
    from agentshore.state import OrchestratorState, PlayType


class PlayRegistry:
    """Mutable registry that maps PlayType → Play, frozen after bootstrap.

    Usage::

        registry = PlayRegistry()
        registry.register(MyPlay())
        registry.freeze()

        play = registry.get(PlayType.ISSUE_PICKUP)
        ok = registry.preconditions_met(PlayType.ISSUE_PICKUP, state)
    """

    def __init__(self) -> None:
        self._plays: dict[PlayType, Play] = {}
        self._frozen = False

    def register(self, play: Play) -> None:
        """Add *play* to the registry.

        Raises RuntimeError if the registry is already frozen or if a play for
        the same PlayType has already been registered.
        """
        if self._frozen:
            raise RuntimeError(
                f"Registry is frozen — cannot register {play.play_type!r} after freeze()"
            )
        if play.play_type in self._plays:
            raise ValueError(f"Duplicate play registration for {play.play_type!r}")
        self._plays[play.play_type] = play

    def freeze(self) -> None:
        """Prevent further registrations."""
        self._frozen = True

    def get(self, play_type: PlayType) -> Play:
        """Return the Play for *play_type*, raising KeyError if not registered."""
        try:
            return self._plays[play_type]
        except KeyError:
            raise KeyError(f"No play registered for {play_type!r}") from None

    def covered(self) -> set[PlayType]:
        """Return the set of play types currently registered."""
        return set(self._plays)

    def preconditions_met(self, play_type: PlayType, state: OrchestratorState) -> bool:
        """Return True iff the play's preconditions list is empty.

        Raises ``KeyError`` if *play_type* is not registered. The default
        registry registers all PlayTypes and freezes, so a missing lookup is a
        registry-wiring bug, not a runtime "not eligible" condition — surfacing
        it loudly beats masking it as a benign ``False``.
        """
        play = self.get(play_type)
        return len(play.preconditions(state)) == 0


def build_default_registry(cfg: RuntimeConfig | None = None) -> PlayRegistry:
    """Instantiate and register all plays, then freeze and return the registry.

    *cfg* lets caller-tunable settings (e.g. agent spawn limits) flow into the
    plays.  Tests that exercise mask logic can omit it; defaults apply.
    """
    from agentshore.plays.internal.end_agent import EndAgentPlay
    from agentshore.plays.internal.end_session import EndSessionPlay
    from agentshore.plays.internal.instantiate_agent import InstantiateAgentPlay
    from agentshore.plays.internal.reserved_action import (
        FutureEightPlay,
        FutureFourPlay,
        FutureSevenPlay,
    )
    from agentshore.plays.internal.take_break import TakeBreakPlay
    from agentshore.plays.skill_backed.calibrate_alignment import CalibrateAlignmentPlay
    from agentshore.plays.skill_backed.cleanup import CleanupPlay
    from agentshore.plays.skill_backed.code_review import CodeReviewPlay
    from agentshore.plays.skill_backed.design_audit import DesignAuditPlay
    from agentshore.plays.skill_backed.groom_backlog import GroomBacklogPlay
    from agentshore.plays.skill_backed.issue_pickup import IssuePickupPlay
    from agentshore.plays.skill_backed.merge_pr import MergePRPlay
    from agentshore.plays.skill_backed.prune import PrunePlay
    from agentshore.plays.skill_backed.reconcile_state import ReconcileStatePlay
    from agentshore.plays.skill_backed.refine_tasks import RefineTaskBreakdownPlay
    from agentshore.plays.skill_backed.run_qa import RunQAPlay
    from agentshore.plays.skill_backed.seed_project import SeedProjectPlay
    from agentshore.plays.skill_backed.systematic_debugging import SystematicDebuggingPlay
    from agentshore.plays.skill_backed.unblock_pr import UnblockPrPlay
    from agentshore.plays.skill_backed.write_plan import WriteImplementationPlanPlay

    spawn_cfg = cfg.agent_spawn if cfg is not None else None
    seed_project_ceiling = (
        cfg.scope.seed_project_mid_session_issue_ceiling if cfg is not None else 10
    )

    registry = PlayRegistry()
    # Registration order mirrors PlayType enum definition order so the action
    # space's V1_ACTION_ORDER and this registry stay in lockstep — easy to scan
    # against ``src/agentshore/state.py:PlayType``.
    for play in (
        InstantiateAgentPlay(spawn_cfg=spawn_cfg),
        UnblockPrPlay(),
        WriteImplementationPlanPlay(),
        EndAgentPlay(),
        IssuePickupPlay(),
        CodeReviewPlay(),
        MergePRPlay(),
        RunQAPlay(),
        SystematicDebuggingPlay(),
        DesignAuditPlay(),
        EndSessionPlay(),
        ReconcileStatePlay(),
        RefineTaskBreakdownPlay(),
        CleanupPlay(),
        FutureFourPlay(),
        TakeBreakPlay(),
        GroomBacklogPlay(),
        SeedProjectPlay(mid_session_issue_ceiling=seed_project_ceiling),
        CalibrateAlignmentPlay(),
        PrunePlay(),
        FutureSevenPlay(),
        FutureEightPlay(),
    ):
        registry.register(play)
    registry.freeze()
    return registry
