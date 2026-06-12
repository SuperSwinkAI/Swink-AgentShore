"""Shared pull-request state helpers."""

from __future__ import annotations

BLOCKED_PR_STATES = {"blocked", "changes_requested", "ci_failed"}
BLOCKED_PR_LABELS = {
    "blocked",
    "agentshore/blocked",
    "agentshore/manual-required",
    "needs-work",
    "changes-requested",
    "do-not-merge",
}
FAILED_STATUS_STATES = {
    "FAILURE",
    "ERROR",
    "FAILED",
    "TIMED_OUT",
    "CANCELLED",
    "ACTION_REQUIRED",
}
PENDING_STATUS_STATES = {"PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "REQUESTED"}
SUCCESS_STATUS_STATES = {"SUCCESS", "PASSED"}


def label_names(raw: object) -> list[str]:
    """Normalize GitHub label payloads into label names."""
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for label in raw:
        if isinstance(label, dict):
            name = label.get("name")
            if name is not None:
                names.append(str(name))
        else:
            names.append(str(label))
    return names


def status_rollup_has_failure(raw: object) -> bool:
    """Return true when a GitHub statusCheckRollup payload contains a failed check."""
    return bool(_collect_rollup_states(raw) & FAILED_STATUS_STATES)


def status_rollup_summary(raw: object) -> str | None:
    """Summarize a GitHub statusCheckRollup payload for UI/state consumers."""
    states = _collect_rollup_states(raw)
    if states & FAILED_STATUS_STATES:
        return "failed"
    if not states:
        return None
    if states & PENDING_STATUS_STATES:
        return "pending"
    if states <= SUCCESS_STATUS_STATES:
        return "passed"
    return "unknown"


def blocked_reasons(
    *,
    state: str,
    labels: list[str],
    review_decision: str | None,
    status_check_summary: str | None,
    is_draft: bool | None = None,
    mergeable: str | None = None,
) -> list[str]:
    """Derive normalized reasons a PR is not ready for normal review/merge flow.

    A current GitHub ``CHANGES_REQUESTED`` review decision always blocks. An
    AgentShore ``PASS`` verdict never overrides it — previously a PASS logged at
    the same head SHA as a fresh human CHANGES_REQUESTED suppressed the reason,
    which let ``merge_pr`` repeatedly select a PR a human had explicitly blocked
    (#344 merge starvation: the PASS and the CHANGES_REQUESTED were both at the
    PR's current head, indistinguishable in order, so the suppression fired on a
    live human verdict). A CHANGES_REQUESTED clears only when GitHub's
    ``reviewDecision`` itself changes — i.e. a fresh review/approval at the new
    head, which is exactly the gate GitHub branch protection enforces.
    """
    if is_draft:
        return ["draft"]

    reasons: list[str] = []
    normalized_state = state.lower()
    if normalized_state in BLOCKED_PR_STATES:
        reasons.append(normalized_state)
    if review_decision == "CHANGES_REQUESTED":
        reasons.append("changes_requested")
    if "agentshore/manual-required" in labels:
        reasons.append("manual_required")
    if any(label in BLOCKED_PR_LABELS for label in labels):
        reasons.append("blocked_label")
    if status_check_summary == "failed":
        reasons.append("ci_failed")
    if mergeable == "CONFLICTING":
        reasons.append("merge_conflicts")

    return list(dict.fromkeys(reasons))


def _collect_rollup_states(raw: object) -> set[str]:
    """Recursively pull every ``status``/``conclusion`` value from a rollup payload.

    Single traversal of the arbitrarily-nested ``dict | list`` statusCheckRollup
    structure; callers derive failure/pending/success predicates from the
    returned set so they cannot disagree about what counts as a state.
    """
    if isinstance(raw, dict):
        states = {
            str(raw.get("status", "")).upper(),
            str(raw.get("conclusion", "")).upper(),
        }
        states.discard("")
        for value in raw.values():
            states.update(_collect_rollup_states(value))
        return states
    if isinstance(raw, list):
        list_states: set[str] = set()
        for value in raw:
            list_states.update(_collect_rollup_states(value))
        return list_states
    return set()
