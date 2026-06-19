"""SkillBackedPlay — abstract base class for all skill-dispatched plays."""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import structlog

from agentshore.agents import cli_antigravity
from agentshore.agents.handle import is_noop_invocation
from agentshore.errors import GITHUB_AUTH_ERROR_MARKERS, ErrorClass, FailureKind
from agentshore.plays.base import Play
from agentshore.plays.dispatch import (
    params_to_json_safe_dict,
    play_context_relative_path,
    render_skill_prompt,
    serialize_state_for_skill,
    write_play_context,
)
from agentshore.result_parser import parse_skill_result
from agentshore.state import PlayOutcome, PlayType, SkillResult

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agentshore.agents.handle import AgentInvocationResult
    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.plays.skill_backed.gates import Gate
    from agentshore.rl.mask_reason import MaskReason
    from agentshore.state import OrchestratorState

_logger = structlog.get_logger(__name__)

# Sent (not the full original skill prompt) when the agent finished work but omitted the
# JSON envelope and we retry over ``--resume``. The agent already holds all of its prior
# context in the resumed session, so re-sending the ~10 KB skill prompt only risks it
# re-doing the work and buries the one thing we need. This short, recency-optimal nudge
# asks only for the missing block and explicitly forbids redoing work. See #223.
_JSON_RETRY_PROMPT = (
    "Your previous turn completed work but did not emit the required JSON result block. "
    "Do not redo or repeat any work. Output only the fenced JSON result block for what "
    "you already did, matching the schema in your instructions."
)

# Defect-specific variant (#229): used when the agent DID emit a JSON object but it
# lacked the required top-level boolean ``success`` (the near-miss case flagged by
# ``SkillResult.missing_success_envelope``). Naming the exact defect recovers far more
# of these than the generic nudge, which the agent answers by re-emitting the same shape.
_JSON_RETRY_MISSING_SUCCESS_PROMPT = (
    "Your previous turn emitted a JSON result block, but it is missing the required "
    "top-level boolean `success` field. Do not redo or repeat any work. Re-emit the exact "
    'same JSON for what you already did, adding a top-level "success": true (or false). '
    "Do not invent other keys. Output only that one fenced JSON block."
)

# #236: agy-specific variant for when the agent ended its turn by delegating to its
# internal manage_task async tool instead of completing the work. Unlike the generic nudge
# (which says "don't redo work"), the work was never finished — the agent must re-run it
# synchronously without manage_task. Resume context gives it the full history of what it
# was trying to do; the nudge redirects execution style, not scope.
_JSON_RETRY_ASYNC_HANDOFF_PROMPT = (
    "Your previous turn ended by delegating a command to manage_task instead of running "
    "it to completion. Do not use manage_task. Re-run the remaining work in this turn "
    "synchronously — wait for each command to finish before proceeding — then emit the "
    "fenced JSON result block. Do not end this turn until the JSON block is emitted."
)

# First-byte deadline for the no-JSON resume-retry dispatch (#232). The resume only asks
# the agent to re-print a result block it already computed, so it should start streaming
# within seconds. Without an override it inherits the per-agent-type default — 1800s for
# antigravity (cli_agent._FIRST_BYTE_DEADLINE_BY_TYPE), which turns a silent resume hang
# into 30 min of dead slot time. A short budget fast-fails (recoverable TIMEOUT_STREAM_IDLE)
# and frees the slot. This is safe precisely because a re-emission is NOT a fresh long task.
# Not applied for manage_task handoffs (#236) — those require completing real work.
_JSON_RETRY_FIRST_BYTE_S = 120.0

# Maximum consecutive clean-exit empty no-op dispatches before the play is failed
# and the agent is routed into a standard take_break. The first dispatch counts as
# attempt 1, so this bounds the play at 1 initial + 2 fresh re-dispatches. Each
# re-dispatch is FRESH (no --resume): an empty agy session resumes empty (verified
# live), so resuming a no-op is useless — only a fresh turn can recover. These 3
# attempts ARE the "3 no-ops in a row" that trip the break (desktop no-op resilience).
_NOOP_STREAK_LIMIT = 3


def _worktree_cwd_override(params: PlayParams) -> Path | None:
    """Return the dispatch cwd from an AgentShore-managed worktree allocation.

    ``WorktreeAllocation`` (PR / branch-creating) and ``TrunkAllocation``
    are both honoured — the latter resolves to the main repo path. Returns
    ``None`` when no allocation is present (legacy / internal plays that
    bypass the dispatcher allocator hook), letting ``dispatch_cli`` fall
    back to ``handle.working_dir``.
    """
    from agentshore.agents.worktree import TrunkAllocation, WorktreeAllocation

    # Issue #565: allocation moved off ``params.extras`` (which JSON-serializes)
    # onto the private ``_runtime_allocation`` field.
    allocation = params._runtime_allocation
    if isinstance(allocation, (WorktreeAllocation, TrunkAllocation)):
        return allocation.path
    return None


_REVIEW_PATTERN_INJECTION_PLAYS: frozenset[PlayType] = frozenset(
    {
        PlayType.ISSUE_PICKUP,
        PlayType.UNBLOCK_PR,
        PlayType.SYSTEMATIC_DEBUGGING,
    }
)
# Minimum worktree age before any destructive worktree sweep may delete it.
# Shared by PRUNE and RECONCILE_STATE — both remove worktrees and both must keep
# anything younger, which is the load-bearing guard against the allocate-then-
# delete race (#189, #218): a worktree created inside this window is protected
# regardless of how stale a claim query looks.
_WORKTREE_MIN_AGE_HOURS = 3


class SkillBackedPlay(Play, ABC):
    """Base class for plays that delegate work to a Claude/Codex skill.

    Subclasses must define:
    - ``play_type``        — the PlayType enum value
    - ``skill_name``       — the slash-command name (e.g. "agentshore-issue-pickup")
    - ``capability``       — the AgentManager capability key (e.g. "can_implement")

    Precondition behavior is declarative: subclasses set ``gates`` to a tuple
    of ``Gate`` callables (see ``agentshore.plays.skill_backed.gates``). The
    default ``preconditions()`` walks the tuple and collects non-None reasons.
    Heavy plays may still override ``preconditions()`` for bespoke logic; they
    can call ``super().preconditions(state)`` to run the declared gates first
    and then append additional checks.

    ``_capability_check`` remains for custom precondition overrides that have
    not yet migrated to declarative gates. Standard in-flight and cooldown
    checks live in ``InFlightGate`` and ``CooldownGate``.

    The ``execute()`` implementation:
      1. Writes a play-specific context file via the dispatch helpers.
      2. Renders the slash-command prompt string.
      3. Dispatches to the pre-selected agent (``params.agent_id``).
      4. Parses the raw output into a ``SkillResult``.
      5. Maps the result to a ``PlayOutcome``.
    """

    # Declarative preconditions. Subclasses override to declare the gates that
    # mask this play. Empty tuple == no preconditions (eligible whenever the
    # cross-cutting masks in ``rl/mask.py`` permit).
    gates: Sequence[Gate] = ()

    # Declarative executor-behavior flags (see ``Play`` for semantics). Inert by
    # default; the handful of plays that opt in override the relevant flag.
    authors_prs: bool = False
    retarget_pr_base: bool = False
    is_handoff: bool = False
    is_observation: bool = False
    requeue_on_anti_confirmation: bool = False

    @property
    @abstractmethod
    def play_type(self) -> PlayType: ...

    @property
    @abstractmethod
    def skill_name(self) -> str: ...

    @property
    @abstractmethod
    def capability(self) -> str | None: ...

    def preconditions(self, state: OrchestratorState) -> list[MaskReason]:
        """Walk ``self.gates``, then append authority validity-fn reasons.

        Two layers, in order:

        1. The declarative gates in ``self.gates`` (capability, in-flight,
           cooldown, warmup, beads-init, …) — policy-adjacent eligibility
           checks that stay on the play.
        2. The A-type candidate-validity function registered for this play
           type in ``EligibilityAuthority`` (``_VALIDITY_FNS``), if any. This
           is the single source of truth for "is there a concrete target this
           play could act on right now" — consolidated out of the bespoke
           ``preconditions()`` overrides that previously lived on each play.

        The authority owns validity; the play owns its gates. Subclasses with
        bespoke needs may still override this and call
        ``super().preconditions(state)`` first.

        Imports of ``build_candidate_plan`` / ``EligibilityAuthority`` are
        lazy to avoid an import cycle (``eligibility`` → ``candidates`` →
        plays).
        """
        reasons: list[MaskReason] = []
        for gate in self.gates:
            r = gate(state)
            if r is not None:
                reasons.append(r)

        from agentshore.rl.eligibility import EligibilityAuthority

        validity_fn = EligibilityAuthority.validity_fn_for(self.play_type)
        if validity_fn is not None:
            from agentshore.plays.candidates import build_candidate_plan

            reasons.extend(validity_fn(state, build_candidate_plan(state)))

        return reasons

    def _capability_check(self, state: OrchestratorState) -> list[MaskReason]:
        """Return a non-empty list if no IDLE non-rate-limited agent has this play's capability.

        Delegates to :class:`CapabilityGate` so the precondition-override helper
        and the gate apply the *same* filter — including the circuit-breaker
        exclusion (#22). A hand-rolled copy here previously omitted the
        circuit-broken check, so issue_pickup / groom_backlog could be deemed
        eligible on an agent the breaker had marked dead.
        """
        from agentshore.plays.skill_backed.gates import CapabilityGate  # noqa: PLC0415

        cap_key = self.capability
        if cap_key is None:
            return []
        reason = CapabilityGate(cap_key)(state)
        return [reason] if reason is not None else []

    def _is_trunk_scoped_dispatch(self, dispatch_cwd: Path | None, project_path: Path) -> bool:
        """True when this play dispatches into the main checkout and is a trunk type.

        Only the trunk-scoped play types can leave untracked root artifacts (they
        run their agent in the main repo, not an isolated worktree). ``None`` cwd
        means the dispatcher falls back to ``handle.working_dir``, which for these
        plays is the main repo; an explicit cwd must equal the project path.
        """
        from agentshore.core.trunk_artifacts import TRUNK_SCOPED_PLAY_TYPES

        if self.play_type not in TRUNK_SCOPED_PLAY_TYPES:
            return False
        if dispatch_cwd is None:
            return True
        try:
            return dispatch_cwd.resolve() == project_path.resolve()
        except OSError:
            return False

    def _cwd_is_main_checkout(self, dispatch_cwd: Path, project_path: Path) -> bool:
        """True when *dispatch_cwd* resolves to the main repo checkout."""
        try:
            return dispatch_cwd.resolve() == project_path.resolve()
        except OSError:
            return False

    def _inject_worktree_guards(
        self, extra_context: dict[str, object], ctx: PlayExecutionContext
    ) -> None:
        """Inject the protected-worktree lists a destructive worktree sweep must honour.

        Shared by PRUNE and RECONCILE_STATE: both delete worktrees and must keep any
        that (a) carry a live work claim or (b) are too young to prove stale. The age
        guard is the load-bearing protection against the allocate-then-delete race
        (#189, #218) — a worktree created inside the min-age window is kept regardless
        of claim-query freshness. Best-effort: a missing list is the conservative
        outcome (skip nothing extra) and never blocks the dispatch.
        """
        from agentshore.core.wedge_signals import (
            collect_active_worktree_paths,
            collect_recent_worktree_paths,
        )

        extra_context["worktree_min_age_hours"] = _WORKTREE_MIN_AGE_HOURS
        try:
            extra_context["active_worktree_paths"] = collect_active_worktree_paths(
                ctx.project_path,
                session_id=ctx.session_id,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; empty list is safe
            _logger.warning(
                "worktree_active_inject_failed",
                error=str(exc),
                play_id=ctx.play_id,
                play_type=str(self.play_type),
            )
        try:
            extra_context["young_worktree_paths"] = collect_recent_worktree_paths(
                ctx.project_path,
                session_id=ctx.session_id,
                min_age_hours=_WORKTREE_MIN_AGE_HOURS,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; empty list is safe
            _logger.warning(
                "worktree_young_inject_failed",
                error=str(exc),
                play_id=ctx.play_id,
                play_type=str(self.play_type),
            )

    def estimated_cost(self, state: OrchestratorState) -> float:
        return 0.10

    # The executor reads this attribute to access requested_mutations.
    _last_skill_result: SkillResult | None = None

    async def execute(
        self,
        state: OrchestratorState,
        params: PlayParams,
        *,
        ctx: PlayExecutionContext,
    ) -> PlayOutcome:
        """Write context, render prompt, dispatch, parse, return outcome."""
        agent_id = params.agent_id
        if agent_id is None:
            return PlayOutcome.failed(self.play_type, "agent_id not resolved before execute")

        # Load top-k learnings for context.json injection
        top_learnings: list[dict[str, object]] = []
        learnings_count = 0
        if ctx.cfg.learnings.inject_into_prompts and ctx.cfg.learnings.enabled:
            try:
                from agentshore.learnings import load, top_k

                path = ctx.project_path / ctx.cfg.learnings.file
                all_entries = await asyncio.to_thread(load, path)
                top = top_k(all_entries, k=ctx.cfg.learnings.max_prompt_entries)
                learnings_count = len(all_entries)
                top_learnings = [
                    {"pattern": e.pattern, "confidence": round(e.confidence, 2)} for e in top
                ]
            except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                _logger.warning("learnings_injection_failed", error=str(exc))

        assigned_identity: str | None = None
        for agent in state.agents:
            if agent.agent_id == agent_id:
                assigned_identity = agent.github_identity
                break

        review_patterns: list[dict[str, object]] = []
        if self.play_type in _REVIEW_PATTERN_INJECTION_PLAYS:
            try:
                all_patterns = await ctx.store.list_review_patterns(ctx.session_id)
                top_patterns = all_patterns[: ctx.cfg.learnings.max_prompt_entries]
                review_patterns = [
                    {
                        "pattern": p.pattern,
                        "category": p.category,
                        "frequency": p.frequency,
                    }
                    for p in top_patterns
                ]
                pattern_ids = [p.pattern_id for p in top_patterns if isinstance(p.pattern_id, int)]
                if pattern_ids:
                    await ctx.store.mark_review_patterns_injected(ctx.session_id, pattern_ids)
            except (AttributeError, TypeError, ValueError) as exc:
                _logger.warning("review_pattern_injection_failed", error=str(exc))

        context_relative_path = play_context_relative_path(ctx.play_id, session_id=ctx.session_id)

        extra_context: dict[str, object] = {"review_patterns": review_patterns}
        if self.play_type == PlayType.RECONCILE_STATE:
            # Pre-write structured diagnostic signals so the skill can
            # diagnose wedge pathologies (dirty trunk, orphan worktrees,
            # recent failed plays) without re-deriving them from the
            # log/DB inside the agent prompt. See ``agentshore/core/wedge_signals.py``.
            from agentshore.core.wedge_signals import build_recent_wedge_signals

            try:
                extra_context["recent_wedge_signals"] = build_recent_wedge_signals(
                    state,
                    ctx.project_path,
                    session_id=ctx.session_id,
                )
            except Exception as exc:  # noqa: BLE001 — diagnostic is best-effort
                _logger.warning(
                    "reconcile_state_wedge_signals_failed",
                    error=str(exc),
                    play_id=ctx.play_id,
                )
            # RECONCILE_STATE also removes worktrees (orphan + #214 divergent-
            # stale paths), so it needs the same active-claim + age guards prune
            # uses — without them its diagnose-then-remediate-later flow can delete
            # a worktree allocated mid-run to a freshly dispatched agent (#218).
            self._inject_worktree_guards(extra_context, ctx)
        elif self.play_type == PlayType.PRUNE:
            # Inject the set of currently-claimed / too-young worktrees so the
            # skill skips them, even when they have no pushed branch yet. Without
            # this, active pickup worktrees look like orphans (no open PR, no
            # commits beyond target) and get deleted mid-play.
            self._inject_worktree_guards(extra_context, ctx)

        # Write isolated context so concurrent plays cannot read each other's state.
        payload = serialize_state_for_skill(
            session_id=ctx.session_id,
            play_id=ctx.play_id,
            play_type=self.play_type,
            skill_name=self.skill_name,
            params=params,
            open_issues=state.open_issues,
            budget_enabled=state.budget.enabled if state.budget else ctx.cfg.budget.enabled,
            budget_total=state.budget.total_budget if state.budget else ctx.cfg.budget.total,
            budget_spent=state.budget.spent if state.budget else 0.0,
            learnings_count=learnings_count,
            pull_requests=state.pull_requests,
            top_learnings=top_learnings,
            mode=ctx.cfg.mode,
            assigned_github_identity=assigned_identity,
            target_branch=ctx.cfg.project.target_branch,
            project_path=str(ctx.project_path.resolve()),
            extra=extra_context,
        )
        await asyncio.to_thread(
            write_play_context,
            ctx.project_path,
            payload,
            context_relative_path=context_relative_path,
        )

        dispatch_cwd = _worktree_cwd_override(params)

        cached_retry_prompt = params.extras.get("__retry_prompt")
        if isinstance(cached_retry_prompt, str) and cached_retry_prompt:
            prompt = cached_retry_prompt
        else:
            prompt = await render_skill_prompt(
                self.skill_name,
                params,
                project_path=ctx.project_path,
                context_path=context_relative_path,
                dispatch_cwd=dispatch_cwd,
            )

        claim_group_id_raw = params.extras.get("claim_group_id")
        if isinstance(claim_group_id_raw, str) and claim_group_id_raw:
            await ctx.store.save_dispatch_replay(
                session_id=ctx.session_id,
                claim_group_id=claim_group_id_raw,
                play_id=ctx.play_id,
                skill_name=self.skill_name,
                params_json=json.dumps(params_to_json_safe_dict(params)),
                prompt=prompt,
                branch=params.branch,
            )

        # Worktree-isolation guard for PR-scoped / branch-creating plays. Their
        # agent creates/switches branches, which MUST happen inside an allocated
        # worktree — never the main checkout, where ``git switch -c`` moves the
        # main repo's HEAD onto a feature branch and wedges the trunk-dispatch
        # guard (the contamination behind the #175 wedge).
        from agentshore.agents.worktree.manager import requires_isolated_worktree

        if requires_isolated_worktree(self.play_type):
            if dispatch_cwd is not None and self._cwd_is_main_checkout(
                dispatch_cwd, ctx.project_path
            ):
                # Unambiguous misroute: a main/trunk allocation was handed to an
                # isolation-requiring play. The allocator never does this today,
                # so refuse loudly rather than contaminate trunk if it regresses.
                _logger.error(
                    "play_misrouted_to_main_checkout",
                    play_type=self.play_type.value,
                    play_id=ctx.play_id,
                    agent_id=agent_id,
                    project_path=str(ctx.project_path),
                )
                return PlayOutcome(
                    play_type=self.play_type,
                    agent_id=agent_id,
                    success=False,
                    partial=False,
                    duration_seconds=0.0,
                    token_cost=0,
                    dollar_cost=0.0,
                    artifacts=[],
                    alignment_delta=0.0,
                    error=(
                        f"{self.play_type.value} requires an isolated worktree but its "
                        "allocation resolved to the main checkout; refused to dispatch to "
                        "avoid moving the main repo HEAD off the default branch"
                    ),
                    failure_kind=None,
                )
            if dispatch_cwd is None:
                # No allocation reached us — the dispatcher's ``_runtime_allocation``
                # stamp was lost (a replay/retry rebuilt ``PlayParams``, or a legacy
                # caller). ``dispatch_cli`` will fall back to ``handle.working_dir``
                # (the main checkout), so surface the hypothesized contamination
                # vector for telemetry. We do not hard-fail here — ``None`` is the
                # documented legacy fallback of ``_worktree_cwd_override`` — and
                # ``restore_default_branch`` (#175) now recovers any HEAD move this
                # causes instead of latching a permanent dispatch pause.
                _logger.warning(
                    "play_dispatch_no_worktree_allocation",
                    play_type=self.play_type.value,
                    play_id=ctx.play_id,
                    agent_id=agent_id,
                )

        # Snapshot untracked root files before a trunk-scoped dispatch so we can
        # reclaim any the agent leaves behind (#162/#164). Only meaningful when
        # the play runs in the main checkout, not an isolated worktree.
        trunk_artifact_pre: set[str] | None = None
        if self._is_trunk_scoped_dispatch(dispatch_cwd, ctx.project_path):
            from agentshore.core.trunk_artifacts import snapshot_untracked_root_artifacts

            try:
                trunk_artifact_pre = snapshot_untracked_root_artifacts(ctx.project_path)
            except Exception as exc:  # noqa: BLE001 — best-effort diagnostic
                _logger.warning(
                    "trunk_artifact_presnapshot_failed", error=str(exc), play_id=ctx.play_id
                )

        # Graceful guard for the worktree-reclaim TOCTOU race (#176): the
        # allocated worktree can be removed by reconcile / collision-reclaim
        # churn between allocation and this dispatch. If the resolved cwd is gone,
        # short-circuit to a recoverable failure rather than letting the spawn
        # raise (which ``cli_agent`` now maps to AgentProcessCrashed anyway — this
        # is the cheaper, no-spawn path). PPO re-picks cleanly on the next tick.
        if dispatch_cwd is not None and not dispatch_cwd.exists():
            _logger.warning(
                "play_dispatch_cwd_reclaimed",
                play_type=self.play_type.value,
                play_id=ctx.play_id,
                agent_id=agent_id,
                dispatch_cwd=str(dispatch_cwd),
            )
            return PlayOutcome.failed(
                self.play_type,
                error=(f"worktree reclaimed before dispatch: {dispatch_cwd} no longer exists"),
                agent_id=agent_id,
                retry_requested=True,
                failure_kind=FailureKind.AGENT_ERROR,
            )

        invocation = await ctx.manager.dispatch(
            agent_id,
            prompt,
            capability=self.capability,
            play_type=self.play_type.value,
            cwd_override=dispatch_cwd,
        )

        # desktop no-op resilience: a clean-exit empty no-op (agy returns an empty
        # task envelope — exit 0, no output) is a transient agy/backend flake, not
        # real work. Re-dispatch FRESH (no --resume; an empty session resumes empty)
        # up to _NOOP_STREAK_LIMIT times. Any attempt that produces output recovers
        # the play; _NOOP_STREAK_LIMIT consecutive no-ops is treated like a quota
        # limit — the agent takes a standard break and the play fails for re-pick.
        if is_noop_invocation(invocation):
            attempt = 1
            _logger.info(
                "agent_noop",
                agent_id=agent_id,
                play_type=self.play_type.value,
                attempt=attempt,
                duration_ms=invocation.duration_ms,
            )
            while is_noop_invocation(invocation) and attempt < _NOOP_STREAK_LIMIT:
                # Same worktree-reclaim TOCTOU window the json-retry guards below.
                if dispatch_cwd is not None and not dispatch_cwd.exists():
                    return PlayOutcome.failed(
                        self.play_type,
                        error=(
                            f"worktree reclaimed before no-op retry: {dispatch_cwd} "
                            "no longer exists"
                        ),
                        agent_id=agent_id,
                        retry_requested=True,
                        failure_kind=FailureKind.AGENT_ERROR,
                    )
                retry_invocation = await ctx.manager.dispatch(
                    agent_id,
                    prompt,
                    capability=self.capability,
                    play_type=self.play_type.value,
                    cwd_override=dispatch_cwd,
                )
                invocation = _merge_invocation_costs(invocation, retry_invocation)
                attempt += 1
                if is_noop_invocation(invocation):
                    _logger.info(
                        "agent_noop",
                        agent_id=agent_id,
                        play_type=self.play_type.value,
                        attempt=attempt,
                        duration_ms=retry_invocation.duration_ms,
                    )
            recovered = not is_noop_invocation(invocation)
            _logger.info(
                "agent_noop_retry_outcome",
                agent_id=agent_id,
                play_type=self.play_type.value,
                recovered=recovered,
                attempts=attempt,
            )
            if not recovered:
                # _NOOP_STREAK_LIMIT in a row: route the agent into the standard
                # take_break via a recoverable NO_OP error, then fail for re-pick.
                await ctx.manager.mark_agent_error(
                    agent_id,
                    ErrorClass.NO_OP,
                    f"agent produced no output on {attempt} consecutive dispatches (no-op)",
                )
                return PlayOutcome.failed(
                    self.play_type,
                    error=(
                        "no valid result block found in agent output (agent produced no "
                        f"output on {attempt} consecutive dispatches)"
                    ),
                    agent_id=agent_id,
                    retry_requested=True,
                    failure_kind=FailureKind.AGENT_ERROR,
                )

        # Parse the raw result block emitted by the skill
        skill_result = parse_skill_result(invocation.raw_output)

        # desktop-dy2j: single bounded retry when the agent produced output but
        # omitted the structured JSON envelope. Covers both a clean exit that
        # forgot the envelope and a post-response idle kill (exit_code None) that
        # salvaged a non-envelope line — both leave a resumable session, which is
        # the only real prerequisite. This is the narrow exception to the
        # --resume ban — see feedback_persistent_sessions for the general rule.
        if (
            not skill_result.success
            and skill_result.error
            and "no valid result block" in skill_result.error
            and invocation.session_id is not None
            and len(invocation.raw_output) > 0
        ):
            # Worktree may be reclaimed between initial dispatch return and here
            # (same TOCTOU window the pre-dispatch guard at line 437 covers).
            if dispatch_cwd is not None and not dispatch_cwd.exists():
                _logger.warning(
                    "play_dispatch_cwd_reclaimed",
                    play_type=self.play_type.value,
                    play_id=ctx.play_id,
                    agent_id=agent_id,
                    dispatch_cwd=str(dispatch_cwd),
                    during="json_retry",
                )
                return PlayOutcome.failed(
                    self.play_type,
                    error=f"worktree reclaimed before json retry: {dispatch_cwd} no longer exists",
                    agent_id=agent_id,
                    retry_requested=True,
                    failure_kind=FailureKind.AGENT_ERROR,
                )
            # #236: agy manage_task handoff — agent delegated work async instead of
            # completing it; the work is unfinished so we cannot ask for re-emission.
            # #229: near-miss — JSON present but no top-level boolean ``success``.
            # Otherwise: generic "emit the JSON block" nudge.
            is_manage_task = cli_antigravity.is_manage_task_handoff(invocation.raw_output)
            if is_manage_task:
                retry_prompt = _JSON_RETRY_ASYNC_HANDOFF_PROMPT
            elif skill_result.missing_success_envelope:
                retry_prompt = _JSON_RETRY_MISSING_SUCCESS_PROMPT
            else:
                retry_prompt = _JSON_RETRY_PROMPT
            _logger.info(
                "agent_json_retry",
                agent_id=agent_id,
                play_type=self.play_type.value,
                session_id=invocation.session_id,
                original_output_length=len(invocation.raw_output),
                missing_success_envelope=skill_result.missing_success_envelope,
                manage_task_handoff=is_manage_task,
            )
            retry_invocation = await ctx.manager.dispatch(
                agent_id,
                retry_prompt,
                capability=self.capability,
                play_type=self.play_type.value,
                cwd_override=dispatch_cwd,
                resume_session_id=invocation.session_id,
                # #232: a re-emission should stream promptly — don't inherit agy's
                # 1800s fresh-task first-byte deadline; fast-fail instead.
                # #236: manage_task handoffs require completing real work, not just
                # re-printing — let them inherit the full per-agent-type deadline.
                first_byte_timeout_override=(None if is_manage_task else _JSON_RETRY_FIRST_BYTE_S),
            )
            retry_result = parse_skill_result(retry_invocation.raw_output)
            _logger.info(
                "agent_json_retry_outcome",
                agent_id=agent_id,
                play_type=self.play_type.value,
                success=retry_result.success,
                retry_output_length=len(retry_invocation.raw_output),
            )
            if retry_result.success or "no valid result block" not in (retry_result.error or ""):
                skill_result = retry_result
            # Accumulate retry cost into total
            invocation = _merge_invocation_costs(invocation, retry_invocation)

        self._last_skill_result = skill_result

        # Reclaim untracked root files this trunk-scoped play introduced and left
        # behind, so they don't wedge merge_pr / reconcile_state (#162/#164).
        if trunk_artifact_pre is not None:
            await _reclaim_trunk_artifacts_for_play(ctx, self.play_type, trunk_artifact_pre)

        failure_kind: FailureKind | None = None
        if not skill_result.success and _looks_like_auth_failure(skill_result.error):
            failure_kind = FailureKind.AUTH
            await ctx.manager.mark_agent_error(
                agent_id,
                "auth",
                skill_result.error or "skill reported GitHub authentication failure",
            )

        return PlayOutcome(
            play_type=self.play_type,
            agent_id=agent_id,
            success=skill_result.success,
            partial=False,
            duration_seconds=invocation.duration_ms / 1000.0,
            token_cost=invocation.tokens_in + invocation.tokens_out,
            dollar_cost=invocation.dollar_cost,
            artifacts=skill_result.artifacts,
            alignment_delta=0.0,
            error=skill_result.error,
            failure_kind=failure_kind,
        )


async def _reclaim_trunk_artifacts_for_play(
    ctx: PlayExecutionContext, play_type: PlayType, pre: set[str]
) -> None:
    """Quarantine untracked root files this trunk-scoped play introduced.

    Diffs a post-dispatch snapshot against *pre*; the delta is the set of
    top-level scratch files the play created and left untracked. Reclaim is
    deferred (skipped) when another trunk-scoped play is concurrently in flight,
    because the new file's ownership is then ambiguous across the overlapping
    plays (#162) — the session-start sweep resolves those deterministically by
    DB window. Best-effort: never raises, never affects the play outcome.
    """
    try:
        from agentshore.core.trunk_artifacts import (
            TRUNK_SCOPED_PLAY_TYPES,
            reclaim_artifacts,
            snapshot_untracked_root_artifacts,
        )
        from agentshore.data.models import ExternalMutationRecord
        from agentshore.utils import now_iso

        new = snapshot_untracked_root_artifacts(ctx.project_path) - pre
        if not new:
            return
        concurrent = await ctx.store.count_running_trunk_plays(
            ctx.session_id,
            exclude_play_id=ctx.play_id,
            play_types=[pt.value for pt in TRUNK_SCOPED_PLAY_TYPES],
        )
        if concurrent > 0:
            _logger.info(
                "trunk_artifact_reclaim_deferred",
                play_id=ctx.play_id,
                play_type=play_type.value,
                candidate_count=len(new),
                concurrent_trunk_plays=concurrent,
            )
            return
        moved = reclaim_artifacts(ctx.project_path, new, play_id=ctx.play_id)
        for rel in moved:
            await ctx.store.record_external_mutation(
                ExternalMutationRecord(
                    session_id=ctx.session_id,
                    play_id=ctx.play_id,
                    idempotency_key=f"reclaim:{ctx.play_id}:{rel}",
                    mutation_type="trunk_artifact_reclaim",
                    target=rel,
                    status="reclaimed",
                    created_at=now_iso(),
                )
            )
        if moved:
            _logger.info(
                "trunk_artifacts_reclaimed",
                play_id=ctx.play_id,
                play_type=play_type.value,
                count=len(moved),
                paths=moved,
            )
    except Exception as exc:  # noqa: BLE001 — reclaim must never fail a play
        _logger.warning("trunk_artifact_reclaim_errored", play_id=ctx.play_id, error=str(exc))


def _merge_invocation_costs(
    original: AgentInvocationResult,
    retry: AgentInvocationResult,
) -> AgentInvocationResult:
    """Combine token/cost metrics from original + retry into one result."""
    from dataclasses import replace

    return replace(
        retry,
        tokens_in=original.tokens_in + retry.tokens_in,
        tokens_out=original.tokens_out + retry.tokens_out,
        cached_tokens_in=original.cached_tokens_in + retry.cached_tokens_in,
        cache_write_tokens_in=original.cache_write_tokens_in + retry.cache_write_tokens_in,
        dollar_cost=original.dollar_cost + retry.dollar_cost,
        duration_ms=original.duration_ms + retry.duration_ms,
    )


def _looks_like_auth_failure(error: str | None) -> bool:
    # Skill error strings are work-product-adjacent free text, so this stays on
    # the high-precision GITHUB_AUTH_ERROR_MARKERS view (phrased forms like
    # "http 403", not the bare "403"/"forbidden" tokens in the broad AUTH_MARKERS
    # superset) to avoid false positives — the same precision rationale as the
    # stdout-vs-stderr split. The view is pinned ⊆ AUTH_MARKERS in
    # tests/test_error_markers.py.
    text = (error or "").lower()
    return any(marker in text for marker in GITHUB_AUTH_ERROR_MARKERS)
