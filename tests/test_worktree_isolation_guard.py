"""Worktree-isolation guard for PR-scoped / branch-creating plays.

These plays' agents create/switch branches; that MUST happen in an allocated
worktree. If such a play is dispatched into the main checkout, the agent's
``git switch -c`` moves the main repo's HEAD onto a feature branch and wedges
the trunk-dispatch guard (the contamination behind the #175 wedge). The
skill-backed dispatcher hard-refuses an explicit main-checkout misroute and
emits telemetry for the ``None``-allocation fallback (which ``restore_default_branch``
now recovers).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.agents.handle import AgentInvocationResult
from agentshore.agents.worktree import TrunkAllocation
from agentshore.agents.worktree.manager import requires_isolated_worktree
from agentshore.plays.base import PlayParams
from agentshore.plays.skill_backed.issue_pickup import IssuePickupPlay
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    OrchestratorState,
    PlayType,
    SessionState,
)


def test_requires_isolated_worktree_covers_pr_and_branch_creating_plays() -> None:
    # PR-scoped + branch-creating plays MUST be isolated.
    assert requires_isolated_worktree(PlayType.CODE_REVIEW) is True
    assert requires_isolated_worktree(PlayType.UNBLOCK_PR) is True
    assert requires_isolated_worktree(PlayType.ISSUE_PICKUP) is True
    # Trunk-scoped plays run in the main checkout by design — not isolated.
    assert requires_isolated_worktree(PlayType.WRITE_IMPLEMENTATION_PLAN) is False
    assert requires_isolated_worktree(PlayType.REFINE_TASK_BREAKDOWN) is False
    assert requires_isolated_worktree(PlayType.RUN_QA) is False
    assert requires_isolated_worktree(PlayType.MERGE_PR) is False


def _ctx(project_path: Path) -> MagicMock:
    from agentshore.config import RuntimeConfig

    ctx = MagicMock()
    ctx.cfg = RuntimeConfig()
    ctx.manager = AsyncMock()
    ctx.manager.dispatch = AsyncMock(
        return_value=AgentInvocationResult(
            raw_output='{"success": true, "artifacts": []}',
            tokens_in=10,
            tokens_out=5,
            dollar_cost=0.01,
            duration_ms=100,
            exit_code=0,
            session_id="sess-1",
        )
    )
    ctx.store = AsyncMock()
    ctx.store.get_open_issues = AsyncMock(return_value=[])
    ctx.store.list_review_patterns = AsyncMock(return_value=[])
    ctx.project_path = project_path
    ctx.repo_root = project_path
    ctx.session_id = "sess"
    ctx.play_id = 1
    return ctx


def _state() -> OrchestratorState:
    return OrchestratorState(
        session_id="sess",
        session_state=SessionState.RUNNING,
        total_plays=1,
        total_cost=0.0,
        agents=[
            AgentSnapshot(
                agent_id="a1",
                agent_type=AgentType.CLAUDE_CODE,
                status=AgentStatus.IDLE,
                context_size=0,
                total_cost=0.0,
                total_tokens=0,
                tasks_completed=0,
                tasks_failed=0,
            )
        ],
    )


@pytest.mark.asyncio
async def test_isolation_play_misrouted_to_main_checkout_is_refused(tmp_path: Path) -> None:
    """A TrunkAllocation handed to issue_pickup must be refused before dispatch."""
    play = IssuePickupPlay()
    ctx = _ctx(tmp_path)
    # Misroute: an isolation-requiring play resolves to the main checkout.
    params = dataclasses.replace(
        PlayParams(issue_number=42, agent_id="a1"),
        _runtime_allocation=TrunkAllocation(path=tmp_path),
    )

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(_state(), params, ctx=ctx)

    assert outcome.success is False
    assert "main checkout" in (outcome.error or "")
    # The agent was never dispatched — no contamination of the main HEAD.
    ctx.manager.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_isolation_play_without_allocation_warns_but_proceeds(tmp_path: Path) -> None:
    """A None allocation (legacy fallback) is surfaced but not hard-failed."""
    play = IssuePickupPlay()
    ctx = _ctx(tmp_path)
    params = PlayParams(issue_number=42, agent_id="a1")  # no _runtime_allocation

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(_state(), params, ctx=ctx)

    # Proceeds to dispatch (the documented None=legacy fallback), does not refuse.
    assert outcome.success is True
    ctx.manager.dispatch.assert_awaited()
