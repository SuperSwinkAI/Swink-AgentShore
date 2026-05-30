"""ReportDataCollector — pure-data aggregation for report templates.

Queries the DataStore and returns pre-computed dicts (TypedDicts) ready
for Jinja2 template rendering.  No dependency on Jinja2, TUI, IPC, or RL.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agentshore.beads import ProjectGraph, load_graph
from agentshore.reports.types import (
    ActiveAgentEntry,
    AgentPerformanceData,
    AgentSpecializationData,
    AlignmentTrajectoryEntry,
    BudgetSufficiencyEntry,
    ClosedIssueEntry,
    ClosureByPlayTypeEntry,
    ClusterAlignmentData,
    ComparisonData,
    ControlRejectionStatsEntry,
    CostBreakdownData,
    EndSessionReportData,
    EpicClosureTimelineData,
    EpicSummary,
    FailureAnalysisEntry,
    FailurePlayEntry,
    IssueInflationData,
    IssueThroughputData,
    LearningsDiffData,
    LoopIncidentEntry,
    OverviewData,
    PlayLogColumnEntry,
    PlayLogRowEntry,
    PlayStatsEntry,
    PlayTimelineEntry,
    ProgressReportData,
    ReviewPatternEntry,
    SessionSummaryData,
    TrajectoryAnalysisData,
    TrajectorySnapshotEntry,
)
from agentshore.state import INTERNAL_PLAY_TYPES

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentshore.data.store import (
        AgentRecord,
        DataStore,
        GitHubIssueRecord,
        PlayRecord,
        ReviewFeedbackPatternRecord,
        ScopeDriftRecord,
        SessionLearningRecord,
        SessionRecord,
        TrajectorySnapshotRecord,
    )

# Re-export all TypedDicts so that existing ``from agentshore.reports.collector import ...``
# statements continue to work.
__all__ = [
    "ActiveAgentEntry",
    "AgentPerformanceData",
    "AgentSpecializationData",
    "AlignmentTrajectoryEntry",
    "ClosedIssueEntry",
    "ClusterAlignmentData",
    "ComparisonData",
    "ControlRejectionStatsEntry",
    "CostBreakdownData",
    "EndSessionReportData",
    "EpicSummary",
    "FailureAnalysisEntry",
    "FailurePlayEntry",
    "IssueInflationData",
    "IssueThroughputData",
    "LearningsDiffData",
    "LoopIncidentEntry",
    "OverviewData",
    "PlayLogColumnEntry",
    "PlayLogRowEntry",
    "PlayStatsEntry",
    "PlayTimelineEntry",
    "ProgressReportData",
    "ReportDataCollector",
    "ReviewPatternEntry",
    "SessionSummaryData",
    "TrajectoryAnalysisData",
    "TrajectorySnapshotEntry",
]


@dataclass(slots=True)
class _PlayStatsAccumulator:
    total: int = 0
    successful: int = 0
    total_cost: float = 0.0
    total_duration_seconds: float = 0.0


_PLAY_LOG_ORDER: tuple[tuple[str, str, int, int, bool], ...] = (
    ("instantiate_agent", "INSTANTIATE_AGENT", 0, 1, False),
    ("seed_project", "SEED_PROJECT", 17, 1, False),
    ("design_audit", "DESIGN_AUDIT", 9, 1, False),
    ("refine_task_breakdown", "REFINE_TASK_BREAKDOWN", 12, 1, False),
    ("groom_backlog", "GROOM_BACKLOG", 16, 1, False),
    ("calibrate_alignment", "CALIBRATE_ALIGNMENT", 18, 1, False),
    ("write_implementation_plan", "WRITE_IMPLEMENTATION_PLAN", 2, 2, False),
    ("issue_pickup", "ISSUE_PICKUP", 4, 2, False),
    ("unblock_pr", "UNBLOCK_PR", 1, 2, False),
    ("systematic_debugging", "SYSTEMATIC_DEBUGGING", 8, 2, False),
    ("code_review", "CODE_REVIEW", 5, 3, False),
    ("run_qa", "RUN_QA", 7, 3, False),
    ("browser_verification", "BROWSER_VERIFICATION", 14, 3, False),
    ("merge_pr", "MERGE_PR", 6, 4, False),
    ("cleanup", "CLEANUP", 13, 4, False),
    ("take_break", "TAKE_BREAK", 15, 5, False),
    ("reconcile_state", "RECONCILE_STATE", 11, 5, False),
    ("prune", "PRUNE", 19, 6, False),
    ("future_7", "FUTURE_7", 20, 6, True),
    ("future_8", "FUTURE_8", 21, 6, True),
    ("end_agent", "END_AGENT", 3, 7, False),
    ("end_session", "END_SESSION", 10, 7, False),
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
}


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
      4. The literal ``"agentshore"`` if agent_id is None — only happens for
         malformed plays since internal heartbeats are filtered upstream.
    """
    if agent_id is None:
        return "agentshore"
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


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class ReportDataCollector:
    """Pure-data aggregation layer between DataStore and report templates."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

    # -- public API ----------------------------------------------------------

    async def collect_session_summary(self, session_id: str) -> SessionSummaryData:
        """Collect all data needed for a full session summary report."""
        session = await self._store.get_session(session_id)
        if session is None:
            msg = f"Session {session_id!r} not found"
            raise ValueError(msg)

        plays = await self._store.get_play_history(session_id)
        agents = await self._store.get_agents(session_id)
        drifts = await self._store.list_scope_drift(session_id)
        issues = await self._store.list_all_issues(session_id)
        snapshots = await self._store.list_trajectory_snapshots(session_id)
        learnings = await self._store.list_learnings(session_id)
        patterns = await self._store.list_review_patterns(session_id)
        graph = await load_graph(Path(session.project_path))

        overview = self._compute_overview(session, plays)

        return SessionSummaryData(
            overview=overview,
            play_timeline=self._compute_play_timeline(plays),
            cost_breakdown=self._compute_cost_breakdown(plays, agents),
            agent_performance=self._compute_agent_performance(agents, plays),
            agent_specialization=self._compute_agent_specialization(plays),
            cluster_alignment=[],
            failure_analysis=self._compute_failure_analysis(plays),
            scope_drift_count=self._compute_scope_drift(drifts),
            anti_confirmation_violations=self._compute_anti_confirmation_audit(plays),
            issue_inflation=self._compute_issue_inflation(issues, plays),
            trajectory_snapshots=self._compute_trajectory(snapshots),
            trajectory_analysis=self._compute_trajectory_analysis(
                snapshots, plays, overview["total_cost"]
            ),
            learnings_count=self._compute_knowledge(learnings),
            revert_count=self._compute_cleanup_history(plays),
            loop_incidents=self._compute_loop_incidents(plays),
            review_patterns=self._compute_review_patterns(patterns),
            recommendations=self._compute_recommendations(plays, agents),
            epic_summaries=self._compute_epic_summaries(graph),
            epic_closure_timeline=self._compute_epic_closure_timeline(graph, plays),
        )

    async def collect_progress_report(self, session_id: str) -> ProgressReportData:
        """Collect data for a mid-session progress snapshot."""
        session = await self._store.get_session(session_id)
        if session is None:
            msg = f"Session {session_id!r} not found"
            raise ValueError(msg)

        plays = await self._store.get_play_history(session_id)
        agents = await self._store.get_agents(session_id)
        trajectory = await self._store.get_latest_trajectory(session_id)

        overview = self._compute_overview(session, plays)
        timeline = self._compute_play_timeline(plays)
        recent = timeline[-10:] if len(timeline) > 10 else timeline

        budget_remaining: float | None = None
        if trajectory is not None:
            budget_remaining = trajectory.estimated_remaining_cost

        active_agents: list[ActiveAgentEntry] = [
            ActiveAgentEntry(
                agent_id=a.agent_id,
                agent_type=a.agent_type,
                status="active",
                tasks_completed=a.tasks_completed,
                tasks_failed=a.tasks_failed,
                total_cost=a.total_cost,
            )
            for a in agents
            if a.terminated_at is None
        ]

        return ProgressReportData(
            overview=overview,
            cluster_alignment=[],
            recent_plays=recent,
            budget_remaining=budget_remaining,
            active_agents=active_agents,
        )

    async def collect_end_session_report(self, session_id: str) -> EndSessionReportData:
        """Collect the compact shutdown-time end-of-session report data."""
        session = await self._store.get_session(session_id)
        if session is None:
            msg = f"Session {session_id!r} not found"
            raise ValueError(msg)

        plays = await self._store.get_play_history(session_id)
        issues = await self._store.list_all_issues(session_id)
        control_rejections = await self._store.list_external_mutations(
            session_id,
            mutation_types=("dispatch_revalidation_block", "selector_rejection"),
        )
        agents = await self._store.get_agents(session_id)
        overview = self._compute_overview(session, plays)
        overview["total_cost"] = sum(p.dollar_cost for p in plays)

        return EndSessionReportData(
            overview=overview,
            repo_url=await self._resolve_repo_url(session.project_path, issues),
            play_stats=self._compute_play_stats(plays),
            control_rejections=self._compute_control_rejections(control_rejections),
            closed_issues=self._compute_closed_issues(session, issues),
            play_log_columns=self._compute_play_log_columns(),
            play_log_rows=self._compute_play_log_rows(plays, agents),
            play_log_unique_agents=self._compute_play_log_unique_agents(plays),
            play_log_plays_in_use=self._compute_play_log_plays_in_use(plays),
            play_log_total_slots=self._compute_play_log_total_slots(),
        )

    async def collect_comparison(self, id1: str, id2: str) -> ComparisonData:
        """Compare two sessions side-by-side."""
        session_a = await self._store.get_session(id1)
        session_b = await self._store.get_session(id2)
        if session_a is None:
            msg = f"Session {id1!r} not found"
            raise ValueError(msg)
        if session_b is None:
            msg = f"Session {id2!r} not found"
            raise ValueError(msg)

        plays_a = await self._store.get_play_history(id1)
        plays_b = await self._store.get_play_history(id2)
        agents_a = await self._store.get_agents(id1)
        agents_b = await self._store.get_agents(id2)
        issues_a = await self._store.list_all_issues(id1)
        issues_b = await self._store.list_all_issues(id2)
        learnings_a = await self._store.list_learnings(id1)
        learnings_b = await self._store.list_learnings(id2)

        ov_a = self._compute_overview(session_a, plays_a)
        ov_b = self._compute_overview(session_b, plays_b)

        alignment_a = ov_a["final_alignment"] if ov_a["final_alignment"] is not None else 0.0
        alignment_b = ov_b["final_alignment"] if ov_b["final_alignment"] is not None else 0.0

        return ComparisonData(
            session_a=ov_a,
            session_b=ov_b,
            cost_diff=ov_b["total_cost"] - ov_a["total_cost"],
            alignment_diff=alignment_b - alignment_a,
            play_count_diff=ov_b["total_plays"] - ov_a["total_plays"],
            cost_breakdown_a=self._compute_cost_breakdown(plays_a, agents_a),
            cost_breakdown_b=self._compute_cost_breakdown(plays_b, agents_b),
            issue_throughput_a=self._compute_issue_throughput(issues_a),
            issue_throughput_b=self._compute_issue_throughput(issues_b),
            play_distribution_a=self._compute_play_distribution(plays_a),
            play_distribution_b=self._compute_play_distribution(plays_b),
            learnings_diff=self._compute_learnings_diff(learnings_a, learnings_b),
            alignment_trajectory_a=self._compute_alignment_trajectory(plays_a),
            alignment_trajectory_b=self._compute_alignment_trajectory(plays_b),
        )

    # -- private aggregation helpers -----------------------------------------

    @staticmethod
    def _compute_overview(session: SessionRecord, plays: list[PlayRecord]) -> OverviewData:
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
        failed = len(plays) - successful

        return OverviewData(
            session_id=session.session_id,
            duration_seconds=duration,
            total_plays=len(plays),
            successful_plays=successful,
            failed_plays=failed,
            total_cost=session.total_cost,
            final_alignment=session.final_alignment,
            started_at=session.started_at,
            ended_at=session.ended_at,
        )

    @staticmethod
    def _compute_play_timeline(plays: list[PlayRecord]) -> list[PlayTimelineEntry]:
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

    @staticmethod
    def _compute_play_stats(plays: list[PlayRecord]) -> list[PlayStatsEntry]:
        """Aggregate play counts, success rates, cost, and average duration by play type.

        Plays whose ``play_type`` is in ``INTERNAL_PLAY_TYPES`` are excluded.
        The set is empty after desktop-rni0; the filter is retained for any
        future bookkeeping plays that should stay out of the ESR.
        """
        internal_play_values = {pt.value for pt in INTERNAL_PLAY_TYPES}
        by_type: dict[str, _PlayStatsAccumulator] = defaultdict(_PlayStatsAccumulator)
        for play in plays:
            if play.play_type in internal_play_values:
                continue
            acc = by_type[play.play_type]
            acc.total += 1
            acc.successful += int(play.success)
            acc.total_cost += play.dollar_cost
            acc.total_duration_seconds += (
                (play.duration_ms / 1000.0) if play.duration_ms is not None else 0.0
            )

        result: list[PlayStatsEntry] = []
        for play_type, acc in by_type.items():
            failed = acc.total - acc.successful
            result.append(
                PlayStatsEntry(
                    play_type=play_type,
                    total=acc.total,
                    successful=acc.successful,
                    failed=failed,
                    success_rate=acc.successful / max(acc.total, 1),
                    total_cost=acc.total_cost,
                    avg_duration_seconds=acc.total_duration_seconds / max(acc.total, 1),
                )
            )
        return sorted(result, key=lambda row: (-row["total"], row["play_type"]))

    @staticmethod
    def _compute_control_rejections(
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

    @staticmethod
    def _compute_play_log_columns() -> list[PlayLogColumnEntry]:
        """Return the lifecycle-ordered play columns used by the ESR play log.

        Internal plays (``INTERNAL_PLAY_TYPES``) are excluded — the play log
        is user-facing agent activity. After desktop-rni0 the registry has
        22 entries with 18 active and 4 reserved FUTURE_N slots.
        """
        internal_play_values = {pt.value for pt in INTERNAL_PLAY_TYPES}
        result: list[PlayLogColumnEntry] = []
        previous_phase: int | None = None
        for play_type, label, action_index, phase, future in _PLAY_LOG_ORDER:
            if play_type in internal_play_values:
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

    @staticmethod
    def _compute_play_log_rows(
        plays: list[PlayRecord],
        agents: Sequence[AgentRecord] | None = None,
    ) -> list[PlayLogRowEntry]:
        """Convert play records into compact one-row play-log entries.

        Internal plays (``INTERNAL_PLAY_TYPES``) are filtered out — they are
        orchestrator-only activity. The set is empty after desktop-rni0.

        Agent display names (desktop-j8b): renders ``<Type>/<tier>:
        <Name>`` (e.g. "Claude/large: Ember Raven") when the agents table
        has model_tier + display_name persisted; falls back to
        ``<Type>:<6-char-uuid-suffix>`` for old DBs whose agents were
        registered before the schema migration, and the bare agent_id as
        last resort. Empty agent_id (internal play) is impossible here
        because internals are already filtered above.
        """
        internal_play_values = {pt.value for pt in INTERNAL_PLAY_TYPES}
        agent_lookup: dict[str, AgentRecord] = {}
        if agents is not None:
            agent_lookup = {a.agent_id: a for a in agents}

        rows: list[PlayLogRowEntry] = []
        row_number = 0
        for play in plays:
            if play.play_type in internal_play_values:
                continue
            row_number += 1
            duration_s = (play.duration_ms / 1000.0) if play.duration_ms is not None else 0.0
            rows.append(
                PlayLogRowEntry(
                    row_number=row_number,
                    play_id=play.play_id if play.play_id is not None else 0,
                    play_type=play.play_type,
                    agent_name=_format_agent_label(play.agent_id, agent_lookup),
                    success=play.success,
                    started_at=play.started_at,
                    duration_seconds=duration_s,
                    dollar_cost=play.dollar_cost,
                    error=play.error,
                )
            )
        return rows

    @staticmethod
    def _compute_play_log_unique_agents(plays: list[PlayRecord]) -> int:
        """Count distinct concrete agents represented in the play log."""
        return len({play.agent_id for play in plays if play.agent_id is not None})

    @staticmethod
    def _compute_play_log_plays_in_use(plays: list[PlayRecord]) -> int:
        """Count distinct active play types used, excluding reserved future
        slots and internal plays (``INTERNAL_PLAY_TYPES``).
        """
        internal_play_values = {pt.value for pt in INTERNAL_PLAY_TYPES}
        future = {play_type for play_type, _, _, _, is_future in _PLAY_LOG_ORDER if is_future}
        excluded = future | internal_play_values
        return len({play.play_type for play in plays if play.play_type not in excluded})

    @staticmethod
    def _compute_play_log_total_slots() -> int:
        """Total user-facing play slots = registry minus internal heartbeats.

        The ESR header renders "<plays_in_use> / <total_slots>". Previously
        this denominator was hardcoded to 17 in the Jinja template (which
        was wrong both pre- and post-INTERNAL filter). Now computed from
        the registry so it stays correct if the action space grows again.
        """
        internal_play_values = {pt.value for pt in INTERNAL_PLAY_TYPES}
        return sum(1 for pt, *_ in _PLAY_LOG_ORDER if pt not in internal_play_values)

    @staticmethod
    def _compute_closed_issues(
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

    @staticmethod
    def _compute_cost_breakdown(
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

    @staticmethod
    def _compute_agent_performance(
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

    @staticmethod
    def _compute_agent_specialization(
        plays: list[PlayRecord],
    ) -> list[AgentSpecializationData]:
        """Break out per-agent success rates by play type from existing play history.

        Mirrors the matrix surfaced via ``SessionStatsSnapshot.agent_specialization``
        but produces a plain-dict shape ready for Jinja2 templates. Plays with no
        ``agent_id`` are skipped because there is nothing to attribute them to.
        """
        from agentshore.rl.metrics import compute_agent_specialization
        from agentshore.state import PlayType as _PlayType

        cells = compute_agent_specialization(plays)
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

    @staticmethod
    def _compute_failure_analysis(plays: list[PlayRecord]) -> list[FailureAnalysisEntry]:
        """Group failed plays by failure category."""
        by_category: dict[str, list[FailurePlayEntry]] = {}
        for p in plays:
            if not p.success:
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

    @staticmethod
    def _compute_anti_confirmation_audit(plays: list[PlayRecord]) -> int:
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

    @staticmethod
    def _compute_scope_drift(drifts: list[ScopeDriftRecord]) -> int:
        """Count scope drift entries."""
        return len(drifts)

    @staticmethod
    def _compute_issue_inflation(
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
                for issue in issues:
                    created_at = _parse_iso(issue.created_at)
                    if created_at is not None and _is_within_boundary(
                        created_at, prev_boundary, boundary
                    ):
                        opened_count += 1
                    closed_at = _parse_iso(issue.closed_at)
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

    @staticmethod
    def _compute_issue_throughput(
        issues: Sequence[GitHubIssueRecord],
    ) -> IssueThroughputData:
        opened = sum(1 for i in issues if i.state == "open")
        closed = sum(1 for i in issues if i.state == "closed")
        return IssueThroughputData(opened=opened, closed=closed, net_velocity=closed - opened)

    @staticmethod
    def _compute_play_distribution(plays: list[PlayRecord]) -> dict[str, int]:
        dist: dict[str, int] = {}
        for p in plays:
            dist[p.play_type] = dist.get(p.play_type, 0) + 1
        return dist

    @staticmethod
    def _compute_learnings_diff(
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

    @staticmethod
    def _compute_alignment_trajectory(plays: list[PlayRecord]) -> list[AlignmentTrajectoryEntry]:
        trajectory: list[AlignmentTrajectoryEntry] = []
        running = 0.0
        for idx, p in enumerate(plays):
            running += p.alignment_delta if p.alignment_delta is not None else 0.0
            trajectory.append(AlignmentTrajectoryEntry(play_index=idx, alignment=running))
        return trajectory

    @staticmethod
    def _compute_trajectory(
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

    @staticmethod
    def _compute_trajectory_analysis(
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

    @staticmethod
    def _compute_knowledge(learnings: Sequence[SessionLearningRecord]) -> int:
        """Count session learnings."""
        return len(learnings)

    @staticmethod
    def _compute_cleanup_history(plays: list[PlayRecord]) -> int:
        """Count CLEANUP plays."""
        return sum(1 for p in plays if p.play_type == "cleanup")

    @staticmethod
    def _compute_loop_incidents(plays: list[PlayRecord]) -> list[LoopIncidentEntry]:
        """Walk plays in order and emit one entry per same-type failure streak >= 3.

        Resolution classification:
          * ``succeeded_after_streak_N`` — the streak ended because the next play of the
            same type succeeded; ``N`` is the peak failure streak.
          * ``force_masked`` — the streak ended because the next play was a different
            play type *and* peak streak was >= 5 (i.e. a force-switch tier streak).
          * ``play_switched_after_streak_N`` — peak streak in 3-4 ended by a different
            play type (the warning tier — informational, not force-masked).
          * ``human_override`` — final failing play has ``failure_category ==
            "human_override"``.
          * ``human_escalation`` — final failing play has ``failure_category ==
            "human_escalation"``, OR the streak was still active at the end of the
            session with peak streak >= 7.
          * ``session_ended_in_streak`` — the streak was still active at the last play
            and peak streak < 7.
        """

        def _tier(peak: int) -> str:
            if peak >= 7:
                return "escalation"
            if peak >= 5:
                return "force_switch"
            return "warning"

        incidents: list[LoopIncidentEntry] = []
        streak_type: str | None = None
        streak_count = 0
        streak_start_index = 0
        streak_start_play_id: int | None = None
        streak_start_started_at = ""
        last_failure: PlayRecord | None = None

        def _emit(resolution: str) -> None:
            assert streak_type is not None
            assert last_failure is not None
            incidents.append(
                LoopIncidentEntry(
                    play_type=streak_type,
                    peak_streak=streak_count,
                    tier=_tier(streak_count),
                    start_play_id=streak_start_play_id,
                    end_play_id=last_failure.play_id,
                    start_play_index=streak_start_index,
                    end_play_index=streak_start_index + streak_count - 1,
                    started_at=streak_start_started_at,
                    ended_at=last_failure.started_at,
                    resolution=resolution,
                )
            )

        def _classify_terminated_by_other(peak: int) -> str:
            if last_failure is not None:
                fc = last_failure.failure_category
                if fc == "human_override":
                    return "human_override"
                if fc == "human_escalation":
                    return "human_escalation"
            if peak >= 5:
                return "force_masked"
            return f"play_switched_after_streak_{peak}"

        def _classify_same_type_success(peak: int) -> str:
            if last_failure is not None:
                fc = last_failure.failure_category
                if fc == "human_override":
                    return "human_override"
                if fc == "human_escalation":
                    return "human_escalation"
            return f"succeeded_after_streak_{peak}"

        for idx, p in enumerate(plays):
            if not p.success:
                if p.play_type == streak_type:
                    streak_count += 1
                else:
                    # Finalize any prior active streak ended by a different-type play.
                    if streak_type is not None and streak_count >= 3:
                        _emit(_classify_terminated_by_other(streak_count))
                    streak_type = p.play_type
                    streak_count = 1
                    streak_start_index = idx
                    streak_start_play_id = p.play_id
                    streak_start_started_at = p.started_at
                last_failure = p
            else:
                # Success: terminates any active streak.
                if streak_type is not None and streak_count >= 3:
                    if p.play_type == streak_type:
                        _emit(_classify_same_type_success(streak_count))
                    else:
                        _emit(_classify_terminated_by_other(streak_count))
                streak_type = None
                streak_count = 0
                streak_start_index = 0
                streak_start_play_id = None
                streak_start_started_at = ""
                last_failure = None

        # Finalize a trailing active streak.
        if streak_type is not None and streak_count >= 3 and last_failure is not None:
            fc = last_failure.failure_category
            if fc == "human_override":
                resolution = "human_override"
            elif fc == "human_escalation" or streak_count >= 7:
                resolution = "human_escalation"
            else:
                resolution = "session_ended_in_streak"
            incidents.append(
                LoopIncidentEntry(
                    play_type=streak_type,
                    peak_streak=streak_count,
                    tier=_tier(streak_count),
                    start_play_id=streak_start_play_id,
                    end_play_id=last_failure.play_id,
                    start_play_index=streak_start_index,
                    end_play_index=len(plays) - 1,
                    started_at=streak_start_started_at,
                    ended_at=last_failure.started_at,
                    resolution=resolution,
                )
            )

        return incidents

    @staticmethod
    def _compute_review_patterns(
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

    @staticmethod
    def _compute_recommendations(
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

    @staticmethod
    def _compute_epic_summaries(graph: ProjectGraph | None) -> list[EpicSummary]:
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

    @staticmethod
    def _compute_epic_closure_timeline(
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

    async def _resolve_repo_url(
        self,
        project_path: str,
        issues: Sequence[GitHubIssueRecord],
    ) -> str | None:
        """Return the best report link for the repository, using local and GitHub data."""
        for issue in issues:
            if issue.url:
                repo_url = _repo_url_from_github_child_url(issue.url)
                if repo_url is not None:
                    return repo_url

        remote = await _git_remote_url(project_path)
        if remote is None:
            return None
        return _normalize_repo_url(remote)


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


def _repo_url_from_github_child_url(url: str) -> str | None:
    match = re.match(r"^(https://github\.com/[^/]+/[^/]+)/(?:issues|pull)/\d+", url)
    if match is None:
        return None
    return match.group(1)


async def _git_remote_url(project_path: str) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            project_path,
            "config",
            "--get",
            "remote.origin.url",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    remote = stdout.decode(errors="replace").strip()
    return remote or None


def _normalize_repo_url(remote: str) -> str | None:
    value = remote.removesuffix(".git")
    if value.startswith("git@github.com:"):
        return "https://github.com/" + value.removeprefix("git@github.com:")
    if value.startswith("ssh://git@github.com/"):
        return "https://github.com/" + value.removeprefix("ssh://git@github.com/")
    if value.startswith("https://") or value.startswith("http://"):
        return value
    return None
