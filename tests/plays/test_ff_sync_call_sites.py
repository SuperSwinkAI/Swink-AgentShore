"""Wiring tests: merge_pr and reconcile_state invoke the post-merge ff-sync.

The fast-forward logic itself is covered in ``tests/test_branch_sync.py``;
these assert only that each call site fires the helper with the configured
target branch when one is set (threading the resolved git-auth fetch overlay,
#178), and skips it when ``target_branch`` is unset.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.plays.base import PlayParams
from agentshore.plays.skill_backed.merge_pr import MergePRPlay
from agentshore.plays.skill_backed.reconcile_state import ReconcileStatePlay
from agentshore.state import AgentStatus, PlayOutcome, PlayType, SkillResult


def _ctx(target_branch: str | None) -> Any:
    ctx = MagicMock()
    ctx.session_id = "s"
    ctx.play_id = 1
    ctx.store = AsyncMock()
    ctx.cfg.project.target_branch = target_branch
    ctx.project_path = MagicMock()
    return ctx


def _state() -> Any:
    state = MagicMock()
    agent = MagicMock()
    agent.agent_id = "agent-1"
    agent.status = AgentStatus.IDLE
    state.agents = [agent]
    state.graph = None
    return state


def _outcome(play_type: PlayType) -> PlayOutcome:
    return PlayOutcome(
        play_type=play_type,
        agent_id="agent-1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=10,
        dollar_cost=0.01,
        artifacts=[],
        alignment_delta=0.0,
    )


_OVERLAY = {"GIT_CONFIG_COUNT": "3"}


@pytest.mark.asyncio
async def test_merge_pr_ff_syncs_when_target_set() -> None:
    play = MergePRPlay()
    ctx = _ctx("integration")

    async def _super(*_a: object, **_k: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=True, issues_closed=[])
        return _outcome(PlayType.MERGE_PR)

    spy = AsyncMock()
    with (
        patch("agentshore.plays.skill_backed.base.SkillBackedPlay.execute", new=_super),
        patch("agentshore.plays.skill_backed.merge_pr.fast_forward_local_branch", spy),
        patch(
            "agentshore.plays.skill_backed._merge_reconcile.resolve_ff_fetch_overlay",
            return_value=_OVERLAY,
        ),
        patch("agentshore.plays.skill_backed.merge_pr._fetch_pr_links", AsyncMock(return_value=[])),
        patch(
            "agentshore.plays.skill_backed.merge_pr._fetch_pr_body", AsyncMock(return_value=None)
        ),
    ):
        await play.execute(_state(), PlayParams(agent_id="agent-1", pr_number=42), ctx=ctx)

    spy.assert_awaited_once_with(ctx.project_path, "integration", fetch_env_overlay=_OVERLAY)


@pytest.mark.asyncio
async def test_merge_pr_skips_ff_when_target_unset() -> None:
    play = MergePRPlay()
    ctx = _ctx(None)

    async def _super(*_a: object, **_k: object) -> PlayOutcome:
        play._last_skill_result = SkillResult(success=True, issues_closed=[])
        return _outcome(PlayType.MERGE_PR)

    spy = AsyncMock()
    with (
        patch("agentshore.plays.skill_backed.base.SkillBackedPlay.execute", new=_super),
        patch("agentshore.plays.skill_backed.merge_pr.fast_forward_local_branch", spy),
        patch("agentshore.plays.skill_backed.merge_pr._fetch_pr_links", AsyncMock(return_value=[])),
        patch(
            "agentshore.plays.skill_backed.merge_pr._fetch_pr_body", AsyncMock(return_value=None)
        ),
    ):
        await play.execute(_state(), PlayParams(agent_id="agent-1", pr_number=42), ctx=ctx)

    spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_ff_syncs_when_target_set() -> None:
    play = ReconcileStatePlay()
    ctx = _ctx("integration")

    async def _super(*_a: object, **_k: object) -> PlayOutcome:
        return _outcome(PlayType.RECONCILE_STATE)

    spy = AsyncMock()
    with (
        patch("agentshore.plays.skill_backed.base.SkillBackedPlay.execute", new=_super),
        patch("agentshore.plays.skill_backed.reconcile_state.fast_forward_local_branch", spy),
        patch(
            "agentshore.plays.skill_backed.reconcile_state.resolve_ff_fetch_overlay",
            return_value=_OVERLAY,
        ),
    ):
        await play.execute(_state(), PlayParams(agent_id="agent-1"), ctx=ctx)

    spy.assert_awaited_once_with(ctx.project_path, "integration", fetch_env_overlay=_OVERLAY)


@pytest.mark.asyncio
async def test_reconcile_skips_ff_when_target_unset() -> None:
    play = ReconcileStatePlay()
    ctx = _ctx(None)

    async def _super(*_a: object, **_k: object) -> PlayOutcome:
        return _outcome(PlayType.RECONCILE_STATE)

    spy = AsyncMock()
    with (
        patch("agentshore.plays.skill_backed.base.SkillBackedPlay.execute", new=_super),
        patch("agentshore.plays.skill_backed.reconcile_state.fast_forward_local_branch", spy),
    ):
        await play.execute(_state(), PlayParams(agent_id="agent-1"), ctx=ctx)

    spy.assert_not_awaited()
