"""SeedProjectPlay — run agentshore-seed-project to audit the canonical backlog."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from agentshore.plays.skill_backed.base import SkillBackedPlay
from agentshore.plays.skill_backed.gates import InFlightGate
from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.state import JsonArtifact, JsonObject, OrchestratorState, PlayOutcome

_SEED_PROJECT_DEFAULT_MID_SESSION_ISSUE_CEILING = 10


class SeedProjectPlay(SkillBackedPlay):
    """Audit seed/design scope and repair the beads/GitHub backlog.

    Mid-session gate (desktop-hzgb): after the first play of a session,
    seed_project is allowed iff ``open_issues_count < ceiling``.  The
    ceiling defaults to 10 and is tunable via
    ``cfg.scope.seed_project_mid_session_issue_ceiling``.  There is no
    post-failure carve-out — the asymmetric retry path powered the
    17-back-to-back-seeds incident and has been removed.
    """

    gates = (InFlightGate(PlayType.SEED_PROJECT),)

    def __init__(
        self,
        *,
        mid_session_issue_ceiling: int = _SEED_PROJECT_DEFAULT_MID_SESSION_ISSUE_CEILING,
    ) -> None:
        self._mid_session_issue_ceiling = mid_session_issue_ceiling

    @property
    def play_type(self) -> PlayType:
        return PlayType.SEED_PROJECT

    @property
    def skill_name(self) -> str:
        return "agentshore-seed-project"

    @property
    def capability(self) -> str | None:
        return "can_implement"

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        reasons = super().preconditions(state)
        if reasons:
            return reasons

        # Carve-out for projects that actually need seeding: the bd graph is
        # empty (issue #566). The previous gate keyed on
        # ``state.last_play_success_by_type.get(SEED_PROJECT) is None`` —
        # which conflated "this session's first play" with "project needs
        # seeding". On a fresh session against an already-seeded project (200
        # open issues, established epic graph), the old carve-out fired and
        # bypassed the 10-issue ceiling. The new predicate keys on the actual
        # signal: if the bd graph already has epics, the project has been
        # seeded and the ceiling applies uniformly regardless of session
        # history.
        graph_is_empty = state.graph is None or not state.graph.has_epics
        if graph_is_empty:
            return []

        # Already-seeded project: allow iff open_issues_count < ceiling
        # (desktop-hzgb).  The ceiling prevents the unlimited-retry pattern
        # that powered the 17-back-to-back-seeds incident.
        count = len(state.open_issues)
        ceiling = self._mid_session_issue_ceiling
        if count >= ceiling:
            return [
                MaskReason(
                    text=(
                        f"seed_project gated: {count} open issues exceeds "
                        f"the {ceiling}-issue ceiling (graph already seeded)"
                    ),
                    classification=MaskClassification.HARD,
                    source=MaskSource.PRECONDITION,
                )
            ]
        return []

    def estimated_cost(self, state: OrchestratorState) -> float:
        return 0.10

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

        audit_error = _validate_seed_audit_artifact(outcome.artifacts)
        if audit_error is None:
            return outcome

        return replace(
            outcome,
            success=False,
            partial=True,
            error=audit_error,
        )


# Fail seed_project when it creates an implausible number of new issues.
_SEED_PROJECT_MAX_NEW_ISSUES_PER_RUN = 25


def _validate_seed_audit_artifact(artifacts: list[JsonArtifact]) -> str | None:
    audit = _find_seed_audit_artifact(artifacts)
    if audit is None:
        return "seed_project result missing required seed_audit artifact"

    counts: dict[str, int] = {}
    for key in (
        "requirements_total",
        "verified_requirements",
        "represented_open_requirements",
        "scope_gaps_found",
        "unresolved_scope_gaps",
        "unknown_requirements",
    ):
        value = _coerce_nonnegative_int(audit.get(key))
        if value is None:
            return f"seed_project seed_audit artifact has invalid {key!r}"
        counts[key] = value

    if counts["scope_gaps_found"] > _SEED_PROJECT_MAX_NEW_ISSUES_PER_RUN:
        return (
            f"too_many_scope_gaps_detected: {counts['scope_gaps_found']} "
            f"scope gaps exceeds per-run cap of "
            f"{_SEED_PROJECT_MAX_NEW_ISSUES_PER_RUN} — human review required "
            "before backlog explosion"
        )

    if counts["unresolved_scope_gaps"] > 0 or counts["unknown_requirements"] > 0:
        return (
            "seed_project audit found "
            f"{counts['unresolved_scope_gaps']} unresolved scope gaps and "
            f"{counts['unknown_requirements']} unknown requirements"
        )

    scope_gap_issue_numbers = audit.get("scope_gap_issue_numbers")
    if counts["scope_gaps_found"] > 0:
        if not isinstance(scope_gap_issue_numbers, list):
            return "seed_project seed_audit artifact missing scope_gap_issue_numbers"
        created_or_linked_scope_gaps = [
            issue_number
            for issue_number in scope_gap_issue_numbers
            if _coerce_nonnegative_int(issue_number) is not None
        ]
        if len(created_or_linked_scope_gaps) < counts["scope_gaps_found"]:
            return (
                "seed_project audit did not create/link issues for all scope gaps "
                f"({len(created_or_linked_scope_gaps)}/{counts['scope_gaps_found']} covered)"
            )

    covered_requirements = counts["verified_requirements"] + counts["represented_open_requirements"]
    if covered_requirements < counts["requirements_total"]:
        return (
            "seed_project audit did not account for all requirements "
            f"({covered_requirements}/{counts['requirements_total']} covered)"
        )

    return None


def _find_seed_audit_artifact(artifacts: list[JsonArtifact]) -> JsonObject | None:
    for artifact in artifacts:
        if isinstance(artifact, dict) and artifact.get("type") == "seed_audit":
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
