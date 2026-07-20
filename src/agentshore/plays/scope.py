"""Scope validation for issue inflation.

After each skill-backed play, ``validate_scope`` inspects the SkillResult:

- **Issue inflation**: if issues_created exceeds the expected count for this
  play type, raises ``IssueInflationDetected``.

Artifact drift detection used to rely on retired cluster path-prefix hints.
There is currently no reliable beads-native path boundary, so strict drift
blocking is intentionally not implemented. The drift tables remain available
as an evidence log for other consumers.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from agentshore.errors import IssueInflationDetected
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.config import ScopeConfig
    from agentshore.data.store import DataStore
    from agentshore.state import SkillResult

_PR_BODY_ISSUE_RE = re.compile(r"(?:Closes|Fixes|Resolves)\s+#(\d+)", re.IGNORECASE | re.MULTILINE)


# ---------------------------------------------------------------------------
# Expected issues-created per play type. Missing entry = 0 expected.
# ---------------------------------------------------------------------------

_EXPECTED_ISSUES: dict[PlayType, int | None] = {
    PlayType.SEED_PROJECT: None,  # beads seed creates as many as needed
    PlayType.GROOM_BACKLOG: None,  # backlog grooming may create/close issues
    # REFINE_TASK_BREAKDOWN is deliberately uncapped: decomposing one parent
    # into 3-5 children is its whole job. It also means every issue an audit
    # play files can fan out ~4x downstream, so the audit allowances below are
    # set with that multiplier in mind (a 5-issue QA run can become ~20-25
    # nodes once refinement runs).
    PlayType.REFINE_TASK_BREAKDOWN: None,
    # Issue-filing audit plays. These legitimately create issues, so a missing
    # entry (allowance 0) would trip on the very first finding. The effective
    # ceiling is ``expected * scope_cfg.issue_inflation_threshold`` (2.0 by
    # default), i.e. the numbers below double.
    #
    # RUN_QA / DESIGN_AUDIT (5 -> 10): whole-branch audits, so they surface the
    #   widest set of findings; the skills cluster by root cause and cap the
    #   per-run filing count, and 10 is well above a healthy run while still
    #   catching the 18-22 issue bursts seen in session 4f4596b2 (#368).
    # CODE_REVIEW (3 -> 6): scoped to a single PR's diff; follow-ups beyond a
    #   handful mean the review is filing style nits or re-filing known work.
    # CLEANUP (3 -> 6): files only for failures its auto-fixers could not fix,
    #   grouped by root cause, so a healthy run is 0-3.
    PlayType.RUN_QA: 5,
    PlayType.DESIGN_AUDIT: 5,
    PlayType.CODE_REVIEW: 3,
    PlayType.CLEANUP: 3,
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def validate_scope(
    *,
    skill_result: SkillResult,
    play_id: int,
    play_type: PlayType,
    session_id: str,
    scope_cfg: ScopeConfig,
    store: DataStore,
) -> None:
    """Check *skill_result* for issue inflation.

    Raises:
        IssueInflationDetected: issues_created exceeds the expected count.
    """
    _check_issue_inflation(skill_result, play_type, scope_cfg)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _check_issue_inflation(
    skill_result: SkillResult,
    play_type: PlayType,
    scope_cfg: ScopeConfig,
) -> None:
    expected = _EXPECTED_ISSUES.get(play_type, 0)
    if expected is None:
        return
    created = len(skill_result.issues_created)
    if created > expected * scope_cfg.issue_inflation_threshold:
        raise IssueInflationDetected(
            f"{play_type.value} created {created} issues "
            f"(expected {expected}, threshold ×{scope_cfg.issue_inflation_threshold})"
        )
