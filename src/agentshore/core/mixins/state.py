"""State construction: ``build_state``, ``fetch_state_data``, action mask annotation."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import TYPE_CHECKING, Protocol

import aiosqlite

from agentshore.core.context import _StateData
from agentshore.core.helpers import _logger
from agentshore.github.trust import trusted_issue_author_logins
from agentshore.rl.action_space import ACTION_SPACE_VERSION
from agentshore.state import (
    INTERNAL_PLAY_TYPES,
    BudgetSnapshot,
    OrchestratorState,
    PendingReviewSnapshot,
    PlayType,
    PullRequestSnapshot,
    SessionState,
    loop_level_for_streak,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from agentshore.agents.manager import AgentManager
    from agentshore.config.models import BudgetConfig
    from agentshore.core.main_repo_guard import MainRepoGuard
    from agentshore.core.mixins.snapshots import SnapshotProjector
    from agentshore.core.override_queue import OverrideQueue
    from agentshore.core.recovery_tracker import RecoveryTracker
    from agentshore.core.session_runtime import SessionRuntime
    from agentshore.core.velocity_tracker import VelocityTracker
    from agentshore.data.store import (
        DataStore,
        GitHubIssueRecord,
        PlayRecord,
        TrajectorySnapshotRecord,
    )
    from agentshore.plays.executor import PlayExecutor
    from agentshore.rl.config_head import ConfigKey


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


class _StateBuilderHost(Protocol):
    """Orchestrator *behaviour* the :class:`StateBuilder` invokes.

    All shared session *state* now lives on :class:`SessionRuntime` (reached via
    ``self._runtime``); this Protocol is the narrow behaviour seam that remains so
    the cross-component methods resolve on the composition root without a circular
    import. ``_OrchestratorBase`` structurally satisfies it.
    """

    def effective_budget_caps(self) -> BudgetConfig:
        """Live-effective budget caps (overrides shadowing ``cfg.budget``)."""
        ...

    def _selector_config_index(self) -> tuple[ConfigKey, ...] | None: ...


class StateBuilder:
    """Compose ``OrchestratorState`` from DB reads + live agent handles."""

    def __init__(
        self,
        *,
        host: _StateBuilderHost,
        runtime: SessionRuntime,
        store: DataStore,
        manager: AgentManager,
        executor: PlayExecutor,
        session_id: str,
        repo_root: Path,
        main_repo: MainRepoGuard,
        snapshots: SnapshotProjector,
        velocity: VelocityTracker,
        recovery: RecoveryTracker,
        overrides: OverrideQueue,
    ) -> None:
        self._host = host
        self._runtime = runtime
        self._store = store
        self._manager = manager
        self._executor = executor
        self._session_id = session_id
        self._repo_root = repo_root
        self._main_repo = main_repo
        self._snapshots = snapshots
        self._velocity = velocity
        self._recovery = recovery
        self._overrides = overrides
        # Per-agent idle-tick counter for stale claim release (owned here).
        self._idle_agent_claim_ticks: dict[str, int] = {}
        # Cached (total_plays, total_cost) so the budget-countdown heartbeat can
        # re-derive remaining time off a fresh clock without a DB read. None
        # until the first full state assembly.
        self._last_budget_inputs: tuple[int, float] | None = None

    # ------------------------------------------------------------------

    async def fetch_state_data(self) -> _StateData:
        """Fan out independent DB reads concurrently and return a ``_StateData``.

        ``build_state`` consumes this snapshot. The ten reads are independent,
        so each is launched as its own task inside a single ``asyncio.TaskGroup``
        to maximise parallelism and avoid sequential round-trips; the typed
        ``asyncio.Task[T]`` handles keep mypy strict happy without the 5-arg
        ``asyncio.gather`` overload limit that previously forced a two-group
        split. Error handling is unchanged in practice: a failing read still
        aborts the fan-out, and every caller wraps ``build_state`` in
        ``except Exception`` (the loop per-tick guard, the drain checkpoint
        blocks), which catches the TaskGroup's ``ExceptionGroup`` just as it did
        the old raw exception.

        Trajectory fetch is the only one that historically swallowed errors;
        that behaviour is preserved in ``safe_get_latest_trajectory``.
        """
        from agentshore.beads import GraphReadError, ProjectGraph, load_graph

        async def _safe_load_graph() -> ProjectGraph | None:
            """Load the beads graph, returning None on GraphReadError.

            GraphReadError signals a persistent bd failure (uninstalled binary,
            corrupted store, wedged lock). We surface it as alignment_delta=None
            in the assembled state rather than aborting the entire tick — the RL
            loop must keep running even when beads is temporarily unavailable.
            """
            try:
                return await load_graph(self._repo_root)
            except GraphReadError as exc:
                _logger.warning(
                    "beads_graph_read_failed_using_none",
                    project_path=str(self._repo_root),
                    error=str(exc),
                )
                return None

        async with asyncio.TaskGroup() as tg:
            open_issues_task = tg.create_task(self._store.get_open_issues(self._session_id))
            recently_closed_issues_task = tg.create_task(
                self._store.list_recently_closed_issues(self._session_id)
            )
            pr_records_task = tg.create_task(
                self._store.list_active_pull_requests(self._session_id)
            )
            play_history_task = tg.create_task(self._store.get_play_history(self._session_id))
            trajectory_task = tg.create_task(self.safe_get_latest_trajectory())
            pending_reviews_task = tg.create_task(
                self._store.list_pending_reviews(self._session_id)
            )
            graph_task = tg.create_task(_safe_load_graph())
            latest_checkpoint_task = tg.create_task(
                self._store.load_latest_checkpoint(self._session_id)
            )
            learnings_count_task = tg.create_task(self._count_learnings_from_json())
            human_feedback_count_task = tg.create_task(
                self._store.count_human_feedback(self._session_id)
            )

        open_issues = open_issues_task.result()
        recently_closed_issues = recently_closed_issues_task.result()
        pr_records = pr_records_task.result()
        play_history = play_history_task.result()
        trajectory_record = trajectory_task.result()
        pending_reviews = pending_reviews_task.result()
        graph = graph_task.result()
        latest_checkpoint = latest_checkpoint_task.result()
        learnings_count = learnings_count_task.result()
        human_feedback_count = human_feedback_count_task.result()

        # WAL flush is async: freshly-recorded plays can lag get_play_history by
        # tens-hundreds of ms, long enough for same-tick instantiate_agent pairs
        # to slip past the cooldown mask (desktop-65bg). Deque capped at 64.
        play_history = _merge_recent_completions(
            play_history, self._runtime.recent_play_completions
        )

        # Sibling shadow for per-issue applied labels (desktop-quv9): the gh CLI
        # label-add + add_issue_labels write lag a fast follow-up get_open_issues
        # read, so without overlaying the shadow a just-labelled issue would be
        # re-selected next tick (issue_available_for_debug filter misses it).
        open_issues = _merge_recent_applied_labels(open_issues, self._runtime.recent_applied_labels)

        # Recently-closed issues feed the dashboard Done column; frontend routes
        # state="closed" there, so the same projection as open issues suffices.
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

    async def _count_learnings_from_json(self) -> int:
        """Count learnings from the JSON store (non-zero when the file has entries).

        Runs the blocking file read via ``asyncio.to_thread`` so it fits
        naturally inside the ``TaskGroup`` that fans out the other DB reads.
        Falls back to 0 on any I/O or parse error to preserve the same
        best-effort semantics the old ``count_learnings`` DB call had.
        """
        import json as _json

        from agentshore.learnings import load as _load_learnings

        try:
            cfg = self._runtime.cfg
            path = self._repo_root / cfg.learnings.file
            learnings = await asyncio.to_thread(_load_learnings, path)
            return len(learnings)
        except (OSError, _json.JSONDecodeError, KeyError, ValueError, TypeError):
            return 0

    async def safe_get_latest_trajectory(self) -> TrajectorySnapshotRecord | None:
        """Best-effort trajectory fetch; logs and returns ``None`` on failure.

        Preserves the original ``try/except`` semantics from the monolithic
        ``build_state``: a trajectory read failure must not abort state
        construction.
        """
        try:
            return await self._store.get_latest_trajectory(self._session_id)
        except aiosqlite.Error as exc:
            _logger.warning("trajectory_snapshot_failed", error=str(exc))
            return None

    async def abandon_work_for_missing_agents(self) -> None:
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

    def _in_flight_claim_group_ids(self) -> set[str]:
        """Claim groups backing currently-dispatched plays — never release these."""
        ids: set[str] = set()
        for ctx in self._runtime.dispatch_ctx.values():
            claim_group_id = getattr(getattr(ctx, "params", None), "extras", {}).get(
                "claim_group_id"
            )
            if isinstance(claim_group_id, str) and claim_group_id:
                ids.add(claim_group_id)
        return ids

    async def release_claims_for_prolonged_idle_agents(self, state: OrchestratorState) -> None:
        """Release active claims owned by agents that have stayed idle for several ticks."""
        from agentshore.state import AgentStatus

        threshold = self._runtime.cfg.rl.stale_idle_claim_release_ticks
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
        protected_claim_group_ids = self._in_flight_claim_group_ids()
        # #205: a claim group in ``retrying`` has no live dispatch (retry queued
        # in the override channel), so without this it looks idle-owned and gets
        # released out from under the pending retry. Protect those groups too.
        list_retrying = getattr(self._store, "list_retrying_claim_group_ids", None)
        if callable(list_retrying):
            retrying_group_ids = await list_retrying(self._session_id)
            if retrying_group_ids:
                protected_claim_group_ids = protected_claim_group_ids | retrying_group_ids
        releasable_claims = [
            claim
            for claim in claims
            if getattr(claim, "claim_group_id", None) not in protected_claim_group_ids
        ]
        claim_agent_ids = {
            claim.agent_id
            for claim in releasable_claims
            if getattr(claim, "agent_id", None) in idle_ids
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

        if protected_claim_group_ids:
            released_count = await release_method(
                self._session_id,
                release_agent_ids,
                exclude_claim_group_ids=protected_claim_group_ids,
            )
        else:
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

    def annotate_action_mask(self, state: OrchestratorState) -> None:
        """Attach action_mask + mask_reasons; logs and continues on failure."""
        from agentshore.plays.registry import PlayRegistry as _PlayRegistry

        registry = self._runtime.registry
        if not isinstance(registry, _PlayRegistry):
            return
        from agentshore.plays.candidates import build_candidate_plan
        from agentshore.rl.mask import compute_action_mask, compute_mask_reasons

        try:
            cfg = self._runtime.cfg
            config_index = self._host._selector_config_index()
            candidate_plan = build_candidate_plan(state)
            mask_arr = compute_action_mask(
                state,
                registry,
                cfg=cfg,
                config_index=config_index,
                apply_reverse_failsafe=cfg.rl.reverse_failsafe_enabled,
                candidate_plan=candidate_plan,
            )
            state.action_mask = tuple(bool(b) for b in mask_arr)
            state.mask_reasons = compute_mask_reasons(
                state,
                registry,
                cfg=cfg,
                config_index=config_index,
                apply_reverse_failsafe=cfg.rl.reverse_failsafe_enabled,
                candidate_plan=candidate_plan,
            )
        except (KeyError, ValueError, AttributeError) as exc:
            _logger.warning("action_mask_compute_failed", error=str(exc))

    @staticmethod
    def _filter_pull_requests_to_target(
        pull_requests: list[PullRequestSnapshot],
        target_branch: str | None,
    ) -> tuple[list[PullRequestSnapshot], int]:
        """Drop open PRs whose base branch != ``target_branch`` (Piece C).

        Returns ``(kept, hidden_count)``. The filter only engages when
        ``target_branch`` is explicitly set; otherwise the input list is returned
        unchanged. A PR is dropped only when its ``base_ref`` is a known, non-empty
        string that differs from the target — PRs with an unknown base (``None`` /
        empty) are kept so missing data never hides work. One ``github_pr_ignored``
        event is emitted per dropped PR (the direct analog of
        ``github_issue_ignored``) for forensics.
        """
        if not target_branch:
            return pull_requests, 0
        kept: list[PullRequestSnapshot] = []
        hidden = 0
        for pr in pull_requests:
            base = getattr(pr, "base_ref", None)
            if isinstance(base, str) and base and base != target_branch:
                hidden += 1
                _logger.info(
                    "github_pr_ignored",
                    reason="wrong_base_branch",
                    pr_number=pr.pr_number,
                    base_ref=base,
                    target_branch=target_branch,
                )
            else:
                kept.append(pr)
        return kept, hidden

    def _drain_wedge_cooldowns(self) -> frozenset[str]:
        """Seed/decay transient launch-wedge cooldowns; return the active set.

        Mirror of the permanent auth-suppression drain, but DECAYING: a wedge the
        manager recorded since the last snapshot seeds an expiry tick
        (``current_tick + _GROK_WEDGE_COOLDOWN_TICKS``), and any entry whose
        expiry has passed is dropped so the type auto-recovers (#202). Pure
        dict/set ops, no I/O. ``last_play_id`` is the monotonic per-play tick
        counter (0 before the first play). ``getattr`` tolerates a stub manager
        without the attribute.
        """
        from agentshore.agents.manager import _GROK_WEDGE_COOLDOWN_TICKS

        current_tick = self._runtime.last_play_id or 0
        cooldown_until = self._runtime.wedge_cooldown_until

        manager_wedged: set[str] = getattr(self._manager, "wedge_cooldown_types", set())
        newly_wedged = sorted(set(manager_wedged) - set(cooldown_until))
        for agent_type in newly_wedged:
            cooldown_until[agent_type] = current_tick + _GROK_WEDGE_COOLDOWN_TICKS
        if newly_wedged:
            # Reason tag per type (#233): "launch_wedge" (Grok first-byte) vs
            # "stream_hang_cluster" (agy zero-stdout). Collapse to one label when
            # uniform, else "mixed".
            reasons_map: dict[str, str] = getattr(self._manager, "wedge_cooldown_reasons", {})
            reasons = {reasons_map.get(t, "launch_wedge") for t in newly_wedged}
            reason = reasons.pop() if len(reasons) == 1 else "mixed"
            _logger.warning(
                "agent_type_wedge_cooldown",
                session_id=self._session_id,
                agent_types=newly_wedged,
                reason=reason,
                cooldown_ticks=_GROK_WEDGE_COOLDOWN_TICKS,
                expires_at_tick=current_tick + _GROK_WEDGE_COOLDOWN_TICKS,
            )

        # Drop expired entries (current tick reached expiry) so the type recovers.
        expired = [
            agent_type for agent_type, expiry in cooldown_until.items() if current_tick >= expiry
        ]
        for agent_type in expired:
            del cooldown_until[agent_type]
        if expired:
            _logger.info(
                "agent_type_wedge_cooldown_expired",
                session_id=self._session_id,
                agent_types=sorted(expired),
                current_tick=current_tick,
            )

        return frozenset(cooldown_until)

    def current_budget_snapshot(self) -> BudgetSnapshot | None:
        """Re-derive the budget snapshot from cached inputs + a fresh clock.

        Used by the loop's budget-countdown heartbeat so the dashboard's
        remaining-time figure keeps ticking during quiet stretches without a DB
        read. Returns ``None`` until the first full state assembly has cached the
        dollar inputs, or when no time cap is configured (nothing to count down).
        Only the time fields move between calls — the dollar figures are the
        last-assembled values, which is correct because spend only changes on a
        dispatch/completion, both of which already push a full state update.
        """
        inputs = self._last_budget_inputs
        if inputs is None:
            return None
        budget_cfg = self._host.effective_budget_caps()
        if not budget_cfg.time_enabled:
            return None
        total_plays, total_cost = inputs
        loop_started_at = self._runtime.loop_started_at
        elapsed_minutes = (
            (time.monotonic() - loop_started_at) / 60.0 if loop_started_at > 0 else 0.0
        )
        return self._snapshots.build_budget_snapshot(
            total_plays,
            total_cost,
            budget_cfg=budget_cfg,
            elapsed_minutes=elapsed_minutes,
        )

    async def build_budget_only(self) -> BudgetSnapshot:
        """Build only the budget snapshot via one cheap aggregate query.

        Side-effect-free read path for the live ``session.get_budget`` prefill
        (the desktop "Adjust Budget…" dialog, #281). Unlike :meth:`build_state`
        it skips the ten-read fan-out + beads graph load and the two mutating
        helpers (``abandon_work_for_missing_agents`` /
        ``release_claims_for_prolonged_idle_agents``) — a prefill must never
        abandon work or release claims. Reuses the single
        ``COUNT(*)/SUM(dollar_cost)`` ``session_play_totals`` query and refreshes
        the cached dollar inputs so the budget-countdown heartbeat keeps working.

        ``session_play_totals`` counts every play row (internal types included),
        whereas :meth:`assemble_state` excludes internal plays from ``total_plays``
        — but that count only feeds the cosmetic ``estimated_cost_per_play`` field,
        which the ``session.get_budget`` echo does not return, so the dollar/time
        figures the dialog prefills are identical either way.
        """
        total_plays, total_cost = await self._store.session_play_totals(self._session_id)
        self._last_budget_inputs = (total_plays, total_cost)
        loop_started_at = self._runtime.loop_started_at
        elapsed_minutes = (
            (time.monotonic() - loop_started_at) / 60.0 if loop_started_at > 0 else 0.0
        )
        return self._snapshots.build_budget_snapshot(
            total_plays,
            total_cost,
            budget_cfg=self._host.effective_budget_caps(),
            elapsed_minutes=elapsed_minutes,
        )

    def assemble_state(self, data: _StateData) -> OrchestratorState:
        """Pure transformation: ``_StateData`` + live handles -> ``OrchestratorState``.

        No I/O. Unit-testable by constructing a ``_StateData`` directly.
        """
        # Drain manager-stamped backend-auth failures into the session
        # suppression set. This is the one point with both manager + runtime in
        # hand, so per-agent AUTH classification becomes session-wide agent-type
        # suppression the candidate analyzer masks on (#zeke auth-hang).
        manager_auth_failed: set[str] = getattr(self._manager, "last_auth_failed_types", set())
        newly_auth_suppressed = manager_auth_failed - self._runtime.auth_suppressed_agent_types
        if newly_auth_suppressed:
            self._runtime.auth_suppressed_agent_types |= newly_auth_suppressed
            _logger.warning(
                "agent_type_auth_suppressed",
                session_id=self._session_id,
                agent_types=sorted(newly_auth_suppressed),
                reason="backend_auth_failed",
            )
        wedge_cooldown_types = self._drain_wedge_cooldowns()
        cfg = self._runtime.cfg
        agents = self._snapshots.build_agent_snapshots(data.play_history)
        open_issues = self._snapshots.project_open_issues(data.issue_records, data.graph)
        pull_requests = self._snapshots.project_pull_requests(data.pr_records)
        # Piece C: when target_branch is set, drop open PRs whose known base
        # differs — out of scope and must not reach dashboard/candidates/
        # backpressure. PRs with unknown base are kept; skipped when unset.
        pull_requests, ignored_pr_count = self._filter_pull_requests_to_target(
            pull_requests, cfg.project.target_branch
        )
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

        # total_plays excludes internal plays; same filter as compute_session_stats
        # so the HUD counter matches ESR.
        internal_play_values = {pt.value for pt in INTERNAL_PLAY_TYPES}
        total_plays = sum(1 for p in data.play_history if p.play_type not in internal_play_values)
        total_cost = sum(p.dollar_cost for p in data.play_history)
        same_type_failure_streak, same_type_streak = self._snapshots.compute_play_streaks(
            data.play_history,
            override_play_ids=self._overrides.dispatched_play_ids,
        )
        (
            last_play_type,
            plays_since_last_instantiate,
            plays_since_last_play_type,
            last_play_success_by_type,
            last_play_skipped_by_type,
            seed_freshness,
            consecutive_nonproductive_by_type,
        ) = self._snapshots.compute_play_recency(data.play_history)
        loop_started_at = self._runtime.loop_started_at
        elapsed_minutes = (
            (time.monotonic() - loop_started_at) / 60.0 if loop_started_at > 0 else 0.0
        )
        budget = self._snapshots.build_budget_snapshot(
            total_plays,
            total_cost,
            budget_cfg=self._host.effective_budget_caps(),
            elapsed_minutes=elapsed_minutes,
        )
        # Cache dollar inputs so the budget heartbeat re-derives the countdown
        # off a fresh clock without a DB read.
        self._last_budget_inputs = (total_plays, total_cost)
        trajectory = self._snapshots.extract_trajectory(data.trajectory_record)
        stats = self._snapshots.compute_session_stats(data.play_history)

        if self._runtime.stop_requested:
            session_state = SessionState.SHUTTING_DOWN
        elif self._runtime.draining:
            session_state = SessionState.DRAINING
        elif not self._runtime.pause_event.is_set():
            session_state = SessionState.PAUSED
        else:
            session_state = SessionState.RUNNING
        in_flight = self._runtime.in_flight
        in_flight_plays = [
            ctx.play_type
            for dispatch_id, ctx in self._runtime.dispatch_ctx.items()
            if dispatch_id in in_flight and not in_flight[dispatch_id].done()
        ]

        # Snapshot runtime latches so the mask hides the matching plays from PPO;
        # dispatch_play gates 1-2 still re-check live (state can flip between
        # selection and dispatch). end_session_in_flight mirrors gate 2 (started
        # latch OR any in-flight END_SESSION dispatch).
        main_repo_dispatch_paused = self._main_repo.dispatch_paused
        end_session_in_flight = self._runtime.end_session_dispatch_started or (
            PlayType.END_SESSION in in_flight_plays
        )

        state = OrchestratorState(
            session_id=self._session_id,
            session_state=session_state,
            total_plays=total_plays,
            total_cost=total_cost,
            policy_mode=cfg.rl.policy_mode,
            target_branch=cfg.project.target_branch,
            agents=agents,
            open_issues=open_issues,
            pull_requests=pull_requests,
            ignored_pr_count=ignored_pr_count,
            pending_review_queue=pending_review_queue,
            budget=budget,
            trajectory=trajectory,
            same_type_failure_streak=same_type_failure_streak,
            same_type_streak=same_type_streak,
            last_play_type=last_play_type,
            recent_executor_skip=self._velocity.recent_executor_skip,
            in_flight_plays=in_flight_plays,
            in_flight_issues=list(self._executor.inflight_issues),
            planned_issues=self._executor.planned_issues,
            restrict_issues_to_trusted_authors=cfg.trusted_ids.restrict_issues_to_trusted_authors,
            trusted_issue_authors=(
                trusted_issue_author_logins(cfg)
                if cfg.trusted_ids.restrict_issues_to_trusted_authors
                else frozenset()
            ),
            parked_resource_keys=frozenset(self._runtime.parked_resource_keys),
            auth_suppressed_agent_types=frozenset(self._runtime.auth_suppressed_agent_types),
            wedge_cooldown_agent_types=wedge_cooldown_types,
            plays_since_last_instantiate=plays_since_last_instantiate,
            plays_since_last_play_type=plays_since_last_play_type,
            last_play_success_by_type=last_play_success_by_type,
            last_play_skipped_by_type=last_play_skipped_by_type,
            consecutive_nonproductive_by_type=consecutive_nonproductive_by_type,
            loop_level=loop_level_for_streak(same_type_failure_streak),
            main_repo_dispatch_paused=main_repo_dispatch_paused,
            end_session_in_flight=end_session_in_flight,
            recovery_exhausted_agent_ids=self._recovery.recovery_exhausted_agent_ids(agents),
            drain_reason=self._runtime.drain_reason if self._runtime.draining else None,
            graph=data.graph,
            stats=stats,
            run_mode=cfg.mode,
            action_space_version=ACTION_SPACE_VERSION,
            policy_version=self._runtime.policy_version,
            policy_checkpoint_id=data.policy_checkpoint_id,
            seed_freshness=seed_freshness,
            learnings_count=data.learnings_count,
            human_feedback_count=data.human_feedback_count,
        )

        self.annotate_action_mask(state)
        return state

    async def build_state(self) -> OrchestratorState:
        """Rebuild authoritative OrchestratorState from live handles + DB (no caching).

        Thin orchestration: fan out DB reads via ``fetch_state_data``, then
        delegate the pure construction to ``assemble_state``.
        """
        await self.abandon_work_for_missing_agents()
        data = await self.fetch_state_data()
        state = self.assemble_state(data)
        await self.release_claims_for_prolonged_idle_agents(state)
        return state
