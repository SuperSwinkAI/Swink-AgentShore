"""Tests for ReportGenerator — HTML report generation via Jinja2."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agentshore.reports.collector import (
    AlignmentTrajectoryEntry,
    ComparisonData,
    CostBreakdownData,
    EndSessionReportData,
    EpicSummary,
    IssueInflationData,
    IssueThroughputData,
    LearningsDiffData,
    OverviewData,
    PlayTimelineEntry,
    ProgressReportData,
    SessionSummaryData,
)
from agentshore.reports.generator import ReportGenerator

# ---------------------------------------------------------------------------
# Canned data fixtures
# ---------------------------------------------------------------------------

SID = "sess-gen-test-0001"
SID2 = "sess-gen-test-0002"
NOW = "2026-04-27T00:00:00+00:00"


def _overview(
    session_id: str = SID,
    *,
    final_alignment: float | None = 0.85,
    total_plays: int = 3,
) -> OverviewData:
    return OverviewData(
        session_id=session_id,
        duration_seconds=3600.0,
        total_plays=total_plays,
        successful_plays=2,
        failed_plays=1,
        total_cost=5.0,
        final_alignment=final_alignment,
        started_at=NOW,
        ended_at="2026-04-27T01:00:00+00:00",
    )


def _play_entry(
    play_id: int = 1,
    play_type: str = "issue_pickup",
    success: bool = True,
) -> PlayTimelineEntry:
    return PlayTimelineEntry(
        play_id=play_id,
        play_type=play_type,
        agent_id="agent-1",
        success=success,
        duration_seconds=10.0,
        dollar_cost=0.5,
        alignment_delta=0.02,
        error=None if success else "timeout",
        started_at=NOW,
    )


def _session_summary_data(
    *,
    total_plays: int = 3,
    final_alignment: float | None = 0.85,
) -> SessionSummaryData:
    plays = []
    if total_plays > 0:
        plays = [
            _play_entry(play_id=1, play_type="issue_pickup", success=True),
            _play_entry(play_id=2, play_type="code_review", success=True),
            _play_entry(play_id=3, play_type="run_qa", success=False),
        ][:total_plays]
    return SessionSummaryData(
        overview=_overview(final_alignment=final_alignment, total_plays=total_plays),
        play_timeline=plays,
        cost_breakdown=CostBreakdownData(
            by_play_type={"issue_pickup": 0.5, "code_review": 0.3} if total_plays > 0 else {},
            by_agent={"agent-1": 0.8} if total_plays > 0 else {},
            cumulative=[(0, 0.5), (1, 0.8), (2, 1.3)] if total_plays > 0 else [],
        ),
        agent_performance=[
            {
                "agent_id": "agent-1",
                "agent_type": "claude_code",
                "tasks_completed": 2,
                "tasks_failed": 1,
                "success_rate": 0.67,
                "total_cost": 0.8,
                "avg_duration": 10.0,
            }
        ]
        if total_plays > 0
        else [],
        agent_specialization=[
            {
                "agent_id": "agent-1",
                "play_type": "issue_pickup",
                "total": 1,
                "successful": 1,
                "failed": 0,
                "success_rate": 1.0,
                "rolling_success_rate": 1.0,
            },
            {
                "agent_id": "agent-1",
                "play_type": "code_review",
                "total": 1,
                "successful": 1,
                "failed": 0,
                "success_rate": 1.0,
                "rolling_success_rate": 1.0,
            },
            {
                "agent_id": "agent-1",
                "play_type": "run_qa",
                "total": 1,
                "successful": 0,
                "failed": 1,
                "success_rate": 0.0,
                "rolling_success_rate": 0.0,
            },
        ]
        if total_plays > 0
        else [],
        cluster_alignment=[],
        epic_summaries=[
            EpicSummary(
                bead_id="epic-auth",
                title="Auth system",
                closure_ratio=0.9,
                total_tasks=10,
                closed_tasks=9,
            )
        ]
        if total_plays > 0
        else [],
        epic_closure_timeline={
            "global_ratio_start": 0.4 if total_plays > 0 else 0.0,
            "global_ratio_midpoint": 0.65 if total_plays > 0 else 0.0,
            "global_ratio_end": 0.9 if total_plays > 0 else 0.0,
            "tasks_closed_by_play_type": [
                {"play_type": "issue_pickup", "plays_executed": 1, "estimated_tasks_closed": 3},
                {"play_type": "merge_pr", "plays_executed": 0, "estimated_tasks_closed": 0},
                {"play_type": "run_qa", "plays_executed": 1, "estimated_tasks_closed": 1},
            ]
            if total_plays > 0
            else [],
        },
        failure_analysis=[{"category": "timeout", "count": 1, "plays": [{"play_type": "run_qa"}]}]
        if total_plays > 0
        else [],
        scope_drift_count=0,
        anti_confirmation_violations=0,
        issue_inflation=IssueInflationData(
            total_opened=2,
            total_closed=1,
            ratio=2.0,
            per_play=[(1, 1, 0, 1), (2, 1, 1, 0)],
            warnings_triggered=0,
            by_source={"agentshore/intake": 1, "agentshore/qa": 1, "agentshore/review": 0},
            recovery={"reversed": False, "contributing_plays": []},
        ),
        trajectory_snapshots=[],
        trajectory_analysis={
            "trend": "flat",
            "estimated_total_cost_early": 5.0,
            "actual_total_cost": 5.0,
            "budget_sufficiency": [],
        },
        learnings_count=1,
        revert_count=0,
        loop_incidents=[],
        review_patterns=[],
        recommendations=["Agent 'agent-1' had 33% failure rate (1/3)"] if total_plays > 0 else [],
    )


def _progress_report_data() -> ProgressReportData:
    return ProgressReportData(
        overview=_overview(),
        cluster_alignment=[],
        recent_plays=[
            _play_entry(play_id=1, play_type="issue_pickup", success=True),
        ],
        budget_remaining=15.0,
        active_agents=[
            {
                "agent_id": "agent-1",
                "agent_type": "claude_code",
                "tasks_completed": 2,
                "total_cost": 0.8,
            }
        ],
    )


def _end_session_report_data() -> EndSessionReportData:
    return EndSessionReportData(
        overview=_overview(),
        repo_url="https://github.com/acme/widgets",
        play_stats=[
            {
                "play_type": "issue_pickup",
                "total": 2,
                "successful": 1,
                "failed": 1,
                "success_rate": 0.5,
                "total_cost": 1.5,
                "avg_duration_seconds": 12.0,
            },
            {
                "play_type": "merge_pr",
                "total": 1,
                "successful": 1,
                "failed": 0,
                "success_rate": 1.0,
                "total_cost": 0.25,
                "avg_duration_seconds": 4.0,
            },
        ],
        control_rejections=[
            {
                "kind": "dispatch_revalidation_block",
                "play_type": "run_qa",
                "reason": "run_qa cooldown (1/25 plays since last)",
                "count": 2,
            }
        ],
        closed_issues=[
            {
                "issue_number": 10,
                "title": "Closed in session",
                "closed_at": "2026-04-27T00:30:00+00:00",
                "labels": ["bug"],
            }
        ],
        play_log_columns=[
            {
                "play_type": "instantiate_agent",
                "label": "INSTANTIATE_AGENT",
                "action_index": 0,
                "phase": 1,
                "phase_start": True,
                "future": False,
            },
            {
                "play_type": "seed_project",
                "label": "SEED_PROJECT",
                "action_index": 17,
                "phase": 1,
                "phase_start": False,
                "future": False,
            },
            {
                "play_type": "issue_pickup",
                "label": "ISSUE_PICKUP",
                "action_index": 4,
                "phase": 2,
                "phase_start": True,
                "future": False,
            },
            {
                "play_type": "design_audit",
                "label": "DESIGN_AUDIT",
                "action_index": 9,
                "phase": 1,
                "phase_start": True,
                "future": False,
            },
            {
                "play_type": "end_session",
                "label": "END_SESSION",
                "action_index": 10,
                "phase": 7,
                "phase_start": True,
                "future": False,
            },
        ],
        play_log_rows=[
            {
                "row_number": 1,
                "play_id": 1,
                "play_type": "instantiate_agent",
                "agent_name": "agentshore",
                "success": True,
                "started_at": NOW,
                "duration_seconds": 1.0,
                "dollar_cost": 0.0,
                "error": None,
            },
            {
                "row_number": 2,
                "play_id": 2,
                "play_type": "issue_pickup",
                "agent_name": "agent-1",
                "success": False,
                "started_at": NOW,
                "duration_seconds": 12.0,
                "dollar_cost": 1.5,
                "error": "timeout",
            },
        ],
        play_log_unique_agents=1,
        play_log_plays_in_use=2,
    )


def _comparison_data() -> ComparisonData:
    cost_breakdown = CostBreakdownData(
        by_play_type={"issue_pickup": 1.0},
        by_agent={"ag-1": 1.0},
        cumulative=[(0, 1.0)],
    )
    return ComparisonData(
        session_a=_overview(session_id=SID),
        session_b=_overview(session_id=SID2, final_alignment=0.92, total_plays=5),
        cost_diff=2.0,
        alignment_diff=0.07,
        play_count_diff=2,
        cost_breakdown_a=cost_breakdown,
        cost_breakdown_b=cost_breakdown,
        issue_throughput_a=IssueThroughputData(opened=1, closed=2, net_velocity=1),
        issue_throughput_b=IssueThroughputData(opened=0, closed=3, net_velocity=3),
        play_distribution_a={"issue_pickup": 2, "code_review": 1},
        play_distribution_b={"issue_pickup": 3, "merge_pr": 1},
        learnings_diff=LearningsDiffData(
            added=["new pattern"],
            removed=["old pattern"],
            shared=["common pattern"],
        ),
        alignment_trajectory_a=[
            AlignmentTrajectoryEntry(play_index=0, alignment=0.1),
            AlignmentTrajectoryEntry(play_index=1, alignment=0.2),
        ],
        alignment_trajectory_b=[
            AlignmentTrajectoryEntry(play_index=0, alignment=0.15),
        ],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_generator(
    collector_mock: AsyncMock | None = None,
) -> ReportGenerator:
    """Build a ReportGenerator with a mocked DataStore and optionally a mocked collector."""
    store = MagicMock()
    gen = ReportGenerator(store)
    if collector_mock is not None:
        gen._collector = collector_mock
    return gen


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSessionSummary:
    """Tests for session summary report generation."""

    async def test_generates_html_file(self, tmp_path: Path) -> None:
        collector = AsyncMock()
        collector.collect_session_summary.return_value = _session_summary_data()
        gen = _make_generator(collector)

        path = await gen.generate_session_summary(SID, tmp_path)

        assert path.exists()
        assert path.suffix == ".html"
        assert path.name == f"session-{SID[:8]}-summary.html"

    async def test_html_contains_sections(self, tmp_path: Path) -> None:
        collector = AsyncMock()
        collector.collect_session_summary.return_value = _session_summary_data()
        gen = _make_generator(collector)

        path = await gen.generate_session_summary(SID, tmp_path)
        html = path.read_text(encoding="utf-8")

        expected_headings = [
            "Overview",
            "Play Timeline",
            "Cost Breakdown",
            "Agent Performance",
            "Epic Closure",
            "Epic Closure Timeline",
            "Failure Analysis",
            "Scope Drift",
            "Anti-Confirmation Bias Audit",
            "Issue Inflation",
            "Trajectory Snapshots",
            "Cleanup History",
            "Loop Incidents",
            "Review Patterns",
            "Recommendations",
        ]
        for heading in expected_headings:
            assert heading in html, f"Missing section heading: {heading}"

    async def test_is_self_contained(self, tmp_path: Path) -> None:
        collector = AsyncMock()
        collector.collect_session_summary.return_value = _session_summary_data()
        gen = _make_generator(collector)

        path = await gen.generate_session_summary(SID, tmp_path)
        html = path.read_text(encoding="utf-8")

        # Split into lines and check each — allow footer GitHub link and
        # URLs inside bundled library comments (e.g. jsdelivr.com in Chart.js).
        allowed_domains = {"github.com", "jsdelivr.com", "chartjs.org"}
        for line in html.splitlines():
            if "https://" in line or "http://" in line:
                assert any(d in line for d in allowed_domains), (
                    f"External URL found outside allowed domains: {line.strip()}"
                )

    async def test_embeds_chartjs(self, tmp_path: Path) -> None:
        collector = AsyncMock()
        collector.collect_session_summary.return_value = _session_summary_data()
        gen = _make_generator(collector)

        path = await gen.generate_session_summary(SID, tmp_path)
        html = path.read_text(encoding="utf-8")

        assert "Chart" in html
        assert "window.Chart" in html
        assert len(html) > 50_000, "Report should embed real Chart.js (~200KB), not the stub"

    async def test_empty_session(self, tmp_path: Path) -> None:
        collector = AsyncMock()
        collector.collect_session_summary.return_value = _session_summary_data(
            total_plays=0, final_alignment=None
        )
        gen = _make_generator(collector)

        path = await gen.generate_session_summary(SID, tmp_path)

        assert path.exists()
        html = path.read_text(encoding="utf-8")
        assert "No plays recorded." in html
        assert "Epic Closure Timeline" not in html

    async def test_session_summary_renders_loop_incident_table(self, tmp_path: Path) -> None:
        data = _session_summary_data()
        data["loop_incidents"] = [
            {
                "play_type": "code_review",
                "peak_streak": 4,
                "tier": "warning",
                "start_play_id": 1,
                "end_play_id": 4,
                "start_play_index": 0,
                "end_play_index": 3,
                "started_at": NOW,
                "ended_at": NOW,
                "resolution": "succeeded_after_streak_4",
            },
            {
                "play_type": "merge_pr",
                "peak_streak": 6,
                "tier": "force_switch",
                "start_play_id": 5,
                "end_play_id": 10,
                "start_play_index": 4,
                "end_play_index": 9,
                "started_at": NOW,
                "ended_at": NOW,
                "resolution": "force_masked",
            },
        ]
        collector = AsyncMock()
        collector.collect_session_summary.return_value = data
        gen = _make_generator(collector)

        path = await gen.generate_session_summary(SID, tmp_path)
        html = path.read_text(encoding="utf-8")

        assert "<td>code_review</td>" in html
        assert "<td>merge_pr</td>" in html
        assert "<td>succeeded_after_streak_4</td>" in html
        assert "<td>force_masked</td>" in html
        assert "<td>warning</td>" in html
        assert "<td>force_switch</td>" in html


class TestProgressReport:
    """Tests for progress report generation."""

    async def test_generates_html(self, tmp_path: Path) -> None:
        collector = AsyncMock()
        collector.collect_progress_report.return_value = _progress_report_data()
        gen = _make_generator(collector)

        path = await gen.generate_progress_report(SID, tmp_path)

        assert path.exists()
        assert path.suffix == ".html"
        assert path.name == f"session-{SID[:8]}-progress.html"

    async def test_contains_sections(self, tmp_path: Path) -> None:
        collector = AsyncMock()
        collector.collect_progress_report.return_value = _progress_report_data()
        gen = _make_generator(collector)

        path = await gen.generate_progress_report(SID, tmp_path)
        html = path.read_text(encoding="utf-8")

        assert "Budget Status" in html
        assert "Epic Closure" in html
        assert "Recent Plays" in html
        assert "Active Agents" in html


class TestComparisonReport:
    """Tests for archive comparison report generation."""

    async def test_generates_html(self, tmp_path: Path) -> None:
        collector = AsyncMock()
        collector.collect_comparison.return_value = _comparison_data()
        gen = _make_generator(collector)

        path = await gen.generate_comparison(SID, SID2, tmp_path)

        assert path.exists()
        assert path.suffix == ".html"
        assert path.name == f"comparison-{SID[:8]}-vs-{SID2[:8]}.html"


class TestEndSessionReport:
    """Tests for compact end-of-session report generation."""

    async def test_generates_named_portable_html(self, tmp_path: Path) -> None:
        collector = AsyncMock()
        collector.collect_end_session_report.return_value = _end_session_report_data()
        gen = _make_generator(collector)

        path = await gen.generate_end_session_report(SID, tmp_path)
        html = path.read_text(encoding="utf-8")

        assert path.name == f"end-session-{SID}.html"
        assert "End Session Report" in html
        assert "Play Statistics" in html
        assert "Control Rejections" in html
        assert "run_qa cooldown" in html
        assert "Issues Closed During Session" in html
        assert "Play Log" in html
        assert "play-log-window" in html
        assert "INSTANTIATE_AGENT" in html
        assert "ISSUE_PICKUP" in html
        assert "agent-1" in html
        assert ">FAIL<" in html
        assert "Closed in session" in html
        assert html.count("<a href=") == 1
        assert "https://github.com/acme/widgets" in html

    async def test_uses_light_mode_style_guide_tokens(self, tmp_path: Path) -> None:
        collector = AsyncMock()
        collector.collect_end_session_report.return_value = _end_session_report_data()
        gen = _make_generator(collector)

        path = await gen.generate_end_session_report(SID, tmp_path)
        html = path.read_text(encoding="utf-8")

        assert "--color-fm-bg: #f8f9fb;" in html
        assert "--color-fm-panel: rgba(255,255,255,0.94);" in html
        assert "--color-fm-ok: #168e49;" in html
        assert '--font-mono: "JetBrains Mono"' in html
        assert "color-scheme: light;" in html
        assert "--log-paper" not in html
        assert "#f6f2e8" not in html

    async def test_open_browser_uses_file_uri(self, tmp_path: Path) -> None:
        collector = AsyncMock()
        collector.collect_end_session_report.return_value = _end_session_report_data()
        gen = _make_generator(collector)

        with patch("agentshore.reports.generator.webbrowser.open") as mock_open:
            path = await gen.generate_end_session_report(SID, tmp_path, open_browser=True)
            mock_open.assert_called_once_with(path.resolve().as_uri())


class TestBrowserAndDir:
    """Tests for browser opening and directory creation."""

    async def test_open_browser_flag(self, tmp_path: Path) -> None:
        collector = AsyncMock()
        collector.collect_session_summary.return_value = _session_summary_data()
        gen = _make_generator(collector)

        with patch("agentshore.reports.generator.webbrowser.open") as mock_open:
            path = await gen.generate_session_summary(SID, tmp_path, open_browser=True)
            mock_open.assert_called_once_with(path.resolve().as_uri())

    async def test_output_dir_created(self, tmp_path: Path) -> None:
        collector = AsyncMock()
        collector.collect_session_summary.return_value = _session_summary_data()
        gen = _make_generator(collector)

        nested = tmp_path / "deeply" / "nested" / "dir"
        assert not nested.exists()

        path = await gen.generate_session_summary(SID, nested)

        assert nested.exists()
        assert path.exists()
