"""Static helpers that project DB records and history into snapshot dataclasses."""

from __future__ import annotations

import math
from contextlib import suppress
from typing import TYPE_CHECKING

from agentshore.core.base import _OrchestratorBase
from agentshore.core.helpers import _logger
from agentshore.github.pr_links import issue_numbers_for_pr
from agentshore.pr_state import blocked_reasons
from agentshore.state import (
    INTERNAL_PLAY_TYPES,
    AgentSnapshot,
    BudgetSnapshot,
    IssueSnapshot,
    PlayType,
    PlayTypeStatsSnapshot,
    PullRequestSnapshot,
    SessionStatsSnapshot,
    TrajectorySnapshot,
)
from agentshore.utils import now_iso

if TYPE_CHECKING:
    from agentshore.agents.manager import AgentManager
    from agentshore.beads import ProjectGraph
    from agentshore.config import RuntimeConfig
    from agentshore.data.store import (
        DataStore,
        GitHubIssueRecord,
        PlayRecord,
        PullRequestRecord,
        TrajectorySnapshotRecord,
    )
    from agentshore.state import (
        OrchestratorState,
        PlayOutcome,
    )


MIN_COST_PER_PLAY = 0.05
COLD_START_COST_ESTIMATE = 0.05

# Non-work plays — bookkeeping that progresses the loop without representing
# user-visible work. Empty after desktop-rni0 (idle waits and recovery are
# loop-side, not plays). Retained for forward-compat: future bookkeeping plays
# would be added here.
_NON_WORK_PLAY_VALUES: frozenset[str] = frozenset()


class _SnapshotsMixin(_OrchestratorBase):
    """Projection of records/history to snapshot dataclasses + trajectory math."""

    _cfg: RuntimeConfig
    _session_id: str
    _store: DataStore
    _manager: AgentManager
    _extra_budget: float

    # ------------------------------------------------------------------

    def _build_agent_snapshots(self, play_history: list[PlayRecord]) -> list[AgentSnapshot]:
        """Project live agent handles into the immutable IPC snapshot type.

        ``tasks_completed``/``tasks_failed`` count plays this agent has run in
        the current session, derived from ``play_history``. The legacy
        ``AgentHandle.task_history`` source was never populated; downstream
        consumers (end_agent precondition, _resolve_end_agent scoring) need
        real numbers.

        ``dispatch_count`` (desktop-31h2) comes from the live
        ``AgentHandle.dispatches`` counter — bumped once per
        ``AgentManager.dispatch`` call regardless of outcome. The DB column
        (``agents.dispatch_count``) is the durable mirror; the in-memory
        counter is what populates the snapshot to avoid a per-tick DB
        round-trip. ``dispatch_share`` is the agent's slice of the live
        fleet total (0.0 when nothing has been dispatched yet).
        """
        plays_by_agent: dict[str, tuple[int, int]] = {}
        for p in play_history:
            agent_id = p.agent_id
            if not agent_id:
                continue
            ok, fail = plays_by_agent.get(agent_id, (0, 0))
            if p.success:
                plays_by_agent[agent_id] = (ok + 1, fail)
            else:
                plays_by_agent[agent_id] = (ok, fail + 1)

        handles = list(self._manager.handles.values())
        # Cast through int() so tests that drop a MagicMock into
        # ``handles`` (see tests/test_ipc.py::test_play_completion_emits_agent_idle)
        # don't trip the ``> 0`` comparison below — MagicMock.__gt__ doesn't
        # play nice with int.
        dispatches_by_handle = {id(h): int(getattr(h, "dispatches", 0) or 0) for h in handles}
        total_dispatches = sum(dispatches_by_handle.values())

        return [
            AgentSnapshot(
                agent_id=h.agent_id,
                agent_type=h.agent_type,
                status=h.status,
                context_size=h.context_size,
                total_cost=h.total_cost,
                total_tokens=h.total_tokens,
                tasks_completed=plays_by_agent.get(h.agent_id, (0, 0))[0],
                tasks_failed=plays_by_agent.get(h.agent_id, (0, 0))[1],
                display_name=h.display_name,
                model=h.model,
                model_tier=h.model_tier,
                reasoning_effort=h.reasoning_effort,
                current_play_type=h.current_play_type,
                current_play_id=h.current_play_id,
                current_play_started_at=h.current_play_started_at,
                current_play_issue_number=h.current_play_issue_number,
                current_play_pr_number=h.current_play_pr_number,
                current_play_branch=h.current_play_branch,
                last_error_class=h.last_error_class,
                timeout_count=h.timeout_count,
                github_identity=h.github_identity,
                dispatch_count=dispatches_by_handle[id(h)],
                dispatch_share=(
                    dispatches_by_handle[id(h)] / total_dispatches if total_dispatches > 0 else 0.0
                ),
            )
            for h in handles
        ]

    @staticmethod
    def _project_open_issues(
        records: list[GitHubIssueRecord],
        graph: ProjectGraph | None,
    ) -> list[IssueSnapshot]:
        from agentshore.beads import pick_bead_for_issue

        graph_tasks = graph.tasks if graph is not None else ()
        tasks_by_issue = {
            record.issue_number: pick_bead_for_issue(graph_tasks, record.issue_number)
            for record in records
        }
        snapshots: list[IssueSnapshot] = []
        for record in records:
            task = tasks_by_issue.get(record.issue_number)
            snapshots.append(
                IssueSnapshot(
                    issue_number=record.issue_number,
                    title=record.title,
                    state=record.state,
                    priority=record.priority,
                    labels=record.labels,
                    source=record.source,
                    url=record.url,
                    created_at=record.created_at,
                    closed_at=record.closed_at,
                    bead_id=task.bead_id if task is not None else None,
                    bead_epic_id=task.epic_id if task is not None else None,
                    bead_epic_title=task.epic_title if task is not None else None,
                    bead_status=task.status.value if task is not None else None,
                    bead_ready=task.ready if task is not None else False,
                    bead_mirror_status="mirrored" if task is not None else "missing",
                )
            )
        return snapshots

    @staticmethod
    def _project_pull_requests(
        records: list[PullRequestRecord],
    ) -> list[PullRequestSnapshot]:
        snapshots: list[PullRequestSnapshot] = []
        for pr in records:
            reasons = blocked_reasons(
                state=pr.state,
                labels=pr.labels,
                review_decision=pr.review_decision,
                status_check_summary=pr.status_check_summary,
                is_draft=pr.is_draft,
                mergeable=pr.mergeable,
                head_sha=pr.head_sha,
                last_reviewed_sha=pr.last_reviewed_sha,
                last_review_status=pr.last_review_status,
            )
            if pr.branch is None and pr.state and pr.state.lower() != "merged":
                # Safety net for issue #567: an active PR record without a branch
                # will cause worktree_allocate_failed: missing_branch on the next
                # code_review/merge_pr dispatch. Surface the leak with enough
                # context to trace back to the construction site.
                _logger.warning(
                    "pr_snapshot_missing_branch",
                    pr_number=pr.pr_number,
                    state=pr.state,
                    url=pr.url,
                    author_agent_id=pr.author_agent_id,
                )
            snapshots.append(
                PullRequestSnapshot(
                    pr_number=pr.pr_number,
                    title=pr.title,
                    state=pr.state,
                    branch=pr.branch,
                    issue_number=pr.issue_number,
                    linked_issue_numbers=issue_numbers_for_pr(pr),
                    labels=pr.labels,
                    review_decision=pr.review_decision,
                    status_check_summary=pr.status_check_summary,
                    is_draft=bool(pr.is_draft),
                    blocked=bool(reasons),
                    blocked_reasons=reasons,
                    url=pr.url,
                    github_author=pr.github_author,
                    author_agent_id=pr.author_agent_id,
                    author_agent_type=pr.author_agent_type,
                    head_sha=pr.head_sha,
                    mergeable=pr.mergeable,
                    base_ref=pr.base_ref,
                    last_reviewed_sha=pr.last_reviewed_sha,
                    last_review_status=pr.last_review_status,
                )
            )
        return snapshots

    @staticmethod
    def _compute_play_streaks(
        play_history: list[PlayRecord],
        *,
        override_play_ids: set[int] | None = None,
    ) -> tuple[int, int]:
        """Compute (same_type_failure_streak, same_type_streak) from history.

        - same_type_failure_streak: tail of consecutive failures of the same type.
        - same_type_streak: tail of same play type regardless of success —
          catches PPO collapse onto a cheap repeated action where the failure
          counter would never advance.

        Internal play types in ``INTERNAL_PLAY_TYPES`` are excluded. The set
        is empty after desktop-rni0 but the filter is retained so future
        bookkeeping plays can opt into the same treatment.

        Plays dispatched from the override queue (bootstrap recipe,
        retry) are excluded via ``override_play_ids`` because
        they were not selected by the policy — counting them would fire
        loop_detected on legitimate fleet spin-up bursts (e.g. bootstrap
        queuing 4 instantiate_agent overrides in sequence).
        """
        internal_play_values = {pt.value for pt in INTERNAL_PLAY_TYPES}
        override_ids = override_play_ids or set()
        same_type_failure_streak = 0
        same_type_streak = 0
        last_seen_type: str | None = None
        failure_streak_active = True  # becomes False once a success interrupts
        completed_history = [
            p
            for p in play_history
            if p.ended_at is not None
            and p.success is not None
            and p.play_type not in internal_play_values
            and (p.play_id is None or p.play_id not in override_ids)
        ]
        for p in reversed(completed_history):
            if last_seen_type is None:
                last_seen_type = p.play_type
            if p.play_type != last_seen_type:
                break
            same_type_streak += 1
            if failure_streak_active:
                if p.success:
                    failure_streak_active = False
                else:
                    same_type_failure_streak += 1
        return same_type_failure_streak, same_type_streak

    @staticmethod
    def _compute_play_recency(
        play_history: list[PlayRecord],
    ) -> tuple[
        PlayType | None,
        int | None,
        dict[PlayType, int],
        dict[PlayType, bool],
        dict[PlayType, bool],
        int | None,
        dict[PlayType, int],
    ]:
        """Compute play recency and latest success/failure by play type.

        Returns ``(last_play_type, plays_since_last_instantiate,
        plays_since_last_play_type, last_play_success_by_type,
        last_play_skipped_by_type, seed_freshness,
        consecutive_nonproductive_by_type)``. ``last_play_skipped_by_type``
        records whether each type's most-recent outcome was a no-op ``skip:*``
        (vs a genuine failure) so self-heal gates don't treat a skip as a wedge.
        ``seed_freshness`` is plays since the most-recent *successful*
        SEED_PROJECT, or ``None`` if no successful seed exists (V1 contract).
        ``consecutive_nonproductive_by_type`` is the tail run of consecutive
        ``not success`` outcomes per play type (fail OR skip, since skips record
        ``success=False``) — the signal the 3-strikes circuit breaker masks on.
        """
        play_history = [p for p in play_history if p.ended_at is not None]
        last_play_type: PlayType | None = None
        if play_history:
            with suppress(ValueError):
                last_play_type = PlayType(play_history[-1].play_type)

        plays_since_last_instantiate: int | None = None
        plays_since_last_play_type: dict[PlayType, int] = {}
        last_play_success_by_type: dict[PlayType, bool] = {}
        last_play_skipped_by_type: dict[PlayType, bool] = {}
        consecutive_nonproductive_by_type: dict[PlayType, int] = {}
        _streak_broken: set[PlayType] = set()
        seed_freshness: int | None = None
        # Non-work plays (``_NON_WORK_PLAY_VALUES``) are skipped so the
        # cooldown offset reflects "real plays since last X". The set is
        # empty after desktop-rni0 but the seam stays in place.
        real_offset = 0
        for p in reversed(play_history):
            if p.play_type in _NON_WORK_PLAY_VALUES:
                continue
            if (
                p.play_type == PlayType.INSTANTIATE_AGENT.value
                and p.success
                and plays_since_last_instantiate is None
            ):
                plays_since_last_instantiate = real_offset
            if p.play_type == PlayType.SEED_PROJECT.value and p.success and seed_freshness is None:
                seed_freshness = real_offset
            with suppress(ValueError):
                pt = PlayType(p.play_type)
                if pt not in plays_since_last_play_type:
                    plays_since_last_play_type[pt] = real_offset
                if pt not in last_play_success_by_type:
                    last_play_success_by_type[pt] = p.success
                if pt not in last_play_skipped_by_type:
                    # A ``skip:*`` outcome (recorded success=False) is a no-op,
                    # not a wedge — track it so ArmedByFailureGate doesn't arm a
                    # self-heal play off a skip (the write_impl↔reconcile spin).
                    last_play_skipped_by_type[pt] = (p.failure_category or "").startswith("skip:")
                # Tail run of consecutive non-productive (fail OR skip) outcomes
                # per type — keep counting newest→oldest until this type's first
                # success terminates its streak (the 3-strikes breaker signal).
                if pt not in _streak_broken:
                    if p.success:
                        _streak_broken.add(pt)
                        consecutive_nonproductive_by_type.setdefault(pt, 0)
                    else:
                        consecutive_nonproductive_by_type[pt] = (
                            consecutive_nonproductive_by_type.get(pt, 0) + 1
                        )
            real_offset += 1
        return (
            last_play_type,
            plays_since_last_instantiate,
            plays_since_last_play_type,
            last_play_success_by_type,
            last_play_skipped_by_type,
            seed_freshness,
            consecutive_nonproductive_by_type,
        )

    def _build_budget_snapshot(self, total_plays: int, total_cost: float) -> BudgetSnapshot:
        total_budget = self._cfg.budget.total + self._extra_budget
        remaining = (
            max(0.0, total_budget - total_cost) if self._cfg.budget.enabled else float("inf")
        )
        return BudgetSnapshot(
            total_budget=total_budget,
            spent=total_cost,
            remaining=remaining,
            # Cold-start fallback until real cost data is available.
            estimated_cost_per_play=(
                total_cost / total_plays if total_plays > 0 else COLD_START_COST_ESTIMATE
            ),
            enabled=self._cfg.budget.enabled,
        )

    @staticmethod
    def _extract_trajectory(
        record: TrajectorySnapshotRecord | None,
    ) -> TrajectorySnapshot | None:
        if record is None:
            return None
        return TrajectorySnapshot(
            projected_alignment_at_budget_end=record.projected_alignment_at_budget_end,
            estimated_remaining_plays=record.estimated_remaining_plays,
            estimated_remaining_cost=record.estimated_remaining_cost,
        )

    def _compute_trajectory_record(
        self,
        outcome: PlayOutcome,
        next_state: OrchestratorState,
        history: list[PlayRecord],
    ) -> TrajectorySnapshotRecord | None:
        from agentshore.data.store import TrajectorySnapshotRecord

        if outcome.play_id is None or next_state.budget is None:
            return None

        budget = next_state.budget
        if budget.enabled and budget.remaining > 0:
            avg_cost = max(budget.estimated_cost_per_play, MIN_COST_PER_PLAY)
            estimated_remaining_plays = max(0, int(budget.remaining / avg_cost))
            estimated_remaining_cost = max(0.0, float(budget.remaining))
        else:
            estimated_remaining_plays = 0
            estimated_remaining_cost = 0.0

        current_alignment = 0.0
        if next_state.graph is not None and math.isfinite(next_state.graph.global_closure_ratio):
            current_alignment = float(next_state.graph.global_closure_ratio)

        prior_deltas = [p.alignment_delta for p in history if p.alignment_delta is not None]
        if len(prior_deltas) >= 2:
            window = prior_deltas[-10:]
            slope = sum(window) / len(window)
            projected = current_alignment + slope * estimated_remaining_plays
        else:
            projected = current_alignment
        if not math.isfinite(projected):
            projected = current_alignment
        projected = max(0.0, min(1.0, projected))

        return TrajectorySnapshotRecord(
            session_id=self._session_id,
            play_id=outcome.play_id,
            projected_alignment_at_budget_end=projected,
            estimated_remaining_plays=estimated_remaining_plays,
            estimated_remaining_cost=estimated_remaining_cost,
            created_at=now_iso(),
        )

    async def _record_trajectory_snapshot(
        self,
        outcome: PlayOutcome,
        next_state: OrchestratorState,
    ) -> None:
        if not outcome.success:
            return
        try:
            history = await self._store.get_play_history(self._session_id)
        except Exception as exc:
            _logger.error(
                "safe_call_failed",
                label="get_play_history",
                error=str(exc),
                exc_info=True,
            )
            history = []
        record = self._compute_trajectory_record(outcome, next_state, history)
        if record is None:
            return
        await self._safe_call(
            self._store.record_trajectory_snapshot(record),
            "record_trajectory_snapshot",
        )

    @staticmethod
    def _compute_session_stats(play_history: list[PlayRecord]) -> SessionStatsSnapshot:
        """Aggregate full-session play stats for dashboard consumers.

        Non-work plays (``_NON_WORK_PLAY_VALUES``) are filtered out of the
        user-facing total/success/failure counters. The set is empty after
        desktop-rni0 (idle waits and recovery are loop-side, not plays);
        the filter is retained as the canonical seam for future bookkeeping
        plays.
        """
        work_plays = [p for p in play_history if p.play_type not in _NON_WORK_PLAY_VALUES]
        total_plays = len(work_plays)
        _gate_categories = ("gate_rejection", "skip:")
        successful_plays = sum(1 for play in work_plays if play.success)
        gate_rejected_plays = sum(
            1
            for play in work_plays
            if not play.success and (play.failure_category or "").startswith(_gate_categories)
        )
        failed_plays = total_plays - successful_plays - gate_rejected_plays
        # Cost and tokens still total over EVERY play including idle —
        # idle ticks cost ~$0 anyway, and the budget meter should account
        # for any incidental cost. Duration similarly tracks elapsed.
        total_cost = sum(play.dollar_cost for play in play_history)
        total_tokens = sum(play.token_cost for play in play_history)
        total_duration_seconds = sum((play.duration_ms or 0) / 1000 for play in play_history)

        by_type: dict[str, dict[str, float | int]] = {}
        for play in play_history:
            is_gate = (play.failure_category or "").startswith(_gate_categories)
            bucket = by_type.setdefault(
                play.play_type,
                {
                    "total": 0,
                    "successful": 0,
                    "failed": 0,
                    "gate_rejected": 0,
                    "total_cost": 0.0,
                    "total_duration_seconds": 0.0,
                },
            )
            bucket["total"] = int(bucket["total"]) + 1
            if play.success:
                bucket["successful"] = int(bucket["successful"]) + 1
            elif is_gate:
                bucket["gate_rejected"] = int(bucket["gate_rejected"]) + 1
            else:
                bucket["failed"] = int(bucket["failed"]) + 1
            bucket["total_cost"] = float(bucket["total_cost"]) + play.dollar_cost
            bucket["total_duration_seconds"] = (
                float(bucket["total_duration_seconds"]) + (play.duration_ms or 0) / 1000
            )

        rows: list[PlayTypeStatsSnapshot] = []
        for raw_play_type, bucket in by_type.items():
            total = int(bucket["total"])
            successful = int(bucket["successful"])
            failed = int(bucket["failed"])
            play_type: PlayType | str
            try:
                play_type = PlayType(raw_play_type)
            except ValueError:
                play_type = raw_play_type
            rows.append(
                PlayTypeStatsSnapshot(
                    play_type=play_type,
                    total=total,
                    successful=successful,
                    failed=failed,
                    success_rate=successful / total if total else 0.0,
                    total_cost=float(bucket["total_cost"]),
                    avg_duration_seconds=(
                        float(bucket["total_duration_seconds"]) / total if total else 0.0
                    ),
                )
            )

        rows.sort(key=lambda row: (-row.total, str(row.play_type)))

        from agentshore.rl.metrics import compute_agent_specialization

        return SessionStatsSnapshot(
            total_plays=total_plays,
            successful_plays=successful_plays,
            failed_plays=failed_plays,
            success_rate=successful_plays / total_plays if total_plays else 0.0,
            total_cost=total_cost,
            avg_cost_per_play=total_cost / total_plays if total_plays else 0.0,
            total_tokens=total_tokens,
            avg_duration_seconds=(total_duration_seconds / total_plays if total_plays else 0.0),
            by_play_type=rows,
            agent_specialization=compute_agent_specialization(play_history),
        )
