"""Tests for the expanded ComparisonData — collector + template rendering."""

from __future__ import annotations

import pytest

from agentshore.data.models import SessionLearningRecord
from agentshore.data.store import (
    AgentRecord,
    DataStore,
    GitHubIssueRecord,
    PlayRecord,
    SessionRecord,
)
from agentshore.reports.collector import ReportDataCollector
from agentshore.reports.generator import ReportGenerator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = "2026-04-27T00:00:00+00:00"
END = "2026-04-27T01:00:00+00:00"


@pytest.fixture
async def store(tmp_path):
    db = DataStore(tmp_path / "cmp_test.db")
    await db.initialize()
    yield db
    await db.close()


async def _make_session(
    store: DataStore,
    sid: str,
    *,
    total_cost: float = 4.0,
    final_alignment: float | None = 0.8,
) -> None:
    await store.create_session(
        SessionRecord(
            session_id=sid,
            project_path="/tmp/proj",
            started_at=NOW,
            ended_at=END,
            status="completed",
            total_cost=total_cost,
            total_plays=0,
            final_alignment=final_alignment,
        )
    )


async def _add_play(
    store: DataStore,
    sid: str,
    *,
    play_type: str = "issue_pickup",
    agent_id: str | None = "ag-1",
    success: bool = True,
    dollar_cost: float = 1.0,
    alignment_delta: float | None = 0.05,
) -> None:
    await store.record_play(
        PlayRecord(
            session_id=sid,
            play_type=play_type,
            agent_id=agent_id,
            started_at=NOW,
            success=success,
            dollar_cost=dollar_cost,
            alignment_delta=alignment_delta,
        )
    )


async def _add_agent(store: DataStore, sid: str, agent_id: str = "ag-1") -> None:
    await store.register_agent(
        AgentRecord(
            agent_id=agent_id,
            session_id=sid,
            agent_type="claude_code",
            created_at=NOW,
            tasks_completed=3,
            tasks_failed=1,
            total_cost=2.0,
        )
    )


async def _add_issues(store: DataStore, sid: str, *, opened: int = 2, closed: int = 3) -> None:
    issues = []
    for i in range(opened):
        issues.append(
            GitHubIssueRecord(
                issue_number=100 + i,
                session_id=sid,
                title=f"Open {i}",
                state="open",
                created_at=NOW,
            )
        )
    for i in range(closed):
        issues.append(
            GitHubIssueRecord(
                issue_number=200 + i,
                session_id=sid,
                title=f"Closed {i}",
                state="closed",
                created_at=NOW,
                closed_at=END,
            )
        )
    await store.cache_github_issues(sid, issues)


async def _add_learning(store: DataStore, sid: str, pattern: str) -> None:
    await store.record_learning(
        SessionLearningRecord(
            session_id=sid,
            pattern=pattern,
            category="general",
            created_at=NOW,
            last_reinforced_at=NOW,
        )
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_comparison_new_fields_populated(store):
    await _make_session(store, "s-a", total_cost=3.0, final_alignment=0.7)
    await _make_session(store, "s-b", total_cost=5.0, final_alignment=0.9)

    for _ in range(2):
        await _add_play(
            store,
            "s-a",
            play_type="issue_pickup",
            agent_id="ag-a",
            dollar_cost=1.0,
            alignment_delta=0.1,
        )
    await _add_play(
        store,
        "s-a",
        play_type="code_review",
        agent_id="ag-a",
        dollar_cost=0.5,
        alignment_delta=0.05,
    )
    for _ in range(3):
        await _add_play(
            store,
            "s-b",
            play_type="issue_pickup",
            agent_id="ag-b",
            dollar_cost=0.8,
            alignment_delta=0.1,
        )
    await _add_play(
        store, "s-b", play_type="merge_pr", agent_id="ag-b", dollar_cost=0.2, alignment_delta=0.0
    )

    await _add_agent(store, "s-a", agent_id="ag-a")
    await _add_agent(store, "s-b", agent_id="ag-b")

    collector = ReportDataCollector(store)
    cmp = await collector.collect_comparison("s-a", "s-b")

    # existing scalars still present
    assert cmp["cost_diff"] == pytest.approx(2.0)
    assert cmp["alignment_diff"] == pytest.approx(0.2)
    assert cmp["play_count_diff"] == 1  # 4 - 3

    # cost_breakdown fields
    cb_a = cmp["cost_breakdown_a"]
    assert "issue_pickup" in cb_a["by_play_type"]
    assert cb_a["by_play_type"]["issue_pickup"] == pytest.approx(2.0)
    assert "code_review" in cb_a["by_play_type"]
    assert "ag-a" in cb_a["by_agent"]

    cb_b = cmp["cost_breakdown_b"]
    assert "merge_pr" in cb_b["by_play_type"]

    # play_distribution fields
    dist_a = cmp["play_distribution_a"]
    assert dist_a["issue_pickup"] == 2
    assert dist_a["code_review"] == 1

    dist_b = cmp["play_distribution_b"]
    assert dist_b["issue_pickup"] == 3
    assert dist_b["merge_pr"] == 1


async def test_comparison_issue_throughput(store):
    await _make_session(store, "s-a")
    await _make_session(store, "s-b")

    await _add_issues(store, "s-a", opened=2, closed=3)
    await _add_issues(store, "s-b", opened=5, closed=1)

    collector = ReportDataCollector(store)
    cmp = await collector.collect_comparison("s-a", "s-b")

    tp_a = cmp["issue_throughput_a"]
    assert tp_a["opened"] == 2
    assert tp_a["closed"] == 3
    assert tp_a["net_velocity"] == 1  # 3 - 2

    tp_b = cmp["issue_throughput_b"]
    assert tp_b["opened"] == 5
    assert tp_b["closed"] == 1
    assert tp_b["net_velocity"] == -4  # 1 - 5


async def test_comparison_learnings_diff(store):
    await _make_session(store, "s-a")
    await _make_session(store, "s-b")

    await _add_learning(store, "s-a", "always run tests before merging")
    await _add_learning(store, "s-a", "prefer small commits")
    await _add_learning(store, "s-b", "always run tests before merging")
    await _add_learning(store, "s-b", "use conventional commits")

    collector = ReportDataCollector(store)
    cmp = await collector.collect_comparison("s-a", "s-b")

    diff = cmp["learnings_diff"]
    assert "use conventional commits" in diff["added"]
    assert "prefer small commits" in diff["removed"]
    assert "always run tests before merging" in diff["shared"]


async def test_comparison_alignment_trajectory(store):
    await _make_session(store, "s-a")
    await _make_session(store, "s-b")

    await _add_play(store, "s-a", alignment_delta=0.1)
    await _add_play(store, "s-a", alignment_delta=0.2)
    await _add_play(store, "s-a", alignment_delta=-0.05)

    collector = ReportDataCollector(store)
    cmp = await collector.collect_comparison("s-a", "s-b")

    traj_a = cmp["alignment_trajectory_a"]
    assert len(traj_a) == 3
    assert traj_a[0]["play_index"] == 0
    assert traj_a[0]["alignment"] == pytest.approx(0.1)
    assert traj_a[1]["alignment"] == pytest.approx(0.3)
    assert traj_a[2]["alignment"] == pytest.approx(0.25)

    # session_b has no plays — empty trajectory
    assert cmp["alignment_trajectory_b"] == []


async def test_comparison_empty_sessions(store):
    await _make_session(store, "s-a", total_cost=0.0, final_alignment=None)
    await _make_session(store, "s-b", total_cost=0.0, final_alignment=None)

    collector = ReportDataCollector(store)
    cmp = await collector.collect_comparison("s-a", "s-b")

    assert cmp["play_distribution_a"] == {}
    assert cmp["play_distribution_b"] == {}
    assert cmp["issue_throughput_a"] == {"opened": 0, "closed": 0, "net_velocity": 0}
    assert cmp["issue_throughput_b"] == {"opened": 0, "closed": 0, "net_velocity": 0}
    assert cmp["learnings_diff"] == {"added": [], "removed": [], "shared": []}
    assert cmp["alignment_trajectory_a"] == []
    assert cmp["alignment_trajectory_b"] == []


async def test_comparison_template_renders(store, tmp_path):
    """Template must render without error given the full ComparisonData shape."""
    await _make_session(store, "s-a", total_cost=3.0, final_alignment=0.7)
    await _make_session(store, "s-b", total_cost=5.0, final_alignment=0.9)

    await _add_play(store, "s-a", play_type="issue_pickup", dollar_cost=1.0, alignment_delta=0.1)
    await _add_play(store, "s-a", play_type="code_review", dollar_cost=0.5, alignment_delta=0.05)
    await _add_play(store, "s-b", play_type="issue_pickup", dollar_cost=0.8, alignment_delta=0.2)
    await _add_play(store, "s-b", play_type="merge_pr", dollar_cost=0.2, alignment_delta=0.0)

    await _add_issues(store, "s-a", opened=1, closed=2)
    await _add_issues(store, "s-b", opened=3, closed=1)

    await _add_learning(store, "s-a", "write tests first")
    await _add_learning(store, "s-b", "write tests first")
    await _add_learning(store, "s-b", "keep PRs small")

    generator = ReportGenerator(store)

    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    report_path = await generator.generate_comparison("s-a", "s-b", output_dir)

    html = report_path.read_text()
    assert "Comparison" in html
    assert "Cost" in html
    assert "Play Distribution" in html
    assert "Alignment Trajectory" in html
    assert "Learnings Diff" in html
    assert "write tests first" in html
    assert "keep PRs small" in html
    assert "added" in html
    assert "shared" in html
