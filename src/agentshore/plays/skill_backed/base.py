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
from agentshore.state import AgentType, PlayOutcome, PlayType, SkillResult

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agentshore.agents.handle import AgentInvocationResult
    from agentshore.plays.base import PlayExecutionContext, PlayParams
    from agentshore.plays.skill_backed.gates import Gate
    from agentshore.rl.mask_reason import MaskReason
    from agentshore.state import OrchestratorState

_logger = structlog.get_logger(__name__)

# Resume-retry nudge (#223): agent finished work but omitted the JSON envelope. The resumed
# session still holds full context, so re-sending the ~10 KB prompt risks redoing work; ask
# only for the missing block and forbid redoing.
_JSON_RETRY_PROMPT = (
    "Your previous turn completed work but did not emit the required JSON result block. "
    "Do not redo or repeat any work. Output only the fenced JSON result block for what "
    "you already did, matching the schema in your instructions."
)

# Near-miss variant (#229/#313): JSON emitted but missing the top-level boolean ``success``
# (``SkillResult.missing_success_envelope``). Naming the exact defect recovers far more than
# the generic nudge, which the agent answers by re-emitting the same shape.
#
# #313: the original wording ("re-emit the exact same JSON, adding success; do not invent
# other keys") was calibrated for a true near-miss — an otherwise-correct envelope one field
# short. agy's dominant mode is not that: it finishes the work and then reports it in an
# ad-hoc shape of its own ({"result": "completed", "pr": "<url>"}), or as a bare payload
# array with no envelope at all. Against that shape the old prompt was actively harmful —
# "do not invent other keys" forbids the very keys the envelope requires, so the retry
# faithfully re-emitted the same unusable object and a real, merged-able PR was discarded.
# It must therefore restate the FULL envelope, not just the one missing field.
_JSON_RETRY_MISSING_SUCCESS_PROMPT = (
    "Your previous turn emitted JSON, but not the result envelope required by your skill "
    "instructions. Do not redo or repeat any work — everything you already did still "
    "counts, and this turn only reports it.\n"
    "Re-emit ONE fenced JSON block describing that same completed work, using the FULL "
    "result envelope:\n"
    '  {"success": <true|false>, "artifacts": [{"type": "<exact type>", ...}], ...}\n'
    "Requirements:\n"
    "- `success` MUST be present at the TOP level and be a JSON boolean (true/false) — "
    "not a string, not nested inside another object.\n"
    "- `artifacts` MUST be a top-level array of objects, each carrying the exact `type` "
    "string your skill instructions specify.\n"
    "- Every other field your skill instructions require (for example `issue_picked_up`, "
    "`branch`) MUST appear at the TOP level of the same envelope.\n"
    "- A bare payload, a bare array, or an ad-hoc object of your own design (for example "
    '{"result": "completed"}) will be REJECTED and the work you already finished will be '
    "discarded.\n"
    "Output only that one fenced JSON block."
)


def _missing_envelope_retry_prompt(required_artifact_types: Sequence[str]) -> str:
    """Return the missing-envelope nudge, naming the exact artifact ``type`` strings.

    The base prompt can only point at "your skill instructions"; when the play declares
    ``required_artifact_types`` we can restate the literal strings its validator
    exact-matches on, which is the defect in #313's occurrence #3 (a complete audit
    payload emitted under ``design-audit-result``).
    """
    if not required_artifact_types:
        return _JSON_RETRY_MISSING_SUCCESS_PROMPT
    types = ", ".join(f'"{t}"' for t in required_artifact_types)
    return (
        f"{_JSON_RETRY_MISSING_SUCCESS_PROMPT}\n"
        f"For this play, `artifacts` MUST contain an object whose `type` is exactly "
        f"{types} — spelled exactly that way, underscores included. Any other spelling "
        "is rejected."
    )


# #236: agy async/background-handoff variant — agent deferred work to an async task instead
# of finishing it. Unlike the generic nudge, the work is unfinished, so it must re-run
# synchronously; this redirects execution style, not scope.
_JSON_RETRY_ASYNC_HANDOFF_PROMPT = (
    "Your previous turn ended by deferring a command to an async or background task and "
    "waiting on it, instead of running it to completion. Do not use manage_task, do not "
    "background commands, and do not pause to wait for a task to finish. Re-run the "
    "remaining work in this turn synchronously — wait for each command to finish before "
    "proceeding — then emit the fenced JSON result block. Do not end this turn until the "
    "JSON block is emitted."
)

# #242: agy auto-backgrounds long commands and ends the ``-p`` turn narrating it is "waiting
# for the background task" (prose, no JSON). Appended to the INITIAL dispatch so the handoff
# is prevented, not just retried (verified Gemini 3.5 Flash + 3.1 Pro: ~0/4 without → ~7/8
# with; residual leak caught by ``cli_antigravity.is_async_handoff``). agy-only — other CLIs
# don't auto-background.
_ANTIGRAVITY_SYNCHRONOUS_DIRECTIVE = (
    "\n\n## Antigravity: run every command synchronously\n\n"
    "Run every shell command in the FOREGROUND and BLOCK until it returns, no matter how "
    "long it takes. Do NOT send commands to the background, do NOT use a task or "
    "manage_task tool, and do NOT pause to 'wait for a background task to finish' or "
    "'wait for a notification' — there is no scheduler that will wake you up. Do NOT end "
    "your turn until every command has returned and you have emitted the fenced JSON "
    "result block."
)


def _with_antigravity_sync_directive(prompt: str, agent_type: AgentType | None) -> str:
    """Append the agy synchronous-execution directive for ANTIGRAVITY dispatches only."""
    if agent_type == AgentType.ANTIGRAVITY:
        return prompt + _ANTIGRAVITY_SYNCHRONOUS_DIRECTIVE
    return prompt


# First-byte deadline for the no-JSON resume-retry (#232). A re-emission should stream in
# seconds; without this it inherits agy's 1800s default (cli_agent._FIRST_BYTE_DEADLINE_BY_TYPE),
# turning a silent resume hang into 30 min of dead slot. Short budget fast-fails (recoverable
# TIMEOUT_STREAM_IDLE). Not applied to async handoffs (#236) — those do real work.
_JSON_RETRY_FIRST_BYTE_S = 120.0

# Why an agent produced output but no usable result envelope. Drives both the retry prompt
# and — when no retry is possible — the classification recorded on the failure (#313).
_ENVELOPE_DEFECT_ASYNC_HANDOFF = "async_handoff"
_ENVELOPE_DEFECT_MISSING_ENVELOPE = "missing_success_envelope"
_ENVELOPE_DEFECT_NO_JSON = "no_json_block"

# Operator-facing diagnosis per defect, appended to the play error when the retry could not
# run. Without this a 19-min dispatch that ended in a textbook async handoff reported only
# the generic "no valid result block", so the session's costliest failure carried neither
# mitigation nor classification (#313, session 16515f9b).
_ENVELOPE_DEFECT_DIAGNOSIS: dict[str, str] = {
    _ENVELOPE_DEFECT_ASYNC_HANDOFF: (
        "diagnosis: agent deferred work to an async/background task and ended the turn "
        "waiting on it instead of completing it and emitting the result envelope (#236)"
    ),
    _ENVELOPE_DEFECT_MISSING_ENVELOPE: (
        "diagnosis: agent emitted JSON without the required result envelope (no top-level "
        "boolean 'success'); completed work may have been reported in an ad-hoc shape (#313)"
    ),
    _ENVELOPE_DEFECT_NO_JSON: ("diagnosis: agent produced output but no JSON result block at all"),
}


def _classify_envelope_defect(raw_output: str, skill_result: SkillResult) -> str:
    """Return the ``_ENVELOPE_DEFECT_*`` label for a missing-result-envelope failure.

    Single source of truth for the defect taxonomy so the retry path and the
    no-retry-possible path (no resumable session id) always agree on the label.
    """
    if cli_antigravity.is_async_handoff(raw_output):
        return _ENVELOPE_DEFECT_ASYNC_HANDOFF
    if skill_result.missing_success_envelope:
        return _ENVELOPE_DEFECT_MISSING_ENVELOPE
    return _ENVELOPE_DEFECT_NO_JSON


# Max consecutive clean-exit empty no-op dispatches before failing the play and routing the
# agent into take_break. First dispatch is attempt 1, so this bounds it at 1 initial + 2
# re-dispatches. Re-dispatches are FRESH (no --resume): an empty agy session resumes empty
# (verified), so only a fresh turn recovers.
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

    # #565: allocation moved off ``params.extras`` (JSON-serializes) onto private
    # ``_runtime_allocation``.
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
# Min worktree age before any destructive sweep may delete it. Shared by PRUNE and
# RECONCILE_STATE; load-bearing guard against the allocate-then-delete race (#189, #218) —
# a worktree created inside this window is protected however stale a claim query looks.
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

    # Artifact ``type`` strings this play's result validator requires, if any. Purely
    # advisory: restated verbatim in the missing-envelope retry nudge so a re-emission
    # uses the exact spelling the validator matches on (#313). Plays with no artifact
    # contract leave this empty and get the generic envelope nudge.
    required_artifact_types: Sequence[str] = ()

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

    async def _inject_worktree_guards(
        self, extra_context: dict[str, object], ctx: PlayExecutionContext
    ) -> None:
        """Inject the protected-worktree lists a destructive worktree sweep must honour.

        Shared by PRUNE and RECONCILE_STATE: both delete worktrees and must keep any
        that (a) carry a live work claim or (b) are too young to prove stale. The age
        guard is the load-bearing protection against the allocate-then-delete race
        (#189, #218) — a worktree created inside the min-age window is kept regardless
        of claim-query freshness. Best-effort: a missing list is the conservative
        outcome (skip nothing extra) and never blocks the dispatch.

        ``active_worktree_paths`` starts from the narrow DB-only
        ``collect_active_worktree_paths`` query (kept for back-compat / cheap
        best-effort) and is WIDENED with ``ctx.manager.worktrees``' hardened
        live-dispatch truth (in-flight registry + active/reaping DB rows) — the
        same protection set the deterministic reaper (Regime 1) already trusts
        via ``_protected_paths``/``_live_alias_paths``. The DB-only query alone
        misses ``status='reaping'`` rows and in-flight dispatches whose
        ``worktrees`` row write hasn't landed yet — the exact gap #311 exploited
        to let the LLM-driven PRUNE/RECONCILE_STATE skill (Regime 2) delete a
        worktree the reaper would have protected. This widening is still purely
        advisory (the skill can ignore it); see ``execute()``'s post-hoc guard
        for the hard backstop.
        """
        from agentshore.core.wedge_signals import (
            collect_active_worktree_paths,
            collect_recent_worktree_paths,
        )

        extra_context["worktree_min_age_hours"] = _WORKTREE_MIN_AGE_HOURS
        active_paths: set[str] = set()
        try:
            active_paths.update(
                collect_active_worktree_paths(
                    ctx.project_path,
                    session_id=ctx.session_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; empty list is safe
            _logger.warning(
                "worktree_active_inject_failed",
                error=str(exc),
                play_id=ctx.play_id,
                play_type=str(self.play_type),
            )
        manager = getattr(ctx, "manager", None)
        worktrees_manager = getattr(manager, "worktrees", None) if manager is not None else None
        if worktrees_manager is not None:
            try:
                active_paths.update(await worktrees_manager.live_protected_paths())
            except Exception as exc:  # noqa: BLE001 — widening is best-effort too
                _logger.warning(
                    "worktree_manager_protected_paths_inject_failed",
                    error=str(exc),
                    play_id=ctx.play_id,
                    play_type=str(self.play_type),
                )
        extra_context["active_worktree_paths"] = sorted(active_paths)
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

    async def _guard_against_protected_worktree_removal(
        self, ctx: PlayExecutionContext, skill_result: SkillResult
    ) -> tuple[SkillResult, bool]:
        """Hard, code-level backstop against a destructive sweep clobbering a live worktree.

        ``_inject_worktree_guards`` only *advises* the LLM-driven PRUNE skill which
        paths to keep — the skill can still misclassify a worktree and run
        ``git worktree remove --force`` on it (#311). Because the skill is an
        opaque CLI subprocess, we cannot intercept the removal mid-flight; the
        realistic enforcement point is post-hoc detection: after the skill
        returns, re-derive the manager's live-protected truth
        (``WorktreeManager.live_protected_rows`` — the same in-flight-registry +
        active/reaping-DB-row union the deterministic reaper already trusts) and
        check whether any of those directories are now missing from disk.

        A missing directory alone is NOT proof of a clobber, though: the DB half
        of that union is bookkeeping, and it goes stale (an already-merged
        worktree can keep an ``active`` row for the rest of the session), so
        every vanished row is first reconciled by
        ``WorktreeManager.reconcile_vanished_protected_rows`` against actual
        liveness — the in-flight dispatch registry plus active work claims on
        the row's issue/PR. Rows with no live work behind them are stale
        bookkeeping: they get retired (``reaped``) and logged at WARNING, and the
        play keeps its success, because a maintenance play must repair such
        imperfections rather than fail on them (#360 — nine already-merged
        worktrees failed a correct prune).

        Rows that ARE backed by live work are the real incident: a worktree the
        manager considers in-flight can only legitimately lose its directory via
        the reaper (which checks protection first) or
        ``finalize_after_dispatch``'s branch-creating cleanup (a narrow
        single-await race). Both are vanishingly unlikely to coincide with this
        check, so those force ``skill_result.success`` to ``False`` (even if the
        skill self-reported success) per #189/#195/#203/#238/#243/#250's
        precedent that a worktree-clobber must never read as a clean success.

        Returns the (possibly amended) ``SkillResult`` and whether a violation
        was detected, so the caller can set an informative ``failure_kind``.
        Best-effort: any error probing manager/disk state leaves the result
        untouched — this is a safety net, not a new failure mode of its own.
        """
        worktrees_manager = getattr(ctx.manager, "worktrees", None)
        if worktrees_manager is None:
            return skill_result, False
        try:
            reconciliation = await worktrees_manager.reconcile_vanished_protected_rows()
        except Exception as exc:  # noqa: BLE001 — safety net must never crash the play
            _logger.warning(
                "prune_protected_worktree_guard_failed",
                error=str(exc),
                play_id=ctx.play_id,
                play_type=str(self.play_type),
            )
            return skill_result, False
        if reconciliation.retired:
            _logger.warning(
                "prune_retired_stale_worktree_rows",
                play_id=ctx.play_id,
                play_type=self.play_type.value,
                retired_paths=sorted(reconciliation.retired.values()),
                reasons={
                    str(wid): reconciliation.reasons.get(wid, "")
                    for wid in sorted(reconciliation.retired)
                },
            )
        clobbered = sorted(reconciliation.in_flight.values())
        if not clobbered:
            return skill_result, False
        _logger.error(
            "prune_removed_protected_worktree",
            play_id=ctx.play_id,
            play_type=self.play_type.value,
            clobbered_paths=clobbered,
        )
        from dataclasses import replace  # noqa: PLC0415

        message = "destructive worktree sweep removed a protected/in-flight worktree: " + ", ".join(
            clobbered
        )
        combined_error = f"{skill_result.error}; {message}" if skill_result.error else message
        return replace(skill_result, success=False, error=combined_error), True

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
        dispatch_agent_type: AgentType | None = None
        for agent in state.agents:
            if agent.agent_id == agent_id:
                assigned_identity = agent.github_identity
                dispatch_agent_type = agent.agent_type
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
            await self._inject_worktree_guards(extra_context, ctx)
        elif self.play_type == PlayType.PRUNE:
            # Inject the set of currently-claimed / too-young worktrees so the
            # skill skips them, even when they have no pushed branch yet. Without
            # this, active pickup worktrees look like orphans (no open PR, no
            # commits beyond target) and get deleted mid-play.
            await self._inject_worktree_guards(extra_context, ctx)

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
            # agy auto-backgrounds long commands and ends the turn waiting (#242);
            # append the synchronous-execution directive to its INITIAL prompt.
            prompt = _with_antigravity_sync_directive(prompt, dispatch_agent_type)

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
        #
        # #313: the session-id prerequisite is now checked *inside* the branch, not as
        # part of its trigger. Guarding the whole branch on it meant an unresolvable
        # conversation id (agy's ``resolve_conversation_id`` returning None) skipped the
        # retry *and* the classification, so a 19-min dispatch that ended in a textbook
        # async handoff fell through to a bare "no valid result block" with no retry
        # event logged at all. The retry still cannot run without a resumable session —
        # we do not fabricate one — but the failure is now named.
        missing_envelope = (
            not skill_result.success
            and skill_result.error is not None
            and "no valid result block" in skill_result.error
            and len(invocation.raw_output) > 0
        )
        if missing_envelope and invocation.session_id is None:
            defect = _classify_envelope_defect(invocation.raw_output, skill_result)
            _logger.warning(
                "agent_json_retry_skipped",
                agent_id=agent_id,
                play_type=self.play_type.value,
                reason="no_resumable_session_id",
                defect=defect,
                original_output_length=len(invocation.raw_output),
                missing_success_envelope=skill_result.missing_success_envelope,
                async_handoff=defect == _ENVELOPE_DEFECT_ASYNC_HANDOFF,
            )
            from dataclasses import replace  # noqa: PLC0415

            skill_result = replace(
                skill_result,
                error=(
                    f"{skill_result.error}; {_ENVELOPE_DEFECT_DIAGNOSIS[defect]}; "
                    "json retry skipped: agent reported no resumable session id"
                ),
            )
        elif missing_envelope and invocation.session_id is not None:
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
            # #236: agy async/background handoff — agent deferred work to an async
            # task and ended the turn waiting on it instead of completing it; the
            # work is unfinished so we cannot ask for re-emission.
            # #229/#313: near-miss — JSON present but no top-level boolean ``success``;
            # ask for the FULL envelope, naming this play's artifact types.
            # Otherwise: generic "emit the JSON block" nudge.
            defect = _classify_envelope_defect(invocation.raw_output, skill_result)
            is_async_handoff = defect == _ENVELOPE_DEFECT_ASYNC_HANDOFF
            if is_async_handoff:
                retry_prompt = _JSON_RETRY_ASYNC_HANDOFF_PROMPT
            elif defect == _ENVELOPE_DEFECT_MISSING_ENVELOPE:
                retry_prompt = _missing_envelope_retry_prompt(self.required_artifact_types)
            else:
                retry_prompt = _JSON_RETRY_PROMPT
            _logger.info(
                "agent_json_retry",
                agent_id=agent_id,
                play_type=self.play_type.value,
                session_id=invocation.session_id,
                original_output_length=len(invocation.raw_output),
                missing_success_envelope=skill_result.missing_success_envelope,
                async_handoff=is_async_handoff,
                defect=defect,
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
                # #236: async/background handoffs require completing real work, not
                # just re-printing — let them inherit the full per-agent-type deadline.
                first_byte_timeout_override=(
                    None if is_async_handoff else _JSON_RETRY_FIRST_BYTE_S
                ),
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

        # Hard backstop for the destructive-sweep skills (PRUNE): even though
        # ``_inject_worktree_guards`` advised the skill which worktrees to keep,
        # the LLM can still ignore it and remove one out from under a live
        # dispatch (#311). Detect that post-hoc and refuse to let it read as a
        # clean success.
        worktree_guard_violated = False
        if self.play_type == PlayType.PRUNE:
            (
                skill_result,
                worktree_guard_violated,
            ) = await self._guard_against_protected_worktree_removal(ctx, skill_result)
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
        elif worktree_guard_violated:
            failure_kind = FailureKind.AGENT_ERROR

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
            learnings=skill_result.learnings,
            learnings_compacted=skill_result.learnings_compacted,
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
