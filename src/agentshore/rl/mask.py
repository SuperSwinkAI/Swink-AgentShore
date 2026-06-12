"""Action mask computation — maps registry preconditions to a boolean numpy array."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import numpy as np

from agentshore.agents._selection import allowed_tiers_for
from agentshore.agents.capabilities import AGENT_CAPABILITIES
from agentshore.agents.model_tiers import DEFAULT_MODEL_TIER
from agentshore.play_rules import (
    CANDIDATE_REQUIRED_PLAY_TYPES,
    TERMINAL_SHUTDOWN_EVIDENCE_WINDOW_PLAYS,
)
from agentshore.plays.candidates import (
    PlayCandidatePlan,
    WorkAvailability,
    build_candidate_plan,
    qa_ran_within_terminal_window,
    terminal_audits_are_fresh,
)
from agentshore.rl.action_space import NUM_ACTIONS, V1_ACTION_ORDER
from agentshore.rl.eligibility import EligibilityAuthority, EligibilityReport
from agentshore.rl.eligibility import (
    compute_agent_eligibility_mask as compute_agent_eligibility_mask,
)
from agentshore.rl.eligibility import (
    compute_config_mask as compute_config_mask,
)
from agentshore.rl.mask_reason import (
    END_SESSION_IN_FLIGHT,
    MAIN_REPO_DISPATCH_PAUSED,
    NOT_AVAILABLE,
    RESERVED_SLOT,
    SESSION_DRAINING,
    MaskClassification,
    MaskReason,
    MaskSource,
)
from agentshore.state import AgentStatus, AgentType, PlayType, SessionState

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from agentshore.config.models import RuntimeConfig
    from agentshore.plays.registry import PlayRegistry
    from agentshore.rl.action_space import ConfigKey
    from agentshore.state import OrchestratorState

_TERMINAL_QA_RECENT_WINDOW: Final[int] = TERMINAL_SHUTDOWN_EVIDENCE_WINDOW_PLAYS

# Reverse failsafe: when normal policy gates paint AgentShore into an all-masked
# corner even though open work and idle capacity exist, expose a broad fallback
# menu. These actions stay hard-masked because they are not progress actions or
# are reserved tensor slots.
_REVERSE_FAILSAFE_HARD_MASKS: Final[frozenset[PlayType]] = frozenset(
    {
        PlayType.SEED_PROJECT,
        PlayType.END_AGENT,
        PlayType.END_SESSION,
        PlayType.TAKE_BREAK,
        PlayType.FUTURE_4,
        PlayType.FUTURE_7,
        PlayType.FUTURE_8,
    }
)
_REVERSE_FAILSAFE_CONTROL_PLAYS: Final[frozenset[PlayType]] = frozenset(
    {PlayType.END_AGENT, PlayType.END_SESSION}
)
# 3-strikes circuit breaker: a work play that records this many consecutive
# non-productive (fail OR skip) outcomes is masked until ``_CIRCUIT_BREAKER_
# COOLDOWN_PLAYS`` have elapsed since its last attempt, then the policy may
# retry it once (a fresh strike re-arms it). This benches a play that can only
# skip — e.g. write_implementation_plan losing the resolve-time TOCTOU race —
# instead of letting the policy re-select it every tick. This breaker cooldown
# is intentionally separate from ``play_pacing.standard_cooldown_plays`` because
# it is a retry bench for nonproductive outcomes, not a normal post-run play
# cadence. Internal control plays and RECONCILE_STATE (self-heal must stay
# available) are excluded.
_CIRCUIT_BREAKER_THRESHOLD: Final[int] = 3
_CIRCUIT_BREAKER_COOLDOWN_PLAYS: Final[int] = 20
_CIRCUIT_BREAKER_ELIGIBLE_PLAYS: Final[frozenset[PlayType]] = CANDIDATE_REQUIRED_PLAY_TYPES | {
    PlayType.RUN_QA,
    PlayType.DESIGN_AUDIT,
    PlayType.CALIBRATE_ALIGNMENT,
}

# Lifecycle-churn breaker (#163): once a session can no longer dispatch
# productive work (PR-candidate gates, anti-confirmation, blocked worktree
# allocation), the PPO can still pick the lifecycle plays and oscillates
# INSTANTIATE_AGENT <-> END_AGENT with zero dispatches, burning budget. When the
# recent play tail is exclusively lifecycle AND idle capacity already exists AND
# a work play has run before but is now stale, mask the lifecycle plays so the
# engine goes quiescent (the selector idles safely on an all-masked tick) and
# resumes when work unblocks. Pure option-removal, like the circuit breaker.
_LIFECYCLE_PLAY_TYPES: Final[frozenset[PlayType]] = frozenset(
    {PlayType.INSTANTIATE_AGENT, PlayType.END_AGENT}
)
# "Productive work" for staleness detection: dispatching work plays plus the
# maintenance plays that make real progress. RECONCILE_STATE is excluded on
# purpose — a repeatedly-failing self-heal must not reset the staleness guard —
# as are the control/terminal/reserved plays.
_WORK_PLAY_TYPES: Final[frozenset[PlayType]] = _CIRCUIT_BREAKER_ELIGIBLE_PLAYS | {
    PlayType.SEED_PROJECT,
    PlayType.CLEANUP,
    PlayType.PRUNE,
}
_LIFECYCLE_CHURN_THRESHOLD: Final[int] = 6


@dataclass(frozen=True, slots=True)
class TerminalNoWorkDecision:
    """Terminal no-work action mask plus diagnostics."""

    mask: NDArray[np.bool_]
    mode: str
    availability: WorkAvailability
    qa_plays_since_last: int | None


def compute_terminal_no_work_config_mask(
    state: OrchestratorState,
    cfg: RuntimeConfig,
    config_index: tuple[ConfigKey, ...],
) -> NDArray[np.bool_]:
    """Return spawn configs valid for terminal final-QA setup."""

    config_mask = compute_config_mask(state, cfg, config_index)
    excluded = set(cfg.agent_preferences.exclude.get(PlayType.RUN_QA.value, []))
    filtered = np.zeros_like(config_mask)
    for i, (agent_type, tier) in enumerate(config_index):
        if not config_mask[i] or tier != "large" or agent_type in excluded:
            continue
        try:
            agent_type_enum = AgentType(agent_type)
        except ValueError:
            continue
        if bool(AGENT_CAPABILITIES.get(agent_type_enum, {}).get("can_test", False)):
            filtered[i] = True
    return filtered


def _has_terminal_qa_agent(
    state: OrchestratorState,
    registry: PlayRegistry,
    *,
    cfg: RuntimeConfig | None,
) -> bool:
    """Return True when an idle agent can execute final QA now."""

    idx = V1_ACTION_ORDER.index(PlayType.RUN_QA)
    if cfg is not None:
        return bool(compute_agent_eligibility_mask(state, registry, cfg=cfg)[idx])

    try:
        play = registry.get(PlayType.RUN_QA)
    except KeyError:
        return False
    cap_key = play.capability
    if cap_key is None:
        return False
    allowed_tiers = allowed_tiers_for(PlayType.RUN_QA)
    return any(
        agent.status == AgentStatus.IDLE
        and (allowed_tiers is None or (agent.model_tier or DEFAULT_MODEL_TIER) in allowed_tiers)
        and bool(AGENT_CAPABILITIES.get(agent.agent_type, {}).get(cap_key, False))
        for agent in state.agents
    )


def compute_terminal_no_work_decision(
    state: OrchestratorState,
    registry: PlayRegistry,
    *,
    cfg: RuntimeConfig | None = None,
    config_index: tuple[ConfigKey, ...] | None = None,
    candidate_plan: PlayCandidatePlan | None = None,
) -> TerminalNoWorkDecision | None:
    """Return the terminal no-work mask decision, if the state qualifies."""

    if state.session_state != SessionState.RUNNING:
        return None

    candidate_plan = candidate_plan or build_candidate_plan(state)
    availability = candidate_plan.work_availability
    if not availability.terminal_no_work:
        return None

    # Block terminal plays while beads has open tasks. GitHub workable-issue
    # counts can lag calibrate_alignment syncs, so beads takes precedence here.
    if availability.ready_task_count > 0:
        return None

    mask = np.zeros(NUM_ACTIONS, dtype=bool)
    qa_plays_since_last = state.plays_since_last_play_type.get(PlayType.RUN_QA)
    if qa_ran_within_terminal_window(state, window=_TERMINAL_QA_RECENT_WINDOW):
        mask[V1_ACTION_ORDER.index(PlayType.END_SESSION)] = True
        return TerminalNoWorkDecision(
            mask=mask,
            mode="end_session_recent_qa",
            availability=availability,
            qa_plays_since_last=qa_plays_since_last,
        )

    if _has_terminal_qa_agent(state, registry, cfg=cfg):
        mask[V1_ACTION_ORDER.index(PlayType.RUN_QA)] = True
        return TerminalNoWorkDecision(
            mask=mask,
            mode="final_qa",
            availability=availability,
            qa_plays_since_last=qa_plays_since_last,
        )

    if cfg is not None and config_index is not None:
        qa_config_mask = compute_terminal_no_work_config_mask(state, cfg, config_index)
        if qa_config_mask.any():
            mask[V1_ACTION_ORDER.index(PlayType.INSTANTIATE_AGENT)] = True
            return TerminalNoWorkDecision(
                mask=mask,
                mode="spawn_large_qa",
                availability=availability,
                qa_plays_since_last=qa_plays_since_last,
            )

    return None


def _agent_needs_reaping(state: OrchestratorState) -> bool:
    """True when some agent genuinely needs retiring (wedged / terminal error).

    Mirrors the wedged-END_AGENT re-enable condition in the EligibilityAuthority
    (recovery-exhausted or non-recoverable ERROR). Used to carve END_AGENT out of
    the lifecycle-churn breaker so a stuck agent can still be retired (#163).
    """
    if state.recovery_exhausted_agent_ids:
        return True
    return EligibilityAuthority._has_terminal_error_agent(state)


def _lifecycle_churn_active(state: OrchestratorState) -> bool:
    """True when the session is oscillating on lifecycle plays with no work (#163).

    Fires only when ALL hold, so it cannot suppress legitimate fleet growth:
      0. NOT terminal-no-work (a terminal final-QA spawn is legitimate).
      1. an IDLE agent exists — with no idle capacity INSTANTIATE_AGENT may
         legitimately grow the fleet, so that is not churn.
      2. a work play has run at least once — else this is cold-start bootstrap,
         where a burst of INSTANTIATE_AGENT is correct.
      3. the most-recently-run work play is now stale (>= the threshold) — the
         recent tail is lifecycle / no-op churn rather than progress.
    """
    if build_candidate_plan(state).work_availability.terminal_no_work:
        return False
    if not any(a.status == AgentStatus.IDLE for a in state.agents):
        return False
    since = [
        state.plays_since_last_play_type[pt]
        for pt in _WORK_PLAY_TYPES
        if pt in state.plays_since_last_play_type
    ]
    if not since:
        return False
    return min(since) >= _LIFECYCLE_CHURN_THRESHOLD


def reverse_failsafe_should_unmask(state: OrchestratorState) -> bool:
    """Return True when the all-masked escape hatch should expose fallback plays."""
    if state.session_state != SessionState.RUNNING:
        return False
    has_open_issue = any(issue.state.upper() == "OPEN" for issue in state.open_issues)
    has_terminal_dead_end = build_candidate_plan(state).work_availability.terminal_no_work
    has_idle_agent = any(agent.status == AgentStatus.IDLE for agent in state.agents)
    return (has_open_issue or has_terminal_dead_end) and has_idle_agent


def compute_reverse_failsafe_mask(
    state: OrchestratorState,
    *,
    cfg: RuntimeConfig | None = None,
    config_index: tuple[ConfigKey, ...] | None = None,
    allow_control_plays: bool = False,
    base_mask: NDArray[np.bool_] | None = None,
) -> NDArray[np.bool_]:
    """Return an overlay that preserves ``base_mask`` and opens gated fallback actions."""
    lifted = np.ones(NUM_ACTIONS, dtype=bool)
    hard_masks = _REVERSE_FAILSAFE_HARD_MASKS
    if allow_control_plays:
        hard_masks = hard_masks - _REVERSE_FAILSAFE_CONTROL_PLAYS
    for play_type in hard_masks:
        if play_type in V1_ACTION_ORDER:
            lifted[V1_ACTION_ORDER.index(play_type)] = False
    if allow_control_plays and PlayType.END_SESSION in V1_ACTION_ORDER:
        has_in_flight = bool(state.in_flight_plays) or any(
            agent.status == AgentStatus.BUSY for agent in state.agents
        )
        availability = build_candidate_plan(state).work_availability
        if (
            has_in_flight
            or not terminal_audits_are_fresh(state)
            or not qa_ran_within_terminal_window(state, window=_TERMINAL_QA_RECENT_WINDOW)
            or availability.ready_task_count > 0
        ):
            lifted[V1_ACTION_ORDER.index(PlayType.END_SESSION)] = False

    if PlayType.INSTANTIATE_AGENT in V1_ACTION_ORDER:
        # Don't grow the fleet via the failsafe when idle capacity already exists
        # (spawning more can't unblock work — the bottleneck is masked dispatch,
        # not fleet size; #163) or when no config is spawnable at all (empty or
        # saturated config_index; #159). The failsafe only ever runs with an idle
        # agent present (reverse_failsafe_should_unmask), so genuine cold-start
        # (zero agents) is unaffected — that first spawn comes from the base mask.
        idle_agent_exists = any(a.status == AgentStatus.IDLE for a in state.agents)
        no_spawnable_config = cfg is not None and (
            not config_index or not compute_config_mask(state, cfg, config_index).any()
        )
        if idle_agent_exists or no_spawnable_config:
            lifted[V1_ACTION_ORDER.index(PlayType.INSTANTIATE_AGENT)] = False

    candidate_plan = build_candidate_plan(state)
    for candidate_pt in CANDIDATE_REQUIRED_PLAY_TYPES:
        if candidate_pt in V1_ACTION_ORDER and not candidate_plan.candidates_for(candidate_pt):
            lifted[V1_ACTION_ORDER.index(candidate_pt)] = False

    # Overlay invariant: any action enabled by the base mask stays enabled.
    # This is what makes the semantics structural rather than replacement —
    # reverse failsafe can ADD opportunities, never REMOVE them.
    if base_mask is not None:
        return lifted | base_mask
    return lifted


# ---------------------------------------------------------------------------
# ActionMaskBuilder — staged pipeline for action mask computation
# ---------------------------------------------------------------------------


class ActionMaskBuilder:
    """Compute an action mask by composing the eligibility authority with policy overlays.

    Holds shared state (registry, config, candidate plan) so the policy-overlay
    stages read from ``self`` instead of receiving repeated keyword arguments.

    The base mask — every A-type validity gate (preconditions, agent
    eligibility, candidate-required, instantiate-config viability, end-session,
    take-break, wedged-END_AGENT re-enable) — now comes from a single
    :class:`~agentshore.rl.eligibility.EligibilityAuthority` computation via
    ``EligibilityAuthority(...).eligibility().mask()``. The authority is the one
    source of truth for validity, used both here (to present options to the PPO)
    and at confirm time (to validate the play the PPO selected).

    On top of that base mask this builder layers ONLY the policy overlays, in
    order:

    1. ``_stage_consecutive_failure_breaker`` — bench a play under the 3-strikes
       circuit breaker (zero-only).
    2. ``_stage_reserved_slots``              — zero reserved tensor slots.
    3. ``_stage_drain_mode``                  — short-circuit: END_AGENT-only when
       draining (replaces the mask entirely).
    4. reverse-failsafe overlay               — when the composed mask is empty
       and open work + idle capacity exist, lift a constrained fallback menu.

    Validity gates live in the authority, not here; the wedged-END_AGENT
    re-enable moved into the authority's eligibility computation. Directional
    choices (final QA vs spawn vs end, when to end the session) remain the
    policy's to make — the overlays only remove genuinely-unavailable actions.
    """

    def __init__(
        self,
        state: OrchestratorState,
        registry: PlayRegistry,
        *,
        cfg: RuntimeConfig | None = None,
        config_index: tuple[ConfigKey, ...] | None = None,
        candidate_plan: PlayCandidatePlan | None = None,
    ) -> None:
        self._state = state
        self._registry = registry
        self._cfg = cfg
        self._config_index = config_index
        self._candidate_plan = candidate_plan or build_candidate_plan(state)
        self._mask = np.zeros(NUM_ACTIONS, dtype=bool)
        self._report: EligibilityReport | None = None

    @property
    def candidate_plan(self) -> PlayCandidatePlan:
        return self._candidate_plan

    # -- policy-overlay stages (mutate self._mask in place) ------------------

    def _breaker_benched(self, pt: PlayType) -> bool:
        """True if ``pt`` is currently benched by the 3-strikes circuit breaker.

        Benched = at least ``_CIRCUIT_BREAKER_THRESHOLD`` consecutive
        non-productive (fail OR skip) outcomes AND fewer than
        ``_CIRCUIT_BREAKER_COOLDOWN_PLAYS`` real plays since its last attempt.
        Once the cooldown elapses the mask lifts so the policy may retry. Pure
        option-removal — the policy still chooses freely among valid plays.
        """
        if pt not in _CIRCUIT_BREAKER_ELIGIBLE_PLAYS:
            return False
        if self._state.consecutive_nonproductive_by_type.get(pt, 0) < _CIRCUIT_BREAKER_THRESHOLD:
            return False
        since = self._state.plays_since_last_play_type.get(pt)
        return since is not None and since < _CIRCUIT_BREAKER_COOLDOWN_PLAYS

    def _stage_consecutive_failure_breaker(self) -> None:
        for pt in _CIRCUIT_BREAKER_ELIGIBLE_PLAYS:
            if pt in V1_ACTION_ORDER and self._breaker_benched(pt):
                self._mask[V1_ACTION_ORDER.index(pt)] = False

    def _stage_lifecycle_churn_breaker(self) -> None:
        """Mask lifecycle plays when the session is churning them with no work (#163).

        Stops the INSTANTIATE_AGENT <-> END_AGENT oscillation that burns budget
        once work becomes undispatchable. END_AGENT stays available when an agent
        genuinely needs reaping (wedged / terminal error) so a stuck agent can
        still be retired. Pure option-removal; an all-masked result idles safely
        in the selector.
        """
        if not _lifecycle_churn_active(self._state):
            return
        self._mask[V1_ACTION_ORDER.index(PlayType.INSTANTIATE_AGENT)] = False
        if not _agent_needs_reaping(self._state):
            self._mask[V1_ACTION_ORDER.index(PlayType.END_AGENT)] = False

    def _stage_reserved_slots(self) -> None:
        for reserved in (PlayType.FUTURE_4, PlayType.FUTURE_7, PlayType.FUTURE_8):
            if reserved in V1_ACTION_ORDER:
                self._mask[V1_ACTION_ORDER.index(reserved)] = False

    def _stage_end_session_in_flight(self) -> None:
        """Hide END_SESSION when one is already started / in-flight.

        Mirrors ``dispatch_play`` gate 2. Option-removal overlay; gate 2 remains
        the dispatch-time backstop.
        """
        if self._state.end_session_in_flight and PlayType.END_SESSION in V1_ACTION_ORDER:
            self._mask[V1_ACTION_ORDER.index(PlayType.END_SESSION)] = False

    # -- short-circuit stages (finalize the mask and skip reverse-failsafe) ----

    def _stage_drain_mode(self) -> bool:
        if (
            self._state.session_state == SessionState.DRAINING
            and PlayType.END_AGENT in V1_ACTION_ORDER
        ):
            self._mask[:] = False
            self._mask[V1_ACTION_ORDER.index(PlayType.END_AGENT)] = True
            return True
        return False

    def _stage_main_repo_paused(self) -> bool:
        """Hide every play but END_AGENT / RECONCILE_STATE when dispatch is paused.

        Mirrors ``dispatch_play`` gate 1's allow-list exactly. This is a
        SHORT-CIRCUIT stage (returns True when it fires) so the reverse-failsafe
        below cannot re-enable a work play the trunk pause just removed — during
        a main-repo pause the whole point is to withhold work until the trunk
        heals (via RECONCILE_STATE) or an agent retires (END_AGENT). Unlike drain
        mode it does not force-enable the allow-list; it only removes the rest,
        matching gate 1 (which drops disallowed plays but never forces one). The
        live dispatch-time recheck (gate 1) remains the backstop.
        """
        if not self._state.main_repo_dispatch_paused:
            return False
        for i, pt in enumerate(V1_ACTION_ORDER):
            if pt not in (PlayType.END_AGENT, PlayType.RECONCILE_STATE):
                self._mask[i] = False
        return True

    # -- pipeline entry points -----------------------------------------------

    def _eligibility_report(self) -> EligibilityReport:
        """Compute (and cache) the one authority report — base A-type validity."""
        if self._report is None:
            self._report = EligibilityAuthority(
                self._state,
                self._registry,
                cfg=self._cfg,
                config_index=self._config_index,
                candidate_plan=self._candidate_plan,
            ).eligibility()
        return self._report

    def build(self, *, apply_reverse_failsafe: bool = False) -> NDArray[np.bool_]:
        """Run the mask pipeline and return the result.

        The base mask is the eligibility authority's verdict (every A-type
        validity gate). The remaining stages are policy overlays the authority
        deliberately does not own.
        """
        self._mask = self._eligibility_report().mask()

        self._stage_consecutive_failure_breaker()
        self._stage_lifecycle_churn_breaker()
        self._stage_reserved_slots()
        self._stage_end_session_in_flight()

        # Short-circuit stages run before the reverse-failsafe so the failsafe
        # can never re-enable a play they removed. Drain takes precedence over a
        # main-repo pause (mirrors dispatch: a draining session winds down even
        # with a paused trunk).
        if self._stage_drain_mode():
            return self._mask
        if self._stage_main_repo_paused():
            return self._mask

        if (
            apply_reverse_failsafe
            and not self._mask.any()
            and reverse_failsafe_should_unmask(self._state)
        ):
            self._mask = compute_reverse_failsafe_mask(
                self._state,
                cfg=self._cfg,
                config_index=self._config_index,
                base_mask=self._mask,
            )

        return self._mask

    def build_reasons(self, *, apply_reverse_failsafe: bool = False) -> dict[PlayType, MaskReason]:
        """Return a reason for every masked play type.

        Mask and reasons come from the one authority computation: ``build`` runs
        the full pipeline (authority base + B-type overlays) and caches the
        report, then A-type reasons are read from
        ``EligibilityReport.verdicts[pt].reason`` while the B-type overlays
        (drain short-circuit, circuit breaker, reserved slots) supply their own.
        """
        mask = self.build(apply_reverse_failsafe=apply_reverse_failsafe)
        state = self._state
        verdicts = self._eligibility_report().verdicts

        reasons: dict[PlayType, MaskReason] = {}

        # Drain short-circuit (B-type overlay): every play but END_AGENT is
        # masked with the SESSION_DRAINING reason.
        if state.session_state == SessionState.DRAINING:
            for pt in V1_ACTION_ORDER:
                if pt != PlayType.END_AGENT:
                    reasons[pt] = SESSION_DRAINING
            return reasons

        for i, pt in enumerate(V1_ACTION_ORDER):
            if mask[i]:
                continue

            # Circuit breaker (B-type overlay) takes priority: it benches a play
            # even when the authority deemed it valid.
            if self._breaker_benched(pt):
                strikes = state.consecutive_nonproductive_by_type.get(pt, 0)
                reasons[pt] = MaskReason(
                    text=(
                        f"circuit breaker: {strikes} consecutive non-productive "
                        f"outcomes — cooling down ({_CIRCUIT_BREAKER_COOLDOWN_PLAYS} plays)"
                    ),
                    classification=MaskClassification.TRANSIENT,
                    source=MaskSource.CIRCUIT_BREAKER,
                )
                continue

            # Lifecycle-churn breaker (B-type overlay): a lifecycle play masked to
            # stop instantiate<->end_agent churn while no work is dispatchable.
            if pt in _LIFECYCLE_PLAY_TYPES and _lifecycle_churn_active(state):
                reasons[pt] = MaskReason(
                    text=(
                        "lifecycle-churn breaker: only lifecycle plays selectable with no "
                        "dispatchable work — idling until work unblocks"
                    ),
                    classification=MaskClassification.TRANSIENT,
                    source=MaskSource.CIRCUIT_BREAKER,
                )
                continue

            # Reserved tensor slots (B-type overlay).
            if pt in (PlayType.FUTURE_4, PlayType.FUTURE_7, PlayType.FUTURE_8):
                reasons[pt] = RESERVED_SLOT
                continue

            # Main-repo dispatch paused (B-type overlay): every play but
            # END_AGENT / RECONCILE_STATE is masked while the latch is set.
            if state.main_repo_dispatch_paused and pt not in (
                PlayType.END_AGENT,
                PlayType.RECONCILE_STATE,
            ):
                reasons[pt] = MAIN_REPO_DISPATCH_PAUSED
                continue

            # END_SESSION already in-flight (B-type overlay).
            if pt == PlayType.END_SESSION and state.end_session_in_flight:
                reasons[pt] = END_SESSION_IN_FLIGHT
                continue

            # A-type validity reason from the single authority computation.
            verdict = verdicts.get(pt)
            if verdict is not None and verdict.reason is not None:
                reasons[pt] = verdict.reason
            else:
                reasons[pt] = NOT_AVAILABLE

        return reasons


def compute_action_mask(
    state: OrchestratorState,
    registry: PlayRegistry,
    *,
    cfg: RuntimeConfig | None = None,
    config_index: tuple[ConfigKey, ...] | None = None,
    apply_reverse_failsafe: bool = False,
    candidate_plan: PlayCandidatePlan | None = None,
) -> NDArray[np.bool_]:
    """Return a boolean array of shape (NUM_ACTIONS,) where True means the action is valid."""
    return ActionMaskBuilder(
        state,
        registry,
        cfg=cfg,
        config_index=config_index,
        candidate_plan=candidate_plan,
    ).build(apply_reverse_failsafe=apply_reverse_failsafe)


def compute_mask_reasons(
    state: OrchestratorState,
    registry: PlayRegistry,
    *,
    cfg: RuntimeConfig | None = None,
    config_index: tuple[ConfigKey, ...] | None = None,
    apply_reverse_failsafe: bool = False,
    candidate_plan: PlayCandidatePlan | None = None,
) -> dict[PlayType, MaskReason]:
    """Return a dict mapping each masked play type to a typed MaskReason."""
    return ActionMaskBuilder(
        state,
        registry,
        cfg=cfg,
        config_index=config_index,
        candidate_plan=candidate_plan,
    ).build_reasons(apply_reverse_failsafe=apply_reverse_failsafe)
