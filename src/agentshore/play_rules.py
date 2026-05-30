"""Shared play eligibility rules used outside individual play classes."""

from __future__ import annotations

SEED_PROJECT_COOLDOWN_PLAYS = 50
DESIGN_AUDIT_COOLDOWN_PLAYS = 20
TERMINAL_SHUTDOWN_EVIDENCE_WINDOW_PLAYS = 50


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
