"""Action mask computation — maps registry preconditions to a boolean numpy array."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import numpy as np
import structlog

from agentshore.agents._selection import allowed_tiers_for
from agentshore.agents.capabilities import AGENT_CAPABILITIES
from agentshore.agents.model_tiers import (
    DEFAULT_MODEL_TIER,
    effective_model_tier_config,
    enabled_model_tiers,
)
from agentshore.identity_names import canonical_identity_name, same_identity
from agentshore.play_rules import TERMINAL_SHUTDOWN_EVIDENCE_WINDOW_PLAYS, needs_review
from agentshore.plays.candidates import PlayCandidatePlan, build_candidate_plan
from agentshore.rl.action_space import NUM_ACTIONS, V1_ACTION_ORDER
from agentshore.rl.mask_reason import (
    NOT_AVAILABLE,
    RESERVED_SLOT,
    SESSION_DRAINING,
    MaskClassification,
    MaskReason,
    MaskSource,
)
from agentshore.state import AgentStatus, AgentType, PlayType, SessionState
from agentshore.work_availability import (
    WorkAvailability,
    qa_ran_within_terminal_window,
    terminal_audits_are_fresh,
)

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from agentshore.config.models import RuntimeConfig
    from agentshore.plays.registry import PlayRegistry
    from agentshore.rl.action_space import ConfigKey
    from agentshore.state import OrchestratorState

_logger = structlog.get_logger(__name__)

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
        PlayType.FUTURE_7,
        PlayType.FUTURE_8,
    }
)
_REVERSE_FAILSAFE_CONTROL_PLAYS: Final[frozenset[PlayType]] = frozenset(
    {PlayType.END_AGENT, PlayType.END_SESSION}
)
_CANDIDATE_REQUIRED_PLAY_TYPES: Final[frozenset[PlayType]] = frozenset(
    {
        PlayType.UNBLOCK_PR,
        PlayType.WRITE_IMPLEMENTATION_PLAN,
        PlayType.ISSUE_PICKUP,
        PlayType.CODE_REVIEW,
        PlayType.MERGE_PR,
        PlayType.SYSTEMATIC_DEBUGGING,
        PlayType.REFINE_TASK_BREAKDOWN,
        PlayType.GROOM_BACKLOG,
    }
)

# 3-strikes circuit breaker: a work play that records this many consecutive
# non-productive (fail OR skip) outcomes is masked until ``_CIRCUIT_BREAKER_
# COOLDOWN_PLAYS`` have elapsed since its last attempt, then the policy may
# retry it once (a fresh strike re-arms it). This benches a play that can only
# skip — e.g. write_implementation_plan losing the resolve-time TOCTOU race —
# instead of letting the policy re-select it every tick. Cooldown matches the
# project-standard 20-play window (cf. SEED/DESIGN_AUDIT cooldowns). Internal
# control plays and RECONCILE_STATE (self-heal must stay available) are excluded.
_CIRCUIT_BREAKER_THRESHOLD: Final[int] = 3
_CIRCUIT_BREAKER_COOLDOWN_PLAYS: Final[int] = 20
_CIRCUIT_BREAKER_ELIGIBLE_PLAYS: Final[frozenset[PlayType]] = _CANDIDATE_REQUIRED_PLAY_TYPES | {
    PlayType.RUN_QA,
    PlayType.DESIGN_AUDIT,
    PlayType.CALIBRATE_ALIGNMENT,
}


@dataclass(frozen=True, slots=True)
class TerminalNoWorkDecision:
    """Terminal no-work action mask plus diagnostics."""

    mask: NDArray[np.bool_]
    mode: str
    availability: WorkAvailability
    qa_plays_since_last: int | None


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
        if a.status == AgentStatus.ERROR and a.last_error_class == "rate_limit"
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
    - live count for the (type, tier) pair is below ``agent_spawn.max_per_config``

    The previous global ``max_total`` ceiling was removed in desktop-ty04 —
    per-(type, tier) gating is sufficient because PPO can't concentrate
    all spawns in one cell anymore.
    """
    n = len(config_index)
    if n == 0:
        return np.zeros(0, dtype=bool)

    spawn_cfg = cfg.agent_spawn

    # Per-(type, tier) live counts. Rate-limited ERROR agents are included
    # so a quota-exhausted type isn't immediately re-spawned; other ERROR /
    # TERMINATED agents are excluded so their slots stay open.
    counts: dict[tuple[str, str], int] = {}
    idle_configs: set[tuple[str, str]] = set()
    blocked_auth_configs: set[tuple[str, str, str | None]] = set()
    blocked_model_configs: set[tuple[str, str, str | None]] = set()
    for a in state.agents:
        if a.status.value == "terminated":
            continue
        tier = a.model_tier or "medium"
        key = (a.agent_type.value, tier)
        if a.status == AgentStatus.IDLE:
            idle_configs.add(key)
        if a.status.value == "error" and a.last_error_class == "auth":
            blocked_auth_configs.add((a.agent_type.value, tier, a.github_identity))
            continue
        if a.status.value == "error" and a.last_error_class == "invalid_model":
            blocked_model_configs.add((a.agent_type.value, tier, a.model))
            continue
        if a.status.value == "error" and a.last_error_class != "rate_limit":
            continue
        counts[key] = counts.get(key, 0) + 1

    mask = np.zeros(n, dtype=bool)
    for i, (agent_type, tier) in enumerate(config_index):
        agent_cfg = cfg.agents.get(agent_type)
        if agent_cfg is None or not agent_cfg.enabled:
            continue
        configured_model = None
        try:
            agent_type_enum = AgentType(agent_type)
            configured_model = effective_model_tier_config(agent_type_enum, agent_cfg, tier).model
        except ValueError:
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
        if counts.get((agent_type, tier), 0) >= spawn_cfg.max_per_config:
            continue
        mask[i] = True
    return mask


def _instantiate_config_block_reason(
    state: OrchestratorState,
    cfg: RuntimeConfig,
    config_index: tuple[ConfigKey, ...],
) -> MaskReason:
    if not config_index:
        return MaskReason(
            text="No eligible agent configuration",
            classification=MaskClassification.HARD,
            source=MaskSource.CONFIG,
        )

    idle_configs = {
        (a.agent_type.value, a.model_tier or DEFAULT_MODEL_TIER)
        for a in state.agents
        if a.status == AgentStatus.IDLE
    }
    enabled_configs = 0
    enabled_configs_without_idle = 0
    for agent_type, tier in config_index:
        agent_cfg = cfg.agents.get(agent_type)
        if agent_cfg is None or not agent_cfg.enabled:
            continue
        try:
            agent_type_enum = AgentType(agent_type)
        except ValueError:
            continue
        if tier not in enabled_model_tiers(agent_type_enum, agent_cfg):
            continue
        enabled_configs += 1
        if (agent_type, tier) not in idle_configs:
            enabled_configs_without_idle += 1

    if enabled_configs > 0 and enabled_configs_without_idle == 0:
        return MaskReason(
            text="Idle agent already available for every eligible type/tier",
            classification=MaskClassification.TRANSIENT,
            source=MaskSource.CONFIG,
        )
    return MaskReason(
        text="No eligible agent configuration",
        classification=MaskClassification.HARD,
        source=MaskSource.CONFIG,
    )


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
    """Compute the reverse-failsafe overlay.

    Phase 3 of the v0.15 architecture refactor: this function now models
    reverse failsafe as an *overlay* on the base mask, not a replacement.
    The returned mask is a structural superset of ``base_mask`` — any
    action enabled by the base mask is preserved. The "lift" opens a
    constrained set of additional actions per the gate logic below.

    Lift gates (applied in order, each a subtractive constraint on the
    initial all-ones mask):

    1. Hard masks: SEED_PROJECT, END_AGENT, END_SESSION, TAKE_BREAK,
       FUTURE_7/8 stay False (END_AGENT and END_SESSION conditionally
       opened by ``allow_control_plays``).
    2. END_SESSION evidence: terminal audits + recent QA + idle fleet.
    3. INSTANTIATE_AGENT config viability: at least one eligible config
       must exist.
    4. Candidate-required plays (WIP, PICKUP, REVIEW, MERGE, UNBLOCK,
       DEBUG, REFINE, GROOM): stays False if the candidate set is empty.
       Reverse failsafe cannot conjure a target out of nothing —
       v0.14.4 fix for desktop-wwr.

    Passing ``base_mask`` is recommended for callers that want the strict
    overlay contract. When omitted, behaviour matches v0.14.4 (lift mask
    only).
    """
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
        if (
            has_in_flight
            or not terminal_audits_are_fresh(state)
            or not qa_ran_within_terminal_window(state, window=_TERMINAL_QA_RECENT_WINDOW)
        ):
            lifted[V1_ACTION_ORDER.index(PlayType.END_SESSION)] = False

    if (
        cfg is not None
        and config_index is not None
        and PlayType.INSTANTIATE_AGENT in V1_ACTION_ORDER
        and not compute_config_mask(state, cfg, config_index).any()
    ):
        lifted[V1_ACTION_ORDER.index(PlayType.INSTANTIATE_AGENT)] = False

    candidate_plan = build_candidate_plan(state)
    for candidate_pt in _CANDIDATE_REQUIRED_PLAY_TYPES:
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
    """Staged pipeline that computes an action mask from OrchestratorState.

    Holds shared state (registry, config, candidate plan) so individual
    stages read from ``self`` instead of receiving repeated keyword arguments.

    Pipeline stages run in this order:

    1. ``_stage_preconditions``      — seed mask from registry preconditions
    2. ``_stage_agent_eligibility``  — AND in agent-eligibility mask
    3. ``_stage_wedged_end_agent``   — re-enable END_AGENT for recovery-exhausted agents
    4. ``_stage_candidate_required`` — zero candidate-required plays with no target
    5. ``_stage_instantiate_config`` — zero INSTANTIATE_AGENT if no config viable
    6. ``_stage_end_session``        — zero END_SESSION while actionable work remains
    7. ``_stage_take_break``         — zero TAKE_BREAK unless rate_limit/unknown
    8. ``_stage_reserved_slots``     — zero reserved tensor slots
    9. ``_stage_drain_mode``         — short-circuit: END_AGENT-only when draining

    All stages except ``_stage_wedged_end_agent`` and ``_stage_drain_mode`` are
    zero-only (a play stays valid only if every stage agrees). The wedged stage
    is the sole re-enable: it lifts the precondition mask on END_AGENT so the
    PPO — not deterministic code — decides whether to retire a wedged agent.
    ``_stage_drain_mode`` is a short-circuit that replaces the mask entirely.
    Directional choices (final QA vs spawn vs end, when to end the session) are
    left to the policy; the pipeline only masks genuinely-invalid actions.
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

    @property
    def candidate_plan(self) -> PlayCandidatePlan:
        return self._candidate_plan

    # -- zero-only stages (mutate self._mask in place) -----------------------

    def _stage_preconditions(self) -> None:
        for i, pt in enumerate(V1_ACTION_ORDER):
            try:
                self._mask[i] = self._registry.preconditions_met(pt, self._state)
            except (KeyError, ValueError, AttributeError, RuntimeError) as exc:
                _logger.warning("precondition_check_failed", play_type=pt.value, error=str(exc))
                self._mask[i] = False

    def _stage_agent_eligibility(self) -> None:
        if self._cfg is None:
            return
        self._mask &= compute_agent_eligibility_mask(self._state, self._registry, cfg=self._cfg)

    def _stage_wedged_end_agent(self) -> None:
        """Re-enable END_AGENT when a recovery-exhausted agent exists.

        This is the only re-enable in the pipeline. When an agent has burned
        through its break-recovery attempts it is wedged (typically ERROR
        state), so the normal END_AGENT preconditions (>=2 active agents, a
        minimum play count) would keep it masked. Lifting that mask hands the
        retire-or-not decision to the PPO instead of a forced override. The
        resolver targets the wedged agent with ``bypass_preconditions``.
        Suppressed during DRAINING, which the drain short-circuit owns.
        """
        if PlayType.END_AGENT not in V1_ACTION_ORDER:
            return
        if self._state.session_state == SessionState.DRAINING:
            return
        if self._state.recovery_exhausted_agent_ids:
            self._mask[V1_ACTION_ORDER.index(PlayType.END_AGENT)] = True

    def _stage_candidate_required(self) -> None:
        for candidate_pt in _CANDIDATE_REQUIRED_PLAY_TYPES:
            if candidate_pt in V1_ACTION_ORDER and not self._candidate_plan.candidates_for(
                candidate_pt
            ):
                self._mask[V1_ACTION_ORDER.index(candidate_pt)] = False

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

    def _stage_instantiate_config(self) -> None:
        if PlayType.INSTANTIATE_AGENT not in V1_ACTION_ORDER:
            return
        idx = V1_ACTION_ORDER.index(PlayType.INSTANTIATE_AGENT)
        # Don't open an *empty* fleet when there is nothing for the agent to do:
        # zero active agents AND no remaining work AND not a terminal QA-setup.
        # Without this an empty no-work fleet keeps INSTANTIATE_AGENT selectable
        # forever, so the loop spins spawning idle agents instead of going idle.
        # Scaling a non-empty fleet stays the policy's call (handled below); and
        # when work remains or we are in terminal no-work, the first spawn is
        # legitimately valid so the PPO can open the fleet or set up final QA.
        no_active_agents = not any(
            a.status in (AgentStatus.IDLE, AgentStatus.BUSY) for a in self._state.agents
        )
        if (
            no_active_agents
            and not self._candidate_plan.has_remaining_work
            and not self._candidate_plan.work_availability.terminal_no_work
        ):
            self._mask[idx] = False
            return
        if self._cfg is None or self._config_index is None:
            return
        config_mask = compute_config_mask(self._state, self._cfg, self._config_index)
        if not config_mask.any():
            self._mask[idx] = False

    def _stage_end_session(self) -> None:
        # END_SESSION is masked only while genuinely-actionable work remains.
        # ``terminal_no_work`` already folds in "graph has epics, terminal
        # audits fresh, nothing in flight, no actionable work" — once that holds
        # the PPO is free to end the session (or keep going) as a judgment call.
        # No closure-ratio or failure-streak gate: ending is a directional
        # decision the policy owns, not a deterministic threshold.
        if PlayType.END_SESSION not in V1_ACTION_ORDER:
            return
        if not self._candidate_plan.work_availability.terminal_no_work:
            self._mask[V1_ACTION_ORDER.index(PlayType.END_SESSION)] = False

    def _stage_take_break(self) -> None:
        if PlayType.TAKE_BREAK not in V1_ACTION_ORDER:
            return
        has_break_trigger = any(
            a.status == AgentStatus.ERROR
            and a.last_error_class in ("rate_limit", "unknown")
            and a.current_play_type != PlayType.TAKE_BREAK
            for a in self._state.agents
        )
        if not has_break_trigger:
            self._mask[V1_ACTION_ORDER.index(PlayType.TAKE_BREAK)] = False

    def _stage_reserved_slots(self) -> None:
        for reserved in (PlayType.FUTURE_7, PlayType.FUTURE_8):
            if reserved in V1_ACTION_ORDER:
                self._mask[V1_ACTION_ORDER.index(reserved)] = False

    # -- short-circuit stages (replace mask entirely when they fire) ----------

    def _stage_drain_mode(self) -> bool:
        if (
            self._state.session_state == SessionState.DRAINING
            and PlayType.END_AGENT in V1_ACTION_ORDER
        ):
            self._mask[:] = False
            self._mask[V1_ACTION_ORDER.index(PlayType.END_AGENT)] = True
            return True
        return False

    # -- pipeline entry points -----------------------------------------------

    def build(self, *, apply_reverse_failsafe: bool = False) -> NDArray[np.bool_]:
        """Run the full mask pipeline and return the result."""
        self._stage_preconditions()
        self._stage_agent_eligibility()
        self._stage_wedged_end_agent()
        self._stage_candidate_required()
        self._stage_consecutive_failure_breaker()
        self._stage_instantiate_config()
        self._stage_end_session()
        self._stage_take_break()
        self._stage_reserved_slots()

        if self._stage_drain_mode():
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
        """Run the pipeline and return a reason for every masked play type."""
        mask = self.build(apply_reverse_failsafe=apply_reverse_failsafe)
        state = self._state
        cfg = self._cfg
        candidate_plan = self._candidate_plan
        config_index = self._config_index

        elig_mask = (
            compute_agent_eligibility_mask(state, self._registry, cfg=cfg)
            if cfg is not None
            else None
        )

        reasons: dict[PlayType, MaskReason] = {}

        if state.session_state == SessionState.DRAINING:
            for pt in V1_ACTION_ORDER:
                if pt != PlayType.END_AGENT:
                    reasons[pt] = SESSION_DRAINING
            return reasons

        for i, pt in enumerate(V1_ACTION_ORDER):
            if mask[i]:
                continue

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

            if pt in (PlayType.FUTURE_7, PlayType.FUTURE_8):
                reasons[pt] = RESERVED_SLOT
            elif pt == PlayType.TAKE_BREAK:
                reasons[pt] = MaskReason(
                    text="No agent in rate_limit or unknown-error state",
                    classification=MaskClassification.HARD,
                    source=MaskSource.PRECONDITION,
                )
            elif pt == PlayType.END_SESSION:
                reasons[pt] = self._end_session_reason(candidate_plan)
            elif pt == PlayType.INSTANTIATE_AGENT:
                no_active_agents = not any(
                    a.status in (AgentStatus.IDLE, AgentStatus.BUSY) for a in state.agents
                )
                if (
                    no_active_agents
                    and not candidate_plan.has_remaining_work
                    and not candidate_plan.work_availability.terminal_no_work
                ):
                    reasons[pt] = MaskReason(
                        text="No agents and no remaining work — nothing to spawn an agent for",
                        classification=MaskClassification.INDEFINITE_WAIT,
                        source=MaskSource.PRECONDITION,
                    )
                elif (
                    cfg is not None
                    and config_index is not None
                    and not compute_config_mask(state, cfg, config_index).any()
                ):
                    reasons[pt] = _instantiate_config_block_reason(state, cfg, config_index)
                else:
                    reasons[pt] = _precondition_reason(self._registry, pt, state)
            elif elig_mask is not None and not elig_mask[i] and cfg is not None:
                reasons[pt] = _eligibility_reason(state, self._registry, pt, cfg)
            else:
                reasons[pt] = _precondition_reason(self._registry, pt, state)
            if (
                pt in _CANDIDATE_REQUIRED_PLAY_TYPES
                and not candidate_plan.candidates_for(pt)
                and reasons.get(pt) == NOT_AVAILABLE
            ):
                blocked_reasons_for_pt = candidate_plan.blocked_reasons_by_play_type.get(pt, ())
                blocked_text = (
                    blocked_reasons_for_pt[0]
                    if blocked_reasons_for_pt
                    else f"no {pt.value} candidates"
                )
                reasons[pt] = MaskReason(
                    text=blocked_text,
                    classification=MaskClassification.HARD,
                    source=MaskSource.CANDIDATE,
                )

        return reasons

    def _end_session_reason(self, candidate_plan: PlayCandidatePlan) -> MaskReason:
        # Mirrors ``_stage_end_session``: the only gate is "actionable work
        # still remains." Once no actionable work is left, ending is the
        # policy's call, so any residual mask falls through to the registry
        # precondition reason.
        if not candidate_plan.work_availability.terminal_no_work:
            return MaskReason(
                text="Actionable work still remains",
                classification=MaskClassification.INDEFINITE_WAIT,
                source=MaskSource.TERMINAL,
            )
        return _precondition_reason(self._registry, PlayType.END_SESSION, self._state)


# ---------------------------------------------------------------------------
# Backward-compatible free functions
# ---------------------------------------------------------------------------


def _stage_preconditions(state: OrchestratorState, registry: PlayRegistry) -> NDArray[np.bool_]:
    """Seed a fresh mask from the registry's per-play precondition checks."""
    builder = ActionMaskBuilder(state, registry)
    builder._stage_preconditions()
    return builder._mask


def _stage_agent_eligibility(
    mask: NDArray[np.bool_],
    state: OrchestratorState,
    registry: PlayRegistry,
    *,
    cfg: RuntimeConfig,
) -> NDArray[np.bool_]:
    """AND in the agent-eligibility mask (mutates ``mask`` in place)."""
    mask &= compute_agent_eligibility_mask(state, registry, cfg=cfg)
    return mask


def _stage_wedged_end_agent(mask: NDArray[np.bool_], state: OrchestratorState) -> NDArray[np.bool_]:
    """Re-enable END_AGENT when a recovery-exhausted agent exists (not draining)."""
    if PlayType.END_AGENT not in V1_ACTION_ORDER:
        return mask
    if state.session_state == SessionState.DRAINING:
        return mask
    if state.recovery_exhausted_agent_ids:
        mask[V1_ACTION_ORDER.index(PlayType.END_AGENT)] = True
    return mask


def _stage_candidate_required(
    mask: NDArray[np.bool_], candidate_plan: PlayCandidatePlan
) -> NDArray[np.bool_]:
    """Zero out candidate-required plays that have no concrete candidate."""
    for candidate_pt in _CANDIDATE_REQUIRED_PLAY_TYPES:
        if candidate_pt in V1_ACTION_ORDER and not candidate_plan.candidates_for(candidate_pt):
            mask[V1_ACTION_ORDER.index(candidate_pt)] = False
    return mask


def _stage_instantiate_config(
    mask: NDArray[np.bool_],
    state: OrchestratorState,
    *,
    cfg: RuntimeConfig,
    config_index: tuple[ConfigKey, ...],
) -> NDArray[np.bool_]:
    """Zero ``INSTANTIATE_AGENT`` when no spawn config is viable or there is no work."""
    if PlayType.INSTANTIATE_AGENT not in V1_ACTION_ORDER:
        return mask
    idx = V1_ACTION_ORDER.index(PlayType.INSTANTIATE_AGENT)
    candidate_plan = build_candidate_plan(state)
    no_active_agents = not any(
        a.status in (AgentStatus.IDLE, AgentStatus.BUSY) for a in state.agents
    )
    if (
        no_active_agents
        and not candidate_plan.has_remaining_work
        and not candidate_plan.work_availability.terminal_no_work
    ):
        mask[idx] = False
        return mask
    config_mask = compute_config_mask(state, cfg, config_index)
    if not config_mask.any():
        mask[idx] = False
    return mask


def _stage_end_session(
    mask: NDArray[np.bool_],
    state: OrchestratorState,
    candidate_plan: PlayCandidatePlan,
) -> NDArray[np.bool_]:
    """Zero out ``END_SESSION`` while genuinely-actionable work remains."""
    if PlayType.END_SESSION not in V1_ACTION_ORDER:
        return mask
    if not candidate_plan.work_availability.terminal_no_work:
        mask[V1_ACTION_ORDER.index(PlayType.END_SESSION)] = False
    return mask


def _stage_take_break(mask: NDArray[np.bool_], state: OrchestratorState) -> NDArray[np.bool_]:
    """Zero out ``TAKE_BREAK`` unless an agent is in rate_limit/unknown error."""
    if PlayType.TAKE_BREAK not in V1_ACTION_ORDER:
        return mask
    has_break_trigger = any(
        a.status == AgentStatus.ERROR
        and a.last_error_class in ("rate_limit", "unknown")
        and a.current_play_type != PlayType.TAKE_BREAK
        for a in state.agents
    )
    if not has_break_trigger:
        mask[V1_ACTION_ORDER.index(PlayType.TAKE_BREAK)] = False
    return mask


def _stage_reserved_slots(mask: NDArray[np.bool_]) -> NDArray[np.bool_]:
    """Zero out reserved tensor slots that are not currently valid actions."""
    for reserved in (PlayType.FUTURE_7, PlayType.FUTURE_8):
        if reserved in V1_ACTION_ORDER:
            mask[V1_ACTION_ORDER.index(reserved)] = False
    return mask


def _stage_drain_mode(state: OrchestratorState) -> NDArray[np.bool_] | None:
    """Short-circuit when draining: return an ``END_AGENT``-only mask, else ``None``."""
    if state.session_state == SessionState.DRAINING and PlayType.END_AGENT in V1_ACTION_ORDER:
        drain_mask = np.zeros(NUM_ACTIONS, dtype=bool)
        drain_mask[V1_ACTION_ORDER.index(PlayType.END_AGENT)] = True
        return drain_mask
    return None


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


def _precondition_reason(
    registry: PlayRegistry, pt: PlayType, state: OrchestratorState
) -> MaskReason:
    """Return the first unmet precondition for *pt*, or a generic fallback."""
    try:
        unmet = registry.get(pt).preconditions(state)
        return unmet[0] if unmet else NOT_AVAILABLE
    except (KeyError, ValueError, AttributeError, RuntimeError) as exc:
        _logger.debug("precondition_check_failed", play=pt.value, error=str(exc))
        return NOT_AVAILABLE


def _eligibility_reason(
    state: OrchestratorState,
    registry: PlayRegistry,
    pt: PlayType,
    cfg: RuntimeConfig,
) -> MaskReason:
    """Return a typed MaskReason explaining why the eligibility gate fired."""
    try:
        play = registry.get(pt)
    except KeyError:
        return MaskReason(
            text="No play registered",
            classification=MaskClassification.HARD,
            source=MaskSource.ELIGIBILITY,
        )

    cap_key: str | None = play.capability
    if cap_key is None:
        return NOT_AVAILABLE

    allowed_tiers = allowed_tiers_for(pt)
    excluded_types = set(cfg.agent_preferences.exclude.get(pt.value, []))

    idle = [a for a in state.agents if a.status == AgentStatus.IDLE]

    # Diagnose most-specific reason in priority order. All eligibility
    # reasons are TRANSIENT — they clear as soon as an idle agent matching
    # the constraints appears.
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

    # Must be anti-confirmation (CODE_REVIEW).
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
