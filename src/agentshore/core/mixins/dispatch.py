"""Override consumption, play selection, dispatch, and mask handling."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import uuid
from typing import TYPE_CHECKING, Protocol

from agentshore.core.context import _DispatchContext
from agentshore.core.git_safety import (
    current_head_ref,
    path_contains_backslash_space,
)
from agentshore.core.helpers import _log_task_exception, _logger, _ppo_selector_cls, _str_extra
from agentshore.data.store import ExternalMutationRecord
from agentshore.errors import is_disk_full
from agentshore.plays.override import OverrideEntry, OverrideKind
from agentshore.rl.mask_reason import (
    ACTION_MASKED,
    MaskClassification,
    MaskReason,
    MaskSource,
)
from agentshore.state import (
    ActivePlay,
    PlayType,
    SessionState,
)
from agentshore.utils import now_iso

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path

    from agentshore.agents.manager import AgentManager
    from agentshore.core.main_repo_guard import MainRepoGuard
    from agentshore.core.mixins.completion import CompletionProcessor
    from agentshore.core.mixins.state import StateBuilder
    from agentshore.core.override_queue import OverrideQueue
    from agentshore.core.session_runtime import SessionRuntime
    from agentshore.data.store import DataStore
    from agentshore.plays.base import PlayParams
    from agentshore.plays.executor import PlayExecutor
    from agentshore.rl.config_head import ConfigKey
    from agentshore.rl.eligibility import LiveGraphLoader
    from agentshore.state import (
        OrchestratorState,
        PlayOutcome,
    )


_MAX_MASKED_OVERRIDE_REQUEUES = 3
# Consecutive worktree-allocation failures on one resource key before it is
# parked for the session (Piece A, #60). Allows retries for a transient blip
# while stopping a structurally-unallocatable PR from re-selecting every tick.
_WORKTREE_PARK_THRESHOLD = 3


def _is_git_work_tree(path: Path) -> bool:
    """Return True when ``path`` (or one of its parents) hosts a git work tree.

    Used to short-circuit ``WorktreeManager`` allocation in test harnesses
    that pass a bare ``tmp_path`` as ``repo_root``. Production paths are
    always real git checkouts. A ``.git`` directory or file (the latter is
    how nested worktrees point back at their git common dir) satisfies the
    check without needing a ``git`` subprocess.
    """
    candidate = path
    for _ in range(6):  # bounded walk; project paths nest only a few deep
        if (candidate / ".git").exists():
            return True
        if candidate.parent == candidate:
            return False
        candidate = candidate.parent
    return False


class _DispatcherHost(Protocol):
    """Orchestrator *behaviour* the :class:`Dispatcher` invokes.

    All shared session *state* now lives on :class:`SessionRuntime` (reached via
    ``self._runtime``); this Protocol is the narrow behaviour seam that remains so
    the cross-component methods resolve on the composition root without a circular
    import. ``_OrchestratorBase`` structurally satisfies it.
    """

    async def _safe_call(self, coro: Awaitable[object], label: str) -> None: ...

    def _selector_config_index(self) -> tuple[ConfigKey, ...] | None: ...


class Dispatcher:
    """Override resolution, selector calls, dispatch, and mask handling.

    Stable services / collaborators (store, manager, executor, the 1a
    collaborators ``main_repo``/``overrides``, and the sibling components
    ``state_builder``/``completion``) are captured via the constructor; all shared
    session state (read or written) lives on the injected :class:`SessionRuntime`,
    and the cross-component behaviour methods resolve via the narrow
    :class:`_DispatcherHost` behaviour seam.
    """

    def __init__(
        self,
        *,
        host: _DispatcherHost,
        runtime: SessionRuntime,
        store: DataStore,
        manager: AgentManager,
        executor: PlayExecutor,
        session_id: str,
        repo_root: Path,
        main_repo: MainRepoGuard,
        overrides: OverrideQueue,
        state_builder: StateBuilder,
        completion: CompletionProcessor,
    ) -> None:
        self._host = host
        self._runtime = runtime
        self._store = store
        self._manager = manager
        self._executor = executor
        self._session_id = session_id
        self._repo_root = repo_root
        self._main_repo = main_repo
        self._overrides = overrides
        self._state_builder = state_builder
        self._completion = completion

    # ------------------------------------------------------------------

    async def revalidate_end_session_before_dispatch(self, *, failsafe: bool = False) -> bool:
        """Refresh external work state before allowing an END_SESSION play.

        END_SESSION is a terminal lifecycle play. Before dispatching it, force
        a fresh GitHub/cache snapshot and rebuild the candidate plan so a stale
        no-work state cannot shut down the session while newly-created QA or
        audit follow-up issues are available.

        When ``failsafe`` is True the END_SESSION arrived via the reverse-failsafe
        deadlock-breaker (every agent idle, every other play masked for N ticks).
        In that wedged state the revalidation blocks only on genuinely dispatchable
        work — open workable issues or actionable PRs — and treats beads bookkeeping
        (``backlog_sync_work`` / ``ready_tasks``) as non-blocking, since no play in
        the current fleet state can act on it. The normal path keeps the strict
        ``has_remaining_work`` check; its eligibility gate already masks END_SESSION
        whenever ready tasks remain, so this looser rule cannot end a healthy session
        early.
        """

        from agentshore.plays.candidates import build_candidate_plan

        if self._runtime.end_session_dispatch_started or any(
            ctx.play_type == PlayType.END_SESSION
            for dispatch_id, ctx in self._runtime.dispatch_ctx.items()
            if dispatch_id in self._runtime.in_flight
            and not self._runtime.in_flight[dispatch_id].done()
        ):
            _logger.warning(
                "end_session_revalidation_blocked",
                session_id=self._session_id,
                reason="end_session_already_in_flight",
            )
            return False

        await self._host._safe_call(
            self._completion.refresh_issues(), "refresh_issues_before_end_session"
        )
        fresh_state = await self._state_builder.build_state()
        candidate_plan = build_candidate_plan(fresh_state)
        availability = candidate_plan.work_availability
        if failsafe:
            dispatchable_work = (
                availability.workable_issue_count > 0 or availability.actionable_pr_work_count > 0
            )
            if not dispatchable_work:
                return True
        elif not candidate_plan.has_remaining_work:
            return True

        self._runtime.last_selection_digest = None
        _logger.warning(
            "end_session_revalidation_blocked",
            session_id=self._session_id,
            failsafe=failsafe,
            tracked_issues=availability.tracked_issue_count,
            github_open_issues=availability.github_open_issue_count,
            workable_issues=availability.workable_issue_count,
            implementation_eligible=availability.implementation_eligible_count,
            planning_eligible=availability.planning_eligible_count,
            ready_tasks=availability.ready_task_count,
            backlog_sync_work=availability.backlog_sync_work_count,
            actionable_pr_work=availability.actionable_pr_work_count,
            beads_blocks_issue_pickup=availability.beads_blocks_issue_pickup,
        )
        await self._host._safe_call(
            self._runtime.state_provider.on_state_update(fresh_state),
            "on_state_update_end_session_revalidated",
        )
        return False

    def shutdown_allows_only_end_agent(self, state: OrchestratorState) -> bool:
        """Return True once the session may only wind down live agents."""
        return (
            self._runtime.draining
            or self._runtime.stop_requested
            or state.session_state in (SessionState.DRAINING, SessionState.SHUTTING_DOWN)
        )

    async def consume_override(
        self, state: OrchestratorState
    ) -> tuple[PlayType, PlayParams] | None:
        """Pop one queued override play, masking it if preconditions disallow.

        Order: first-play override (seed) wins over the human-override queue.
        Returns ``None`` if there is no override available or if the candidate
        is masked by the action mask.

        Side-effect: sets ``self._overrides.pending_override_kind`` to the OverrideKind
        of the consumed entry so ``_dispatch_play`` can mark the resulting
        ``_DispatchContext`` for the loop detector to skip. Reset to None at
        the top of every consume so a stale value can't leak through.
        """
        self._overrides.pending_override_kind = None
        shutdown_only = self.shutdown_allows_only_end_agent(state)
        if self._overrides.first_play_override is not None:
            override_play: tuple[PlayType, PlayParams] = self._overrides.first_play_override
            self._overrides.first_play_override = None
            if shutdown_only and override_play[0] != PlayType.END_AGENT:
                _logger.warning(
                    "override_dropped_during_shutdown",
                    play_type=override_play[0].value,
                    session_id=self._session_id,
                )
                return None
            # first_play_override is set during seed/bootstrap — treat as bootstrap kind.
            self._overrides.pending_override_kind = OverrideKind.BOOTSTRAP
            _logger.info(
                "first_play_override",
                play_type=override_play[0].value,
                session_id=self._session_id,
            )
            return override_play

        if self._overrides.empty():
            return None

        entry: OverrideEntry | None = self._overrides.get_nowait()
        while shutdown_only and entry is not None and entry.play_type != PlayType.END_AGENT:
            _logger.warning(
                "override_dropped_during_shutdown",
                play_type=entry.play_type.value,
                kind=entry.kind.value,
                session_id=self._session_id,
            )
            entry = None if self._overrides.empty() else self._overrides.get_nowait()

        # #569: sequencing gate that survives bypass_preconditions. Hold the
        # entry until wait_for_play_type has completed at least once (appears in
        # plays_since_last_play_type). The bootstrap medium INSTANTIATE_AGENT
        # uses this so the trunk-touching first-play finishes before a second
        # agent comes online and PPO races it onto PR-scoped plays.
        if entry is not None and entry.wait_for_play_type is not None:
            awaited = entry.wait_for_play_type
            if awaited not in state.plays_since_last_play_type:
                wait_reason = MaskReason(
                    text=f"waiting for {awaited.value} to complete before releasing override",
                    classification=MaskClassification.INDEFINITE_WAIT,
                    source=MaskSource.PRECONDITION,
                )
                _override_log = _logger.debug if self._runtime.idle_streak > 1 else _logger.info
                _override_log(
                    "override_waiting_for_play_type",
                    play_type=entry.play_type.value,
                    kind=entry.kind.value,
                    wait_for_play_type=awaited.value,
                    session_id=self._session_id,
                )
                await self.handle_masked_override(entry, wait_reason)
                entry = None

        from agentshore.plays.registry import PlayRegistry as _PlayRegistry
        from agentshore.rl.action_space import V1_ACTION_ORDER
        from agentshore.rl.eligibility import EligibilityAuthority

        if (
            entry is not None
            and not entry.params.bypass_preconditions
            and isinstance(self._runtime.registry, _PlayRegistry)
            and entry.play_type in V1_ACTION_ORDER
        ):
            # Single source of truth: route the override through the same
            # EligibilityAuthority that masks PPO's action space. A not-valid
            # verdict means a clean re-pick via the masked-override requeue path.
            authority = EligibilityAuthority(
                state,
                self._runtime.registry,
                cfg=self._runtime.cfg,
                config_index=self._host._selector_config_index(),
                live_graph_loader=self._override_confirm_live_loader(),
            )
            verdict = await authority.confirm(entry.play_type, entry.params, state)
            if not verdict.valid:
                reason = verdict.reason or ACTION_MASKED
                log_fn = (
                    _logger.info
                    if self.mask_reason_is_indefinite_wait(reason)
                    or entry.enqueue_classification == MaskClassification.INDEFINITE_WAIT
                    else _logger.warning
                )
                log_fn(
                    "override_masked",
                    play_type=entry.play_type.value,
                    kind=entry.kind.value,
                    reason=str(reason),
                    classification=reason.classification.value,
                    enqueue_classification=(
                        entry.enqueue_classification.value
                        if entry.enqueue_classification is not None
                        else None
                    ),
                    session_id=self._session_id,
                )
                await self.handle_masked_override(entry, reason)
                entry = None

        if entry is not None:
            _logger.info(
                "override_queue_dequeued",
                play_type=entry.play_type.value,
                kind=entry.kind.value,
                session_id=self._session_id,
            )
            self._overrides.pending_override_kind = entry.kind
            return entry.play_type, entry.params
        return None

    def _override_confirm_live_loader(self) -> LiveGraphLoader | None:
        """Live-graph loader for the override-confirm path.

        Reuses the selector's loader so an override is revalidated against the
        same fresh-beads view as a PPO-selected play. Without it, ``confirm``
        would fall back to the snapshot and miss the selection→dispatch drift a
        sibling agent can introduce (e.g. flipping a bead to in_progress).
        Returns ``None`` (snapshot-only confirm) when the selector is not a real
        PPO selector (test stubs / non-beads sessions), matching the selector's
        own fallback.
        """
        selector = self._runtime.selector
        if not isinstance(selector, _ppo_selector_cls()):
            return None
        return selector._build_live_graph_loader()

    @staticmethod
    def mask_reason_is_transient(reason: MaskReason) -> bool:
        """True if the override should re-queue with a bounded retry counter.

        ``MaskReason.classification`` is the single source of truth — every
        override-queue caller now passes a typed reason (the eligibility
        authority and the sequencing gate both emit ``MaskReason``).
        """
        return reason.classification == MaskClassification.TRANSIENT

    @staticmethod
    def mask_reason_is_indefinite_wait(reason: MaskReason) -> bool:
        """True if the override should re-queue without bumping the retry counter.

        Deterministic-clear waits (cooldown, sequencing, evidence windows) live
        here — the override survives until the awaited condition lifts. Driven
        entirely by ``MaskReason.classification``.
        """
        return reason.classification == MaskClassification.INDEFINITE_WAIT

    async def handle_masked_override(self, entry: OverrideEntry, reason: MaskReason) -> None:
        # 1. BOOTSTRAP entries never drop. They drive the fleet-sequencing
        #    invariant (large → seed → medium of a different type) and must
        #    survive any cooldown/wait mask until the awaited condition lifts.
        if entry.kind == OverrideKind.BOOTSTRAP:
            self._overrides.put_nowait(dataclasses.replace(entry, kind=OverrideKind.MASK_REQUEUE))
            return

        # 2. INDEFINITE_WAIT (typed at mask source or declared at enqueue) re-
        #    queues without bumping the retry counter — the wait clears
        #    deterministically.
        if (
            self.mask_reason_is_indefinite_wait(reason)
            or entry.enqueue_classification == MaskClassification.INDEFINITE_WAIT
        ):
            self._overrides.put_nowait(dataclasses.replace(entry, kind=OverrideKind.MASK_REQUEUE))
            return

        # 3. TRANSIENT classifications re-queue with a bounded retry counter.
        if (
            self.mask_reason_is_transient(reason)
            and entry.requeue_attempts < _MAX_MASKED_OVERRIDE_REQUEUES
        ):
            self._overrides.put_nowait(
                dataclasses.replace(
                    entry.with_bumped_attempts(),
                    kind=OverrideKind.MASK_REQUEUE,
                )
            )
            return

        # 4. Everything else (HARD, or exhausted transient budget) drops with a
        #    surfaced error.
        await self.release_masked_override(entry, reason=reason)

    async def release_masked_override(self, entry: OverrideEntry, *, reason: MaskReason) -> None:
        play_type = entry.play_type
        params = entry.params
        claim_group_id = params.extras.get("claim_group_id")
        if isinstance(claim_group_id, str) and claim_group_id:
            await self._store.release_work_claim_group(self._session_id, claim_group_id)

        _logger.warning(
            "override_dropped_masked",
            play_type=play_type.value,
            kind=entry.kind.value,
            reason=str(reason),
            classification=reason.classification.value,
            session_id=self._session_id,
        )

    async def record_control_rejection(
        self,
        *,
        kind: str,
        play_type: PlayType,
        params: PlayParams,
        reason: MaskReason | str,
    ) -> None:
        payload = {
            "play": play_type.value,
            "reason": str(reason),
            "issue": params.issue_number,
            "pr": params.pr_number,
            "agent": params.agent_id,
            "resource_keys": params.extras.get("resource_keys", []),
        }
        await self._host._safe_call(
            self._store.record_external_mutation(
                ExternalMutationRecord(
                    session_id=self._session_id,
                    idempotency_key=f"{kind}:{self._session_id}:{uuid.uuid4().hex}",
                    mutation_type=kind,
                    target=play_type.value,
                    status="recorded",
                    created_at=now_iso(),
                    request_json=json.dumps(payload),
                )
            ),
            f"record_{kind}",
        )

    async def drop_selected_play_before_dispatch(
        self,
        play_type: PlayType,
        params: PlayParams,
        *,
        reason: MaskReason | str,
        event: str,
    ) -> None:
        if isinstance(self._runtime.selector, _ppo_selector_cls()):
            self._runtime.selector.consume_pending()
        claim_group_id = params.extras.get("claim_group_id")
        if isinstance(claim_group_id, str) and claim_group_id:
            await self._host._safe_call(
                self._store.release_work_claim_group(self._session_id, claim_group_id),
                "release_dispatch_revalidation_claim",
            )
        await self.record_control_rejection(
            kind="dispatch_revalidation_block",
            play_type=play_type,
            params=params,
            reason=reason,
        )
        selected_at = params.extras.get("selected_at_monotonic")
        revalidated_at = params.extras.get("revalidated_at_monotonic")
        revalidation_window_seconds = params.extras.get("revalidation_window_seconds")
        _logger.warning(
            event,
            play_type=play_type.value,
            reason=str(reason),
            classification=(
                reason.classification.value if isinstance(reason, MaskReason) else "unknown"
            ),
            session_id=self._session_id,
            issue=params.issue_number,
            pr=params.pr_number,
            resource_keys=params.extras.get("resource_keys", []),
            selected_at=selected_at,
            revalidated_at=revalidated_at,
            revalidation_window_seconds=revalidation_window_seconds,
        )

    def register_worktree_allocation_failure(self, params: PlayParams) -> bool:
        """Tally a worktree-allocation failure per resource key; park on threshold.

        Increments the per-resource failure counter for each resource key carried
        on ``params`` and, once a key reaches ``_WORKTREE_PARK_THRESHOLD``, adds it
        to the session park set so the candidate analyzer stops re-selecting it
        (Piece A backstop, issue #60). Returns ``True`` when any of this play's
        resource keys is parked as of this failure — the caller uses that to
        classify the drop as HARD (structurally stuck) vs TRANSIENT (still
        retrying). Resources with no usable key are treated as transient.
        """
        raw = params.extras.get("resource_keys", [])
        keys = (
            [k for k in raw if isinstance(k, str) and k] if isinstance(raw, (list, tuple)) else []
        )
        if not keys:
            return False
        newly_parked: list[str] = []
        for key in keys:
            if key in self._runtime.parked_resource_keys:
                continue
            count = self._runtime.resource_failure_counts.get(key, 0) + 1
            self._runtime.resource_failure_counts[key] = count
            if count >= _WORKTREE_PARK_THRESHOLD:
                self._runtime.parked_resource_keys.add(key)
                newly_parked.append(key)
        if newly_parked:
            _logger.warning(
                "dispatch_resource_parked",
                session_id=self._session_id,
                reason="worktree_allocation_failed",
                resource_keys=newly_parked,
                threshold=_WORKTREE_PARK_THRESHOLD,
            )
        return any(key in self._runtime.parked_resource_keys for key in keys)

    async def select_play(
        self,
        state: OrchestratorState,
        *,
        override_play: tuple[PlayType, PlayParams] | None,
    ) -> tuple[PlayType, PlayParams] | None:
        """Select the next play: queued override > selector > None (idle)."""
        if override_play is not None:
            return override_play
        if self._runtime.selector is not None:
            return await self._runtime.selector.select(state)
        return None

    async def dispatch_play(
        self,
        play_type: PlayType,
        params: PlayParams,
        state: OrchestratorState,
    ) -> bool:
        """Build the dispatch context and create the play task (fire-and-forget).

        Emits ``on_state_update`` and (if the executor doesn't) ``on_play_started``
        before launching the task so IPC/TUI consumers see the new active play
        immediately.

        Play validity is settled upstream by EligibilityAuthority. Dispatch owns
        worktree allocation, active-play state, context creation, and task launch
        using the selector snapshot.
        """
        # desktop-kqo5: hard pause when auto-restore failed — no new work until
        # the trunk is healed. END_AGENT stays allowed so a drain can finish.
        # RECONCILE_STATE is ALSO allowed: it heals the dirty trunk, so blocking
        # it wedged the loop in a catch-22; a successful reconcile clears the
        # latch (see _check_main_repo_invariant).
        if self._main_repo.dispatch_paused and play_type not in (
            PlayType.END_AGENT,
            PlayType.RECONCILE_STATE,
        ):
            await self.drop_selected_play_before_dispatch(
                play_type,
                params,
                reason="main_repo_dispatch_paused",
                event="dispatch_blocked_main_repo_paused",
            )
            return False
        if play_type == PlayType.END_SESSION and (
            self._runtime.end_session_dispatch_started
            or any(
                ctx.play_type == PlayType.END_SESSION
                for dispatch_id, ctx in self._runtime.dispatch_ctx.items()
                if dispatch_id in self._runtime.in_flight
                and not self._runtime.in_flight[dispatch_id].done()
            )
        ):
            await self.drop_selected_play_before_dispatch(
                play_type,
                params,
                reason="end_session_already_in_flight",
                event="dispatch_revalidation_blocked",
            )
            return False
        if self.shutdown_allows_only_end_agent(state) and play_type != PlayType.END_AGENT:
            await self.drop_selected_play_before_dispatch(
                play_type,
                params,
                reason="shutdown_allows_only_end_agent",
                event="dispatch_blocked_during_shutdown",
            )
            return False
        # desktop-4ugk part 2: refuse to spawn into a working dir whose path
        # contains a literal backslash-space (a skill-template quoting leak);
        # the subprocess `cd` would fail or land in a leaked sibling. Reject
        # before wiring up the dispatch context.
        manager_working_dir = getattr(getattr(self, "_manager", None), "_working_dir", None)
        candidate_paths: list[str] = []
        if manager_working_dir is not None:
            candidate_paths.append(str(manager_working_dir))
        extras_worktree = params.extras.get("worktree_path")
        if isinstance(extras_worktree, str) and extras_worktree:
            candidate_paths.append(extras_worktree)
        for candidate in candidate_paths:
            if path_contains_backslash_space(candidate):
                _logger.error(
                    "pre_dispatch_worktree_path_invalid",
                    session_id=self._session_id,
                    play_type=play_type.value,
                    path=candidate,
                    reason=(
                        "Working directory path contains literal backslash-space; "
                        "refusing to spawn agent subprocess (desktop-4ugk part 2)."
                    ),
                )
                await self.drop_selected_play_before_dispatch(
                    play_type,
                    params,
                    reason="worktree_path_backslash_space",
                    event="pre_dispatch_worktree_path_invalid",
                )
                return False

        # Worktree allocation (desktop-mr1i) runs *before* the active_play
        # snapshot so a failed allocation drops the play without entering the
        # in-flight set. Trunk-scoped / internal plays get a ``TrunkAllocation``
        # at the main repo — no row written, no on-disk worktree created.
        from agentshore.agents.worktree import (
            TrunkAllocation,
            WorktreeAllocation,
            WorktreeAllocationFailed,
            WorktreeBranchGone,
        )

        worktree_mgr = getattr(getattr(self, "_manager", None), "worktrees", None)
        if worktree_mgr is None:
            _logger.error(
                "worktree_manager_unavailable",
                session_id=self._session_id,
                play_type=play_type.value,
            )
            await self.drop_selected_play_before_dispatch(
                play_type,
                params,
                reason="worktree_manager_unavailable",
                event="dispatch_blocked_no_worktree_manager",
            )
            return False
        # Non-git project path (test harnesses passing a bare ``tmp_path``):
        # short-circuit to a TrunkAllocation at the main repo. Production paths
        # are always git checkouts; this spares tests that mock
        # ``_executor.execute`` from also stubbing the worktree manager.
        allocation: WorktreeAllocation | TrunkAllocation
        if not _is_git_work_tree(worktree_mgr.main_repo):
            allocation = TrunkAllocation(path=worktree_mgr.main_repo)
            _logger.debug(
                "worktree_skipped_non_git_repo",
                session_id=self._session_id,
                play_type=play_type.value,
                path=str(worktree_mgr.main_repo),
            )
        else:
            # Pre-dispatch disk guard (#180): reap idle worktrees (LRU, skipping
            # in-flight) first; if still below the floor, skip this dispatch
            # rather than risk an ENOSPC cascade mid-play (spend-while-dropping).
            # ``min_free_disk_mb == 0`` disables the guard.
            floor_mb = self._runtime.cfg.worktrees.min_free_disk_mb
            if floor_mb > 0:
                free_mb = worktree_mgr.free_disk_mb()
                if free_mb < floor_mb:
                    await worktree_mgr.reap_for_disk_pressure(target_free_mb=floor_mb)
                    free_mb = worktree_mgr.free_disk_mb()
                if free_mb < floor_mb:
                    _logger.warning(
                        "pre_dispatch_disk_guard_paused",
                        session_id=self._session_id,
                        play_type=play_type.value,
                        free_mb=free_mb,
                        floor_mb=floor_mb,
                    )
                    await self.drop_selected_play_before_dispatch(
                        play_type,
                        params,
                        reason=MaskReason(
                            text=f"disk below floor ({free_mb}MiB < {floor_mb}MiB)",
                            classification=MaskClassification.TRANSIENT,
                            source=MaskSource.SPAWN,
                        ),
                        event="dispatch_blocked_disk_pressure",
                    )
                    return False
            try:
                allocation = await worktree_mgr.allocate_for_dispatch(
                    play_type=play_type, params=params
                )
            except (WorktreeAllocationFailed, WorktreeBranchGone, OSError) as exc:
                # Disk-full is an *environment* condition, not a structurally-
                # stuck resource: parking the key would wrongly suppress it for
                # the session. Reap and drop TRANSIENT so it retries once the
                # host has room (#180).
                if is_disk_full(exc):
                    _logger.warning(
                        "worktree_allocate_disk_full",
                        session_id=self._session_id,
                        play_type=play_type.value,
                        error=str(exc),
                    )
                    await worktree_mgr.reap_for_disk_pressure(
                        target_free_mb=max(self._runtime.cfg.worktrees.min_free_disk_mb, 1),
                    )
                    await self.drop_selected_play_before_dispatch(
                        play_type,
                        params,
                        reason=MaskReason(
                            text="worktree allocation failed (disk full)",
                            classification=MaskClassification.TRANSIENT,
                            source=MaskSource.SPAWN,
                        ),
                        event="dispatch_worktree_disk_full",
                    )
                    return False
                if isinstance(exc, OSError):
                    # Non-ENOSPC OSError isn't a recognized allocation failure;
                    # re-raise rather than swallow it as a worktree-create miss.
                    raise
                alloc_reason = getattr(exc, "reason", None) or getattr(exc, "branch", None)
                _logger.warning(
                    "worktree_allocate_failed",
                    session_id=self._session_id,
                    play_type=play_type.value,
                    error=str(exc),
                    reason=alloc_reason,
                )
                # Piece A: tally per resource key and park keys past the retry
                # threshold. A parked resource is structurally stuck (HARD) — the
                # candidate analyzer stops offering it, so the drop no longer
                # hot-loops; below threshold it's still retrying (TRANSIENT).
                parked = self.register_worktree_allocation_failure(params)
                drop_reason = MaskReason(
                    text=f"worktree allocation failed ({alloc_reason})"
                    + (" — resource parked for session" if parked else ""),
                    classification=(
                        MaskClassification.HARD if parked else MaskClassification.TRANSIENT
                    ),
                    source=MaskSource.SPAWN,
                )
                await self.drop_selected_play_before_dispatch(
                    play_type,
                    params,
                    reason=drop_reason,
                    event="dispatch_worktree_create_failed",
                )
                return False

        # Stamp allocator output:
        # (a) The allocation dataclass goes on a private ``_runtime_allocation``
        #     field — runtime-only, excluded from ``params_to_json_safe_dict``.
        #     It was in ``extras["worktree_allocation"]``, but extras crosses the
        #     JSON boundary (context.json + dispatch_replay) and live dataclass
        #     handles there caused a recurring serialize bug (#563/#565).
        # (b) String-only views (worktree_path, worktree_scope) stay in extras so
        #     skills can keep reading them; the backslash-space validator above
        #     also matches worktree_path from the allocator.
        params = dataclasses.replace(
            params,
            extras={
                **params.extras,
                "worktree_path": str(allocation.path),
                "worktree_scope": (
                    allocation.scope if isinstance(allocation, WorktreeAllocation) else "trunk"
                ),
            },
            _runtime_allocation=allocation,
        )
        if isinstance(allocation, TrunkAllocation):
            _logger.debug(
                "worktree_trunk_allocation",
                session_id=self._session_id,
                play_type=play_type.value,
                path=str(allocation.path),
            )
        else:
            # Mark the worktree in-flight BEFORE the task is created so a
            # concurrent reap can't reclaim it mid-play. The manager owns the
            # protected set from here; finalize_after_dispatch releases it.
            if worktree_mgr is not None:
                worktree_mgr.register_dispatch(allocation)
            _logger.info(
                "worktree_allocated",
                session_id=self._session_id,
                play_type=play_type.value,
                worktree_id=allocation.worktree_id,
                path=str(allocation.path),
                branch=allocation.branch_name,
                pre_branch_key=allocation.pre_branch_key,
                scope=allocation.scope,
            )

        if play_type == PlayType.END_SESSION:
            self._runtime.end_session_dispatch_started = True
        # OrchestratorState is intentionally non-frozen for in-loop patching.
        # Populate the ActivePlay snapshot so IPC consumers see what's running,
        # who, and when without waiting for the on_play_started event.
        state.active_play = ActivePlay(
            play_type=play_type,
            agent_id=params.agent_id,
            started_at=now_iso(),
            issue_number=params.issue_number,
            pr_number=params.pr_number,
            branch=params.branch,
            trigger_agent_id=_str_extra(params, "trigger_agent_id"),
            trigger_agent_type=_str_extra(params, "trigger_agent_type"),
            trigger_error_class=_str_extra(params, "trigger_error_class"),
        )
        await self._host._safe_call(
            self._runtime.state_provider.on_state_update(state), "on_state_update"
        )
        # The real executor emits this after agent selection. Tests and
        # adapters may provide a simpler executor that does not.
        if getattr(self._executor, "emits_play_started", None) is not True:
            await self._host._safe_call(
                self._runtime.state_provider.on_play_started(play_type, params),
                "on_play_started",
            )

        dispatch_id = str(uuid.uuid4())
        # desktop-kqo5: snapshot the main-repo symbolic ref BEFORE the task
        # fires (detached HEAD → None, which CompletionProcessor reads as a
        # mutation at completion). Synchronous — pre-task ordering is load-
        # bearing and the git read is ~5ms.
        try:
            pre_play_ref = current_head_ref(self._repo_root)
        except Exception as exc:
            _logger.warning(
                "main_repo_check_failed",
                phase="pre_play",
                session_id=self._session_id,
                play_type=play_type.value,
                error=str(exc),
            )
            pre_play_ref = None
        self._main_repo.record_pre_play_branch(dispatch_id, pre_play_ref)

        pending: object | None = None
        if isinstance(self._runtime.selector, _ppo_selector_cls()):
            pending = self._runtime.selector.consume_pending()

        # Read-and-clear: this dispatch consumes whatever _consume_override left
        # behind; any subsequent dispatch (e.g. PPO after an override miss)
        # defaults to None.
        override_kind = self._overrides.pending_override_kind
        self._overrides.pending_override_kind = None

        ctx = _DispatchContext(
            dispatch_id=dispatch_id,
            play_type=play_type,
            params=params,
            state_at_dispatch=state,
            pending_step=pending,
            dispatched_at=time.monotonic(),
            override_kind=override_kind,
        )

        task_obj: asyncio.Task[PlayOutcome] = asyncio.create_task(
            self._executor.execute(play_type, state, override=params)
        )
        task_obj.add_done_callback(_log_task_exception)
        self._runtime.in_flight[dispatch_id] = task_obj
        self._runtime.dispatch_ctx[dispatch_id] = ctx
        return True
