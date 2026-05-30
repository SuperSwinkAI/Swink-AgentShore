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
    PlayType.REFINE_TASK_BREAKDOWN: None,  # decomposition creates sub-issues
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
