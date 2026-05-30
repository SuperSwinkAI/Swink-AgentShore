"""Tests for the executor's PR-base self-heal (#8).

issue_pickup agents sometimes open PRs against the repo default branch instead
of the configured ``project.target_branch`` (the skill's ``$TARGET_BRANCH`` does
not survive across separate shell invocations). The executor retargets such PRs
before dispatching any PR-scoped play so merge_pr can merge and code_review
diffs the right base.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.config import AgentPreferencesConfig, RuntimeConfig, ScopeConfig
from agentshore.config.models import ProjectConfig
from agentshore.plays.base import PlayParams
from agentshore.plays.executor import PlayExecutor
from agentshore.state import (
    OrchestratorState,
    PlayType,
    PullRequestSnapshot,
    SessionState,
)


def _pr(pr_number: int, base_ref: str | None) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        pr_number=pr_number,
        title="t",
        state="open",
        branch="agentshore/9-x",
        issue_number=9,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
        base_ref=base_ref,
    )


def _executor(*, target_branch: str | None, github: MagicMock | None) -> PlayExecutor:
    return PlayExecutor(
        registry=MagicMock(),
        resolver=AsyncMock(),
        store=AsyncMock(),
        manager=MagicMock(),
        cfg=RuntimeConfig(
            scope=ScopeConfig(),
            agent_preferences=AgentPreferencesConfig(),
            project=ProjectConfig(target_branch=target_branch),
        ),
        project_path=Path("/tmp/project"),
        session_id="sess-test",
        github=github,
    )


def _state(pr: PullRequestSnapshot) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess-test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        agents=[],
        pull_requests=[pr],
    )


@pytest.mark.asyncio
async def test_retargets_wrong_base_to_target() -> None:
    gh = MagicMock()
    gh.retarget_pr_base = AsyncMock(return_value=True)
    ex = _executor(target_branch="integration", github=gh)
    await ex._maybe_retarget_pr_base(
        PlayType.MERGE_PR, PlayParams(pr_number=9), _state(_pr(9, "main"))
    )
    gh.retarget_pr_base.assert_awaited_once()
    args, kwargs = gh.retarget_pr_base.call_args
    assert args[0] == 9
    assert args[1] == "integration"


@pytest.mark.asyncio
async def test_noop_when_base_already_matches() -> None:
    gh = MagicMock()
    gh.retarget_pr_base = AsyncMock(return_value=True)
    ex = _executor(target_branch="integration", github=gh)
    await ex._maybe_retarget_pr_base(
        PlayType.MERGE_PR, PlayParams(pr_number=9), _state(_pr(9, "integration"))
    )
    gh.retarget_pr_base.assert_not_awaited()


@pytest.mark.asyncio
async def test_noop_when_no_target_configured() -> None:
    gh = MagicMock()
    gh.retarget_pr_base = AsyncMock(return_value=True)
    ex = _executor(target_branch=None, github=gh)
    await ex._maybe_retarget_pr_base(
        PlayType.MERGE_PR, PlayParams(pr_number=9), _state(_pr(9, "main"))
    )
    gh.retarget_pr_base.assert_not_awaited()


@pytest.mark.asyncio
async def test_noop_when_base_unknown() -> None:
    gh = MagicMock()
    gh.retarget_pr_base = AsyncMock(return_value=True)
    ex = _executor(target_branch="integration", github=gh)
    await ex._maybe_retarget_pr_base(
        PlayType.MERGE_PR, PlayParams(pr_number=9), _state(_pr(9, None))
    )
    gh.retarget_pr_base.assert_not_awaited()
