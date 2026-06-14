"""Pure-data aggregation helpers for report templates.

Extracted from ``reports/collector.py`` (TNQA 10 H1) — all ``@staticmethod``
``_compute_*`` methods that were in ``ReportDataCollector``, plus the module-level
helpers they depend on.  Every function here is pure (no I/O, no subprocess).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agentshore.reports.types import (
    ActiveAgentEntry,
    AgentPerformanceData,
    AgentSpecializationData,
    AlignmentTrajectoryEntry,
    BudgetSufficiencyEntry,
    ClosedIssueEntry,
    ClosureByPlayTypeEntry,
    ControlRejectionStatsEntry,
    CostBreakdownData,
    EpicClosureTimelineData,
    EpicSummary,
    FailureAnalysisEntry,
    FailurePlayEntry,
    IssueInflationData,
    IssueThroughputData,
    LearningsDiffData,
    OverviewData,
    PlayLogColumnEntry,
    PlayLogRowEntry,
    PlayStatsEntry,
    PlayTimelineEntry,
    ReviewPatternEntry,
    TrajectoryAnalysisData,
    TrajectorySnapshotEntry,
)
from agentshore.rl.action_space import PLAY_TO_INDEX, RESERVED_PLAYS
from agentshore.state import INTERNAL_PLAY_TYPES, PlayType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentshore.beads import ProjectGraph
    from agentshore.data.store import (
        AgentRecord,
        GitHubIssueRecord,
        PlayRecord,
        ReviewFeedbackPatternRecord,
        ScopeDriftRecord,
        SessionLearningRecord,
        SessionRecord,
        TrajectorySnapshotRecord,
    )

# ---------------------------------------------------------------------------
# Module-level constants and small helpers shared across aggregators
# ---------------------------------------------------------------------------

_INTERNAL_PLAY_VALUES = frozenset(pt.value for pt in INTERNAL_PLAY_TYPES)

# Presentation metadata for the ESR play-log table — the ONLY hand-maintained
# part. Each entry is ``PlayType -> (display label, lifecycle phase)`` in the
# exact render order the play log uses (grouped by phase, not by action index).
# The action index (``PLAY_TO_INDEX``), play set (``PlayType``), and future flag
# (``RESERVED_PLAYS``) are all derived from ``rl/action_space`` below, so an
# action-space rev no longer silently drifts this table.
_PLAY_LOG_PRESENTATION: tuple[tuple[PlayType, str, int], ...] = (
    (PlayType.INSTANTIATE_AGENT, "INSTANTIATE_AGENT", 1),
    (PlayType.SEED_PROJECT, "SEED_PROJECT", 1),
    (PlayType.DESIGN_AUDIT, "DESIGN_AUDIT", 1),
    (PlayType.REFINE_TASK_BREAKDOWN, "REFINE_TASK_BREAKDOWN", 1),
    (PlayType.GROOM_BACKLOG, "GROOM_BACKLOG", 1),
    (PlayType.CALIBRATE_ALIGNMENT, "CALIBRATE_ALIGNMENT", 1),
    (PlayType.WRITE_IMPLEMENTATION_PLAN, "WRITE_IMPLEMENTATION_PLAN", 2),
    (PlayType.ISSUE_PICKUP, "ISSUE_PICKUP", 2),
    (PlayType.UNBLOCK_PR, "UNBLOCK_PR", 2),
    (PlayType.SYSTEMATIC_DEBUGGING, "SYSTEMATIC_DEBUGGING", 2),
    (PlayType.CODE_REVIEW, "CODE_REVIEW", 3),
    (PlayType.RUN_QA, "RUN_QA", 3),
    (PlayType.MERGE_PR, "MERGE_PR", 4),
    (PlayType.CLEANUP, "CLEANUP", 4),
    (PlayType.TAKE_BREAK, "TAKE_BREAK", 5),
    (PlayType.RECONCILE_STATE, "RECONCILE_STATE", 5),
    (PlayType.PRUNE, "PRUNE", 6),
    (PlayType.FUTURE_4, "FUTURE_4", 6),
    (PlayType.FUTURE_7, "FUTURE_7", 6),
    (PlayType.FUTURE_8, "FUTURE_8", 6),
    (PlayType.END_AGENT, "END_AGENT", 7),
    (PlayType.END_SESSION, "END_SESSION", 7),
)

# Loud guard: the presentation table must cover exactly the user-facing action
# space. A future action-space rev that adds/removes/renames a play (without
# updating this table) fails at import time rather than silently dropping a row.
_EXPECTED_PLAY_LOG_KEYS = set(PlayType) - INTERNAL_PLAY_TYPES
if {pt for pt, _, _ in _PLAY_LOG_PRESENTATION} != _EXPECTED_PLAY_LOG_KEYS:
    msg = (
        "_PLAY_LOG_PRESENTATION keys must equal set(PlayType) - INTERNAL_PLAY_TYPES; "
        "an action-space rev changed the play set — update the presentation table."
    )
    raise ValueError(msg)

# Derived play-log order: ``(play_type_str, label, action_index, phase, is_future)``.
# Index from ``PLAY_TO_INDEX``, future flag from ``RESERVED_PLAYS`` — both single-
# sourced from ``rl/action_space``. Consumers below read this exactly as before.
_PLAY_LOG_ORDER: tuple[tuple[str, str, int, int, bool], ...] = tuple(
    (play_type.value, label, PLAY_TO_INDEX[play_type], phase, play_type in RESERVED_PLAYS)
    for play_type, label, phase in _PLAY_LOG_PRESENTATION
)

_AGENTSHORE_SOURCE_LABEL_PREFIX = "agentshore/source:"
_LEGACY_AGENTSHORE_SOURCE_LABELS: frozenset[str] = frozenset(
    {
        "agentshore/cleanup",
        "agentshore/follow-up",
        "agentshore/intake",
        "agentshore/qa",
        "agentshore/review",
        "agentshore/slop",
    }
)


def _is_issue_source_label(label: str) -> bool:
    return (
        label.startswith(_AGENTSHORE_SOURCE_LABEL_PREFIX)
        or label in _LEGACY_AGENTSHORE_SOURCE_LABELS
    )


_AGENT_TYPE_LABEL: dict[str, str] = {
    "claude_code": "Claude",
    "codex": "Codex",
    "gemini": "Gemini",
    "grok": "Grok",
}


def _is_skip(play: PlayRecord) -> bool:
    """True for a play that was PPO-selected then gated — never dispatched to an agent.

    Skips are persisted with ``success=False`` and ``failure_category="skip:<kind>"``
    (see ``Executor._record_pre_dispatch_skip``). They are *not* failed plays and were
    never assigned to an agent, so report surfaces must not render them as FAIL or
    attribute them to "agentshore".
    """
    return (play.failure_category or "").startswith("skip:")


def _format_agent_label(
    agent_id: str | None,
    agents: dict[str, AgentRecord],
) -> str:
    """Format an agent_id for display in the ESR Play Log.

    Preference order (desktop-j8b):
      1. Persisted display_name when both display_name and model_tier are
         set (post-schema-migration agents).
      2. ``<Type>:<6-char-uuid-suffix>`` when the agent record exists but
         lacks the persisted fields (back-compat for older DBs).
      3. Bare ``agent_id`` if no AgentRecord is found at all.
      4. ``"—"`` if agent_id is None — orchestrator-internal plays
         (e.g. failed ``instantiate_agent``) that were never dispatched to
         a CLI agent.
    """
    if agent_id is None:
        return "—"
    record = agents.get(agent_id)
    if record is None:
        return agent_id
    if record.display_name:
        return record.display_name
    type_label = _AGENT_TYPE_LABEL.get(record.agent_type, record.agent_type)
    short = agent_id[-6:] if len(agent_id) >= 6 else agent_id
    if record.model_tier:
        return f"{type_label}/{record.model_tier}:{short}"
    return f"{type_label}:{short}"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_within_boundary(ts: datetime, start: datetime | None, end: datetime) -> bool:
    if start is None:
        return ts <= end
    return start < ts <= end


@dataclass(slots=True)
class _PlayStatsAccumulator:
    total: int = 0
    successful: int = 0
    skipped: int = 0
    total_cost: float = 0.0
    total_duration_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Aggregator functions (one per former @staticmethod on ReportDataCollector)
# ---------------------------------------------------------------------------


def compute_overview(session: SessionRecord, plays: list[PlayRecord]) -> OverviewData:
    """Compute high-level session overview from session record and plays."""
    duration = 0.0
    if session.started_at and session.ended_at:
        try:
            start = datetime.fromisoformat(session.started_at)
            end = datetime.fromisoformat(session.ended_at)
            duration = (end - start).total_seconds()
        except ValueError:
            pass

    successful = sum(1 for p in plays if p.success)
    skipped = sum(1 for p in plays if _is_skip(p))
    # Skips are gated no-ops, not failures — exclude them from the failed count.
    failed = len(plays) - successful - skipped

    return OverviewData(
        session_id=session.session_id,
        duration_seconds=duration,
        total_plays=len(plays),
        successful_plays=successful,
        failed_plays=failed,
        skipped_plays=skipped,
        # Single, self-consistent definition: sum the per-play costs (the
        # same rows the play log renders) rather than session.total_cost, so
        # every report (session summary, end-of-session, comparison) agrees.
        total_cost=sum(p.dollar_cost for p in plays),
        final_alignment=session.final_alignment,
        started_at=session.started_at,
        ended_at=session.ended_at,
    )


def compute_play_timeline(plays: list[PlayRecord]) -> list[PlayTimelineEntry]:
    """Convert PlayRecords to timeline entries (already ordered by play_id)."""
    result: list[PlayTimelineEntry] = []
    for p in plays:
        duration_s = (p.duration_ms / 1000.0) if p.duration_ms is not None else 0.0
        result.append(
            PlayTimelineEntry(
                play_id=p.play_id if p.play_id is not None else 0,
                play_type=p.play_type,
                agent_id=p.agent_id,
                success=p.success,
                duration_seconds=duration_s,
                dollar_cost=p.dollar_cost,
                alignment_delta=p.alignment_delta if p.alignment_delta is not None else 0.0,
                error=p.error,
                started_at=p.started_at,
            )
        )
    return result


def compute_play_stats(plays: list[PlayRecord]) -> list[PlayStatsEntry]:
    """Aggregate user-facing play stats, excluding ``INTERNAL_PLAY_TYPES``."""
    by_type: dict[str, _PlayStatsAccumulator] = defaultdict(_PlayStatsAccumulator)
    for play in plays:
        if play.play_type in _INTERNAL_PLAY_VALUES:
            continue
        acc = by_type[play.play_type]
        acc.total += 1
        acc.successful += int(play.success)
        acc.skipped += int(_is_skip(play))
        acc.total_cost += play.dollar_cost
        acc.total_duration_seconds += (
            (play.duration_ms / 1000.0) if play.duration_ms is not None else 0.0
        )

    result: list[PlayStatsEntry] = []
    for play_type, acc in by_type.items():
        # Skips are gated no-ops, not failures — report them in their own bucket
        # and keep success_rate over real (dispatched) attempts only.
        failed = acc.total - acc.successful - acc.skipped
        dispatched = acc.total - acc.skipped
        result.append(
            PlayStatsEntry(
                play_type=play_type,
                total=acc.total,
                successful=acc.successful,
                failed=failed,
                skipped=acc.skipped,
                success_rate=acc.successful / max(dispatched, 1),
                total_cost=acc.total_cost,
                avg_duration_seconds=acc.total_duration_seconds / max(acc.total, 1),
            )
        )
    return sorted(result, key=lambda row: (-row["total"], row["play_type"]))


def compute_control_rejections(
    mutations: Sequence[object],
) -> list[ControlRejectionStatsEntry]:
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for mutation in mutations:
        kind = str(getattr(mutation, "mutation_type", "unknown"))
        play_type = str(getattr(mutation, "target", "unknown"))
        reason = "unknown"
        request_json = getattr(mutation, "request_json", None)
        if isinstance(request_json, str) and request_json:
            try:
                payload = json.loads(request_json)
            except json.JSONDecodeError:
                payload = {}
            raw_reason = payload.get("reason") if isinstance(payload, dict) else None
            if isinstance(raw_reason, str) and raw_reason:
                reason = raw_reason
        counts[(kind, play_type, reason)] += 1

    return [
        ControlRejectionStatsEntry(
            kind=kind,
            play_type=play_type,
            reason=reason,
            count=count,
        )
        for (kind, play_type, reason), count in sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1], item[0][2]),
        )
    ]


def compute_play_log_columns() -> list[PlayLogColumnEntry]:
    """Return lifecycle-ordered ESR play-log columns."""
    result: list[PlayLogColumnEntry] = []
    previous_phase: int | None = None
    for play_type, label, action_index, phase, future in _PLAY_LOG_ORDER:
        if play_type in _INTERNAL_PLAY_VALUES:
            continue
        result.append(
            PlayLogColumnEntry(
                play_type=play_type,
                label=label,
                action_index=action_index,
                phase=phase,
                phase_start=phase != previous_phase,
                future=future,
            )
        )
        previous_phase = phase
    return result


def compute_play_log_rows(
    plays: list[PlayRecord],
    agents: Sequence[AgentRecord] | None = None,
) -> list[PlayLogRowEntry]:
    """Convert play records into ESR rows with best-known agent display names."""
    agent_lookup: dict[str, AgentRecord] = {}
    if agents is not None:
        agent_lookup = {a.agent_id: a for a in agents}

    rows: list[PlayLogRowEntry] = []
    row_number = 0
    for play in plays:
        if play.play_type in _INTERNAL_PLAY_VALUES:
            continue
        # A gated/skipped play was PPO-selected then masked before dispatch —
        # it never reached an agent (0ms, $0, agent_id=None). It is not an
        # executed play, so it has no place in the play-log timeline. The
        # per-play-type stats table still accounts for it via its ``skipped``
        # bucket; here it is omitted entirely.
        if _is_skip(play):
            continue
        row_number += 1
        duration_s = (play.duration_ms / 1000.0) if play.duration_ms is not None else 0.0
        status = "ok" if play.success else "fail"
        agent_name = _format_agent_label(play.agent_id, agent_lookup)
        rows.append(
            PlayLogRowEntry(
                row_number=row_number,
                play_id=play.play_id if play.play_id is not None else 0,
                play_type=play.play_type,
                agent_name=agent_name,
                success=play.success,
                status=status,
                started_at=play.started_at,
                duration_seconds=duration_s,
                dollar_cost=play.dollar_cost,
                error=play.error,
            )
        )
    return rows


def compute_play_log_unique_agents(plays: list[PlayRecord]) -> int:
    """Count distinct concrete agents represented in the play log."""
    return len({play.agent_id for play in plays if play.agent_id is not None})


def compute_play_log_plays_in_use(plays: list[PlayRecord]) -> int:
    """Count distinct active play types used, excluding reserved future
    slots and internal plays (``INTERNAL_PLAY_TYPES``).
    """
    future = {play_type for play_type, _, _, _, is_future in _PLAY_LOG_ORDER if is_future}
    excluded = future | _INTERNAL_PLAY_VALUES
    return len({play.play_type for play in plays if play.play_type not in excluded})


def compute_play_log_total_slots() -> int:
    """Total user-facing play slots = registry minus internal heartbeats.

    The ESR header renders "<plays_in_use> / <total_slots>". Previously
    this denominator was hardcoded to 17 in the Jinja template (which
    was wrong both pre- and post-INTERNAL filter). Now computed from
    the registry so it stays correct if the action space grows again.
    """
    return sum(1 for pt, *_ in _PLAY_LOG_ORDER if pt not in _INTERNAL_PLAY_VALUES)


def compute_closed_issues(
    session: SessionRecord,
    issues: Sequence[GitHubIssueRecord],
) -> list[ClosedIssueEntry]:
    """Return issues closed inside this session's time window."""
    started = _parse_iso(session.started_at)

    closed: list[ClosedIssueEntry] = []
    for issue in issues:
        if issue.state.lower() != "closed" or issue.closed_at is None:
            continue
        closed_at = _parse_iso(issue.closed_at)
        if closed_at is None:
            continue
        if started is not None and closed_at < started:
            continue
        closed.append(
            ClosedIssueEntry(
                issue_number=issue.issue_number,
                title=issue.title,
                closed_at=issue.closed_at,
                labels=issue.labels,
            )
        )
    return sorted(closed, key=lambda row: row["issue_number"])


def compute_cost_breakdown(
    plays: list[PlayRecord],
    agents: list[AgentRecord],
) -> CostBreakdownData:
    """Group costs by play type and agent; compute cumulative."""
    by_type: dict[str, float] = {}
    by_agent: dict[str, float] = {}
    cumulative: list[tuple[int, float]] = []
    running = 0.0

    for idx, p in enumerate(plays):
        by_type[p.play_type] = by_type.get(p.play_type, 0.0) + p.dollar_cost
        if p.agent_id is not None:
            by_agent[p.agent_id] = by_agent.get(p.agent_id, 0.0) + p.dollar_cost
        running += p.dollar_cost
        cumulative.append((idx, running))

    # Fill in agents that may not have plays
    for a in agents:
        if a.agent_id not in by_agent:
            by_agent[a.agent_id] = a.total_cost

    return CostBreakdownData(
        by_play_type=by_type,
        by_agent=by_agent,
        cumulative=cumulative,
    )


def compute_agent_performance(
    agents: list[AgentRecord],
    plays: list[PlayRecord],
) -> list[AgentPerformanceData]:
    """Compute per-agent performance metrics.

    desktop-31h2: also computes ``dispatch_share`` per agent — the agent's
    share of the fleet-wide cumulative dispatch_count. 0.0 when no plays
    have been dispatched yet (avoids divide-by-zero). Surfaces fleet
    utilisation imbalance where some agents get 0 plays over long
    stretches while work is available.
    """
    # Gather durations from plays per agent
    durations_by_agent: dict[str, list[float]] = {}
    for p in plays:
        if p.agent_id is not None:
            dur = (p.duration_ms / 1000.0) if p.duration_ms is not None else 0.0
            durations_by_agent.setdefault(p.agent_id, []).append(dur)

    total_dispatches = sum(a.dispatch_count for a in agents)

    result: list[AgentPerformanceData] = []
    for a in agents:
        total_tasks = a.tasks_completed + a.tasks_failed
        success_rate = a.tasks_completed / max(total_tasks, 1)
        agent_durations = durations_by_agent.get(a.agent_id, [])
        avg_dur = sum(agent_durations) / len(agent_durations) if agent_durations else 0.0
        dispatch_share = a.dispatch_count / total_dispatches if total_dispatches > 0 else 0.0
        result.append(
            AgentPerformanceData(
                agent_id=a.agent_id,
                agent_type=a.agent_type,
                tasks_completed=a.tasks_completed,
                tasks_failed=a.tasks_failed,
                success_rate=success_rate,
                total_cost=a.total_cost,
                avg_duration=avg_dur,
                dispatch_count=a.dispatch_count,
                dispatch_share=dispatch_share,
            )
        )
    return result


def compute_agent_specialization(
    plays: list[PlayRecord],
) -> list[AgentSpecializationData]:
    """Break out per-agent success rates by play type from existing play history.

    Mirrors the matrix surfaced via ``SessionStatsSnapshot.agent_specialization``
    but produces a plain-dict shape ready for Jinja2 templates. Plays with no
    ``agent_id`` are skipped because there is nothing to attribute them to.
    """
    from agentshore.rl.metrics import compute_agent_specialization as _compute_specialization
    from agentshore.state import PlayType as _PlayType

    cells = _compute_specialization(plays)
    result: list[AgentSpecializationData] = []
    for cell in cells:
        play_type = (
            cell.play_type.value if isinstance(cell.play_type, _PlayType) else cell.play_type
        )
        result.append(
            AgentSpecializationData(
                agent_id=cell.agent_id,
                play_type=play_type,
                total=cell.total,
                successful=cell.successful,
                failed=cell.failed,
                success_rate=cell.success_rate,
                rolling_success_rate=cell.rolling_success_rate,
            )
        )
    return result


def compute_failure_analysis(plays: list[PlayRecord]) -> list[FailureAnalysisEntry]:
    """Group failed plays by failure category."""
    by_category: dict[str, list[FailurePlayEntry]] = {}
    for p in plays:
        # Skips are gated no-ops (failure_category="skip:*"), not failures.
        if not p.success and not _is_skip(p):
            cat = p.failure_category or "unknown"
            entry = FailurePlayEntry(
                play_id=p.play_id if p.play_id is not None else 0,
                play_type=p.play_type,
                error=p.error,
                agent_id=p.agent_id,
            )
            by_category.setdefault(cat, []).append(entry)

    return [
        FailureAnalysisEntry(category=cat, count=len(entries), plays=entries)
        for cat, entries in by_category.items()
    ]


def compute_anti_confirmation_audit(plays: list[PlayRecord]) -> int:
    """Count CODE_REVIEW violations against the preceding ISSUE_PICKUP play.

    QA is not identity-blocked in the current implementation.
    """
    violations = 0
    last_pickup_agent: str | None = None
    for p in plays:
        if p.play_type == "issue_pickup":
            last_pickup_agent = p.agent_id
        elif (
            p.play_type == "code_review"
            and p.agent_id is not None
            and last_pickup_agent is not None
            and p.agent_id == last_pickup_agent
        ):
            violations += 1
    return violations


def compute_scope_drift(drifts: list[ScopeDriftRecord]) -> int:
    """Count scope drift entries."""
    return len(drifts)


def compute_issue_inflation(
    issues: Sequence[GitHubIssueRecord],
    plays: Sequence[PlayRecord],
) -> IssueInflationData:
    """Compute issue inflation metrics across issue snapshots and play boundaries."""
    total_opened = 0
    total_closed = 0
    by_source: Counter[str] = Counter()
    for issue in issues:
        if issue.state == "open":
            total_opened += 1
        elif issue.state == "closed":
            total_closed += 1
        for label in issue.labels:
            if _is_issue_source_label(label):
                by_source[label] += 1
    ratio = total_opened / max(total_closed, 1)
    issue_boundaries = [
        (_parse_iso(issue.created_at), _parse_iso(issue.closed_at)) for issue in issues
    ]
    sorted_plays = sorted(plays, key=lambda p: (p.play_id or 0, p.started_at))

    per_play: list[tuple[int, int, int, int]] = []
    prev_boundary: datetime | None = None
    cumulative = 0
    peak = 0
    peak_index = 0
    for idx, play in enumerate(sorted_plays, start=1):
        boundary = _parse_iso(play.ended_at) or _parse_iso(play.started_at)
        opened_count = 0
        closed_count = 0
        if boundary is not None:
            for created_at, closed_at in issue_boundaries:
                if created_at is not None and _is_within_boundary(
                    created_at, prev_boundary, boundary
                ):
                    opened_count += 1
                if closed_at is not None and _is_within_boundary(
                    closed_at, prev_boundary, boundary
                ):
                    closed_count += 1
        net_open = opened_count - closed_count
        cumulative += net_open
        if cumulative > peak:
            peak = cumulative
            peak_index = idx
        per_play.append((idx, opened_count, closed_count, net_open))
        prev_boundary = boundary

    warnings_triggered = 0
    streak = 0
    for _, _, _, net_open in per_play:
        if net_open > 0:
            streak += 1
        else:
            if streak >= 5:
                warnings_triggered += 1
            streak = 0
    if streak >= 5:
        warnings_triggered += 1

    post_peak = per_play[peak_index:] if peak_index > 0 else []
    recovery_plays = [idx for idx, _, _, net_open in post_peak if net_open < 0]
    recovery_reversed = bool(peak_index > 0 and recovery_plays and cumulative < peak)
    return IssueInflationData(
        total_opened=total_opened,
        total_closed=total_closed,
        ratio=ratio,
        per_play=per_play,
        warnings_triggered=warnings_triggered,
        by_source=dict(sorted(by_source.items())),
        recovery={
            "reversed": recovery_reversed,
            "contributing_plays": recovery_plays,
        },
    )


def compute_issue_throughput(
    issues: Sequence[GitHubIssueRecord],
) -> IssueThroughputData:
    opened = sum(1 for i in issues if i.state == "open")
    closed = sum(1 for i in issues if i.state == "closed")
    return IssueThroughputData(opened=opened, closed=closed, net_velocity=closed - opened)


def compute_play_distribution(plays: list[PlayRecord]) -> dict[str, int]:
    dist: dict[str, int] = {}
    for p in plays:
        dist[p.play_type] = dist.get(p.play_type, 0) + 1
    return dist


def compute_learnings_diff(
    learnings_a: Sequence[SessionLearningRecord],
    learnings_b: Sequence[SessionLearningRecord],
) -> LearningsDiffData:
    patterns_a = {lr.pattern for lr in learnings_a}
    patterns_b = {lr.pattern for lr in learnings_b}
    return LearningsDiffData(
        added=sorted(patterns_b - patterns_a),
        removed=sorted(patterns_a - patterns_b),
        shared=sorted(patterns_a & patterns_b),
    )


def compute_alignment_trajectory(plays: list[PlayRecord]) -> list[AlignmentTrajectoryEntry]:
    trajectory: list[AlignmentTrajectoryEntry] = []
    running = 0.0
    for idx, p in enumerate(plays):
        running += p.alignment_delta if p.alignment_delta is not None else 0.0
        trajectory.append(AlignmentTrajectoryEntry(play_index=idx, alignment=running))
    return trajectory


def compute_trajectory(
    snapshots: list[TrajectorySnapshotRecord],
) -> list[TrajectorySnapshotEntry]:
    """Convert trajectory snapshots to plain dicts."""
    return [
        TrajectorySnapshotEntry(
            play_id=s.play_id,
            projected_alignment=s.projected_alignment_at_budget_end,
            remaining_plays=s.estimated_remaining_plays,
            remaining_cost=s.estimated_remaining_cost,
            created_at=s.created_at,
        )
        for s in snapshots
    ]


def compute_trajectory_analysis(
    snapshots: list[TrajectorySnapshotRecord],
    plays: list[PlayRecord],
    actual_total_cost: float,
) -> TrajectoryAnalysisData:
    if not snapshots:
        return TrajectoryAnalysisData(
            trend="flat",
            estimated_total_cost_early=actual_total_cost,
            actual_total_cost=actual_total_cost,
            budget_sufficiency=[
                {"budget_consumed_pct": 25.0, "projected_sufficient": False},
                {"budget_consumed_pct": 50.0, "projected_sufficient": False},
                {"budget_consumed_pct": 75.0, "projected_sufficient": False},
            ],
        )

    ordered = sorted(snapshots, key=lambda s: s.play_id)
    first = ordered[0]
    last = ordered[-1]

    if last.projected_alignment_at_budget_end > first.projected_alignment_at_budget_end:
        trend = "converging"
    elif last.projected_alignment_at_budget_end < first.projected_alignment_at_budget_end:
        trend = "diverging"
    else:
        trend = "flat"

    current_cost_at_first_snapshot = sum(
        p.dollar_cost for p in plays if p.play_id is not None and p.play_id <= first.play_id
    )
    estimated_total_cost_early = first.estimated_remaining_cost + current_cost_at_first_snapshot
    estimated_total_cost_early = max(estimated_total_cost_early, 0.0)

    checkpoints: list[BudgetSufficiencyEntry] = []
    for checkpoint in (25.0, 50.0, 75.0):
        threshold = checkpoint / 100.0
        selected = ordered[-1]
        for snapshot in ordered:
            consumed_pct = 0.0
            if estimated_total_cost_early > 0:
                consumed_pct = max(
                    0.0,
                    min(
                        1.0,
                        (estimated_total_cost_early - snapshot.estimated_remaining_cost)
                        / estimated_total_cost_early,
                    ),
                )
            if consumed_pct >= threshold:
                selected = snapshot
                break

        remaining_budget = max(0.0, estimated_total_cost_early * (1.0 - threshold))
        checkpoints.append(
            {
                "budget_consumed_pct": checkpoint,
                "projected_sufficient": selected.estimated_remaining_cost <= remaining_budget,
            }
        )

    return TrajectoryAnalysisData(
        trend=trend,
        estimated_total_cost_early=estimated_total_cost_early,
        actual_total_cost=actual_total_cost,
        budget_sufficiency=checkpoints,
    )


def compute_knowledge(learnings: Sequence[SessionLearningRecord]) -> int:
    """Count session learnings."""
    return len(learnings)


def compute_cleanup_history(plays: list[PlayRecord]) -> int:
    """Count CLEANUP plays."""
    return sum(1 for p in plays if p.play_type == "cleanup")


def compute_review_patterns(
    patterns: list[ReviewFeedbackPatternRecord],
) -> list[ReviewPatternEntry]:
    """Convert review feedback patterns to plain dicts."""
    return [
        ReviewPatternEntry(
            pattern=p.pattern,
            category=p.category,
            frequency=p.frequency,
            injected=p.injected,
        )
        for p in patterns
    ]


def compute_recommendations(
    plays: list[PlayRecord],
    agents: list[AgentRecord],
) -> list[str]:
    """Generate simple heuristic-based recommendations."""
    recs: list[str] = []

    # High failure rate agents
    for a in agents:
        total = a.tasks_completed + a.tasks_failed
        if total > 0:
            fail_rate = a.tasks_failed / total
            if fail_rate > 0.3:
                pct = int(fail_rate * 100)
                recs.append(
                    f"Agent {a.agent_id!r} had {pct}% failure rate "
                    f"({a.tasks_failed}/{total}) — consider switching agent type"
                )

    # High revert ratio
    total_plays = len(plays)
    cleanups = sum(1 for p in plays if p.play_type == "cleanup")
    if total_plays > 0 and cleanups / total_plays > 0.15:
        recs.append(
            f"High cleanup rate ({cleanups}/{total_plays}) — "
            f"consider reducing the cleanup cooldown or investigating recurring lint failures"
        )

    # High overall failure rate
    failures = sum(1 for p in plays if not p.success)
    if total_plays > 0 and failures / total_plays > 0.4:
        recs.append(
            f"Overall failure rate is {int(failures / total_plays * 100)}% — "
            f"investigate root causes"
        )

    return recs


def compute_epic_summaries(graph: ProjectGraph | None) -> list[EpicSummary]:
    if graph is None:
        return []
    return [
        EpicSummary(
            bead_id=epic.bead_id,
            title=epic.title,
            closure_ratio=epic.closure_ratio,
            total_tasks=epic.total_tasks,
            closed_tasks=epic.closed_tasks,
        )
        for epic in graph.epics
    ]


def compute_epic_closure_timeline(
    graph: ProjectGraph | None,
    plays: list[PlayRecord],
) -> EpicClosureTimelineData:
    if graph is None:
        return EpicClosureTimelineData(
            global_ratio_start=0.0,
            global_ratio_midpoint=0.0,
            global_ratio_end=0.0,
            tasks_closed_by_play_type=[],
        )

    deltas = [
        play.alignment_delta
        for play in plays
        if play.alignment_delta is not None and play.alignment_delta > 0
    ]
    cumulative_gain = sum(deltas)
    end_ratio = graph.global_closure_ratio
    start_ratio = max(0.0, end_ratio - cumulative_gain)
    midpoint_ratio = min(end_ratio, start_ratio + (cumulative_gain / 2.0))

    tracked_types = ("issue_pickup", "merge_pr", "run_qa")
    closed_total = sum(epic.closed_tasks for epic in graph.epics)
    total_delta = sum(
        play.alignment_delta
        for play in plays
        if play.alignment_delta is not None and play.play_type in tracked_types
    )

    by_play: list[ClosureByPlayTypeEntry] = []
    for play_type in tracked_types:
        typed_plays = [play for play in plays if play.play_type == play_type]
        typed_delta = sum(play.alignment_delta or 0.0 for play in typed_plays)
        if total_delta > 0 and typed_delta > 0:
            estimated_closed = round(closed_total * (typed_delta / total_delta))
        else:
            estimated_closed = 0
        by_play.append(
            ClosureByPlayTypeEntry(
                play_type=play_type,
                plays_executed=len(typed_plays),
                estimated_tasks_closed=max(0, estimated_closed),
            )
        )

    return EpicClosureTimelineData(
        global_ratio_start=max(0.0, min(1.0, start_ratio)),
        global_ratio_midpoint=max(0.0, min(1.0, midpoint_ratio)),
        global_ratio_end=max(0.0, min(1.0, end_ratio)),
        tasks_closed_by_play_type=by_play,
    )


# Re-export the ActiveAgentEntry TypedDict that collect_progress_report uses inline
__all__ = ["ActiveAgentEntry"]
