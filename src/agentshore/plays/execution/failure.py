"""Failure-category inference for play outcomes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.error_markers import AUTH_MARKERS

if TYPE_CHECKING:
    from agentshore.state import PlayOutcome


def _infer_failure_category(outcome: PlayOutcome) -> str:
    """Map a failed PlayOutcome to a FailureCategory string.

    Prefer the typed ``failure_kind`` the play set at the failure site; the
    substring ladder below is the fallback for legacy / uncaught-Exception
    paths that never set a kind.
    """
    if outcome.failure_kind is not None:
        return str(outcome.failure_kind.to_category())
    error = (outcome.error or "").lower()
    # Canonical AUTH_MARKERS superset (Phase 4): covers GitHub-table spellings
    # ("repository not found", "http 401/403") the publish subset lacked, and
    # avoids the bare "auth" fallback that false-matched "author"/"authorized".
    if any(marker in error for marker in AUTH_MARKERS):
        return "agent_error"
    if error.startswith(("test", "ci", "pytest", "lint")):
        return "test_failure"
    if "anti_confirmation" in error or "approval" in error or "scope" in error:
        return "alignment_drift"
    if any(
        kw in error
        for kw in (
            "timeout",
            "crash",
            "circuit breaker",
            "circuit_breaker",
            "malformed",
            "invalid output",
        )
    ):
        return "agent_error"
    if any(
        kw in error
        for kw in (
            "needs different reviewer",
            "status-checks-pending",
            "status_checks_pending",
            "too ambiguous",
            "blocked by open dependency",
            "merge_conflicts",
        )
    ):
        return "gate_rejection"
    return "code_error"
