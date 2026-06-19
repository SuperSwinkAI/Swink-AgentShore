"""Pure predicate functions for PR/issue candidate evaluation (RL mask hot path).

These are module-level functions that must NOT allocate PlayCandidateAnalyzer —
callers on the hot RL mask path must be able to invoke them cheaply.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from agentshore.agents.worktree import TRUNK_MUTATING_PLAYS
from agentshore.github.labels import (
    BUG_LABELS,
    MANUAL_REQUIRED_LABEL,
    PRIORITY_SCORES,
)
from agentshore.github.pr_links import canonical_issue_numbers, issue_numbers_for_pr
from agentshore.play_rules import needs_review
from agentshore.pr_state import blocked_reasons

if TYPE_CHECKING:
    from agentshore.state import IssueSnapshot, OrchestratorState

# Synthetic resource key for trunk-scoped plays. Must match the constant
# in ``agentshore.plays.resolver`` — kept duplicated to avoid a circular
# import (resolver imports candidates already).
_TRUNK_RESOURCE_KEY = "trunk:main_repo"

_SIZE_RANK: dict[str, int] = {
    "size/S": 0,
    "size/M": 1,
    "size/L": 2,
    "size/XL": 3,
}
_NO_PRIORITY_SORT_KEY = 999


# ---------------------------------------------------------------------------
# Shared field-extraction helpers
# ---------------------------------------------------------------------------


def _labels(pr: object) -> list[str]:
    labels = getattr(pr, "labels", [])
    return labels if isinstance(labels, list) else []


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _bool_or_none(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# Resource-key helpers
# ---------------------------------------------------------------------------


def pr_resource_keys(
    pr_number: int,
    issue_number: int | None = None,
    linked_issue_numbers: object = (),
) -> tuple[str, ...]:
    keys = [f"pr:{pr_number}"]
    raw_links: tuple[object, ...]
    if linked_issue_numbers is None:
        raw_links = ()
    elif isinstance(linked_issue_numbers, str):
        raw_links = (linked_issue_numbers,)
    elif isinstance(linked_issue_numbers, Iterable):
        raw_links = tuple(linked_issue_numbers)
    else:
        raw_links = (linked_issue_numbers,)
    for linked_issue_number in canonical_issue_numbers((issue_number, *raw_links)):
        keys.append(f"issue:{linked_issue_number}")
    return tuple(keys)


def pr_resource_keys_for_pr(pr: object) -> tuple[str, ...]:
    pr_number = getattr(pr, "pr_number", None)
    if isinstance(pr_number, bool) or not isinstance(pr_number, int):
        return ()
    return pr_resource_keys(
        pr_number,
        getattr(pr, "issue_number", None),
        issue_numbers_for_pr(pr),
    )


def issue_resource_keys(issue_number: int) -> tuple[str, ...]:
    return (f"issue:{issue_number}",)


def active_resource_keys(state: OrchestratorState) -> frozenset[str]:
    """Return canonical resource keys currently owned by in-flight work."""

    keys: set[str] = set()
    pr_to_issues = {
        pr.pr_number: issue_numbers_for_pr(pr)
        for pr in state.pull_requests
        if issue_numbers_for_pr(pr)
    }
    issue_to_prs: dict[int, set[int]] = {}
    for pr in state.pull_requests:
        for issue_number in issue_numbers_for_pr(pr):
            issue_to_prs.setdefault(issue_number, set()).add(pr.pr_number)

    def add_issue(issue_number: int | None) -> None:
        if issue_number is None:
            return
        keys.add(f"issue:{issue_number}")
        for pr_number in issue_to_prs.get(issue_number, ()):
            keys.add(f"pr:{pr_number}")

    def add_pr(pr_number: int | None) -> None:
        if pr_number is None:
            return
        keys.add(f"pr:{pr_number}")
        for issue_number in pr_to_issues.get(pr_number, ()):
            add_issue(issue_number)

    for issue_number in state.in_flight_issues:
        add_issue(issue_number)

    active_play = state.active_play
    if active_play is not None:
        add_issue(active_play.issue_number)
        add_pr(active_play.pr_number)

    for agent in state.agents:
        if agent.current_play_type is None:
            continue
        add_issue(agent.current_play_issue_number)
        add_pr(agent.current_play_pr_number)
        # Only trunk-*mutating* plays mark trunk busy. A running read-only
        # trunk play (run_qa, design_audit, …) must not mask merge_pr (#17).
        if agent.current_play_type in TRUNK_MUTATING_PLAYS:
            keys.add(_TRUNK_RESOURCE_KEY)

    # in_flight_plays carries play types that lack agent context (e.g. a
    # play dispatched before the agent snapshot updates). Still need to
    # honor their trunk lock — but again only for trunk-mutating plays.
    for play_type in state.in_flight_plays:
        if play_type in TRUNK_MUTATING_PLAYS:
            keys.add(_TRUNK_RESOURCE_KEY)
            break

    return frozenset(keys)


def resource_conflict_reason(
    resource_keys: tuple[str, ...],
    active_keys: frozenset[str],
) -> str | None:
    """Return a stable blocked reason when a candidate overlaps active work."""

    conflicts = sorted(set(resource_keys) & set(active_keys))
    if not conflicts:
        return None
    return f"resource already in flight: {', '.join(conflicts)}"


def _candidate_resolved_agent_type(
    candidate: PlayCandidate,
    agent_id_to_type: dict[str, str],
) -> str | None:
    """Resolve the agent-type value a candidate would run on, or ``None``.

    A candidate names its agent type either directly via
    ``params.target_agent_type`` (an instantiate_agent's created type, or a
    type-pinned dispatch) or indirectly via the concrete agent it targets
    (``params.target_agent_id``, mapped through *agent_id_to_type*). Candidates
    with neither (issue/PR plays whose runner the resolver picks later) return
    ``None`` and are never auth-suppressed at this layer.
    """
    params = candidate.params
    if params.target_agent_type:
        return params.target_agent_type
    if params.target_agent_id is not None:
        return agent_id_to_type.get(params.target_agent_id)
    return None


def _candidate_auth_suppressed_type(
    candidate: PlayCandidate,
    auth_suppressed: frozenset[str],
    agent_id_to_type: dict[str, str],
) -> str | None:
    """Return the suppressed agent-type value if this candidate is masked, else None."""
    if not auth_suppressed:
        return None
    resolved = _candidate_resolved_agent_type(candidate, agent_id_to_type)
    if resolved is not None and resolved in auth_suppressed:
        return resolved
    return None


def _candidate_wedge_cooldown_type(
    candidate: PlayCandidate,
    wedge_cooldown: frozenset[str],
    agent_id_to_type: dict[str, str],
) -> str | None:
    """Return the cooled-down agent-type value if this candidate is masked, else None.

    Sibling of ``_candidate_auth_suppressed_type`` for the DECAYING launch-wedge
    cooldown (#202): the cooldown set already contains only active (non-expired)
    types, so auto-unmask happens for free once the state-builder drops the
    expired entry.
    """
    if not wedge_cooldown:
        return None
    resolved = _candidate_resolved_agent_type(candidate, agent_id_to_type)
    if resolved is not None and resolved in wedge_cooldown:
        return resolved
    return None


def issue_pickup_sort_key(issue: IssueSnapshot) -> tuple[int, int, int, int]:
    """Sort tuple: bug first, then priority, size, and issue number."""

    is_bug = 0 if any(lbl in BUG_LABELS for lbl in issue.labels) else 1
    if issue.priority is not None:
        priority = issue.priority
    else:
        priority = next(
            (PRIORITY_SCORES[lbl] for lbl in issue.labels if lbl in PRIORITY_SCORES),
            _NO_PRIORITY_SORT_KEY,
        )
    size = next(
        (_SIZE_RANK[lbl] for lbl in issue.labels if lbl in _SIZE_RANK),
        _NO_PRIORITY_SORT_KEY,
    )
    return (is_bug, priority, size, issue.issue_number)


def _pr_blocked_reasons(pr: object, *, labels: list[str] | None = None) -> list[str]:
    """Gather the eight PR fields once and return the canonical blocked reasons.

    Shared by :func:`pr_merge_ready` and :func:`pr_unblockable` so the two
    predicates can never disagree on the underlying field reads. ``labels`` may
    be passed when the caller has already computed them to avoid a second
    ``_labels(pr)`` traversal.
    """
    return blocked_reasons(
        state=str(getattr(pr, "state", "")),
        labels=_labels(pr) if labels is None else labels,
        review_decision=_string_or_none(getattr(pr, "review_decision", None)),
        status_check_summary=_string_or_none(getattr(pr, "status_check_summary", None)),
        is_draft=_bool_or_none(getattr(pr, "is_draft", None)),
        mergeable=getattr(pr, "mergeable", None),
    )


def pr_merge_ready(pr: object, *, target_branch: str | None = None) -> bool:
    """Return True for the one canonical merge-pr readiness predicate.

    A PR is merge-ready only when GitHub reports it mergeable, an approval
    signal is present (GitHub APPROVED or an AgentShore code-review PASS at the
    current head_sha), and no blocking reason (changes_requested, blocked
    label, ci_failed, manual_required, merge_conflicts) is in effect. The
    blocking check shares :func:`_pr_blocked_reasons` with
    :func:`pr_unblockable` so the two predicates can never simultaneously be
    true for the same PR — without this, a stale AgentShore PASS verdict can
    keep a PR in the merge_ready set even after a human reviewer adds
    CHANGES_REQUESTED or a ``blocked`` label.

    When ``target_branch`` is provided, a PR whose ``base_ref`` is known and
    does NOT match it is refused — a deterministic backstop so ``merge_pr`` can
    never merge a PR opened against the wrong base (e.g. ``main`` instead of the
    configured ``integration``), independent of whether the authoring/merging
    agent honored the skill's base step. The create-side auto-correction
    (executor ``_wire_deferrals``) retargets such PRs to the target, after which
    they re-qualify here.
    """

    reasons = _pr_blocked_reasons(pr)
    if reasons and reasons != ["draft"]:
        return False

    base_ref = getattr(pr, "base_ref", None)
    if isinstance(base_ref, str) and base_ref.startswith("agentshore/"):
        return False
    # Deterministic base gate: refuse to merge a PR whose known base is not the
    # configured target branch. Pairs with the create-side auto-correction.
    if target_branch and isinstance(base_ref, str) and base_ref and base_ref != target_branch:
        return False

    review_decision = getattr(pr, "review_decision", None)
    last_review_status = getattr(pr, "last_review_status", None)
    last_reviewed_sha = getattr(pr, "last_reviewed_sha", None)
    head_sha = getattr(pr, "head_sha", None)
    # The AgentShore PASS-at-head branch is the autonomous approval path (no
    # human reviewer). It must never override a live human CHANGES_REQUESTED —
    # _pr_blocked_reasons already returns early on that, but guard explicitly so
    # the invariant is local and can't regress if the gate above changes (#344).
    return bool(
        getattr(pr, "mergeable", None) == "MERGEABLE"
        and review_decision != "CHANGES_REQUESTED"
        and (
            review_decision == "APPROVED"
            or (
                last_review_status == "PASS"
                and last_reviewed_sha is not None
                and head_sha is not None
                and last_reviewed_sha == head_sha
            )
        )
    )


def pr_review_needed(pr: object) -> bool:
    return bool(not getattr(pr, "is_draft", False) and needs_review(pr))


def pr_reviewable(pr: object) -> bool:
    """code_review is actionable only when the PR needs review AND is not parked
    for human intervention.

    Like :func:`pr_unblockable` and :func:`pr_merge_ready` (which exclude
    ``MANUAL_REQUIRED_LABEL`` via ``_pr_blocked_reasons``), this excludes a
    manual-required PR, so all three actionable-PR predicates agree: a
    manual-required PR is never agent-actionable. Without this, an unreviewed
    manual-required PR leaks into the reviewable set, keeping
    ``has_actionable_work`` (and thus ``terminal_no_work``) wrong and pinning
    END_SESSION masked forever.
    """
    if MANUAL_REQUIRED_LABEL in _labels(pr):
        return False
    return pr_review_needed(pr)


def pr_unblockable(pr: object) -> bool:
    labels = _labels(pr)
    if MANUAL_REQUIRED_LABEL in labels:
        return False
    if getattr(pr, "blocked", False):
        return True
    if getattr(pr, "mergeable", None) == "CONFLICTING":
        return True
    reasons = _pr_blocked_reasons(pr, labels=labels)
    return bool(reasons and reasons != ["draft"])


# ---------------------------------------------------------------------------
# Forward reference for type annotations — PlayCandidate is defined in __init__
# but _candidate_*_type functions reference it. We use TYPE_CHECKING import
# to avoid the circular dependency at runtime.
# ---------------------------------------------------------------------------

if TYPE_CHECKING:
    from agentshore.plays.candidates import PlayCandidate
