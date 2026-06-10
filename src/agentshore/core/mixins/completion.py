"""Play-completion harvesting, RL experience persistence, and learnings update."""

from __future__ import annotations

import asyncio
import enum
import json
import re
from typing import TYPE_CHECKING, Protocol

import aiosqlite

from agentshore.budget import budget_reserve_reached
from agentshore.core.git_safety import check_main_repo_branch_mutated, restore_default_branch
from agentshore.core.helpers import (
    _logger,
    _ppo_selector_cls,
    _str_extra,
)
from agentshore.core.recovery_tracker import BREAK_RECOVERY_FAILURE_LIMIT
from agentshore.data.store import PlayRecord, PullRequestRecord
from agentshore.errors import ErrorClass
from agentshore.github.labels import (
    MANUAL_REQUIRED_LABEL,
    ROOT_CAUSE_FOUND_LABEL,
)
from agentshore.plays.base import PlayParams
from agentshore.plays.override import OverrideEntry, OverrideKind
from agentshore.state import AgentStatus, PlaySkipReason, PlayType
from agentshore.utils import now_iso

if TYPE_CHECKING:
    import collections
    from collections.abc import Awaitable
    from pathlib import Path

    from agentshore.agents.manager import AgentManager
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.config import RuntimeConfig
    from agentshore.core.context import _DispatchContext
    from agentshore.core.experience_recorder import ExperienceRecorder
    from agentshore.core.main_repo_guard import MainRepoGuard
    from agentshore.core.mixins.drain import DrainController
    from agentshore.core.mixins.lifecycle import LifecycleController
    from agentshore.core.mixins.snapshots import SnapshotProjector
    from agentshore.core.mixins.state import StateBuilder
    from agentshore.core.override_queue import OverrideQueue
    from agentshore.core.progress_monitor import ForwardProgressMonitor
    from agentshore.core.recovery_tracker import RecoveryTracker
    from agentshore.core.velocity_tracker import VelocityTracker
    from agentshore.data.store import DataStore
    from agentshore.plays.executor import PlayExecutor
    from agentshore.plays.selector import PlaySelector
    from agentshore.rl.metrics import MetricsEngine
    from agentshore.state import (
        OrchestratorState,
        PlayOutcome,
        StateProvider,
    )


DEFAULT_LEARNING_CONFIDENCE = 0.5


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


# Error classes that trigger loop-produced take_break overrides. Crash, auth,
# invalid-model, and timeout classes are intentionally excluded.
_RATE_LIMIT_RECOVERY_ERROR_CLASSES: frozenset[ErrorClass] = frozenset({ErrorClass.RATE_LIMIT})
_UNKNOWN_ERROR_RECOVERY_ERROR_CLASSES: frozenset[ErrorClass] = frozenset(
    {ErrorClass.UNKNOWN, ErrorClass.CODEX_ROLLOUT, ErrorClass.TRANSIENT_NETWORK}
)

# Substrings in an unblock_pr failure that mean the PR cannot be unblocked by an
# agent — it needs a human maintainer or CI/infra change. Matching any marks the
# PR manual-required on the FIRST such failure (#6), instead of waiting for the
# attempt-count exhaustion threshold, so the orchestrator stops re-dispatching
# expensive agents at a permanently-blocked PR. Transient blockers (CI pending,
# resolvable merge conflicts) intentionally do NOT match and stay retryable.
_UNBLOCK_MANUAL_REQUIRED_MARKERS: tuple[str, ...] = (
    "forbidden by skill policy",
    "ci-change",
    "human maintainer",
    "manual maintainer",
    "not fixable in code",
    "infrastructure failures",
    "external ci",
    "ci config or infrastructure",
)

# Used by ``refresh_issues`` — declared module-level so renaming inside the
# function body doesn't drift them away from the constants the original
# monolithic ``core.py`` referenced.
_DUPLICATE_BEAD_TITLE_RE = re.compile(r"^Duplicate bead", re.IGNORECASE)
_PR_LIMIT = 50

# Plays that should trigger a *full* paginated re-sync of GitHub issues, vs.
# the incremental ``since=`` sync used for everything else. Deletions and
# repo transfers don't bump ``updated_at`` and so are invisible to incremental
# sync — these plays act as the belt-and-suspenders that catches them.
_FULL_ISSUE_SYNC_PLAYS: frozenset[PlayType] = frozenset(
    {
        PlayType.SEED_PROJECT,
        PlayType.CLEANUP,
        PlayType.RECONCILE_STATE,
        PlayType.PRUNE,
    }
)

# Substring signatures that mean "the agent ran issue_pickup, looked at the
# real GH state, and discovered the issue was already CLOSED while our cache
# still listed it open." GitHub's incremental ``since=`` query has been
# observed missing close-state transitions for 30+ refresh cycles, leaving
# the orchestrator burning $0.10–0.20 per phantom pickup. Detecting this
# signal forces a paginated full sync on the next refresh so the cache
# self-heals (observed 2026-05-28 session 08a948ed, issue #966).
_ALREADY_CLOSED_SIGNATURES: tuple[str, ...] = (
    "is already closed",
    "already CLOSED",
    "already closed",
)


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
    import json as _json

    try:
        serialised = _json.dumps(outcome.artifacts, default=str)
    except (TypeError, ValueError):
        return False
    return any(sig in serialised for sig in _ALREADY_CLOSED_SIGNATURES)


class _CompletionHost(Protocol):
    """Orchestrator runtime/control state read OR written live by :class:`CompletionProcessor`.

    These members are accessed fresh via ``self._host.<attr>`` on every call so
    SIGHUP config swaps (``_cfg``) and per-tick mutation (in-flight maps,
    completion-processing latches, pause event, recent-completion shadows,
    bootstrap-assigned collaborators) are always current — never captured at
    construction. Fields the processor *writes* (``_completion_processing_count``,
    ``_last_selection_digest``, ``_idle_streak``, ``_last_play_id``,
    ``_natural_exit_reason``, ``_stop_requested``,
    ``_feedback_cadence_plays_since_ack``) are declared as plain annotated
    attributes (not read-only ``@property``) so the assignments type-check.
    ``_OrchestratorBase`` structurally satisfies this Protocol; the cross-component
    methods (``_safe_call``, ``_initiate_autonomous_stop``,
    ``_check_stagnation_escalation``) are resolved live on the composition root.
    """

    # --- written by the processor ------------------------------------------
    _completion_processing_count: int
    _last_selection_digest: bytes | None
    _idle_streak: int
    _last_play_id: int | None
    _natural_exit_reason: str | None
    _stop_requested: bool
    _feedback_cadence_plays_since_ack: int
    # --- read by the processor ---------------------------------------------
    _cfg: RuntimeConfig
    _selector: PlaySelector | None
    _state_provider: StateProvider
    _in_flight: dict[str, asyncio.Task[PlayOutcome]]
    _dispatch_ctx: dict[str, _DispatchContext]
    _completion_processing_idle: asyncio.Event
    _metrics: MetricsEngine | None
    _pause_event: asyncio.Event
    _last_refresh_time: float
    _end_session_dispatch_started: bool
    context_pressure_hints: dict[str, float]
    _recent_play_outcomes: collections.deque[tuple[bool, str]]
    _recent_play_completions: collections.deque[PlayRecord]
    _recent_applied_labels: collections.deque[tuple[int, str]]
    _experience_recorder: ExperienceRecorder | None
    _progress_monitor: ForwardProgressMonitor | None
    _worktrees: WorktreeManager | None

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
    all orchestrator runtime/control state (read or written) flows through the
    :class:`_CompletionHost` Protocol so SIGHUP and per-tick mutation never goes
    stale.
    """

    def __init__(
        self,
        *,
        host: _CompletionHost,
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

    # ------------------------------------------------------------------

    async def harvest_completed(self) -> None:
        """Drain finished play tasks and process each via ``process_completion``.

        Invalidates the selection-digest cache when any task is harvested:
        a play completion (success or unhandled exception) is always worth a
        fresh selector pass, even when ``state.total_plays`` doesn't reflect
        it (e.g. tasks that raised before recording a row).
        """
        if not hasattr(self._host, "_completion_processing_idle"):
            self._host._completion_processing_count = 0
            self._host._completion_processing_idle = asyncio.Event()
            self._host._completion_processing_idle.set()
        completed = [did for did, t in self._host._in_flight.items() if t.done()]
        for did in completed:
            task = self._host._in_flight.pop(did)
            self._host._completion_processing_count += 1
            self._host._completion_processing_idle.clear()
            try:
                await self.process_completion(did, task)
            finally:
                self._host._completion_processing_count -= 1
                if self._host._completion_processing_count <= 0:
                    self._host._completion_processing_count = 0
                    self._host._completion_processing_idle.set()
        if completed:
            self._host._last_selection_digest = None
            self._host._idle_streak = 0

    async def wait_for_in_flight(self, *, timeout: float) -> None:
        """``asyncio.wait`` with first-completed semantics on the in-flight set."""
        await asyncio.wait(
            self._host._in_flight.values(),
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
            # No snapshot recorded (or already detached pre-play). Tracking
            # the post-only state offers no signal — silent return.
            return
        try:
            mutated, post_ref, restored = await asyncio.to_thread(
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
        if not restored:
            _logger.error(
                "main_repo_auto_restore_failed",
                session_id=self._session_id,
                play_type=play_type.value,
                agent_id=agent_id,
                agent_type=agent_type,
                default_branch=self._main_repo.default_branch,
                post_play_branch=post_ref,
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
        next_state = await self._run_completion_control_checks(outcome)
        await self._record_completion_experience(
            ctx,
            outcome,
            state_before,
            next_state,
            completed_play_type,
        )
        await self._publish_completion_results(outcome, next_state, completed_play_type)
        await self._handle_end_session_completion(ctx, outcome, next_state, completed_play_type)

    def _pop_completed_dispatch(
        self, dispatch_id: str, task: asyncio.Task[PlayOutcome]
    ) -> tuple[_DispatchContext, PlayOutcome] | None:
        ctx = self._host._dispatch_ctx.pop(dispatch_id, None)
        if ctx is None:
            # Still drop the pre-play snapshot if we somehow have one without
            # matching dispatch context. Prevents the dict from leaking.
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

        # desktop-kqo5: main-repo symbolic-ref invariant guard. Fires at every
        # play boundary, including skipped outcomes — a play that skipped at
        # the executor could still have left the repo poisoned if it ran any
        # checkout commands before bailing.
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

        # desktop-kqo5 catch-22 fix: a successful RECONCILE_STATE is the loop's
        # path out of a latched trunk-dispatch pause (RECONCILE_STATE is now
        # exempt from the pause in Dispatcher so it can run while wedged).
        # Re-verify the main checkout is back on a clean default branch (the
        # restore is idempotent and conflict-aware) and, only if so, lift the
        # pause so normal dispatch resumes.
        if (
            completed_play_type == PlayType.RECONCILE_STATE
            and outcome.success
            and self._main_repo.dispatch_paused
        ):
            restored = await asyncio.to_thread(
                restore_default_branch, self._repo_root, self._main_repo.default_branch
            )
            self._main_repo.dispatch_paused = not restored
            _logger.info(
                "main_repo_dispatch_pause_cleared"
                if restored
                else "main_repo_dispatch_pause_persists",
                session_id=self._session_id,
                via="reconcile_state",
                default_branch=self._main_repo.default_branch,
            )

    async def _handle_skipped_completion(self, outcome: PlayOutcome) -> _CompletionVerdict:
        completed_play_type = outcome.play_type
        if getattr(outcome, "skipped", False) is True:
            # desktop-85ex: unify the ``play_skipped`` schema between the
            # loop-tick selector-None path (loop.py) and the executor-time
            # divergence path here. Both emit a structured ``reason`` enum
            # plus a stable ``event_source`` so log post-processing can
            # join them without grep-and-pray. ``skip_category`` is retained
            # for back-compat with existing dashboards.
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
            # Track executor-time masked skips as a one-shot diagnostic flag
            # surfaced in state.recent_executor_skip. The rolling divergence
            # window that feeds observation slot ``executor_skip_rate_recent_50``
            # is NO LONGER fed here: post the eligibility refactor the authority
            # masks invalid plays up front and confirms the selected play with a
            # live read, so the executor masked-skip path is vestigial. The
            # divergence signal now counts EligibilityAuthority confirm-repicks,
            # drained from the selector once per selection cycle (see
            # ``_record_selection_repicks``). Other skip categories (no_target,
            # staffing) never indicated state divergence.
            is_masked_skip = outcome.skip_category == "masked"
            self._velocity.set_recent_executor_skip(is_masked_skip)
            # All-category no-op window for spin detection. The skip path returns
            # early (below) before the loop-detection/stagnation checks ever run,
            # so the no-op-spin check is invoked HERE — this is the exact path the
            # write_impl↔reconcile spin lived on.
            self._host._recent_play_outcomes.append((True, completed_play_type.value))
            post_state = await self._state_builder.build_state()
            await self._host._safe_call(
                self._host._state_provider.on_state_update(post_state), "on_state_update_post"
            )
            # A skip is the canonical no-forward-progress tick (no agent
            # dispatched) — feed the forward-progress monitor here, on the skip
            # path that returns early before the main checks below.
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
        # divergence window is fed from selector confirm-repicks (see
        # ``_record_selection_repicks``), not from play completions, so nothing
        # is appended here anymore.
        self._velocity.set_recent_executor_skip(False)
        self._host._recent_play_outcomes.append((False, completed_play_type.value))

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
            self._host._last_play_id = outcome.play_id
            # If this play was dispatched from the override queue, record the
            # play_id so compute_play_streaks can ignore it. Override-dispatched
            # plays (bootstrap recipe, user request, retry) are not PPO-collapse,
            # so a burst of them must not trigger loop_detected.
            if ctx.override_kind is not None:
                self._overrides.dispatched_play_ids.add(outcome.play_id)
            # Capture the just-completed play in memory so the next
            # _build_state tick sees it even if the SQLite WAL write hasn't
            # been visible to a fresh `get_play_history` read yet. Without
            # this, same-tick instantiate_agent pairs slipped past the
            # cooldown mask (desktop-65bg).
            started_at_raw = ctx.params.extras.get("started_at")
            started_at = started_at_raw if isinstance(started_at_raw, str) else ""
            self._host._recent_play_completions.append(
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
            # Label shadow (desktop-quv9): on a successful systematic_debugging,
            # the skill template applies ``agentshore/root-cause-found`` to the
            # issue via gh CLI from inside the agent subprocess. The label
            # lands on GitHub immediately, but AgentShore's cached issue snapshot
            # only learns about it on the next ``refresh_issues`` poll —
            # which can be many seconds out. The next selector tick fires
            # well before that, so PPO can re-select (issue, systematic_debugging)
            # against the stale snapshot (observed in session 2b8729bf:
            # play_id 3938 success → play_id 3947 same-issue re-pick 20s later).
            # Pushing the label onto the shadow makes
            # ``_merge_recent_applied_labels`` overlay it on the issue records
            # at the next state build, so ``issue_available_for_debug``
            # excludes it from the systematic_debugging candidate set.
            if (
                outcome.success
                and completed_play_type == PlayType.SYSTEMATIC_DEBUGGING
                and isinstance(ctx.params.issue_number, int)
            ):
                self._host._recent_applied_labels.append(
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
        # Track per-PR unblock_pr ATTEMPTS so the resolver can stop retrying
        # irresolvable-conflict PRs after _UNBLOCK_PR_EXHAUSTION_THRESHOLD
        # attempts. We count every completion — success or failure — because
        # a "successful" unblock_pr can still leave the PR in pr_unblockable()
        # (e.g. agent committed work but CI is still red, or one conflict
        # resolved while another appeared). Counting only failures let
        # chronically-stuck PRs absorb dispatches indefinitely (desktop-uwg).
        # If an unblock truly worked, the PR drops out of the predicate's
        # match set and the counter never gets exercised again — so this
        # change costs nothing in the happy path.
        if completed_play_type == PlayType.UNBLOCK_PR and ctx.params.pr_number is not None:
            exhausted = self._executor._resolver.record_unblock_pr_failure(ctx.params.pr_number)
            # Fast-path (#6): a failure that names a human/CI-infra blocker can
            # never be resolved by re-dispatching an agent, so mark it
            # manual-required immediately rather than burning the full attempt
            # budget. The attempt-count exhaustion still backstops ambiguous
            # cases (resolvable-looking failures that nonetheless keep recurring).
            error_text = (outcome.error or "").lower()
            terminal = any(m in error_text for m in _UNBLOCK_MANUAL_REQUIRED_MARKERS)
            if exhausted or terminal:
                await self._host._safe_call(
                    self.mark_pr_manual_required(ctx.params.pr_number),
                    "mark_pr_manual_required",
                )

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
                self._host._natural_exit_reason = reason
            self._host._stop_requested = True
        elif reason is not None and self._host._pause_event.is_set():
            await self._lifecycle.pause_with_reason(reason)

        await self.check_no_forward_progress(next_state, outcome)
        if (
            await self._host._check_stagnation_escalation(next_state)
            and self._host._pause_event.is_set()
        ):
            await self._lifecycle.pause_with_reason("stagnation")
        self._host._feedback_cadence_plays_since_ack += 1
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
        # Phase 3: RL experience collection and policy update.
        #
        # The fragile, crash-prone tail (snapshots, reward, observation encoding,
        # ExperienceRecord build+persist, policy update, checkpoint) lives in the
        # fully-guarded ``ExperienceRecorder`` — a failure there degrades to a
        # skipped record / skipped update with a logged error, instead of
        # propagating out of ``run_until_idle`` and killing the loop (the
        # ``sidecar_orchestrator_run_failed`` crash). Only the cheap, safe
        # bookkeeping (velocity events, ``done``) stays inline here.
        if (
            self._host._experience_recorder is not None
            and isinstance(self._host._selector, _ppo_selector_cls())
            and self._host._metrics is not None
        ):
            from agentshore.rl.selector import _PendingStep

            done = (
                completed_play_type == PlayType.END_SESSION
                or self._host._stop_requested
                or (
                    next_state.budget is not None
                    and next_state.budget.enabled
                    and budget_reserve_reached(
                        spent=next_state.budget.spent,
                        total_budget=next_state.budget.total_budget,
                    )
                )
            )

            # Update velocity tracking (before the recorder snapshots so
            # ctx_after sees current velocity).
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

            await self._host._experience_recorder.record_and_update(
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
    ) -> None:
        # Refresh issue cache after plays that modify issues. QA and design
        # audit can create follow-up issues even if their play result is
        # partial, so they always trigger a post-play refresh.
        refresh_on_success = (
            PlayType.SEED_PROJECT,
            PlayType.GROOM_BACKLOG,
            PlayType.ISSUE_PICKUP,
            PlayType.MERGE_PR,
            PlayType.CODE_REVIEW,
            PlayType.WRITE_IMPLEMENTATION_PLAN,
            PlayType.REFINE_TASK_BREAKDOWN,
        )
        # desktop-rla8: CLEANUP and RECONCILE_STATE always trigger a full
        # paginated re-sync via ``_FULL_ISSUE_SYNC_PLAYS``; that's the
        # belt-and-suspenders for issues whose ``updated_at`` doesn't move
        # (deletions, transfers). They land here whether or not they
        # succeeded.
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
            # Force a paginated full sync when issue_pickup discovers an issue
            # already CLOSED on GitHub — the incremental ``since=`` cursor
            # has been observed missing close-state transitions for many
            # refresh cycles, leaving the cache stale.
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
        if self._host._cfg.learnings.enabled and outcome.play_id is not None:
            await self._host._safe_call(
                self.update_learnings(outcome, completed_play_type),
                "update_learnings",
            )

        await self._host._safe_call(
            self._host._state_provider.on_play_completed(outcome), "on_play_completed"
        )
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
                self._host._state_provider.on_agent_changed(outcome.agent_id, final_status),
                "on_agent_changed_final",
            )
            await self._retire_or_recover_errored_agent(outcome.agent_id, final_status)
        if completed_play_type == PlayType.TAKE_BREAK:
            self._handle_take_break_outcome(outcome)
        if (
            completed_play_type == PlayType.END_AGENT
            and outcome.success
            and outcome.agent_id is not None
        ):
            # The agent slot was cleared by the END_AGENT play. Drop any stale
            # break-recovery count so a re-instantiated agent reusing the id
            # doesn't inherit an elevated (recovery-exhausted) counter.
            self._recovery.clear_break_failures(outcome.agent_id)
        # Second state_update after play completes so consumers see the fresh result
        post_state = await self._state_builder.build_state()
        await self._host._safe_call(
            self._host._state_provider.on_state_update(post_state), "on_state_update_post"
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
                self._host._end_session_dispatch_started = False
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
                isinstance(self._host._selector, _ppo_selector_cls())
                and len(self._host._selector.buffer) > 0
            ):
                await self._host._selector.update_policy(next_state_value=0.0)
                final_state = await self._state_builder.build_state()
                weights_dir = self._repo_root / ".agentshore" / "weights"
                await self._host._selector.save_checkpoint(
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
        await self._store.add_pull_request_labels(
            self._session_id,
            pr_number,
            [MANUAL_REQUIRED_LABEL],
        )
        github = getattr(self._executor, "_github", None)
        if github is not None:
            await github.label_issue(
                pr_number,
                [MANUAL_REQUIRED_LABEL],
                f"manual_required:pr{pr_number}",
            )
        _logger.warning(
            "pr_manual_required",
            session_id=self._session_id,
            pr_number=pr_number,
            label=MANUAL_REQUIRED_LABEL,
        )

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
        draining = getattr(self._host, "_draining", False) or getattr(
            self._host, "_stop_requested", False
        )
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

        if final_status != AgentStatus.ERROR:
            self._recovery.clear_rate_limit_enqueued(agent_id)
            self._recovery.clear_unknown_error_enqueued(agent_id)
            return
        handle = self._manager.handles.get(agent_id)
        if handle is None:
            return
        error_class = getattr(handle, "last_error_class", None)

        if error_class in _RATE_LIMIT_RECOVERY_ERROR_CLASSES:
            kind = OverrideKind.RATE_LIMIT_RECOVERY
            event = "rate_limit_recovery_enqueued"
            already = self._recovery.is_rate_limit_enqueued(agent_id)
            mark = self._recovery.mark_rate_limit_enqueued
        elif error_class in _UNKNOWN_ERROR_RECOVERY_ERROR_CLASSES:
            kind = OverrideKind.UNKNOWN_ERROR_RECOVERY
            event = "unknown_error_recovery_enqueued"
            already = self._recovery.is_unknown_error_enqueued(agent_id)
            mark = self._recovery.mark_unknown_error_enqueued
        else:
            # Not a recovery-eligible class (auth, invalid_model, crash_*,
            # timeout*) — leave it for the END_AGENT path, no take_break.
            return

        if already:
            return
        params = PlayParams(
            agent_id=agent_id,
            extras={
                "trigger_agent_id": agent_id,
                "trigger_error_class": error_class,
            },
        )
        self._overrides.put_nowait(
            OverrideEntry(
                play_type=PlayType.TAKE_BREAK,
                params=params,
                kind=kind,
            )
        )
        mark(agent_id)
        _logger.info(
            event,
            session_id=self._session_id,
            agent_id=agent_id,
            error_class=error_class,
        )

    def _handle_take_break_outcome(self, outcome: PlayOutcome) -> None:
        """Track consecutive take_break failures for END_AGENT eligibility."""

        agent_id = outcome.agent_id
        if agent_id is None:
            return
        # Clear both recovery latches on any take_break completion so the next
        # ERROR transition for this agent can re-arm the appropriate override
        # (the break could have been triggered by either path).
        self._recovery.clear_rate_limit_enqueued(agent_id)
        self._recovery.clear_unknown_error_enqueued(agent_id)
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

    async def on_crash(self, agent_id: str, return_code: int) -> None:
        """Log crash; leave handle in ERROR state. No auto-recovery in Phase 2."""
        _logger.error(
            "agent_crashed",
            session_id=self._session_id,
            agent_id=agent_id,
            return_code=return_code,
        )

        await self._host._safe_call(
            self._host._state_provider.on_agent_changed(agent_id, AgentStatus.ERROR),
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
        self._host.context_pressure_hints[agent_id] = ratio

        await self._host._safe_call(
            self._host._state_provider.on_agent_changed(agent_id, AgentStatus.BUSY),
            "on_agent_changed",
        )

    async def update_learnings(self, outcome: PlayOutcome, play_type: PlayType) -> None:
        """Reinforce learnings on success; harvest new entries after GROOM_BACKLOG."""
        from agentshore.learnings import Learning, load, reinforce, save_atomic, top_k

        learnings_path = self._repo_root / self._host._cfg.learnings.file
        entries = await asyncio.to_thread(load, learnings_path)
        changed = False

        if outcome.success and outcome.play_id is not None:
            # Build a reinforcement key from skill_name + play_type + artifact paths
            artifact_paths = " ".join(
                str(a.get("path", "")) for a in outcome.artifacts if isinstance(a, dict)
            )
            reinforce_key = f"{play_type.value} {artifact_paths}".strip()
            reinforced = reinforce(entries, reinforce_key, source_play_id=outcome.play_id)
            if any(
                r.last_reinforced_play_id != e.last_reinforced_play_id
                for r, e in zip(reinforced, entries, strict=True)
            ):
                entries = reinforced
                changed = True

        # Harvest new learnings from GROOM_BACKLOG artifacts
        if play_type == PlayType.GROOM_BACKLOG and outcome.success:
            import uuid as _uuid
            from datetime import UTC, datetime

            for artifact in outcome.artifacts:
                if not isinstance(artifact, dict):
                    continue
                if artifact.get("type") != "learnings":
                    continue
                raw_learnings = artifact.get("learnings", [])
                if not isinstance(raw_learnings, list):
                    continue
                for raw_entry in raw_learnings:
                    if not isinstance(raw_entry, dict):
                        continue
                    pattern = raw_entry.get("pattern", "")
                    if not pattern:
                        continue
                    if any(e.pattern == pattern for e in entries):
                        continue
                    entries.append(
                        Learning(
                            id=str(_uuid.uuid4()),
                            pattern=pattern,
                            confidence=float(
                                raw_entry.get("confidence", DEFAULT_LEARNING_CONFIDENCE)
                            ),
                            sessions_since_use=0,
                            source_play_id=outcome.play_id,
                            last_reinforced_play_id=outcome.play_id,
                            created_at=datetime.now(UTC).isoformat(),
                            category=str(raw_entry.get("category", "general")),
                        )
                    )
                changed = True

        # Trim to max_entries keeping highest confidence
        if len(entries) > self._host._cfg.learnings.max_entries:
            entries = top_k(entries, k=self._host._cfg.learnings.max_entries)
            changed = True

        if changed:
            await asyncio.to_thread(save_atomic, learnings_path, entries)

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
        monitor = self._host._progress_monitor
        if monitor is None:
            return
        if getattr(self._host, "_draining", False) or getattr(self._host, "_stop_requested", False):
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
        import time as _time

        from agentshore.core.github_syncer import GitHubSyncer, sync_cursor_now

        try:
            from agentshore.github.adapter import GitHubAdapter

            gh = GitHubAdapter(store=self._store, session_id=self._session_id, cfg=self._host._cfg)
            await gh.probe()
            syncer = GitHubSyncer(
                gh=gh, store=self._store, cfg=self._host._cfg, session_id=self._session_id
            )
            if gh.available:
                last_sync = await self._store.get_last_issue_sync_at(self._session_id)
                full_sync = (
                    force_full_sync
                    or completing_play in _FULL_ISSUE_SYNC_PLAYS
                    or last_sync is None
                )
                since = None if full_sync else last_sync

                # Capture the cutoff *before* the fetch so anything that
                # updates mid-fetch is picked up next time. Lookback absorbs
                # clock skew between gh and the local box.
                new_cutoff = sync_cursor_now()

                # ``state="all"`` so close/reopen transitions surface — the
                # cache_github_issues upsert flips local state to match.
                issues = await syncer.fetch_issues(state="all", since=since)
                if issues is None:
                    _logger.warning(
                        "github_issues_refresh_failed",
                        full_sync=full_sync,
                        since=since,
                    )
                else:
                    await syncer.cache_issues(issues, cursor=new_cutoff)
                    _logger.info(
                        "github_issues_refreshed",
                        changed_count=len(issues),
                        full_sync=full_sync,
                        cursor=new_cutoff,
                    )

                # Duplicate-bead close sweep runs only on full sync — it
                # needs the complete open-issue set to safely identify
                # issues whose only linked beads are closed duplicates.
                if full_sync and issues is not None:
                    open_issues = [iss for iss in issues if iss.state == "open"]
                    from agentshore.beads import (
                        BeadStatus,
                        GraphReadError,
                        GraphTask,
                        load_graph,
                    )

                    try:
                        graph = await load_graph(self._repo_root)
                    except GraphReadError:
                        graph = None
                    if graph is not None:
                        tasks_by_issue: dict[int, list[GraphTask]] = {}
                        for task in graph.tasks:
                            issue_number = task.issue_number
                            if issue_number is None:
                                continue
                            tasks_by_issue.setdefault(issue_number, []).append(task)
                        for issue in open_issues:
                            related = tasks_by_issue.get(issue.issue_number, [])
                            if not related:
                                continue
                            has_live = any(task.status != BeadStatus.CLOSED for task in related)
                            if has_live:
                                continue
                            if not any(
                                _DUPLICATE_BEAD_TITLE_RE.match(task.title) for task in related
                            ):
                                continue
                            key = f"{self._session_id}:duplicate-close:{issue.issue_number}"
                            closed = await gh.close_issue(issue.issue_number, idempotency_key=key)
                            if closed:
                                await self._store.update_issue_state(
                                    issue.issue_number,
                                    self._session_id,
                                    "closed",
                                )
                                _logger.info(
                                    "github_issue_duplicate_bead_closed",
                                    issue_number=issue.issue_number,
                                    bead_count=len(related),
                                )
                trusted_pr_authors = syncer.trusted_authors()
                pull_requests = await syncer.fetch_trusted_open_pull_requests(
                    limit=_PR_LIMIT,
                    trusted_authors=trusted_pr_authors,
                    context="refresh_open",
                )
                refetched = await syncer.resync_missing_pull_requests(
                    fetched_open=pull_requests,
                    limit=_PR_LIMIT,
                    trusted_authors=trusted_pr_authors,
                )
                if refetched:
                    pull_requests.extend(refetched)
                    _logger.info("github_pull_requests_state_resync", count=len(refetched))
                if pull_requests:
                    await syncer.cache_pull_requests(pull_requests)
                    _logger.info("github_pull_requests_refreshed", changed_count=len(pull_requests))
                # desktop-12g9: mark worktree rows ``stale`` for PRs that
                # transitioned to MERGED or CLOSED, then run the TTL reaper.
                # ``refetched`` carries the resolved state for previously-open
                # PRs that disappeared from the open-list. ``stale`` rows older
                # than ``reap_ttl_seconds`` get reaped.
                await self._mark_worktrees_stale_for_closed_prs(refetched)
                await self._sweep_closed_pr_worktrees()
        except (FileNotFoundError, TimeoutError, OSError, aiosqlite.Error) as exc:
            _logger.warning("github_refresh_failed", error=str(exc))
        finally:
            self._host._last_refresh_time = _time.monotonic()
            await self._ensure_ssh_key_fresh()

    async def _ensure_ssh_key_fresh(self) -> None:
        """Re-check the SSH signing key periodically so merge_pr doesn't fail."""
        try:
            from agentshore.core.git_safety import ensure_ssh_signing_key_loaded

            loaded, detail = await asyncio.to_thread(ensure_ssh_signing_key_loaded)
            if not loaded:
                _logger.debug("ssh_signing_key_refresh_failed", detail=detail)
        except Exception:
            pass

    async def _mark_worktrees_stale_for_closed_prs(
        self,
        refetched_prs: list[PullRequestRecord],
    ) -> None:
        """Transition worktree rows to ``stale`` for PRs that just closed/merged.

        Called from ``refresh_issues`` with the PRs we re-pulled at
        ``state='all'`` to confirm their new state. A PR whose new state is
        anything other than ``"open"`` no longer needs an active worktree;
        the closed-PR TTL reaper will sweep it after the grace period.
        """
        if self._host._worktrees is None or not refetched_prs:
            return
        from agentshore.agents.worktree.registry import lookup_by_branch, mark_status

        for pr in refetched_prs:
            if pr.state == "open" or not pr.branch:
                continue
            try:
                row = await lookup_by_branch(
                    self._store, session_id=self._session_id, branch_name=pr.branch
                )
            except (OSError, aiosqlite.Error) as exc:
                _logger.warning(
                    "worktree_stale_lookup_failed",
                    branch=pr.branch,
                    error=str(exc),
                )
                continue
            if row is None or row.status != "active":
                continue
            try:
                await mark_status(
                    self._store,
                    worktree_id=row.worktree_id,
                    status="stale",
                    failure_reason=f"pr_closed_state_{pr.state}",
                )
                _logger.info(
                    "worktree_marked_stale_for_closed_pr",
                    worktree_id=row.worktree_id,
                    branch=pr.branch,
                    pr_state=pr.state,
                )
            except (OSError, aiosqlite.Error) as exc:
                _logger.warning(
                    "worktree_stale_mark_failed",
                    worktree_id=row.worktree_id,
                    branch=pr.branch,
                    error=str(exc),
                )

    async def _sweep_closed_pr_worktrees(self) -> None:
        """Run the TTL reaper for ``stale`` worktree rows in the current session."""
        if self._host._worktrees is None:
            return
        try:
            report = await self._host._worktrees.reap_closed_prs(
                ttl_seconds=self._host._cfg.worktrees.reap_ttl_seconds,
            )
        except (OSError, aiosqlite.Error, ValueError) as exc:
            _logger.warning("worktree_pr_ttl_reap_failed", error=str(exc))
            return
        if report.total > 0:
            _logger.info(
                "worktree_pr_ttl_reap",
                reaped=len(report.removed),
                failed=len(report.failed),
                ttl_seconds=self._host._cfg.worktrees.reap_ttl_seconds,
            )
