"""IPC serializer — pure data-conversion layer with no I/O or sockets.

Converts AgentShore domain objects into plain dicts suitable for JSON encoding.

Outbound message types:
    state_update          — full OrchestratorState snapshot after each play cycle
    play_event            — play started / completed / failed notification
    feedback_requested    — escalation trigger requiring human feedback
    issue_landscape_changed — external issue churn detected
    verification_checkpoint — manual review / verification needed
"""

from __future__ import annotations

import dataclasses
import itertools
import math
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from agentshore.beads import BeadStatus, EpicStatus, GraphTask, ProjectGraph
from agentshore.ipc.wire import frame as _frame
from agentshore.plays.candidates import build_candidate_plan
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
    SessionStatsSnapshot,
    TrajectorySnapshot,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

# Monotonic sequence counter, incremented once per outbound message.
_seq: itertools.count[int] = itertools.count(1)


# Wire payload TypedDicts: single source of truth for per-message field sets.
# Mirrored in dashboard/src/types.ts (PlayEventStarted/PlayEventCompleted/
# ActivePlay) — keep all three in sync.


class PlayStartedPayload(TypedDict):
    """The ``play_event`` payload emitted with ``status == "started"``."""

    play_type: str
    status: Literal["started"]
    agent_id: str | None
    issue_number: int | None
    pr_number: int | None
    branch: str | None
    play_id: int | None
    started_at: str | None
    trigger_agent_id: str | None
    trigger_agent_type: str | None
    trigger_error_class: str | None


class PlayCompletedPayload(TypedDict):
    """The ``play_event`` payload emitted with ``status in {"completed", "failed"}``."""

    play_type: str
    agent_id: str | None
    success: bool
    duration_seconds: float
    dollar_cost: float
    token_cost: int
    artifacts: list[str]
    alignment_delta: float | None
    error: str | None
    play_id: int | None
    skipped: bool
    skip_category: str | None
    status: Literal["completed", "failed"]


class ActivePlayPayload(TypedDict):
    """The ``active_play`` field set, shared by ``state_update`` and the bridge
    ``active_play_replay`` cache so the shape is derived in exactly one place."""

    play_type: str | None
    agent_id: str | None
    started_at: str | None
    play_id: int | None
    issue_number: int | None
    pr_number: int | None
    branch: str | None
    phase: str | None
    trigger_agent_id: str | None
    trigger_agent_type: str | None
    trigger_error_class: str | None


# Internal helpers


def _serialize_agent(agent: AgentSnapshot) -> dict[str, object]:
    return {
        "agent_id": agent.agent_id,
        "agent_type": agent.agent_type.value,
        "display_name": agent.display_name,
        "model": agent.model,
        "model_tier": agent.model_tier,
        "reasoning_effort": agent.reasoning_effort,
        "status": agent.status.value,
        "context_size": agent.context_size,
        "total_cost": agent.total_cost,
        "total_tokens": agent.total_tokens,
        "tasks_completed": agent.tasks_completed,
        "tasks_failed": agent.tasks_failed,
        # desktop-31h2: per-agent dispatch count + fleet share; dashboard
        # surfaces a "Dispatch share" badge so operators can spot idling agents.
        "dispatch_count": agent.dispatch_count,
        "dispatch_share": agent.dispatch_share,
        "last_error_class": agent.last_error_class,
        # TNQA critical: these three were silently dropped from the wire
        # payload (state.py:342-379 has them, this dict didn't) — nothing
        # asserted parity until the guard below.
        "timeout_count": agent.timeout_count,
        "consecutive_timeouts": agent.consecutive_timeouts,
        "github_identity": agent.github_identity,
        "current_play": (
            {
                "play_type": agent.current_play_type.value,
                "play_id": agent.current_play_id,
                "started_at": agent.current_play_started_at,
                "issue_number": agent.current_play_issue_number,
                "pr_number": agent.current_play_pr_number,
                "branch": agent.current_play_branch,
            }
            if agent.current_play_type is not None
            else None
        ),
    }


def _serialize_issue(issue: IssueSnapshot) -> dict[str, object]:
    return {
        "issue_number": issue.issue_number,
        "title": issue.title,
        "state": issue.state,
        "priority": issue.priority,
        "labels": list(issue.labels),
        "source": issue.source,
        "url": issue.url,
        "created_at": issue.created_at,
        "closed_at": issue.closed_at,
        "github_author": issue.github_author,
        "bead_id": issue.bead_id,
        "bead_epic_id": issue.bead_epic_id,
        "bead_epic_title": issue.bead_epic_title,
        "bead_status": issue.bead_status,
        "bead_ready": issue.bead_ready,
        "bead_mirror_status": issue.bead_mirror_status,
    }


def _serialize_pull_request(pr: PullRequestSnapshot) -> dict[str, object]:
    return {
        "pr_number": pr.pr_number,
        "title": pr.title,
        "state": pr.state,
        "branch": pr.branch,
        "issue_number": pr.issue_number,
        "linked_issue_numbers": list(pr.linked_issue_numbers),
        "labels": list(pr.labels),
        "review_decision": pr.review_decision,
        "status_check_summary": pr.status_check_summary,
        "is_draft": pr.is_draft,
        "blocked": pr.blocked,
        "blocked_reasons": list(pr.blocked_reasons),
        "url": pr.url,
        "github_author": pr.github_author,
        "author_agent_id": pr.author_agent_id,
        "author_agent_type": pr.author_agent_type,
        "head_sha": pr.head_sha,
        "mergeable": pr.mergeable,
        "base_ref": pr.base_ref,
        "last_reviewed_sha": pr.last_reviewed_sha,
        "last_review_status": pr.last_review_status,
    }


def serialize_budget_update(budget: BudgetSnapshot) -> dict[str, object]:
    """Wire payload for a budget-only ``budget_update`` heartbeat frame.

    Carries just the budget snapshot so the dashboard refreshes the budget bar
    (the remaining-time countdown) without re-processing agents.
    """
    return {"budget": _serialize_budget(budget)}


def _serialize_budget(budget: BudgetSnapshot) -> dict[str, object]:
    def _finite_or_none(value: float | None) -> float | None:
        return value if value is not None and math.isfinite(value) else None

    return {
        "enabled": budget.enabled,
        "total_budget": budget.total_budget if budget.enabled else None,
        "spent": budget.spent,
        "remaining": (
            budget.remaining if budget.enabled and math.isfinite(budget.remaining) else None
        ),
        "estimated_cost_per_play": budget.estimated_cost_per_play,
        "time_enabled": budget.time_enabled,
        "time_total_minutes": _finite_or_none(budget.time_total_minutes)
        if budget.time_enabled
        else None,
        "time_elapsed_minutes": _finite_or_none(budget.time_elapsed_minutes)
        if budget.time_enabled
        else None,
        "time_remaining_minutes": _finite_or_none(budget.time_remaining_minutes)
        if budget.time_enabled
        else None,
    }


def _serialize_epic_status(epic: EpicStatus) -> dict[str, object]:
    return {
        "bead_id": epic.bead_id,
        "title": epic.title,
        "total_tasks": epic.total_tasks,
        "closed_tasks": epic.closed_tasks,
        "closure_ratio": epic.closure_ratio,
    }


def _serialize_graph_task(task: GraphTask) -> dict[str, object]:
    return {
        "bead_id": task.bead_id,
        "title": task.title,
        "status": task.status.value,
        "parent_id": task.parent_id,
        "epic_id": task.epic_id,
        "epic_title": task.epic_title,
        "external_ref": task.external_ref,
        "issue_number": task.issue_number,
        "ready": task.ready,
        # Sorted for wire determinism — the source fields are frozensets.
        "depends_on_ids": sorted(task.depends_on_ids),
        "blocked_by_ids": sorted(task.blocked_by_ids),
        "closed_at": task.closed_at,
        "updated_at": task.updated_at,
    }


def _serialize_graph(graph: ProjectGraph) -> dict[str, object]:
    return {
        "epics": [_serialize_epic_status(e) for e in graph.epics],
        "tasks": [_serialize_graph_task(t) for t in graph.tasks],
        "tasks_ready": graph.tasks_ready,
        "tasks_blocked": graph.tasks_blocked,
        "tasks_total": graph.tasks_total,
        "global_closure_ratio": graph.global_closure_ratio,
    }


def _serialize_trajectory(trajectory: TrajectorySnapshot) -> dict[str, object]:
    return {
        "projected_alignment_at_budget_end": trajectory.projected_alignment_at_budget_end,
        "estimated_remaining_plays": trajectory.estimated_remaining_plays,
        "estimated_remaining_cost": trajectory.estimated_remaining_cost,
    }


def _serialize_active_play(active: ActivePlay) -> ActivePlayPayload:
    """Serialize an ``ActivePlay`` to the IPC-documented shape."""
    return {
        "play_type": active.play_type.value,
        "agent_id": active.agent_id,
        "started_at": active.started_at,
        "play_id": active.play_id,
        "issue_number": active.issue_number,
        "pr_number": active.pr_number,
        "branch": active.branch,
        "phase": active.phase,
        "trigger_agent_id": active.trigger_agent_id,
        "trigger_agent_type": active.trigger_agent_type,
        "trigger_error_class": active.trigger_error_class,
    }


def _serialize_play_type_stats(stats: PlayTypeStatsSnapshot) -> dict[str, object]:
    play_type = stats.play_type.value if isinstance(stats.play_type, PlayType) else stats.play_type
    return {
        "play_type": play_type,
        "total": stats.total,
        "successful": stats.successful,
        "failed": stats.failed,
        "success_rate": stats.success_rate,
        "total_cost": stats.total_cost,
        "avg_duration_seconds": stats.avg_duration_seconds,
    }


def _serialize_agent_specialization(
    cell: AgentPlaySpecializationSnapshot,
) -> dict[str, object]:
    play_type = cell.play_type.value if isinstance(cell.play_type, PlayType) else cell.play_type
    return {
        "agent_id": cell.agent_id,
        "play_type": play_type,
        "total": cell.total,
        "successful": cell.successful,
        "failed": cell.failed,
        "success_rate": cell.success_rate,
        "rolling_success_rate": cell.rolling_success_rate,
    }


def _serialize_session_stats(stats: SessionStatsSnapshot) -> dict[str, object]:
    return {
        "total_plays": stats.total_plays,
        "successful_plays": stats.successful_plays,
        "failed_plays": stats.failed_plays,
        "success_rate": stats.success_rate,
        "total_cost": stats.total_cost,
        "avg_cost_per_play": stats.avg_cost_per_play,
        "total_tokens": stats.total_tokens,
        "avg_duration_seconds": stats.avg_duration_seconds,
        "by_play_type": [_serialize_play_type_stats(row) for row in stats.by_play_type],
        "agent_specialization": [
            _serialize_agent_specialization(cell) for cell in stats.agent_specialization
        ],
    }


# Wire-field parity guards
#
# Every _serialize_* function above hand-enumerates a source dataclass's fields
# instead of deriving them, so nothing stopped a new/renamed dataclass field
# from silently never reaching the wire (TNQA critical: this is exactly how
# AgentSnapshot.timeout_count/consecutive_timeouts/github_identity and five
# PullRequestSnapshot review/merge fields went missing). Each guard below
# builds a minimal probe instance, calls the real serializer, and diffs the
# emitted key set against ``dataclasses.fields()`` minus an explicit
# ``_WIRE_OMITTED_*`` allowlist — same pattern as
# ``reports/_aggregations.py``'s ``_EXPECTED_PLAY_LOG_KEYS`` guard. A drifted
# dataclass fails import instead of silently dropping data.


def _assert_field_parity(
    dataclass_type: type[Any],
    emitted_keys: set[str],
    *,
    omitted: frozenset[str],
    label: str,
) -> None:
    expected = {f.name for f in dataclasses.fields(dataclass_type)} - omitted
    if emitted_keys != expected:
        missing = sorted(expected - emitted_keys)
        extra = sorted(emitted_keys - expected)
        msg = (
            f"{label} field coverage drifted from {dataclass_type.__name__}: "
            f"missing={missing!r} extra={extra!r}. Update the serializer "
            f"(or its _WIRE_OMITTED_* allowlist) to match."
        )
        raise ValueError(msg)


# AgentSnapshot's current_play_* fields are folded into the nested
# "current_play" sub-object rather than emitted as top-level keys.
_AGENT_CURRENT_PLAY_FIELDS: frozenset[str] = frozenset(
    {
        "current_play_type",
        "current_play_id",
        "current_play_started_at",
        "current_play_issue_number",
        "current_play_pr_number",
        "current_play_branch",
    }
)
_WIRE_OMITTED_AGENT_FIELDS: frozenset[str] = frozenset()  # nothing omitted


def _agent_parity_probe() -> AgentSnapshot:
    return AgentSnapshot(
        agent_id="probe",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        current_play_type=PlayType.RUN_QA,  # force the nested current_play branch
        current_play_id=0,
        current_play_started_at="t",
        current_play_issue_number=0,
        current_play_pr_number=0,
        current_play_branch="b",
    )


_agent_probe_payload = _serialize_agent(_agent_parity_probe())
_assert_field_parity(
    AgentSnapshot,
    (set(_agent_probe_payload) - {"current_play"}) | _AGENT_CURRENT_PLAY_FIELDS,
    omitted=_WIRE_OMITTED_AGENT_FIELDS,
    label="_serialize_agent",
)

_WIRE_OMITTED_ISSUE_FIELDS: frozenset[str] = frozenset()  # nothing omitted
_assert_field_parity(
    IssueSnapshot,
    set(
        _serialize_issue(
            IssueSnapshot(
                issue_number=0, title="t", state="open", priority=None, labels=[], source=None
            )
        )
    ),
    omitted=_WIRE_OMITTED_ISSUE_FIELDS,
    label="_serialize_issue",
)

_WIRE_OMITTED_PR_FIELDS: frozenset[str] = frozenset()  # nothing omitted
_assert_field_parity(
    PullRequestSnapshot,
    set(
        _serialize_pull_request(
            PullRequestSnapshot(
                pr_number=0,
                title="t",
                state="open",
                branch=None,
                issue_number=None,
                labels=[],
                review_decision=None,
                status_check_summary=None,
                is_draft=False,
                blocked=False,
                blocked_reasons=[],
            )
        )
    ),
    omitted=_WIRE_OMITTED_PR_FIELDS,
    label="_serialize_pull_request",
)

_WIRE_OMITTED_BUDGET_FIELDS: frozenset[str] = frozenset()  # nothing omitted
_assert_field_parity(
    BudgetSnapshot,
    set(
        _serialize_budget(
            BudgetSnapshot(total_budget=0.0, spent=0.0, remaining=0.0, estimated_cost_per_play=0.0)
        )
    ),
    omitted=_WIRE_OMITTED_BUDGET_FIELDS,
    label="_serialize_budget",
)

_WIRE_OMITTED_EPIC_FIELDS: frozenset[str] = frozenset()  # nothing omitted
_assert_field_parity(
    EpicStatus,
    set(
        _serialize_epic_status(
            EpicStatus(bead_id="b", title="t", total_tasks=0, closed_tasks=0, closure_ratio=0.0)
        )
    ),
    omitted=_WIRE_OMITTED_EPIC_FIELDS,
    label="_serialize_epic_status",
)

# depends_on_ids/blocked_by_ids default to empty frozensets, which is enough
# for a key-existence check (the guard diffs keys, not values).
_WIRE_OMITTED_GRAPH_TASK_FIELDS: frozenset[str] = frozenset()  # nothing omitted
_assert_field_parity(
    GraphTask,
    set(_serialize_graph_task(GraphTask(bead_id="b", title="t", status=BeadStatus.OPEN))),
    omitted=_WIRE_OMITTED_GRAPH_TASK_FIELDS,
    label="_serialize_graph_task",
)

_WIRE_OMITTED_PROJECT_GRAPH_FIELDS: frozenset[str] = frozenset()  # nothing omitted
_assert_field_parity(
    ProjectGraph,
    set(_serialize_graph(ProjectGraph())),
    omitted=_WIRE_OMITTED_PROJECT_GRAPH_FIELDS,
    label="_serialize_graph",
)

_WIRE_OMITTED_TRAJECTORY_FIELDS: frozenset[str] = frozenset()  # nothing omitted
_assert_field_parity(
    TrajectorySnapshot,
    set(
        _serialize_trajectory(
            TrajectorySnapshot(
                projected_alignment_at_budget_end=0.0,
                estimated_remaining_plays=0,
                estimated_remaining_cost=0.0,
            )
        )
    ),
    omitted=_WIRE_OMITTED_TRAJECTORY_FIELDS,
    label="_serialize_trajectory",
)

_WIRE_OMITTED_ACTIVE_PLAY_FIELDS: frozenset[str] = frozenset()  # nothing omitted
_assert_field_parity(
    ActivePlay,
    set(
        _serialize_active_play(ActivePlay(play_type=PlayType.RUN_QA, agent_id=None, started_at="t"))
    ),
    omitted=_WIRE_OMITTED_ACTIVE_PLAY_FIELDS,
    label="_serialize_active_play",
)

_WIRE_OMITTED_PLAY_TYPE_STATS_FIELDS: frozenset[str] = frozenset()  # nothing omitted
_assert_field_parity(
    PlayTypeStatsSnapshot,
    set(
        _serialize_play_type_stats(
            PlayTypeStatsSnapshot(
                play_type=PlayType.RUN_QA,
                total=0,
                successful=0,
                failed=0,
                success_rate=0.0,
                total_cost=0.0,
                avg_duration_seconds=0.0,
            )
        )
    ),
    omitted=_WIRE_OMITTED_PLAY_TYPE_STATS_FIELDS,
    label="_serialize_play_type_stats",
)

_WIRE_OMITTED_AGENT_SPECIALIZATION_FIELDS: frozenset[str] = frozenset()  # nothing omitted
_assert_field_parity(
    AgentPlaySpecializationSnapshot,
    set(
        _serialize_agent_specialization(
            AgentPlaySpecializationSnapshot(
                agent_id="a",
                play_type=PlayType.RUN_QA,
                total=0,
                successful=0,
                failed=0,
                success_rate=0.0,
                rolling_success_rate=0.0,
            )
        )
    ),
    omitted=_WIRE_OMITTED_AGENT_SPECIALIZATION_FIELDS,
    label="_serialize_agent_specialization",
)

_WIRE_OMITTED_SESSION_STATS_FIELDS: frozenset[str] = frozenset()  # nothing omitted
_assert_field_parity(
    SessionStatsSnapshot,
    set(
        _serialize_session_stats(
            SessionStatsSnapshot(
                total_plays=0,
                successful_plays=0,
                failed_plays=0,
                success_rate=0.0,
                total_cost=0.0,
                avg_cost_per_play=0.0,
                total_tokens=0,
                avg_duration_seconds=0.0,
            )
        )
    ),
    omitted=_WIRE_OMITTED_SESSION_STATS_FIELDS,
    label="_serialize_session_stats",
)


# Public serialization functions


def serialize_state(state: OrchestratorState) -> dict[str, object]:
    """Serialize a OrchestratorState to a plain dict for JSON encoding.

    All enum values are converted to their string ``.value``.
    None fields remain None.  Lists of dataclasses become lists of dicts.
    """
    work_availability = build_candidate_plan(state).work_availability.to_dict()
    return {
        "session_id": state.session_id,
        "session_state": state.session_state.value,
        "policy_mode": state.policy_mode.value,
        "total_plays": state.total_plays,
        "total_cost": state.total_cost,
        "agents": [_serialize_agent(a) for a in state.agents],
        "open_issues": [_serialize_issue(i) for i in state.open_issues],
        "pull_requests": [_serialize_pull_request(pr) for pr in state.pull_requests],
        "work_availability": work_availability,
        "budget": _serialize_budget(state.budget) if state.budget is not None else None,
        "trajectory": (
            _serialize_trajectory(state.trajectory) if state.trajectory is not None else None
        ),
        "active_play": (
            _serialize_active_play(state.active_play) if state.active_play is not None else None
        ),
        "graph": _serialize_graph(state.graph) if state.graph is not None else None,
        "stats": _serialize_session_stats(state.stats) if state.stats is not None else None,
        "same_type_failure_streak": state.same_type_failure_streak,
        "same_type_streak": state.same_type_streak,
        "last_play_type": (
            state.last_play_type.value if state.last_play_type is not None else None
        ),
        "loop_level": state.loop_level,
        "main_repo_dispatch_paused": state.main_repo_dispatch_paused,
        "end_session_in_flight": state.end_session_in_flight,
        "plays_since_last_instantiate": state.plays_since_last_instantiate,
        "last_play_success_by_type": {
            play_type.value: success
            for play_type, success in state.last_play_success_by_type.items()
        },
        "action_mask": list(state.action_mask),
        "mask_reasons": {pt.value: str(reason) for pt, reason in state.mask_reasons.items()},
        # V1_CONTRACT.md §"AgentShore State Snapshot"
        "run_mode": state.run_mode.value,
        "action_space_version": state.action_space_version,
        "policy_version": state.policy_version,
        "policy_checkpoint_id": state.policy_checkpoint_id,
        "seed_freshness": state.seed_freshness,
        "learnings_count": state.learnings_count,
        "human_feedback_count": state.human_feedback_count,
    }


def serialize_play_event(
    outcome: PlayOutcome,
    status: Literal["started", "completed", "failed"],
) -> dict[str, object]:
    """Serialize a PlayOutcome plus a lifecycle status string to a plain dict.

    The live started-event path goes through :func:`build_play_started_payload`
    (the single canonical started producer); this function builds the
    completion-shaped payload (its field set is pinned by
    :class:`PlayCompletedPayload`) and is the producer for completed/failed
    events. ``status="started"`` is still accepted for back-compat with callers
    that only have a :class:`PlayOutcome` to hand.

    Fields included: play_type (string), agent_id, success, duration_seconds,
    dollar_cost, token_cost, artifacts, alignment_delta, error, play_id,
    skipped, skip_category, status.
    """
    return {
        "play_type": outcome.play_type.value,
        "agent_id": outcome.agent_id,
        "success": outcome.success,
        "duration_seconds": outcome.duration_seconds,
        "dollar_cost": outcome.dollar_cost,
        "token_cost": outcome.token_cost,
        "artifacts": list(outcome.artifacts),
        "alignment_delta": outcome.alignment_delta,
        "error": outcome.error,
        "play_id": outcome.play_id,
        "skipped": outcome.skipped,
        "skip_category": outcome.skip_category,
        "status": status,
    }


def build_play_started_payload(
    *,
    play_type: str,
    agent_id: str | None,
    issue_number: int | None,
    pr_number: int | None,
    branch: str | None,
    play_id: int | None,
    started_at: str | None,
    trigger_agent_id: str | None,
    trigger_agent_type: str | None,
    trigger_error_class: str | None,
) -> PlayStartedPayload:
    """Build the single canonical ``play_event``/``started`` payload.

    This is the one producer of the started-event shape — both the live
    provider hook and any future replay/synthetic path go through here so the
    field set never drifts from :class:`PlayStartedPayload`.
    """
    return {
        "play_type": play_type,
        "status": "started",
        "agent_id": agent_id,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "branch": branch,
        "play_id": play_id,
        "started_at": started_at,
        "trigger_agent_id": trigger_agent_id,
        "trigger_agent_type": trigger_agent_type,
        "trigger_error_class": trigger_error_class,
    }


def active_play_from_started(
    fields: dict[str, object],
    *,
    default_started_at: str,
) -> ActivePlayPayload:
    """Derive the ``active_play`` field set from a started ``play_event``.

    The bridge caches the result so a reconnecting tab can replay the
    in-progress play. Centralised here so the ``active_play`` shape is selected
    in one place rather than re-derived inline. ``status`` (present on the
    started payload) is intentionally dropped, ``phase`` is filled as ``None``
    (started events do not carry a phase), and ``started_at`` falls back to
    ``default_started_at`` when the event omitted it.
    """
    started_at = fields.get("started_at")
    return {
        "play_type": _as_str_or_none(fields.get("play_type")),
        "agent_id": _as_str_or_none(fields.get("agent_id")),
        "started_at": started_at if isinstance(started_at, str) else default_started_at,
        "play_id": _as_int_or_none(fields.get("play_id")),
        "issue_number": _as_int_or_none(fields.get("issue_number")),
        "pr_number": _as_int_or_none(fields.get("pr_number")),
        "branch": _as_str_or_none(fields.get("branch")),
        "phase": None,
        "trigger_agent_id": _as_str_or_none(fields.get("trigger_agent_id")),
        "trigger_agent_type": _as_str_or_none(fields.get("trigger_agent_type")),
        "trigger_error_class": _as_str_or_none(fields.get("trigger_error_class")),
    }


def _as_str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


_FEEDBACK_TRIGGER_MAP: dict[str, str] = {
    "budget_exhausted": "budget_exhaustion",
    "budget_predictive": "budget_exhaustion",
    "loop_detected": "loop_escalation",
    "stagnation": "stagnation",
}


def serialize_feedback_requested(reason: str) -> dict[str, object]:
    """Serialize a feedback-requested event to a plain dict.

    Maps ``reason`` to a ``trigger`` classification:
    - "budget_exhausted" / "budget_predictive" → "budget_exhaustion"
    - "loop_detected" → "loop_escalation"
    - "stagnation" → "stagnation"
    - anything else → "ambiguous_intake"

    Both ``reason`` and ``trigger`` are included in the output.
    """
    trigger = _FEEDBACK_TRIGGER_MAP.get(reason, "ambiguous_intake")
    return {
        "reason": reason,
        "trigger": trigger,
    }


def make_message(msg_type: str, payload: Mapping[str, object]) -> str:
    """Wrap a payload dict into a single-line NDJSON message string.

    Produces the documented envelope format::

        {"type": "<msg_type>", "id": "<uuid4>", "timestamp": "<ISO-8601 UTC>",
         "payload": { ... }}

    Returns the envelope as a JSON string terminated with ``"\\n"``.
    """
    envelope: dict[str, object] = {
        "type": msg_type,
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "seq": next(_seq),
        "payload": payload,
    }
    return _frame(envelope)
