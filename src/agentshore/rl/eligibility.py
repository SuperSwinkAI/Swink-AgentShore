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
import structlog

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
    PlayCandidateAnalyzer,
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
from agentshore.state import (
    CONSECUTIVE_TIMEOUT_BENCH_LIMIT,
    RECOVERABLE_ERROR_CLASSES,
    AgentStatus,
    AgentType,
    PlayType,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from numpy.typing import NDArray

    from agentshore.beads import ProjectGraph
    from agentshore.config.models import RuntimeConfig
    from agentshore.plays.base import PlayParams
    from agentshore.plays.registry import PlayRegistry
    from agentshore.rl.config_head import ConfigKey
    from agentshore.state import AgentSnapshot, OrchestratorState

    # Returns a freshly-loaded beads graph (or None on a live-read blip). The
    # one live read ``confirm()`` is permitted, supplied by the dispatch layer
    # which owns the repo path.
    LiveGraphLoader = Callable[[], Awaitable[ProjectGraph | None]]


_logger = structlog.get_logger(__name__)


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
    """Validity for WRITE_IMPLEMENTATION_PLAN (moved from write_plan.py).

    Masks HARD when no open issue satisfies the LIVE ``issue_available_for_plan``
    predicate rather than trusting only the snapshot candidate COUNT (#191). The
    snapshot count could stay non-empty while the resolver — which re-derives
    candidates from the same predicate at dispatch via ``build_candidate_plan`` —
    found nothing eligible (an issue flipped PR-covered / in-flight / planned /
    beads-in-progress between mask and dispatch), failing the play and re-selecting
    it next tick. Re-deriving from ``state.open_issues`` with the exact
    ``PlayCandidateAnalyzer.issue_available_for_plan`` the candidate builder owns
    guarantees the mask matches what the resolver computes; both consume the same
    ``state`` (the authority builds ``plan`` from this same ``state``), so the two
    can never drift.
    """
    issues: list[MaskReason] = []
    if not any(issue.state.upper() == "OPEN" for issue in state.open_issues):
        issues.append(
            MaskReason(
                text="no open issues available to plan",
                classification=MaskClassification.HARD,
                source=MaskSource.PRECONDITION,
            )
        )
    # Call the real predicate (not a hand-copied one) so the mask can't drift from
    # the resolver. The analyzer is the same one ``build_candidate_plan`` uses.
    analyzer = PlayCandidateAnalyzer(state)
    if not any(analyzer.issue_available_for_plan(issue) for issue in state.open_issues):
        issues.append(
            _candidate_reason(
                plan,
                PlayType.WRITE_IMPLEMENTATION_PLAN,
                "no eligible issue for write_implementation_plan"
                " (all covered by open PR, in-flight, already planned, or labeled out)",
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
        if state.target_branch and pr.base_ref and pr.base_ref != state.target_branch
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


@dataclass(frozen=True, slots=True)
class _AgentEligibilityContext:
    """Per-play parameters the agent-eligibility stage chain filters against."""

    pt: PlayType
    state: OrchestratorState
    cap_key: str
    allowed_tiers: frozenset[str] | None
    excluded_types: frozenset[str]
    rate_limited_types: frozenset[str]


@dataclass(frozen=True, slots=True)
class _EligibilityStage:
    """One link in the agent-eligibility chain.

    ``filter_agents`` narrows the candidate list; ``reason`` is the typed
    reason to report when this stage is the one that empties it. Both
    ``compute_agent_eligibility_mask`` (folds every stage to a bool) and
    ``EligibilityAuthority._eligibility_reason`` (stops at the first stage
    that empties the set) walk ``_AGENT_ELIGIBILITY_STAGES`` in this same
    order, so the mask and its human-readable reason cannot drift apart —
    there is exactly one chain, not two that must be kept in sync.
    """

    name: str
    filter_agents: Callable[[list[AgentSnapshot], _AgentEligibilityContext], list[AgentSnapshot]]
    reason: Callable[[_AgentEligibilityContext], MaskReason]


def _stage_idle_filter(
    agents: list[AgentSnapshot], _ctx: _AgentEligibilityContext
) -> list[AgentSnapshot]:
    return [a for a in agents if a.status == AgentStatus.IDLE]


def _stage_idle_reason(_ctx: _AgentEligibilityContext) -> MaskReason:
    return MaskReason(
        text="No IDLE agents",
        classification=MaskClassification.TRANSIENT,
        source=MaskSource.ELIGIBILITY,
    )


def _stage_tier_filter(
    agents: list[AgentSnapshot], ctx: _AgentEligibilityContext
) -> list[AgentSnapshot]:
    return [
        a
        for a in agents
        if ctx.allowed_tiers is None or (a.model_tier or DEFAULT_MODEL_TIER) in ctx.allowed_tiers
    ]


def _stage_tier_reason(ctx: _AgentEligibilityContext) -> MaskReason:
    tier_str = "|".join(sorted(ctx.allowed_tiers)) if ctx.allowed_tiers else "any"
    return MaskReason(
        text=f"No IDLE agent of allowed tier ({tier_str})",
        classification=MaskClassification.TRANSIENT,
        source=MaskSource.ELIGIBILITY,
    )


def _stage_exclude_filter(
    agents: list[AgentSnapshot], ctx: _AgentEligibilityContext
) -> list[AgentSnapshot]:
    return [a for a in agents if a.agent_type.value not in ctx.excluded_types]


def _stage_exclude_reason(ctx: _AgentEligibilityContext) -> MaskReason:
    return MaskReason(
        text=f"No IDLE agent type permitted by exclude rule for {ctx.pt.value!r}",
        classification=MaskClassification.TRANSIENT,
        source=MaskSource.ELIGIBILITY,
    )


def _stage_rate_limit_filter(
    agents: list[AgentSnapshot], ctx: _AgentEligibilityContext
) -> list[AgentSnapshot]:
    return [a for a in agents if a.agent_type.value not in ctx.rate_limited_types]


def _stage_rate_limit_reason(ctx: _AgentEligibilityContext) -> MaskReason:
    return MaskReason(
        text=f"No IDLE agent of a non-rate-limited type for {ctx.pt.value!r}",
        classification=MaskClassification.TRANSIENT,
        source=MaskSource.ELIGIBILITY,
    )


def _stage_capability_filter(
    agents: list[AgentSnapshot], ctx: _AgentEligibilityContext
) -> list[AgentSnapshot]:
    return [
        a for a in agents if bool(AGENT_CAPABILITIES.get(a.agent_type, {}).get(ctx.cap_key, False))
    ]


def _stage_capability_reason(ctx: _AgentEligibilityContext) -> MaskReason:
    return MaskReason(
        text=f"No IDLE agent with {ctx.cap_key!r} capability",
        classification=MaskClassification.TRANSIENT,
        source=MaskSource.ELIGIBILITY,
    )


def _stage_anti_confirmation_filter(
    agents: list[AgentSnapshot], ctx: _AgentEligibilityContext
) -> list[AgentSnapshot]:
    """CODE_REVIEW only: keep ``agents`` iff some (agent, PR-author) pair clears
    anti-confirmation (reviewer identity != PR author). No-op for every other
    play type. An empty ``review_authors`` (no PR actually needs review) also
    yields no viable pair, so it naturally falls through to "not viable"
    without a separate empty-check.
    """
    if ctx.pt != PlayType.CODE_REVIEW:
        return agents
    state = ctx.state
    pr_by_number = {pr.pr_number: pr for pr in state.pull_requests}
    if state.pending_review_queue:
        review_authors: list[str | None] = [
            pr_by_number[row.pr_number].github_author if row.pr_number in pr_by_number else None
            for row in state.pending_review_queue
        ]
    else:
        review_authors = [
            pr.github_author for pr in state.pull_requests if not pr.is_draft and needs_review(pr)
        ]
    for author in review_authors:
        for agent in agents:
            if author is None or not same_identity(agent.github_identity, author):
                return agents
    return []


def _stage_anti_confirmation_reason(_ctx: _AgentEligibilityContext) -> MaskReason:
    return MaskReason(
        text=(
            "No eligible reviewer for any open PR"
            " (anti-confirmation: all candidates authored the PR)"
        ),
        classification=MaskClassification.TRANSIENT,
        source=MaskSource.ELIGIBILITY,
    )


# The canonical agent-eligibility chain. Order is the single source of truth:
# both the mask fold and the reason lookup walk this same list.
_AGENT_ELIGIBILITY_STAGES: tuple[_EligibilityStage, ...] = (
    _EligibilityStage("idle", _stage_idle_filter, _stage_idle_reason),
    _EligibilityStage("tier", _stage_tier_filter, _stage_tier_reason),
    _EligibilityStage("exclude", _stage_exclude_filter, _stage_exclude_reason),
    _EligibilityStage("rate_limit", _stage_rate_limit_filter, _stage_rate_limit_reason),
    _EligibilityStage("capability", _stage_capability_filter, _stage_capability_reason),
    _EligibilityStage(
        "anti_confirmation", _stage_anti_confirmation_filter, _stage_anti_confirmation_reason
    ),
)


def _agent_eligibility_candidates(
    pt: PlayType,
    state: OrchestratorState,
    cfg: RuntimeConfig,
    cap_key: str,
) -> tuple[list[AgentSnapshot], MaskReason | None]:
    """Walk ``_AGENT_ELIGIBILITY_STAGES`` for one play type.

    Returns the surviving candidate agents and, when some stage emptied the
    set, that stage's reason (``None`` when eligible). Callers resolve the
    play's registry entry and ``capability`` themselves (the two callers mask
    that lookup differently: silent skip vs. a typed reason), then hand this
    function the already-known ``cap_key`` for the restricted case.
    """
    ctx = _AgentEligibilityContext(
        pt=pt,
        state=state,
        cap_key=cap_key,
        allowed_tiers=allowed_tiers_for(pt),
        excluded_types=frozenset(cfg.agent_preferences.exclude.get(pt.value, ())),
        rate_limited_types=frozenset(
            a.agent_type.value
            for a in state.agents
            if a.status == AgentStatus.ERROR and a.last_error_class == ErrorClass.RATE_LIMIT
        ),
    )
    candidates: list[AgentSnapshot] = list(state.agents)
    for stage in _AGENT_ELIGIBILITY_STAGES:
        candidates = stage.filter_agents(candidates, ctx)
        if not candidates:
            return [], stage.reason(ctx)
    return candidates, None


def has_terminal_error_agent(state: OrchestratorState) -> bool:
    """True if any agent is in a non-recoverable ERROR state (#20).

    Such an agent has no TAKE_BREAK recovery path, so it never reaches
    ``recovery_exhausted`` and END_AGENT would otherwise stay masked, leaking it
    (and any subprocess it holds) until end_session. Unmasking END_AGENT hands
    the retire decision to the PPO — it does not force one.
    """
    return any(
        a.status == AgentStatus.ERROR and a.last_error_class not in RECOVERABLE_ERROR_CLASSES
        for a in state.agents
    )


def agent_needs_reaping(state: OrchestratorState) -> bool:
    """True when some agent genuinely needs retiring (wedged / terminal error).

    The single predicate behind the wedged-END_AGENT re-enable. An agent needs
    reaping when it is recovery-exhausted, consecutive-timeout-benched (#161), or
    in a non-recoverable ERROR state (#20). All three sit IDLE-or-ERROR but are
    excluded from selection, so they never recover on their own — keeping
    END_AGENT available lets the PPO reap them instead of wedging. Used both by
    the authority's wedged re-enable and by the mask-pipeline lifecycle-churn
    breaker so the two never drift.
    """
    if state.recovery_exhausted_agent_ids:
        return True
    if any(a.consecutive_timeouts >= CONSECUTIVE_TIMEOUT_BENCH_LIMIT for a in state.agents):
        return True
    return has_terminal_error_agent(state)


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
        wedged_end_agent_reenable = pt == PlayType.END_AGENT and agent_needs_reaping(state)

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
        #    Escape hatch (#166): when the open-PR queue is wedged on human
        #    intervention (>= MAX_OPEN_PRS - 1 PRs carry manual-required, so the
        #    backpressure cap blocks new issue_pickup and nothing can drain into
        #    a mergeable PR), END_SESSION becomes a valid choice even while
        #    nominal issue/task work still looks plannable. It is non-forcing —
        #    the PPO still weighs it against any genuinely-actionable PR play, and
        #    _stage_end_session_in_flight still prevents a double-fire.
        #    Escape hatch (#330): when ``merge_pr`` has failed repeatedly with
        #    the same ``dirty_trunk`` cause — a root-level untracked file a
        #    deterministic reclaim sweep correctly leaves alone (e.g. it looks
        #    like real user WIP) — the trunk is wedged and cannot drain via
        #    merge_pr no matter how many times it's retried.
        #    ``MainRepoGuard.is_trunk_wedged()`` (surfaced as
        #    ``state.trunk_wedged``) fires after
        #    ``_DIRTY_TRUNK_WEDGE_THRESHOLD`` consecutive same-cause failures.
        #    Same non-forcing contract as the #166 hatch: this only unmasks
        #    END_SESSION for the PPO to weigh against other actionable work, it
        #    never stops the session itself.
        if (
            pt == PlayType.END_SESSION
            and not plan.work_availability.pr_queue_human_blocked
            and not state.trunk_wedged
            and (
                not plan.work_availability.terminal_no_work
                or plan.work_availability.ready_task_count > 0
            )
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
        """Return a typed reason explaining why the agent-eligibility gate fired.

        Only called when ``agent_mask[idx]`` is False, which itself only
        happens when ``self._cfg`` is not None (see ``eligibility()``) — so
        ``self._cfg`` is trusted non-None here.
        """
        try:
            play = self._registry.get(pt)
        except KeyError:
            return MaskReason(
                text="No play registered",
                classification=MaskClassification.HARD,
                source=MaskSource.ELIGIBILITY,
            )
        cap_key = play.capability
        if cap_key is None or self._cfg is None:
            return NOT_AVAILABLE

        _, reason = _agent_eligibility_candidates(pt, state, self._cfg, cap_key)
        return reason or MaskReason(
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
    bypass agent selection entirely. The per-play filter chain itself lives in
    ``_AGENT_ELIGIBILITY_STAGES`` (walked via ``_agent_eligibility_candidates``)
    — the same chain ``EligibilityAuthority._eligibility_reason`` walks to
    explain a False bit, so mask and reason cannot drift apart.
    """
    mask = np.ones(NUM_ACTIONS, dtype=bool)
    for i, pt in enumerate(V1_ACTION_ORDER):
        try:
            play = registry.get(pt)
        except KeyError:
            continue

        # Internal plays bypass agent selection — no eligibility gate.
        if play.capability is None:
            continue

        candidates, _ = _agent_eligibility_candidates(pt, state, cfg, play.capability)
        if not candidates:
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
    - no same-provider agent is currently in TAKE_BREAK
    - no IDLE agent already exists for the same (agent_type, model_tier)
    - non-terminated count for the (type, tier) pair is below that tier's ``max``

    The previous global ``max_total`` ceiling was removed in desktop-ty04 —
    per-(type, tier) gating is sufficient because PPO can't concentrate
    all spawns in one cell anymore.
    """
    n = len(config_index)
    if n == 0:
        return np.zeros(0, dtype=bool)

    # Per-(type, tier) counts. ERROR agents are included so a failing or
    # quota-exhausted type cannot respawn past its configured cap; TERMINATED
    # agents are excluded because end_agent has freed their slot.
    counts: dict[tuple[str, str], int] = {}
    idle_configs: set[tuple[str, str]] = set()
    providers_in_take_break: set[str] = set()
    blocked_auth_configs: set[tuple[str, str, str | None]] = set()
    blocked_model_configs: set[tuple[str, str, str | None]] = set()
    for a in state.agents:
        if a.status == AgentStatus.TERMINATED:
            continue
        tier = a.model_tier or "medium"
        key = (a.agent_type.value, tier)
        if a.current_play_type == PlayType.TAKE_BREAK:
            providers_in_take_break.add(a.agent_type.value)
        if a.status == AgentStatus.IDLE:
            idle_configs.add(key)
        if a.status == AgentStatus.ERROR and a.last_error_class == ErrorClass.AUTH:
            blocked_auth_configs.add((a.agent_type.value, tier, a.github_identity))
        if a.status == AgentStatus.ERROR and a.last_error_class == ErrorClass.INVALID_MODEL:
            blocked_model_configs.add((a.agent_type.value, tier, a.model))
        counts[key] = counts.get(key, 0) + 1

    mask = np.zeros(n, dtype=bool)
    for i, (agent_type, tier) in enumerate(config_index):
        agent_cfg = cfg.agents.get(agent_type)
        if agent_cfg is None or not agent_cfg.enabled:
            continue
        if agent_type in providers_in_take_break:
            continue
        try:
            agent_type_enum = AgentType(agent_type)
        except ValueError:
            continue  # unrecognised agent type — skip
        configured_model: str | None = None
        try:
            configured_model = effective_model_tier_config(agent_type_enum, agent_cfg, tier).model
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            _logger.warning(
                "compute_config_mask.model_resolution_failed",
                agent_type=agent_type,
                tier=tier,
                error=str(exc),
            )
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
    "agent_needs_reaping",
    "compute_agent_eligibility_mask",
    "compute_config_mask",
    "has_terminal_error_agent",
]
