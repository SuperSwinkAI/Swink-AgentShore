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


def _invocation(
    *,
    raw_output: str,
    session_id: str | None = None,
    exit_code: int = 0,
    duration_ms: int = 1000,
):
    return AgentInvocationResult(
        raw_output=raw_output,
        tokens_in=100,
        tokens_out=50,
        dollar_cost=0.01,
        duration_ms=duration_ms,
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
NEAR_MISS = '{"artifacts": [{"type": "design_audit"}], "gap_filled": ["Distribution"]}'
NOOP = ""
ASYNC_HANDOFF_TAIL = (
    "I will run `cargo test` to execute the full test suite. I will check the status of "
    "the test suite. I will wait for cargo test to finish in the background. The system "
    "will notify me."
)
AD_HOC_ENVELOPE = (
    '{"result": "completed", "pr": "https://github.com/SuperSwinkAI/SuperSwink-Coding/pull/200"}'
)


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
    from agentshore.plays.skill_backed.base import _JSON_RETRY_PROMPT

    assert second_call.args[1] == _JSON_RETRY_PROMPT
    assert outcome.dollar_cost == pytest.approx(0.02)
    from agentshore.plays.skill_backed.base import _JSON_RETRY_FIRST_BYTE_S

    assert second_call.kwargs["first_byte_timeout_override"] == _JSON_RETRY_FIRST_BYTE_S


@pytest.mark.asyncio
async def test_missing_success_envelope_uses_targeted_nudge() -> None:
    """#229: a near-miss (JSON present, no boolean ``success``) gets the defect-specific
    nudge naming the missing field, not the generic 'emit the JSON block' prompt."""
    play = IssuePickupPlay()
    ctx = _ctx()
    state = _state()
    params = PlayParams(issue_number=42, agent_id="claude-1")

    first_result = _invocation(raw_output=NEAR_MISS, session_id="sess-nm")
    retry_result = _invocation(raw_output=VALID_JSON, session_id="sess-nm")
    ctx.manager.dispatch = AsyncMock(side_effect=[first_result, retry_result])

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(state, params, ctx=ctx)

    from agentshore.plays.skill_backed.base import (
        _JSON_RETRY_MISSING_SUCCESS_PROMPT,
        _JSON_RETRY_PROMPT,
    )

    assert outcome.success is True
    assert ctx.manager.dispatch.await_count == 2
    second_call = ctx.manager.dispatch.call_args_list[1]
    assert second_call.args[1] == _JSON_RETRY_MISSING_SUCCESS_PROMPT
    assert second_call.args[1] != _JSON_RETRY_PROMPT


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
    assert "json retry skipped" in (outcome.error or "")
    assert "no resumable session id" in (outcome.error or "")


@pytest.mark.asyncio
async def test_async_handoff_without_session_id_is_still_classified() -> None:
    """The single most expensive failure in session 16515f9b: agy ran issue_pickup for
    19 min, emitted 22KB of prose ending in a textbook async handoff, and its
    conversation id was unresolvable — so the retry branch (guarded on session_id)
    never fired and the play reported only a generic 'no valid result block'. Zero
    mitigation is unavoidable here; zero *classification* is not.
    """
    play = IssuePickupPlay()
    ctx = _ctx()
    state = _state_with_agent(AgentType.ANTIGRAVITY, "agy-1")
    params = PlayParams(issue_number=42, agent_id="agy-1")

    result = _invocation(raw_output=ASYNC_HANDOFF_TAIL, session_id=None)
    ctx.manager.dispatch = AsyncMock(return_value=result)

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(state, params, ctx=ctx)

    assert outcome.success is False
    assert ctx.manager.dispatch.await_count == 1
    error = outcome.error or ""
    assert "no valid result block" in error
    assert "async/background task" in error
    assert "#236" in error
    assert "json retry skipped" in error


@pytest.mark.asyncio
async def test_missing_envelope_without_session_id_is_classified_as_envelope_defect() -> None:
    """#313 mode 1b: agy finished the work (real PR #200) then reported it in an ad-hoc
    shape. With no session id the retry can't run, so the diagnosis must name the
    envelope defect rather than implying the agent did nothing."""
    play = IssuePickupPlay()
    ctx = _ctx()
    state = _state_with_agent(AgentType.ANTIGRAVITY, "agy-1")
    params = PlayParams(issue_number=42, agent_id="agy-1")

    result = _invocation(raw_output=AD_HOC_ENVELOPE, session_id=None)
    ctx.manager.dispatch = AsyncMock(return_value=result)

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(state, params, ctx=ctx)

    assert outcome.success is False
    error = outcome.error or ""
    assert "without the required result envelope" in error
    assert "json retry skipped" in error
    assert "async/background task" not in error


@pytest.mark.asyncio
async def test_no_classification_when_agent_produced_no_output() -> None:
    """An empty no-op is a different failure and must not pick up an envelope
    diagnosis — the streak/take_break path owns it."""
    play = IssuePickupPlay()
    ctx = _ctx()
    state = _state()
    params = PlayParams(issue_number=42, agent_id="claude-1")

    noop = _invocation(raw_output=NOOP, session_id=None)
    ctx.manager.dispatch = AsyncMock(side_effect=[noop, noop, noop])

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(state, params, ctx=ctx)

    assert "json retry skipped" not in (outcome.error or "")


def test_missing_envelope_prompt_names_the_required_envelope_keys() -> None:
    """The old wording ('re-emit the same JSON, add success, do not invent other keys')
    was calibrated for a one-field near-miss. Against agy's real mode — completed work
    reported as {"result": "completed", "pr": "<url>"} — it actively forbade the fix.
    """
    from agentshore.plays.skill_backed.base import _JSON_RETRY_MISSING_SUCCESS_PROMPT

    prompt = _JSON_RETRY_MISSING_SUCCESS_PROMPT

    assert "success" in prompt
    assert "artifacts" in prompt
    assert "boolean" in prompt
    assert "TOP level" in prompt or "top-level" in prompt
    assert "REJECTED" in prompt
    assert "bare array" in prompt
    assert "Do not redo" in prompt
    assert "Do not invent other keys" not in prompt


def test_missing_envelope_prompt_names_play_specific_artifact_types() -> None:
    """When a play declares required artifact types, the nudge restates the exact
    strings its validator matches on (#313 occurrence #3: a complete audit payload
    emitted under "design-audit-result")."""
    from agentshore.plays.skill_backed.base import (
        _JSON_RETRY_MISSING_SUCCESS_PROMPT,
        _missing_envelope_retry_prompt,
    )
    from agentshore.plays.skill_backed.design_audit import DesignAuditPlay

    prompt = _missing_envelope_retry_prompt(DesignAuditPlay().required_artifact_types)
    assert prompt.startswith(_JSON_RETRY_MISSING_SUCCESS_PROMPT)
    assert '"design_audit"' in prompt
    assert "exactly" in prompt

    assert _missing_envelope_retry_prompt(()) == _JSON_RETRY_MISSING_SUCCESS_PROMPT


@pytest.mark.asyncio
async def test_design_audit_retry_nudge_names_its_artifact_type() -> None:
    """End-to-end: the play-specific artifact type reaches the dispatched retry prompt."""
    from agentshore.plays.skill_backed.design_audit import DesignAuditPlay

    play = DesignAuditPlay()
    ctx = _ctx()
    state = _state()
    params = PlayParams(agent_id="claude-1")

    first = _invocation(raw_output=AD_HOC_ENVELOPE, session_id="sess-da")
    retry = _invocation(raw_output=VALID_JSON, session_id="sess-da")
    ctx.manager.dispatch = AsyncMock(side_effect=[first, retry])

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        await play.execute(state, params, ctx=ctx)

    assert ctx.manager.dispatch.await_count == 2
    assert '"design_audit"' in ctx.manager.dispatch.call_args_list[1].args[1]


@pytest.mark.asyncio
async def test_async_handoff_retry_uses_sync_prompt_and_full_first_byte_deadline() -> None:
    """An async handoff has UNFINISHED work, so the retry must (a) tell the agent to
    re-run synchronously rather than just re-print, and (b) keep the full per-agent-type
    first-byte deadline instead of the 120s re-emission fast-fail.
    """
    play = IssuePickupPlay()
    ctx = _ctx()
    state = _state_with_agent(AgentType.ANTIGRAVITY, "agy-1")
    params = PlayParams(issue_number=42, agent_id="agy-1")

    first = _invocation(raw_output=ASYNC_HANDOFF_TAIL, session_id="conv-uuid-1")
    retry = _invocation(raw_output=VALID_JSON, session_id="conv-uuid-1")
    ctx.manager.dispatch = AsyncMock(side_effect=[first, retry])

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(state, params, ctx=ctx)

    from agentshore.plays.skill_backed.base import (
        _JSON_RETRY_ASYNC_HANDOFF_PROMPT,
        _JSON_RETRY_MISSING_SUCCESS_PROMPT,
        _JSON_RETRY_PROMPT,
    )

    assert outcome.success is True
    assert ctx.manager.dispatch.await_count == 2
    second_call = ctx.manager.dispatch.call_args_list[1]
    assert second_call.args[1] == _JSON_RETRY_ASYNC_HANDOFF_PROMPT
    assert second_call.args[1] not in (_JSON_RETRY_PROMPT, _JSON_RETRY_MISSING_SUCCESS_PROMPT)
    assert second_call.kwargs["first_byte_timeout_override"] is None


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
        (AgentType.COPILOT, "copilot-1", "407397b3-e46e-4da3-9b4d-fa75a095eccc"),
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
    assert "resume_session_id" not in ctx.manager.dispatch.call_args_list[1].kwargs
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
    assert ctx.manager.dispatch.await_count == 3
    ctx.manager.mark_agent_error.assert_awaited_once()
    assert ctx.manager.mark_agent_error.await_args.args[1] == ErrorClass.NO_OP


@pytest.mark.asyncio
async def test_noop_retry_stops_early_when_time_budget_is_spent() -> None:
    """#416: a no-op that already burned the streak's whole time budget is not the
    fast backend flake this retry is for — re-running an identical prompt just
    repeats a deterministic failure at full cost, so no re-dispatch happens."""
    from agentshore.plays.skill_backed.base import _NOOP_RETRY_TIME_BUDGET_MS

    play = IssuePickupPlay()
    ctx = _ctx()
    state = _state()
    params = PlayParams(issue_number=42, agent_id="claude-1")

    slow_noop = _invocation(
        raw_output=NOOP,
        session_id=None,
        exit_code=0,
        duration_ms=_NOOP_RETRY_TIME_BUDGET_MS + 1,
    )
    ctx.manager.dispatch = AsyncMock(side_effect=[slow_noop, slow_noop, slow_noop])

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(state, params, ctx=ctx)

    from agentshore.errors import ErrorClass, FailureKind

    assert ctx.manager.dispatch.await_count == 1
    assert outcome.success is False
    assert outcome.failure_kind == FailureKind.AGENT_ERROR
    assert outcome.retry_requested is True
    assert "retry time budget" in (outcome.error or "")
    ctx.manager.mark_agent_error.assert_awaited_once()
    assert ctx.manager.mark_agent_error.await_args.args[1] == ErrorClass.NO_OP


@pytest.mark.asyncio
async def test_noop_retry_uses_full_streak_when_attempts_are_cheap() -> None:
    """#416 must not shorten the streak for fast no-ops — the budget is a ceiling on
    wasted wall-clock, not a replacement for the count-based bound."""
    from agentshore.plays.skill_backed.base import _NOOP_RETRY_TIME_BUDGET_MS

    play = IssuePickupPlay()
    ctx = _ctx()
    state = _state()
    params = PlayParams(issue_number=42, agent_id="claude-1")

    cheap = _invocation(
        raw_output=NOOP,
        session_id=None,
        exit_code=0,
        duration_ms=_NOOP_RETRY_TIME_BUDGET_MS // 10,
    )
    ctx.manager.dispatch = AsyncMock(side_effect=[cheap, cheap, cheap])

    with (
        patch("agentshore.plays.skill_backed.base.render_skill_prompt", return_value="prompt"),
        patch("agentshore.plays.skill_backed.base.write_play_context"),
    ):
        outcome = await play.execute(state, params, ctx=ctx)

    assert ctx.manager.dispatch.await_count == 3
    assert "retry time budget" not in (outcome.error or "")


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

    for call in ctx.manager.dispatch.call_args_list[1:]:
        assert call.kwargs.get("resume_session_id") is None
