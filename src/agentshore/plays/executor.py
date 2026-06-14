"""Shared play lifecycle.

Covers target confirmation, context creation, execution, validation, and persistence.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time
from typing import TYPE_CHECKING

from agentshore.agents._selection import select_agent_for
from agentshore.beads import load_graph
from agentshore.data.models import ReviewQueueRecord
from agentshore.data.store import (
    ExternalMutationRecord,
    HandoffRecord,
    PlayRecord,
    PullRequestRecord,
)
from agentshore.errors import (
    AgentOutputInvalid,
    AgentProcessCrashed,
    AgentTimeout,
    AntiConfirmationViolation,
    FailureKind,
    IssueInflationDetected,
    PreconditionFailed,
)
from agentshore.github.labels import DISALLOWED_LABEL
from agentshore.identity_names import same_identity
from agentshore.logging import get_logger
from agentshore.plays._publish_reconciler import (
    _AUTH_ERROR_MARKERS,
    IssuePickupPublishReconciler,
    _pr_number_from_payload,
)
from agentshore.plays.base import PlayExecutionContext, PlayParams
from agentshore.plays.scope import validate_scope
from agentshore.state import AgentStatus, PlayOutcome, PlayType, SkillResult
from agentshore.utils import now_iso

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentshore.agents.handle import AgentHandle
    from agentshore.agents.manager import AgentManager
    from agentshore.config import RuntimeConfig
    from agentshore.data.store import DataStore
    from agentshore.github.adapter import GitHubAdapter
    from agentshore.plays.base import Play
    from agentshore.plays.registry import PlayRegistry
    from agentshore.plays.resolver import ParameterResolver
    from agentshore.state import OrchestratorState, StateProvider

_logger = get_logger(__name__)


# Executor branching on play behavior is now declarative: each ``Play`` exposes
# ``is_observation`` / ``requeue_on_anti_confirmation`` / ``is_handoff`` /
# ``retarget_pr_base`` / ``authors_prs`` (defaulted inert on the base classes,
# overridden by the opt-in plays). See the ``Play`` protocol docstring.
_MAX_REQUEUE_ATTEMPTS = 3

_POLICY_DISALLOWED_ERROR_MARKERS = (
    "forbidden by skill policy",
    "ci-change requested",
)


@dataclasses.dataclass(frozen=True)
class _ExecutionSetup:
    play_id: int
    params: PlayParams
    ctx: PlayExecutionContext
    started_at: str
    alignment_before: float | None
    current_play_handle: AgentHandle | None
    source_context_size: int


def _claim_group_id(params: PlayParams | None) -> str | None:
    if params is None:
        return None
    raw = params.extras.get("claim_group_id")
    return raw if isinstance(raw, str) and raw else None


def _is_policy_disallowed(result: SkillResult) -> bool:
    error = (result.error or "").lower()
    return any(marker in error for marker in _POLICY_DISALLOWED_ERROR_MARKERS)


def _issue_number_from_value(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = re.search(r"(?:issue[#:-]?|gh-)?(\d+)", value.strip(), re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _labels_from_value(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return []


def _issue_label_mutation(mut: dict[str, object]) -> tuple[int, list[str]] | None:
    mut_type = str(mut.get("type", ""))
    if mut_type == "label" and str(mut.get("action", "add")).lower() not in {"add", "apply"}:
        return None
    if mut_type not in {"label", "label_issue"}:
        return None

    issue_number = _issue_number_from_value(
        mut.get("issue") or mut.get("issue_number") or mut.get("target") or mut.get("target_issue")
    )
    labels = (
        _labels_from_value(mut.get("labels"))
        or _labels_from_value(mut.get("label"))
        or _labels_from_value(mut.get("value"))
    )
    if issue_number is None or not labels:
        return None
    return issue_number, labels


class _SkipDispatchError(Exception):
    """Internal signal that a phase short-circuited with a terminal PlayOutcome.

    Raised by ``_prepare_dispatch``, ``_select_skill_agent``, and
    ``_prepare_execution_context`` instead of returning a ``PlayOutcome``
    union.  Caught exactly once by ``execute`` so the early-exit path is a
    single ``try/except`` rather than three repeated isinstance guards.
    """

    def __init__(self, outcome: PlayOutcome) -> None:
        self.outcome = outcome
        super().__init__()


class PlayExecutor:
    """Orchestrates every play through its full lifecycle."""

    def __init__(
        self,
        *,
        registry: PlayRegistry,
        resolver: ParameterResolver,
        store: DataStore,
        manager: AgentManager,
        cfg: RuntimeConfig,
        project_path: Path,
        session_id: str,
        state_provider: StateProvider | None = None,
        github: GitHubAdapter | None = None,
        requeue_callback: Callable[[PlayType, PlayParams], None] | None = None,
        is_draining: Callable[[], bool] | None = None,
    ) -> None:
        self._registry = registry
        self._resolver = resolver
        self._store = store
        self._manager = manager
        self._cfg = cfg
        self._project_path = project_path
        self._session_id = session_id
        self._state_provider = state_provider
        self._github = github
        self._reconciler: IssuePickupPublishReconciler | None = (
            IssuePickupPublishReconciler(github, manager, cfg, project_path)
            if github is not None
            else None
        )
        self._requeue_callback = requeue_callback
        # Lets sleeping plays (take_break) observe a wind-down and abort early
        # (#30). Wired post-construction by the orchestrator once it owns the
        # drain flag (mirrors ``_requeue_callback``).
        self._is_draining = is_draining
        self.emits_play_started = True
        self._inflight_issues: set[int] = set()
        # Session-scoped set of issues that have had a WRITE_IMPLEMENTATION_PLAN
        # started this session. Intentionally not cleared after completion so
        # sequential re-plans are blocked even before the GH label refresh (~2 min)
        # catches up. Concurrent re-plans are also blocked since the add happens
        # before dispatch.
        self._planned_issues: set[int] = set()

    @property
    def inflight_issues(self) -> frozenset[int]:
        return frozenset(self._inflight_issues)

    @property
    def planned_issues(self) -> frozenset[int]:
        return frozenset(self._planned_issues)

    async def execute(
        self,
        play_type: PlayType,
        state: OrchestratorState,
        *,
        override: PlayParams | None = None,
    ) -> PlayOutcome:
        """Run *play_type* through the full execution lifecycle.

        Never raises — all exceptions are captured and embedded in the returned
        PlayOutcome.
        """
        started_at = now_iso()
        try:
            play, params = await self._prepare_dispatch(play_type, state, override, started_at)

            # PR-base self-heal (#8) ----------------------------------------
            # issue_pickup agents sometimes open PRs against the repo default
            # instead of the configured target branch. Retarget before running
            # any PR-scoped play so merge_pr can merge and code_review diffs the
            # right base. Idempotent; a no-op when the base already matches.
            if params.pr_number is not None and play.retarget_pr_base:
                await self._maybe_retarget_pr_base(play_type, params, state)

            params = await self._select_skill_agent(play, play_type, params, started_at)
            setup = await self._prepare_execution_context(
                play, play_type, params, state, started_at
            )
        except _SkipDispatchError as e:
            return e.outcome
        return await self._run_finalize_and_persist(play, play_type, state, setup)

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    async def _prepare_dispatch(
        self,
        play_type: PlayType,
        state: OrchestratorState,
        override: PlayParams | None,
        started_at: str,
    ) -> tuple[Play, PlayParams]:
        try:
            play = self._registry.get(play_type)
        except KeyError:
            await self._record_pre_dispatch_skip(
                play_type,
                started_at=started_at,
                skip_category="code_error",
                error=f"no play registered for {play_type!r}",
            )
            raise _SkipDispatchError(
                PlayOutcome.failed(
                    play_type,
                    f"no play registered for {play_type!r}",
                    failure_kind=FailureKind.CODE_ERROR,
                )
            ) from None

        # When override params are already populated, the EligibilityAuthority's
        # confirm() has already validated this play and the resolver has already
        # enumerated + claimed the target — trust that here and do NOT re-resolve
        # or re-check eligibility (no preconditions recheck). PPO can never reach
        # the no_target / resolve-None path because it never re-resolves.
        if override is not None and override != PlayParams():
            return play, override

        # Legacy / non-PPO callers (no populated override) still drive a resolve()
        # pass; they have no authority confirm upstream. A None here means the
        # resolver found no claimable target — surface the no_target skip so the
        # run history reflects it (this path is unreachable from the PPO loop).
        params = await self._resolver.resolve(play_type, state, override=override)
        if params is None:
            await self._finish_claim_group(override, status="released")
            await self._record_pre_dispatch_skip(
                play_type,
                started_at=started_at,
                skip_category="no_target",
                error="unresolved parameters",
            )
            raise _SkipDispatchError(
                PlayOutcome.skipped_outcome(
                    play_type,
                    "no_target",
                    error="unresolved parameters",
                )
            )
        return play, params

    async def _select_skill_agent(
        self,
        play: Play,
        play_type: PlayType,
        params: PlayParams,
        started_at: str,
    ) -> PlayParams:
        if play.skill_name is None:
            return params

        # Look up the PR's GitHub author for the identity-based
        # anti-confirmation filter. This consults the DB row populated by the
        # GH cache refresh at bootstrap (and updated by record_pull_request).
        # Falls back to None for PRs not yet cached, letting the executor's
        # _anti_confirmation_check act as the backstop.
        pr_github_author: str | None = None
        if play_type == PlayType.CODE_REVIEW and params.pr_number is not None:
            pr_github_author = await self._store.get_pr_github_author(
                params.pr_number, self._session_id
            )
        try:
            handle = select_agent_for(
                play_type,
                self._manager.handles,
                pr_github_author=pr_github_author,
                branch_exposure=self._manager.branch_exposure,
                preferences=self._cfg.agent_preferences,
                branch=params.branch,
                required_agent_type=params.target_agent_type,
                required_agent_id=params.target_agent_id,
            )
        except AntiConfirmationViolation as exc:
            raise _SkipDispatchError(
                await self._handle_selection_violation(play, play_type, params, started_at, exc)
            ) from exc

        # Anti-confirmation DB re-check (defense in depth)
        anti_err = await self._anti_confirmation_check(play_type, params, handle.agent_id)
        if anti_err:
            await self._finish_claim_group(params, status="released")
            await self._record_pre_dispatch_skip(
                play_type,
                started_at=started_at,
                skip_category="staffing",
                error=anti_err,
                agent_id=handle.agent_id,
            )
            raise _SkipDispatchError(
                PlayOutcome.skipped_outcome(
                    play_type,
                    "staffing",
                    error=anti_err,
                    agent_id=handle.agent_id,
                )
            )
        return dataclasses.replace(params, agent_id=handle.agent_id)

    async def _handle_selection_violation(
        self,
        play: Play,
        play_type: PlayType,
        params: PlayParams,
        started_at: str,
        exc: AntiConfirmationViolation,
    ) -> PlayOutcome:
        # Read-only observation plays soft-mask when no agent qualifies rather
        # than failing — the play didn't fail, the staffing did.
        if play.is_observation:
            await self._finish_claim_group(params, status="released")
            await self._record_pre_dispatch_skip(
                play_type,
                started_at=started_at,
                skip_category="masked",
                error=f"masked: {exc}",
                agent_id=params.agent_id,
            )
            return PlayOutcome.skipped_outcome(
                play_type,
                "masked",
                error=f"masked: {exc}",
            )

        # Requeueable plays defer to the next tick (up to a cap) rather than
        # taking a failure penalty — the timing race is transient.
        _raw_attempts = params.extras.get("requeue_attempts") or 0
        attempt = _raw_attempts if isinstance(_raw_attempts, int) else int(str(_raw_attempts))
        if (
            play.requeue_on_anti_confirmation
            and attempt < _MAX_REQUEUE_ATTEMPTS
            and self._requeue_callback is not None
        ):
            requeue_extras = {
                k: v for k, v in params.extras.items() if k not in ("play_id", "started_at")
            }
            requeue_extras["requeue_attempts"] = attempt + 1
            # Clear target_agent_id on requeue: the previously chosen agent
            # may no longer be IDLE, and the resolver re-picks against fresh
            # state on the next dispatch.
            requeue_params = dataclasses.replace(
                params,
                agent_id=None,
                target_agent_id=None,
                extras=requeue_extras,
            )
            self._requeue_callback(play_type, requeue_params)
            _logger.info(
                "code_review_requeued",
                pr_number=params.pr_number,
                attempt=attempt + 1,
                target_agent_id=params.target_agent_id,
                target_agent_type=params.target_agent_type,
                reason=str(exc),
                session_id=self._session_id,
            )
            # Requeue isn't a no-op for the user — surface it in the
            # plays-table so a CR PR that keeps bouncing on anti-confirmation
            # doesn't look invisible in history.
            await self._record_pre_dispatch_skip(
                play_type,
                started_at=started_at,
                skip_category="staffing",
                error=f"requeued: {exc}",
                agent_id=params.agent_id,
            )
            return PlayOutcome.skipped_outcome(
                play_type,
                "staffing",
                error=f"requeued: {exc}",
            )

        await self._finish_claim_group(params, status="released")
        await self._record_pre_dispatch_skip(
            play_type,
            started_at=started_at,
            skip_category="staffing",
            error=str(exc),
            agent_id=params.agent_id,
        )
        return PlayOutcome.skipped_outcome(
            play_type,
            "staffing",
            error=str(exc),
        )

    async def _prepare_execution_context(
        self,
        play: Play,
        play_type: PlayType,
        params: PlayParams,
        state: OrchestratorState,
        started_at: str,
    ) -> _ExecutionSetup:
        # Snapshot alignment_before from the dispatch-time beads graph.
        alignment_before = state.graph.global_closure_ratio if state.graph is not None else None

        # Insert placeholder play row (provides play_id for FK constraints).
        play_id = await self._store.record_play(
            PlayRecord(
                session_id=self._session_id,
                play_type=play_type.value,
                started_at=started_at,
                success=False,
                alignment_before=alignment_before,
            )
        )
        params = dataclasses.replace(
            params,
            extras={**params.extras, "play_id": play_id, "started_at": started_at},
        )
        ctx = PlayExecutionContext(
            session_id=self._session_id,
            play_id=play_id,
            manager=self._manager,
            store=self._store,
            cfg=self._cfg,
            project_path=self._project_path,
            state_provider=self._state_provider,
            is_draining=self._is_draining,
        )

        if not await self._start_claim_group(params, play_id):
            await self._finish_claim_group(params, status="released")
            await self._persist_play(
                play_id,
                started_at,
                False,
                error="work claim inactive",
                failure_category="code_error",
                agent_id=params.agent_id,
            )
            raise _SkipDispatchError(
                PlayOutcome.failed(
                    play_type,
                    "work claim inactive",
                    agent_id=params.agent_id,
                    failure_kind=FailureKind.CODE_ERROR,
                )
            )

        current_play_handle = await self._notify_dispatch_started(
            play, play_type, params, play_id, started_at
        )
        return _ExecutionSetup(
            play_id=play_id,
            params=params,
            ctx=ctx,
            started_at=started_at,
            alignment_before=alignment_before,
            current_play_handle=current_play_handle,
            source_context_size=self._snapshot_context_size(play, params),
        )

    async def _notify_dispatch_started(
        self,
        play: Play,
        play_type: PlayType,
        params: PlayParams,
        play_id: int,
        started_at: str,
    ) -> AgentHandle | None:
        # Notify that the agent is transitioning to BUSY before dispatch begins.
        if (
            play.skill_name is not None
            and self._state_provider is not None
            and params.agent_id is not None
        ):
            await self._state_provider.on_agent_changed(params.agent_id, AgentStatus.BUSY)

        # Track issue_pickup plays so the resolver can avoid double-claiming.
        if play_type == PlayType.ISSUE_PICKUP and params.issue_number is not None:
            self._inflight_issues.add(params.issue_number)
        # Track write_plan plays so concurrent and sequential re-plans on the
        # same issue are blocked before the GH label refresh propagates.
        if play_type == PlayType.WRITE_IMPLEMENTATION_PLAN and params.issue_number is not None:
            self._planned_issues.add(params.issue_number)

        current_play_handle = self._mark_agent_current_play(play_type, params, play_id, started_at)

        _logger.info(
            "play_started",
            session_id=self._session_id,
            play_type=play_type.value,
            agent_id=params.agent_id,
            pr_number=params.pr_number,
            issue_number=params.issue_number,
            branch=params.branch,
            play_id=play_id,
        )

        # Notify that the play has started (with agent_id resolved).
        if self._state_provider is not None:
            await self._state_provider.on_play_started(play_type, params)
        return current_play_handle

    async def _run_finalize_and_persist(
        self,
        play: Play,
        play_type: PlayType,
        state: OrchestratorState,
        setup: _ExecutionSetup,
    ) -> PlayOutcome:
        play_id = setup.play_id
        params = setup.params
        started_at = setup.started_at

        t0 = time.monotonic()
        try:
            outcome = await self._run_play(play, play_type, state, params, setup.ctx)
        finally:
            if setup.current_play_handle is not None:
                setup.current_play_handle.clear_play(play_id)
            if play_type == PlayType.ISSUE_PICKUP and params.issue_number is not None:
                self._inflight_issues.discard(params.issue_number)
        elapsed_s = time.monotonic() - t0
        ended_at = now_iso()

        # Recover issue-pickup runs that completed local work/tests but failed
        # during PR publication. This must happen before scope validation and
        # deferral wiring so a recovered PR is recorded like a normal pickup.
        skill_result = getattr(play, "_last_skill_result", None)
        if (
            skill_result is not None
            and isinstance(skill_result, SkillResult)
            and self._reconciler is not None
        ):
            outcome = await self._reconciler.reconcile(
                play_type,
                params,
                outcome,
                skill_result,
                state,
            )

        # Finalize the worktree allocation (desktop-mr1i). For PR-scoped plays
        # this just bumps last_used_at; for branch-creating plays it re-keys
        # the row from ``pre_branch_key`` to ``SkillResult.branch`` and renames
        # the directory. Trunk allocations have no DB row and skip finalize.
        # Wrapped in try/except so a finalize failure can't poison the outcome —
        # the session-start reaper will sweep any orphan worktree.
        discovered_branch = await self._finalize_worktree(params, outcome, skill_result, play_type)
        if discovered_branch and not params.branch:
            params = dataclasses.replace(params, branch=discovered_branch)

        alignment_after, alignment_delta = await self._load_post_play_alignment(
            play_id,
            play_type,
            setup.alignment_before,
        )

        inflation_raised = False
        if play.skill_name is not None:
            outcome, inflation_raised = await self._check_scope(outcome, play_id, play_type, state)

        if outcome.success or outcome.partial:
            await self._wire_deferrals(
                play,
                play_type,
                params,
                outcome,
                play_id,
                setup.source_context_size,
                elapsed_s,
            )

        if (
            play_type == PlayType.WRITE_IMPLEMENTATION_PLAN
            and params.issue_number is not None
            and not outcome.success
        ):
            self._planned_issues.discard(params.issue_number)

        if skill_result is not None and isinstance(skill_result, SkillResult):
            await self._persist_mutations(play_id, params, skill_result)

        await self._persist_completed_play(
            play_id=play_id,
            started_at=started_at,
            ended_at=ended_at,
            elapsed_s=elapsed_s,
            outcome=outcome,
            params=params,
            alignment_before=setup.alignment_before,
            alignment_after=alignment_after,
            alignment_delta=alignment_delta,
        )
        await self._finish_claim_group(
            params,
            status="retrying"
            if outcome.retry_requested
            else ("completed" if outcome.success else "released"),
        )

        # Stamp play_id, alignment_delta (live beads delta), and
        # inflation_raised on outcome.
        return dataclasses.replace(
            outcome,
            play_id=play_id,
            alignment_delta=alignment_delta,
            inflation_raised=inflation_raised,
        )

    async def _load_post_play_alignment(
        self,
        play_id: int,
        play_type: PlayType,
        alignment_before: float | None,
    ) -> tuple[float | None, float | None]:
        # Reload beads after the play and any reconciliation side effects have
        # run. Calibration and merge plays can close beads, so the persisted
        # play row must use the post-play graph rather than the dispatch
        # snapshot.
        try:
            post_graph = await load_graph(self._project_path)
        except Exception as exc:  # pragma: no cover - defensive logging path
            _logger.warning(
                "post_play_graph_reload_failed",
                play_id=play_id,
                play_type=play_type.value,
                error=str(exc),
            )
            post_graph = None
        alignment_after = (
            post_graph.global_closure_ratio if post_graph is not None else alignment_before
        )
        alignment_delta = (
            alignment_after - alignment_before
            if alignment_before is not None and alignment_after is not None
            else None
        )
        return alignment_after, alignment_delta

    async def _persist_completed_play(
        self,
        *,
        play_id: int,
        started_at: str,
        ended_at: str,
        elapsed_s: float,
        outcome: PlayOutcome,
        params: PlayParams,
        alignment_before: float | None,
        alignment_after: float | None,
        alignment_delta: float | None,
    ) -> None:
        failure_category = _infer_failure_category(outcome) if not outcome.success else None
        await self._persist_play(
            play_id,
            started_at,
            outcome.success,
            ended_at=ended_at,
            duration_ms=int(elapsed_s * 1000),
            error=outcome.error,
            failure_category=failure_category,
            # Failure outcomes often omit agent_id, but params.agent_id holds
            # the agent this play was dispatched to (set at selection). Without
            # the fallback, failed skill-backed plays persisted agent_id=None
            # and the ESR Play Log rendered them as the literal "agentshore"
            # instead of the agent that ran them. Internal (agentless) plays
            # keep None.
            agent_id=outcome.agent_id or params.agent_id,
            token_cost=outcome.token_cost,
            dollar_cost=outcome.dollar_cost,
            partial=outcome.partial,
            alignment_before=alignment_before,
            alignment_after=alignment_after,
            alignment_delta=alignment_delta,
        )

    async def _start_claim_group(self, params: PlayParams, play_id: int) -> bool:
        claim_group_id = _claim_group_id(params)
        if claim_group_id is None:
            return True
        return await self._store.start_work_claim_group(
            self._session_id,
            claim_group_id,
            play_id=play_id,
            agent_id=params.agent_id,
        )

    async def _finish_claim_group(self, params: PlayParams | None, *, status: str) -> None:
        claim_group_id = _claim_group_id(params)
        if claim_group_id is None:
            return
        await self._store.finish_work_claim_group(self._session_id, claim_group_id, status=status)

    def _mark_agent_current_play(
        self,
        play_type: PlayType,
        params: PlayParams,
        play_id: int,
        started_at: str,
    ) -> AgentHandle | None:
        if params.agent_id is None:
            return None
        try:
            handle = self._manager.get_handle(params.agent_id)
        except PreconditionFailed:
            return None
        handle.start_play(
            play_type=play_type,
            play_id=play_id,
            started_at=started_at,
            issue_number=params.issue_number,
            pr_number=params.pr_number,
            branch=params.branch,
        )
        return handle

    async def _anti_confirmation_check(
        self, play_type: PlayType, params: PlayParams, candidate_agent_id: str
    ) -> str | None:
        """Return an error string if the candidate violates self-review.

        CODE_REVIEW is the only play with an anti-confirmation invariant:
        the reviewer's GitHub identity must differ from the PR author's
        GitHub login. Every other play (including RUN_QA, which exercises
        the merged trunk) accepts any qualified agent.

        Identity is the only deconfliction key. Agent type plays no role —
        a human and an agent can share a GH login, and two agents of the
        same type can have different logins. The resolver pre-filters to a
        cross-identity reviewer; this check is defense-in-depth against
        races (handle reassignment between resolve and dispatch).
        """
        if play_type != PlayType.CODE_REVIEW or params.pr_number is None:
            return None

        try:
            handle = self._manager.get_handle(candidate_agent_id)
        except (PreconditionFailed, KeyError):
            return None
        candidate_identity = handle.github_identity
        if candidate_identity is None:
            return None

        pr_author = await self._store.get_pr_github_author(params.pr_number, self._session_id)
        if pr_author is None:
            return None

        if same_identity(candidate_identity, pr_author):
            return (
                f"anti_confirmation_violation: agent {candidate_agent_id!r} "
                f"identity {candidate_identity!r} authored PR #{params.pr_number}"
            )
        return None

    def _snapshot_context_size(self, play: Play, params: PlayParams) -> int:
        """Return source agent's context_size before handoff plays reset it."""
        if not play.is_handoff:
            return 0
        agent_id = params.source_agent_id or params.agent_id
        if agent_id is None:
            return 0
        try:
            return self._manager.get_handle(agent_id).context_size
        except (PreconditionFailed, KeyError) as exc:
            _logger.warning(
                "context_size_snapshot_failed",
                play_type=play.play_type.value,
                agent_id=agent_id,
                error=str(exc),
            )
            return 0

    async def _run_play(
        self,
        play: Play,
        play_type: PlayType,
        state: OrchestratorState,
        params: PlayParams,
        ctx: PlayExecutionContext,
    ) -> PlayOutcome:
        """Call play.execute, catching known exceptions and converting to outcomes."""
        try:
            return await play.execute(state, params, ctx=ctx)
        except AgentTimeout as exc:
            _logger.warning("play_execution_timeout", play_type=play_type.value, error=str(exc))
            return PlayOutcome.failed(
                play_type,
                str(exc),
                agent_id=params.agent_id,
                partial=True,
                retry_requested=True,
                failure_kind=FailureKind.AGENT_ERROR,
            )
        except (PreconditionFailed, AgentProcessCrashed, AgentOutputInvalid) as exc:
            _logger.warning("play_execution_error", play_type=play_type.value, error=str(exc))
            return PlayOutcome.failed(
                play_type,
                str(exc),
                agent_id=params.agent_id,
                failure_kind=FailureKind.AGENT_ERROR,
            )
        except Exception as exc:
            _logger.exception(
                "unexpected_play_error",
                play_type=play_type.value,
                exc_type=type(exc).__name__,
                error=str(exc),
            )
            return PlayOutcome.failed(play_type, str(exc), agent_id=params.agent_id)

    async def _finalize_worktree(
        self,
        params: PlayParams,
        outcome: PlayOutcome,
        skill_result: SkillResult | None,
        play_type: PlayType,
    ) -> str | None:
        """Hand back the allocation to ``WorktreeManager``.

        Returns the discovered branch name when a branch-creating worktree
        was successfully rekeyed. The caller uses this to back-fill
        ``params.branch`` before PR records are persisted (desktop-edtl).

        - ``TrunkAllocation`` and missing entries are no-ops: trunk-scoped
          plays don't have a row to touch, and missing entries (e.g.
          execution paths that bypass the dispatch mixin's allocator hook)
          have nothing for the manager to clean up.
        - Failures in finalize never poison the play outcome; the
          session-start reaper is the backstop.
        """
        from agentshore.agents.worktree import TrunkAllocation, WorktreeAllocation

        allocation = params._runtime_allocation
        if not isinstance(allocation, WorktreeAllocation):
            if isinstance(allocation, TrunkAllocation):
                _logger.debug(
                    "worktree_finalize_trunk",
                    play_type=play_type.value,
                    path=str(allocation.path),
                )
            return None
        try:
            return await self._manager.worktrees.finalize_after_dispatch(
                allocation,
                result=skill_result if isinstance(skill_result, SkillResult) else None,
                play_outcome=outcome,
            )
        except Exception as exc:  # pragma: no cover - defensive
            _logger.warning(
                "worktree_finalize_failed",
                play_type=play_type.value,
                worktree_id=allocation.worktree_id,
                error=str(exc),
            )
            return None

    async def _check_scope(
        self,
        outcome: PlayOutcome,
        play_id: int,
        play_type: PlayType,
        state: OrchestratorState,
    ) -> tuple[PlayOutcome, bool]:
        """Run scope validation and report issue inflation separately."""
        sr = SkillResult(success=outcome.success, artifacts=outcome.artifacts)
        inflation_raised = False
        try:
            await validate_scope(
                skill_result=sr,
                play_id=play_id,
                play_type=play_type,
                session_id=self._session_id,
                scope_cfg=self._cfg.scope,
                store=self._store,
            )
        except IssueInflationDetected as exc:
            _logger.warning("issue_inflation", play_type=play_type.value, error=str(exc))
            inflation_raised = True
        return outcome, inflation_raised

    async def _wire_deferrals(
        self,
        play: Play,
        play_type: PlayType,
        params: PlayParams,
        outcome: PlayOutcome,
        play_id: int,
        source_context_size: int,
        elapsed_s: float,
    ) -> None:
        """Write Phase-1 deferred DB rows (handoffs, PR records, branch activity)."""
        # Handoff for terminating plays (agent termination)
        if play.is_handoff and params.agent_id:
            await self._store.record_handoff(
                HandoffRecord(
                    session_id=self._session_id,
                    play_id=play_id,
                    source_agent_id=params.agent_id,
                    target_agent_id=params.agent_id,
                    context_tokens_transferred=source_context_size,
                    ramp_up_duration_ms=int(elapsed_s * 1000),
                    context_loss_estimate=1.0,
                )
            )

        # PR and branch artifacts
        now = now_iso()
        for artifact in outcome.artifacts:
            if not isinstance(artifact, dict):
                continue
            artifact_type = artifact.get("type", "")
            if artifact_type in ("pull_request", "pr"):
                # Non-authoring plays (unblock_pr, code_review, …) may emit a
                # PR reference artifact for traceability but must not stamp
                # authorship — skip the entire authorship-recording block.
                if not play.authors_prs:
                    continue
                await self._record_pr_artifact(play_type, params, outcome, artifact, now)
            elif artifact_type == "commit":
                await self._record_commit_artifact(params, outcome, artifact)

    async def _record_pr_artifact(
        self,
        play_type: PlayType,
        params: PlayParams,
        outcome: PlayOutcome,
        artifact: dict[str, object],
        now: str,
    ) -> None:
        """Record authorship, enrich, retarget, enqueue, and label a created PR."""
        pr_number = _pr_number_from_payload(artifact)
        branch = str(artifact.get("branch") or params.branch or "")
        if pr_number is None or not outcome.agent_id:
            return

        if not branch:
            # Surface the leak loudly: a PR-authoring play returned a PR
            # artifact without a branch, and the dispatch params had no
            # fallback either. The record will be persisted with
            # ``branch=None``; the COALESCE upsert preserves any later refresh,
            # but the in-memory snapshot used by the next code_review dispatch
            # will see ``None`` and fail worktree allocation with
            # ``missing_branch``. See issue #567 follow-up.
            _logger.warning(
                "pr_record_missing_branch",
                pr_number=pr_number,
                play_type=play_type.value,
                agent_id=outcome.agent_id,
                artifact_keys=sorted(artifact.keys()),
                params_branch=params.branch,
            )
        self._manager.record_branch_exposure(branch, outcome.agent_id)

        author_agent_type, author_github_login = self._resolve_pr_author(outcome.agent_id)
        await self._store.record_pull_request(
            PullRequestRecord(
                pr_number=pr_number,
                session_id=self._session_id,
                issue_number=params.issue_number,
                branch=branch or None,
                state="open",
                author_agent_id=outcome.agent_id,
                author_agent_type=author_agent_type,
                # Stamp the resolved GH login here so identity-based
                # anti-confirmation works the moment the PR is recorded — without
                # waiting for the next GitHub refresh to fill github_author from
                # the API.
                github_author=author_github_login,
                created_at=now,
            )
        )
        await self._enrich_and_retarget_pr(
            pr_number, outcome.agent_id, author_agent_type, author_github_login
        )
        # Enqueue PR for code review.
        await self._store.enqueue_review(
            ReviewQueueRecord(
                pr_number=pr_number,
                session_id=self._session_id,
                author_label=author_agent_type,
                enqueued_at=now,
            )
        )
        # Apply author label to GitHub PR for visibility.
        if author_agent_type is not None and self._github is not None:
            label_name = f"{self._cfg.intake.label_prefix}author:{author_agent_type}"
            idem_key = f"author_label:pr{pr_number}:{author_agent_type}"
            await self._github.label_issue(pr_number, [label_name], idem_key)

    def _resolve_pr_author(self, agent_id: str) -> tuple[str | None, str | None]:
        """Return (agent_type, github_login) for the PR author, or (None, None).

        Falls back to (None, None) when the agent has already terminated: the
        executor's identity check treats author=None as "any reviewer eligible"
        and the next GitHub state refresh will populate github_author.
        """
        try:
            handle = self._manager.get_handle(agent_id)
        except (PreconditionFailed, KeyError):
            return None, None
        return handle.agent_type.value, handle.github_identity

    async def _enrich_and_retarget_pr(
        self,
        pr_number: int,
        author_agent_id: str,
        author_agent_type: str | None,
        author_github_login: str | None,
    ) -> None:
        """Enrich the freshly-recorded PR row from GitHub and correct its base.

        Immediately enrich review_decision/mergeable/head_sha/is_draft from
        GitHub so the next code_review / merge_pr eligibility check sees real
        data, not the NULL defaults left by ``record_pull_request``. Without
        this, the next periodic refresh's COALESCE upsert can fail to populate
        these fields if the PR-trust filter or another sync path drops the
        record before the cache write commits.
        """
        if self._github is None:
            return
        try:
            enriched = await self._github.fetch_pull_request_by_number(pr_number)
        except (OSError, RuntimeError, ValueError) as exc:  # pragma: no cover
            _logger.warning("pr_enrichment_failed", pr_number=pr_number, error=str(exc))
            enriched = None
        if enriched is None:
            return
        # Preserve the authorship fields we just stamped.
        enriched.author_agent_id = author_agent_id
        enriched.author_agent_type = author_agent_type
        if author_github_login is not None:
            enriched.github_author = author_github_login
        # Deterministic base correction at creation: agents skip the skill's
        # base step ~1-in-6 times, opening PRs against the wrong base (e.g.
        # `main`). Retarget to the configured target_branch now, using the fresh
        # enriched base_ref (not a stale snapshot — the gap in the pre-merge
        # _maybe_retarget_pr_base path). Idempotent via the mutation ledger;
        # pairs with the merge-side gate so a wrong-base PR never lands on the
        # wrong trunk regardless of agent adherence.
        retargeted = await self._retarget_pr_to_target(
            pr_number,
            enriched.base_ref,
            idempotency_prefix="create_retarget_base",
            success_event="pr_base_auto_corrected",
            failure_event="pr_base_auto_correct_failed",
        )
        if retargeted:
            enriched.base_ref = self._cfg.project.target_branch
        await self._store.record_pull_request(enriched)

    async def _record_commit_artifact(
        self,
        params: PlayParams,
        outcome: PlayOutcome,
        artifact: dict[str, object],
    ) -> None:
        """Record branch-commit activity from a ``commit`` artifact."""
        branch = str(artifact.get("branch") or params.branch or "")
        sha = str(artifact.get("sha") or "")
        if branch and outcome.agent_id:
            self._manager.record_branch_commit(branch, outcome.agent_id, sha)
            await self._store.update_branch_activity(
                branch, self._session_id, outcome.agent_id, sha or None
            )

    async def _persist_mutations(
        self, play_id: int, params: PlayParams, skill_result: SkillResult
    ) -> None:
        """Persist each requested mutation with an idempotency key."""
        now = now_iso()
        applied_labels: set[tuple[int, str]] = set()
        for mut in skill_result.requested_mutations:
            key = build_idempotency_key(self._session_id, mut)
            issue_label = _issue_label_mutation(mut)
            if issue_label is not None:
                issue_number, labels = issue_label
                await self._apply_issue_labels(issue_number, labels, key)
                applied_labels.update((issue_number, label) for label in labels)
                continue
            # ``request_play`` was an agent-driven "run this play next" directive
            # that bypassed PPO selection; the mechanism has been removed, so any
            # such emission is ignored (never recorded, never promoted). The PPO
            # policy chooses the next play from the post-completion state instead.
            if str(mut.get("type", "")) == "request_play":
                continue
            await self._store.record_external_mutation(
                ExternalMutationRecord(
                    session_id=self._session_id,
                    idempotency_key=key,
                    mutation_type=str(mut.get("type", "unknown")),
                    target=str(mut.get("target", "")),
                    status="pending",
                    created_at=now,
                    play_id=play_id,
                    request_json=json.dumps(mut),
                )
            )

        if _is_policy_disallowed(skill_result) and params.issue_number is not None:
            issue_number = params.issue_number
            if (issue_number, DISALLOWED_LABEL) not in applied_labels:
                mutation = {
                    "type": "label_issue",
                    "issue": issue_number,
                    "labels": [DISALLOWED_LABEL],
                    "reason": "policy_disallowed",
                }
                key = build_idempotency_key(self._session_id, mutation)
                await self._apply_issue_labels(issue_number, [DISALLOWED_LABEL], key)

    async def _apply_issue_labels(
        self, issue_number: int, labels: list[str], idempotency_key: str
    ) -> bool:
        if self._github is None or not labels:
            return False
        ok = await self._github.label_issue(issue_number, labels, idempotency_key)
        if ok:
            await self._store.add_issue_labels(issue_number, self._session_id, labels)
        return bool(ok)

    async def _maybe_retarget_pr_base(
        self,
        play_type: PlayType,
        params: PlayParams,
        state: OrchestratorState,
    ) -> None:
        """Retarget a PR opened against the wrong base to the configured target.

        No-op when GitHub is unavailable, no ``project.target_branch`` is
        configured, the PR's base is unknown, or the base already matches.
        Idempotent via the mutation ledger (see
        :meth:`GitHubAdapter.retarget_pr_base`). Self-heals #8 regardless of
        whether the authoring agent honored the skill's base instruction.
        """
        pr_number = params.pr_number
        if pr_number is None:
            return
        snapshot = next((pr for pr in state.pull_requests if pr.pr_number == pr_number), None)
        base_ref = snapshot.base_ref if snapshot is not None else None
        await self._retarget_pr_to_target(
            pr_number,
            base_ref,
            idempotency_prefix="retarget_base",
            success_event="pr_base_retargeted",
            failure_event="pr_base_retarget_failed",
            log_fields={"play_type": play_type.value},
        )

    async def _retarget_pr_to_target(
        self,
        pr_number: int,
        current_base: str | None,
        *,
        idempotency_prefix: str,
        success_event: str,
        failure_event: str,
        log_fields: dict[str, str] | None = None,
    ) -> bool:
        """Retarget *pr_number* from *current_base* to the configured target.

        Single source for both the pre-dispatch self-heal
        (:meth:`_maybe_retarget_pr_base`) and the create-time correction in
        :meth:`_wire_deferrals`. Returns True only when a retarget was issued
        and GitHub reported success. No-op (returns False) when GitHub is
        unavailable, no ``project.target_branch`` is configured, the base is
        unknown, or it already matches the target. Idempotent via the mutation
        ledger keyed on ``<idempotency_prefix>:<pr>:<from>-><to>``.
        """
        if self._github is None:
            return False
        target = self._cfg.project.target_branch
        if not target or not current_base or current_base == target:
            return False
        retargeted = await self._github.retarget_pr_base(
            pr_number,
            target,
            idempotency_key=f"{idempotency_prefix}:{pr_number}:{current_base}->{target}",
        )
        _logger.info(
            success_event if retargeted else failure_event,
            pr_number=pr_number,
            from_base=current_base,
            to_base=target,
            session_id=self._session_id,
            **(log_fields or {}),
        )
        return retargeted

    async def _record_pre_dispatch_skip(
        self,
        play_type: PlayType,
        *,
        started_at: str,
        skip_category: str,
        error: str,
        agent_id: str | None = None,
    ) -> None:
        """Persist a plays-table row for a skip that fires before ``record_play``.

        Issue #565 (Bug B): five executor skip branches return a
        ``PlayOutcome.skipped_outcome(...)`` without ever inserting a plays
        row. The UI sets ``state.active_play`` in ``dispatch.py:940`` before
        the executor runs, so these skips produce a "ghost" card in the
        active-play panel that never appears in run history.

        Record a row with ``success=False`` and ``failure_category=skip:<kind>``
        so the run history reflects what the UI showed. The ``skip:`` prefix
        keeps the column semantics broad while still letting consumers
        discriminate skips from real failures (e.g. dashboard styling, ESR
        rollups, PPO reward filtering).
        """
        ended_at = now_iso()
        try:
            await self._store.record_play(
                PlayRecord(
                    session_id=self._session_id,
                    play_type=play_type.value,
                    started_at=started_at,
                    success=False,
                    ended_at=ended_at,
                    duration_ms=0,
                    error=error,
                    failure_category=f"skip:{skip_category}",
                    agent_id=agent_id,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            # Never let skip-row visibility crash the play. The structured
            # log line below preserves observability if the DB write fails.
            _logger.warning(
                "pre_dispatch_skip_record_failed",
                play_type=play_type.value,
                skip_category=skip_category,
                error=str(exc),
            )
        _logger.info(
            "pre_dispatch_skip_recorded",
            session_id=self._session_id,
            play_type=play_type.value,
            skip_category=skip_category,
            error=error,
            agent_id=agent_id,
        )

    async def _persist_play(
        self,
        play_id: int,
        started_at: str,
        success: bool,
        *,
        ended_at: str | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
        failure_category: str | None = None,
        agent_id: str | None = None,
        token_cost: int = 0,
        dollar_cost: float = 0.0,
        partial: bool = False,
        alignment_before: float | None = None,
        alignment_after: float | None = None,
        alignment_delta: float | None = None,
        reward: float | None = None,
    ) -> None:
        await self._store.update_play(
            play_id,
            success=success,
            ended_at=ended_at or now_iso(),
            duration_ms=duration_ms,
            partial=partial,
            token_cost=token_cost,
            dollar_cost=dollar_cost,
            alignment_before=alignment_before,
            alignment_after=alignment_after,
            alignment_delta=alignment_delta,
            reward=reward,
            error=error,
            failure_category=failure_category,
            agent_id=agent_id,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def build_idempotency_key(session_id: str, mutation: dict[str, object]) -> str:
    """Build a globally-unique idempotency key for an external mutation.

    The key is a 16-character hex prefix of the SHA-256 digest of
    ``{"session": session_id, **mutation}`` (keys sorted for stability).

    ``session_id`` is always embedded so that cross-session runs cannot
    collide: a pending row from a killed session cannot block the same
    mutation in a fresh session.

    Raises ``ValueError`` if *session_id* is empty.
    """
    if not session_id:
        raise ValueError("session_id must not be empty when building an idempotency key")
    key_payload = {"session": session_id, **mutation}
    return hashlib.sha256(json.dumps(key_payload, sort_keys=True).encode()).hexdigest()[:16]


def _infer_failure_category(outcome: PlayOutcome) -> str:
    """Map a failed PlayOutcome to a FailureCategory string.

    Prefer the typed ``failure_kind`` the play set at the failure site; the
    substring ladder below is the fallback for legacy / uncaught-Exception
    paths that never set a kind.
    """
    if outcome.failure_kind is not None:
        return str(outcome.failure_kind.to_category())
    error = (outcome.error or "").lower()
    if any(marker in error for marker in _AUTH_ERROR_MARKERS) or "auth" in error:
        return "agent_error"
    if error.startswith(("test", "ci", "pytest", "lint")):
        return "test_failure"
    if "anti_confirmation" in error or "approval" in error or "scope" in error:
        return "alignment_drift"
    if any(
        kw in error
        for kw in (
            "timeout",
            "crash",
            "circuit breaker",
            "circuit_breaker",
            "malformed",
            "invalid output",
        )
    ):
        return "agent_error"
    if any(
        kw in error
        for kw in (
            "needs different reviewer",
            "status-checks-pending",
            "status_checks_pending",
            "too ambiguous",
            "blocked by open dependency",
            "merge_conflicts",
        )
    ):
        return "gate_rejection"
    return "code_error"
