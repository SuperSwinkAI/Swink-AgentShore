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

from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import (
    ArmedByFailureGate,
    CapabilityGate,
    InFlightGate,
)
from agentshore.state import PlayType


class ReconcileStatePlay(SkillBackedPlay):
    """Diagnose and remediate wedged session state.

    Operates locally only — no GitHub mutations, no force-push, no ``git
    stash``, no CI config touches. See the skill template for the full
    forbidden-mutations list.
    """

    gates = (
        CapabilityGate("can_create_issues"),
        InFlightGate(PlayType.RECONCILE_STATE),
        ArmedByFailureGate(PlayType.RECONCILE_STATE),
    )

    @property
    def play_type(self) -> PlayType:
        return PlayType.RECONCILE_STATE

    @property
    def skill_name(self) -> str:
        return "agentshore-reconcile-state"

    @property
    def capability(self) -> str | None:
        # The skill can ``gh issue create`` a follow-up for genuinely-new
        # bugs it can't classify, so it needs issue-creation capability.
        return "can_create_issues"
