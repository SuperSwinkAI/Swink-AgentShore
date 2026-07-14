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
# A JSON near-miss (#229): balanced object, but no top-level boolean ``success``
# (mirrors the live agy design_audit failure where prose bucket names displaced it).
NEAR_MISS = '{"artifacts": [{"type": "design_audit"}], "gap_filled": ["Distribution"]}'
# A clean-exit empty no-op: agy's empty task envelope already flattened to "".
NOOP = ""
# #313, session 16515f9b: the real agy issue_pickup tail after a 19-min (1158s) dispatch
# that emitted 22KB of prose and no envelope — a textbook async handoff.
ASYNC_HANDOFF_TAIL = (
    "I will run `cargo test` to execute the full test suite. I will check the status of "
    "the test suite. I will wait for cargo test to finish in the background. The system "
    "will notify me."
)
# #313 mode 1b: agy DID the work — committed, pushed the branch and opened real PR #200 —
# then reported it in a shape of its own invention instead of the required envelope, so
# the play scored FAILED and a merged-able PR was discarded.
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
    # The resume retry sends the short finalize nudge, NOT the full original prompt —
    # the agent already holds its prior context in-session. See #223 / _JSON_RETRY_PROMPT.
    from agentshore.plays.skill_backed.base import _JSON_RETRY_PROMPT

    assert second_call.args[1] == _JSON_RETRY_PROMPT
    assert outcome.dollar_cost == pytest.approx(0.02)
    # #232: the resume retry must NOT inherit a fresh-dispatch first-byte deadline
    # (1800s for agy) — it carries a short one-off override so a silent resume hang
    # fast-fails instead of riding 30 min.
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
    # #313: the retry cannot run without a resumable session, but the failure must
    # still say *why* rather than falling through to a bare parse error.
    assert "json retry skipped" in (outcome.error or "")
    assert "no resumable session id" in (outcome.error or "")


# --- #313 GAP 2: retry impossible (no session id) must still classify ----------


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
    # No resume was fabricated — exactly one dispatch.
    assert ctx.manager.dispatch.await_count == 1
    error = outcome.error or ""
    # The original parse error is preserved, and the diagnosis names the real defect.
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
    # It is NOT an async handoff — the classifier must not mislabel it.
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


# --- #313 GAP 1b: the missing-envelope nudge must restate the FULL envelope -----


def test_missing_envelope_prompt_names_the_required_envelope_keys() -> None:
    """The old wording ('re-emit the same JSON, add success, do not invent other keys')
    was calibrated for a one-field near-miss. Against agy's real mode — completed work
    reported as {"result": "completed", "pr": "<url>"} — it actively forbade the fix.
    """
    from agentshore.plays.skill_backed.base import _JSON_RETRY_MISSING_SUCCESS_PROMPT

    prompt = _JSON_RETRY_MISSING_SUCCESS_PROMPT

    # Names the envelope and its required top-level keys.
    assert "success" in prompt
    assert "artifacts" in prompt
    assert "boolean" in prompt
    assert "TOP level" in prompt or "top-level" in prompt
    # States the consequence, so a bare payload/array is not "close enough".
    assert "REJECTED" in prompt
    assert "bare array" in prompt
    # Still forbids redoing the work — the whole point is the work is already done.
    assert "Do not redo" in prompt
    # The counterproductive "do not invent other keys" instruction is gone.
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

    # A play with no artifact contract gets the generic envelope nudge unchanged.
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


# --- #236 async-handoff retry branch (previously untested at play level) --------


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
    # #236: None == inherit the full deadline; the work still has to be *done*.
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


# No-op retry (clean-exit empty output) — desktop no-op resilience


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
