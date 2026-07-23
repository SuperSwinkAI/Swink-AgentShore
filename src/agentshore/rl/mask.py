"""Action mask computation — maps registry preconditions to a boolean numpy array."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import numpy as np

from agentshore.agents._selection import allowed_tiers_for
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
)
from agentshore.preferences import USER_DISABLEABLE_PLAYS
from agentshore.rl.action_space import NUM_ACTIONS, RESERVED_PLAYS, V1_ACTION_ORDER
from agentshore.rl.eligibility import EligibilityAuthority, EligibilityReport
from agentshore.rl.eligibility import (
    agent_needs_reaping as _agent_needs_reaping,
)
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
    USER_DISABLED,
    MaskClassification,
    MaskReason,
    MaskSource,
)
from agentshore.state import (
    AgentStatus,
    AgentType,
    PlayType,
    SessionState,
)

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from agentshore.config.models import RuntimeConfig
    from agentshore.plays.registry import PlayRegistry
    from agentshore.rl.config_head import ConfigKey
    from agentshore.state import OrchestratorState

_TERMINAL_QA_RECENT_WINDOW: Final[int] = TERMINAL_SHUTDOWN_EVIDENCE_WINDOW_PLAYS

# Stay hard-masked even on the reverse-failsafe path: non-progress plays and
# reserved tensor slots.
_REVERSE_FAILSAFE_HARD_MASKS: Final[frozenset[PlayType]] = (
    frozenset(
        {
            PlayType.SEED_PROJECT,
            PlayType.END_AGENT,
            PlayType.END_SESSION,
            PlayType.TAKE_BREAK,
        }
    )
    | RESERVED_PLAYS
)
_REVERSE_FAILSAFE_CONTROL_PLAYS: Final[frozenset[PlayType]] = frozenset(
    {PlayType.END_AGENT, PlayType.END_SESSION}
)
# 3-strikes circuit breaker: a work play with this many consecutive
# non-productive (fail OR skip) outcomes is benched for _CIRCUIT_BREAKER_
# COOLDOWN_PLAYS, then the policy may retry once (a fresh strike re-arms it) —
# stops re-selecting a play that can only skip (e.g. a resolve-time TOCTOU race).
# Separate from play_pacing.standard_cooldown_plays: a nonproductive-outcome
# retry bench, not a normal post-run cadence. RECONCILE_STATE (self-heal) and
# control plays are excluded.
_CIRCUIT_BREAKER_THRESHOLD: Final[int] = 3
_CIRCUIT_BREAKER_COOLDOWN_PLAYS: Final[int] = 20
# SEED_PROJECT (#357): its only cooldown, seed_audit_is_fresh(), keys off a
# prior *success* and never engages on failure, so a persistently-failing
# seed_project (e.g. an unreadable/schema-drifted beads store) had zero
# backoff and could re-dispatch every few minutes. Circuit-breaker membership
# gives it the same 3-consecutive-fail/skip -> benched-for-20-plays -> retry
# pattern every other candidate-required play already has.
_CIRCUIT_BREAKER_ELIGIBLE_PLAYS: Final[frozenset[PlayType]] = CANDIDATE_REQUIRED_PLAY_TYPES | {
    PlayType.RUN_QA,
    PlayType.DESIGN_AUDIT,
    PlayType.CALIBRATE_ALIGNMENT,
    PlayType.SEED_PROJECT,
}

# Lifecycle-churn breaker (#163): when work is undispatchable the PPO can
# oscillate INSTANTIATE_AGENT <-> END_AGENT with zero dispatches, burning budget.
# Masking lifecycle plays (when tail is all-lifecycle + idle capacity + a work
# play has run but gone stale) lets the engine idle quiescent until work unblocks.
_LIFECYCLE_PLAY_TYPES: Final[frozenset[PlayType]] = frozenset(
    {PlayType.INSTANTIATE_AGENT, PlayType.END_AGENT}
)
# "Productive work" for staleness detection. RECONCILE_STATE excluded so a
# repeatedly-failing self-heal can't reset the staleness guard (as are
# control/terminal/reserved plays).
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
            AgentType(agent_type)
        except ValueError:
            continue
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
    if play.capability is None:
        return False
    allowed_tiers = allowed_tiers_for(PlayType.RUN_QA)
    return any(
        agent.status == AgentStatus.IDLE
        and (allowed_tiers is None or (agent.model_tier or DEFAULT_MODEL_TIER) in allowed_tiers)
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


def _work_tail_stale(state: OrchestratorState) -> bool:
    """True once the most-recently-run work play is stale (>= the threshold).

    False during cold-start bootstrap (no work play has run yet), where a burst
    of INSTANTIATE_AGENT is correct.
    """
    since = [
        state.plays_since_last_play_type[pt]
        for pt in _WORK_PLAY_TYPES
        if pt in state.plays_since_last_play_type
    ]
    if not since:
        return False
    return min(since) >= _LIFECYCLE_CHURN_THRESHOLD


def _lifecycle_churn_active(
    state: OrchestratorState, *, availability: WorkAvailability | None = None
) -> bool:
    """True when the session is oscillating on lifecycle plays with no work (#163).

    Fires only when ALL hold, so it cannot suppress legitimate fleet growth:
      0. NOT terminal-no-work (a terminal final-QA spawn is legitimate).
      1. an IDLE agent exists — with no idle capacity INSTANTIATE_AGENT may
         legitimately grow the fleet, so that is not churn.
      2. a work play has run at least once — else this is cold-start bootstrap.
      3. the most-recently-run work play is now stale (>= the threshold) — the
         recent tail is lifecycle / no-op churn rather than progress.

    Gates the END_AGENT side of the breaker (reaping-churn needs idle capacity to
    reap). The INSTANTIATE_AGENT side additionally uses ``_growth_pointless_no_work``
    so reaping the last idle agent cannot re-open the spawn play (#166).
    """
    availability = availability or build_candidate_plan(state).work_availability
    if availability.terminal_no_work:
        return False
    if not any(a.status == AgentStatus.IDLE for a in state.agents):
        return False
    return _work_tail_stale(state)


def _growth_pointless_no_work(
    state: OrchestratorState, *, availability: WorkAvailability | None = None
) -> bool:
    """True when spawning another agent cannot accomplish anything (#166).

    A superset of ``_lifecycle_churn_active`` that drops the idle-agent
    requirement: it holds even with NO idle agent present, so after END_AGENT
    reaps the last idle agent the breaker keeps INSTANTIATE_AGENT masked instead
    of re-opening it — this is what stops the reap -> respawn limit cycle that
    kept the fleet-idle backstop and reverse-failsafe accumulators pinned at
    zero. Fires only post-bootstrap (work has run, now stale), with no
    dispatchable issue/PR/backlog/groom work, and not during a terminal final-QA
    spawn — so it cannot suppress legitimate fleet growth.
    """
    availability = availability or build_candidate_plan(state).work_availability
    if availability.terminal_no_work:
        return False
    if availability.has_actionable_work:
        return False
    return _work_tail_stale(state)


def reverse_failsafe_should_unmask(state: OrchestratorState) -> bool:
    """Return True when the all-masked escape hatch should expose fallback plays."""
    if state.session_state != SessionState.RUNNING:
        return False
    has_open_issue = any(issue.state.upper() == "OPEN" for issue in state.open_issues)
    has_terminal_dead_end = build_candidate_plan(state).work_availability.terminal_no_work
    has_idle_agent = any(agent.status == AgentStatus.IDLE for agent in state.agents)
    return (has_open_issue or has_terminal_dead_end) and has_idle_agent


def _resolve_user_disabled_plays(cfg: RuntimeConfig | None) -> frozenset[PlayType]:
    """User-disabled plays from global Preferences, allowlist-guarded.

    Re-checks :data:`USER_DISABLEABLE_PLAYS` so a hand-edited preferences file
    can never disable a delivery/lifecycle/self-heal play even if it names one.
    Shared by :meth:`ActionMaskBuilder._user_disabled_plays` and the reverse
    failsafe so an explicit user choice is honored on every path (#240).
    """
    if cfg is None:
        return frozenset()
    disabled: set[PlayType] = set()
    for value in cfg.preferences.disabled_plays:
        try:
            pt = PlayType(value)
        except ValueError:
            continue
        if pt in USER_DISABLEABLE_PLAYS and pt in V1_ACTION_ORDER:
            disabled.add(pt)
    return frozenset(disabled)


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
    # END_SESSION carries no terminal-readiness preconditions on the failsafe
    # path (#240): the terminal-evidence guards are for the normal path. The
    # failsafe only fires once the session is demonstrably wedged, where those
    # guards would keep END_SESSION masked exactly when a clean handoff is needed.
    # When control plays are allowed, END_SESSION stays lifted for the PPO to decide.

    if PlayType.INSTANTIATE_AGENT in V1_ACTION_ORDER:
        # Don't grow the fleet via the failsafe when idle capacity already exists
        # (bottleneck is masked dispatch, not fleet size; #163) or no config is
        # spawnable (empty/saturated config_index; #159). Failsafe only runs with
        # an idle agent present, so genuine cold-start spawns from the base mask.
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

    # Overlay invariant: any base-mask-enabled action stays enabled — the
    # failsafe can ADD opportunities, never REMOVE them.
    result = lifted | base_mask if base_mask is not None else lifted

    # The one exception to the additive invariant: a user-disabled play must
    # survive the failsafe (#240). The selector calls this without re-running the
    # builder's _stage_user_disabled, so enforce it here so both call sites are safe.
    for pt in _resolve_user_disabled_plays(cfg):
        result[V1_ACTION_ORDER.index(pt)] = False
    return result


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
        self._user_disabled: frozenset[PlayType] | None = None
        self._reasons: dict[PlayType, MaskReason] | None = None

    @property
    def candidate_plan(self) -> PlayCandidatePlan:
        return self._candidate_plan

    # -- policy-overlay stages (mutate self._mask in place) ------------------

    def _user_disabled_plays(self) -> frozenset[PlayType]:
        """Plays the user turned off via global Preferences (allowlist-guarded).

        Re-checks :data:`USER_DISABLEABLE_PLAYS` so a hand-edited preferences
        file can never disable a delivery/lifecycle/self-heal play even if it
        names one. Cached per build.
        """
        if self._user_disabled is None:
            self._user_disabled = _resolve_user_disabled_plays(self._cfg)
        return self._user_disabled

    def _stage_user_disabled(self) -> None:
        """Hard-mask user-disabled plays.

        Structural, like reserved slots: applied among the overlays AND re-run
        as the final word after the reverse-failsafe so the escape hatch can
        never resurrect an explicit user choice.
        """
        for pt in self._user_disabled_plays():
            self._mask[V1_ACTION_ORDER.index(pt)] = False

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
        """Mask lifecycle plays when the session is churning them with no work (#163/#166).

        Stops the INSTANTIATE_AGENT <-> END_AGENT oscillation that burns budget
        once work becomes undispatchable. INSTANTIATE_AGENT is masked whenever
        spawning is pointless (no dispatchable work) — even with no idle agent —
        so reaping the last idle agent cannot re-open the spawn play and restart
        the limit cycle (#166); this lets the fleet settle into a genuinely
        all-masked tick so the selector's reverse-failsafe and the loop's
        fleet-idle backstop can accumulate. END_AGENT stays available when an
        agent genuinely needs reaping (wedged / terminal error) so a stuck agent
        can still be retired. Pure option-removal; an all-masked result idles
        safely in the selector.
        """
        availability = self._candidate_plan.work_availability
        churn = _lifecycle_churn_active(self._state, availability=availability)
        pointless = _growth_pointless_no_work(self._state, availability=availability)
        if churn or pointless:
            self._mask[V1_ACTION_ORDER.index(PlayType.INSTANTIATE_AGENT)] = False
        if churn and not _agent_needs_reaping(self._state):
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

    def build(
        self, *, apply_reverse_failsafe: bool = False, include_reasons: bool = False
    ) -> NDArray[np.bool_]:
        """Run the mask pipeline and return the result.

        The base mask is the eligibility authority's verdict (every A-type
        validity gate). The remaining stages are policy overlays the authority
        deliberately does not own.

        ``include_reasons`` computes the per-play mask reason in the same pass
        (cached on ``self._reasons``, read by :meth:`build_reasons`) instead of
        requiring a second traversal that re-evaluates the same overlay
        predicates independently. It defaults to False so the mask-only hot
        path (``compute_action_mask``, called every selector tick) doesn't pay
        for reason derivation nobody asked for.
        """
        self._mask = self._eligibility_report().mask()
        self._reasons = None

        self._stage_consecutive_failure_breaker()
        self._stage_lifecycle_churn_breaker()
        self._stage_reserved_slots()
        self._stage_user_disabled()
        self._stage_end_session_in_flight()

        # Short-circuit stages run before the reverse-failsafe so the failsafe
        # can never re-enable a play they removed. Drain takes precedence over a
        # main-repo pause (mirrors dispatch: a draining session winds down even
        # with a paused trunk).
        if self._stage_drain_mode():
            if include_reasons:
                self._reasons = self._compute_reasons()
            return self._mask
        if self._stage_main_repo_paused():
            if include_reasons:
                self._reasons = self._compute_reasons()
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
            # The failsafe may lift a play the user disabled — re-apply the
            # user-disabled mask as the final, authoritative word.
            self._stage_user_disabled()

        if include_reasons:
            self._reasons = self._compute_reasons()
        return self._mask

    def _compute_reasons(self) -> dict[PlayType, MaskReason]:
        """Derive the reason for every currently-masked play, in priority order.

        Every predicate here (breaker, lifecycle-churn, reserved-slot/
        main-repo-pause/end-session-in-flight membership, user-disabled) is a
        pure function of ``self._state`` / ``self._candidate_plan`` — not of
        mask-mutation history — so evaluating them once here, against the
        final mask (post drain/main-repo-pause short-circuit and post
        reverse-failsafe), gives the identical attribution the old two-pass
        ``build`` + ``build_reasons`` produced, without re-deriving the mask
        itself. Priority order (first match wins) mirrors the pre-split
        behavior exactly: user-disabled > circuit breaker > lifecycle churn >
        reserved slot > main-repo-pause > end-session-in-flight > A-type
        authority verdict.
        """
        state = self._state

        # Drain short-circuit: every play but END_AGENT is masked with the
        # SESSION_DRAINING reason, overriding any other B-type reason.
        if state.session_state == SessionState.DRAINING:
            return {pt: SESSION_DRAINING for pt in V1_ACTION_ORDER if pt != PlayType.END_AGENT}

        availability = self._candidate_plan.work_availability
        verdicts = self._eligibility_report().verdicts
        user_disabled = self._user_disabled_plays()

        reasons: dict[PlayType, MaskReason] = {}
        for i, pt in enumerate(V1_ACTION_ORDER):
            if self._mask[i]:
                continue

            # User preference (B-type overlay) is authoritative: an explicit
            # user choice explains the mask before any policy/validity reason.
            if pt in user_disabled:
                reasons[pt] = USER_DISABLED
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
            if pt in _LIFECYCLE_PLAY_TYPES and (
                _lifecycle_churn_active(state, availability=availability)
                or _growth_pointless_no_work(state, availability=availability)
            ):
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

    def build_reasons(self, *, apply_reverse_failsafe: bool = False) -> dict[PlayType, MaskReason]:
        """Return a reason for every masked play type.

        Thin accessor: ``build`` computes the mask and (with
        ``include_reasons=True``) the reason map together in one pass, so this
        just triggers that combined build and returns the cached result.
        """
        self.build(apply_reverse_failsafe=apply_reverse_failsafe, include_reasons=True)
        return self._reasons if self._reasons is not None else {}


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
