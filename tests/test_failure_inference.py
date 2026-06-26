"""Coverage for the executor's substring failure-category inferer (Phase 4)."""

from __future__ import annotations

from agentshore.plays.execution.failure import _infer_failure_category
from agentshore.state import PlayOutcome, PlayType


def _outcome(error: str) -> PlayOutcome:
    return PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id="agent-1",
        success=False,
        partial=False,
        duration_seconds=1.0,
        token_cost=0,
        dollar_cost=0.0,
        artifacts=[],
        alignment_delta=0.0,
        error=error,
    )


def test_github_table_auth_spelling_is_agent_error() -> None:
    # Phase 4: inferer uses canonical AUTH_MARKERS superset, catching GitHub-table
    # auth spellings the narrow publish subset missed.
    assert _infer_failure_category(_outcome("fatal: repository not found")) == "agent_error"
    assert _infer_failure_category(_outcome("HTTP 401 Unauthorized")) == "agent_error"


def test_bare_auth_substring_no_longer_false_matches() -> None:
    # Phase 4 dropped the bare ``"auth" in error`` fallback that false-matched
    # "author"/"authorization" → no real auth marker, so these go code_error not agent_error.
    assert _infer_failure_category(_outcome("the author of the PR disagrees")) == "code_error"
    assert _infer_failure_category(_outcome("missing authorization header in handler")) == (
        "code_error"
    )


def test_known_auth_marker_still_classifies() -> None:
    assert _infer_failure_category(_outcome("bad credentials")) == "agent_error"


def test_typed_failure_kind_still_wins_over_substrings() -> None:
    from agentshore.errors import FailureKind

    outcome = PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id="agent-1",
        success=False,
        partial=False,
        duration_seconds=1.0,
        token_cost=0,
        dollar_cost=0.0,
        artifacts=[],
        alignment_delta=0.0,
        error="the author of the PR",
        failure_kind=FailureKind.TEST,
    )
    assert _infer_failure_category(outcome) == "test_failure"
