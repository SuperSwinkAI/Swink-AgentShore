"""Tests for ReportDataCollector — pure-data aggregation layer."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from agentshore.beads import EpicStatus, ProjectGraph
from agentshore.core.concurrency_log import CONCURRENCY_FILENAME, RECORD_VERSION
from agentshore.data.store import (
    AgentRecord,
    DataStore,
    ExternalMutationRecord,
    GitHubIssueRecord,
    PlayRecord,
    SessionRecord,
    TrajectorySnapshotRecord,
)
from agentshore.reports.collector import ReportDataCollector
from agentshore.session_path import session_dir

SID = "sess-report-test"
NOW = "2026-04-27T00:00:00+00:00"


@pytest_asyncio.fixture
async def store(tmp_path):
    db = DataStore(tmp_path / "report_test.db")
    await db.initialize()
    yield db
    await db.close()


async def _seed_session(
    store: DataStore,
    *,
    session_id: str = SID,
    project_path: str = "/tmp/proj",
    started_at: str = NOW,
    ended_at: str | None = "2026-04-27T01:00:00+00:00",
    total_cost: float = 5.0,
    total_plays: int = 0,
    final_alignment: float | None = 0.85,
) -> None:
    await store.create_session(
        SessionRecord(
            session_id=session_id,
            project_path=project_path,
            started_at=started_at,
            ended_at=ended_at,
            status="completed",
            total_cost=total_cost,
            total_plays=total_plays,
            final_alignment=final_alignment,
        )
    )


async def _seed_play(
    store: DataStore,
    *,
    session_id: str = SID,
    play_type: str = "issue_pickup",
    agent_id: str | None = "agent-1",
    success: bool = True,
    dollar_cost: float = 0.5,
    duration_ms: int | None = 10_000,
    alignment_delta: float | None = 0.02,
    error: str | None = None,
    failure_category: str | None = None,
    started_at: str = "2026-04-27T00:10:00+00:00",
) -> int:
    return await store.record_play(
        PlayRecord(
            session_id=session_id,
            play_type=play_type,
            agent_id=agent_id,
            started_at=started_at,
            success=success,
            dollar_cost=dollar_cost,
            duration_ms=duration_ms,
            alignment_delta=alignment_delta,
            error=error,
            failure_category=failure_category,
        )
    )


async def _seed_agent(
    store: DataStore,
    *,
    agent_id: str = "agent-1",
    session_id: str = SID,
    agent_type: str = "claude_code",
    tasks_completed: int = 5,
    tasks_failed: int = 0,
    total_cost: float = 2.0,
    model_tier: str | None = None,
    display_name: str | None = None,
    dispatch_count: int = 0,
) -> None:
    await store.register_agent(
        AgentRecord(
            agent_id=agent_id,
            session_id=session_id,
            agent_type=agent_type,
            created_at=NOW,
            tasks_completed=tasks_completed,
            tasks_failed=tasks_failed,
            total_cost=total_cost,
            model_tier=model_tier,
            display_name=display_name,
            dispatch_count=dispatch_count,
        )
    )


async def _seed_issues(store: DataStore, session_id: str = SID) -> None:
    await store.cache_github_issues(
        session_id,
        [
            GitHubIssueRecord(
                issue_number=10,
                session_id=session_id,
                title="Closed in session",
                state="closed",
                created_at="2026-04-26T00:00:00+00:00",
                closed_at="2026-04-27T00:30:00+00:00",
                labels=["bug"],
                url="https://github.com/acme/widgets/issues/10",
            ),
            GitHubIssueRecord(
                issue_number=11,
                session_id=session_id,
                title="Closed before session",
                state="closed",
                created_at="2026-04-20T00:00:00+00:00",
                closed_at="2026-04-26T00:30:00+00:00",
                labels=[],
                url="https://github.com/acme/widgets/issues/11",
            ),
            GitHubIssueRecord(
                issue_number=12,
                session_id=session_id,
                title="Still open",
                state="open",
                created_at="2026-04-27T00:00:00+00:00",
                labels=[],
                url="https://github.com/acme/widgets/issues/12",
            ),
        ],
    )


async def test_collect_session_summary_overview(store):
    await _seed_session(store)
    await _seed_play(store, success=True, dollar_cost=1.0)
    await _seed_play(store, success=False, dollar_cost=0.5, error="timeout")
    await _seed_agent(store)

    collector = ReportDataCollector(store)
    summary = await collector.collect_session_summary(SID)
    ov = summary["overview"]

    assert ov["session_id"] == SID
    assert ov["total_plays"] == 2
    assert ov["successful_plays"] == 1
    assert ov["failed_plays"] == 1
    # total_cost is the play-sum (1.0 + 0.5), not session.total_cost (5.0), so
    # every report agrees on a single self-consistent definition (H4).
    assert ov["total_cost"] == pytest.approx(1.5)
    assert ov["final_alignment"] == pytest.approx(0.85)
    assert ov["duration_seconds"] == pytest.approx(3600.0)
    assert ov["started_at"] == NOW
    assert ov["ended_at"] == "2026-04-27T01:00:00+00:00"


async def test_collect_end_session_report_stats_and_closed_issues(store):
    await _seed_session(store)
    await _seed_play(store, play_type="issue_pickup", success=True, dollar_cost=1.0)
    await _seed_play(store, play_type="issue_pickup", success=False, dollar_cost=0.5)
    await _seed_play(store, play_type="merge_pr", success=True, dollar_cost=0.25)
    await _seed_issues(store)

    collector = ReportDataCollector(store)
    report = await collector.collect_end_session_report(SID)

    assert report["repo_url"] == "https://github.com/acme/widgets"
    assert report["overview"]["total_cost"] == pytest.approx(1.75)
    assert report["play_stats"][0]["play_type"] == "issue_pickup"
    assert report["play_stats"][0]["total"] == 2
    assert report["play_stats"][0]["failed"] == 1
    assert [issue["issue_number"] for issue in report["closed_issues"]] == [10]
    # desktop-rni0: INTERNAL_PLAY_TYPES is empty, so all 22 registry entries
    # appear. Slot 11 is now RECONCILE_STATE (AgentShore #593); slot 14 is now
    # FUTURE_4 (reserved, formerly browser_verification), so 18 active +
    # 3 reserved FUTURE_N slots (4/7/8) + 1 internal = 22 columns.
    assert len(report["play_log_columns"]) == 22
    column_labels = [c["label"] for c in report["play_log_columns"]]
    assert "IDLE_TICK" not in column_labels
    assert "RECOVER" not in column_labels
    assert "FUTURE_5" not in column_labels
    assert "FUTURE_6" not in column_labels
    assert "BROWSER_VERIFICATION" not in column_labels
    assert "RECONCILE_STATE" in column_labels
    assert "PRUNE" in column_labels
    assert "FUTURE_4" in column_labels
    assert "FUTURE_7" in column_labels
    assert "FUTURE_8" in column_labels
    assert report["play_log_columns"][0]["label"] == "INSTANTIATE_AGENT"
    assert report["play_log_columns"][1]["label"] == "SEED_PROJECT"
    assert report["play_log_columns"][-1]["label"] == "END_SESSION"
    assert [row["play_type"] for row in report["play_log_rows"]] == [
        "issue_pickup",
        "issue_pickup",
        "merge_pr",
    ]
    # desktop-j8b: this test seeds plays without any matching AgentRecord,
    # so the rendering falls back to the bare agent_id. A separate test
    # below exercises the persisted-display_name path.
    assert report["play_log_rows"][0]["agent_name"] == "agent-1"
    assert report["play_log_unique_agents"] == 1
    assert report["play_log_plays_in_use"] == 2
    # Denominator: 22 registry entries with no internal heartbeats to subtract.
    assert report["play_log_total_slots"] == 22
    assert report["control_rejections"] == []


async def test_collect_end_session_report_play_log_uses_persisted_display_name(store):
    """desktop-j8b: when AgentRecord.display_name is set, the ESR play log
    renders that human-readable name instead of any UUID-derived fallback.
    """
    await _seed_session(store)
    await _seed_agent(
        store,
        agent_id="b90a79b5-0902-4146-ab10-b63f443c216a",
        agent_type="claude_code",
        model_tier="large",
        display_name="Claude/large: Ember Raven",
    )
    await _seed_play(
        store,
        play_type="issue_pickup",
        agent_id="b90a79b5-0902-4146-ab10-b63f443c216a",
        success=True,
    )
    await _seed_issues(store)

    collector = ReportDataCollector(store)
    report = await collector.collect_end_session_report(SID)

    assert report["play_log_rows"][0]["agent_name"] == "Claude/large: Ember Raven"


async def test_gated_skip_excluded_from_play_log(store):
    """A PPO-selected-then-gated play (skip:*, no agent) is omitted from the play log.

    Gated skips never reach an agent (0ms, $0, agent_id=None) — they are not
    executed plays, so they have no row in the play-log timeline. The per-type
    stats table still accounts for them via its ``skipped`` bucket.
    """
    await _seed_session(store)
    await _seed_agent(store, agent_id="agent-1", display_name="Claude/large: Ember Raven")
    # A real dispatched play that failed, and a gated skip (no agent).
    await _seed_play(store, play_type="issue_pickup", agent_id="agent-1", success=True)
    await _seed_play(
        store,
        play_type="write_implementation_plan",
        agent_id=None,
        success=False,
        dollar_cost=0.0,
        duration_ms=0,
        failure_category="skip:masked",
    )
    await _seed_issues(store)

    collector = ReportDataCollector(store)
    report = await collector.collect_end_session_report(SID)
    summary = await collector.collect_session_summary(SID)

    rows = {r["play_type"]: r for r in report["play_log_rows"]}
    # Gated skip absent from the play log; the real play remains.
    assert "write_implementation_plan" not in rows
    assert rows["issue_pickup"]["status"] == "ok"
    assert all(r["agent_name"] != "agentshore" for r in report["play_log_rows"])
    assert all(r["status"] != "skip" for r in report["play_log_rows"])

    # Overview: the skip is not a failure.
    ov = summary["overview"]
    assert ov["skipped_plays"] == 1
    assert ov["failed_plays"] == 0
    assert ov["successful_plays"] == 1

    # Skip lands in the skipped bucket (not failed) and is excluded from failure analysis.
    stats = {s["play_type"]: s for s in report["play_stats"]}
    assert stats["write_implementation_plan"]["skipped"] == 1
    assert stats["write_implementation_plan"]["failed"] == 0
    assert all(not fa["category"].startswith("skip:") for fa in summary["failure_analysis"])


async def test_collect_end_session_report_includes_control_rejection_counts(store):
    await _seed_session(store)
    await store.record_external_mutation(
        ExternalMutationRecord(
            session_id=SID,
            idempotency_key="dispatch-1",
            mutation_type="dispatch_revalidation_block",
            target="run_qa",
            status="recorded",
            created_at=NOW,
            request_json='{"reason": "run_qa cooldown (1/25 plays since last)"}',
        )
    )
    await store.record_external_mutation(
        ExternalMutationRecord(
            session_id=SID,
            idempotency_key="dispatch-2",
            mutation_type="dispatch_revalidation_block",
            target="run_qa",
            status="recorded",
            created_at=NOW,
            request_json='{"reason": "run_qa cooldown (1/25 plays since last)"}',
        )
    )

    report = await ReportDataCollector(store).collect_end_session_report(SID)

    assert report["control_rejections"] == [
        {
            "kind": "dispatch_revalidation_block",
            "play_type": "run_qa",
            "reason": "run_qa cooldown (1/25 plays since last)",
            "count": 2,
        }
    ]


async def test_collect_end_session_report_includes_fleet_concurrency(
    store,
    tmp_path,
    monkeypatch,
) -> None:
    project_path = tmp_path / "proj"
    project_path.mkdir()
    (project_path / "agentshore.yaml").write_text(
        """
agents:
  claude_code:
    enabled: true
    model_tiers:
      large:
        enabled: true
        max: 2
  codex:
    enabled: true
    model_tiers:
      medium:
        enabled: true
        max: 5
""",
        encoding="utf-8",
    )
    await _seed_session(store, project_path=str(project_path))
    monkeypatch.setattr("agentshore.session_path._SESSIONS_DIR", tmp_path / "sessions")
    log_path = session_dir(project_path) / CONCURRENCY_FILENAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "v": RECORD_VERSION,
                        "session_id": SID,
                        "seq": 1,
                        "ts": "2026-06-18T00:01:00+00:00",
                        "play_type": "issue_pickup",
                        "completed_agent_type": "claude_code",
                        "completed_model_tier": "large",
                        "completed_error_class": None,
                        "busy_total": 1,
                        "busy_by_type": {"claude_code": 1},
                        "busy_by_type_tier": {"claude_code/large": 1},
                    }
                ),
                json.dumps(
                    {
                        "v": RECORD_VERSION,
                        "session_id": SID,
                        "seq": 2,
                        "ts": "2026-06-18T00:02:00+00:00",
                        "play_type": "issue_pickup",
                        "completed_agent_type": "claude_code",
                        "completed_model_tier": "large",
                        "completed_error_class": "rate_limit",
                        "busy_total": 3,
                        "busy_by_type": {"claude_code": 2, "codex": 1},
                        "busy_by_type_tier": {"claude_code/large": 2, "codex/medium": 1},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = await ReportDataCollector(store).collect_end_session_report(SID)

    assert report["fleet_concurrency"] is not None
    assert report["fleet_concurrency"]["sample_count"] == 2
    assert report["fleet_concurrency"]["peak_busy"] == 3
    assert report["fleet_concurrency"]["peak_by_harness_tier"] == [
        {"label": "claude_code/large", "peak_busy": 2, "config_max": 2},
        {"label": "codex/medium", "peak_busy": 1, "config_max": 5},
    ]
    assert report["fleet_concurrency"]["rate_limit_samples"][0]["busy_total"] == 3
    assert report["fleet_concurrency"]["timeline"]["capacity_total"] == 7
    harness_caps = {
        row["label"]: row["capacity_max"]
        for row in report["fleet_concurrency"]["timeline"]["harnesses"]
    }
    assert harness_caps == {"claude_code": 2, "codex": 5}


async def test_collect_end_session_report_without_fleet_concurrency_log_degrades(
    store,
    tmp_path,
    monkeypatch,
) -> None:
    await _seed_session(store)
    await _seed_play(store)
    monkeypatch.setattr("agentshore.session_path._SESSIONS_DIR", tmp_path / "sessions")

    report = await ReportDataCollector(store).collect_end_session_report(SID)

    assert report["fleet_concurrency"] is None
    assert report["overview"]["session_id"] == SID


async def test_collect_session_summary_empty_session(store):
    await _seed_session(store, total_cost=0.0, final_alignment=None, ended_at=None)

    collector = ReportDataCollector(store)
    summary = await collector.collect_session_summary(SID)

    assert summary["overview"]["total_plays"] == 0
    assert summary["overview"]["successful_plays"] == 0
    assert summary["overview"]["failed_plays"] == 0
    assert summary["overview"]["duration_seconds"] == 0.0
    assert summary["overview"]["final_alignment"] is None
    assert summary["play_timeline"] == []
    assert summary["cost_breakdown"]["by_play_type"] == {}
    assert summary["cost_breakdown"]["cumulative"] == []
    assert summary["agent_performance"] == []
    assert summary["failure_analysis"] == []
    assert summary["scope_drift_count"] == 0
    assert summary["revert_count"] == 0
    assert summary["loop_incidents"] == []


async def test_collect_session_summary_cost_breakdown(store):
    await _seed_session(store)
    await _seed_play(store, play_type="issue_pickup", agent_id="a1", dollar_cost=1.0)
    await _seed_play(store, play_type="issue_pickup", agent_id="a1", dollar_cost=2.0)
    await _seed_play(store, play_type="code_review", agent_id="a2", dollar_cost=0.5)
    await _seed_agent(store, agent_id="a1", total_cost=3.0)
    await _seed_agent(store, agent_id="a2", total_cost=0.5)

    collector = ReportDataCollector(store)
    summary = await collector.collect_session_summary(SID)
    cb = summary["cost_breakdown"]

    assert cb["by_play_type"]["issue_pickup"] == pytest.approx(3.0)
    assert cb["by_play_type"]["code_review"] == pytest.approx(0.5)
    assert cb["by_agent"]["a1"] == pytest.approx(3.0)
    assert cb["by_agent"]["a2"] == pytest.approx(0.5)
    assert len(cb["cumulative"]) == 3
    assert cb["cumulative"][-1] == (2, pytest.approx(3.5))


async def test_collect_session_summary_agent_performance(store):
    await _seed_session(store)
    await _seed_agent(store, agent_id="a1", tasks_completed=7, tasks_failed=3, total_cost=4.0)
    await _seed_play(store, agent_id="a1", duration_ms=5000, started_at="2026-04-27T00:10:00+00:00")
    await _seed_play(
        store, agent_id="a1", duration_ms=15000, started_at="2026-04-27T00:11:00+00:00"
    )

    collector = ReportDataCollector(store)
    summary = await collector.collect_session_summary(SID)
    perf = summary["agent_performance"]

    assert len(perf) == 1
    a1 = perf[0]
    assert a1["agent_id"] == "a1"
    assert a1["agent_type"] == "claude_code"
    assert a1["tasks_completed"] == 7
    assert a1["tasks_failed"] == 3
    # success_rate = 7 / (7+3) = 0.7
    assert a1["success_rate"] == pytest.approx(0.7)
    assert a1["total_cost"] == pytest.approx(4.0)
    # avg duration = (5 + 15) / 2 = 10 seconds
    assert a1["avg_duration"] == pytest.approx(10.0)


async def test_collect_session_summary_agent_dispatch_share(store):
    """desktop-31h2: dispatch_share is each agent's slice of the fleet total.

    Three agents at 6 / 3 / 1 dispatches — shares should be 0.6 / 0.3 / 0.1.
    Empty-fleet case (no dispatches yet) defaults to 0.0 to avoid divide-by-zero.
    """
    await _seed_session(store)
    await _seed_agent(store, agent_id="a1", dispatch_count=6, tasks_completed=5)
    await _seed_agent(store, agent_id="a2", dispatch_count=3, tasks_completed=2)
    await _seed_agent(store, agent_id="a3", dispatch_count=1, tasks_completed=1)

    collector = ReportDataCollector(store)
    summary = await collector.collect_session_summary(SID)
    perf_by_id = {row["agent_id"]: row for row in summary["agent_performance"]}

    assert perf_by_id["a1"]["dispatch_count"] == 6
    assert perf_by_id["a1"]["dispatch_share"] == pytest.approx(0.6)
    assert perf_by_id["a2"]["dispatch_count"] == 3
    assert perf_by_id["a2"]["dispatch_share"] == pytest.approx(0.3)
    assert perf_by_id["a3"]["dispatch_count"] == 1
    assert perf_by_id["a3"]["dispatch_share"] == pytest.approx(0.1)


async def test_collect_session_summary_agent_dispatch_share_empty_fleet(store):
    """No dispatches yet → dispatch_share is 0.0 for every agent, not NaN."""
    await _seed_session(store)
    await _seed_agent(store, agent_id="a1", dispatch_count=0)
    await _seed_agent(store, agent_id="a2", dispatch_count=0)

    collector = ReportDataCollector(store)
    summary = await collector.collect_session_summary(SID)
    perf_by_id = {row["agent_id"]: row for row in summary["agent_performance"]}

    assert perf_by_id["a1"]["dispatch_share"] == 0.0
    assert perf_by_id["a2"]["dispatch_share"] == 0.0


async def test_collect_session_summary_play_timeline(store):
    await _seed_session(store)
    await _seed_play(store, started_at="2026-04-27T00:10:00+00:00", play_type="issue_pickup")
    await _seed_play(store, started_at="2026-04-27T00:20:00+00:00", play_type="code_review")
    await _seed_play(store, started_at="2026-04-27T00:30:00+00:00", play_type="run_qa")

    collector = ReportDataCollector(store)
    summary = await collector.collect_session_summary(SID)
    timeline = summary["play_timeline"]

    assert len(timeline) == 3
    # Returned in play_id order (chronological insertion).
    assert timeline[0]["play_type"] == "issue_pickup"
    assert timeline[1]["play_type"] == "code_review"
    assert timeline[2]["play_type"] == "run_qa"
    assert timeline[0]["duration_seconds"] == pytest.approx(10.0)


async def test_collect_session_summary_cluster_alignment(store):
    # Legacy compatibility field; beads graph closure is exposed through epic_summaries.
    await _seed_session(store)
    collector = ReportDataCollector(store)
    summary = await collector.collect_session_summary(SID)
    # Key must be present for old consumers, but remains empty now.
    assert "cluster_alignment" in summary
    assert summary["cluster_alignment"] == []


async def test_collect_session_summary_populates_epic_closure_from_beads_graph(store, monkeypatch):
    await _seed_session(store)
    await _seed_play(
        store, play_type="issue_pickup", alignment_delta=0.2, started_at="2026-04-27T00:01:00+00:00"
    )
    await _seed_play(
        store, play_type="merge_pr", alignment_delta=0.1, started_at="2026-04-27T00:02:00+00:00"
    )
    await _seed_play(
        store, play_type="run_qa", alignment_delta=0.1, started_at="2026-04-27T00:03:00+00:00"
    )

    async def _fake_load_graph(_project_path):
        return ProjectGraph(
            epics=[
                EpicStatus(
                    bead_id="epic-1",
                    title="Epic One",
                    total_tasks=10,
                    closed_tasks=7,
                    closure_ratio=0.7,
                )
            ],
            tasks=[],
            tasks_ready=0,
            tasks_total=10,
            global_closure_ratio=0.7,
        )

    monkeypatch.setattr("agentshore.reports.collector.load_graph", _fake_load_graph)

    summary = await ReportDataCollector(store).collect_session_summary(SID)

    assert len(summary["epic_summaries"]) == 1
    epic_summary = summary["epic_summaries"][0]
    assert epic_summary.bead_id == "epic-1"
    assert epic_summary.title == "Epic One"
    assert epic_summary.closure_ratio == pytest.approx(0.7)
    assert epic_summary.total_tasks == 10
    assert epic_summary.closed_tasks == 7
    timeline = summary["epic_closure_timeline"]
    assert timeline["global_ratio_start"] == pytest.approx(0.3)
    assert timeline["global_ratio_midpoint"] == pytest.approx(0.5)
    assert timeline["global_ratio_end"] == pytest.approx(0.7)
    assert timeline["tasks_closed_by_play_type"] == [
        {
            "play_type": "issue_pickup",
            "plays_executed": 1,
            "estimated_tasks_closed": 4,
        },
        {
            "play_type": "merge_pr",
            "plays_executed": 1,
            "estimated_tasks_closed": 2,
        },
        {
            "play_type": "run_qa",
            "plays_executed": 1,
            "estimated_tasks_closed": 2,
        },
    ]


async def test_collect_session_summary_defaults_epic_summaries_when_no_beads_data(store):
    await _seed_session(store)

    collector = ReportDataCollector(store)
    summary = await collector.collect_session_summary(SID)

    assert "epic_summaries" in summary
    assert summary["epic_summaries"] == []


async def test_collect_session_summary_failure_analysis(store):
    await _seed_session(store)
    await _seed_play(
        store,
        success=False,
        failure_category="timeout",
        error="timed out",
        started_at="2026-04-27T00:10:00+00:00",
    )
    await _seed_play(
        store,
        success=False,
        failure_category="timeout",
        error="timed out again",
        started_at="2026-04-27T00:11:00+00:00",
    )
    await _seed_play(
        store,
        success=False,
        failure_category="parse_error",
        error="bad JSON",
        started_at="2026-04-27T00:12:00+00:00",
    )

    collector = ReportDataCollector(store)
    summary = await collector.collect_session_summary(SID)
    failures = summary["failure_analysis"]

    categories = {f["category"]: f["count"] for f in failures}
    assert categories["timeout"] == 2
    assert categories["parse_error"] == 1


async def test_compute_loop_incidents_structured(store):
    """Structured loop-incident list captures play_type, peak_streak, tier, resolution.

    Scenario seeded:
      (a) code_review fails 4 times then succeeds (same type)
          → tier=warning, resolution=succeeded_after_streak_4
      (b) merge_pr fails 6 times, then a different play type (code_review success)
          → tier=force_switch, resolution=force_masked
      (c) run_qa fails 7 times and the session ends mid-streak
          → tier=escalation, resolution=human_escalation
    """
    await _seed_session(store)

    base = "2026-04-27T00:"

    async def _add(play_type: str, success: bool, minute: int) -> None:
        await _seed_play(
            store,
            play_type=play_type,
            success=success,
            started_at=f"{base}{minute:02d}:00+00:00",
        )

    minute = 0
    # (a) code_review: 4 failures then a same-type success
    for _ in range(4):
        await _add("code_review", False, minute)
        minute += 1
    await _add("code_review", True, minute)
    minute += 1

    # (b) merge_pr: 6 failures then a different play type
    for _ in range(6):
        await _add("merge_pr", False, minute)
        minute += 1
    await _add("code_review", True, minute)
    minute += 1

    # (c) run_qa: 7 failures, session ends in streak
    for _ in range(7):
        await _add("run_qa", False, minute)
        minute += 1

    collector = ReportDataCollector(store)
    summary = await collector.collect_session_summary(SID)
    incidents = summary["loop_incidents"]

    assert len(incidents) == 3

    inc_a, inc_b, inc_c = incidents

    assert inc_a["play_type"] == "code_review"
    assert inc_a["peak_streak"] == 4
    assert inc_a["tier"] == "warning"
    assert inc_a["resolution"] == "succeeded_after_streak_4"
    assert inc_a["start_play_index"] == 0
    assert inc_a["end_play_index"] == 3
    assert inc_a["ended_at"] == "2026-04-27T00:03:00+00:00"

    assert inc_b["play_type"] == "merge_pr"
    assert inc_b["peak_streak"] == 6
    assert inc_b["tier"] == "force_switch"
    assert inc_b["resolution"] == "force_masked"
    assert inc_b["start_play_index"] == 5
    assert inc_b["end_play_index"] == 10
    assert inc_b["ended_at"] == "2026-04-27T00:10:00+00:00"

    assert inc_c["play_type"] == "run_qa"
    assert inc_c["peak_streak"] == 7
    assert inc_c["tier"] == "escalation"
    assert inc_c["resolution"] == "human_escalation"


async def test_collect_session_summary_recommendations(store):
    await _seed_session(store)
    # 2 completed / 5 failed = 71% fail rate, over the 30% recommendation threshold.
    await _seed_agent(store, agent_id="bad-agent", tasks_completed=2, tasks_failed=5)

    collector = ReportDataCollector(store)
    summary = await collector.collect_session_summary(SID)

    recs = summary["recommendations"]
    assert len(recs) >= 1
    assert any("bad-agent" in r for r in recs)
    assert any("failure rate" in r for r in recs)


async def test_collect_progress_report(store):
    await _seed_session(store, ended_at=None, final_alignment=None)
    await _seed_play(store)
    await _seed_agent(store)
    play_id = await _seed_play(store, started_at="2026-04-27T00:20:00+00:00")
    await store.record_trajectory_snapshot(
        TrajectorySnapshotRecord(
            session_id=SID,
            play_id=play_id,
            projected_alignment_at_budget_end=0.9,
            estimated_remaining_plays=5,
            estimated_remaining_cost=2.5,
            created_at="2026-04-27T00:20:00+00:00",
        )
    )

    collector = ReportDataCollector(store)
    report = await collector.collect_progress_report(SID)

    assert "overview" in report
    assert "cluster_alignment" in report
    assert "recent_plays" in report
    assert "budget_remaining" in report
    assert "active_agents" in report

    assert report["budget_remaining"] == pytest.approx(2.5)
    assert len(report["active_agents"]) == 1
    assert report["active_agents"][0]["agent_id"] == "agent-1"


async def test_collect_progress_report_recent_plays(store):
    await _seed_session(store)
    for i in range(15):
        await _seed_play(
            store,
            started_at=f"2026-04-27T00:{i:02d}:00+00:00",
            dollar_cost=0.1,
        )

    collector = ReportDataCollector(store)
    report = await collector.collect_progress_report(SID)

    # Capped at the last 10 plays.
    assert len(report["recent_plays"]) == 10


async def test_collect_comparison(store):
    await _seed_session(
        store,
        session_id="s-a",
        total_cost=3.0,
        final_alignment=0.7,
        total_plays=5,
    )
    await _seed_session(
        store,
        session_id="s-b",
        total_cost=5.0,
        final_alignment=0.9,
        total_plays=8,
    )
    for _ in range(2):
        await _seed_play(store, session_id="s-a", dollar_cost=1.0)
    for _ in range(4):
        await _seed_play(store, session_id="s-b", dollar_cost=0.5)

    collector = ReportDataCollector(store)
    comparison = await collector.collect_comparison("s-a", "s-b")

    assert comparison["session_a"]["session_id"] == "s-a"
    assert comparison["session_b"]["session_id"] == "s-b"
    # cost_diff uses the play-sum overview (H4): s-b 4×0.5=2.0 minus s-a 2×1.0=2.0.
    assert comparison["cost_diff"] == pytest.approx(0.0)
    # alignment_diff = 0.9 - 0.7 = 0.2
    assert comparison["alignment_diff"] == pytest.approx(0.2)
    # play_count_diff = 4 - 2 = 2 (actual plays in DB)
    assert comparison["play_count_diff"] == 2


async def test_collect_session_summary_issue_inflation(store):
    await _seed_session(store)
    await _seed_play(store, started_at="2026-04-27T00:01:00+00:00")
    await _seed_play(store, started_at="2026-04-27T00:02:00+00:00")
    await _seed_play(store, started_at="2026-04-27T00:03:00+00:00")
    await store.cache_github_issues(
        SID,
        [
            GitHubIssueRecord(
                issue_number=1,
                session_id=SID,
                title="Open issue 1",
                state="open",
                created_at="2026-04-27T00:01:00+00:00",
                labels=["agentshore/review", "agentshore/approved", "agentshore/author:codex"],
            ),
            GitHubIssueRecord(
                issue_number=2,
                session_id=SID,
                title="Open issue 2",
                state="open",
                created_at="2026-04-27T00:02:00+00:00",
                labels=["agentshore/follow-up", "bug"],
            ),
            GitHubIssueRecord(
                issue_number=3,
                session_id=SID,
                title="Closed issue",
                state="closed",
                created_at="2026-04-27T00:01:30+00:00",
                labels=["agentshore/qa", "agentshore/source:seed-project", "agentshore/blocked"],
                closed_at="2026-04-27T00:03:00+00:00",
            ),
        ],
    )

    collector = ReportDataCollector(store)
    summary = await collector.collect_session_summary(SID)
    inflation = summary["issue_inflation"]

    assert inflation["total_opened"] == 2
    assert inflation["total_closed"] == 1
    assert inflation["per_play"] == [(1, 1, 0, 1), (2, 2, 0, 2), (3, 0, 1, -1)]
    assert inflation["warnings_triggered"] == 0
    assert inflation["by_source"] == {
        "agentshore/follow-up": 1,
        "agentshore/qa": 1,
        "agentshore/review": 1,
        "agentshore/source:seed-project": 1,
    }
    assert inflation["recovery"]["reversed"] is True
    assert inflation["recovery"]["contributing_plays"] == [3]
    assert inflation["ratio"] == pytest.approx(2.0)


@pytest.mark.parametrize(
    ("projected_values", "expected_trend"),
    [
        ([0.2, 0.4, 0.7], "converging"),
        ([0.8, 0.5, 0.2], "diverging"),
        ([0.5, 0.5, 0.5], "flat"),
    ],
)
async def test_collect_session_summary_trajectory_analysis_trend(
    store,
    projected_values: list[float],
    expected_trend: str,
):
    await _seed_session(store)
    play_ids: list[int] = []
    for idx, _ in enumerate(projected_values, start=1):
        play_ids.append(
            await _seed_play(
                store,
                dollar_cost=0.25,
                started_at=f"2026-04-27T00:0{idx}:00+00:00",
            )
        )
    for play_id, projected in zip(play_ids, projected_values, strict=True):
        await store.record_trajectory_snapshot(
            TrajectorySnapshotRecord(
                session_id=SID,
                play_id=play_id,
                projected_alignment_at_budget_end=projected,
                estimated_remaining_plays=10 - play_id,
                estimated_remaining_cost=6.0 - play_id,
                created_at=f"2026-04-27T00:{play_id:02d}:00+00:00",
            )
        )

    summary = await ReportDataCollector(store).collect_session_summary(SID)
    assert summary["trajectory_analysis"]["trend"] == expected_trend


async def test_collect_session_summary_trajectory_analysis_cost_and_sufficiency(store):
    await _seed_session(store, total_cost=6.0)
    # Actual cost at session end = 6.0, early estimate from first snapshot = 8.0.
    play_ids = [
        await _seed_play(store, dollar_cost=2.0, started_at="2026-04-27T00:10:00+00:00"),
        await _seed_play(store, dollar_cost=2.0, started_at="2026-04-27T00:11:00+00:00"),
        await _seed_play(store, dollar_cost=2.0, started_at="2026-04-27T00:12:00+00:00"),
    ]

    snapshots = [
        (0, 0.30, 6.0),  # 25% consumed checkpoint should be sufficient
        (1, 0.45, 5.0),  # 50% consumed checkpoint should be sufficient
        (2, 0.55, 3.0),  # 75% consumed checkpoint should be insufficient
    ]
    for index, projected_alignment, remaining_cost in snapshots:
        await store.record_trajectory_snapshot(
            TrajectorySnapshotRecord(
                session_id=SID,
                play_id=play_ids[index],
                projected_alignment_at_budget_end=projected_alignment,
                estimated_remaining_plays=6 - (index + 1),
                estimated_remaining_cost=remaining_cost,
                created_at=f"2026-04-27T00:1{index + 1}:00+00:00",
            )
        )

    summary = await ReportDataCollector(store).collect_session_summary(SID)
    analysis = summary["trajectory_analysis"]

    assert analysis["estimated_total_cost_early"] == pytest.approx(8.0)
    assert analysis["actual_total_cost"] == pytest.approx(6.0)
    assert analysis["budget_sufficiency"] == [
        {"budget_consumed_pct": 25.0, "projected_sufficient": True},
        {"budget_consumed_pct": 50.0, "projected_sufficient": True},
        {"budget_consumed_pct": 75.0, "projected_sufficient": False},
    ]


async def test_trajectory_analysis_uses_orchestrator_snapshots(store):
    await _seed_session(store, total_cost=6.0)
    play_ids = [
        await _seed_play(store, dollar_cost=2.0, started_at="2026-04-27T00:20:00+00:00"),
        await _seed_play(store, dollar_cost=2.0, started_at="2026-04-27T00:21:00+00:00"),
        await _seed_play(store, dollar_cost=2.0, started_at="2026-04-27T00:22:00+00:00"),
    ]

    snapshots = [
        (play_ids[0], 0.20, 6.0),
        (play_ids[1], 0.55, 4.0),
        (play_ids[2], 0.80, 2.0),
    ]
    for play_id, projected_alignment, remaining_cost in snapshots:
        await store.record_trajectory_snapshot(
            TrajectorySnapshotRecord(
                session_id=SID,
                play_id=play_id,
                projected_alignment_at_budget_end=projected_alignment,
                estimated_remaining_plays=4,
                estimated_remaining_cost=remaining_cost,
                created_at=f"2026-04-27T00:{play_id:02d}:00+00:00",
            )
        )

    summary = await ReportDataCollector(store).collect_session_summary(SID)
    analysis = summary["trajectory_analysis"]
    assert analysis["trend"] == "converging"
    assert any(row["projected_sufficient"] for row in analysis["budget_sufficiency"])


async def test_collect_session_summary_issue_inflation_warning_streak(store):
    await _seed_session(store)
    for i in range(1, 8):
        await _seed_play(store, started_at=f"2026-04-27T00:0{i}:00+00:00")

    issues = [
        GitHubIssueRecord(
            issue_number=i,
            session_id=SID,
            title=f"Open issue {i}",
            state="open",
            created_at=f"2026-04-27T00:0{i}:00+00:00",
        )
        for i in range(1, 7)
    ]
    issues.append(
        GitHubIssueRecord(
            issue_number=99,
            session_id=SID,
            title="Closed issue",
            state="closed",
            created_at="2026-04-27T00:07:00+00:00",
            closed_at="2026-04-27T00:07:00+00:00",
        )
    )
    await store.cache_github_issues(SID, issues)

    collector = ReportDataCollector(store)
    summary = await collector.collect_session_summary(SID)
    inflation = summary["issue_inflation"]

    assert inflation["warnings_triggered"] == 1


# Issue #333: per-(agent, play_type) specialization breakdown.
async def test_collect_session_summary_agent_specialization(store):
    await _seed_session(store)
    await _seed_agent(store, agent_id="agent-a", tasks_completed=2, tasks_failed=2)
    await _seed_play(
        store,
        agent_id="agent-a",
        play_type="issue_pickup",
        success=True,
        started_at="2026-04-27T00:10:00+00:00",
    )
    await _seed_play(
        store,
        agent_id="agent-a",
        play_type="issue_pickup",
        success=False,
        started_at="2026-04-27T00:11:00+00:00",
    )
    await _seed_play(
        store,
        agent_id="agent-a",
        play_type="code_review",
        success=True,
        started_at="2026-04-27T00:12:00+00:00",
    )
    await _seed_play(
        store,
        agent_id="agent-a",
        play_type="run_qa",
        success=False,
        started_at="2026-04-27T00:13:00+00:00",
    )

    collector = ReportDataCollector(store)
    summary = await collector.collect_session_summary(SID)
    spec = summary["agent_specialization"]

    by_play = {row["play_type"]: row for row in spec}

    pickup = by_play["issue_pickup"]
    assert pickup["agent_id"] == "agent-a"
    assert pickup["total"] == 2
    assert pickup["successful"] == 1
    assert pickup["failed"] == 1
    assert pickup["success_rate"] == pytest.approx(0.5)
    assert pickup["rolling_success_rate"] == pytest.approx(0.5)

    review = by_play["code_review"]
    assert review["total"] == 1
    assert review["successful"] == 1
    assert review["success_rate"] == pytest.approx(1.0)

    qa = by_play["run_qa"]
    assert qa["total"] == 1
    assert qa["failed"] == 1
    assert qa["success_rate"] == pytest.approx(0.0)
