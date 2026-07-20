"""ReconcileStatePlay — self-heal AgentShore session state when wedged.

Reads AgentShore's structured logs, the worktree/plays DB, and live git state.
Identifies known pathologies (dirty trunk from a killed mutator, orphan
worktrees, zombie subprocesses, stuck lockfiles) and remediates locally.
Never touches GitHub state.

Precondition gating is declarative — see ``gates`` below. The play is
``armed`` (eligible) after any non-self failure and ``consumed`` (masked)
once it runs, until the next post-completion failure re-arms it. This makes
the gate robust to parallel-agent cascades where the failure streak counter
resets via interleaved successes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from agentshore.core.branch_sync import fast_forward_local_branch, resolve_ff_fetch_overlay
from agentshore.core.trunk_artifacts import (
    TRUNK_SCOPED_PLAY_TYPES,
    PlayWindow,
    sweep_and_reclaim_orphans,
)
from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import (
    ArmedByFailureGate,
    CapabilityGate,
    InFlightGate,
)
from agentshore.state import PlayOutcome, PlayType
from agentshore.utils import iso_to_epoch

if TYPE_CHECKING:
    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.state import OrchestratorState

_logger = structlog.get_logger(__name__)


class ReconcileStatePlay(SkillBackedPlay):
    """Diagnose and remediate wedged session state.

    Operates locally only — no GitHub mutations, no force-push, no ``git
    stash``, no CI config touches. See the skill template for the full
    forbidden-mutations list.
    """

    gates = (
        CapabilityGate("can_run_skill"),
        InFlightGate(PlayType.RECONCILE_STATE),
        ArmedByFailureGate(PlayType.RECONCILE_STATE),
    )

    async def execute(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
    ) -> PlayOutcome:
        """Run the reconcile skill, then fast-forward the local target branch.

        The post-skill sync is a deterministic safety net for drift the
        immediate post-merge hook can't catch — e.g. a human or remote merge
        into the target branch that AgentShore never executed. Fast-forward
        only and best-effort; it never raises and never alters the outcome.
        """
        outcome = await super().execute(state, params, ctx=ctx)
        await _sweep_mid_session_orphans(state, ctx)
        target_branch = ctx.cfg.project.target_branch
        if target_branch:
            await fast_forward_local_branch(
                ctx.project_path,
                target_branch,
                fetch_env_overlay=resolve_ff_fetch_overlay(ctx.cfg),
            )
        return outcome

    @property
    def play_type(self) -> PlayType:
        return PlayType.RECONCILE_STATE

    @property
    def skill_name(self) -> str:
        return "agentshore-reconcile-state"

    @property
    def capability(self) -> str | None:
        # Needs issue-creation: the skill can ``gh issue create`` a follow-up
        # for genuinely-new bugs it can't classify.
        return "can_run_skill"


async def _sweep_mid_session_orphans(state: OrchestratorState, ctx: PlayExecutionContext) -> None:
    """Reclaim orphaned trunk-scoped artifacts on every RECONCILE_STATE dispatch.

    RECONCILE_STATE is armed by any non-self failure — including ``merge_pr``'s
    ``dirty_trunk`` failure, the exact symptom of an orphaned untracked root file
    (#330). The session-start sweep only runs once at bootstrap, so a
    trunk-scoped play that crashes or defers its own per-play reclaim mid-session
    otherwise has no further reclaim opportunity until a full restart. Unlike
    bootstrap, dispatch is open here, so live in-flight trunk-scoped plays'
    windows must be excluded via ``active_windows`` — their work is in progress,
    not orphaned. Best-effort: never raises, never alters the play outcome.
    """
    try:
        rows = await ctx.store.list_trunk_play_windows(
            play_types=[pt.value for pt in TRUNK_SCOPED_PLAY_TYPES]
        )
        owner_windows: list[PlayWindow] = []
        for play_id, started_at, ended_at in rows:
            started = iso_to_epoch(started_at)
            if started is None:
                continue
            owner_windows.append(
                PlayWindow(play_id=play_id, started_at=started, ended_at=iso_to_epoch(ended_at))
            )
        active_windows = [
            PlayWindow(play_id=agent.current_play_id, started_at=start, ended_at=None)
            for agent in state.agents
            if agent.current_play_id is not None
            and agent.current_play_type in TRUNK_SCOPED_PLAY_TYPES
            and (start := iso_to_epoch(agent.current_play_started_at)) is not None
        ]
        reclaimed = await sweep_and_reclaim_orphans(
            ctx.project_path,
            store=ctx.store,
            session_id=ctx.session_id,
            owner_windows=owner_windows,
            active_windows=active_windows,
            status="reclaimed_reconcile",
        )
        if reclaimed:
            _logger.info(
                "reconcile_state_trunk_artifacts_reclaimed",
                session_id=ctx.session_id,
                reclaimed=reclaimed,
            )
    except Exception as exc:  # noqa: BLE001 — sweep must never fail reconcile_state
        _logger.warning(
            "reconcile_state_trunk_artifacts_sweep_failed",
            session_id=ctx.session_id,
            error=str(exc),
            exc_type=type(exc).__name__,
        )
