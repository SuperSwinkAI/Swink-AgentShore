"""Tests for the unblock_pr terminal-failure → manual-required fast path (#6).

A failure that names a human/CI-infra blocker can never be resolved by
re-dispatching an agent, so the PR must be marked manual-required on the FIRST
such failure rather than after the attempt-count exhaustion threshold (which let
the same permanently-blocked PR absorb three expensive dispatches). Transient
blockers must NOT match — they remain retryable.
"""

from __future__ import annotations

import pytest

from agentshore.core.mixins.completion import _UNBLOCK_MANUAL_REQUIRED_MARKERS


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
