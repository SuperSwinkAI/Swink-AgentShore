"""Scope validation for issue inflation.

After each skill-backed play, ``validate_scope`` inspects the SkillResult:

- **Issue inflation**: if issues_created exceeds the expected count for this
  play type, raises ``IssueInflationDetected``.

Strict drift blocking is not implemented. The drift tables remain available
as an evidence log for other consumers."""

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


_EXPECTED_ISSUES: dict[PlayType, int | None] = {
    PlayType.SEED_PROJECT: None,
    PlayType.GROOM_BACKLOG: None,
    PlayType.REFINE_TASK_BREAKDOWN: None,
    PlayType.RUN_QA: 5,
    PlayType.DESIGN_AUDIT: 5,
    PlayType.CODE_REVIEW: 3,
    PlayType.CLEANUP: 3,
    PlayType.WRITE_IMPLEMENTATION_PLAN: 1,
    PlayType.SYSTEMATIC_DEBUGGING: 1,
}


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
