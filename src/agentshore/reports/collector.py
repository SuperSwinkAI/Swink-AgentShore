"""ReportDataCollector — pure-data aggregation for report templates.

Queries the DataStore and returns pre-computed dicts (TypedDicts) ready
for Jinja2 template rendering.  No dependency on Jinja2, TUI, IPC, or RL.

Implementation split (TNQA 10 H1):
  ``_aggregations.py`` — all pure ``compute_*`` helpers
  ``_loop_incidents.py`` — loop-incident state machine
  ``_repo_url.py``       — subprocess I/O + URL normalization
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agentshore.beads import GraphReadError, load_graph
from agentshore.core.concurrency_log import CONCURRENCY_FILENAME
from agentshore.reports._aggregations import (
    compute_agent_performance,
    compute_agent_specialization,
    compute_alignment_trajectory,
    compute_anti_confirmation_audit,
    compute_cleanup_history,
    compute_closed_issues,
    compute_control_rejections,
    compute_cost_breakdown,
    compute_epic_closure_timeline,
    compute_epic_summaries,
    compute_failure_analysis,
    compute_issue_inflation,
    compute_issue_throughput,
    compute_knowledge,
    compute_learnings_diff,
    compute_overview,
    compute_play_distribution,
    compute_play_log_columns,
    compute_play_log_plays_in_use,
    compute_play_log_rows,
    compute_play_log_total_slots,
    compute_play_log_unique_agents,
    compute_play_stats,
    compute_play_timeline,
    compute_recommendations,
    compute_review_patterns,
    compute_scope_drift,
    compute_trajectory,
    compute_trajectory_analysis,
)
from agentshore.reports._fleet_concurrency import collect_fleet_concurrency
from agentshore.reports._loop_incidents import compute_loop_incidents
from agentshore.reports._repo_url import resolve_repo_url
from agentshore.reports.types import (
    ActiveAgentEntry,
    AgentPerformanceData,
    AgentSpecializationData,
    AlignmentTrajectoryEntry,
    ClosedIssueEntry,
    ClusterAlignmentData,
    ComparisonData,
    ControlRejectionStatsEntry,
    CostBreakdownData,
    EndSessionReportData,
    EpicSummary,
    FailureAnalysisEntry,
    FailurePlayEntry,
    FleetConcurrencyData,
    FleetConcurrencyHistogramEntry,
    FleetConcurrencyPeakEntry,
    FleetConcurrencyRateLimitEntry,
    FleetConcurrencyTierPeakEntry,
    FleetConcurrencyTimelineAxisLabel,
    FleetConcurrencyTimelineData,
    FleetConcurrencyTimelineHarnessEntry,
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

if TYPE_CHECKING:
    from agentshore.data.store import DataStore

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
    "FleetConcurrencyData",
    "FleetConcurrencyHistogramEntry",
    "FleetConcurrencyPeakEntry",
    "FleetConcurrencyRateLimitEntry",
    "FleetConcurrencyTierPeakEntry",
    "FleetConcurrencyTimelineAxisLabel",
    "FleetConcurrencyTimelineData",
    "FleetConcurrencyTimelineHarnessEntry",
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


def _load_tier_config_maxes(project_path: Path) -> dict[str, int]:
    """Return ``agent_type/tier`` spawn caps from the project's config, best-effort."""
    try:
        from agentshore.agents.model_tiers import effective_model_tier_config
        from agentshore.config import load_config
        from agentshore.state import AgentType

        cfg = load_config(project_path / "agentshore.yaml")
    except Exception:
        return {}

    maxes: dict[str, int] = {}
    for agent_name, agent_cfg in cfg.agents.items():
        try:
            agent_type = AgentType(agent_name)
        except ValueError:
            continue
        for tier in agent_cfg.model_tiers:
            maxes[f"{agent_type.value}/{tier}"] = effective_model_tier_config(
                agent_type,
                agent_cfg,
                tier,
            ).max
    return maxes


class ReportDataCollector:
    """Pure-data aggregation layer between DataStore and report templates."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

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
        try:
            graph = await load_graph(Path(session.project_path))
        except GraphReadError:
            graph = None

        overview = compute_overview(session, plays)

        return SessionSummaryData(
            overview=overview,
            play_timeline=compute_play_timeline(plays),
            cost_breakdown=compute_cost_breakdown(plays, agents),
            agent_performance=compute_agent_performance(agents, plays),
            agent_specialization=compute_agent_specialization(plays),
            cluster_alignment=[],
            failure_analysis=compute_failure_analysis(plays),
            scope_drift_count=compute_scope_drift(drifts),
            anti_confirmation_violations=compute_anti_confirmation_audit(plays),
            issue_inflation=compute_issue_inflation(issues, plays),
            trajectory_snapshots=compute_trajectory(snapshots),
            trajectory_analysis=compute_trajectory_analysis(
                snapshots, plays, overview["total_cost"]
            ),
            learnings_count=compute_knowledge(learnings),
            revert_count=compute_cleanup_history(plays),
            loop_incidents=compute_loop_incidents(plays),
            review_patterns=compute_review_patterns(patterns),
            recommendations=compute_recommendations(plays, agents),
            epic_summaries=compute_epic_summaries(graph),
            epic_closure_timeline=compute_epic_closure_timeline(graph, plays),
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

        overview = compute_overview(session, plays)
        timeline = compute_play_timeline(plays)
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
        overview = compute_overview(session, plays)
        project_path = Path(session.project_path)
        from agentshore.session_path import session_dir

        concurrency_path = session_dir(project_path) / CONCURRENCY_FILENAME

        return EndSessionReportData(
            overview=overview,
            repo_url=await resolve_repo_url(session.project_path, issues),
            fleet_concurrency=collect_fleet_concurrency(
                concurrency_path,
                session_id,
                tier_config_maxes=_load_tier_config_maxes(project_path),
            ),
            play_stats=compute_play_stats(plays),
            control_rejections=compute_control_rejections(control_rejections),
            closed_issues=compute_closed_issues(session, issues),
            play_log_columns=compute_play_log_columns(),
            play_log_rows=compute_play_log_rows(plays, agents),
            play_log_unique_agents=compute_play_log_unique_agents(plays),
            play_log_plays_in_use=compute_play_log_plays_in_use(plays),
            play_log_total_slots=compute_play_log_total_slots(),
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

        ov_a = compute_overview(session_a, plays_a)
        ov_b = compute_overview(session_b, plays_b)

        alignment_a = ov_a["final_alignment"] if ov_a["final_alignment"] is not None else 0.0
        alignment_b = ov_b["final_alignment"] if ov_b["final_alignment"] is not None else 0.0

        return ComparisonData(
            session_a=ov_a,
            session_b=ov_b,
            cost_diff=ov_b["total_cost"] - ov_a["total_cost"],
            alignment_diff=alignment_b - alignment_a,
            play_count_diff=ov_b["total_plays"] - ov_a["total_plays"],
            cost_breakdown_a=compute_cost_breakdown(plays_a, agents_a),
            cost_breakdown_b=compute_cost_breakdown(plays_b, agents_b),
            issue_throughput_a=compute_issue_throughput(issues_a),
            issue_throughput_b=compute_issue_throughput(issues_b),
            play_distribution_a=compute_play_distribution(plays_a),
            play_distribution_b=compute_play_distribution(plays_b),
            learnings_diff=compute_learnings_diff(learnings_a, learnings_b),
            alignment_trajectory_a=compute_alignment_trajectory(plays_a),
            alignment_trajectory_b=compute_alignment_trajectory(plays_b),
        )
