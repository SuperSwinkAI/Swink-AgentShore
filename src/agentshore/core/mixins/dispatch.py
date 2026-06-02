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
    from agentshore.config import RuntimeConfig
    from agentshore.core.main_repo_guard import MainRepoGuard
    from agentshore.core.mixins.completion import CompletionProcessor
    from agentshore.core.mixins.state import StateBuilder
    from agentshore.core.override_queue import OverrideQueue
    from agentshore.data.store import DataStore
    from agentshore.plays.base import PlayParams
    from agentshore.plays.executor import PlayExecutor
    from agentshore.plays.selector import PlaySelector
    from agentshore.rl.action_space import ConfigKey
    from agentshore.rl.eligibility import LiveGraphLoader
    from agentshore.state import (
        OrchestratorState,
        PlayOutcome,
        StateProvider,
    )


_MAX_MASKED_OVERRIDE_REQUEUES = 3


def _is_git_work_tree(path: Path) -> bool:
    """Return True when ``path`` (or one of its parents) hosts a git work tree.

    Used to short-circuit ``WorktreeManager`` allocation in test harnesses
    that pass a bare ``tmp_path`` as ``repo_root``. Production paths are
    always real git checkouts. A ``.git`` directory or file (the latter is
    how nested worktrees point back at their git common dir) satisfies the
    check without needing a ``git`` subprocess.
    """
    candidate = path
    for _ in range(6):  # bounded walk; project paths nest at most a few deep
        if (candidate / ".git").exists():
            return True
        if candidate.parent == candidate:
            return False
        candidate = candidate.parent
    return False


class _DispatcherHost(Protocol):
    """Orchestrator runtime/control state read OR written live by :class:`Dispatcher`.

    These members are accessed fresh via ``self._host.<attr>`` on every call so
    SIGHUP config swaps (``_cfg``) and per-tick mutation (in-flight maps,
    dispatch-context map, selection digest, idle streak, end-session latch,
    bootstrap-assigned registry/selector) are always current — never captured at
    construction. Fields the dispatcher *writes* (``_end_session_dispatch_started``,
    ``_last_selection_digest``) are declared as plain annotated attributes (not
    read-only ``@property``) so the assignments type-check. ``_in_flight`` /
    ``_dispatch_ctx`` are mutated in place. ``_OrchestratorBase`` structurally
    satisfies this Protocol; the cross-component methods (``_safe_call``,
    ``_selector_config_index``) are resolved live on the composition root.
    """

    # --- written by the dispatcher -----------------------------------------
    _end_session_dispatch_started: bool
    _last_selection_digest: bytes | None
    # --- read by the dispatcher (and the two maps mutated in place) ---------
    _cfg: RuntimeConfig
    _selector: PlaySelector | None
    _state_provider: StateProvider
    _stop_requested: bool
    _draining: bool
    _in_flight: dict[str, asyncio.Task[PlayOutcome]]
    _dispatch_ctx: dict[str, _DispatchContext]
    _registry: object | None
    _idle_streak: int

    async def _safe_call(self, coro: Awaitable[object], label: str) -> None: ...

    def _selector_config_index(self) -> tuple[ConfigKey, ...] | None: ...


class Dispatcher:
    """Override resolution, selector calls, dispatch, and mask handling.

    Stable services / collaborators (store, manager, executor, the 1a
    collaborators ``main_repo``/``overrides``, and the sibling components
    ``state_builder``/``completion``) are captured via the constructor; all
    orchestrator runtime/control state (read or written) flows through the
    :class:`_DispatcherHost` Protocol so SIGHUP and per-tick mutation never goes
    stale.
    """

    def __init__(
        self,
        *,
        host: _DispatcherHost,
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

    async def revalidate_end_session_before_dispatch(self) -> bool:
        """Refresh external work state before allowing an END_SESSION play.

        END_SESSION is a terminal lifecycle play. Before dispatching it, force
        a fresh GitHub/cache snapshot and rebuild the candidate plan so a stale
        no-work state cannot shut down the session while newly-created QA or
        audit follow-up issues are available.
        """

        from agentshore.plays.candidates import build_candidate_plan

        if self._host._end_session_dispatch_started or any(
            ctx.play_type == PlayType.END_SESSION
            for dispatch_id, ctx in self._host._dispatch_ctx.items()
            if dispatch_id in self._host._in_flight
            and not self._host._in_flight[dispatch_id].done()
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
        if not candidate_plan.has_remaining_work:
            return True

        availability = candidate_plan.work_availability
        self._host._last_selection_digest = None
        _logger.warning(
            "end_session_revalidation_blocked",
            session_id=self._session_id,
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
            self._host._state_provider.on_state_update(fresh_state),
            "on_state_update_end_session_revalidated",
        )
        return False

    def shutdown_allows_only_end_agent(self, state: OrchestratorState) -> bool:
        """Return True once the session may only wind down live agents."""
        return (
            self._host._draining
            or self._host._stop_requested
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

        # issue #569: targeted sequencing gate that survives bypass_preconditions.
        # When wait_for_play_type is set, hold the entry until that play type has
        # appeared in plays_since_last_play_type (i.e. completed at least once).
        # The bootstrap medium INSTANTIATE_AGENT uses this so the cleanup /
        # seed_project first-play finishes before a second agent comes online
        # and PPO can dispatch it onto PR-scoped plays that race trunk.
        if entry is not None and entry.wait_for_play_type is not None:
            awaited = entry.wait_for_play_type
            if awaited not in state.plays_since_last_play_type:
                wait_reason = MaskReason(
                    text=f"waiting for {awaited.value} to complete before releasing override",
                    classification=MaskClassification.INDEFINITE_WAIT,
                    source=MaskSource.PRECONDITION,
                )
                _override_log = _logger.debug if self._host._idle_streak > 1 else _logger.info
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
            and isinstance(self._host._registry, _PlayRegistry)
            and entry.play_type in V1_ACTION_ORDER
        ):
            # Single source of truth: route the override through the same
            # EligibilityAuthority that masks PPO's action space. One live
            # confirm; a not-valid verdict means a clean re-pick via the
            # existing masked-override requeue taxonomy.
            authority = EligibilityAuthority(
                state,
                self._host._registry,
                cfg=self._host._cfg,
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
        selector = self._host._selector
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
        #    invariant (large agent → seed → medium of different type) and
        #    must survive arbitrary cooldown / wait masks until the awaited
        #    condition lifts.
        if entry.kind == OverrideKind.BOOTSTRAP:
            self._overrides.put_nowait(dataclasses.replace(entry, kind=OverrideKind.MASK_REQUEUE))
            return

        # 2. INDEFINITE_WAIT classifications (typed at the mask source or
        #    declared at enqueue time) re-queue without bumping the retry
        #    counter — the wait clears deterministically.
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

        # 4. Everything else (HARD classifications, exhausted transient
        #    budget) drops with a surfaced error.
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
        if isinstance(self._host._selector, _ppo_selector_cls()):
            self._host._selector.consume_pending()
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

    async def select_play(
        self,
        state: OrchestratorState,
        *,
        override_play: tuple[PlayType, PlayParams] | None,
    ) -> tuple[PlayType, PlayParams] | None:
        """Select the next play: queued override > selector > None (idle)."""
        if override_play is not None:
            return override_play
        if self._host._selector is not None:
            return await self._host._selector.select(state)
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

        Phase 4 of the v0.15 architecture refactor: this method now takes the
        same state the selector used for the selection decision. State is no
        longer rebuilt at dispatch time — that rebuild was the structural
        cause of the cross-tick divergence (HOTSPOT 1 in
        docs/design/play_lifecycle.html) and produced the
        ``play_skipped_masked_at_executor`` events fixed in v0.14.4.

        Eligibility refactor (Wave 1): this method is now purely
        side-effecting (worktree alloc, ``active_play`` snapshot, dispatch
        context + task creation). Play validity is settled upstream by the
        ``EligibilityAuthority`` — the action mask presents only valid plays
        to PPO, and ``_consume_override`` confirms overrides against the same
        authority. The dispatch-time revalidation pass is gone, so there is no
        ``revalidate`` parameter to thread through.
        """
        # desktop-kqo5: hard pause when auto-restore failed. Refuse to spawn
        # further work until the trunk is healed. END_AGENT is still allowed so a
        # draining shutdown can complete cleanly. RECONCILE_STATE is ALSO allowed:
        # it is the dirty-trunk healer, so blocking it under the pause created a
        # catch-22 that wedged the loop. Letting it through lets the session
        # self-heal a conflicted trunk; a successful reconcile clears the latch
        # (see _check_main_repo_invariant).
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
            self._host._end_session_dispatch_started
            or any(
                ctx.play_type == PlayType.END_SESSION
                for dispatch_id, ctx in self._host._dispatch_ctx.items()
                if dispatch_id in self._host._in_flight
                and not self._host._in_flight[dispatch_id].done()
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
        # desktop-4ugk part 2: refuse to spawn any agent against a working
        # directory whose path contains a literal backslash-space. The
        # canonical leak comes from a quoting bug in a skill template; once
        # the path is on disk the subprocess `cd` would fail or land in a
        # leaked sibling. Reject before we wire up the dispatch context.
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

        # AgentShore-managed worktree allocation (desktop-mr1i). Runs *before* the
        # active_play snapshot so a failed allocation drops the play without
        # ever entering the in-flight set. Trunk-scoped / internal plays get
        # back a ``TrunkAllocation`` pointing at the main repo — no row is
        # written and no on-disk worktree is created.
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
        # When the project path isn't a git work tree (test harnesses that pass
        # a bare ``tmp_path``), short-circuit to a TrunkAllocation pointing at
        # the main repo. Production paths are always git checkouts; this guard
        # keeps the dispatcher honest without forcing every existing test that
        # mocks ``_executor.execute`` to also stub the worktree manager.
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
            try:
                allocation = await worktree_mgr.allocate_for_dispatch(
                    play_type=play_type, params=params
                )
            except (WorktreeAllocationFailed, WorktreeBranchGone) as exc:
                _logger.warning(
                    "worktree_allocate_failed",
                    session_id=self._session_id,
                    play_type=play_type.value,
                    error=str(exc),
                    reason=getattr(exc, "reason", None) or getattr(exc, "branch", None),
                )
                await self.drop_selected_play_before_dispatch(
                    play_type,
                    params,
                    reason="worktree_create_failed",
                    event="dispatch_worktree_create_failed",
                )
                return False

        # Stamp allocator output:
        #
        # (a) The allocation dataclass itself goes on a private
        #     ``_runtime_allocation`` field on PlayParams — runtime-only,
        #     excluded from ``params_to_json_safe_dict``. Previously this was
        #     stamped into ``params.extras["worktree_allocation"]``, but
        #     extras crosses the JSON boundary (context.json + dispatch_replay
        #     rows) and shipping live Python dataclass handles through that
        #     surface produced a recurring JSON-serialize bug (issue #563
        #     onion: TrunkAllocation → Path → PlayType enum). Issue #565
        #     moves the handle off extras so the JSON path can't see it.
        #
        # (b) String-only views of the allocation stay in extras so skills
        #     can keep reading them as documented (worktree_path,
        #     worktree_scope). The existing backslash-space validator above
        #     also matches against worktree_path coming out of the allocator.
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
            self._host._end_session_dispatch_started = True
        # OrchestratorState is intentionally non-frozen to allow in-loop state patching.
        # Populate the typed ActivePlay snapshot so IPC consumers see what's
        # running, who is running it, and when it started without waiting
        # for the separate on_play_started event.
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
            self._host._state_provider.on_state_update(state), "on_state_update"
        )
        # The real executor emits this after agent selection. Tests and
        # adapters may provide a simpler executor that does not.
        if getattr(self._executor, "emits_play_started", None) is not True:
            await self._host._safe_call(
                self._host._state_provider.on_play_started(play_type, params),
                "on_play_started",
            )

        dispatch_id = str(uuid.uuid4())
        # desktop-kqo5: snapshot the main-repo symbolic ref BEFORE the task
        # fires. ``current_head_ref`` returns None for detached HEAD, which
        # CompletionProcessor treats as a mutation of its own at completion.
        # Run synchronously — the git read is small (~5ms) and pre-task
        # ordering is load-bearing.
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
        if isinstance(self._host._selector, _ppo_selector_cls()):
            pending = self._host._selector.consume_pending()

        # Read-and-clear: the very next dispatch consumes whatever
        # _consume_override left behind. Any subsequent dispatch (e.g. PPO-
        # selected following an override miss) defaults to None.
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
        self._host._in_flight[dispatch_id] = task_obj
        self._host._dispatch_ctx[dispatch_id] = ctx
        return True
