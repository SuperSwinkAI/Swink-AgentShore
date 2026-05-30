"""State construction: ``_build_state``, ``_fetch_state_data``, action mask annotation."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING

import aiosqlite

from agentshore.core.base import _OrchestratorBase
from agentshore.core.context import _StateData
from agentshore.core.helpers import _logger
from agentshore.core.mixins.completion import BREAK_RECOVERY_FAILURE_LIMIT
from agentshore.rl.action_space import ACTION_SPACE_VERSION
from agentshore.state import (
    INTERNAL_PLAY_TYPES,
    OrchestratorState,
    PendingReviewSnapshot,
    PlayType,
    SessionState,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from agentshore.agents.manager import AgentManager
    from agentshore.beads import ProjectGraph
    from agentshore.config import RuntimeConfig
    from agentshore.core.context import _DispatchContext
    from agentshore.data.store import (
        CheckpointRecord,
        DataStore,
        GitHubIssueRecord,
        PlayRecord,
        PullRequestRecord,
        ReviewQueueRecord,
        TrajectorySnapshotRecord,
    )
    from agentshore.plays.executor import PlayExecutor
    from agentshore.state import (
        PlayOutcome,
    )


def _merge_recent_applied_labels(
    issue_records: list[GitHubIssueRecord],
    recent: Iterable[tuple[int, str]],
) -> list[GitHubIssueRecord]:
    """Return ``issue_records`` with shadow-applied labels overlaid.

    Pairs with ``_recent_applied_labels`` on ``_OrchestratorBase`` (desktop-quv9).
    The shadow exists so a label applied at the end of a successful play is
    visible to the very next selector tick even if the gh CLI label-add or the
    follow-up ``add_issue_labels`` SQLite write hasn't been flushed yet. Same
    WAL-lag class as ``_merge_recent_completions``.

    For each (issue_number, label) in ``recent`` whose ``issue_number`` matches
    a record in ``issue_records``: a copy of that record is produced with the
    label appended (if not already present). The input list is not mutated;
    the dataclass slots prevent direct attribute writes on the original record
    and the labels list is cloned so downstream consumers can mutate freely.

    Records with no matching shadow entry are passed through by reference —
    the common case of "no labels were applied this tick" stays allocation-free.
    """
    if not recent:
        return issue_records
    by_issue: dict[int, set[str]] = {}
    for issue_number, label in recent:
        if not label:
            continue
        by_issue.setdefault(issue_number, set()).add(label)
    if not by_issue:
        return issue_records
    out: list[GitHubIssueRecord] = []
    mutated = False
    for record in issue_records:
        extra = by_issue.get(record.issue_number)
        if not extra:
            out.append(record)
            continue
        existing = set(record.labels)
        new_labels = extra - existing
        if not new_labels:
            out.append(record)
            continue
        merged = list(record.labels) + sorted(new_labels)
        out.append(dataclasses.replace(record, labels=merged))
        mutated = True
    return out if mutated else issue_records


def _merge_recent_completions(
    db_history: list[PlayRecord],
    recent: Iterable[PlayRecord],
) -> list[PlayRecord]:
    """Return ``db_history`` with any newer in-memory completions overlaid.

    For each entry in ``recent``: if its ``play_id`` already appears in
    ``db_history`` it is skipped (DB is authoritative for fields like
    duration_ms or alignment_after that the in-memory shadow doesn't carry).
    Otherwise it's appended — the DB read missed it due to WAL-flush lag.

    Within the shadow itself, multiple entries that share a ``play_id`` are
    collapsed to the latest one. In production each ``record_play`` assigns
    a unique ID, so the dedup is a no-op; the safeguard exists for test
    fixtures that reuse a single ``play_id`` across mock dispatches.

    Output ordering matches the original ``db_history`` plus appended
    completions in their captured order, which preserves the "newest last"
    invariant that recency math relies on.
    """
    known_play_ids = {p.play_id for p in db_history if p.play_id is not None}
    shadow_by_id: dict[int, PlayRecord] = {}
    for p in recent:
        if p.play_id is not None:
            shadow_by_id[p.play_id] = p
    extras = [p for play_id, p in shadow_by_id.items() if play_id not in known_play_ids]
    if not extras:
        return db_history
    return list(db_history) + extras


class _StateMixin(_OrchestratorBase):
    """Compose ``OrchestratorState`` from DB reads + live agent handles."""

    _cfg: RuntimeConfig
    _session_id: str
    _repo_root: Path
    _store: DataStore
    _manager: AgentManager
    _executor: PlayExecutor
    _draining: bool
    _stop_requested: bool
    _in_flight: dict[str, asyncio.Task[PlayOutcome]]
    _dispatch_ctx: dict[str, _DispatchContext]
    _override_dispatched_play_ids: set[int]
    _pause_event: asyncio.Event
    _drain_reason: str | None
    _forced_mask_play_types: tuple[PlayType, ...]
    _policy_version: str
    _recent_executor_skip: bool
    _idle_agent_claim_ticks: dict[str, int]
    _registry: object | None

    # ------------------------------------------------------------------

    async def _fetch_state_data(self) -> _StateData:
        """Fan out independent DB reads concurrently and return a ``_StateData``.

        ``_build_state`` consumes this snapshot. Reads are independent so all
        seven are launched in a single ``asyncio.gather`` call to maximise
        parallelism and avoid sequential round-trips.

        Because ``asyncio.gather`` stubs only carry typed overloads for up to
        five arguments, the gather is split across two coroutines that execute
        concurrently inside an outer two-way ``gather`` — preserving full
        parallelism while keeping mypy happy.

        Trajectory fetch is the only one that historically swallowed errors;
        that behaviour is preserved in ``_extract_trajectory``.
        """
        from agentshore.beads import load_graph

        async def _fetch_group1() -> tuple[
            list[GitHubIssueRecord],
            list[GitHubIssueRecord],
            list[PullRequestRecord],
            list[PlayRecord],
            TrajectorySnapshotRecord | None,
        ]:
            return await asyncio.gather(
                self._store.get_open_issues(self._session_id),
                self._store.list_recently_closed_issues(self._session_id),
                self._store.list_active_pull_requests(self._session_id),
                self._store.get_play_history(self._session_id),
                self._safe_get_latest_trajectory(),
            )

        async def _fetch_group2() -> tuple[
            list[ReviewQueueRecord],
            ProjectGraph | None,
            CheckpointRecord | None,
            int,
            int,
        ]:
            return await asyncio.gather(
                self._store.list_pending_reviews(self._session_id),
                load_graph(self._repo_root),
                self._store.load_latest_checkpoint(self._session_id),
                self._store.count_learnings(self._session_id),
                self._store.count_human_feedback(self._session_id),
            )

        (
            (
                open_issues,
                recently_closed_issues,
                pr_records,
                play_history,
                trajectory_record,
            ),
            (
                pending_reviews,
                graph,
                latest_checkpoint,
                learnings_count,
                human_feedback_count,
            ),
        ) = await asyncio.gather(_fetch_group1(), _fetch_group2())

        # Merge in-memory recent completions with the DB read. SQLite WAL flush
        # is async, so freshly-recorded plays may not appear in get_play_history
        # for tens to hundreds of ms — long enough for same-tick instantiate_agent
        # pairs to slip past the cooldown mask (desktop-65bg). The deque is
        # capped at 64 plays so the merge cost is bounded.
        play_history = _merge_recent_completions(play_history, self._recent_play_completions)

        # Sibling shadow for per-issue applied labels (desktop-quv9). Without
        # this, a successful systematic_debugging that adds ROOT_CAUSE_FOUND_LABEL
        # to issue N can be re-selected on the very next tick — the gh CLI
        # label-add + ``add_issue_labels`` write haven't propagated to a fast
        # follow-up ``get_open_issues`` read. The merge augments the cached
        # issue records with shadow labels so the candidate filter
        # (``issue_available_for_debug``) excludes the freshly-labelled issue.
        open_issues = _merge_recent_applied_labels(open_issues, self._recent_applied_labels)

        # Closed issues from the last 24 hours feed the dashboard's Done
        # column. The frontend routes anything with state="closed" to Done,
        # so passing them through the same projection as open issues is
        # sufficient — no schema or projection change needed.
        return _StateData(
            issue_records=open_issues + recently_closed_issues,
            pr_records=pr_records,
            pending_reviews=pending_reviews,
            play_history=play_history,
            trajectory_record=trajectory_record,
            graph=graph,
            policy_checkpoint_id=(
                str(latest_checkpoint.checkpoint_id)
                if latest_checkpoint is not None and latest_checkpoint.checkpoint_id is not None
                else None
            ),
            learnings_count=learnings_count,
            human_feedback_count=human_feedback_count,
        )

    async def _safe_get_latest_trajectory(self) -> TrajectorySnapshotRecord | None:
        """Best-effort trajectory fetch; logs and returns ``None`` on failure.

        Preserves the original ``try/except`` semantics from the monolithic
        ``_build_state``: a trajectory read failure must not abort state
        construction.
        """
        try:
            return await self._store.get_latest_trajectory(self._session_id)
        except aiosqlite.Error as exc:
            _logger.warning("trajectory_snapshot_failed", error=str(exc))
            return None

    async def _abandon_work_for_missing_agents(self) -> None:
        """Recover running claims/play rows whose owning agent handle disappeared."""
        method = getattr(self._store, "abandon_work_for_missing_agents", None)
        if not callable(method):
            return
        active_agent_ids = frozenset(self._manager.handles)
        result = await method(
            self._session_id,
            active_agent_ids,
            reason="orphaned work abandoned during state refresh",
        )
        if not (
            isinstance(result, tuple)
            and len(result) == 2
            and all(isinstance(count, int) for count in result)
        ):
            return
        claim_count, play_count = result
        if claim_count or play_count:
            _logger.warning(
                "orphaned_work_abandoned",
                session_id=self._session_id,
                active_agent_count=len(active_agent_ids),
                claim_count=claim_count,
                play_count=play_count,
            )

    async def _release_claims_for_prolonged_idle_agents(self, state: OrchestratorState) -> None:
        """Release active claims owned by agents that have stayed idle for several ticks."""
        from agentshore.state import AgentStatus

        threshold = self._cfg.rl.stale_idle_claim_release_ticks
        if threshold <= 0:
            self._idle_agent_claim_ticks.clear()
            return

        idle_ids = {agent.agent_id for agent in state.agents if agent.status == AgentStatus.IDLE}
        if not idle_ids:
            self._idle_agent_claim_ticks.clear()
            return

        find_method = getattr(self._store, "find_active_work_claims_for_agents", None)
        release_method = getattr(self._store, "release_active_work_claims_for_agents", None)
        if not callable(find_method) or not callable(release_method):
            return

        claims = await find_method(self._session_id, idle_ids)
        claim_agent_ids = {
            claim.agent_id for claim in claims if getattr(claim, "agent_id", None) in idle_ids
        }
        for agent_id in list(self._idle_agent_claim_ticks):
            if agent_id not in claim_agent_ids:
                self._idle_agent_claim_ticks.pop(agent_id, None)

        release_agent_ids: list[str] = []
        for agent_id in sorted(claim_agent_ids):
            ticks = self._idle_agent_claim_ticks.get(agent_id, 0) + 1
            self._idle_agent_claim_ticks[agent_id] = ticks
            if ticks >= threshold:
                release_agent_ids.append(agent_id)

        if not release_agent_ids:
            return

        released_count = await release_method(self._session_id, release_agent_ids)
        for agent_id in release_agent_ids:
            self._idle_agent_claim_ticks.pop(agent_id, None)
        if released_count:
            _logger.warning(
                "idle_agent_claims_released",
                session_id=self._session_id,
                agent_ids=release_agent_ids,
                released_claim_count=released_count,
                idle_tick_threshold=threshold,
            )

    def _annotate_action_mask(self, state: OrchestratorState) -> None:
        """Attach action_mask + mask_reasons; logs and continues on failure."""
        from agentshore.plays.registry import PlayRegistry as _PlayRegistry

        if not isinstance(self._registry, _PlayRegistry):
            return
        from agentshore.plays.candidates import build_candidate_plan
        from agentshore.rl.mask import compute_action_mask, compute_mask_reasons

        try:
            config_index = self._selector_config_index()
            candidate_plan = build_candidate_plan(state)
            mask_arr = compute_action_mask(
                state,
                self._registry,
                cfg=self._cfg,
                config_index=config_index,
                apply_reverse_failsafe=self._cfg.rl.reverse_failsafe_enabled,
                candidate_plan=candidate_plan,
            )
            state.action_mask = tuple(bool(b) for b in mask_arr)
            state.mask_reasons = compute_mask_reasons(
                state,
                self._registry,
                cfg=self._cfg,
                config_index=config_index,
                apply_reverse_failsafe=self._cfg.rl.reverse_failsafe_enabled,
                candidate_plan=candidate_plan,
            )
        except (KeyError, ValueError, AttributeError) as exc:
            _logger.warning("action_mask_compute_failed", error=str(exc))

    def _assemble_state(self, data: _StateData) -> OrchestratorState:
        """Pure transformation: ``_StateData`` + live handles -> ``OrchestratorState``.

        No I/O. Unit-testable by constructing a ``_StateData`` directly.
        """
        agents = self._build_agent_snapshots(data.play_history)
        open_issues = self._project_open_issues(data.issue_records, data.graph)
        pull_requests = self._project_pull_requests(data.pr_records)
        active_pr_numbers = {pr.pr_number for pr in pull_requests}
        pending_review_queue = [
            PendingReviewSnapshot(
                queue_id=r.queue_id,
                pr_number=r.pr_number,
                author_label=r.author_label,
                enqueued_at=r.enqueued_at,
            )
            for r in data.pending_reviews
            if r.queue_id is not None and r.pr_number in active_pr_numbers
        ]

        # User-facing total_plays excludes non-work bookkeeping plays
        # (currently none — desktop-rni0). Same filter as _compute_session_stats
        # so HUD counter matches ESR.
        internal_play_values = {pt.value for pt in INTERNAL_PLAY_TYPES}
        total_plays = sum(1 for p in data.play_history if p.play_type not in internal_play_values)
        total_cost = sum(p.dollar_cost for p in data.play_history)
        same_type_failure_streak, same_type_streak = self._compute_play_streaks(
            data.play_history,
            override_play_ids=self._override_dispatched_play_ids,
        )
        (
            last_play_type,
            plays_since_last_instantiate,
            plays_since_last_play_type,
            last_play_success_by_type,
            seed_freshness,
        ) = self._compute_play_recency(data.play_history)
        budget = self._build_budget_snapshot(total_plays, total_cost)
        trajectory = self._extract_trajectory(data.trajectory_record)
        stats = self._compute_session_stats(data.play_history)

        if self._stop_requested:
            session_state = SessionState.SHUTTING_DOWN
        elif self._draining:
            session_state = SessionState.DRAINING
        elif not self._pause_event.is_set():
            session_state = SessionState.PAUSED
        else:
            session_state = SessionState.RUNNING
        in_flight_plays = [
            ctx.play_type
            for dispatch_id, ctx in self._dispatch_ctx.items()
            if dispatch_id in self._in_flight and not self._in_flight[dispatch_id].done()
        ]

        state = OrchestratorState(
            session_id=self._session_id,
            session_state=session_state,
            total_plays=total_plays,
            total_cost=total_cost,
            policy_mode=self._cfg.rl.policy_mode,
            target_branch=self._cfg.project.target_branch,
            agents=agents,
            open_issues=open_issues,
            pull_requests=pull_requests,
            pending_review_queue=pending_review_queue,
            budget=budget,
            trajectory=trajectory,
            same_type_failure_streak=same_type_failure_streak,
            same_type_streak=same_type_streak,
            last_play_type=last_play_type,
            recent_executor_skip=self._recent_executor_skip,
            in_flight_plays=in_flight_plays,
            in_flight_issues=list(self._executor.inflight_issues),
            planned_issues=self._executor.planned_issues,
            plays_since_last_instantiate=plays_since_last_instantiate,
            plays_since_last_play_type=plays_since_last_play_type,
            last_play_success_by_type=last_play_success_by_type,
            forced_mask_zeros=self._forced_mask_play_types,
            recovery_exhausted_agent_ids=frozenset(
                a.agent_id
                for a in agents
                if self._break_recovery_failures.get(a.agent_id, 0) >= BREAK_RECOVERY_FAILURE_LIMIT
            ),
            drain_reason=self._drain_reason if self._draining else None,
            graph=data.graph,
            stats=stats,
            run_mode=self._cfg.mode,
            action_space_version=ACTION_SPACE_VERSION,
            policy_version=self._policy_version,
            policy_checkpoint_id=data.policy_checkpoint_id,
            seed_freshness=seed_freshness,
            learnings_count=data.learnings_count,
            human_feedback_count=data.human_feedback_count,
        )

        self._annotate_action_mask(state)
        return state

    async def _build_state(self) -> OrchestratorState:
        """Rebuild authoritative OrchestratorState from live handles + DB (no caching).

        Thin orchestration: fan out DB reads via ``_fetch_state_data``, then
        delegate the pure construction to ``_assemble_state``.
        """
        await self._abandon_work_for_missing_agents()
        data = await self._fetch_state_data()
        state = self._assemble_state(data)
        await self._release_claims_for_prolonged_idle_agents(state)
        return state
