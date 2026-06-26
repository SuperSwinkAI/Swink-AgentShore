"""Tests for the unblock_pr terminal-failure → manual-required fast path (#6).

A failure that names a human/CI-infra blocker can never be resolved by
re-dispatching an agent, so the PR must be marked manual-required on the FIRST
such failure rather than after the attempt-count exhaustion threshold (which let
the same permanently-blocked PR absorb three expensive dispatches). Transient
blockers must NOT match — they remain retryable.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.core.mixins.completion import (
    _UNBLOCK_MANUAL_REQUIRED_MARKERS,
    CompletionProcessor,
    _outcome_blocked_by_sibling_pr,
    _outcome_resolved_target_pr,
)
from agentshore.state import PlayType


def _is_terminal(error: str) -> bool:
    text = error.lower()
    return any(m in text for m in _UNBLOCK_MANUAL_REQUIRED_MARKERS)


@pytest.mark.parametrize(
    "error",
    [
        "ci-change requested but forbidden by skill policy",
        "Remaining CI blockers require human maintainer action: ...",
        "CI blocked by infrastructure failures not fixable in code: linux build",
        "External CI blockers remain and cannot be resolved from PR code",
        "all require CI config or infrastructure changes, forbidden by skill policy",
    ],
)
def test_terminal_failures_match(error: str) -> None:
    assert _is_terminal(error), error


@pytest.mark.parametrize(
    "error",
    [
        "ci_pending: CI checks still in progress after re-check",
        "ci_not_green",
        "merge_blocked",
        "wrong_base_branch",
        "rebase produced conflicts in src/lib.rs that need resolution",
    ],
)
def test_transient_failures_do_not_match(error: str) -> None:
    assert not _is_terminal(error), error


# ---------------------------------------------------------------------------
# blocked_by_pr: a target gated on an unmerged sibling PR must NOT be counted
# toward exhaustion or parked as manual-required (the stacked-PR trap fix).
# ---------------------------------------------------------------------------


def _outcome(
    artifacts: list[object], error: str | None = None, *, success: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(artifacts=artifacts, error=error, success=success)


def test_blocked_by_pr_artifact_detected() -> None:
    outcome = _outcome([{"type": "blocked_by_pr", "target": 40, "blocker": 99}])
    assert _outcome_blocked_by_sibling_pr(outcome) is True


def test_blocked_by_pr_absent_for_other_artifacts() -> None:
    assert _outcome_blocked_by_sibling_pr(_outcome([])) is False
    assert (
        _outcome_blocked_by_sibling_pr(_outcome([{"type": "pr_unblock_attempt", "number": 40}]))
        is False
    )
    # Non-dict artifacts (e.g. plain strings) must not crash the scan.
    assert _outcome_blocked_by_sibling_pr(_outcome(["done"])) is False


def _processor(*, record_returns: bool = False) -> tuple[CompletionProcessor, MagicMock]:
    """Build a bare CompletionProcessor with only the attributes
    ``_record_unblock_attempt_if_needed`` touches."""
    proc = object.__new__(CompletionProcessor)
    proc._session_id = "sess"  # type: ignore[attr-defined]
    resolver = MagicMock()
    resolver.record_unblock_pr_failure = MagicMock(return_value=record_returns)
    proc._executor = MagicMock()  # type: ignore[attr-defined]
    proc._executor._resolver = resolver
    proc._host = MagicMock()  # type: ignore[attr-defined]
    proc._host._safe_call = AsyncMock()
    # Shadow the coroutine method so mocked _safe_call gets a plain value, not a coroutine.
    proc.mark_pr_manual_required = MagicMock()  # type: ignore[attr-defined]
    return proc, resolver


def _ctx(pr_number: int = 40) -> SimpleNamespace:
    return SimpleNamespace(params=SimpleNamespace(pr_number=pr_number))


@pytest.mark.asyncio
async def test_blocked_by_sibling_skips_increment_and_park() -> None:
    proc, resolver = _processor()
    outcome = _outcome(
        [{"type": "blocked_by_pr", "target": 40, "blocker": 99}],
        error="PR #40 is blocked by unmerged sibling #99 (needs_unblock)",
    )

    await proc._record_unblock_attempt_if_needed(_ctx(), outcome, PlayType.UNBLOCK_PR)

    resolver.record_unblock_pr_failure.assert_not_called()
    proc._host._safe_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_three_blocked_by_sibling_never_park() -> None:
    """The headline trap fix: three sibling-blocked failures never reach
    exhaustion, so the target is never wrongly stamped manual-required."""
    proc, resolver = _processor()
    outcome = _outcome([{"type": "blocked_by_pr", "target": 40, "blocker": 99}])

    for _ in range(3):
        await proc._record_unblock_attempt_if_needed(_ctx(), outcome, PlayType.UNBLOCK_PR)

    resolver.record_unblock_pr_failure.assert_not_called()
    proc._host._safe_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_failure_still_increments() -> None:
    """Regression: a normal (non-sibling) failure still ticks the counter."""
    proc, resolver = _processor(record_returns=False)
    outcome = _outcome([], error="rebase produced conflicts in src/lib.rs")

    await proc._record_unblock_attempt_if_needed(_ctx(), outcome, PlayType.UNBLOCK_PR)

    resolver.record_unblock_pr_failure.assert_called_once_with(40)
    proc._host._safe_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_exhaustion_still_parks() -> None:
    """Regression: genuine exhaustion still parks the PR as manual-required."""
    proc, resolver = _processor(record_returns=True)
    outcome = _outcome([], error="generic unresolved failure")

    await proc._record_unblock_attempt_if_needed(_ctx(), outcome, PlayType.UNBLOCK_PR)

    resolver.record_unblock_pr_failure.assert_called_once_with(40)
    proc._host._safe_call.assert_awaited_once()
    proc.mark_pr_manual_required.assert_called_once_with(40)


# ---------------------------------------------------------------------------
# resolved-target: a successful unblock that merged the target or cleared its
# sole stale CHANGES_REQUESTED review is a win — never counted or parked
# (blocky PR #517: three successful stale-review short-circuits parked a
# merge-ready PR because every completion ticked the failure counter).
# ---------------------------------------------------------------------------


def test_resolved_target_detects_merged_and_stale_review() -> None:
    assert (
        _outcome_resolved_target_pr(_outcome([{"type": "pr_merged", "pr": 40}], success=True), 40)
        is True
    )
    assert (
        _outcome_resolved_target_pr(
            _outcome([{"type": "stale_review_state", "pr": 40}], success=True), 40
        )
        is True
    )
    # An artifact carrying no pr/number is treated as the dispatch target.
    assert _outcome_resolved_target_pr(_outcome([{"type": "pr_merged"}], success=True), 40) is True


def test_resolved_target_negatives() -> None:
    # success=False is never a resolution, even with a pr_merged artifact.
    assert (
        _outcome_resolved_target_pr(_outcome([{"type": "pr_merged", "pr": 40}], success=False), 40)
        is False
    )
    # A merged *sibling* (different pr number) does not resolve the target.
    assert (
        _outcome_resolved_target_pr(_outcome([{"type": "pr_merged", "pr": 99}], success=True), 40)
        is False
    )
    # Unrelated artifact types do not count; non-dict artifacts don't crash.
    assert (
        _outcome_resolved_target_pr(
            _outcome([{"type": "pr_unblock_attempt", "number": 40}, "noise"], success=True),
            40,
        )
        is False
    )


@pytest.mark.asyncio
async def test_resolved_target_skips_increment_and_resets_counter() -> None:
    """A successful merge of the target must not tick the counter — and must
    clear any prior failures so a later genuine block starts fresh."""
    proc, resolver = _processor()
    outcome = _outcome(
        [{"type": "pr_merged", "pr": 40, "reason": "target_merge_ready"}], success=True
    )

    await proc._record_unblock_attempt_if_needed(_ctx(), outcome, PlayType.UNBLOCK_PR)

    resolver.record_unblock_pr_failure.assert_not_called()
    resolver.reset_unblock_pr_failures.assert_called_once_with(40)
    proc._host._safe_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_three_stale_review_short_circuits_never_park() -> None:
    """The #517 trap: three successful stale-review short-circuits in a row
    must never reach exhaustion or stamp manual-required."""
    proc, resolver = _processor(record_returns=True)
    outcome = _outcome([{"type": "stale_review_state", "pr": 40, "head_sha": "abc"}], success=True)

    for _ in range(3):
        await proc._record_unblock_attempt_if_needed(_ctx(), outcome, PlayType.UNBLOCK_PR)

    resolver.record_unblock_pr_failure.assert_not_called()
    proc.mark_pr_manual_required.assert_not_called()
    proc._host._safe_call.assert_not_awaited()
