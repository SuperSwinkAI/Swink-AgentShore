"""Tests for rl/metrics.py — ObservationContext computation from play history."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentshore.data.models import GitHubIssueRecord
from agentshore.rl.constants import STAGNATION_ENTROPY_MULTIPLIER
from agentshore.rl.metrics import MetricsEngine, _build_context, compute_agent_specialization
from agentshore.state import OrchestratorState, PlayType, SessionState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(offset_seconds: float = 0.0) -> str:
    """Return an ISO-8601 timestamp `offset_seconds` from now (UTC).

    Tests that depend on relative play ordering pass negative offsets so that
    the resulting times remain in the past relative to test wall-clock.
    """
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).isoformat(timespec="seconds")


@dataclass
class _FakePlay:
    play_type: str
    success: bool
    # NOTE: started_at gets a freshly-evaluated default *per instance* via
    # __post_init__ below — using a literal here would freeze a single
    # timestamp at module import time and re-introduce the staleness problem
    # this refactor was meant to fix.
    started_at: str = ""
    ended_at: str | None = None
    duration_ms: int | None = None
    dollar_cost: float = 0.0
    alignment_before: float | None = None
    alignment_after: float | None = None
    alignment_delta: float | None = None
    agent_id: str | None = None
    artifacts: list[object] | None = None

    def __post_init__(self) -> None:
        if self.started_at == "":
            self.started_at = _ts()
        if self.artifacts is None:
            self.artifacts = []


def _state(**kwargs: Any) -> OrchestratorState:
    base: dict[str, Any] = dict(
        session_id="s",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
    )
    base.update(kwargs)
    return OrchestratorState(**base)


def _ctx(history: list[_FakePlay], *, stagnation_warn_after: int = 5, **state_kwargs: Any) -> Any:
    return _build_context(
        _state(**state_kwargs),
        history,  # type: ignore[arg-type]
        prs_open=[],
        prs_approved=[],
        learnings=[],
        stagnation_warn_after=stagnation_warn_after,
    )


# ---------------------------------------------------------------------------
# Rolling success rate
# ---------------------------------------------------------------------------


def test_rolling_success_rate_all_pass():
    history = [_FakePlay("issue_pickup", success=True) for _ in range(5)]
    ctx = _ctx(history)
    assert ctx.rolling_success_rate == pytest.approx(1.0, abs=1e-5)


def test_rolling_success_rate_mixed():
    history = [
        _FakePlay("issue_pickup", success=True),
        _FakePlay("issue_pickup", success=True),
        _FakePlay("code_review", success=False),
        _FakePlay("code_review", success=False),
    ]
    ctx = _ctx(history)
    assert ctx.rolling_success_rate == pytest.approx(0.5, abs=1e-5)


def test_rolling_success_rate_all_fail():
    history = [_FakePlay("code_review", success=False) for _ in range(4)]
    ctx = _ctx(history)
    assert ctx.rolling_success_rate == pytest.approx(0.0, abs=1e-5)


def test_rolling_success_empty_history():
    ctx = _ctx([])
    assert ctx.rolling_success_rate == pytest.approx(0.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Rolling window (last 10 plays)
# ---------------------------------------------------------------------------


def test_rolling_window_uses_last_10():
    # 5 fails then 10 passes — rolling window uses only last 10
    fails = [_FakePlay("code_review", success=False) for _ in range(5)]
    passes = [_FakePlay("issue_pickup", success=True) for _ in range(10)]
    ctx = _ctx(fails + passes)
    assert ctx.rolling_success_rate == pytest.approx(1.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Last play types and success flags
# ---------------------------------------------------------------------------


def test_last_play_types_ordering():
    history = [
        _FakePlay("issue_pickup", success=True),
        _FakePlay("code_review", success=False),
    ]
    ctx = _ctx(history)
    # last_play_types length 5, oldest→newest, None-padded left
    types = ctx.last_play_types
    assert len(types) == 5
    # First 3 should be None (only 2 plays)
    assert types[0] is None
    assert types[1] is None
    assert types[2] is None
    assert types[3] == PlayType.ISSUE_PICKUP
    assert types[4] == PlayType.CODE_REVIEW


def test_last_play_success_flags():
    history = [
        _FakePlay("issue_pickup", success=True),
        _FakePlay("code_review", success=False),
    ]
    ctx = _ctx(history)
    flags = ctx.last_play_success
    assert flags[3] is True
    assert flags[4] is False


def test_no_history_all_none():
    ctx = _ctx([])
    assert all(t is None for t in ctx.last_play_types)
    assert all(f is None for f in ctx.last_play_success)


# ---------------------------------------------------------------------------
# Stagnation counter — whole minutes that all agents have been idle.
# ---------------------------------------------------------------------------


def _busy_agent(agent_id: str = "a-busy") -> Any:
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.BUSY,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )


def _idle_agent(agent_id: str = "a-idle") -> Any:
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )


def test_stagnation_zero_when_any_agent_busy():
    history = [_FakePlay("issue_pickup", success=True, ended_at=_ts(-600))]
    ctx = _ctx(history, agents=[_idle_agent("a"), _busy_agent("b")])
    assert ctx.stagnation_counter == 0


def test_stagnation_zero_when_no_history():
    ctx = _ctx([], agents=[_idle_agent()])
    assert ctx.stagnation_counter == 0


def test_stagnation_counts_minutes_since_last_play_ended_when_all_idle():
    # Most recent play ended ~3 minutes ago.
    history = [
        _FakePlay("issue_pickup", success=True, ended_at=_ts(-600)),
        _FakePlay("issue_pickup", success=True, ended_at=_ts(-185)),
    ]
    ctx = _ctx(history, agents=[_idle_agent()])
    assert ctx.stagnation_counter == 3


def test_stagnation_floors_under_one_minute_to_zero():
    history = [_FakePlay("issue_pickup", success=True, ended_at=_ts(-30))]
    ctx = _ctx(history, agents=[_idle_agent()])
    assert ctx.stagnation_counter == 0


def test_merge_pr_empty_pr_merged_issue_numbers_artifact_does_not_count_throughput():
    # A merge_pr that explicitly closed zero issues (e.g. doc-only PR) must
    # not inflate issues_closed_this_session or issue_churn_rate.
    history = [
        _FakePlay(
            "merge_pr",
            success=True,
            alignment_delta=0.0,
            artifacts=[
                {"type": "pr_merged_issue_numbers", "pr": 42, "issue_numbers": []},
            ],
        ),
        _FakePlay("code_review", success=True, alignment_delta=0.0),
    ]
    ctx = _ctx(history)
    assert ctx.issues_closed_this_session == 0
    assert ctx.issue_churn_rate == pytest.approx(0.0, abs=1e-5)


def test_merge_pr_non_empty_pr_merged_issue_numbers_artifact_counts_throughput():
    # A merge_pr with a non-empty validated issue list still counts as
    # issue-throughput.
    history = [
        _FakePlay(
            "merge_pr",
            success=True,
            alignment_delta=0.3,
            artifacts=[
                {"type": "pr_merged_issue_numbers", "pr": 42, "issue_numbers": [17, 23]},
            ],
        ),
    ]
    ctx = _ctx(history)
    assert ctx.issues_closed_this_session == 1
    assert ctx.stagnation_counter == 0


def test_merge_pr_without_closure_artifact_keeps_legacy_throughput():
    # Historical merge_pr rows persisted before the artifact existed must
    # continue to count, so older sessions retain their metrics.
    history = [
        _FakePlay("merge_pr", success=True, alignment_delta=0.25),
    ]
    ctx = _ctx(history)
    assert ctx.issues_closed_this_session == 1


def test_stagnation_entropy_multiplier_activates_at_warn_threshold():
    # Default warn_after = 1 minute; one idle agent + last play ended >60s ago.
    history = [_FakePlay("code_review", success=True, ended_at=_ts(-90))]
    ctx = _ctx(history, agents=[_idle_agent()], stagnation_warn_after=1)
    assert ctx.stagnation_entropy_multiplier == pytest.approx(
        STAGNATION_ENTROPY_MULTIPLIER, abs=1e-5
    )


def test_stagnation_entropy_multiplier_uses_configured_warn_after():
    # warn_after=3 minutes → only fires once ≥3 minutes of all-idle.
    history = [_FakePlay("code_review", success=True, ended_at=_ts(-185))]
    ctx = _ctx(history, agents=[_idle_agent()], stagnation_warn_after=3)
    assert ctx.stagnation_entropy_multiplier == pytest.approx(1.5, abs=1e-5)

    # Same warn_after, but only ~30s of idle → below threshold.
    shorter = [_FakePlay("code_review", success=True, ended_at=_ts(-30))]
    shorter_ctx = _ctx(shorter, agents=[_idle_agent()], stagnation_warn_after=3)
    assert shorter_ctx.stagnation_entropy_multiplier == pytest.approx(1.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Issue closure/churn metrics
# ---------------------------------------------------------------------------


def test_issues_closed_this_session_counts_successful_merge_pr():
    history = [
        _FakePlay("issue_pickup", success=True),
        _FakePlay("merge_pr", success=True),
        _FakePlay("merge_pr", success=False),
    ]
    ctx = _ctx(history)
    assert ctx.issues_closed_this_session == 1


def test_issue_churn_rate_counts_merge_pr_in_recent_window():
    history = [
        _FakePlay("merge_pr", success=True),
        _FakePlay("code_review", success=True),
        _FakePlay("run_qa", success=True),
    ]
    ctx = _ctx(history, open_issues=[object(), object(), object(), object()])
    assert ctx.issue_churn_rate == pytest.approx(0.25, abs=1e-5)


# ---------------------------------------------------------------------------
# PR counts
# ---------------------------------------------------------------------------


def test_pr_counts_populated():
    @dataclass
    class _PR:
        state: str = "open"

    ctx = _build_context(
        _state(),
        [],
        prs_open=[_PR("open"), _PR("open")],
        prs_approved=[_PR("approved")],
        learnings=[],
    )
    assert ctx.open_pr_count == 2
    assert ctx.prs_approved_unmerged == 1


def test_pr_counts_empty():
    ctx = _ctx([])
    assert ctx.open_pr_count == 0
    assert ctx.prs_awaiting_review == 0
    assert ctx.prs_approved_unmerged == 0


# ---------------------------------------------------------------------------
# Handoff rolling stats
# ---------------------------------------------------------------------------


def test_handoff_rolling_stats_from_recent_handoffs():
    @dataclass
    class _Handoff:
        context_loss_estimate: float | None = None
        ramp_up_duration_ms: int | None = None

    ctx = _build_context(
        _state(),
        [],
        prs_open=[],
        prs_approved=[],
        learnings=[],
        handoffs=[
            _Handoff(context_loss_estimate=0.1, ramp_up_duration_ms=1000),
            _Handoff(context_loss_estimate=0.3, ramp_up_duration_ms=3000),
        ],
    )
    assert ctx.rolling_avg_context_loss == pytest.approx(0.2, abs=1e-5)
    assert ctx.rolling_avg_rampup_ms == pytest.approx(2000.0, abs=1e-5)


def test_handoff_rolling_stats_ignore_null_values():
    @dataclass
    class _Handoff:
        context_loss_estimate: float | None = None
        ramp_up_duration_ms: int | None = None

    ctx = _build_context(
        _state(),
        [],
        prs_open=[],
        prs_approved=[],
        learnings=[],
        handoffs=[
            _Handoff(context_loss_estimate=None, ramp_up_duration_ms=1200),
            _Handoff(context_loss_estimate=0.4, ramp_up_duration_ms=None),
        ],
    )
    assert ctx.rolling_avg_context_loss == pytest.approx(0.4, abs=1e-5)
    assert ctx.rolling_avg_rampup_ms == pytest.approx(1200.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Learnings
# ---------------------------------------------------------------------------


def test_learning_count():
    @dataclass
    class _L:
        confidence: float = 0.8

    ctx = _build_context(_state(), [], [], [], [_L(), _L(0.6)])
    assert ctx.learning_count == 2
    assert ctx.learning_avg_confidence == pytest.approx(0.7, abs=1e-5)


def test_learning_count_zero():
    ctx = _ctx([])
    assert ctx.learning_count == 0
    assert ctx.learning_avg_confidence == pytest.approx(0.0, abs=1e-5)


def test_metrics_engine_does_not_store_last_context_reference():
    store = AsyncMock()
    engine = MetricsEngine(store=store, session_id="s")
    assert not hasattr(engine, "_last_context")


@pytest.mark.asyncio
async def test_snapshot_does_not_store_last_context_reference():
    store = AsyncMock()
    store.get_play_history = AsyncMock(return_value=[])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.list_handoffs = AsyncMock(return_value=[])
    store.list_all_issues = AsyncMock(return_value=[])
    engine = MetricsEngine(store=store, session_id="s")

    await engine.snapshot(_state())

    assert not hasattr(engine, "_last_context")


@pytest.mark.asyncio
async def test_snapshot_uses_agentshore_created_issue_count_for_created_metric():
    store = AsyncMock()
    store.get_play_history = AsyncMock(return_value=[])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.list_handoffs = AsyncMock(return_value=[])
    store.list_all_issues = AsyncMock(
        return_value=[
            GitHubIssueRecord(
                issue_number=101,
                session_id="s",
                title="qa finding 1",
                state="open",
                created_at=_ts(-10),
                labels=["agentshore/qa"],
            ),
            GitHubIssueRecord(
                issue_number=102,
                session_id="s",
                title="qa finding 2",
                state="open",
                created_at=_ts(-5),
                source="agentshore/review",
            ),
            GitHubIssueRecord(
                issue_number=103,
                session_id="s",
                title="not agentshore-created",
                state="open",
                created_at=_ts(-1),
                labels=["bug"],
            ),
        ]
    )
    engine = MetricsEngine(store=store, session_id="s")

    ctx = await engine.snapshot(_state())

    assert ctx.issues_created_this_session == 2


# ---------------------------------------------------------------------------
# same_type_failure_streak passes through from state
# ---------------------------------------------------------------------------


def test_streak_from_state():
    ctx = _ctx([], same_type_failure_streak=4)
    assert ctx.same_type_failure_streak == 4


# ---------------------------------------------------------------------------
# Time-since metrics
# ---------------------------------------------------------------------------


def test_minutes_since_alignment_never_ran():
    ctx = _ctx([])
    assert ctx.minutes_since_last_alignment_check == pytest.approx(480.0, abs=1e-1)


def test_minutes_since_intake_never_ran():
    ctx = _ctx([])
    assert ctx.minutes_since_last_intake == pytest.approx(480.0, abs=1e-1)


def test_minutes_since_alignment_just_ran():
    history = [
        _FakePlay(
            "calibrate_alignment",
            success=True,
            started_at=_ts(-60),
            ended_at=_ts(-30),
        ),
        _FakePlay(
            "issue_pickup",
            success=True,
            started_at=_ts(-30),
            ended_at=_ts(0),
        ),
    ]
    ctx = _ctx(history)
    # last play ended_at is 30 seconds after calibration ended -> ~0.5 min
    assert ctx.minutes_since_last_alignment_check == pytest.approx(0.5, abs=0.1)


# ---------------------------------------------------------------------------
# MetricsEngine.snapshot — graceful degradation when DataStore queries raise.
# Each except branch in snapshot() must log a warning and produce a valid
# ObservationContext using empty results for the failed query.
# ---------------------------------------------------------------------------


def _store_with_one_failure(failing_method: str, exc: Exception) -> AsyncMock:
    """Build a mock DataStore where exactly *failing_method* raises.

    All query methods exercised by snapshot() are stubbed; only the
    target one raises so we can isolate each except branch.
    """
    store = AsyncMock()
    store.get_play_history = AsyncMock(return_value=[])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.list_handoffs = AsyncMock(return_value=[])
    store.list_all_issues = AsyncMock(return_value=[])
    getattr(store, failing_method).side_effect = exc
    return store


@pytest.mark.parametrize(
    "failing_method,query_label",
    [
        ("get_play_history", "play_history"),
        ("list_open_pull_requests", "open_pull_requests"),
        ("list_approved_pull_requests", "approved_pull_requests"),
        ("list_handoffs", "handoffs"),
        ("list_all_issues", "all_issues"),
    ],
)
@pytest.mark.asyncio
async def test_snapshot_degrades_gracefully_when_store_query_raises(
    failing_method: str,
    query_label: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each store-method failure must:
    1. Be caught (snapshot returns a valid ObservationContext)
    2. Be logged with metrics_query_failed + the appropriate query label
    3. Substitute an empty result for the failed query
    """
    store = _store_with_one_failure(failing_method, RuntimeError("simulated failure"))
    engine = MetricsEngine(store=store, session_id="sess")

    with caplog.at_level(logging.WARNING):
        ctx = await engine.snapshot(_state())

    # Snapshot returned successfully — the except branch was taken.
    assert ctx is not None

    # Empty-result substitution for the failed query: when the PR queries raise
    # we see open_pr_count==0 / prs_approved_unmerged==0. The history failure
    # path produces an empty `last_play_types` tuple.
    if failing_method == "list_open_pull_requests":
        assert ctx.open_pr_count == 0
    elif failing_method == "list_approved_pull_requests":
        assert ctx.prs_approved_unmerged == 0
    elif failing_method == "list_all_issues":
        # When list_all_issues raises, issues=[] is substituted, so
        # _count_session_created_issues([])=0 → ctx.issues_created_this_session=0.
        # GH #495 follow-up.
        assert ctx.issues_created_this_session == 0
    elif failing_method == "get_play_history":
        assert all(t is None for t in ctx.last_play_types)

    # The structured warning event was emitted. structlog routing varies by
    # test ordering — accept either capsys (PrintLogger) or caplog (stdlib).
    captured = capsys.readouterr()
    haystack = captured.out + captured.err + " ".join(rec.getMessage() for rec in caplog.records)
    assert "metrics_query_failed" in haystack
    # The specific query that failed must be tagged in the event so operators
    # can identify which DB call broke.
    assert query_label in haystack


# ---------------------------------------------------------------------------
# Learnings from JSON store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_learning_count_comes_from_json_store(tmp_path: Any) -> None:
    """learning_count must reflect the JSON learnings file, not the DataStore."""
    import json
    from pathlib import Path

    # Seed a learnings.json with 3 entries.
    learnings_dir = tmp_path / ".agentshore"
    learnings_dir.mkdir()
    learnings_file = learnings_dir / "learnings.json"
    learnings_file.write_text(
        json.dumps(
            [
                {
                    "id": "1",
                    "pattern": "always add tests",
                    "confidence": 0.9,
                    "sessions_since_use": 0,
                    "source_play_id": None,
                    "last_reinforced_play_id": None,
                },
                {
                    "id": "2",
                    "pattern": "keep PRs small",
                    "confidence": 0.8,
                    "sessions_since_use": 1,
                    "source_play_id": None,
                    "last_reinforced_play_id": None,
                },
                {
                    "id": "3",
                    "pattern": "review before merge",
                    "confidence": 0.7,
                    "sessions_since_use": 0,
                    "source_play_id": None,
                    "last_reinforced_play_id": None,
                },
            ]
        ),
        encoding="utf-8",
    )

    store = AsyncMock()
    store.get_play_history = AsyncMock(return_value=[])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.list_handoffs = AsyncMock(return_value=[])
    store.list_all_issues = AsyncMock(return_value=[])
    engine = MetricsEngine(
        store=store,
        session_id="s",
        repo_root=Path(tmp_path),
        learnings_file=".agentshore/learnings.json",
    )

    ctx = await engine.snapshot(_state())

    assert ctx.learning_count == 3
    assert ctx.learning_avg_confidence == pytest.approx(0.8, abs=1e-5)


@pytest.mark.asyncio
async def test_snapshot_learning_count_zero_when_file_absent(tmp_path: Any) -> None:
    """No learnings file → learning_count == 0 (graceful fallback)."""
    from pathlib import Path

    store = AsyncMock()
    store.get_play_history = AsyncMock(return_value=[])
    store.list_open_pull_requests = AsyncMock(return_value=[])
    store.list_approved_pull_requests = AsyncMock(return_value=[])
    store.list_handoffs = AsyncMock(return_value=[])
    store.list_all_issues = AsyncMock(return_value=[])
    engine = MetricsEngine(
        store=store,
        session_id="s",
        repo_root=Path(tmp_path),
        learnings_file=".agentshore/learnings.json",
    )

    ctx = await engine.snapshot(_state())

    assert ctx.learning_count == 0


# ---------------------------------------------------------------------------
# Per-agent / per-play specialization matrix
# ---------------------------------------------------------------------------


def test_specialization_matrix_groups_success_by_agent_and_play():
    """One agent, three play types, mixed outcomes → exact per-cell stats."""
    history = [
        _FakePlay("issue_pickup", success=True, agent_id="agent-a"),
        _FakePlay("issue_pickup", success=False, agent_id="agent-a"),
        _FakePlay("code_review", success=True, agent_id="agent-a"),
        _FakePlay("run_qa", success=False, agent_id="agent-a"),
    ]
    cells = compute_agent_specialization(history)  # type: ignore[arg-type]

    by_play: dict[str, Any] = {}
    for cell in cells:
        key = cell.play_type.value if isinstance(cell.play_type, PlayType) else cell.play_type
        by_play[key] = cell

    issue_cell = by_play["issue_pickup"]
    assert issue_cell.agent_id == "agent-a"
    assert issue_cell.play_type == PlayType.ISSUE_PICKUP
    assert issue_cell.total == 2
    assert issue_cell.successful == 1
    assert issue_cell.failed == 1
    assert issue_cell.success_rate == pytest.approx(0.5)
    assert issue_cell.rolling_success_rate == pytest.approx(0.5)

    cr_cell = by_play["code_review"]
    assert cr_cell.total == 1
    assert cr_cell.successful == 1
    assert cr_cell.success_rate == pytest.approx(1.0)

    qa_cell = by_play["run_qa"]
    assert qa_cell.total == 1
    assert qa_cell.failed == 1
    assert qa_cell.success_rate == pytest.approx(0.0)


def test_specialization_skips_plays_with_no_agent_id():
    """A play without agent attribution is excluded from the matrix."""
    history = [
        _FakePlay("issue_pickup", success=True, agent_id=None),
        _FakePlay("issue_pickup", success=True, agent_id="agent-x"),
    ]
    cells = compute_agent_specialization(history)  # type: ignore[arg-type]
    assert len(cells) == 1
    assert cells[0].agent_id == "agent-x"
    assert cells[0].total == 1


def test_specialization_rolling_window_caps_at_recent_plays():
    """Rolling rate only averages the last `rolling_window` plays per cell."""
    history = [_FakePlay("issue_pickup", success=False, agent_id="a") for _ in range(5)] + [
        _FakePlay("issue_pickup", success=True, agent_id="a") for _ in range(10)
    ]
    cells = compute_agent_specialization(history)  # type: ignore[arg-type]
    assert len(cells) == 1
    cell = cells[0]
    # All-session: 10 of 15 succeeded.
    assert cell.success_rate == pytest.approx(10 / 15)
    # Rolling (last 10): all 10 succeeded.
    assert cell.rolling_success_rate == pytest.approx(1.0)


def test_specialization_preserves_unknown_play_type_as_string():
    history = [_FakePlay("legacy_play", success=True, agent_id="agent-a")]
    cells = compute_agent_specialization(history)  # type: ignore[arg-type]
    assert len(cells) == 1
    assert cells[0].play_type == "legacy_play"


def test_specialization_sort_is_deterministic():
    history = [
        _FakePlay("run_qa", success=True, agent_id="agent-b"),
        _FakePlay("issue_pickup", success=True, agent_id="agent-a"),
        _FakePlay("code_review", success=True, agent_id="agent-a"),
    ]
    cells = compute_agent_specialization(history)  # type: ignore[arg-type]
    keys = [
        (
            c.agent_id,
            c.play_type.value if isinstance(c.play_type, PlayType) else c.play_type,
        )
        for c in cells
    ]
    assert keys == [
        ("agent-a", "code_review"),
        ("agent-a", "issue_pickup"),
        ("agent-b", "run_qa"),
    ]
