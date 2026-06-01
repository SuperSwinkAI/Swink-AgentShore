"""SystematicDebuggingPlay -- investigate failures before fixes."""

from __future__ import annotations

from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import CapabilityGate
from agentshore.state import PlayType


class SystematicDebuggingPlay(SkillBackedPlay):
    """Find root cause for explicit QA/debug failures before a fix attempt.

    Candidate validity ("is there an eligible QA/debug-labeled issue that is
    not in-flight, PR-linked, or root-cause-found?") lives in
    ``EligibilityAuthority._VALIDITY_FNS`` for ``SYSTEMATIC_DEBUGGING`` and is
    appended by the base ``preconditions`` adapter. This play only declares the
    capability gate.
    """

    gates = (CapabilityGate("can_implement"),)

    @property
    def play_type(self) -> PlayType:
        return PlayType.SYSTEMATIC_DEBUGGING

    @property
    def skill_name(self) -> str:
        return "agentshore-systematic-debugging"

    @property
    def capability(self) -> str | None:
        return "can_implement"
