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

import itertools
import math
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, TypedDict

from agentshore.beads import EpicStatus, GraphTask, ProjectGraph
from agentshore.ipc.wire import frame as _frame
from agentshore.plays.candidates import build_candidate_plan
from agentshore.state import (
    ActivePlay,
    AgentPlaySpecializationSnapshot,
    AgentSnapshot,
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

# ---------------------------------------------------------------------------
# Monotonic sequence counter — incremented once per outbound message
# ---------------------------------------------------------------------------

_seq: itertools.count[int] = itertools.count(1)


# ---------------------------------------------------------------------------
# Wire payload TypedDicts — the single source of truth for the per-message
# field sets shared between the producers (``serializer`` / ``provider``) and
# the consumer (``dashboard.bridge``). The dashboard TS mirrors live in
# ``dashboard/src/types.ts`` (``PlayEventStarted`` / ``PlayEventCompleted`` /
# ``ActivePlay``); keep all three in sync.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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
        # desktop-31h2: per-agent dispatch count + share of the fleet-wide
        # total. Dashboard surfaces this as a "Dispatch share" badge in the
        # agent list so operators can spot agents idling while others
        # absorb all the work.
        "dispatch_count": agent.dispatch_count,
        "dispatch_share": agent.dispatch_share,
        "last_error_class": agent.last_error_class,
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
    }


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
        "closed_at": task.closed_at,
        "updated_at": task.updated_at,
    }


def _serialize_graph(graph: ProjectGraph) -> dict[str, object]:
    return {
        "epics": [_serialize_epic_status(e) for e in graph.epics],
        "tasks": [_serialize_graph_task(t) for t in graph.tasks],
        "tasks_ready": graph.tasks_ready,
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


# ---------------------------------------------------------------------------
# Public serialization functions
# ---------------------------------------------------------------------------


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
