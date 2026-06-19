"""Tests for the agent JSON retry path (desktop-dy2j)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.agents.handle import AgentInvocationResult
from agentshore.plays.base import PlayParams
from agentshore.plays.skill_backed.issue_pickup import IssuePickupPlay
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    OrchestratorState,
    SessionState,
)


def _invocation(*, raw_output: str, session_id: str | None = None, exit_code: int = 0):
    return AgentInvocationResult(
        raw_output=raw_output,
        tokens_in=100,
        tokens_out=50,
        dollar_cost=0.01,
        duration_ms=1000,
        exit_code=exit_code,
        session_id=session_id,
    )


def _ctx(tmp_path=None):
    from pathlib import Path

    from agentshore.config import RuntimeConfig

    cfg = RuntimeConfig()
    project_path = tmp_path or Path("/tmp/test-project")

    ctx = MagicMock()
    ctx.manager = AsyncMock()
    ctx.manager.dispatch = AsyncMock()
    ctx.store = AsyncMock()
    ctx.store.get_open_issues = AsyncMock(return_value=[])
    ctx.store.list_review_patterns = AsyncMock(return_value=[])
    ctx.cfg = cfg
    ctx.project_path = project_path
    ctx.repo_root = project_path
    ctx.session_id = "test-session"
    ctx.play_id = 1
    return ctx


def _state():
    return OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=5,
        total_cost=0.5,
        agents=[
            AgentSnapshot(
                agent_id="claude-1",
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


VALID_JSON = '{"success": true, "artifacts": []}'
NO_JSON = "I did the work but forgot to emit the JSON trailer."
# A clean-exit empty no-op: agy's empty task envelope already flattened to "".
NOOP = ""


@pytest.mark.asyncio
async def test_retry_recovers_on_missing_json() -> None:
    """Agent drops the trailer on first run, emits it on --resume retry."""
    play = IssuePickupPlay()
    ctx = _ctx()
    state = _state()
    params = PlayParams(issue_number=42, agent_id="claude-1")

    first_result = _invocation(raw_output=NO_JSON, session_id="sess-abc123")
    retry_result = _invocation(raw_output=VALID_JSON, session_id="sess-abc123")
    ctx.manager.dispatch = AsyncMock(side_effect=[first_result, retry_result])

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(state, params, ctx=ctx)

    assert outcome.success is True
    assert ctx.manager.dispatch.await_count == 2
    second_call = ctx.manager.dispatch.call_args_list[1]
    assert second_call.kwargs["resume_session_id"] == "sess-abc123"
    # The resume retry sends the short finalize nudge, NOT the full original prompt —
    # the agent already holds its prior context in-session. See #223 / _JSON_RETRY_PROMPT.
    from agentshore.plays.skill_backed.base import _JSON_RETRY_PROMPT

    assert second_call.args[1] == _JSON_RETRY_PROMPT
    assert outcome.dollar_cost == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_retry_fails_both_times_reports_single_failure() -> None:
    """If retry also misses the trailer, failure is reported exactly once."""
    play = IssuePickupPlay()
    ctx = _ctx()
    state = _state()
    params = PlayParams(issue_number=42, agent_id="claude-1")

    first_result = _invocation(raw_output=NO_JSON, session_id="sess-abc123")
    retry_result = _invocation(raw_output="Still no JSON here either.", session_id="sess-abc123")
    ctx.manager.dispatch = AsyncMock(side_effect=[first_result, retry_result])

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(state, params, ctx=ctx)

    assert outcome.success is False
    assert "no valid result block" in (outcome.error or "")
    assert ctx.manager.dispatch.await_count == 2


@pytest.mark.asyncio
async def test_no_retry_when_session_id_unavailable() -> None:
    """No retry is attempted when the agent didn't report a session id."""
    play = IssuePickupPlay()
    ctx = _ctx()
    state = _state()
    params = PlayParams(issue_number=42, agent_id="claude-1")

    result = _invocation(raw_output=NO_JSON, session_id=None)
    ctx.manager.dispatch = AsyncMock(return_value=result)

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(state, params, ctx=ctx)

    assert outcome.success is False
    assert ctx.manager.dispatch.await_count == 1


@pytest.mark.asyncio
async def test_retry_on_killed_exit_with_session() -> None:
    """A post-response idle kill (non-zero / None exit) still retries via --resume.

    exit_code no longer gates the JSON retry: a resumable session id plus a
    salvaged non-empty output are the only prerequisites, so the common
    "agent emitted a partial line then stalled and got killed" case recovers.
    """
    play = IssuePickupPlay()
    ctx = _ctx()
    state = _state()
    params = PlayParams(issue_number=42, agent_id="claude-1")

    first_result = _invocation(raw_output=NO_JSON, session_id="sess-abc", exit_code=1)
    retry_result = _invocation(raw_output=VALID_JSON, session_id="sess-abc", exit_code=1)
    ctx.manager.dispatch = AsyncMock(side_effect=[first_result, retry_result])

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(state, params, ctx=ctx)

    assert outcome.success is True
    assert ctx.manager.dispatch.await_count == 2
    assert ctx.manager.dispatch.call_args_list[1].kwargs["resume_session_id"] == "sess-abc"


def _state_with_agent(agent_type: AgentType, agent_id: str) -> OrchestratorState:
    return OrchestratorState(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=5,
        total_cost=0.5,
        agents=[
            AgentSnapshot(
                agent_id=agent_id,
                agent_type=agent_type,
                status=AgentStatus.IDLE,
                context_size=0,
                total_cost=0.0,
                total_tokens=0,
                tasks_completed=0,
                tasks_failed=0,
            )
        ],
    )


@pytest.mark.parametrize(
    ("agent_type", "agent_id", "session_id"),
    [
        (AgentType.CODEX, "codex-1", "thread_x"),
        (AgentType.GROK, "grok-1", "grok-sess"),
        (AgentType.ANTIGRAVITY, "agy-1", "conv-uuid-42"),
    ],
)
@pytest.mark.asyncio
async def test_retry_recovers_for_non_claude_agents(
    agent_type: AgentType, agent_id: str, session_id: str
) -> None:
    """The JSON retry now recovers for codex/grok/antigravity, not just claude.

    The base flow re-dispatches with the agent's session id regardless of type;
    the per-agent resume *argv* shape (codex ``exec resume``, grok ``-r``, agy
    ``--conversation``) and agy's id resolution are asserted in test_cli_agent.py
    / test_cli_antigravity.py.
    """
    play = IssuePickupPlay()
    ctx = _ctx()
    state = _state_with_agent(agent_type, agent_id)
    params = PlayParams(issue_number=42, agent_id=agent_id)

    first_result = _invocation(raw_output=NO_JSON, session_id=session_id)
    retry_result = _invocation(raw_output=VALID_JSON, session_id=session_id)
    ctx.manager.dispatch = AsyncMock(side_effect=[first_result, retry_result])

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(state, params, ctx=ctx)

    assert outcome.success is True
    assert ctx.manager.dispatch.await_count == 2
    assert ctx.manager.dispatch.call_args_list[1].kwargs["resume_session_id"] == session_id


# ---------------------------------------------------------------------------
# No-op retry (clean-exit empty output) — desktop no-op resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noop_retry_recovers_on_second_attempt() -> None:
    """A clean-exit empty no-op re-dispatches FRESH and recovers on output."""
    play = IssuePickupPlay()
    ctx = _ctx()
    state = _state()
    params = PlayParams(issue_number=42, agent_id="claude-1")

    noop = _invocation(raw_output=NOOP, session_id="sess-x", exit_code=0)
    recovered = _invocation(raw_output=VALID_JSON, session_id="sess-x", exit_code=0)
    ctx.manager.dispatch = AsyncMock(side_effect=[noop, recovered])

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(state, params, ctx=ctx)

    assert outcome.success is True
    assert ctx.manager.dispatch.await_count == 2
    # The no-op retry is FRESH — it must NOT pass resume_session_id (an empty agy
    # session resumes empty; only a fresh turn can recover).
    assert "resume_session_id" not in ctx.manager.dispatch.call_args_list[1].kwargs
    # Recovered before the streak limit → no take_break trigger.
    ctx.manager.mark_agent_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_noop_retry_fails_after_three_and_triggers_break() -> None:
    """Three consecutive no-ops fail the play and route the agent to take_break."""
    play = IssuePickupPlay()
    ctx = _ctx()
    state = _state()
    params = PlayParams(issue_number=42, agent_id="claude-1")

    noop = _invocation(raw_output=NOOP, session_id=None, exit_code=0)
    ctx.manager.dispatch = AsyncMock(side_effect=[noop, noop, noop])

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(state, params, ctx=ctx)

    from agentshore.errors import ErrorClass, FailureKind

    assert outcome.success is False
    assert outcome.failure_kind == FailureKind.AGENT_ERROR
    assert outcome.retry_requested is True
    assert "no output" in (outcome.error or "")
    # 1 initial + 2 fresh re-dispatches == the 3-in-a-row streak limit.
    assert ctx.manager.dispatch.await_count == 3
    # The agent is routed into the standard take_break via a recoverable NO_OP.
    ctx.manager.mark_agent_error.assert_awaited_once()
    assert ctx.manager.mark_agent_error.await_args.args[1] == ErrorClass.NO_OP


@pytest.mark.asyncio
async def test_noop_does_not_resume_even_with_session_id() -> None:
    """A no-op never resumes — distinct from the output-but-no-JSON path."""
    play = IssuePickupPlay()
    ctx = _ctx()
    state = _state()
    params = PlayParams(issue_number=42, agent_id="claude-1")

    noop = _invocation(raw_output=NOOP, session_id="sess-present", exit_code=0)
    ctx.manager.dispatch = AsyncMock(side_effect=[noop, noop, noop])

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        await play.execute(state, params, ctx=ctx)

    # Even though a session id is present, the no-op path re-dispatches fresh.
    for call in ctx.manager.dispatch.call_args_list[1:]:
        assert call.kwargs.get("resume_session_id") is None
