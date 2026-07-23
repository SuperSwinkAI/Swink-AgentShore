"""Tests for the IPC serializer module (W1.1)."""

from __future__ import annotations

import dataclasses
import json

import pytest

from agentshore.beads import BeadStatus, GraphTask, ProjectGraph
from agentshore.ipc import serializer as serializer_module
from agentshore.ipc.serializer import (
    make_message,
    serialize_feedback_requested,
    serialize_play_event,
    serialize_state,
)
from agentshore.state import (
    ActivePlay,
    AgentPlaySpecializationSnapshot,
    AgentSnapshot,
    AgentStatus,
    AgentType,
    BudgetSnapshot,
    IssueSnapshot,
    OrchestratorState,
    PlayOutcome,
    PlayType,
    PlayTypeStatsSnapshot,
    PullRequestSnapshot,
    SessionState,
    SessionStatsSnapshot,
)


def _minimal_state(**overrides: object) -> OrchestratorState:
    """Build the smallest valid OrchestratorState for testing."""
    defaults: dict[str, object] = {
        "session_id": "s1",
        "session_state": SessionState.RUNNING,
        "total_plays": 5,
        "total_cost": 1.23,
        "agents": [],
        "open_issues": [],
        "budget": None,
        "trajectory": None,
        "active_play": None,
        "same_type_failure_streak": 0,
    }
    defaults.update(overrides)
    return OrchestratorState(**defaults)  # type: ignore[arg-type]


def _minimal_outcome(**overrides: object) -> PlayOutcome:
    defaults: dict[str, object] = {
        "play_type": PlayType.ISSUE_PICKUP,
        "agent_id": "agent-1",
        "success": True,
        "partial": False,
        "duration_seconds": 2.5,
        "token_cost": 100,
        "dollar_cost": 0.05,
        "artifacts": [],
        "alignment_delta": 0.1,
        "error": None,
        "play_id": 42,
    }
    defaults.update(overrides)
    return PlayOutcome(**defaults)  # type: ignore[arg-type]


def _minimal_agent(agent_id: str = "agent-1") -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        display_name="Claude: Test Agent",
    )


def test_serialize_state_has_required_keys() -> None:
    state = _minimal_state()
    result = serialize_state(state)
    for key in (
        "session_id",
        "policy_mode",
        "agents",
        "budget",
        "total_plays",
        "active_play",
        "pull_requests",
        "work_availability",
    ):
        assert key in result, f"Missing key: {key}"


def test_serialize_state_omits_legacy_current_play() -> None:
    """The legacy ``current_play`` top-level field is no longer emitted."""
    state = _minimal_state()
    result = serialize_state(state)
    assert "current_play" not in result


def test_serialize_session_state_as_string() -> None:
    state = _minimal_state(session_state=SessionState.RUNNING)
    result = serialize_state(state)
    assert result["session_state"] == "running"


def test_serialize_policy_mode_as_string() -> None:
    state = _minimal_state()
    result = serialize_state(state)
    assert result["policy_mode"] == "learning"


def test_serialize_state_includes_human_feedback_count() -> None:
    state = _minimal_state(human_feedback_count=3)
    result = serialize_state(state)
    assert result["human_feedback_count"] == 3


def test_serialize_state_includes_work_availability_counts() -> None:
    issue = IssueSnapshot(
        issue_number=209,
        title="Blocked issue",
        state="open",
        priority=None,
        labels=["agentshore/blocked", "agentshore/disallowed"],
        source=None,
    )
    result = serialize_state(_minimal_state(open_issues=[issue]))

    availability = result["work_availability"]
    assert isinstance(availability, dict)
    assert availability["github_open_issue_count"] == 1
    assert availability["workable_issue_count"] == 0
    assert "issue_availability" not in result


def test_serialize_paused_session_state_as_string() -> None:
    state = _minimal_state(session_state=SessionState.PAUSED)
    result = serialize_state(state)
    assert result["session_state"] == "paused"


def test_serialize_none_active_play() -> None:
    state = _minimal_state(active_play=None)
    result = serialize_state(state)
    assert result["active_play"] is None


def test_serialize_state_agents_list() -> None:
    agents = [_minimal_agent("a1"), _minimal_agent("a2")]
    state = _minimal_state(agents=agents)
    result = serialize_state(state)
    agent_list = result["agents"]
    assert isinstance(agent_list, list)
    assert len(agent_list) == 2  # type: ignore[arg-type]
    for item in agent_list:  # type: ignore[union-attr]
        assert "agent_id" in item  # type: ignore[operator]
        assert item["display_name"]  # type: ignore[index]
        assert item["current_play"] is None  # type: ignore[index]


def test_serialize_agent_current_play_fields() -> None:
    agent = AgentSnapshot(
        agent_id="a1",
        agent_type=AgentType.CODEX,
        status=AgentStatus.BUSY,
        context_size=1024,
        total_cost=0.25,
        total_tokens=1000,
        tasks_completed=2,
        tasks_failed=0,
        current_play_type=PlayType.RUN_QA,
        current_play_id=77,
        current_play_started_at="2026-01-01T00:00:00Z",
        current_play_issue_number=12,
        current_play_pr_number=None,
        current_play_branch="qa/dashboard",
    )
    state = _minimal_state(agents=[agent])

    result = serialize_state(state)
    agent_payload = result["agents"][0]  # type: ignore[index]

    current_play = agent_payload["current_play"]
    assert isinstance(current_play, dict)
    assert current_play["play_type"] == "run_qa"
    assert current_play["play_id"] == 77
    assert current_play["started_at"] == "2026-01-01T00:00:00Z"
    assert current_play["issue_number"] == 12
    assert current_play["pr_number"] is None
    assert current_play["branch"] == "qa/dashboard"


def test_serialize_state_active_play() -> None:
    active = ActivePlay(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id="agent-1",
        started_at="2026-01-01T00:00:00Z",
        play_id=42,
        issue_number=12,
        pr_number=None,
        branch="feature/foo",
        phase="implementing",
    )
    state = _minimal_state(active_play=active)
    result = serialize_state(state)
    assert result["active_play"] == {
        "play_type": "issue_pickup",
        "agent_id": "agent-1",
        "started_at": "2026-01-01T00:00:00Z",
        "play_id": 42,
        "issue_number": 12,
        "pr_number": None,
        "branch": "feature/foo",
        "phase": "implementing",
        "trigger_agent_id": None,
        "trigger_agent_type": None,
        "trigger_error_class": None,
    }


def test_serialize_state_active_play_minimal_optionals() -> None:
    """ActivePlay's optional fields default to None and serialize as nulls."""
    active = ActivePlay(
        play_type=PlayType.RUN_QA,
        agent_id="agent-1",
        started_at="2026-01-01T00:00:00Z",
    )
    state = _minimal_state(active_play=active)
    result = serialize_state(state)
    payload = result["active_play"]
    assert isinstance(payload, dict)
    assert payload["play_type"] == "run_qa"
    assert payload["agent_id"] == "agent-1"
    assert payload["started_at"] == "2026-01-01T00:00:00Z"
    assert payload["play_id"] is None
    assert payload["issue_number"] is None
    assert payload["pr_number"] is None
    assert payload["branch"] is None
    assert payload["phase"] is None
    assert payload["trigger_agent_id"] is None
    assert payload["trigger_agent_type"] is None
    assert payload["trigger_error_class"] is None


def test_serialize_state_active_play_trigger_metadata() -> None:
    active = ActivePlay(
        play_type=PlayType.TAKE_BREAK,
        agent_id=None,
        started_at="2026-01-01T00:00:00Z",
        trigger_agent_id="grok-1",
        trigger_agent_type="grok",
        trigger_error_class="rate_limit",
    )
    state = _minimal_state(active_play=active)
    result = serialize_state(state)
    payload = result["active_play"]
    assert isinstance(payload, dict)
    assert payload["play_type"] == "take_break"
    assert payload["agent_id"] is None
    assert payload["trigger_agent_id"] == "grok-1"
    assert payload["trigger_agent_type"] == "grok"
    assert payload["trigger_error_class"] == "rate_limit"


def test_serialize_state_pull_requests() -> None:
    prs = [
        PullRequestSnapshot(
            pr_number=42,
            title="Fix blocked flow",
            state="open",
            branch="fix-blocked-flow",
            issue_number=12,
            labels=["changes-requested"],
            review_decision="CHANGES_REQUESTED",
            status_check_summary="failed",
            is_draft=False,
            blocked=True,
            blocked_reasons=["changes_requested", "ci_failed"],
            url="https://github.com/acme/repo/pull/42",
            github_author="octocat",
        )
    ]
    state = _minimal_state(pull_requests=prs)

    result = serialize_state(state)

    assert result["pull_requests"] == [
        {
            "pr_number": 42,
            "title": "Fix blocked flow",
            "state": "open",
            "branch": "fix-blocked-flow",
            "issue_number": 12,
            "linked_issue_numbers": [12],
            "labels": ["changes-requested"],
            "review_decision": "CHANGES_REQUESTED",
            "status_check_summary": "failed",
            "is_draft": False,
            "blocked": True,
            "blocked_reasons": ["changes_requested", "ci_failed"],
            "url": "https://github.com/acme/repo/pull/42",
            "github_author": "octocat",
            "author_agent_id": None,
            "author_agent_type": None,
            "head_sha": None,
            "mergeable": None,
            "base_ref": None,
            "last_reviewed_sha": None,
            "last_review_status": None,
        }
    ]


def test_serialize_state_budget_none() -> None:
    state = _minimal_state(budget=None)
    result = serialize_state(state)
    assert result["budget"] is None


def test_serialize_state_stats() -> None:
    stats = SessionStatsSnapshot(
        total_plays=3,
        successful_plays=2,
        failed_plays=1,
        success_rate=2 / 3,
        total_cost=1.5,
        avg_cost_per_play=0.5,
        total_tokens=3000,
        avg_duration_seconds=12.0,
        by_play_type=[
            PlayTypeStatsSnapshot(
                play_type=PlayType.ISSUE_PICKUP,
                total=2,
                successful=1,
                failed=1,
                success_rate=0.5,
                total_cost=1.2,
                avg_duration_seconds=10.0,
            )
        ],
    )
    state = _minimal_state(stats=stats)

    result = serialize_state(state)

    assert result["stats"] == {
        "total_plays": 3,
        "successful_plays": 2,
        "failed_plays": 1,
        "success_rate": 2 / 3,
        "total_cost": 1.5,
        "avg_cost_per_play": 0.5,
        "total_tokens": 3000,
        "avg_duration_seconds": 12.0,
        "by_play_type": [
            {
                "play_type": "issue_pickup",
                "total": 2,
                "successful": 1,
                "failed": 1,
                "success_rate": 0.5,
                "total_cost": 1.2,
                "avg_duration_seconds": 10.0,
            }
        ],
        "agent_specialization": [],
    }


def test_serialize_state_stats_includes_agent_specialization() -> None:
    """`stats.agent_specialization` must be present as JSON-safe dicts."""
    stats = SessionStatsSnapshot(
        total_plays=2,
        successful_plays=1,
        failed_plays=1,
        success_rate=0.5,
        total_cost=0.0,
        avg_cost_per_play=0.0,
        total_tokens=0,
        avg_duration_seconds=0.0,
        by_play_type=[],
        agent_specialization=[
            AgentPlaySpecializationSnapshot(
                agent_id="agent-a",
                play_type=PlayType.ISSUE_PICKUP,
                total=2,
                successful=1,
                failed=1,
                success_rate=0.5,
                rolling_success_rate=0.5,
            ),
            AgentPlaySpecializationSnapshot(
                agent_id="agent-a",
                play_type="legacy_play",
                total=1,
                successful=1,
                failed=0,
                success_rate=1.0,
                rolling_success_rate=1.0,
            ),
        ],
    )
    state = _minimal_state(stats=stats)

    result = serialize_state(state)

    payload = result["stats"]
    assert isinstance(payload, dict)
    specialization = payload["agent_specialization"]
    assert specialization == [
        {
            "agent_id": "agent-a",
            "play_type": "issue_pickup",
            "total": 2,
            "successful": 1,
            "failed": 1,
            "success_rate": 0.5,
            "rolling_success_rate": 0.5,
        },
        {
            "agent_id": "agent-a",
            "play_type": "legacy_play",
            "total": 1,
            "successful": 1,
            "failed": 0,
            "success_rate": 1.0,
            "rolling_success_rate": 1.0,
        },
    ]


def test_serialize_state_disabled_budget_is_browser_json_safe() -> None:
    state = _minimal_state(
        budget=BudgetSnapshot(
            total_budget=0.0,
            spent=1.23,
            remaining=float("inf"),
            estimated_cost_per_play=0.05,
            enabled=False,
        )
    )

    result = serialize_state(state)

    assert result["budget"] is not None
    assert result["budget"]["enabled"] is False  # type: ignore[index]
    assert result["budget"]["total_budget"] is None  # type: ignore[index]
    assert result["budget"]["remaining"] is None  # type: ignore[index]
    # Time fields default to disabled/None and stay JSON-safe.
    assert result["budget"]["time_enabled"] is False  # type: ignore[index]
    assert result["budget"]["time_remaining_minutes"] is None  # type: ignore[index]


def test_serialize_state_time_budget_fields() -> None:
    state = _minimal_state(
        budget=BudgetSnapshot(
            total_budget=200.0,
            spent=50.0,
            remaining=150.0,
            estimated_cost_per_play=0.5,
            enabled=True,
            time_enabled=True,
            time_total_minutes=1440.0,
            time_elapsed_minutes=100.0,
            time_remaining_minutes=1340.0,
        )
    )

    result = serialize_state(state)
    budget = result["budget"]
    assert budget is not None
    assert budget["time_enabled"] is True  # type: ignore[index]
    assert budget["time_total_minutes"] == 1440.0  # type: ignore[index]
    assert budget["time_elapsed_minutes"] == 100.0  # type: ignore[index]
    assert budget["time_remaining_minutes"] == 1340.0  # type: ignore[index]


def test_serialize_state_time_disabled_nulls_time_fields() -> None:
    # Disabled time dimension nulls all time fields, even an inf that leaks in.
    state = _minimal_state(
        budget=BudgetSnapshot(
            total_budget=200.0,
            spent=50.0,
            remaining=150.0,
            estimated_cost_per_play=0.5,
            enabled=True,
            time_enabled=False,
            time_remaining_minutes=float("inf"),
        )
    )

    result = serialize_state(state)
    budget = result["budget"]
    assert budget is not None
    assert budget["time_enabled"] is False  # type: ignore[index]
    assert budget["time_remaining_minutes"] is None  # type: ignore[index]


def test_serialize_play_event_started() -> None:
    outcome = _minimal_outcome()
    result = serialize_play_event(outcome, status="started")
    assert result["status"] == "started"


def test_serialize_play_event_completed_success() -> None:
    outcome = _minimal_outcome(success=True)
    result = serialize_play_event(outcome, status="completed")
    assert result["status"] == "completed"
    assert result["success"] is True
    assert result["skipped"] is False
    assert result["skip_category"] is None


def test_serialize_play_event_skipped_fields() -> None:
    outcome = PlayOutcome.skipped_outcome(PlayType.MERGE_PR, "no_target")
    result = serialize_play_event(outcome, status="completed")
    assert result["skipped"] is True
    assert result["skip_category"] == "no_target"


def test_serialize_play_event_has_play_type_string() -> None:
    outcome = _minimal_outcome(play_type=PlayType.CODE_REVIEW)
    result = serialize_play_event(outcome, status="completed")
    assert isinstance(result["play_type"], str)
    assert result["play_type"] == "code_review"


def test_serialize_feedback_budget_exhaustion() -> None:
    result = serialize_feedback_requested("budget_exhausted")
    assert result["trigger"] == "budget_exhaustion"
    assert result["reason"] == "budget_exhausted"


def test_serialize_feedback_loop_escalation() -> None:
    result = serialize_feedback_requested("loop_detected")
    assert result["trigger"] == "loop_escalation"


def test_make_message_has_type_field() -> None:
    msg = make_message("state_update", {"foo": 1})
    parsed = json.loads(msg)
    assert parsed["type"] == "state_update"


def test_make_message_has_id_field() -> None:
    msg = make_message("state_update", {"foo": 1})
    parsed = json.loads(msg)
    assert "id" in parsed
    import uuid

    uuid.UUID(parsed["id"], version=4)


def test_make_message_has_timestamp_field() -> None:
    msg = make_message("state_update", {"foo": 1})
    parsed = json.loads(msg)
    assert "timestamp" in parsed
    from datetime import datetime

    # Must be parseable as ISO-8601
    dt = datetime.fromisoformat(parsed["timestamp"])
    assert dt.tzinfo is not None


def test_make_message_has_payload_wrapper() -> None:
    msg = make_message("state_update", {"foo": 1, "bar": "baz"})
    parsed = json.loads(msg)
    assert "payload" in parsed
    assert parsed["payload"] == {"foo": 1, "bar": "baz"}
    # Payload data must NOT be flattened into the top-level envelope
    assert "foo" not in parsed
    assert "bar" not in parsed


def test_make_message_ends_with_newline() -> None:
    msg = make_message("state_update", {"foo": 1})
    assert msg[-1] == "\n"


def test_make_message_is_single_line() -> None:
    msg = make_message("state_update", {"foo": 1, "bar": "baz"})
    # Strip the trailing newline; there must be no other newlines
    content = msg.rstrip("\n")
    assert "\n" not in content


def test_make_message_emits_strict_json_for_non_finite_numbers() -> None:
    msg = make_message(
        "state_update",
        {
            "remaining": float("inf"),
            "nested": {"estimate": float("nan")},
            "history": [1.0, float("-inf")],
        },
    )

    parsed = json.loads(
        msg,
        parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
    )

    assert parsed["payload"]["remaining"] is None
    assert parsed["payload"]["nested"]["estimate"] is None
    assert parsed["payload"]["history"] == [1.0, None]


def test_make_message_unique_ids() -> None:
    """Each call to make_message produces a distinct message ID."""
    msg1 = json.loads(make_message("state_update", {}))
    msg2 = json.loads(make_message("state_update", {}))
    assert msg1["id"] != msg2["id"]


def test_make_message_has_seq_field() -> None:
    """Every message has a positive integer seq at the envelope level."""
    msg = json.loads(make_message("state_update", {}))
    assert "seq" in msg
    assert isinstance(msg["seq"], int)
    assert msg["seq"] > 0


def test_make_message_seq_is_monotonic() -> None:
    """seq strictly increases with each call to make_message."""
    msgs = [json.loads(make_message("play_event", {})) for _ in range(5)]
    seqs = [m["seq"] for m in msgs]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs), "seq values must be unique"


def test_make_message_seq_not_in_payload() -> None:
    """seq belongs to the envelope, not the payload."""
    msg = json.loads(make_message("state_update", {"foo": 1}))
    assert "seq" in msg
    assert "seq" not in msg["payload"]


# Regression coverage for the TNQA critical serializer-drift fix: fields
# present on the source dataclass but previously missing from the wire dict.


def test_serialize_agent_includes_timeout_and_identity_fields() -> None:
    agent = AgentSnapshot(
        agent_id="a1",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        timeout_count=2,
        consecutive_timeouts=1,
        github_identity="agent-bot",
    )
    state = _minimal_state(agents=[agent])

    result = serialize_state(state)
    payload = result["agents"][0]  # type: ignore[index]

    assert payload["timeout_count"] == 2  # type: ignore[index]
    assert payload["consecutive_timeouts"] == 1  # type: ignore[index]
    assert payload["github_identity"] == "agent-bot"  # type: ignore[index]


def test_serialize_issue_includes_github_author() -> None:
    issue = IssueSnapshot(
        issue_number=1,
        title="t",
        state="open",
        priority=None,
        labels=[],
        source=None,
        github_author="reporter",
    )
    result = serialize_state(_minimal_state(open_issues=[issue]))
    payload = result["open_issues"][0]  # type: ignore[index]
    assert payload["github_author"] == "reporter"  # type: ignore[index]


def test_serialize_pull_request_includes_review_and_merge_fields() -> None:
    pr = PullRequestSnapshot(
        pr_number=1,
        title="t",
        state="open",
        branch="b",
        issue_number=None,
        labels=[],
        review_decision=None,
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
        head_sha="abc123",
        mergeable="MERGEABLE",
        base_ref="main",
        last_reviewed_sha="def456",
        last_review_status="APPROVED",
    )
    result = serialize_state(_minimal_state(pull_requests=[pr]))
    payload = result["pull_requests"][0]  # type: ignore[index]
    assert payload["head_sha"] == "abc123"  # type: ignore[index]
    assert payload["mergeable"] == "MERGEABLE"  # type: ignore[index]
    assert payload["base_ref"] == "main"  # type: ignore[index]
    assert payload["last_reviewed_sha"] == "def456"  # type: ignore[index]
    assert payload["last_review_status"] == "APPROVED"  # type: ignore[index]


def test_serialize_graph_task_includes_dependency_ids() -> None:
    task = GraphTask(
        bead_id="bd-1",
        title="t",
        status=BeadStatus.OPEN,
        depends_on_ids=frozenset({"bd-2", "bd-3"}),
        blocked_by_ids=frozenset({"bd-2"}),
    )
    graph = ProjectGraph(tasks=[task], tasks_total=1)
    state = _minimal_state(graph=graph)

    result = serialize_state(state)
    graph_payload = result["graph"]
    assert isinstance(graph_payload, dict)
    task_payload = graph_payload["tasks"][0]
    assert task_payload["depends_on_ids"] == ["bd-2", "bd-3"]
    assert task_payload["blocked_by_ids"] == ["bd-2"]


def test_serialize_graph_includes_tasks_blocked() -> None:
    graph = ProjectGraph(tasks_blocked=3)
    state = _minimal_state(graph=graph)

    result = serialize_state(state)
    graph_payload = result["graph"]
    assert isinstance(graph_payload, dict)
    assert graph_payload["tasks_blocked"] == 3


# Wire-field parity guard: exercise `_assert_field_parity` directly so its
# failure mode is covered, not just its import-time invocations.


def test_assert_field_parity_passes_when_keys_match() -> None:
    @dataclasses.dataclass(frozen=True, slots=True)
    class _Probe:
        a: int
        b: str

    serializer_module._assert_field_parity(
        _Probe, {"a", "b"}, omitted=frozenset(), label="test"
    )


def test_assert_field_parity_raises_on_missing_field() -> None:
    @dataclasses.dataclass(frozen=True, slots=True)
    class _Probe:
        a: int
        b: str

    with pytest.raises(ValueError, match="test"):
        serializer_module._assert_field_parity(
            _Probe, {"a"}, omitted=frozenset(), label="test"
        )


def test_assert_field_parity_raises_on_extra_field() -> None:
    @dataclasses.dataclass(frozen=True, slots=True)
    class _Probe:
        a: int

    with pytest.raises(ValueError, match="test"):
        serializer_module._assert_field_parity(
            _Probe, {"a", "unexpected"}, omitted=frozenset(), label="test"
        )


def test_assert_field_parity_respects_omitted_allowlist() -> None:
    @dataclasses.dataclass(frozen=True, slots=True)
    class _Probe:
        a: int
        internal_only: str = "x"

    serializer_module._assert_field_parity(
        _Probe, {"a"}, omitted=frozenset({"internal_only"}), label="test"
    )
