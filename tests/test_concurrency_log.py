"""Phase 1 fleet-concurrency log: the pure record builder and the NDJSON writer."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

from agentshore.core.concurrency_log import (
    CONCURRENCY_FILENAME,
    RECORD_VERSION,
    ConcurrencyLog,
    NullConcurrencyLog,
    build_concurrency_record,
)
from agentshore.errors import ErrorClass
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    PlayOutcome,
    PlayType,
)

if TYPE_CHECKING:
    from pathlib import Path


def _agent(
    agent_id: str,
    agent_type: AgentType,
    status: AgentStatus,
    *,
    model_tier: str | None = "medium",
    current_play_type: PlayType | None = None,
    last_error_class: ErrorClass | None = None,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=agent_type,
        status=status,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        model_tier=model_tier,
        current_play_type=current_play_type,
        last_error_class=last_error_class,
    )


def _state(agents: list[AgentSnapshot], *, total_plays: int = 1) -> SimpleNamespace:
    """Minimal stand-in for OrchestratorState — the writer only reads these two."""
    return SimpleNamespace(agents=agents, total_plays=total_plays)


def _outcome(agent_id: str | None, *, play_id: int | None = 7) -> PlayOutcome:
    return PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id=agent_id,
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=0,
        dollar_cost=0.0,
        artifacts=[],
        alignment_delta=0.0,
        play_id=play_id,
    )


def _record_for(agents: list[AgentSnapshot], outcome: PlayOutcome) -> dict[str, object]:
    return build_concurrency_record(
        agents=agents,
        total_plays=12,
        outcome=outcome,
        reward=0.5,
        seq=3,
        ts="2026-06-16T00:00:00+00:00",
        session_id="sess-1",
    )


def test_aggregates_split_busy_live_and_per_harness_and_tier() -> None:
    agents = [
        _agent("c1", AgentType.CLAUDE_CODE, AgentStatus.BUSY, model_tier="medium"),
        _agent("c2", AgentType.CLAUDE_CODE, AgentStatus.BUSY, model_tier="small"),
        _agent("c3", AgentType.CLAUDE_CODE, AgentStatus.IDLE),
        _agent("x1", AgentType.CODEX, AgentStatus.BUSY, model_tier="medium"),
        _agent("g1", AgentType.GROK, AgentStatus.ERROR),
        _agent("k1", AgentType.GROK, AgentStatus.TERMINATED),
    ]
    rec = _record_for(agents, _outcome("c1"))

    # busy = simultaneously dispatched (the rate-limit / RAM pressure metric).
    assert rec["busy_total"] == 3
    # live = idle + busy (excludes error/terminated).
    assert rec["live_total"] == 4
    assert rec["busy_by_type"] == {"claude_code": 2, "codex": 1}
    assert rec["live_by_type"] == {"claude_code": 3, "codex": 1}
    assert rec["busy_by_type_tier"] == {
        "claude_code/medium": 1,
        "claude_code/small": 1,
        "codex/medium": 1,
    }
    assert rec["status_totals"] == {"idle": 1, "busy": 3, "error": 1, "terminated": 1}


def test_roster_is_full_source_of_truth() -> None:
    agents = [
        _agent(
            "c1",
            AgentType.CLAUDE_CODE,
            AgentStatus.BUSY,
            current_play_type=PlayType.ISSUE_PICKUP,
        ),
        _agent("x1", AgentType.CODEX, AgentStatus.IDLE),
    ]
    rec = _record_for(agents, _outcome("c1"))
    roster = rec["roster"]
    assert isinstance(roster, list)
    assert len(roster) == 2
    assert roster[0] == {
        "agent_id": "c1",
        "agent_type": "claude_code",
        "model_tier": "medium",
        "status": "busy",
        "play_type": "issue_pickup",
        "error_class": None,
    }


def test_completed_play_context_and_error_class_on_same_line() -> None:
    agents = [
        _agent(
            "c1",
            AgentType.CLAUDE_CODE,
            AgentStatus.IDLE,  # returned to idle after the dispatch finished
            model_tier="large",
            last_error_class=ErrorClass.RATE_LIMIT,
        ),
    ]
    rec = _record_for(agents, _outcome("c1"))
    assert rec["completed_agent_id"] == "c1"
    assert rec["completed_agent_type"] == "claude_code"
    assert rec["completed_model_tier"] == "large"
    assert rec["completed_error_class"] == "rate_limit"
    assert rec["play_type"] == "issue_pickup"
    assert rec["reward"] == 0.5
    assert rec["v"] == RECORD_VERSION


def test_no_completed_agent_when_outcome_has_no_agent() -> None:
    rec = _record_for([_agent("c1", AgentType.CLAUDE_CODE, AgentStatus.IDLE)], _outcome(None))
    assert rec["completed_agent_id"] is None
    assert rec["completed_agent_type"] is None
    assert rec["completed_error_class"] is None


async def test_writer_appends_valid_ndjson_and_increments_seq(tmp_path: Path) -> None:
    log = ConcurrencyLog(tmp_path, "sess-9")
    assert log.path == tmp_path / CONCURRENCY_FILENAME

    agents = [_agent("c1", AgentType.CLAUDE_CODE, AgentStatus.BUSY)]
    await log.record(next_state=_state(agents, total_plays=1), outcome=_outcome("c1"), reward=0.1)
    await log.record(next_state=_state(agents, total_plays=2), outcome=_outcome("c1"), reward=0.2)

    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    # Monotonic per-session sequence + a real timestamp were stamped by the writer.
    assert first["seq"] == 1
    assert second["seq"] == 2
    assert first["session_id"] == "sess-9"
    assert first["busy_total"] == 1
    assert isinstance(first["ts"], str) and first["ts"]


async def test_writer_resets_prior_session_file(tmp_path: Path) -> None:
    stale = tmp_path / CONCURRENCY_FILENAME
    stale.write_text('{"v":0,"stale":true}\n', encoding="utf-8")

    log = ConcurrencyLog(tmp_path, "sess-new")
    await log.record(
        next_state=_state([_agent("c1", AgentType.CLAUDE_CODE, AgentStatus.BUSY)]),
        outcome=_outcome("c1"),
        reward=0.0,
    )
    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # stale line dropped on construction
    assert json.loads(lines[0])["session_id"] == "sess-new"


async def test_record_never_raises_on_bad_input(tmp_path: Path) -> None:
    log = ConcurrencyLog(tmp_path, "sess-x")
    # Missing `.agents` must warn, not propagate — the read happens inside the
    # guard (the "unguarded work as a call argument" crash, #experience-recorder).
    await log.record(next_state=SimpleNamespace(total_plays=0), outcome=_outcome("c1"), reward=0.0)  # type: ignore[arg-type]
    assert not log.path.exists() or log.path.read_text(encoding="utf-8") == ""


async def test_null_log_is_noop(tmp_path: Path) -> None:
    log = NullConcurrencyLog()
    await log.record(
        next_state=_state([_agent("c1", AgentType.CLAUDE_CODE, AgentStatus.BUSY)]),
        outcome=_outcome("c1"),
        reward=0.0,
    )
    assert list(tmp_path.iterdir()) == []
