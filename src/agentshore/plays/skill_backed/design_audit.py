"""DesignAuditPlay — audit design docs and create missing scope issues."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from agentshore.play_rules import DESIGN_AUDIT_COOLDOWN_PLAYS
from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import (
    BeadsInitializedGate,
    CapabilityGate,
    CooldownGate,
    InFlightGate,
)
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.state import JsonArtifact, JsonObject, OrchestratorState, PlayOutcome


class DesignAuditPlay(SkillBackedPlay):
    """Create GitHub/beads work items for gaps found in specs and design docs."""

    gates = (
        BeadsInitializedGate(),
        InFlightGate(PlayType.DESIGN_AUDIT),
        CapabilityGate("can_run_skill"),
        CooldownGate(PlayType.DESIGN_AUDIT, plays=DESIGN_AUDIT_COOLDOWN_PLAYS),
    )

    @property
    def play_type(self) -> PlayType:
        return PlayType.DESIGN_AUDIT

    @property
    def skill_name(self) -> str:
        return "agentshore-design-audit"

    @property
    def capability(self) -> str | None:
        return "can_run_skill"

    def estimated_cost(self, state: OrchestratorState) -> float:
        return 0.08

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

        audit_error = _validate_design_audit_artifact(outcome.artifacts)
        if audit_error is None:
            return outcome

        return replace(
            outcome,
            success=False,
            partial=True,
            error=audit_error,
        )


def _validate_design_audit_artifact(artifacts: list[JsonArtifact]) -> str | None:
    audit = _find_design_audit_artifact(artifacts)
    if audit is None:
        return "design_audit result missing required design_audit artifact"

    counts: dict[str, int] = {}
    for key in (
        "requirements_scanned",
        "gaps_found",
        "issues_created",
        "issues_linked",
        "unresolved_gaps",
        "unknown_requirements",
    ):
        value = _coerce_nonnegative_int(audit.get(key))
        if value is None:
            return f"design_audit artifact has invalid {key!r}"
        counts[key] = value

    if counts["requirements_scanned"] < counts["gaps_found"]:
        return (
            "design_audit artifact reports more gaps than scanned requirements "
            f"({counts['gaps_found']}/{counts['requirements_scanned']})"
        )

    if counts["unresolved_gaps"] > 0 or counts["unknown_requirements"] > 0:
        return (
            "design_audit found "
            f"{counts['unresolved_gaps']} unresolved gaps and "
            f"{counts['unknown_requirements']} unknown requirements"
        )

    if counts["gaps_found"] > 0:
        gap_issue_numbers = audit.get("gap_issue_numbers")
        if not isinstance(gap_issue_numbers, list):
            return "design_audit artifact missing gap_issue_numbers"
        covered_gaps = [
            issue_number
            for issue_number in gap_issue_numbers
            if _coerce_nonnegative_int(issue_number) is not None
        ]
        if len(covered_gaps) < counts["gaps_found"]:
            return (
                "design_audit did not create/link issues for all gaps "
                f"({len(covered_gaps)}/{counts['gaps_found']} covered)"
            )

    return None


def _find_design_audit_artifact(artifacts: list[JsonArtifact]) -> JsonObject | None:
    for artifact in artifacts:
        if isinstance(artifact, dict) and artifact.get("type") == "design_audit":
            return artifact
    return None


def _coerce_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None
