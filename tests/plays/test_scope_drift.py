"""Scope validation — issue inflation and evidence-only artifact drift.

Path-prefix drift detection was removed with the cluster store. No reliable
beads-native path boundary exists yet, so validate_scope does not infer drift.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentshore.plays.scope import validate_scope
from agentshore.state import PlayType, SkillResult


def _mock_store() -> Any:
    """Build a minimal store mock for validate_scope tests."""
    store = AsyncMock()
    store.log_scope_drift = AsyncMock()
    return store


@pytest.mark.asyncio
async def test_no_drift_without_prefix_constraints(capsys: Any, caplog: Any) -> None:
    """No prefix constraints means no artifact drift row is logged."""
    from agentshore.config import RuntimeConfig

    cfg = RuntimeConfig()
    store = _mock_store()
    skill_result = SkillResult(
        success=True,
        artifacts=[{"path": "area/backend/foo.py"}],
    )

    await validate_scope(
        skill_result=skill_result,
        play_id=1,
        play_type=PlayType.ISSUE_PICKUP,
        session_id="sess",
        scope_cfg=cfg.scope,
        store=store,
    )

    store.log_scope_drift.assert_not_called()


@pytest.mark.asyncio
async def test_internal_plays_skip_scope_validation() -> None:
    """SEED_PROJECT may create as many issues as needed during seeding."""
    from agentshore.config import RuntimeConfig

    store = _mock_store()
    skill_result = SkillResult(
        success=False,
        artifacts=[{"path": "completely/unrelated.py"}],
        issues_created=[{"number": 1}, {"number": 2}, {"number": 3}],
    )
    cfg = RuntimeConfig()
    await validate_scope(
        skill_result=skill_result,
        play_id=99,
        play_type=PlayType.SEED_PROJECT,
        session_id="sess",
        scope_cfg=cfg.scope,
        store=store,
    )
    store.log_scope_drift.assert_not_called()


@pytest.mark.asyncio
async def test_no_drift_recorded_for_failed_play() -> None:
    """Failed plays also produce no drift without path boundaries."""
    from agentshore.config import RuntimeConfig

    store = _mock_store()
    skill_result = SkillResult(
        success=False,
        artifacts=[{"path": "area/backend/bar.py"}],
    )
    cfg = RuntimeConfig()

    await validate_scope(
        skill_result=skill_result,
        play_id=5,
        play_type=PlayType.CODE_REVIEW,
        session_id="sess",
        scope_cfg=cfg.scope,
        store=store,
    )

    store.log_scope_drift.assert_not_called()
