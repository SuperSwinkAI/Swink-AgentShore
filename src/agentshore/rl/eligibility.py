"""Single source of truth for play validity.

The :class:`EligibilityAuthority` owns the A-type validity gates that decide
whether a play is structurally valid right now. It is consumed in two places:

1. To present options to the PPO policy — :meth:`EligibilityAuthority.eligibility`
   produces a pure, snapshot-only :class:`EligibilityReport` whose
   :meth:`EligibilityReport.mask` is the action mask over ``V1_ACTION_ORDER``.
2. To validate a play after the policy selects it — :meth:`EligibilityAuthority.confirm`
   performs one live read and returns a :class:`PlayVerdict`. A confirm
   rejection triggers a clean re-pick (re-mask the action, resample); it is
   never recorded as a plays-table skip row or an RL experience sample.

Policy overlays (circuit breaker, reverse failsafe, drain short-circuit,
reserved-slot zeroing) stay OUT of this module — they live in the mask
pipeline. The authority owns validity only.

Import direction: ``mask.py`` imports from this module and re-exports
``compute_agent_eligibility_mask`` / ``compute_config_mask``. This module must
NOT import ``mask.py`` (that would create a cycle).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from agentshore.agents._selection import allowed_tiers_for
from agentshore.agents.capabilities import AGENT_CAPABILITIES
from agentshore.agents.model_tiers import (
    DEFAULT_MODEL_TIER,
    effective_model_tier_config,
)
from agentshore.errors import ErrorClass
from agentshore.identity_names import canonical_identity_name, same_identity
from agentshore.play_rules import (
    CANDIDATE_REQUIRED_PLAY_TYPES,
    LIVE_CONFIRM_PLAY_TYPES,
    needs_review,
)
from agentshore.plays.candidates import (
    PlayCandidate,
    PlayCandidatePlan,
    build_candidate_plan,
    pr_unblockable,
)
from agentshore.rl.action_space import NUM_ACTIONS, V1_ACTION_ORDER
from agentshore.rl.mask_reason import (
    NOT_AVAILABLE,
    SELECTED_CANDIDATE_NO_LONGER_AVAILABLE,
    MaskClassification,
    MaskReason,
    MaskSource,
)
from agentshore.state import RECOVERABLE_ERROR_CLASSES, AgentStatus, AgentType, PlayType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from numpy.typing import NDArray

    from agentshore.beads import ProjectGraph
    from agentshore.config.models import RuntimeConfig
    from agentshore.plays.base import PlayParams
    from agentshore.plays.registry import PlayRegistry
    from agentshore.rl.action_space import ConfigKey
    from agentshore.state import OrchestratorState

    # Returns a freshly-loaded beads graph (or None on a live-read blip). The
    # one live read ``confirm()`` is permitted, supplied by the dispatch layer
    # which owns the repo path.
    LiveGraphLoader = Callable[[], Awaitable[ProjectGraph | None]]


# ---------------------------------------------------------------------------
# Frozen public interface — the contract other components import.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlayVerdict:
    """The validity decision for a single play type.

    ``valid`` is the authority's verdict. ``reason`` is the typed mask reason
    when invalid (``None`` when valid). ``candidates`` is the concrete target
    set the authority resolved for this play (possibly empty).
    """

    play_type: PlayType
    valid: bool
    reason: MaskReason | None
    candidates: tuple[PlayCandidate, ...]


@dataclass(frozen=True, slots=True)
class EligibilityReport:
    """A full snapshot-only validity report over every play type.

    ``verdicts`` maps each evaluated play type to its :class:`PlayVerdict`.
    ``candidate_plan`` is the shared state-only candidate plan the report was
    built from. :meth:`mask` projects the verdicts onto the action-space order.
    """

    verdicts: dict[PlayType, PlayVerdict]
    candidate_plan: PlayCandidatePlan

    def mask(self) -> NDArray[np.bool_]:
        """Return a (NUM_ACTIONS,) bool array over ``V1_ACTION_ORDER``.

        Entry ``i`` is True iff the verdict for ``V1_ACTION_ORDER[i]`` exists
        and is ``valid``. Play types with no verdict are False.
        """
        mask = np.zeros(NUM_ACTIONS, dtype=bool)
        for i, play_type in enumerate(V1_ACTION_ORDER):
            verdict = self.verdicts.get(play_type)
            if verdict is not None and verdict.valid:
                mask[i] = True
        return mask


# ---------------------------------------------------------------------------
# Per-play candidate-validity functions (moved out of the play classes).
#
# Each ``fn(state, plan) -> list[MaskReason]`` returns the validity-blocking
# reasons for one play type, reading the *snapshot* candidate plan rather than
# rebuilding it. An empty list means valid. These are the canonical A-type
# candidate gates the play classes used to derive inline; the authority now
# owns them and the play preconditions delegate.
# ---------------------------------------------------------------------------


def _candidate_reason(plan: PlayCandidatePlan, play_type: PlayType, fallback: str) -> MaskReason:
    """Build a HARD candidate reason, preferring the plan's blocked-reason text."""
    blocked = plan.blocked_reasons_by_play_type.get(play_type, ())
    text = blocked[0] if blocked else fallback
    return MaskReason(
        text=text,
        classification=MaskClassification.HARD,
        source=MaskSource.CANDIDATE,
    )


def _write_plan_validity(state: OrchestratorState, plan: PlayCandidatePlan) -> list[MaskReason]:
    """Validity for WRITE_IMPLEMENTATION_PLAN (moved from write_plan.py)."""
    issues: list[MaskReason] = []
    if not any(issue.state.upper() == "OPEN" for issue in state.open_issues):
        issues.append(
            MaskReason(
                text="no open issues available to plan",
                classification=MaskClassification.HARD,
                source=MaskSource.PRECONDITION,
            )
        )
    if not plan.candidates_for(PlayType.WRITE_IMPLEMENTATION_PLAN):
        issues.append(
            _candidate_reason(
                plan,
                PlayType.WRITE_IMPLEMENTATION_PLAN,
                "no eligible issue for write_implementation_plan"
                " (all covered by open PR, in-flight, or labeled out)",
            )
        )
    return issues


def _systematic_debugging_validity(
    state: OrchestratorState, plan: PlayCandidatePlan
) -> list[MaskReason]:
    """Validity for SYSTEMATIC_DEBUGGING (moved from systematic_debugging.py)."""
    if not plan.candidates_for(PlayType.SYSTEMATIC_DEBUGGING):
        return [
            _candidate_reason(
                plan,
                PlayType.SYSTEMATIC_DEBUGGING,
                "no explicit QA/debug issue available (all in-flight, PR-linked, or none exist)",
            )
        ]
    return []


def _merge_pr_validity(state: OrchestratorState, plan: PlayCandidatePlan) -> list[MaskReason]:
    """Validity for MERGE_PR (moved from merge_pr.py).

    The PR must be approved AND confirmed mergeable. When the plan has no
    merge candidate, distinguish the wrong-base hold from the generic
    awaiting-review/CI case so the operator sees a deterministically held PR.
    """
    if plan.candidates_for(PlayType.MERGE_PR):
        return []
    wrong_base = [
        pr
        for pr in state.pull_requests
        if state.target_branch
        and isinstance(getattr(pr, "base_ref", None), str)
        and pr.base_ref
        and pr.base_ref != state.target_branch
    ]
    if wrong_base:
        nums = ", ".join(f"#{pr.pr_number}" for pr in wrong_base)
        return [
            MaskReason(
                text=(
                    f"PR(s) {nums} target a base other than '{state.target_branch}' "
                    "— held until base is corrected"
                ),
                classification=MaskClassification.HARD,
                source=MaskSource.CANDIDATE,
            )
        ]
    return [
        _candidate_reason(
            plan,
            PlayType.MERGE_PR,
            "no PR with GitHub or AgentShore approval at current head_sha "
            "and mergeable=MERGEABLE (awaiting review or CI)",
        )
    ]


def _unblock_pr_validity(state: OrchestratorState, plan: PlayCandidatePlan) -> list[MaskReason]:
    """Validity for UNBLOCK_PR (moved from unblock_pr.py)."""
    if plan.candidates_for(PlayType.UNBLOCK_PR):
        return []
    in_flight_pr_numbers: set[int] = {
        s.current_play_pr_number
        for s in state.agents
        if s.current_play_type == PlayType.UNBLOCK_PR and s.current_play_pr_number is not None
    }
    # When every unblockable PR is already being worked, the gate is transient
    # (clears when the in-flight unblock completes), not a hard no-candidate.
    has_unblockable = any(
        pr_unblockable(pr) and pr.pr_number in in_flight_pr_numbers for pr in state.pull_requests
    )
    if has_unblockable and in_flight_pr_numbers:
        in_flight_count = len(in_flight_pr_numbers)
        return [
            MaskReason(
                text=f"all blocked PRs already in flight ({in_flight_count} being worked)",
                classification=MaskClassification.TRANSIENT,
                source=MaskSource.PRECONDITION,
            )
        ]
    return [
        _candidate_reason(
            plan,
            PlayType.UNBLOCK_PR,
            "no blocked PRs (no open PR with merge conflicts, CI failures, or block labels)",
        )
    ]


def _code_review_validity(state: OrchestratorState, plan: PlayCandidatePlan) -> list[MaskReason]:
    """Validity for CODE_REVIEW (moved from code_review.py preconditions head).

    Anti-confirmation viability (reviewer identity != PR author) is enforced by
    the agent-eligibility stage; here we only gate on review work existing.
    """
    has_work = bool(state.pending_review_queue) or any(
        not pr.is_draft and needs_review(pr) for pr in state.pull_requests
    )
    if not has_work:
        return [
            MaskReason(
                text="no pending reviews and no unreviewed or stale-review open PRs",
                classification=MaskClassification.HARD,
                source=MaskSource.CANDIDATE,
            )
        ]
    if not plan.candidates_for(PlayType.CODE_REVIEW):
        return [
            _candidate_reason(
                plan,
                PlayType.CODE_REVIEW,
                "no reviewable PR candidate (all in-flight or resource-locked)",
            )
        ]
    return []


def _idle_audit_validity(
    play_type: PlayType,
) -> Callable[[OrchestratorState, PlayCandidatePlan], list[MaskReason]]:
    """Build an idle-validity gate for a trunk audit play (CALIBRATE/DESIGN_AUDIT).

    These plays have no candidate set; their preconditions (beads-init, cooldown,
    in-flight, warmup gates) already govern *when* they may run. The authority
    adds an idle gate: once the session has reached terminal no-work AND the
    audit's own freshness window is satisfied, re-running it is a no-op churn, so
    it is masked. While actionable work or freshness gaps remain the gate is open
    and the registry precondition / cooldown decides.
    """

    def _fn(state: OrchestratorState, plan: PlayCandidatePlan) -> list[MaskReason]:
        availability = plan.work_availability
        if availability.terminal_no_work and not plan.has_remaining_work:
            return [
                MaskReason(
                    text=f"{play_type.value} idle: terminal no-work with fresh audits",
                    classification=MaskClassification.INDEFINITE_WAIT,
                    source=MaskSource.PRECONDITION,
                )
            ]
        return []

    return _fn


# Module-level validity-function table. Each entry maps a play type to a pure
# function that returns the list of validity-blocking reasons for that play
# given the state and the shared candidate plan (empty list == valid).
#
# RECONCILE_STATE is intentionally absent: its ``ArmedByFailureGate`` owns the
# arming decision and there is no candidate gate to add.
_VALIDITY_FNS: dict[
    PlayType, Callable[[OrchestratorState, PlayCandidatePlan], list[MaskReason]]
] = {
    PlayType.WRITE_IMPLEMENTATION_PLAN: _write_plan_validity,
    PlayType.SYSTEMATIC_DEBUGGING: _systematic_debugging_validity,
    PlayType.MERGE_PR: _merge_pr_validity,
    PlayType.UNBLOCK_PR: _unblock_pr_validity,
    PlayType.CODE_REVIEW: _code_review_validity,
    PlayType.CALIBRATE_ALIGNMENT: _idle_audit_validity(PlayType.CALIBRATE_ALIGNMENT),
    PlayType.DESIGN_AUDIT: _idle_audit_validity(PlayType.DESIGN_AUDIT),
}


class EligibilityAuthority:
    """Single source of truth for A-type play validity.

    Construct once per tick with the current state and play registry. The
    candidate plan is built from ``state`` unless injected (hybrid-data
    contract: pure-snapshot eligibility + one live confirm).
    """

    def __init__(
        self,
        state: OrchestratorState,
        registry: PlayRegistry,
        *,
        cfg: RuntimeConfig | None = None,
        config_index: tuple[ConfigKey, ...] | None = None,
        candidate_plan: PlayCandidatePlan | None = None,
        live_graph_loader: LiveGraphLoader | None = None,
    ) -> None:
        self._state = state
        self._registry = registry
        self._cfg = cfg
        self._config_index = config_index
        self._candidate_plan = candidate_plan or build_candidate_plan(state)
        # The one live read confirm() may perform. None (e.g. in the mask path
        # or tests) makes confirm() fall back to the snapshot — still correct,
        # just without fresh-beads drift detection.
        self._live_graph_loader = live_graph_loader

    @property
    def candidate_plan(self) -> PlayCandidatePlan:
        return self._candidate_plan

    def eligibility(self) -> EligibilityReport:
        """Return the pure, snapshot-only validity report (no live reads).

        Composes the A-type validity stages that previously lived in the mask
        pipeline (``_stage_preconditions``, ``_stage_agent_eligibility``,
        ``_stage_candidate_required``, ``_stage_instantiate_config``,
        ``_stage_end_session``, ``_stage_take_break``, ``_stage_wedged_end_agent``)
        plus the per-play ``_VALIDITY_FNS``. B-type policy overlays (circuit
        breaker, reserved slots, drain, reverse failsafe) are NOT applied here —
        they stay in ``mask.py``.
        """
        state = self._state
        plan = self._candidate_plan
        agent_mask = (
            compute_agent_eligibility_mask(state, self._registry, cfg=self._cfg)
            if self._cfg is not None
            else None
        )

        verdicts: dict[PlayType, PlayVerdict] = {}
        for i, pt in enumerate(V1_ACTION_ORDER):
            reason = self._validity_reason_for(pt, state, plan, agent_mask, i)
            verdicts[pt] = PlayVerdict(
                play_type=pt,
                valid=reason is None,
                reason=reason,
                candidates=plan.candidates_for(pt),
            )
        return EligibilityReport(verdicts=verdicts, candidate_plan=plan)

    def _validity_reason_for(
        self,
        pt: PlayType,
        state: OrchestratorState,
        plan: PlayCandidatePlan,
        agent_mask: NDArray[np.bool_] | None,
        idx: int,
    ) -> MaskReason | None:
        """Return the first A-type validity reason for ``pt``, or None if valid."""
        # _stage_wedged_end_agent re-enable: a recovery-exhausted agent lifts
        # END_AGENT even when its registry precondition would mask it (this is
        # the sole re-enable — it hands the retire decision to the policy). It
        # MUST be evaluated before the precondition early-return below, otherwise
        # the precondition reason returns first and the re-enable is unreachable.
        wedged_end_agent_reenable = pt == PlayType.END_AGENT and (
            bool(state.recovery_exhausted_agent_ids) or self._has_terminal_error_agent(state)
        )

        # 1. Registry preconditions (runs each play's declared gates).
        precondition_reason = self._precondition_reason(pt, state)
        if precondition_reason is not None and not wedged_end_agent_reenable:
            return precondition_reason

        # 2. Agent eligibility (tier / exclude / capability / anti-confirmation).
        if agent_mask is not None and not bool(agent_mask[idx]):
            return self._eligibility_reason(pt, state)

        # 3. Per-play candidate-validity functions (moved out of play classes).
        validity_fn = _VALIDITY_FNS.get(pt)
        if validity_fn is not None:
            fn_reasons = validity_fn(state, plan)
            if fn_reasons:
                return fn_reasons[0]

        # 4. Candidate-required plays: no concrete target → masked.
        if pt in CANDIDATE_REQUIRED_PLAY_TYPES and not plan.candidates_for(pt):
            return _candidate_reason(plan, pt, f"no {pt.value} candidates")

        # 5. INSTANTIATE_AGENT config viability.
        if pt == PlayType.INSTANTIATE_AGENT:
            return self._instantiate_config_reason(state, plan)

        # 6. END_SESSION while actionable work remains, or while the beads
        #    backlog is non-empty. GitHub workable-issue counts can lag
        #    calibrate_alignment syncs; ready_task_count is authoritative.
        if pt == PlayType.END_SESSION and (
            not plan.work_availability.terminal_no_work
            or plan.work_availability.ready_task_count > 0
        ):
            return MaskReason(
                text="Actionable work still remains",
                classification=MaskClassification.INDEFINITE_WAIT,
                source=MaskSource.TERMINAL,
            )

        # 7. TAKE_BREAK unless an agent is in rate_limit/unknown error.
        if pt == PlayType.TAKE_BREAK and not self._has_break_trigger(state):
            return MaskReason(
                text="No agent in rate_limit or unknown-error state",
                classification=MaskClassification.HARD,
                source=MaskSource.PRECONDITION,
            )

        # END_AGENT with a recovery-exhausted agent reaches here only via the
        # wedged re-enable (precondition reason was bypassed above) → valid.
        return None

    def _precondition_reason(self, pt: PlayType, state: OrchestratorState) -> MaskReason | None:
        """Return the first unmet registry precondition for ``pt``, or None."""
        try:
            play = self._registry.get(pt)
        except KeyError:
            return NOT_AVAILABLE
        try:
            unmet = play.preconditions(state)
        except (ValueError, AttributeError, RuntimeError):
            return NOT_AVAILABLE
        return unmet[0] if unmet else None

    def _eligibility_reason(self, pt: PlayType, state: OrchestratorState) -> MaskReason:
        """Return a typed reason explaining why the agent-eligibility gate fired."""
        try:
            play = self._registry.get(pt)
        except KeyError:
            return MaskReason(
                text="No play registered",
                classification=MaskClassification.HARD,
                source=MaskSource.ELIGIBILITY,
            )
        cap_key = play.capability
        if cap_key is None:
            return NOT_AVAILABLE

        allowed_tiers = allowed_tiers_for(pt)
        excluded_types = (
            set(self._cfg.agent_preferences.exclude.get(pt.value, []))
            if self._cfg is not None
            else set()
        )
        idle = [a for a in state.agents if a.status == AgentStatus.IDLE]
        if not idle:
            return MaskReason(
                text="No IDLE agents",
                classification=MaskClassification.TRANSIENT,
                source=MaskSource.ELIGIBILITY,
            )
        tier_ok = [
            a
            for a in idle
            if allowed_tiers is None or (a.model_tier or DEFAULT_MODEL_TIER) in allowed_tiers
        ]
        if not tier_ok:
            tier_str = "|".join(sorted(allowed_tiers)) if allowed_tiers else "any"
            return MaskReason(
                text=f"No IDLE agent of allowed tier ({tier_str})",
                classification=MaskClassification.TRANSIENT,
                source=MaskSource.ELIGIBILITY,
            )
        excl_ok = [a for a in tier_ok if a.agent_type.value not in excluded_types]
        if not excl_ok:
            return MaskReason(
                text=f"No IDLE agent type permitted by exclude rule for {pt.value!r}",
                classification=MaskClassification.TRANSIENT,
                source=MaskSource.ELIGIBILITY,
            )
        cap_ok = [
            a for a in excl_ok if bool(AGENT_CAPABILITIES.get(a.agent_type, {}).get(cap_key, False))
        ]
        if not cap_ok:
            return MaskReason(
                text=f"No IDLE agent with {cap_key!r} capability",
                classification=MaskClassification.TRANSIENT,
                source=MaskSource.ELIGIBILITY,
            )
        if pt == PlayType.CODE_REVIEW:
            return MaskReason(
                text=(
                    "No eligible reviewer for any open PR"
                    " (anti-confirmation: all candidates authored the PR)"
                ),
                classification=MaskClassification.TRANSIENT,
                source=MaskSource.ELIGIBILITY,
            )
        return MaskReason(
            text="No eligible agent (eligibility filter)",
            classification=MaskClassification.TRANSIENT,
            source=MaskSource.ELIGIBILITY,
        )

    def _instantiate_config_reason(
        self, state: OrchestratorState, plan: PlayCandidatePlan
    ) -> MaskReason | None:
        """Return the INSTANTIATE_AGENT validity reason, or None if spawnable."""
        no_active_agents = not any(
            a.status in (AgentStatus.IDLE, AgentStatus.BUSY) for a in state.agents
        )
        if (
            no_active_agents
            and not plan.has_remaining_work
            and not plan.work_availability.terminal_no_work
        ):
            return MaskReason(
                text="No agents and no remaining work — nothing to spawn an agent for",
                classification=MaskClassification.INDEFINITE_WAIT,
                source=MaskSource.PRECONDITION,
            )
        if self._cfg is None:
            return None
        if not self._config_index:
            # None OR empty tuple → no (agent_type, tier) cell exists to spawn
            # into. Fail closed: an empty config_index must HARD-mask the action,
            # not leave it selectable (the selector coerces () -> None, so the
            # old ``is None`` guard let an empty index bypass the cap and spin
            # against execute()'s slot check; #159).
            return MaskReason(
                text="No agent configuration index — nothing to spawn",
                classification=MaskClassification.HARD,
                source=MaskSource.CONFIG,
            )
        if not compute_config_mask(state, self._cfg, self._config_index).any():
            return MaskReason(
                text="No eligible agent configuration",
                classification=MaskClassification.HARD,
                source=MaskSource.CONFIG,
            )
        return None

    @staticmethod
    def _has_break_trigger(state: OrchestratorState) -> bool:
        return any(
            a.status == AgentStatus.ERROR
            and a.last_error_class in RECOVERABLE_ERROR_CLASSES
            and a.current_play_type != PlayType.TAKE_BREAK
            for a in state.agents
        )

    @staticmethod
    def _has_terminal_error_agent(state: OrchestratorState) -> bool:
        """True if any agent is in a non-recoverable ERROR state (#20).

        Such an agent has no TAKE_BREAK recovery path, so it never reaches
        ``recovery_exhausted`` and END_AGENT would otherwise stay masked,
        leaking it (and any subprocess it holds) until end_session. Unmasking
        END_AGENT hands the retire decision to the PPO — it does not force one.
        """
        return any(
            a.status == AgentStatus.ERROR and a.last_error_class not in RECOVERABLE_ERROR_CLASSES
            for a in state.agents
        )

    async def confirm(
        self,
        play_type: PlayType,
        params: PlayParams,
        state: OrchestratorState,
    ) -> PlayVerdict:
        """Confirm a policy-selected play (whose target is already resolved) with
        one live read.

        Hybrid-data contract: the snapshot eligibility() produced the mask; this
        method reloads the live beads graph via ``live_graph_loader`` and
        re-derives validity against it, then checks that the *specific* resolved
        target in ``params`` (the issue/PR the selector is about to dispatch) is
        still in the live candidate set. This folds the live-beads IN_PROGRESS
        check for issue-target plays. If the resolved target dropped out of the
        live set, the verdict is ``valid=False`` with a HARD reason and the
        caller cleanly re-picks (releasing the claim it already took). A
        live-read blip falls back to snapshot-valid, never a hard-block.
        ``confirm`` NEVER acquires work-claims or runs any side-effect.

        Returns a :class:`PlayVerdict`. A ``valid=False`` verdict means the
        caller should cleanly re-pick (re-mask the action and resample) — never
        a plays-table skip row, never an RL experience sample.
        """
        # (a) Trusted bypass (bootstrap fleet seeding) skips all gates.
        if params.bypass_preconditions:
            return PlayVerdict(play_type=play_type, valid=True, reason=None, candidates=())

        # (b) THE live read: reload the beads graph (the resource a sibling agent
        #     can flip — e.g. mark a bead in_progress — between selection and
        #     dispatch) and re-derive validity against it. A live-read blip falls
        #     back to the passed snapshot; never a hard block. ``confirm`` NEVER
        #     acquires work-claims.
        live_state = state
        if self._live_graph_loader is not None:
            try:
                fresh_graph = await self._live_graph_loader()
            except (OSError, ValueError, RuntimeError, KeyError):
                fresh_graph = None
            if fresh_graph is not None:
                live_state = dataclasses.replace(state, graph=fresh_graph)

        try:
            live_plan = build_candidate_plan(live_state)
        except (ValueError, AttributeError, RuntimeError, KeyError):
            # Beads/GitHub blip while deriving — fall back to snapshot-valid.
            return PlayVerdict(play_type=play_type, valid=True, reason=None, candidates=())

        agent_mask = (
            compute_agent_eligibility_mask(live_state, self._registry, cfg=self._cfg)
            if self._cfg is not None
            else None
        )
        try:
            idx = V1_ACTION_ORDER.index(play_type)
        except ValueError:
            return PlayVerdict(play_type=play_type, valid=True, reason=None, candidates=())

        snapshot_reason = self._validity_reason_for(
            play_type, live_state, live_plan, agent_mask, idx
        )
        if snapshot_reason is not None:
            return PlayVerdict(
                play_type=play_type,
                valid=False,
                reason=snapshot_reason,
                candidates=live_plan.candidates_for(play_type),
            )

        # (c) Live target presence: for a play whose params name a concrete
        #     issue/PR target, the target must still be in the live candidate
        #     set. This folds the live-beads IN_PROGRESS gate (an issue whose
        #     bead flipped to in_progress drops out of the live plan) and the
        #     resolver-time fallbacks. Plays without a named target (or not in
        #     the live-confirm set) pass through on the snapshot verdict.
        live_reason = self._live_target_reason(play_type, params, live_plan)
        if live_reason is not None:
            return PlayVerdict(
                play_type=play_type,
                valid=False,
                reason=live_reason,
                candidates=live_plan.candidates_for(play_type),
            )

        return PlayVerdict(
            play_type=play_type,
            valid=True,
            reason=None,
            candidates=live_plan.candidates_for(play_type),
        )

    @staticmethod
    def _live_target_reason(
        play_type: PlayType,
        params: PlayParams,
        live_plan: PlayCandidatePlan,
    ) -> MaskReason | None:
        """Hard-block if a named target dropped out of the live candidate set.

        Only issue-target and PR-target plays are confirmed against the live
        plan; plays with no named target return None (snapshot governs). When the
        play has candidates but the *specific* target is gone, that is a live
        drift → HARD ``SELECTED_CANDIDATE_NO_LONGER_AVAILABLE`` → clean re-pick.
        """
        # The live-confirm set: candidate-bearing target plays. Internal/control
        # plays and audit plays are not target-confirmed here.
        if play_type not in LIVE_CONFIRM_PLAY_TYPES:
            return None

        target_issue = params.issue_number
        target_pr = params.pr_number
        if target_issue is None and target_pr is None:
            # No specific target pinned — the snapshot candidate check already
            # confirmed the play has at least one live candidate.
            return None

        live_candidates = live_plan.candidates_for(play_type)
        for candidate in live_candidates:
            if target_pr is not None and candidate.params.pr_number == target_pr:
                return None
            if target_issue is not None and candidate.params.issue_number == target_issue:
                return None

        # The pinned target is gone from the live plan (e.g. issue's bead flipped
        # to in_progress, PR merged/closed, review queue row cleared).
        return SELECTED_CANDIDATE_NO_LONGER_AVAILABLE

    @staticmethod
    def validity_fn_for(
        play_type: PlayType,
    ) -> Callable[[OrchestratorState, PlayCandidatePlan], list[MaskReason]] | None:
        """Return the validity function registered for ``play_type``, or None."""
        return _VALIDITY_FNS.get(play_type)


# ---------------------------------------------------------------------------
# Relocated eligibility/config masks (re-exported by ``mask.py``).
# ---------------------------------------------------------------------------


def _auth_config_blocked(
    blocked: set[tuple[str, str, str | None]],
    *,
    agent_type: str,
    tier: str,
    identity: str | None,
) -> bool:
    """Return True when an auth failure belongs to this spawn config."""
    identity_lower = canonical_identity_name(identity) if identity else None
    for blocked_type, blocked_tier, blocked_identity in blocked:
        if blocked_type != agent_type or blocked_tier != tier:
            continue
        if blocked_identity is None or identity_lower is None:
            return True
        if canonical_identity_name(blocked_identity) == identity_lower:
            return True
    return False


def _model_config_blocked(
    blocked: set[tuple[str, str, str | None]],
    *,
    agent_type: str,
    tier: str,
    model: str | None,
) -> bool:
    """Return True when an invalid-model failure belongs to this spawn config."""
    for blocked_type, blocked_tier, blocked_model in blocked:
        if blocked_type != agent_type or blocked_tier != tier:
            continue
        if blocked_model is None or model is None:
            return True
        if blocked_model == model:
            return True
    return False


def compute_agent_eligibility_mask(
    state: OrchestratorState,
    registry: PlayRegistry,
    *,
    cfg: RuntimeConfig,
) -> NDArray[np.bool_]:
    """Return a (NUM_ACTIONS,) bool mask where False means no eligible agent exists.

    Applies the same hard-filter chain as ``select_agent_for`` in
    ``agents/_selection.py``, but operating purely on ``OrchestratorState`` so the
    PPO never selects plays that the executor would refuse:

    1. Tier eligibility — agent's ``model_tier`` must be in
       ``allowed_tiers_for(play_type)``.
    2. Exclude list — agent's ``agent_type`` must not be in
       ``cfg.agent_preferences.exclude[play_type]``.
    3. Capability — agent must have the play's required capability flag set
       in ``AGENT_CAPABILITIES``.
    4. Anti-confirmation (CODE_REVIEW only) — at least one (IDLE+eligible,
       open-reviewable-PR) pair must exist where the agent's ``github_identity``
       differs from the PR's ``github_author``. Identity is the only
       deconfliction key (humans and agents may share a GH login; agents of
       the same type may have different logins). RUN_QA has no anti-
       confirmation: it runs against the merged trunk.

    Internal plays (``capability is None``) are not restricted here; they
    bypass agent selection entirely.
    """
    mask = np.ones(NUM_ACTIONS, dtype=bool)

    excluded_by_prefs: dict[str, set[str]] = {}
    for pt_val, types in cfg.agent_preferences.exclude.items():
        excluded_by_prefs[pt_val] = set(types)

    # Agent types with an active rate_limit quota hold — all instances share
    # the same API quota, so blocking one blocks all of the same type.
    rate_limited_types: set[str] = {
        a.agent_type.value
        for a in state.agents
        if a.status == AgentStatus.ERROR and a.last_error_class == ErrorClass.RATE_LIMIT
    }

    for i, pt in enumerate(V1_ACTION_ORDER):
        try:
            play = registry.get(pt)
        except KeyError:
            continue

        # Internal plays bypass agent selection — no eligibility gate.
        if play.capability is None:
            continue

        cap_key: str = play.capability  # capability is non-None: checked above
        allowed_tiers = allowed_tiers_for(pt)
        excluded_types = excluded_by_prefs.get(pt.value, set())

        candidates = [
            a
            for a in state.agents
            if a.status == AgentStatus.IDLE
            and (allowed_tiers is None or (a.model_tier or DEFAULT_MODEL_TIER) in allowed_tiers)
            and a.agent_type.value not in excluded_types
            and a.agent_type.value not in rate_limited_types
            and bool(AGENT_CAPABILITIES.get(a.agent_type, {}).get(cap_key, False))
        ]

        if not candidates:
            mask[i] = False
            continue

        # Anti-confirmation for CODE_REVIEW: at least one (eligible agent,
        # reviewable PR) pair must satisfy ``agent.github_identity !=
        # pr.github_author``. PR author is the GitHub creator login (truth);
        # identity is the agent's resolved GH login. When a queue row has
        # no corresponding PR snapshot (or the PR has no recorded author),
        # treat author as unknown and allow any candidate — the resolver
        # / executor's identity check is the final arbiter.
        if pt == PlayType.CODE_REVIEW:
            pr_by_number = {pr.pr_number: pr for pr in state.pull_requests}
            if state.pending_review_queue:
                # Each queue row represents a PR needing review. Use the PR
                # snapshot when available (to read github_author); otherwise
                # treat the row as "review wanted, author unknown."
                review_authors: list[str | None] = [
                    pr_by_number[row.pr_number].github_author
                    if row.pr_number in pr_by_number
                    else None
                    for row in state.pending_review_queue
                ]
            else:
                review_authors = [
                    pr.github_author
                    for pr in state.pull_requests
                    if not pr.is_draft and needs_review(pr)
                ]

            if not review_authors:
                mask[i] = False
                continue

            viable = False
            for author in review_authors:
                for agent in candidates:
                    if author is None or not same_identity(agent.github_identity, author):
                        viable = True
                        break
                if viable:
                    break
            if not viable:
                mask[i] = False

    return mask


def compute_config_mask(
    state: OrchestratorState,
    cfg: RuntimeConfig,
    config_index: tuple[ConfigKey, ...],
) -> NDArray[np.bool_]:
    """Return a (len(config_index),) bool mask: True means this config is spawnable now.

    A config is spawnable when:
    - the agent_type is enabled in agentshore.yaml
    - the model_tier is in that agent's enabled tiers
    - no IDLE agent already exists for the same (agent_type, model_tier)
    - live count for the (type, tier) pair is below that tier's ``max``

    The previous global ``max_total`` ceiling was removed in desktop-ty04 —
    per-(type, tier) gating is sufficient because PPO can't concentrate
    all spawns in one cell anymore.
    """
    n = len(config_index)
    if n == 0:
        return np.zeros(0, dtype=bool)

    # Per-(type, tier) live counts. Rate-limited ERROR agents are included
    # so a quota-exhausted type isn't immediately re-spawned; other ERROR /
    # TERMINATED agents are excluded so their slots stay open.
    counts: dict[tuple[str, str], int] = {}
    idle_configs: set[tuple[str, str]] = set()
    blocked_auth_configs: set[tuple[str, str, str | None]] = set()
    blocked_model_configs: set[tuple[str, str, str | None]] = set()
    for a in state.agents:
        if a.status == AgentStatus.TERMINATED:
            continue
        tier = a.model_tier or "medium"
        key = (a.agent_type.value, tier)
        if a.status == AgentStatus.IDLE:
            idle_configs.add(key)
        if a.status == AgentStatus.ERROR and a.last_error_class == ErrorClass.AUTH:
            blocked_auth_configs.add((a.agent_type.value, tier, a.github_identity))
            continue
        if a.status == AgentStatus.ERROR and a.last_error_class == ErrorClass.INVALID_MODEL:
            blocked_model_configs.add((a.agent_type.value, tier, a.model))
            continue
        if a.status == AgentStatus.ERROR and a.last_error_class != ErrorClass.RATE_LIMIT:
            continue
        counts[key] = counts.get(key, 0) + 1

    mask = np.zeros(n, dtype=bool)
    for i, (agent_type, tier) in enumerate(config_index):
        agent_cfg = cfg.agents.get(agent_type)
        if agent_cfg is None or not agent_cfg.enabled:
            continue
        try:
            agent_type_enum = AgentType(agent_type)
        except ValueError:
            continue  # unrecognised agent type — skip
        configured_model: str | None = None
        try:
            configured_model = effective_model_tier_config(agent_type_enum, agent_cfg, tier).model
        except Exception:
            configured_model = None
        if _auth_config_blocked(
            blocked_auth_configs,
            agent_type=agent_type,
            tier=tier,
            identity=agent_cfg.identity,
        ):
            continue
        if _model_config_blocked(
            blocked_model_configs,
            agent_type=agent_type,
            tier=tier,
            model=configured_model,
        ):
            continue
        if (agent_type, tier) in idle_configs:
            continue
        tier_cap = effective_model_tier_config(agent_type_enum, agent_cfg, tier).max
        if counts.get((agent_type, tier), 0) >= tier_cap:
            continue
        mask[i] = True
    return mask


__all__ = [
    "EligibilityAuthority",
    "EligibilityReport",
    "PlayVerdict",
    "compute_agent_eligibility_mask",
    "compute_config_mask",
]
