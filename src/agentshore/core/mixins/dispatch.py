"""Override consumption, play selection, dispatch, and mask handling."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import uuid
from typing import TYPE_CHECKING

from agentshore.core.base import _OrchestratorBase
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
    SELECTED_CANDIDATE_NO_LONGER_AVAILABLE,
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
    from pathlib import Path

    from agentshore.config import RuntimeConfig
    from agentshore.data.store import DataStore
    from agentshore.plays.base import PlayParams
    from agentshore.plays.executor import PlayExecutor
    from agentshore.plays.selector import PlaySelector
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


_CANDIDATE_REVALIDATED_PLAY_TYPES: frozenset[PlayType] = frozenset(
    {
        PlayType.WRITE_IMPLEMENTATION_PLAN,
        PlayType.ISSUE_PICKUP,
        PlayType.REFINE_TASK_BREAKDOWN,
        PlayType.SYSTEMATIC_DEBUGGING,
        PlayType.CODE_REVIEW,
        PlayType.MERGE_PR,
        PlayType.UNBLOCK_PR,
        PlayType.GROOM_BACKLOG,
    }
)


class _DispatchMixin(_OrchestratorBase):
    """Override resolution, selector calls, dispatch, and mask handling."""

    _cfg: RuntimeConfig
    _session_id: str
    _store: DataStore
    _executor: PlayExecutor
    _selector: PlaySelector | None
    _state_provider: StateProvider
    _stop_requested: bool
    _draining: bool
    _end_session_dispatch_started: bool
    _in_flight: dict[str, asyncio.Task[PlayOutcome]]
    _dispatch_ctx: dict[str, _DispatchContext]
    _first_play_override: tuple[PlayType, PlayParams] | None
    _override_queue: asyncio.Queue[OverrideEntry]
    _pending_override_kind: OverrideKind | None
    _registry: object | None

    _last_selection_digest: bytes | None
    # desktop-kqo5: shared with _CompletionMixin via the base class. Pre-play
    # symbolic ref snapshot keyed by dispatch_id, plus the cached default
    # branch resolved at session start.
    _pre_play_branches: dict[str, str | None]
    _default_branch: str
    _main_repo_dispatch_paused: bool

    # ------------------------------------------------------------------

    async def _revalidate_end_session_before_dispatch(self) -> bool:
        """Refresh external work state before allowing an END_SESSION play.

        END_SESSION is a terminal lifecycle play. Before dispatching it, force
        a fresh GitHub/cache snapshot and rebuild the candidate plan so a stale
        no-work state cannot shut down the session while newly-created QA or
        audit follow-up issues are available.
        """

        from agentshore.plays.candidates import build_candidate_plan

        if self._end_session_dispatch_started or any(
            ctx.play_type == PlayType.END_SESSION
            for dispatch_id, ctx in self._dispatch_ctx.items()
            if dispatch_id in self._in_flight and not self._in_flight[dispatch_id].done()
        ):
            _logger.warning(
                "end_session_revalidation_blocked",
                session_id=self._session_id,
                reason="end_session_already_in_flight",
            )
            return False

        await self._safe_call(self._refresh_issues(), "refresh_issues_before_end_session")
        fresh_state = await self._build_state()
        candidate_plan = build_candidate_plan(fresh_state)
        if not candidate_plan.has_remaining_work:
            return True

        availability = candidate_plan.work_availability
        self._last_selection_digest = None
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
        await self._safe_call(
            self._state_provider.on_state_update(fresh_state),
            "on_state_update_end_session_revalidated",
        )
        return False

    def _shutdown_allows_only_end_agent(self, state: OrchestratorState) -> bool:
        """Return True once the session may only wind down live agents."""
        return (
            self._draining
            or self._stop_requested
            or state.session_state in (SessionState.DRAINING, SessionState.SHUTTING_DOWN)
        )

    async def _consume_override(
        self, state: OrchestratorState
    ) -> tuple[PlayType, PlayParams] | None:
        """Pop one queued override play, masking it if preconditions disallow.

        Order: first-play override (seed) wins over the human-override queue.
        Returns ``None`` if there is no override available or if the candidate
        is masked by the action mask.

        Side-effect: sets ``self._pending_override_kind`` to the OverrideKind
        of the consumed entry so ``_dispatch_play`` can mark the resulting
        ``_DispatchContext`` for the loop detector to skip. Reset to None at
        the top of every consume so a stale value can't leak through.
        """
        self._pending_override_kind = None
        shutdown_only = self._shutdown_allows_only_end_agent(state)
        if self._first_play_override is not None:
            override_play: tuple[PlayType, PlayParams] = self._first_play_override
            self._first_play_override = None
            if shutdown_only and override_play[0] != PlayType.END_AGENT:
                _logger.warning(
                    "override_dropped_during_shutdown",
                    play_type=override_play[0].value,
                    session_id=self._session_id,
                )
                return None
            # first_play_override is set during seed/bootstrap — treat as bootstrap kind.
            self._pending_override_kind = OverrideKind.BOOTSTRAP
            _logger.info(
                "first_play_override",
                play_type=override_play[0].value,
                session_id=self._session_id,
            )
            return override_play

        if self._override_queue.empty():
            return None

        entry: OverrideEntry | None = self._override_queue.get_nowait()
        while shutdown_only and entry is not None and entry.play_type != PlayType.END_AGENT:
            _logger.warning(
                "override_dropped_during_shutdown",
                play_type=entry.play_type.value,
                kind=entry.kind.value,
                session_id=self._session_id,
            )
            entry = None if self._override_queue.empty() else self._override_queue.get_nowait()

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
                _override_log = _logger.debug if self._idle_streak > 1 else _logger.info
                _override_log(
                    "override_waiting_for_play_type",
                    play_type=entry.play_type.value,
                    kind=entry.kind.value,
                    wait_for_play_type=awaited.value,
                    session_id=self._session_id,
                )
                await self._handle_masked_override(entry, wait_reason)
                entry = None

        from agentshore.plays.candidates import build_candidate_plan
        from agentshore.plays.registry import PlayRegistry as _PlayRegistry
        from agentshore.rl.action_space import V1_ACTION_ORDER
        from agentshore.rl.mask import compute_action_mask, compute_mask_reasons

        if (
            entry is not None
            and not entry.params.bypass_preconditions
            and isinstance(self._registry, _PlayRegistry)
            and entry.play_type in V1_ACTION_ORDER
        ):
            config_index = self._selector_config_index()
            candidate_plan = build_candidate_plan(state)
            mask = compute_action_mask(
                state,
                self._registry,
                cfg=self._cfg,
                config_index=config_index,
                apply_reverse_failsafe=self._cfg.rl.reverse_failsafe_enabled,
                candidate_plan=candidate_plan,
            )
            if not mask[V1_ACTION_ORDER.index(entry.play_type)]:
                reasons = compute_mask_reasons(
                    state,
                    self._registry,
                    cfg=self._cfg,
                    config_index=config_index,
                    apply_reverse_failsafe=self._cfg.rl.reverse_failsafe_enabled,
                    candidate_plan=candidate_plan,
                )
                reason = reasons.get(entry.play_type, ACTION_MASKED)
                log_fn = (
                    _logger.info
                    if self._mask_reason_is_indefinite_wait(reason)
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
                await self._handle_masked_override(entry, reason)
                entry = None

        if entry is not None:
            _logger.info(
                "override_queue_dequeued",
                play_type=entry.play_type.value,
                kind=entry.kind.value,
                session_id=self._session_id,
            )
            self._pending_override_kind = entry.kind
            return entry.play_type, entry.params
        return None

    @staticmethod
    def _params_have_dispatch_target(params: PlayParams) -> bool:
        return bool(
            params.issue_number is not None
            or params.pr_number is not None
            or params.extras.get("claim_group_id")
            or params.extras.get("request_mutation_key")
            or params.extras.get("resource_keys")
        )

    @staticmethod
    def _mask_reason_is_transient(reason: MaskReason | str) -> bool:
        """True if the override should re-queue with a bounded retry counter.

        Accepts typed MaskReason (preferred — read .classification) or a raw
        string for the remaining legacy emission sites yet to be migrated.
        """
        if isinstance(reason, MaskReason):
            return reason.classification == MaskClassification.TRANSIENT
        lowered = reason.lower()
        return any(
            marker in lowered
            for marker in (
                "no idle",
                "idle agent",
                "rate_limit",
                "quota",
                "temporarily",
            )
        )

    @staticmethod
    def _mask_reason_is_indefinite_wait(reason: MaskReason | str) -> bool:
        """True if the override should re-queue without bumping the retry counter.

        Deterministic-clear waits (cooldown, sequencing, evidence windows) live
        here — the override survives until the awaited condition lifts.
        Accepts typed MaskReason (preferred — read .classification) or a raw
        string for the remaining legacy emission sites yet to be migrated.
        """
        if isinstance(reason, MaskReason):
            return reason.classification == MaskClassification.INDEFINITE_WAIT
        lowered = reason.lower()
        return "waiting for" in lowered or "cooldown" in lowered or "plays since last" in lowered

    async def _handle_masked_override(self, entry: OverrideEntry, reason: MaskReason | str) -> None:
        # 1. BOOTSTRAP entries never drop. They drive the fleet-sequencing
        #    invariant (large agent → seed → medium of different type) and
        #    must survive arbitrary cooldown / wait masks until the awaited
        #    condition lifts.
        if entry.kind == OverrideKind.BOOTSTRAP:
            self._override_queue.put_nowait(
                dataclasses.replace(entry, kind=OverrideKind.MASK_REQUEUE)
            )
            return

        # 2. INDEFINITE_WAIT classifications (typed at the mask source or
        #    declared at enqueue time) re-queue without bumping the retry
        #    counter — the wait clears deterministically.
        if (
            self._mask_reason_is_indefinite_wait(reason)
            or entry.enqueue_classification == MaskClassification.INDEFINITE_WAIT
        ):
            self._override_queue.put_nowait(
                dataclasses.replace(entry, kind=OverrideKind.MASK_REQUEUE)
            )
            return

        # 3. TRANSIENT classifications re-queue with a bounded retry counter.
        if (
            self._mask_reason_is_transient(reason)
            and entry.requeue_attempts < _MAX_MASKED_OVERRIDE_REQUEUES
        ):
            self._override_queue.put_nowait(
                dataclasses.replace(
                    entry.with_bumped_attempts(),
                    kind=OverrideKind.MASK_REQUEUE,
                )
            )
            return

        # 4. Everything else (HARD classifications, exhausted transient
        #    budget, USER_REQUEST hitting a hard mask) drops with a surfaced
        #    error.
        await self._release_masked_override(entry, reason=reason)

    async def _release_masked_override(
        self, entry: OverrideEntry, *, reason: MaskReason | str
    ) -> None:
        play_type = entry.play_type
        params = entry.params
        claim_group_id = params.extras.get("claim_group_id")
        if isinstance(claim_group_id, str) and claim_group_id:
            await self._store.release_work_claim_group(self._session_id, claim_group_id)

        request_mutation_key = params.extras.get("request_mutation_key")
        if isinstance(request_mutation_key, str) and request_mutation_key:
            await self._store.update_external_mutation_status(
                self._session_id,
                request_mutation_key,
                "blocked",
                json.dumps(
                    {
                        "error": "promoted request_play became masked before dispatch",
                        "play": play_type.value,
                        "reason": str(reason),
                        "pr": params.pr_number,
                        "issue": params.issue_number,
                        "resource_keys": params.extras.get("resource_keys", []),
                    }
                ),
            )

        _logger.warning(
            "override_dropped_masked",
            play_type=play_type.value,
            kind=entry.kind.value,
            reason=str(reason),
            classification=(
                reason.classification.value if isinstance(reason, MaskReason) else "unknown"
            ),
            session_id=self._session_id,
        )

    async def _record_control_rejection(
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
        await self._safe_call(
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

    async def _drop_selected_play_before_dispatch(
        self,
        play_type: PlayType,
        params: PlayParams,
        *,
        reason: MaskReason | str,
        event: str,
    ) -> None:
        if isinstance(self._selector, _ppo_selector_cls()):
            self._selector.consume_pending()
        claim_group_id = params.extras.get("claim_group_id")
        if isinstance(claim_group_id, str) and claim_group_id:
            await self._safe_call(
                self._store.release_work_claim_group(self._session_id, claim_group_id),
                "release_dispatch_revalidation_claim",
            )
        request_mutation_key = params.extras.get("request_mutation_key")
        if isinstance(request_mutation_key, str) and request_mutation_key:
            await self._safe_call(
                self._store.update_external_mutation_status(
                    self._session_id,
                    request_mutation_key,
                    "blocked",
                    json.dumps(
                        {
                            "error": "selected play became invalid before dispatch",
                            "play": play_type.value,
                            "reason": str(reason),
                            "pr": params.pr_number,
                            "issue": params.issue_number,
                            "resource_keys": params.extras.get("resource_keys", []),
                        }
                    ),
                ),
                "update_dispatch_revalidation_request_mutation",
            )
        await self._record_control_rejection(
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

    async def _dispatch_revalidation_reason(
        self,
        play_type: PlayType,
        params: PlayParams,
        state: OrchestratorState,
    ) -> MaskReason | None:
        if params.bypass_preconditions:
            return None
        if play_type in _CANDIDATE_REVALIDATED_PLAY_TYPES and not self._params_have_dispatch_target(
            params
        ):
            return None

        from agentshore.plays.candidates import build_candidate_plan
        from agentshore.plays.registry import PlayRegistry as _PlayRegistry
        from agentshore.rl.action_space import V1_ACTION_ORDER
        from agentshore.rl.mask import compute_action_mask, compute_mask_reasons

        candidate_plan = build_candidate_plan(state)
        if isinstance(self._registry, _PlayRegistry) and play_type in V1_ACTION_ORDER:
            mask = compute_action_mask(
                state,
                self._registry,
                cfg=self._cfg,
                config_index=self._selector_config_index(),
                apply_reverse_failsafe=bool(params.extras.get("reverse_failsafe")),
                candidate_plan=candidate_plan,
            )
            if not mask[V1_ACTION_ORDER.index(play_type)]:
                reasons = compute_mask_reasons(
                    state,
                    self._registry,
                    cfg=self._cfg,
                    config_index=self._selector_config_index(),
                    apply_reverse_failsafe=bool(params.extras.get("reverse_failsafe")),
                    candidate_plan=candidate_plan,
                )
                return reasons.get(play_type, ACTION_MASKED)

        if play_type not in _CANDIDATE_REVALIDATED_PLAY_TYPES:
            return None
        candidates = candidate_plan.candidates_for(play_type)
        if not candidates:
            blocked = candidate_plan.blocked_reasons_by_play_type.get(play_type, ())
            if blocked:
                return MaskReason(
                    text=blocked[0],
                    classification=MaskClassification.HARD,
                    source=MaskSource.CANDIDATE,
                )
            return SELECTED_CANDIDATE_NO_LONGER_AVAILABLE

        for candidate in candidates:
            if params.issue_number is not None:
                if candidate.params.issue_number == params.issue_number:
                    # desktop-xi9d: live beads graph can race ahead of the
                    # cached snapshot the selector saw. Do a final refresh
                    # before claiming dispatch so a CLOSED/IN_PROGRESS bead
                    # produces a HARD revalidation block (which does NOT
                    # consume the action) rather than a play_completed:
                    # skipped row inside execute() (which used to burn a
                    # whole PPO step on the race).
                    live_reason = await self._refresh_live_graph_for_issue(
                        play_type, candidate.params.issue_number
                    )
                    if live_reason is not None:
                        return live_reason
                    return None
                continue
            if params.pr_number is not None:
                if candidate.params.pr_number == params.pr_number:
                    return None
                continue
            return None
        return SELECTED_CANDIDATE_NO_LONGER_AVAILABLE

    async def _refresh_live_graph_for_issue(
        self,
        play_type: PlayType,
        issue_number: int,
    ) -> MaskReason | None:
        """Re-check the live beads graph for *issue_number* at dispatch time.

        Returns a HARD MaskReason when the live graph reports the bead is
        no longer OPEN; ``None`` otherwise (including when the live graph
        is unavailable — fall back to the cached snapshot path used to
        gate dispatch since the desktop-mb5g rework).

        Only fires for the issue-graph plays in
        ``_CANDIDATE_REVALIDATED_PLAY_TYPES``; PR-graph plays use the
        existing candidate set and don't consult beads.
        """
        if play_type not in {
            PlayType.ISSUE_PICKUP,
            PlayType.WRITE_IMPLEMENTATION_PLAN,
            PlayType.SYSTEMATIC_DEBUGGING,
            PlayType.REFINE_TASK_BREAKDOWN,
        }:
            return None

        try:
            from agentshore.beads import BeadStatus, load_graph, pick_bead_for_issue
        except ImportError:
            return None

        try:
            graph = await load_graph(self._repo_root)
        except Exception as exc:  # pragma: no cover - defensive, beads CLI shouldn't blow up
            _logger.warning(
                "dispatch_live_graph_refresh_failed",
                play_type=play_type.value,
                issue_number=issue_number,
                error=str(exc),
                session_id=self._session_id,
            )
            return None

        if graph is None:
            return None

        live_task = pick_bead_for_issue(graph.tasks, issue_number)
        if live_task is None or live_task.status == BeadStatus.OPEN:
            return None

        if live_task.status == BeadStatus.IN_PROGRESS:
            self._record_live_graph_skip(play_type, issue_number)
            return MaskReason(
                text=(
                    f"live beads check: bead {live_task.bead_id} for gh-{issue_number} "
                    f"is in_progress — refusing dispatch"
                ),
                classification=MaskClassification.HARD,
                source=MaskSource.CANDIDATE,
            )

        _logger.info(
            "bead_closed_but_github_open",
            bead_id=live_task.bead_id,
            issue_number=issue_number,
            play_type=play_type.value,
            session_id=self._session_id,
        )
        return None

    def _record_live_graph_skip(self, play_type: PlayType, issue_number: int) -> None:
        """Update the play instance's skip-circuit-breaker, if it exposes one.

        Currently only ``IssuePickupPlay`` tracks per-issue skip streaks; the
        other ``_CANDIDATE_REVALIDATED_PLAY_TYPES`` simply re-evaluate every
        tick. Guard against missing attributes so we stay forward-compatible
        with plays that gain (or lose) the hook.
        """
        from agentshore.plays.registry import PlayRegistry as _PlayRegistry

        if not isinstance(self._registry, _PlayRegistry):
            return
        try:
            play = self._registry.get(play_type)
        except KeyError:
            return
        record_skip = getattr(play, "_record_skip", None)
        if record_skip is None:
            return
        # IssuePickupPlay._record_skip(issue_number, total_plays). The
        # ``total_plays`` field on OrchestratorState normally drives the
        # cooldown expiry; at dispatch time we use the loop counter we
        # already have access to via the orchestrator.
        try:
            record_skip(issue_number, self._total_plays_for_skip())
        except Exception as exc:  # pragma: no cover - defensive
            _logger.warning(
                "live_graph_skip_record_failed",
                play_type=play_type.value,
                issue_number=issue_number,
                error=str(exc),
                session_id=self._session_id,
            )

    def _total_plays_for_skip(self) -> int:
        """Return the current play counter for the skip-circuit-breaker.

        Subclasses can override; the base implementation uses the dispatch
        context dict size which approximates total dispatches for the
        session and is safe for the cooldown's relative-distance check.
        """
        return len(self._dispatch_ctx)

    async def _select_play(
        self,
        state: OrchestratorState,
        *,
        override_play: tuple[PlayType, PlayParams] | None,
    ) -> tuple[PlayType, PlayParams] | None:
        """Select the next play: queued override > selector > None (idle)."""
        if override_play is not None:
            return override_play
        if self._selector is not None:
            return await self._selector.select(state)
        return None

    async def _dispatch_play(
        self,
        play_type: PlayType,
        params: PlayParams,
        state: OrchestratorState,
        *,
        revalidate: bool | None = None,
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
        """
        # desktop-kqo5: hard pause when auto-restore failed. Refuse to spawn
        # further work until the trunk is healed. END_AGENT is still allowed so a
        # draining shutdown can complete cleanly. RECONCILE_STATE is ALSO allowed:
        # it is the dirty-trunk healer, so blocking it under the pause created a
        # catch-22 that wedged the loop. Letting it through lets the session
        # self-heal a conflicted trunk; a successful reconcile clears the latch
        # (see _check_main_repo_invariant).
        if self._main_repo_dispatch_paused and play_type not in (
            PlayType.END_AGENT,
            PlayType.RECONCILE_STATE,
        ):
            await self._drop_selected_play_before_dispatch(
                play_type,
                params,
                reason="main_repo_dispatch_paused",
                event="dispatch_blocked_main_repo_paused",
            )
            return False
        if play_type == PlayType.END_SESSION and (
            self._end_session_dispatch_started
            or any(
                ctx.play_type == PlayType.END_SESSION
                for dispatch_id, ctx in self._dispatch_ctx.items()
                if dispatch_id in self._in_flight and not self._in_flight[dispatch_id].done()
            )
        ):
            await self._drop_selected_play_before_dispatch(
                play_type,
                params,
                reason="end_session_already_in_flight",
                event="dispatch_revalidation_blocked",
            )
            return False
        if revalidate is None:
            revalidate = isinstance(self._selector, _ppo_selector_cls())
        if self._shutdown_allows_only_end_agent(state) and play_type != PlayType.END_AGENT:
            await self._drop_selected_play_before_dispatch(
                play_type,
                params,
                reason="shutdown_allows_only_end_agent",
                event="dispatch_blocked_during_shutdown",
            )
            return False
        if revalidate:
            selected_at = time.monotonic()
            reason = await self._dispatch_revalidation_reason(play_type, params, state)
            if reason is not None:
                revalidated_at = time.monotonic()
                # Capture the selector→revalidate delta so the size of the
                # race window is queryable from the log stream. Stored on
                # the params extras so the warning emission inside
                # _drop_selected_play_before_dispatch picks it up.
                params = dataclasses.replace(
                    params,
                    extras={
                        **params.extras,
                        "selected_at_monotonic": selected_at,
                        "revalidated_at_monotonic": revalidated_at,
                        "revalidation_window_seconds": round(revalidated_at - selected_at, 6),
                    },
                )
                await self._drop_selected_play_before_dispatch(
                    play_type,
                    params,
                    reason=reason,
                    event="dispatch_revalidation_blocked",
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
                await self._drop_selected_play_before_dispatch(
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
            await self._drop_selected_play_before_dispatch(
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
                await self._drop_selected_play_before_dispatch(
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
            self._end_session_dispatch_started = True
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
        await self._safe_call(self._state_provider.on_state_update(state), "on_state_update")
        # The real executor emits this after agent selection. Tests and
        # adapters may provide a simpler executor that does not.
        if getattr(self._executor, "emits_play_started", None) is not True:
            await self._safe_call(
                self._state_provider.on_play_started(play_type, params),
                "on_play_started",
            )

        dispatch_id = str(uuid.uuid4())
        # desktop-kqo5: snapshot the main-repo symbolic ref BEFORE the task
        # fires. ``current_head_ref`` returns None for detached HEAD, which
        # _CompletionMixin treats as a mutation of its own at completion.
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
        self._pre_play_branches[dispatch_id] = pre_play_ref

        pending: object | None = None
        if isinstance(self._selector, _ppo_selector_cls()):
            pending = self._selector.consume_pending()

        # Read-and-clear: the very next dispatch consumes whatever
        # _consume_override left behind. Any subsequent dispatch (e.g. PPO-
        # selected following an override miss) defaults to None.
        override_kind = self._pending_override_kind
        self._pending_override_kind = None

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
        self._in_flight[dispatch_id] = task_obj
        self._dispatch_ctx[dispatch_id] = ctx
        return True
