"""Tests for scope validation after play execution."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agentshore.config import ScopeConfig
from agentshore.errors import IssueInflationDetected
from agentshore.plays.scope import validate_scope
from agentshore.state import PlayType, SkillResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_store() -> AsyncMock:
    store = AsyncMock()
    store.log_scope_drift = AsyncMock()
    return store


def _result(
    artifacts: list[object] | None = None,
    issues_created: list[object] | None = None,
) -> SkillResult:
    return SkillResult(
        success=True,
        artifacts=artifacts or [],
        issues_created=issues_created or [],
    )


def _cfg(strict: bool = False, threshold: float = 2.0) -> ScopeConfig:
    return ScopeConfig(strict_mode=strict, issue_inflation_threshold=threshold)


# ---------------------------------------------------------------------------
# Happy path — artifact drift is evidence-only until beads exposes path bounds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_drift_when_no_prefix_constraints() -> None:
    """No cluster prefix source exists, so artifacts do not create drift rows."""
    store = _make_store()
    result = _result(artifacts=["frontend/Button.tsx"])

    await validate_scope(
        skill_result=result,
        play_id=1,
        play_type=PlayType.ISSUE_PICKUP,
        session_id="sess-1",
        scope_cfg=_cfg(),
        store=store,
    )

    store.log_scope_drift.assert_not_called()


@pytest.mark.asyncio
async def test_no_drift_for_any_artifact() -> None:
    """Without reliable path constraints, arbitrary artifact paths pass."""
    store = _make_store()
    result = _result(artifacts=["some/random/file.py"])

    await validate_scope(
        skill_result=result,
        play_id=1,
        play_type=PlayType.ISSUE_PICKUP,
        session_id="sess-1",
        scope_cfg=_cfg(),
        store=store,
    )

    store.log_scope_drift.assert_not_called()


# ---------------------------------------------------------------------------
# Drift is not inferred by validate_scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_drift_logged_without_path_boundaries() -> None:
    """validate_scope does not infer artifact drift without path boundaries."""
    store = _make_store()
    result = _result(artifacts=["backend/api.py"])

    await validate_scope(
        skill_result=result,
        play_id=2,
        play_type=PlayType.ISSUE_PICKUP,
        session_id="sess-2",
        scope_cfg=_cfg(strict=False),
        store=store,
    )

    store.log_scope_drift.assert_not_called()


@pytest.mark.asyncio
async def test_strict_mode_no_raise_without_drift() -> None:
    """strict mode does not synthesize an approval without a drift source."""
    store = _make_store()
    result = _result(artifacts=["frontend/App.tsx"])

    # Should complete without raising (no drift to trigger it)
    await validate_scope(
        skill_result=result,
        play_id=4,
        play_type=PlayType.CODE_REVIEW,
        session_id="sess-4",
        scope_cfg=_cfg(strict=True),
        store=store,
    )

    store.log_scope_drift.assert_not_called()


# ---------------------------------------------------------------------------
# SEED_PROJECT — always in scope, unbounded issues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_project_always_in_scope_skips_path_check() -> None:
    store = _make_store()
    result = _result(artifacts=["docs/architecture.md"], issues_created=[1, 2, 3, 4, 5])

    await validate_scope(
        skill_result=result,
        play_id=5,
        play_type=PlayType.SEED_PROJECT,
        session_id="sess-5",
        scope_cfg=_cfg(strict=True),  # strict mode is irrelevant for issue inflation
        store=store,
    )

    store.log_scope_drift.assert_not_called()


# ---------------------------------------------------------------------------
# Issue inflation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_inflation_raises_when_exceeds_threshold() -> None:
    store = _make_store()
    # ISSUE_PICKUP expects 0 new issues; threshold=2.0 → >0 raises
    result = _result(issues_created=[10, 11])

    with pytest.raises(IssueInflationDetected):
        await validate_scope(
            skill_result=result,
            play_id=6,
            play_type=PlayType.ISSUE_PICKUP,
            session_id="sess-6",
            scope_cfg=_cfg(threshold=2.0),
            store=store,
        )


@pytest.mark.asyncio
async def test_refine_tasks_unbounded_issues_no_inflation() -> None:
    store = _make_store()
    # REFINE_TASK_BREAKDOWN is unbounded — many issues never trigger inflation
    result = _result(issues_created=list(range(20)))

    await validate_scope(
        skill_result=result,
        play_id=7,
        play_type=PlayType.REFINE_TASK_BREAKDOWN,
        session_id="sess-7",
        scope_cfg=_cfg(threshold=2.0),
        store=store,
    )
