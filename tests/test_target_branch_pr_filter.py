"""Piece C: target-branch PR filter (issue #60 follow-on).

When a project explicitly configures ``project.target_branch``, open PRs whose
base branch differs from it are out of scope for the session and must be dropped
from ``state.pull_requests`` — the single collection that feeds the dashboard,
the candidate pool, and backpressure. Dropped PRs are counted (for the
dashboard "(N hidden)" badge) and logged via ``github_pr_ignored``.

These tests exercise ``StateBuilder._filter_pull_requests_to_target`` directly:
it is a pure static method, so no DB / git fixtures are needed.
"""

from __future__ import annotations

import structlog

from agentshore.core.mixins.state import StateBuilder
from agentshore.state import PullRequestSnapshot


def _pr(pr_number: int, base_ref: str | None) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        pr_number=pr_number,
        title=f"PR {pr_number}",
        state="OPEN",
        branch=f"feature/{pr_number}",
        issue_number=None,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
        base_ref=base_ref,
    )


def test_drops_wrong_base_keeps_on_target_and_unknown() -> None:
    prs = [
        _pr(96, "integration"),  # on target -> kept
        _pr(62, "main"),  # wrong base -> dropped
        _pr(38, "main"),  # wrong base -> dropped
        _pr(10, None),  # unknown base -> kept (no hiding on missing data)
    ]
    kept, hidden = StateBuilder._filter_pull_requests_to_target(prs, "integration")
    assert hidden == 2
    assert [pr.pr_number for pr in kept] == [96, 10]


def test_noop_when_target_branch_unset() -> None:
    prs = [_pr(62, "main"), _pr(96, "integration")]
    kept, hidden = StateBuilder._filter_pull_requests_to_target(prs, None)
    assert hidden == 0
    assert [pr.pr_number for pr in kept] == [62, 96]


def test_empty_string_base_is_kept() -> None:
    # An empty (not just None) base_ref is "unknown", not "wrong" — keep it.
    kept, hidden = StateBuilder._filter_pull_requests_to_target([_pr(7, "")], "integration")
    assert hidden == 0
    assert [pr.pr_number for pr in kept] == [7]


def test_emits_github_pr_ignored_per_dropped_pr() -> None:
    prs = [_pr(96, "integration"), _pr(62, "main")]
    with structlog.testing.capture_logs() as captured:
        _kept, hidden = StateBuilder._filter_pull_requests_to_target(prs, "integration")
    assert hidden == 1
    ignored = [e for e in captured if e.get("event") == "github_pr_ignored"]
    assert len(ignored) == 1
    assert ignored[0]["reason"] == "wrong_base_branch"
    assert ignored[0]["pr_number"] == 62
    assert ignored[0]["base_ref"] == "main"
    assert ignored[0]["target_branch"] == "integration"
