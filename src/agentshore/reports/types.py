"""TypedDict definitions for report data shapes.

These are the template-ready data structures returned by
:class:`~agentshore.reports.collector.ReportDataCollector`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


@dataclass(frozen=True)
class EpicSummary:
    """Closure-ratio snapshot for a single epic, for use in session reports."""

    bead_id: str
    title: str
    closure_ratio: float
    total_tasks: int
    closed_tasks: int


class OverviewData(TypedDict):
    session_id: str
    duration_seconds: float
    total_plays: int
    successful_plays: int
    failed_plays: int
    skipped_plays: int
    total_cost: float
    final_alignment: float | None
    started_at: str
    ended_at: str | None


class PlayTimelineEntry(TypedDict):
    play_id: int
    play_type: str
    agent_id: str | None
    success: bool
    duration_seconds: float
    dollar_cost: float
    alignment_delta: float
    error: str | None
    started_at: str


class PlayStatsEntry(TypedDict):
    play_type: str
    total: int
    successful: int
    failed: int
    skipped: int
    success_rate: float
    total_cost: float
    avg_duration_seconds: float


class ControlRejectionStatsEntry(TypedDict):
    kind: str
    play_type: str
    reason: str
    count: int


class FleetConcurrencyPeakEntry(TypedDict):
    label: str
    peak_busy: int


class FleetConcurrencyTierPeakEntry(TypedDict):
    label: str
    peak_busy: int
    config_max: int | None


class FleetConcurrencyHistogramEntry(TypedDict):
    busy_level: int
    samples: int
    sample_share: float


class FleetConcurrencyRateLimitEntry(TypedDict):
    seq: int | None
    ts: str | None
    play_type: str | None
    completed_agent_type: str | None
    completed_model_tier: str | None
    busy_total: int
    busy_by_type: dict[str, int]
    busy_by_type_tier: dict[str, int]


class FleetConcurrencyTimelineAxisLabel(TypedDict):
    x: float
    y: float
    label: str


class FleetConcurrencyTimelineHarnessEntry(TypedDict):
    label: str
    display_label: str
    color: str
    fill: str
    area_points: str


class FleetConcurrencyTimelineData(TypedDict):
    width: int
    height: int
    y_axis_labels: list[FleetConcurrencyTimelineAxisLabel]
    x_axis_labels: list[FleetConcurrencyTimelineAxisLabel]
    harnesses: list[FleetConcurrencyTimelineHarnessEntry]
    total_points: str
    duration_label: str
    note: str


class FleetConcurrencyData(TypedDict):
    sample_count: int
    peak_busy: int
    mean_busy: float
    peak_by_harness: list[FleetConcurrencyPeakEntry]
    peak_by_harness_tier: list[FleetConcurrencyTierPeakEntry]
    busy_histogram: list[FleetConcurrencyHistogramEntry]
    timeline: FleetConcurrencyTimelineData
    rate_limit_samples: list[FleetConcurrencyRateLimitEntry]


class PlayLogColumnEntry(TypedDict):
    play_type: str
    label: str
    action_index: int
    phase: int
    phase_start: bool
    future: bool


class PlayLogRowEntry(TypedDict):
    row_number: int
    play_id: int
    play_type: str
    agent_name: str
    success: bool
    status: str  # "ok" | "fail" — gated skips are excluded from the play log entirely
    started_at: str
    duration_seconds: float
    dollar_cost: float
    error: str | None


class ClosedIssueEntry(TypedDict):
    issue_number: int
    title: str
    closed_at: str | None
    labels: list[str]


class CostBreakdownData(TypedDict):
    by_play_type: dict[str, float]
    by_agent: dict[str, float]
    cumulative: list[tuple[int, float]]  # (play_index, cumulative_cost)


class AgentPerformanceData(TypedDict):
    agent_id: str
    agent_type: str
    tasks_completed: int
    tasks_failed: int
    success_rate: float
    total_cost: float
    avg_duration: float
    # dispatch_share = dispatch_count / fleet dispatch total (0.0 when none yet);
    # surfaces agents starved of plays while work is available.
    dispatch_count: int
    dispatch_share: float


class AgentSpecializationData(TypedDict):
    agent_id: str
    play_type: str
    total: int
    successful: int
    failed: int
    success_rate: float
    rolling_success_rate: float


class ClusterAlignmentData(TypedDict):
    theme: str
    alignment: float
    issue_count: int


class IssueInflationData(TypedDict):
    total_opened: int
    total_closed: int
    ratio: float  # opened / max(closed, 1)
    per_play: list[tuple[int, int, int, int]]  # (play_index, opened_count, closed_count, net_open)
    warnings_triggered: int
    by_source: dict[str, int]
    recovery: dict[str, bool | list[int]]


class FailurePlayEntry(TypedDict):
    play_id: int
    play_type: str
    error: str | None
    agent_id: str | None


class FailureAnalysisEntry(TypedDict):
    category: str
    count: int
    plays: list[FailurePlayEntry]


class TrajectorySnapshotEntry(TypedDict):
    play_id: int
    projected_alignment: float
    remaining_plays: int
    remaining_cost: float
    created_at: str


class BudgetSufficiencyEntry(TypedDict):
    budget_consumed_pct: float
    projected_sufficient: bool


class TrajectoryAnalysisData(TypedDict):
    trend: str
    estimated_total_cost_early: float
    actual_total_cost: float
    budget_sufficiency: list[BudgetSufficiencyEntry]


class ReviewPatternEntry(TypedDict):
    pattern: str
    category: str
    frequency: int
    injected: bool


class LoopIncidentEntry(TypedDict):
    play_type: str
    peak_streak: int
    tier: str  # "warning" | "force_switch" | "escalation"
    start_play_id: int | None
    end_play_id: int | None
    start_play_index: int
    end_play_index: int
    started_at: str
    ended_at: str
    resolution: str


class ActiveAgentEntry(TypedDict):
    agent_id: str
    agent_type: str
    status: str
    tasks_completed: int
    tasks_failed: int
    total_cost: float


class ClosureByPlayTypeEntry(TypedDict):
    play_type: str
    plays_executed: int
    estimated_tasks_closed: int


class EpicClosureTimelineData(TypedDict):
    global_ratio_start: float
    global_ratio_midpoint: float
    global_ratio_end: float
    tasks_closed_by_play_type: list[ClosureByPlayTypeEntry]


class SessionSummaryData(TypedDict):
    overview: OverviewData
    play_timeline: list[PlayTimelineEntry]
    cost_breakdown: CostBreakdownData
    agent_performance: list[AgentPerformanceData]
    agent_specialization: list[AgentSpecializationData]
    cluster_alignment: list[ClusterAlignmentData]
    failure_analysis: list[FailureAnalysisEntry]
    scope_drift_count: int
    anti_confirmation_violations: int
    issue_inflation: IssueInflationData
    trajectory_snapshots: list[TrajectorySnapshotEntry]
    trajectory_analysis: TrajectoryAnalysisData
    learnings_count: int
    revert_count: int  # count of CLEANUP plays (slot was formerly REVERT_COMMIT)
    loop_incidents: list[LoopIncidentEntry]
    review_patterns: list[ReviewPatternEntry]
    recommendations: list[str]
    epic_summaries: list[EpicSummary]
    epic_closure_timeline: EpicClosureTimelineData


class EndSessionReportData(TypedDict):
    overview: OverviewData
    repo_url: str | None
    fleet_concurrency: FleetConcurrencyData | None
    play_stats: list[PlayStatsEntry]
    control_rejections: list[ControlRejectionStatsEntry]
    closed_issues: list[ClosedIssueEntry]
    play_log_columns: list[PlayLogColumnEntry]
    play_log_rows: list[PlayLogRowEntry]
    play_log_unique_agents: int
    play_log_plays_in_use: int
    # Denominator in "<plays_in_use> / <total_slots>"; derived (registry minus
    # internal heartbeats), not hardcoded.
    play_log_total_slots: int


class ProgressReportData(TypedDict):
    overview: OverviewData
    cluster_alignment: list[ClusterAlignmentData]
    recent_plays: list[PlayTimelineEntry]
    budget_remaining: float | None
    active_agents: list[ActiveAgentEntry]


class IssueThroughputData(TypedDict):
    opened: int
    closed: int
    net_velocity: int  # closed - opened


class LearningsDiffData(TypedDict):
    added: list[str]  # patterns only in session_b
    removed: list[str]  # patterns only in session_a
    shared: list[str]  # patterns in both


class AlignmentTrajectoryEntry(TypedDict):
    play_index: int
    alignment: float


class ComparisonData(TypedDict):
    session_a: OverviewData
    session_b: OverviewData
    cost_diff: float
    alignment_diff: float
    play_count_diff: int
    cost_breakdown_a: CostBreakdownData
    cost_breakdown_b: CostBreakdownData
    issue_throughput_a: IssueThroughputData
    issue_throughput_b: IssueThroughputData
    play_distribution_a: dict[str, int]
    play_distribution_b: dict[str, int]
    learnings_diff: LearningsDiffData
    alignment_trajectory_a: list[AlignmentTrajectoryEntry]
    alignment_trajectory_b: list[AlignmentTrajectoryEntry]
