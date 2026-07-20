"""Play-completion harvesting, RL experience persistence, and learnings update."""

from __future__ import annotations

import asyncio
import enum
import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from agentshore.budget import budget_reserve_reached
from agentshore.core.git_safety import check_main_repo_branch_mutated, restore_default_branch
from agentshore.core.helpers import (
    _logger,
    _ppo_selector_cls,
    _str_extra,
)
from agentshore.core.issue_syncer import _ALREADY_CLOSED_SIGNATURES, IssueSyncer
from agentshore.core.learnings_harvester import LearningsHarvester
from agentshore.core.recovery_tracker import (
    BREAK_RECOVERY_FAILURE_LIMIT,
)
from agentshore.core.terminal_park import (
    _UNBLOCK_MANUAL_REQUIRED_MARKERS,
    _WRITE_PLAN_UNPLANNABLE_MARKERS,
    TerminalParkPolicy,
)
from agentshore.core.trunk_artifacts import force_quarantine_wedge_paths
from agentshore.core.wedge_signals import collect_dirty_trunk_paths
from agentshore.data.models import ExternalMutationRecord
from agentshore.data.store import PlayRecord
from agentshore.github.labels import (
    NEEDS_HUMAN_LABEL,
    ROOT_CAUSE_FOUND_LABEL,
)
from agentshore.plays.base import PlayParams
from agentshore.plays.override import OverrideEntry, OverrideKind
from agentshore.state import AgentStatus, PlaySkipReason, PlayType
from agentshore.utils import now_iso

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from agentshore.agents.manager import AgentManager
    from agentshore.core.context import _DispatchContext
    from agentshore.core.main_repo_guard import MainRepoGuard
    from agentshore.core.mixins.drain import DrainController
    from agentshore.core.mixins.lifecycle import LifecycleController
    from agentshore.core.mixins.snapshots import SnapshotProjector
    from agentshore.core.mixins.state import StateBuilder
    from agentshore.core.override_queue import OverrideQueue
    from agentshore.core.recovery_tracker import RecoveryTracker
    from agentshore.core.session_runtime import SessionRuntime
    from agentshore.core.velocity_tracker import VelocityTracker
    from agentshore.data.store import DataStore
    from agentshore.plays.executor import PlayExecutor
    from agentshore.state import (
        OrchestratorState,
        PlayOutcome,
    )


class _CompletionVerdict(enum.Enum):
    """Outcome of an early completion-pipeline step.

    Replaces the prior ``return True means abort`` bool convention scattered
    across the skip and retry helpers: each early step now returns a typed
    verdict, and one ``match`` in ``process_completion`` decides whether to
    abort (``SKIPPED`` / ``RETRIED``) or run the remaining pipeline
    (``CONTINUE``).
    """

    CONTINUE = "continue"
    SKIPPED = "skipped"
    RETRIED = "retried"


# Plays that trigger a *full* paginated re-sync vs. the incremental since= sync.
# Deletions / repo transfers don't bump updated_at and so are invisible to
# incremental sync; these plays are the belt-and-suspenders that catch them.
_FULL_ISSUE_SYNC_PLAYS: frozenset[PlayType] = frozenset(
    {
        PlayType.SEED_PROJECT,
        PlayType.CLEANUP,
        PlayType.RECONCILE_STATE,
        PlayType.PRUNE,
    }
)

# Substring in an unblock_pr failure's ``error`` text meaning the target has
# irreconcilable merge conflicts (the skill emits
# ``error: "Merge conflicts require manual resolution"`` alongside
# ``blocked_by: "merge_conflicts"`` — the latter has no structured home on
# ``PlayOutcome``, so the error text is the signal, same convention as
# ``_UNBLOCK_MANUAL_REQUIRED_MARKERS``). Distinct from that list: a merge
# conflict is often resolvable once the base branch moves (a later rebase
# succeeds), so it only earns a short, clock-windowed repick cooldown (#312)
# — never the PERMANENT manual-required park that list triggers.
_UNBLOCK_PR_REPICK_COOLDOWN_MARKERS: tuple[str, ...] = ("merge conflict",)


def skip_category_to_reason(skip_category: str | None) -> PlaySkipReason:
    """Map an executor ``skip_category`` to the unified ``PlaySkipReason`` (TNQA 03 L1).

    Single source for the executor-time skip translation. ``masked`` and
    ``invalid_config`` both mean the action mask blocked the play
    (``all_masked``); ``no_target`` / ``staffing`` mean no concrete candidate
    resolved (``no_eligible_targets``); anything else falls back to
    ``selector_returned_none``. The loop-tick selector-None path classifies from
    tick state instead (``LoopRunner.classify_play_skipped_reason``); this table
    covers only the executor's ``skip_category`` vocabulary.
    """
    if skip_category in {"masked", "invalid_config"}:
        return "all_masked"
    if skip_category in {"no_target", "staffing"}:
        return "no_eligible_targets"
    return "selector_returned_none"


def _outcome_signals_already_closed(outcome: PlayOutcome) -> bool:
    """Return True when an issue_pickup outcome describes an already-closed issue."""
    try:
        serialised = json.dumps(outcome.artifacts, default=str)
    except (TypeError, ValueError):
        return False
    return any(sig in serialised for sig in _ALREADY_CLOSED_SIGNATURES)


def _outcome_blocked_by_sibling_pr(outcome: PlayOutcome) -> bool:
    """Return True when an unblock_pr outcome reports the target is gated on an
    unmerged sibling PR (a structured ``blocked_by_pr`` artifact).

    Such a failure is not the target PR's own fault — it is waiting on another
    open PR that this dispatch could not finish merging. It must therefore NOT
    tick the per-PR exhaustion counter or trip the ``manual-required`` park; the
    PPO will pick the blocker as its own candidate, after which the target
    becomes unblockable. ``PlayOutcome`` carries no structured ``blocked_by``
    field, so the artifact is the signal (an error-text marker would risk
    colliding with the ``_UNBLOCK_MANUAL_REQUIRED_MARKERS`` substring scan).
    """
    return any(
        isinstance(artifact, dict) and artifact.get("type") == "blocked_by_pr"
        for artifact in outcome.artifacts
    )


def _outcome_resolved_target_pr(outcome: PlayOutcome, pr_number: int) -> bool:
    """Return True when a *successful* unblock_pr outcome resolved the target PR.

    Resolution means the dispatch either merged the target (``pr_merged``) or
    dismissed the sole stale ``CHANGES_REQUESTED`` review and left the PR ready
    (``stale_review_state``). Both are definitive wins, not failed attempts, so
    they must NOT tick the per-PR exhaustion counter or trip the
    ``manual-required`` park — counting them parked a merge-ready PR after three
    no-op short-circuit successes (blocky PR #517). The artifact carries the PR
    number under ``pr`` or ``number``; an artifact with neither (legacy/loose
    shape) is treated as referring to the dispatch target.
    """
    if not outcome.success:
        return False
    for artifact in outcome.artifacts:
        if not isinstance(artifact, dict):
            continue
        if artifact.get("type") not in {"pr_merged", "stale_review_state"}:
            continue
        artifact_pr = artifact.get("pr", artifact.get("number", pr_number))
        if artifact_pr == pr_number:
            return True
    return False


class _CompletionHost(Protocol):
    """Orchestrator *behaviour* the :class:`CompletionProcessor` invokes.

    All shared session *state* now lives on :class:`SessionRuntime` (reached via
    ``self._runtime``); this Protocol is the narrow behaviour seam that remains so
    the cross-component methods resolve on the composition root without a circular
    import. ``_OrchestratorBase`` structurally satisfies it.
    """

    async def _safe_call(self, coro: Awaitable[object], label: str) -> None: ...

    async def _initiate_autonomous_stop(
        self,
        reason: str,
        *,
        arm_gate_only: bool = False,
        fire_natural_exit: bool = False,
        clear_pause_deadline: bool = False,
    ) -> None: ...

    async def _check_stagnation_escalation(self, state: OrchestratorState) -> bool: ...


class CompletionProcessor:
    """Process completed play tasks, update RL state, persist learnings.

    Stable services / collaborators (store, manager, executor, the 1a state
    collaborators, and the sibling components) are captured via the constructor;
    all shared session state (read or written) lives on the injected
    :class:`SessionRuntime`, and the cross-component behaviour methods resolve via
    the narrow :class:`_CompletionHost` behaviour seam.
    """

    def __init__(
        self,
        *,
        host: _CompletionHost,
        runtime: SessionRuntime,
        store: DataStore,
        manager: AgentManager,
        executor: PlayExecutor,
        session_id: str,
        repo_root: Path,
        main_repo: MainRepoGuard,
        velocity: VelocityTracker,
        recovery: RecoveryTracker,
        overrides: OverrideQueue,
        snapshots: SnapshotProjector,
        state_builder: StateBuilder,
        lifecycle: LifecycleController,
        drain: DrainController,
    ) -> None:
        self._host = host
        self._runtime = runtime
        self._store = store
        self._manager = manager
        self._executor = executor
        self._session_id = session_id
        self._repo_root = repo_root
        self._main_repo = main_repo
        self._velocity = velocity
        self._recovery = recovery
        self._overrides = overrides
        self._snapshots = snapshots
        self._state_builder = state_builder
        self._lifecycle = lifecycle
        self._drain = drain

        # Extracted collaborators — constructed from already-injected deps.
        self._issue_syncer = IssueSyncer(
            store=store,
            session_id=session_id,
            repo_root=repo_root,
            runtime=runtime,
        )
        self._terminal_park = TerminalParkPolicy(
            store=store,
            session_id=session_id,
            github_api=getattr(executor, "_github", None),
        )
        self._learnings_harvester = LearningsHarvester(
            repo_root=repo_root,
            learnings_cfg=runtime.cfg.learnings,
        )

    def _get_terminal_park(self) -> TerminalParkPolicy:
        """Return the cached ``TerminalParkPolicy``, constructing one lazily if absent.

        Test harnesses that bypass ``__init__`` (e.g. the ``_Harness`` in
        ``test_write_plan_unplannable_backoff.py``) only carry ``_store``,
        ``_session_id``, and ``_executor`` — they never set ``_terminal_park``
        directly. The lazy path builds a compatible instance from those same
        attrs so the harness continues to work without test edits.
        """
        park = getattr(self, "_terminal_park", None)
        if park is None:
            park = TerminalParkPolicy(
                store=self._store,
                session_id=self._session_id,
                github_api=getattr(getattr(self, "_executor", None), "_github", None),
            )
            self._terminal_park = park
        return park

    async def _mark_worktrees_stale_for_closed_prs(
        self,
        refetched_prs: list[object],
    ) -> None:
        """Delegation shim — actual logic lives in ``IssueSyncer``.

        Kept on ``CompletionProcessor`` so tests that call
        ``CompletionProcessor._mark_worktrees_stale_for_closed_prs(stub, prs)``
        (unbound) continue to work: the method is called with the stub as
        ``self``, and ``IssueSyncer._mark_worktrees_stale_for_closed_prs``
        only accesses ``self._runtime``, ``self._store``, and
        ``self._session_id`` — exactly the attrs those stubs expose.
        """
        await IssueSyncer._mark_worktrees_stale_for_closed_prs(self, refetched_prs)  # type: ignore[arg-type]

    async def _sweep_closed_pr_worktrees(self) -> None:
        """Delegation shim — actual logic lives in ``IssueSyncer``."""
        await IssueSyncer._sweep_closed_pr_worktrees(self)  # type: ignore[arg-type]

    async def _sweep_disk_pressure_worktrees(self) -> None:
        """Delegation shim — actual logic lives in ``IssueSyncer``."""
        await IssueSyncer._sweep_disk_pressure_worktrees(self)  # type: ignore[arg-type]

    async def _ensure_ssh_key_fresh(self) -> None:
        """Delegation shim — actual logic lives in ``IssueSyncer``."""
        await IssueSyncer._ensure_ssh_key_fresh(self)  # type: ignore[arg-type]

    async def harvest_completed(self) -> None:
        """Drain finished play tasks and process each via ``process_completion``.

        Invalidates the selection-digest cache when any task is harvested:
        a play completion (success or unhandled exception) is always worth a
        fresh selector pass, even when ``state.total_plays`` doesn't reflect
        it (e.g. tasks that raised before recording a row).
        """
        completed = [did for did, t in self._runtime.in_flight.items() if t.done()]
        for did in completed:
            task = self._runtime.in_flight.pop(did)
            self._runtime.completion_processing_count += 1
            self._runtime.completion_processing_idle.clear()
            try:
                await self.process_completion(did, task)
            finally:
                self._runtime.completion_processing_count -= 1
                if self._runtime.completion_processing_count <= 0:
                    self._runtime.completion_processing_count = 0
                    self._runtime.completion_processing_idle.set()
        if completed:
            self._runtime.last_selection_digest = None
            self._runtime.idle_streak = 0

    async def wait_for_in_flight(self, *, timeout: float) -> None:
        """``asyncio.wait`` with first-completed semantics on the in-flight set."""
        await asyncio.wait(
            self._runtime.in_flight.values(),
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

    async def check_main_repo_invariant(
        self,
        *,
        dispatch_id: str,
        play_type: PlayType,
        agent_id: str | None,
        agent_type: str | None,
    ) -> None:
        """Compare pre/post symbolic-ref for the main repo; warn + auto-restore.

        Runs at every play_completed boundary (success and failure). Reads
        the dispatch-time snapshot stored by ``Dispatcher._dispatch_play``
        and the current HEAD via ``check_main_repo_branch_mutated``.

        ``merge_pr`` advances the commit SHA via ``git merge --no-ff`` + ``git
        push`` but leaves the symbolic ref unchanged, so it produces zero
        false positives here (see tests/test_merge_pr_no_false_positive.py).
        """
        pre_play_ref = self._main_repo.pop_pre_play_branch(dispatch_id)
        if pre_play_ref is None:
            # No snapshot (or already detached pre-play); post-only state has no
            # signal — silent return.
            return
        try:
            mutated, post_ref, restore = await asyncio.to_thread(
                check_main_repo_branch_mutated,
                self._repo_root,
                pre_ref=pre_play_ref,
                default_branch=self._main_repo.default_branch,
            )
        except Exception as exc:
            _logger.warning(
                "main_repo_check_failed",
                phase="post_play",
                session_id=self._session_id,
                play_type=play_type.value,
                error=str(exc),
            )
            return
        if not mutated:
            return
        _logger.warning(
            "main_repo_branch_mutated",
            session_id=self._session_id,
            phase="post_play",
            play_type=play_type.value,
            agent_id=agent_id,
            agent_type=agent_type,
            pre_play_branch=pre_play_ref,
            post_play_branch=post_ref,
            default_branch=self._main_repo.default_branch,
        )
        if not restore.ok:
            _logger.error(
                "main_repo_auto_restore_failed",
                session_id=self._session_id,
                play_type=play_type.value,
                agent_id=agent_id,
                agent_type=agent_type,
                default_branch=self._main_repo.default_branch,
                post_play_branch=post_ref,
                reason=restore.stderr,
            )
            self._main_repo.dispatch_paused = True
            return
        _logger.info(
            "main_repo_branch_restored",
            session_id=self._session_id,
            play_type=play_type.value,
            agent_id=agent_id,
            agent_type=agent_type,
            default_branch=self._main_repo.default_branch,
        )

    async def process_completion(self, dispatch_id: str, task: asyncio.Task[PlayOutcome]) -> None:
        """Process a completed play task — RL experience, learnings, and state."""
        loaded = self._pop_completed_dispatch(dispatch_id, task)
        if loaded is None:
            return
        ctx, outcome = loaded
        completed_play_type = outcome.play_type
        state_before = ctx.state_at_dispatch

        await self._check_main_repo_after_completion(dispatch_id, ctx, outcome)
        match await self._handle_skipped_completion(outcome):
            case _CompletionVerdict.SKIPPED:
                return
            case _:
                pass

        self._record_completion_bookkeeping(ctx, outcome, completed_play_type)
        match await self._schedule_retry_if_requested(ctx, outcome, completed_play_type):
            case _CompletionVerdict.RETRIED:
                return
            case _:
                pass

        await self._record_unblock_attempt_if_needed(ctx, outcome, completed_play_type)
        self._record_merge_pr_repick_cooldown_if_needed(ctx, outcome, completed_play_type)
        await self._park_unplannable_issue_if_needed(ctx, outcome, completed_play_type)
        next_state = await self._run_completion_control_checks(outcome)
        await self._record_completion_experience(
            ctx,
            outcome,
            state_before,
            next_state,
            completed_play_type,
        )
        worktree_path_raw = ctx.params.extras.get("worktree_path")
        worktree_path = Path(worktree_path_raw) if isinstance(worktree_path_raw, str) else None
        await self._publish_completion_results(
            outcome, next_state, completed_play_type, worktree_path=worktree_path
        )
        await self._handle_end_session_completion(ctx, outcome, next_state, completed_play_type)

    def _pop_completed_dispatch(
        self, dispatch_id: str, task: asyncio.Task[PlayOutcome]
    ) -> tuple[_DispatchContext, PlayOutcome] | None:
        ctx = self._runtime.dispatch_ctx.pop(dispatch_id, None)
        if ctx is None:
            # Drop the pre-play snapshot even without a matching ctx so the dict
            # doesn't leak.
            self._main_repo.pop_pre_play_branch(dispatch_id)
            return None

        try:
            outcome: PlayOutcome = task.result()
        except asyncio.CancelledError:
            return None
        except Exception as exc:
            _logger.error(
                "play_task_failed",
                dispatch_id=dispatch_id,
                play_type=ctx.play_type.value,
                error=str(exc),
                exc_info=True,
            )
            return None
        return ctx, outcome

    async def _check_main_repo_after_completion(
        self,
        dispatch_id: str,
        ctx: _DispatchContext,
        outcome: PlayOutcome,
    ) -> None:
        completed_play_type = outcome.play_type

        # desktop-kqo5: main-repo symbolic-ref guard at every play boundary, incl.
        # skipped outcomes — a skip could still have left the repo poisoned if it
        # ran checkout commands before bailing.
        agent_type_str: str | None = None
        if outcome.agent_id is not None:
            handle = self._manager.handles.get(outcome.agent_id)
            if handle is not None:
                agent_type_str = handle.agent_type.value
        await self.check_main_repo_invariant(
            dispatch_id=dispatch_id,
            play_type=completed_play_type,
            agent_id=outcome.agent_id,
            agent_type=agent_type_str,
        )

        # desktop-kqo5 catch-22 fix: a successful RECONCILE_STATE is the way out of
        # a latched trunk-dispatch pause (it's exempt from the pause so it can run
        # while wedged). Re-verify the checkout is back on a clean default branch
        # (restore is idempotent) and only then lift the pause.
        if (
            completed_play_type == PlayType.RECONCILE_STATE
            and outcome.success
            and self._main_repo.dispatch_paused
        ):
            restore = await asyncio.to_thread(
                restore_default_branch, self._repo_root, self._main_repo.default_branch
            )
            self._main_repo.dispatch_paused = not restore.ok
            _logger.info(
                "main_repo_dispatch_pause_cleared"
                if restore.ok
                else "main_repo_dispatch_pause_persists",
                session_id=self._session_id,
                via="reconcile_state",
                default_branch=self._main_repo.default_branch,
                reason=None if restore.ok else restore.stderr,
            )

    async def _handle_skipped_completion(self, outcome: PlayOutcome) -> _CompletionVerdict:
        completed_play_type = outcome.play_type
        if getattr(outcome, "skipped", False) is True:
            # desktop-85ex: unify the play_skipped schema with the loop-tick
            # selector-None path — structured reason enum + stable event_source so
            # logs join cleanly. skip_category retained for dashboard back-compat.
            skip_category = outcome.skip_category
            executor_reason = skip_category_to_reason(skip_category)
            _logger.info(
                "play_skipped",
                session_id=self._session_id,
                play_type=completed_play_type.value,
                skip_category=skip_category,
                reason=executor_reason,
                event_source="executor",
                error=outcome.error,
            )
            # Track executor-time masked skips as a one-shot flag in
            # state.recent_executor_skip. No longer feeds the divergence window
            # (obs slot executor_skip_rate_recent_50): post eligibility refactor the
            # authority masks/confirms up front, so this path is vestigial. The
            # divergence signal now counts confirm-repicks (_record_selection_repicks).
            is_masked_skip = outcome.skip_category == "masked"
            self._velocity.set_recent_executor_skip(is_masked_skip)
            # No-op window for spin detection. The skip path returns early before
            # the loop-detection/stagnation checks, so the spin check runs HERE
            # (the write_impl↔reconcile spin lived on this path).
            self._runtime.recent_play_outcomes.append((True, completed_play_type.value))
            post_state = await self._state_builder.build_state()
            await self._host._safe_call(
                self._runtime.state_provider.on_state_update(post_state), "on_state_update_post"
            )
            # A skip is the canonical no-forward-progress tick (no agent dispatched)
            # — feed the monitor here, before this path returns early.
            await self.check_no_forward_progress(post_state, outcome)
            return _CompletionVerdict.SKIPPED
        return _CompletionVerdict.CONTINUE

    def _record_completion_bookkeeping(
        self,
        ctx: _DispatchContext,
        outcome: PlayOutcome,
        completed_play_type: PlayType,
    ) -> None:
        # Any completed (non-skipped) play clears the executor-skip flag. The
        # divergence window is fed from confirm-repicks, not completions, so
        # nothing is appended here.
        self._velocity.set_recent_executor_skip(False)
        self._runtime.recent_play_outcomes.append((False, completed_play_type.value))

        _logger.info(
            "play_completed",
            session_id=self._session_id,
            play_type=completed_play_type.value,
            success=outcome.success,
            error=outcome.error,
        )
        # desktop-65mq: surface bootstrap first-play failures explicitly so
        # operators don't infer them from a generic play_completed entry.
        if (
            ctx.override_kind == OverrideKind.BOOTSTRAP
            and completed_play_type in (PlayType.SEED_PROJECT, PlayType.CLEANUP)
            and not outcome.success
        ):
            _logger.warning(
                "bootstrap_first_play_failed",
                session_id=self._session_id,
                play_type=completed_play_type.value,
                error=outcome.error,
            )
        if outcome.play_id is not None:
            self._runtime.last_play_id = outcome.play_id
            # Record override-dispatched play_ids so compute_play_streaks ignores
            # them: bootstrap/user/retry bursts aren't PPO-collapse and must not
            # trigger loop_detected.
            if ctx.override_kind is not None:
                self._overrides.dispatched_play_ids.add(outcome.play_id)
            # Capture the just-completed play in memory so the next _build_state
            # sees it before the SQLite WAL write is visible to a fresh
            # get_play_history read — without this, same-tick instantiate_agent
            # pairs slipped past the cooldown mask (desktop-65bg).
            started_at_raw = ctx.params.extras.get("started_at")
            started_at = started_at_raw if isinstance(started_at_raw, str) else ""
            self._runtime.recent_play_completions.append(
                PlayRecord(
                    play_id=outcome.play_id,
                    session_id=self._session_id,
                    play_type=completed_play_type.value,
                    started_at=started_at,
                    ended_at=now_iso(),
                    success=outcome.success,
                    agent_id=outcome.agent_id,
                    dollar_cost=outcome.dollar_cost,
                    token_cost=outcome.token_cost,
                )
            )
            # Label shadow (desktop-quv9): a successful systematic_debugging applies
            # root-cause-found via gh CLI in the subprocess; the label lands on
            # GitHub at once but the cached snapshot only learns it on the next
            # refresh_issues poll, so PPO could re-select the same issue against the
            # stale snapshot. Shadowing the label makes _merge_recent_applied_labels
            # overlay it at the next state build so it drops from the debug candidates.
            if (
                outcome.success
                and completed_play_type == PlayType.SYSTEMATIC_DEBUGGING
                and isinstance(ctx.params.issue_number, int)
            ):
                self._runtime.recent_applied_labels.append(
                    (ctx.params.issue_number, ROOT_CAUSE_FOUND_LABEL)
                )

    async def _schedule_retry_if_requested(
        self,
        ctx: _DispatchContext,
        outcome: PlayOutcome,
        completed_play_type: PlayType,
    ) -> _CompletionVerdict:
        if outcome.retry_requested:
            claim_group_id_raw = ctx.params.extras.get("claim_group_id")
            if isinstance(claim_group_id_raw, str) and claim_group_id_raw:
                attempts = await self._store.get_work_claim_retry_attempts(
                    self._session_id, claim_group_id_raw
                )
                if attempts < 2 and outcome.play_id is not None:
                    replay = await self._store.get_dispatch_replay(
                        session_id=self._session_id,
                        claim_group_id=claim_group_id_raw,
                        play_id=outcome.play_id,
                    )
                    if replay is not None:
                        params_payload = json.loads(replay.params_json)
                        params_payload["extras"] = {
                            **dict(params_payload.get("extras") or {}),
                            "__retry_prompt": replay.prompt,
                        }
                        retry_params = PlayParams(**params_payload)
                        self._overrides.put_nowait(
                            OverrideEntry(
                                play_type=completed_play_type,
                                params=retry_params,
                                kind=OverrideKind.RETRY,
                            )
                        )
                        new_attempt = await self._store.increment_work_claim_retry(
                            self._session_id, claim_group_id_raw
                        )
                        _logger.info(
                            "play_retry_scheduled",
                            session_id=self._session_id,
                            play_type=completed_play_type.value,
                            claim_group_id=claim_group_id_raw,
                            attempt=new_attempt,
                        )
                        return _CompletionVerdict.RETRIED
                _logger.warning(
                    "play_retry_exhausted",
                    session_id=self._session_id,
                    play_type=completed_play_type.value,
                    claim_group_id=claim_group_id_raw,
                    attempts=attempts,
                )
                await self._store.finish_work_claim_group(
                    self._session_id,
                    claim_group_id_raw,
                    status="released",
                )
        return _CompletionVerdict.CONTINUE

    async def _record_unblock_attempt_if_needed(
        self,
        ctx: _DispatchContext,
        outcome: PlayOutcome,
        completed_play_type: PlayType,
    ) -> None:
        # Track per-PR unblock_pr ATTEMPTS so the resolver stops retrying
        # irresolvable PRs after _UNBLOCK_PR_EXHAUSTION_THRESHOLD. Count every
        # completion — a "successful" unblock can still leave the PR unblockable
        # (CI still red, new conflict). Counting only failures let stuck PRs absorb
        # dispatches forever (desktop-uwg); a real fix drops the PR from the
        # predicate so the counter never fires again.
        if completed_play_type == PlayType.UNBLOCK_PR and ctx.params.pr_number is not None:
            # A target blocked only by an unmerged sibling PR is not at fault — do
            # NOT count toward exhaustion or park it, else a stacked PR is wrongly
            # stamped manual-required after 3 dispatches that only awaited the sibling.
            if _outcome_blocked_by_sibling_pr(outcome):
                _logger.info(
                    "unblock_pr_blocked_by_sibling",
                    session_id=self._session_id,
                    pr_number=ctx.params.pr_number,
                )
                return
            # A dispatch that merged the target or cleared its sole stale
            # CHANGES_REQUESTED review is a win — never count or park it. Reset
            # prior failures so a later genuine block counts fresh (blocky PR #517).
            # Also clear the #312 repick cooldown: this dispatch just proved the
            # PR fine again, which the cooldown's own lazy rearm check (a live
            # ``mergeable`` re-check on the next resolve) would not necessarily
            # catch — e.g. a stale-review resolution never touched ``mergeable``.
            if _outcome_resolved_target_pr(outcome, ctx.params.pr_number):
                self._executor._resolver.reset_unblock_pr_failures(ctx.params.pr_number)
                self._executor._resolver.clear_pr_repick_cooldown(ctx.params.pr_number)
                _logger.info(
                    "unblock_pr_resolved_target",
                    session_id=self._session_id,
                    pr_number=ctx.params.pr_number,
                )
                return
            exhausted = self._executor._resolver.record_unblock_pr_failure(ctx.params.pr_number)
            # Fast-path (#6): a failure naming a human/CI-infra blocker can't be
            # fixed by re-dispatching, so mark manual-required now instead of
            # burning the attempt budget. Exhaustion still backstops ambiguous cases.
            error_text = (outcome.error or "").lower()
            terminal = any(m in error_text for m in _UNBLOCK_MANUAL_REQUIRED_MARKERS)
            if exhausted or terminal:
                await self._host._safe_call(
                    self.mark_pr_manual_required(ctx.params.pr_number),
                    "mark_pr_manual_required",
                )
            # #312: a merge-conflict failure is not permanently unfixable (the
            # base branch may move and a later rebase succeed), but it is
            # provably not worth re-attempting THIS tick — arm a short repick
            # cooldown so the PPO doesn't immediately re-pick the same PR.
            # threshold=1 in PR_REPICK_COOLDOWN_SPEC means this fires on the
            # very first such failure, well before the 3-attempt exhaustion
            # counter above would exclude it. rearmable=True: the PR's live
            # ``mergeable`` field is free to re-check every resolve, so the
            # cooldown clears the instant a rebase lands (see
            # PlayCandidateService._rearm_pr_repick_cooldown).
            elif any(m in error_text for m in _UNBLOCK_PR_REPICK_COOLDOWN_MARKERS):
                self._executor._resolver.record_pr_repick_cooldown(
                    ctx.params.pr_number,
                    ctx.state_at_dispatch.total_plays,
                    rearmable=True,
                )

    def _record_merge_pr_repick_cooldown_if_needed(
        self,
        ctx: _DispatchContext,
        outcome: PlayOutcome,
        completed_play_type: PlayType,
    ) -> None:
        """Arm the fast per-PR repick cooldown on a merge_pr ``dirty_trunk`` failure (#312).

        Sibling to ``_record_unblock_attempt_if_needed``'s merge_conflicts
        arm, and separate from ``_handle_merge_pr_outcome``'s SESSION-GLOBAL
        same-cause wedge counter (#330, untouched here — that mechanism only
        counts a specific root-untracked-path pathology toward unmasking
        END_SESSION, it carries no per-PR memory at all). This is per-PR: a
        ``dirty_trunk`` failure on PR #42 means re-picking #42 immediately is
        wasted dispatch cost regardless of which untracked-path pathology
        caused it, so this matches on the same ``"dirty_trunk"`` substring
        ``_handle_merge_pr_outcome`` checks but does not require the
        root-untracked refinement that guards the wedge counter's escalation.

        rearmable=False: unlike unblock_pr's merge_conflicts (whose live
        ``mergeable`` field is free to re-check every resolve), there is no
        equivalently cheap live "trunk is clean now" signal available to
        ``PlayCandidateService`` — it rides out the full cooldown window,
        mirroring issue_pickup's non-rearmable timeout/crash case (#222).
        """
        if (
            completed_play_type != PlayType.MERGE_PR
            or outcome.success
            or not isinstance(ctx.params.pr_number, int)
        ):
            return
        error_text = (outcome.error or "").lower()
        if "dirty_trunk" not in error_text:
            return
        self._executor._resolver.record_pr_repick_cooldown(
            ctx.params.pr_number,
            ctx.state_at_dispatch.total_plays,
            rearmable=False,
        )

    async def _park_unplannable_issue_if_needed(
        self,
        ctx: _DispatchContext,
        outcome: PlayOutcome,
        completed_play_type: PlayType,
    ) -> None:
        # #458: a write_implementation_plan that fails because the issue is
        # un-plannable must not be re-selected — the priority sort re-picks it, the
        # agent no-ops the same way, and the session spams comments. Park it with
        # NEEDS_HUMAN_LABEL so _base_issue_available drops it until a human clears it.
        if (
            completed_play_type != PlayType.WRITE_IMPLEMENTATION_PLAN
            or outcome.success
            or not isinstance(ctx.params.issue_number, int)
        ):
            return
        error_text = (outcome.error or "").lower()
        if not any(m in error_text for m in _WRITE_PLAN_UNPLANNABLE_MARKERS):
            return
        await self._host._safe_call(
            self.mark_issue_needs_human(ctx.params.issue_number),
            "mark_issue_needs_human",
        )
        # Shadow the label so the next state build excludes the issue before the
        # gh CLI write is visible to a fresh get_open_issues read (same WAL/refresh
        # lag as the ROOT_CAUSE_FOUND_LABEL shadow above).
        self._runtime.recent_applied_labels.append((ctx.params.issue_number, NEEDS_HUMAN_LABEL))

    async def mark_issue_needs_human(self, issue_number: int) -> None:
        """Park an un-plannable issue behind NEEDS_HUMAN_LABEL (store + GitHub)."""
        await self._get_terminal_park().mark_issue_needs_human(issue_number)

    async def _run_completion_control_checks(self, outcome: PlayOutcome) -> OrchestratorState:
        next_state = await self._state_builder.build_state()
        await self._snapshots.record_trajectory_snapshot(
            outcome, next_state, safe_call=self._host._safe_call
        )
        next_state = await self._lifecycle.begin_budget_reserve_drain_if_needed(next_state)
        should_stop, reason = self._lifecycle.should_terminate(next_state)
        if should_stop:
            _logger.info(
                "loop_terminating",
                reason=reason,
                session_id=self._session_id,
            )
            if reason is not None and reason != "stop_requested":
                self._runtime.natural_exit_reason = reason
            self._runtime.stop_requested = True
        elif reason is not None and self._runtime.pause_event.is_set():
            await self._lifecycle.pause_with_reason(reason)

        await self.check_no_forward_progress(next_state, outcome)
        if (
            await self._host._check_stagnation_escalation(next_state)
            and self._runtime.pause_event.is_set()
        ):
            await self._lifecycle.pause_with_reason("stagnation")
        self._runtime.feedback_cadence_plays_since_ack += 1
        await self._lifecycle.pause_for_feedback_cadence_if_due()
        return next_state

    async def _record_completion_experience(
        self,
        ctx: _DispatchContext,
        outcome: PlayOutcome,
        state_before: OrchestratorState,
        next_state: OrchestratorState,
        completed_play_type: PlayType,
    ) -> None:
        # RL experience collection and policy update. The crash-prone tail
        # (snapshots, reward, encoding, persist, policy update, checkpoint) lives in
        # the guarded ExperienceRecorder — a failure there degrades to a skipped
        # record, not a run_until_idle crash (sidecar_orchestrator_run_failed). Only
        # cheap bookkeeping (velocity events, done) stays inline.
        if (
            self._runtime.experience_recorder is not None
            and isinstance(self._runtime.selector, _ppo_selector_cls())
            and self._runtime.metrics is not None
        ):
            from agentshore.rl.selector import _PendingStep

            done = (
                completed_play_type == PlayType.END_SESSION
                or self._runtime.stop_requested
                or (
                    next_state.budget is not None
                    and next_state.budget.enabled
                    and budget_reserve_reached(
                        spent=next_state.budget.spent,
                        total_budget=next_state.budget.total_budget,
                    )
                )
            )

            # Update velocity before the recorder snapshots so ctx_after sees it.
            play_id_for_velocity = next_state.total_plays
            if outcome.success:
                if completed_play_type == PlayType.MERGE_PR:
                    self._velocity.record_velocity_event(play_id_for_velocity, "pr_merged")
                elif completed_play_type == PlayType.ISSUE_PICKUP:
                    closed_issue = isinstance(outcome.artifacts, list) and any(
                        isinstance(a, dict) and a.get("closed_issue") for a in outcome.artifacts
                    )
                    if closed_issue:
                        self._velocity.record_velocity_event(play_id_for_velocity, "issue_closed")
                elif completed_play_type == PlayType.CLEANUP:
                    pass  # cleanup does not reset velocity
            if outcome.agent_id is not None:
                agent_snap = next(
                    (a for a in next_state.agents if a.agent_id == outcome.agent_id), None
                )
                if agent_snap is not None:
                    self._velocity.record_agent_type(agent_snap.agent_type.value)

            raw_pending = ctx.pending_step
            pending_step: _PendingStep | None = (
                raw_pending if isinstance(raw_pending, _PendingStep) else None
            )

            await self._runtime.experience_recorder.record_and_update(
                state_before=state_before,
                next_state=next_state,
                outcome=outcome,
                pending_step=pending_step,
                done=done,
            )

    async def _publish_completion_results(
        self,
        outcome: PlayOutcome,
        next_state: OrchestratorState,
        completed_play_type: PlayType,
        *,
        worktree_path: Path | None = None,
    ) -> None:
        # Refresh issue cache after plays that modify issues. QA / design audit can
        # create follow-up issues even on partial results, so they always refresh.
        refresh_on_success = (
            PlayType.SEED_PROJECT,
            PlayType.GROOM_BACKLOG,
            PlayType.ISSUE_PICKUP,
            PlayType.MERGE_PR,
            # unblock_pr can merge a target or dismiss a stale review; re-read
            # promptly so the cache reflects it instead of waiting ISSUE_REFRESH_INTERVAL.
            PlayType.UNBLOCK_PR,
            PlayType.CODE_REVIEW,
            PlayType.WRITE_IMPLEMENTATION_PLAN,
            PlayType.REFINE_TASK_BREAKDOWN,
        )
        # desktop-rla8: CLEANUP / RECONCILE_STATE always trigger a full re-sync via
        # _FULL_ISSUE_SYNC_PLAYS (belt-and-suspenders for issues whose updated_at
        # doesn't move — deletions, transfers), success or not.
        refresh_always = (
            PlayType.RUN_QA,
            PlayType.DESIGN_AUDIT,
            PlayType.CLEANUP,
            PlayType.RECONCILE_STATE,
            PlayType.PRUNE,
        )
        if completed_play_type in refresh_always or (
            completed_play_type in refresh_on_success and outcome.success
        ):
            # Force a full sync when issue_pickup finds an issue already CLOSED on
            # GitHub — the incremental since= cursor has been seen missing
            # close-state transitions, leaving the cache stale.
            force_full_sync = (
                completed_play_type == PlayType.ISSUE_PICKUP
                and outcome.success
                and _outcome_signals_already_closed(outcome)
            )
            if force_full_sync:
                _logger.info(
                    "issue_pickup_detected_phantom_open",
                    play_id=outcome.play_id,
                    session_id=self._session_id,
                )
            await self._host._safe_call(
                self.refresh_issues(
                    completing_play=completed_play_type,
                    force_full_sync=force_full_sync,
                ),
                "refresh_issues",
            )

        # Learnings: reinforce on success; harvest new entries after consolidation
        if self._runtime.cfg.learnings.enabled and outcome.play_id is not None:
            await self._host._safe_call(
                self.update_learnings(outcome, completed_play_type),
                "update_learnings",
            )

        # Fork-guard: detect cross-fork PRs or non-origin remotes in the worktree.
        # Detect-and-log only — never blocks completion or mutates any state.
        has_artifacts = bool(outcome.artifacts)
        if has_artifacts or worktree_path is not None:
            await self._host._safe_call(
                self._check_fork_guard(outcome, worktree_path, completed_play_type),
                "fork_guard",
            )

        await self._host._safe_call(
            self._runtime.state_provider.on_play_completed(outcome), "on_play_completed"
        )
        # Book the take_break verdict BEFORE the recovery re-enqueue below (#365).
        # ``_retire_or_recover_errored_agent`` consults the consecutive-failure
        # counter to decide whether another break is still worth enqueueing; if
        # the counter were still one short (incremented after), the failure that
        # *reaches* the limit would enqueue one more identical break before
        # exhaustion was recorded — the unbounded 30-minute cycle in #365.
        if completed_play_type == PlayType.TAKE_BREAK:
            self._handle_take_break_outcome(outcome)
        # The orchestrator owns final lifecycle publication. The executor may
        # update handles, but consumers get the terminal status event here
        # after persistence/reward side effects complete.
        if outcome.agent_id and outcome.agent_id in self._manager.handles:
            handle_status = getattr(
                self._manager.handles[outcome.agent_id],
                "status",
                AgentStatus.IDLE if outcome.success else AgentStatus.ERROR,
            )
            final_status = (
                handle_status
                if isinstance(handle_status, AgentStatus)
                else AgentStatus.IDLE
                if outcome.success
                else AgentStatus.ERROR
            )
            await self._host._safe_call(
                self._runtime.state_provider.on_agent_changed(outcome.agent_id, final_status),
                "on_agent_changed_final",
            )
            await self._retire_or_recover_errored_agent(outcome.agent_id, final_status)
        if completed_play_type == PlayType.MERGE_PR:
            await self._handle_merge_pr_outcome(outcome)
        if (
            completed_play_type == PlayType.END_AGENT
            and outcome.success
            and outcome.agent_id is not None
        ):
            # END_AGENT cleared the slot; drop the break-recovery count so a reused
            # agent id doesn't inherit an elevated (recovery-exhausted) counter.
            self._recovery.clear_break_failures(outcome.agent_id)
        # Second state_update after play completes so consumers see the fresh result
        post_state = await self._state_builder.build_state()
        await self._host._safe_call(
            self._runtime.state_provider.on_state_update(post_state), "on_state_update_post"
        )

    async def _handle_end_session_completion(
        self,
        ctx: _DispatchContext,
        outcome: PlayOutcome,
        next_state: OrchestratorState,
        completed_play_type: PlayType,
    ) -> None:
        if completed_play_type == PlayType.END_SESSION:
            if not outcome.success:
                self._runtime.end_session_dispatch_started = False
                _logger.warning(
                    "end_session_play_failed_before_drain",
                    play_id=outcome.play_id,
                    session_id=self._session_id,
                    error=outcome.error,
                )
                return
            drain_reason = ctx.params.reason or _str_extra(ctx.params, "drain_reason")
            if drain_reason is None:
                drain_reason = "ppo_selected"
            if (
                isinstance(self._runtime.selector, _ppo_selector_cls())
                and len(self._runtime.selector.buffer) > 0
            ):
                await self._runtime.selector.update_policy(next_state_value=0.0)
                final_state = await self._state_builder.build_state()
                weights_dir = self._repo_root / ".agentshore" / "weights"
                await self._runtime.selector.save_checkpoint(
                    self._store, self._session_id, weights_dir, final_state.total_plays
                )
            _logger.info(
                "end_session_play_initiated_shutdown",
                reason=drain_reason,
                source=_str_extra(ctx.params, "shutdown_source") or "selector",
                play_id=outcome.play_id,
                session_id=self._session_id,
            )
            await self._drain.begin_drain(drain_reason)

    async def mark_pr_manual_required(self, pr_number: int) -> None:
        """Persist a terminal manual gate after repeated unblock_pr failures."""
        await self._get_terminal_park().mark_pr_manual_required(pr_number)

    async def _retire_or_recover_errored_agent(
        self,
        agent_id: str,
        final_status: AgentStatus,
    ) -> None:
        """On play completion, recover an errored agent — unless we're draining.

        During wind-down recovery (take_break) is masked, so a recoverable-ERROR
        agent would never reach IDLE/TERMINATED and would wedge ``drain_complete``
        (#30). Retire it immediately instead, and skip the doomed rate-limit
        recovery enqueue (also kills the misleading ``rate_limit_recovery_enqueued``
        telemetry, #23). Outside drain, fall back to the normal recovery path.
        """
        draining = self._runtime.draining or self._runtime.stop_requested
        if draining and final_status == AgentStatus.ERROR:
            # force=True: session is winding down; in-flight tasks already cancelled.
            await self._host._safe_call(
                self._manager.clear(agent_id, force=True), "drain_clear_errored_agent"
            )
            return
        self._maybe_enqueue_error_recovery(agent_id, final_status)

    def _maybe_enqueue_error_recovery(
        self,
        agent_id: str,
        final_status: AgentStatus,
    ) -> None:
        """Enqueue a take_break override for recoverable agent errors."""
        self._recovery.maybe_enqueue_error_recovery(
            agent_id,
            final_status,
            handles=self._manager.handles,
            overrides=self._overrides,
            session_id=self._session_id,
        )

    def _handle_take_break_outcome(self, outcome: PlayOutcome) -> None:
        """Track consecutive take_break failures for END_AGENT eligibility."""

        agent_id = outcome.agent_id
        if agent_id is None:
            return
        # Clear the recovery latches on any take_break completion so the next
        # ERROR transition can re-arm the appropriate override (the break could
        # have come from any path).
        self._recovery.clear_rate_limit_enqueued(agent_id)
        self._recovery.clear_unknown_error_enqueued(agent_id)
        self._recovery.clear_noop_enqueued(agent_id)
        if outcome.success:
            self._recovery.clear_break_failures(agent_id)
            return
        failures = self._recovery.record_break_failure(agent_id)
        if failures < BREAK_RECOVERY_FAILURE_LIMIT:
            _logger.info(
                "break_recovery_failed",
                session_id=self._session_id,
                agent_id=agent_id,
                consecutive_failures=failures,
                limit=BREAK_RECOVERY_FAILURE_LIMIT,
            )
            return
        # Leave the counter elevated so the core tick can unmask END_AGENT.
        _logger.warning(
            "break_recovery_exhausted",
            session_id=self._session_id,
            agent_id=agent_id,
            consecutive_failures=failures,
            limit=BREAK_RECOVERY_FAILURE_LIMIT,
        )

    async def _handle_merge_pr_outcome(self, outcome: PlayOutcome) -> None:
        """Track consecutive same-cause merge_pr ``dirty_trunk`` failures (#330).

        A successful merge_pr clears the counter. A ``dirty_trunk`` failure
        blocked by root-level untracked path(s) a deterministic reclaim sweep
        correctly leaves alone (real user WIP, or predates every known play
        window) records those paths as the failure's cause; once the same
        cause repeats past the guard's threshold, ``state.trunk_wedged``
        unmasks END_SESSION for the PPO (see ``rl/eligibility.py``) — this
        method never *forces* a session action, only feeds the counter. Once
        wedged, it additionally escalates (``_escalate_trunk_wedge``):
        force-quarantining the offending path(s) and emitting a needs-human
        signal, so the wedge has a resolution path instead of only a give-up
        option.
        """
        if outcome.success:
            self._main_repo.clear_dirty_trunk_failures()
            return
        error_text = (outcome.error or "").lower()
        if "dirty_trunk" not in error_text:
            return
        entries = await asyncio.to_thread(collect_dirty_trunk_paths, self._repo_root)
        root_untracked = sorted(e.path for e in entries if e.status == "??" and "/" not in e.path)
        if not root_untracked:
            # Not this pathology (tracked collision or subdirectory debris) —
            # leave to other handling.
            return
        self._main_repo.record_dirty_trunk_failure("|".join(root_untracked))
        if self._main_repo.is_trunk_wedged():
            await self._escalate_trunk_wedge(root_untracked, play_id=outcome.play_id)

    async def _escalate_trunk_wedge(
        self, root_untracked: list[str], *, play_id: int | None
    ) -> None:
        """Resolve + surface a wedged trunk once the same-cause streak hits threshold (#330).

        ``MainRepoGuard`` documents that it "never forces a session action" —
        ``is_trunk_wedged()`` only unmasks END_SESSION for the PPO to weigh.
        That principle is preserved here too: this method does not stop the
        session, block dispatch, or force any play. What it *does* do is act
        on strong evidence the guard itself can't see — three consecutive
        ``dirty_trunk`` failures blocked by the exact same root path(s) means
        a deterministic reclaim sweep's conservative "might be real user WIP"
        assumption has been falsified for this file. Reclaim normally requires
        mtime-window attribution to a closed trunk-scoped play
        (``attribute_orphan_artifacts``); an unattributable file never clears
        that bar and would otherwise wedge forever with no resolution path
        beyond an operator noticing the session ended.

        Two independent actions, both best-effort and non-blocking:

        1. Force-quarantine the recorded path(s) into
           ``.agentshore/reclaimed/wedge/`` (move, never delete) so ``git
           status`` goes clean and the next ``merge_pr`` attempt can succeed
           on its own — no operator action required for the common case.
        2. Emit a ``trunk_wedge_needs_human`` warning (the same log-event
           surface pattern as ``pr_manual_required`` / ``issue_needs_human``
           in ``terminal_park.py``) regardless of whether quarantine fully
           succeeded, so the operator always gets a clear, visible signal
           naming the exact path(s) that wedged the trunk.
        """
        quarantined = await asyncio.to_thread(
            force_quarantine_wedge_paths, self._repo_root, root_untracked
        )
        store = getattr(self, "_store", None)
        if store is not None:
            for rel in quarantined:
                try:
                    await store.record_external_mutation(
                        ExternalMutationRecord(
                            session_id=self._session_id,
                            play_id=play_id,
                            idempotency_key=f"wedge_quarantine:{self._session_id}:{rel}:{now_iso()}",
                            mutation_type="trunk_artifact_wedge_quarantine",
                            target=rel,
                            status="wedge_quarantined",
                            created_at=now_iso(),
                        )
                    )
                except Exception as exc:  # noqa: BLE001 — audit trail is best-effort
                    _logger.warning(
                        "trunk_wedge_quarantine_mutation_record_failed",
                        session_id=self._session_id,
                        path=rel,
                        error=str(exc),
                    )
        _logger.warning(
            "trunk_wedge_needs_human",
            session_id=self._session_id,
            blocking_paths=root_untracked,
            quarantined_paths=quarantined,
            unresolved_paths=sorted(set(root_untracked) - set(quarantined)),
        )

    async def on_crash(self, agent_id: str, return_code: int) -> None:
        """Log crash; leave handle in ERROR state. No auto-recovery in Phase 2."""
        _logger.error(
            "agent_crashed",
            session_id=self._session_id,
            agent_id=agent_id,
            return_code=return_code,
        )

        await self._host._safe_call(
            self._runtime.state_provider.on_agent_changed(agent_id, AgentStatus.ERROR),
            "on_agent_changed",
        )

    async def on_context_pressure(self, agent_id: str, ratio: float) -> None:
        """Annotate pressure hint; do not auto-trigger COMPACT/FRESH_START in Phase 2."""
        _logger.info(
            "context_pressure",
            session_id=self._session_id,
            agent_id=agent_id,
            ratio=ratio,
        )
        self._runtime.context_pressure_hints[agent_id] = ratio

        await self._host._safe_call(
            self._runtime.state_provider.on_agent_changed(agent_id, AgentStatus.BUSY),
            "on_agent_changed",
        )

    async def update_learnings(self, outcome: PlayOutcome, play_type: PlayType) -> None:
        """Reinforce learnings on success; harvest new entries after GROOM_BACKLOG."""
        await self._learnings_harvester.update_learnings(outcome, play_type)

    async def _check_fork_guard(
        self,
        outcome: PlayOutcome,
        worktree_path: Path | None,
        completed_play_type: PlayType,
    ) -> None:
        """Detect cross-fork PRs and non-origin remotes; log findings, never abort.

        Best-effort: derives origin_owner from the main repo's origin remote.
        Returns silently when the origin cannot be parsed.
        """
        from agentshore import command
        from agentshore.core.fork_guard import (
            detect_cross_fork_pr_artifacts,
            detect_non_origin_remotes,
            parse_origin_owner,
        )

        # Derive origin_owner from the main repo's remote.origin.url (best-effort).
        origin_owner: str | None = None
        try:
            remote_result = await command.git(
                "config", "--get", "remote.origin.url", cwd=self._repo_root
            )
            if remote_result.returncode == 0 and remote_result.stdout.strip():
                origin_owner = parse_origin_owner(remote_result.stdout.strip())
        except Exception:
            pass

        if origin_owner is None:
            # Can't derive a baseline owner; skip cross-fork PR check.
            pass
        else:
            for finding in detect_cross_fork_pr_artifacts(outcome.artifacts, origin_owner):
                _logger.warning(
                    "cross_fork_pr_detected",
                    play_id=outcome.play_id,
                    play_type=completed_play_type.value,
                    session_id=self._session_id,
                    detail=finding.detail,
                )

        if worktree_path is not None and worktree_path.exists():
            for finding in await detect_non_origin_remotes(worktree_path):
                _logger.warning(
                    "non_origin_remote_detected",
                    play_id=outcome.play_id,
                    play_type=completed_play_type.value,
                    session_id=self._session_id,
                    detail=finding.detail,
                )

    async def check_no_forward_progress(
        self, state: OrchestratorState, outcome: PlayOutcome
    ) -> None:
        """Forward-progress backstop: drain after N consecutive dead ticks.

        A tick makes forward progress if a play was dispatched to an agent, an
        agent is busy, or the beads/GitHub graph fingerprint changed (an issue/
        PR/beads-task created, closed, or advanced). N consecutive no-progress
        ticks drain the session directly. This is the single autonomous-stop
        signal — it replaces the same-type-streak loop-detector and the no-op-
        spin detector, which watched play activity rather than project progress
        and so missed an interleaved write_impl↔refine churn. Pure backstop: it
        never influences which play the policy selects.
        """
        monitor = self._runtime.progress_monitor
        if monitor is None:
            return
        if self._runtime.draining or self._runtime.stop_requested:
            return
        graph = state.graph
        fingerprint = (
            round(graph.global_closure_ratio, 4) if graph is not None else 0.0,
            graph.tasks_ready if graph is not None else 0,
            len(state.open_issues),
            sum(1 for pr in state.pull_requests if pr.state.upper() == "OPEN"),
            sum(1 for pr in state.pull_requests if pr.state.upper() == "MERGED"),
        )
        dispatched = not outcome.skipped and outcome.agent_id is not None
        any_busy = any(a.status == AgentStatus.BUSY for a in state.agents)
        tripped = monitor.record_tick(
            dispatched_to_agent=dispatched,
            any_agent_busy=any_busy,
            fingerprint=fingerprint,
        )
        if not tripped:
            return
        _logger.warning(
            "no_forward_progress",
            session_id=self._session_id,
            no_progress_ticks=monitor.no_progress_ticks,
            limit=monitor.limit,
            note=(
                "no agent dispatch, all agents idle, and no beads/GitHub change "
                f"for {monitor.limit} consecutive ticks — draining the stalled session"
            ),
        )
        await self._host._initiate_autonomous_stop("no_forward_progress", fire_natural_exit=True)

    async def refresh_issues(
        self,
        completing_play: PlayType | None = None,
        *,
        force_full_sync: bool = False,
    ) -> None:
        """Re-fetch GitHub issues and update the cache.

        Two modes (desktop-rla8):

        - **Full sync**: a complete paginated sweep of all issues. Triggered
          when the completing play is in ``_FULL_ISSUE_SYNC_PLAYS``
          (``seed_project``, ``cleanup``, ``reconcile_state``, ``prune``),
          when ``force_full_sync`` is True (caller has out-of-band evidence
          the incremental cursor is missing a state transition), or when no
          ``last_issue_sync_at`` cursor exists yet. Catches deletions and
          repo transfers, which don't bump ``updated_at`` and so are
          invisible to incremental sync.
        - **Incremental sync**: a ``since=<last_sync_at>`` query that
          typically returns 0–5 changed issues per call. The default.

        For pull requests, the open-only fetch is followed by a "missing PR"
        sweep: any locally-cached open PR that did not appear in the fresh
        open-list has likely transitioned to MERGED or CLOSED on GitHub.
        Re-fetching those by number via ``state="all"`` lets the cache pick
        up the new state.
        """
        await self._issue_syncer.refresh_issues(
            completing_play,
            force_full_sync=force_full_sync,
            full_issue_sync_plays=_FULL_ISSUE_SYNC_PLAYS,
        )
