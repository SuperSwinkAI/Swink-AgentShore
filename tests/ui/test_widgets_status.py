"""Tests for the four TUI status-display widgets (W1.4)."""

from __future__ import annotations

from textual.app import App, ComposeResult

from agentshore.beads import EpicStatus, ProjectGraph
from agentshore.state import (
    BudgetSnapshot,
    IssueSnapshot,
    OrchestratorState,
    PendingReviewSnapshot,
    PlayType,
    PullRequestSnapshot,
    SessionState,
    TrajectorySnapshot,
)

# ---------------------------------------------------------------------------
# Minimal host apps
# ---------------------------------------------------------------------------


class _AlignmentApp(App[None]):
    def compose(self) -> ComposeResult:
        from agentshore.ui.widgets.alignment import AlignmentBars

        yield AlignmentBars()


class _BudgetApp(App[None]):
    def compose(self) -> ComposeResult:
        from agentshore.ui.widgets.budget import BudgetWidget

        yield BudgetWidget()


class _RLStateApp(App[None]):
    def compose(self) -> ComposeResult:
        from agentshore.ui.widgets.rl_state import RLStateBar

        yield RLStateBar()


class _AlertApp(App[None]):
    def compose(self) -> ComposeResult:
        from agentshore.ui.widgets.alert_bar import AlertBar

        yield AlertBar()


class _WorkQueueApp(App[None]):
    def compose(self) -> ComposeResult:
        from agentshore.ui.widgets.work_queue import WorkQueueSummary

        yield WorkQueueSummary()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_graph(closure_ratio: float) -> ProjectGraph:
    return ProjectGraph(
        epics=[
            EpicStatus(
                bead_id="epic-1",
                title="Auth",
                total_tasks=4,
                closed_tasks=round(closure_ratio * 4),
                closure_ratio=closure_ratio,
            )
        ],
        global_closure_ratio=closure_ratio,
        tasks_ready=1,
        tasks_total=4,
    )


def _make_budget(total: float, spent: float) -> BudgetSnapshot:
    return BudgetSnapshot(
        total_budget=total,
        spent=spent,
        remaining=total - spent,
        estimated_cost_per_play=0.1,
    )


def _make_state(
    total_plays: int = 0, total_cost: float = 0.0, streak: int = 0
) -> OrchestratorState:
    return OrchestratorState(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=total_plays,
        total_cost=total_cost,
        same_type_failure_streak=streak,
    )


# ---------------------------------------------------------------------------
# AlignmentBars tests
# ---------------------------------------------------------------------------


async def test_alignment_bars_no_graph() -> None:
    async with _AlignmentApp().run_test() as pilot:
        from agentshore.ui.widgets.alignment import AlignmentBars

        w = pilot.app.query_one(AlignmentBars)
        assert "No beads graph" in w.render()


async def test_alignment_bars_shows_global_ratio() -> None:
    async with _AlignmentApp().run_test() as pilot:
        from agentshore.ui.widgets.alignment import AlignmentBars

        w = pilot.app.query_one(AlignmentBars)
        w.update_clusters(_make_graph(0.8))
        await pilot.pause()
        assert "Global closure" in w.render()
        assert "Auth" in w.render()


async def test_alignment_bars_high_level_label() -> None:
    async with _AlignmentApp().run_test() as pilot:
        from agentshore.ui.widgets.alignment import AlignmentBars

        w = pilot.app.query_one(AlignmentBars)
        w.update_clusters(_make_graph(0.8))
        await pilot.pause()
        assert "[HIGH]" in w.render()


async def test_alignment_bars_low_level_label() -> None:
    async with _AlignmentApp().run_test() as pilot:
        from agentshore.ui.widgets.alignment import AlignmentBars

        w = pilot.app.query_one(AlignmentBars)
        w.update_clusters(_make_graph(0.2))
        await pilot.pause()
        assert "[LOW]" in w.render()


async def test_alignment_bars_high_class() -> None:
    async with _AlignmentApp().run_test() as pilot:
        from agentshore.ui.widgets.alignment import AlignmentBars

        w = pilot.app.query_one(AlignmentBars)
        w.update_clusters(_make_graph(0.8))
        await pilot.pause()
        assert w.has_class("bar--high")


async def test_alignment_bars_med_class() -> None:
    async with _AlignmentApp().run_test() as pilot:
        from agentshore.ui.widgets.alignment import AlignmentBars

        w = pilot.app.query_one(AlignmentBars)
        w.update_clusters(_make_graph(0.4))
        await pilot.pause()
        assert w.has_class("bar--med")


async def test_alignment_bars_low_class() -> None:
    async with _AlignmentApp().run_test() as pilot:
        from agentshore.ui.widgets.alignment import AlignmentBars

        w = pilot.app.query_one(AlignmentBars)
        w.update_clusters(_make_graph(0.1))
        await pilot.pause()
        assert w.has_class("bar--low")


# ---------------------------------------------------------------------------
# BudgetWidget tests
# ---------------------------------------------------------------------------


async def test_budget_healthy_class() -> None:
    async with _BudgetApp().run_test() as pilot:
        from agentshore.ui.widgets.budget import BudgetWidget

        w = pilot.app.query_one(BudgetWidget)
        w.update_budget(_make_budget(total=10.0, spent=2.0))  # 80% remaining
        await pilot.pause()
        assert w.has_class("budget--healthy")


async def test_budget_warning_class() -> None:
    async with _BudgetApp().run_test() as pilot:
        from agentshore.ui.widgets.budget import BudgetWidget

        w = pilot.app.query_one(BudgetWidget)
        w.update_budget(_make_budget(total=10.0, spent=7.0))  # 30% remaining → warning
        await pilot.pause()
        assert w.has_class("budget--warning")


async def test_budget_exhausted_class() -> None:
    async with _BudgetApp().run_test() as pilot:
        from agentshore.ui.widgets.budget import BudgetWidget

        w = pilot.app.query_one(BudgetWidget)
        w.update_budget(_make_budget(total=10.0, spent=10.0))  # 0% remaining
        await pilot.pause()
        assert w.has_class("budget--exhausted")


async def test_budget_none_shows_na() -> None:
    async with _BudgetApp().run_test() as pilot:
        from agentshore.ui.widgets.budget import BudgetWidget

        w = pilot.app.query_one(BudgetWidget)
        # budget starts as None
        assert "N/A" in w.render()


async def test_budget_shows_trajectory_projection() -> None:
    async with _BudgetApp().run_test() as pilot:
        from agentshore.ui.widgets.budget import BudgetWidget

        w = pilot.app.query_one(BudgetWidget)
        w.update_budget(
            _make_budget(total=10.0, spent=4.0),
            TrajectorySnapshot(
                projected_alignment_at_budget_end=0.72,
                estimated_remaining_plays=9,
                estimated_remaining_cost=2.88,
            ),
        )
        await pilot.pause()
        rendered = w.render()
        assert "9 plays" in rendered
        assert "projected alignment 72%" in rendered


# ---------------------------------------------------------------------------
# RLStateBar tests
# ---------------------------------------------------------------------------


async def test_rl_state_bar_none_shows_dash() -> None:
    async with _RLStateApp().run_test() as pilot:
        from agentshore.ui.widgets.rl_state import RLStateBar

        w = pilot.app.query_one(RLStateBar)
        assert "--" in w.render()


async def test_rl_state_bar_shows_plays() -> None:
    async with _RLStateApp().run_test() as pilot:
        from agentshore.ui.widgets.rl_state import RLStateBar

        w = pilot.app.query_one(RLStateBar)
        w.update_state(_make_state(total_plays=42))
        await pilot.pause()
        assert "42" in w.render()


async def test_rl_state_bar_shows_paused_stats_and_mask_counts() -> None:
    async with _RLStateApp().run_test() as pilot:
        from agentshore.state import PlayTypeStatsSnapshot, SessionStatsSnapshot
        from agentshore.ui.widgets.rl_state import RLStateBar

        w = pilot.app.query_one(RLStateBar)
        state = _make_state(total_plays=4, total_cost=1.5, streak=2)
        state.session_state = SessionState.PAUSED
        state.same_type_streak = 3
        state.last_play_type = PlayType.RUN_QA
        state.action_mask = (True, False, True)
        state.stats = SessionStatsSnapshot(
            total_plays=4,
            successful_plays=3,
            failed_plays=1,
            success_rate=0.75,
            total_cost=1.5,
            avg_cost_per_play=0.375,
            total_tokens=1000,
            avg_duration_seconds=12.0,
            by_play_type=[
                PlayTypeStatsSnapshot(
                    play_type="run_qa",
                    total=1,
                    successful=1,
                    failed=0,
                    success_rate=1.0,
                    total_cost=0.1,
                    avg_duration_seconds=1.0,
                )
            ],
        )
        w.update_state(state)
        await pilot.pause()
        rendered = w.render()
        assert "state=paused" in rendered
        assert "policy=learning" in rendered
        assert "success=75%" in rendered
        assert "eligible=2 masked=1" in rendered


async def test_rl_state_bar_loop_warning_at_streak_3() -> None:
    async with _RLStateApp().run_test() as pilot:
        from agentshore.ui.widgets.rl_state import RLStateBar

        w = pilot.app.query_one(RLStateBar)
        state = _make_state(streak=3)
        state.last_play_type = PlayType.ISSUE_PICKUP
        w.update_state(state)
        await pilot.pause()
        assert "Loop: Issue Pickup failed 3x" in w.render()
        assert w.has_class("loop--warning")


async def test_rl_state_bar_loop_force_at_streak_5() -> None:
    async with _RLStateApp().run_test() as pilot:
        from agentshore.ui.widgets.rl_state import RLStateBar

        w = pilot.app.query_one(RLStateBar)
        state = _make_state(streak=5)
        state.last_play_type = PlayType.ISSUE_PICKUP
        w.update_state(state)
        await pilot.pause()
        assert "Loop: Issue Pickup blocked (5x fail)" in w.render()
        assert w.has_class("loop--force")


async def test_rl_state_bar_loop_clears_on_reset() -> None:
    async with _RLStateApp().run_test() as pilot:
        from agentshore.ui.widgets.rl_state import RLStateBar

        w = pilot.app.query_one(RLStateBar)
        state = _make_state(streak=5)
        state.last_play_type = PlayType.ISSUE_PICKUP
        w.update_state(state)
        await pilot.pause()
        reset = _make_state(streak=0)
        reset.last_play_type = PlayType.ISSUE_PICKUP
        w.update_state(reset)
        await pilot.pause()
        assert not w.has_class("loop--warning")
        assert not w.has_class("loop--force")
        assert "Loop: Issue Pickup" not in w.render()


# ---------------------------------------------------------------------------
# AlertBar tests
# ---------------------------------------------------------------------------


async def test_alert_bar_hidden_by_default() -> None:
    async with _AlertApp().run_test() as pilot:
        from agentshore.ui.widgets.alert_bar import AlertBar

        w = pilot.app.query_one(AlertBar)
        assert w.display is False


async def test_alert_bar_shows_on_show() -> None:
    async with _AlertApp().run_test() as pilot:
        from agentshore.ui.widgets.alert_bar import AlertBar

        w = pilot.app.query_one(AlertBar)
        w.show("something went wrong", "error")
        await pilot.pause()
        assert w.display is True


async def test_alert_bar_adds_level_class() -> None:
    async with _AlertApp().run_test() as pilot:
        from agentshore.ui.widgets.alert_bar import AlertBar

        w = pilot.app.query_one(AlertBar)
        w.show("watch out", "warning")
        await pilot.pause()
        assert w.has_class("alert--warning")


async def test_alert_bar_show_loop_renders_escalation() -> None:
    async with _AlertApp().run_test() as pilot:
        from agentshore.ui.widgets.alert_bar import AlertBar

        w = pilot.app.query_one(AlertBar)
        w.show_loop("Issue Pickup", 7)
        await pilot.pause()
        assert "LOOP DETECTED — Issue Pickup failed 7x — [Q]uit or wait for auto-stop" in w.render()
        assert w.display is True
        assert w.has_class("alert--loop")


# ---------------------------------------------------------------------------
# WorkQueueSummary tests
# ---------------------------------------------------------------------------


async def test_work_queue_summary_waiting_for_state() -> None:
    async with _WorkQueueApp().run_test() as pilot:
        from agentshore.ui.widgets.work_queue import WorkQueueSummary

        w = pilot.app.query_one(WorkQueueSummary)
        assert "waiting for state" in w.render()


async def test_work_queue_summary_counts_open_items() -> None:
    async with _WorkQueueApp().run_test() as pilot:
        from agentshore.ui.widgets.work_queue import WorkQueueSummary

        w = pilot.app.query_one(WorkQueueSummary)
        state = _make_state()
        state.open_issues = [
            IssueSnapshot(
                issue_number=12,
                title="Add queue visibility",
                state="open",
                priority=1,
                labels=[],
                source=None,
                bead_ready=True,
            ),
            IssueSnapshot(
                issue_number=13,
                title="Closed item",
                state="closed",
                priority=2,
                labels=[],
                source=None,
            ),
        ]
        state.pull_requests = [
            PullRequestSnapshot(
                pr_number=22,
                title="Improve dashboard",
                state="open",
                branch="codex/tui-dashboard",
                issue_number=12,
                labels=[],
                review_decision=None,
                status_check_summary=None,
                is_draft=True,
                blocked=True,
                blocked_reasons=["ci"],
            )
        ]
        state.pending_review_queue = [
            PendingReviewSnapshot(queue_id=1, pr_number=22, author_label="codex", enqueued_at="now")
        ]

        w.update_state(state)
        await pilot.pause()
        rendered = w.render()
        assert "Issues  1 open" in rendered
        assert "1 ready" in rendered
        assert "PRs     1 open" in rendered
        assert "1 blocked" in rendered
        assert "Reviews queued 1" in rendered
        assert "Next #12" in rendered
