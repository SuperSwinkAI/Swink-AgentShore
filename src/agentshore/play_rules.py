"""Shared play eligibility rules used outside individual play classes."""

from __future__ import annotations

from agentshore.state import PlayType

SEED_PROJECT_COOLDOWN_PLAYS = 50
DESIGN_AUDIT_FRESHNESS_WINDOW_PLAYS = 20
TERMINAL_SHUTDOWN_EVIDENCE_WINDOW_PLAYS = 50

# Play types whose validity is gated on a concrete candidate target existing in
# the snapshot candidate plan. Single source of truth for the candidate-required
# taxonomy, imported by both ``rl.eligibility`` (the authority that confirms a
# play) and ``rl.mask`` (the pipeline that gates one) so the two surfaces can
# never drift.
CANDIDATE_REQUIRED_PLAY_TYPES: frozenset[PlayType] = frozenset(
    {
        PlayType.UNBLOCK_PR,
        PlayType.WRITE_IMPLEMENTATION_PLAN,
        PlayType.ISSUE_PICKUP,
        PlayType.CODE_REVIEW,
        PlayType.MERGE_PR,
        PlayType.SYSTEMATIC_DEBUGGING,
        PlayType.REFINE_TASK_BREAKDOWN,
        PlayType.GROOM_BACKLOG,
    }
)

# Candidate-bearing target plays that ``EligibilityAuthority.confirm`` re-checks
# against the live candidate plan (the candidate-required set minus the backlog
# play GROOM_BACKLOG, which has no pinned issue/PR target). Internal and control
# plays are not target-confirmed.
LIVE_CONFIRM_PLAY_TYPES: frozenset[PlayType] = CANDIDATE_REQUIRED_PLAY_TYPES - {
    PlayType.GROOM_BACKLOG
}


def needs_review(pr: object) -> bool:
    """True when never reviewed, or head has advanced past the reviewed SHA.

    Short-circuits when GitHub already shows the PR as APPROVED at the
    current head: no further code_review is needed, even if AgentShore
    hasn't recorded its own review (``last_reviewed_sha`` is None). Without
    this short-circuit, anti-confirmation can mask code_review entirely
    when every PR is APPROVED but the AgentShore-side review-state cache
    is empty (observed 2026-05-28 session 08a948ed: 5 PRs approved on
    GH, ``code_review`` masked "no eligible reviewer", ``merge_pr`` then
    sequentially the only viable path).
    """
    review_decision = getattr(pr, "review_decision", None)
    head_sha = getattr(pr, "head_sha", None)
    last_reviewed_sha = getattr(pr, "last_reviewed_sha", None)
    if review_decision == "APPROVED":
        # GH approval at any sha counts; if we know the heads disagree,
        # fall through to the usual heads-changed re-review path.
        if last_reviewed_sha is None or head_sha is None:
            return False
        return bool(head_sha != last_reviewed_sha)
    if last_reviewed_sha is None:
        return True
    if head_sha is None:
        return False
    return bool(head_sha != last_reviewed_sha)


def pr_is_approved(pr: object) -> bool:
    """True when GitHub or AgentShore has approved the current PR head."""
    if getattr(pr, "review_decision", None) == "APPROVED":
        return True
    return (
        getattr(pr, "last_review_status", None) == "PASS"
        and getattr(pr, "last_reviewed_sha", None) is not None
        and getattr(pr, "head_sha", None) is not None
        and getattr(pr, "last_reviewed_sha", None) == getattr(pr, "head_sha", None)
    )


def pr_is_awaiting_review(pr: object) -> bool:
    """True for open/review PRs without current-head approval."""
    return getattr(pr, "state", None) in ("open", "review") and not pr_is_approved(pr)
