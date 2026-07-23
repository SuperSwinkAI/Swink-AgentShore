"""PPOSelector — RL-driven play selector implementing PlaySelector Protocol.

Wraps ActorCritic + RolloutBuffer + PPOUpdater + MetricsEngine into the
PlaySelector interface the Orchestrator expects.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
import torch

from agentshore.beads import GraphReadError, load_graph
from agentshore.config.models import PolicyMode
from agentshore.paths import GLOBAL_WEIGHTS_DIR as _GLOBAL_WEIGHTS_DIR
from agentshore.plays.base import PlayParams
from agentshore.plays.candidates import PlayCandidatePlan, build_candidate_plan
from agentshore.rl.action_space import INDEX_TO_PLAY, NUM_ACTIONS

# Re-exported from checkpoint_store so agentshore.core.phases import sites keep
# resolving them through the selector module.
from agentshore.rl.checkpoint_store import (
    _archive_old_canonicals as _archive_old_canonicals,
)
from agentshore.rl.checkpoint_store import (
    _prune_local_checkpoints as _prune_local_checkpoints,
)
from agentshore.rl.checkpoint_store import (
    cleanup_stale_canonical_weights as cleanup_stale_canonical_weights,
)
from agentshore.rl.checkpoint_store import (
    write_global_canonical_blocking,
)
from agentshore.rl.cold_start import apply_cold_start_bias, apply_cold_start_config_bias
from agentshore.rl.eligibility import EligibilityAuthority
from agentshore.rl.experience import RolloutBuffer, Step
from agentshore.rl.mask import (
    TerminalNoWorkDecision,
    compute_action_mask,
    compute_config_mask,
    compute_reverse_failsafe_mask,
    compute_terminal_no_work_config_mask,
    compute_terminal_no_work_decision,
    reverse_failsafe_should_unmask,
)
from agentshore.rl.mask_reason import MaskReason
from agentshore.rl.observation import encode_observation
from agentshore.rl.policy import ActorCritic

# Re-exported from selector_diagnostics so existing imports of these
# formerly-module-local helpers keep resolving through this module.
from agentshore.rl.selector_diagnostics import (
    _is_capacity_wait as _is_capacity_wait,
)
from agentshore.rl.selector_diagnostics import (
    _mask_reasons_by_play as _mask_reasons_by_play,
)
from agentshore.rl.selector_diagnostics import (
    _only_capacity_waiting as _only_capacity_waiting,
)
from agentshore.rl.selector_diagnostics import (
    log_all_masked as _diag_log_all_masked,
)
from agentshore.rl.selector_diagnostics import (
    log_resolver_exhausted as _diag_log_resolver_exhausted,
)
from agentshore.rl.selector_diagnostics import (
    log_reverse_failsafe as _diag_log_reverse_failsafe,
)
from agentshore.rl.selector_diagnostics import (
    log_terminal_no_work as _diag_log_terminal_no_work,
)
from agentshore.rl.selector_lifecycle import reload_shared_weights
from agentshore.rl.selector_lifecycle import (
    save_checkpoint as lifecycle_save_checkpoint,
)
from agentshore.rl.training import PPOUpdater, UpdateStats
from agentshore.state import AgentStatus, PlayType

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from agentshore.beads import ProjectGraph
    from agentshore.config import RLConfig
    from agentshore.config.models import RuntimeConfig
    from agentshore.data.store import DataStore
    from agentshore.plays.registry import PlayRegistry
    from agentshore.plays.resolver import ParameterResolver
    from agentshore.rl.config_head import ConfigKey
    from agentshore.rl.eligibility import LiveGraphLoader
    from agentshore.rl.metrics import MetricsEngine
    from agentshore.state import OrchestratorState


_logger = structlog.get_logger(__name__)


_REVERSE_FAILSAFE_BYPASS_PRECONDITION_PLAYS = frozenset(
    {
        PlayType.INSTANTIATE_AGENT,
        PlayType.ISSUE_PICKUP,
        PlayType.WRITE_IMPLEMENTATION_PLAN,
        PlayType.RUN_QA,
        PlayType.END_AGENT,
        PlayType.END_SESSION,
    }
)

# Pure fleet-management plays. A mask leaving only these means "no real work" —
# the auto reverse-failsafe must keep counting idle ticks, else lifecycle-only
# churn pins the counter below threshold and END_SESSION never opens (#166).
_LIFECYCLE_PLAY_TYPES = frozenset({PlayType.INSTANTIATE_AGENT, PlayType.END_AGENT})


def _build_updater(policy: ActorCritic, cfg: RLConfig) -> PPOUpdater:
    """Construct the ``PPOUpdater`` shared by ``from_cold_start`` and ``load``.

    Both factories build an identical updater around a differently-sourced
    policy (fresh cold-start weights vs. loaded-from-disk weights); this is
    the one place that wires ``cfg`` into ``PPOUpdater`` so the two stay in
    sync.
    """
    return PPOUpdater(
        policy,
        lr=cfg.learning_rate,
        clip_eps=cfg.ppo.clip_epsilon,
        value_coef=cfg.ppo.value_loss_coef,
        entropy_coef=cfg.entropy_coef,
        ppo_epochs=cfg.ppo.ppo_epochs,
        mini_batch_size=cfg.ppo.mini_batch_size,
        max_grad_norm=cfg.ppo.max_grad_norm,
        config_policy_coef=cfg.config_policy_coef,
        config_entropy_coef=cfg.config_entropy_coef,
    )


@dataclass(slots=True)
class _PendingStep:
    """State captured at select() time; consumed by on_play_completed()."""

    obs: NDArray[np.float32]
    action: int
    log_prob: float
    value: float
    mask: NDArray[np.bool_]
    # Config-head state, set only when the play was INSTANTIATE_AGENT.
    config_action: int | None = None
    config_log_prob: float | None = None
    config_mask: NDArray[np.bool_] | None = None


class PPOSelector:
    """Selects plays via a trained ActorCritic policy (PPO).

    Implements the ``PlaySelector`` Protocol; drop-in replacement for
    ``PPOSelector``.
    """

    def __init__(
        self,
        *,
        policy: ActorCritic,
        resolver: ParameterResolver,
        registry: PlayRegistry,
        buffer: RolloutBuffer,
        updater: PPOUpdater,
        metrics: MetricsEngine,
        cfg: RLConfig,
        policy_mode: PolicyMode = PolicyMode.LEARNING,
        policy_version: str = "ppo-v1",
        config_hash: str = "",
        orchestrator_cfg: RuntimeConfig | None = None,
        config_index: tuple[ConfigKey, ...] = (),
    ) -> None:
        self._policy = policy
        self._resolver = resolver
        self._registry = registry
        self._buffer = buffer
        self._updater = updater
        self._metrics = metrics
        self._cfg = cfg
        self._policy_mode = policy_mode
        self._policy_version = policy_version
        self._config_hash = config_hash
        self._orchestrator_cfg = orchestrator_cfg
        self._config_index = config_index
        self._pending: _PendingStep | None = None
        self._no_available_play_ticks: int = 0
        # Dedup key for the verbose per-play mask map (see _masked_plays_log_field).
        # An all-masked wedge re-emits its diagnostic every selector tick with an
        # identical mask map; we log the full map only when it changes so a frozen
        # wedge doesn't flood the log with repeated ~20-entry dumps.
        self._last_masked_plays_digest: str | None = None
        # Confirm-rejection clean re-picks during the last select(); drained by
        # consume_repick_count() into the executor_skip_rate_recent_50 window.
        # Reset at the top of every select().
        self._last_select_repick_count: int = 0
        # Global-weights snapshot at last reload; the delta base for
        # concurrent-safe canonical updates.
        self._reload_base: dict[str, torch.Tensor] | None = None
        # Set when the last reload found a canonical incompatible with this
        # session (load failure or config-index mismatch). save_checkpoint must
        # then NOT write back — we'd overwrite a good on-disk checkpoint with
        # stale local weights. A missing canonical is NOT a rejection (it's the
        # legitimate first-write / full-overwrite case).
        self._reload_rejected: bool = False

    def update_orchestrator_cfg(self, cfg: RuntimeConfig) -> None:
        """Adopt a reloaded ``RuntimeConfig`` mid-session.

        The selector captures the orchestrator config at construction and reads
        it on every ``select()`` to build the action mask — including the
        hard-mask for plays the user has disabled via Preferences. A SIGHUP /
        desktop-triggered reload swaps ``SessionRuntime.cfg`` but does not
        re-create the selector, so without this refresh the selector would keep
        masking against the bootstrap config and a play disabled mid-session
        would stay selectable until the session restarts. The RL sub-config
        (``cfg.rl``) is deliberately not reloadable; only the orchestrator-config
        reference the mask path reads is refreshed here.
        """
        self._orchestrator_cfg = cfg

    # ------------------------------------------------------------------
    # PlaySelector protocol
    # ------------------------------------------------------------------

    async def select(self, state: OrchestratorState) -> tuple[PlayType, PlayParams] | None:
        # Fresh confirm-repick tally for this cycle.
        self._last_select_repick_count = 0

        ctx = await self._metrics.snapshot(state)
        obs = encode_observation(state, ctx, config_index=self._config_index)
        candidate_plan = build_candidate_plan(state)
        base_mask = compute_action_mask(
            state,
            self._registry,
            cfg=self._orchestrator_cfg,
            config_index=self._config_index or None,
            apply_reverse_failsafe=False,
            candidate_plan=candidate_plan,
        )
        terminal_no_work = compute_terminal_no_work_decision(
            state,
            self._registry,
            cfg=self._orchestrator_cfg,
            config_index=self._config_index or None,
            candidate_plan=candidate_plan,
        )
        if terminal_no_work is not None:
            self._log_terminal_no_work(state, terminal_no_work)
        reverse_failsafe = False
        mask = base_mask
        manual_reverse_failsafe = bool(self._cfg.reverse_failsafe_enabled)
        auto_reverse_failsafe = (
            self._cfg.reverse_failsafe_enabled
            and self._auto_reverse_failsafe_should_unmask(state, mask)
        )
        if (manual_reverse_failsafe or auto_reverse_failsafe) and reverse_failsafe_should_unmask(
            state
        ):
            mask = compute_reverse_failsafe_mask(
                state,
                cfg=self._orchestrator_cfg,
                config_index=self._config_index or None,
                allow_control_plays=auto_reverse_failsafe,
                base_mask=base_mask,
            )
            reverse_failsafe = bool(mask.any())
            if reverse_failsafe:
                self._log_reverse_failsafe(
                    state,
                    mask,
                    auto_enabled=auto_reverse_failsafe and not manual_reverse_failsafe,
                )

        if not mask.any():
            self._log_all_masked(state)
            return None

        # Single source of truth for play validity, built from the snapshot
        # candidate plan above. confirm() does the one live read (fresh beads
        # reload) against the *resolved* target after the resolver claims it.
        live_loader = self._build_live_graph_loader()
        authority = EligibilityAuthority(
            state,
            self._registry,
            cfg=self._orchestrator_cfg,
            config_index=self._config_index or None,
            candidate_plan=candidate_plan,
            live_graph_loader=live_loader,
        )

        obs_tensor = torch.tensor(obs, dtype=torch.float32)
        remaining_mask = mask.copy()
        # Every non-returning iteration masks its sampled action, so the loop is
        # self-bounded by the valid-action count. Cap at NUM_ACTIONS so a run of
        # clean re-picks exhausts the valid plays without livelocking.
        max_attempts = NUM_ACTIONS
        attempted_plays: list[str] = []
        for attempt in range(max_attempts):
            if not remaining_mask.any():
                self._log_resolver_exhausted(
                    state,
                    mask,
                    attempted_plays=attempted_plays,
                    reason="remaining_mask_empty",
                    attempt=attempt,
                    candidate_plan=candidate_plan,
                )
                return None

            rm_tensor = torch.tensor(remaining_mask, dtype=torch.bool)
            action, log_prob, value = self._policy.act(
                obs_tensor, rm_tensor, greedy=self._policy_mode.greedy_selection
            )
            play_type = INDEX_TO_PLAY[action]
            attempted_plays.append(play_type.value)

            # If the play head picked INSTANTIATE_AGENT and the config head is
            # active, sample a config and pass it to the resolver. Otherwise
            # the resolver falls back to its priority/round-robin logic.
            config_override: tuple[str, str] | None = None
            cfg_action: int | None = None
            cfg_log_prob: float | None = None
            cfg_mask_arr: NDArray[np.bool_] | None = None
            if (
                play_type == PlayType.INSTANTIATE_AGENT
                and self._orchestrator_cfg is not None
                and self._config_index
                and (
                    self._policy.num_configs > 0
                    or (terminal_no_work is not None and terminal_no_work.mode == "spawn_large_qa")
                )
            ):
                if terminal_no_work is not None and terminal_no_work.mode == "spawn_large_qa":
                    cfg_mask_arr = compute_terminal_no_work_config_mask(
                        state,
                        self._orchestrator_cfg,
                        self._config_index,
                    )
                else:
                    cfg_mask_arr = compute_config_mask(
                        state, self._orchestrator_cfg, self._config_index
                    )
                if cfg_mask_arr.any():
                    if self._policy.num_configs > 0:
                        cm_tensor = torch.tensor(cfg_mask_arr, dtype=torch.bool)
                        cfg_action, cfg_log_prob = self._policy.act_config(
                            obs_tensor, cm_tensor, greedy=self._policy_mode.greedy_selection
                        )
                    else:
                        cfg_action = next(
                            i for i, enabled in enumerate(cfg_mask_arr) if bool(enabled)
                        )
                        cfg_log_prob = None
                    config_override = self._config_index[cfg_action]
                else:
                    # Config mask all-zero despite play mask allowing it — log
                    # and fall through to resolver fallback. compute_action_mask
                    # should have prevented this, so this is a safety net.
                    _logger.warning(
                        "ppo_selector.no_eligible_config",
                        agents=len(state.agents),
                    )

            # Resolve + claim FIRST: the resolver picks the concrete target and
            # acquires its work-claim (the atomic CAS that detects a lost race).
            params = await self._resolver.resolve(
                play_type, state, config_index_override=config_override
            )
            if params is None and reverse_failsafe and play_type == PlayType.END_AGENT:
                params = self._resolve_reverse_failsafe_end_agent(state)

            if params is None:
                # No claimable target (pool drained or the claim CAS was lost to
                # a sibling). Clean re-pick: re-mask + resample, no self._pending,
                # no plays-table skip row, no RL experience sample. (The confirm-
                # repick counter tracks live-confirm rejections specifically, so a
                # lost CAS is logged but not counted there.)
                remaining_mask[action] = False
                self._log_clean_repick(
                    "claim_lost_repick",
                    play_type=play_type,
                    attempt=attempt,
                    reason=None,
                )
                # No early break: a clean re-pick masks this action, so the loop
                # is bounded by remaining_mask / max_attempts. Breaking on a small
                # budget could idle (None) while other valid actions remain.
                continue

            if terminal_no_work is not None and play_type in (
                PlayType.INSTANTIATE_AGENT,
                PlayType.RUN_QA,
            ):
                params = replace(
                    params,
                    bypass_preconditions=True,
                    extras={
                        **params.extras,
                        "terminal_no_work": True,
                        "terminal_no_work_mode": terminal_no_work.mode,
                    },
                )
            if reverse_failsafe:
                rf_params = self._prepare_reverse_failsafe_params(play_type, params, state)
                if rf_params is None:
                    # The failsafe-precondition retry discards this candidate, so
                    # release the work-claim resolve() just acquired before we
                    # drop the params handle — otherwise the resource leaks held.
                    await self._resolver.release_claim(state, params)
                    remaining_mask[action] = False
                    _logger.debug(
                        "ppo_selector.reverse_failsafe_precondition_retry",
                        play_type=play_type.value,
                        attempt=attempt,
                    )
                    continue
                params = rf_params

            # Live confirm the RESOLVED target before committing to dispatch. One
            # live read (fresh beads-graph reload) decides whether the specific
            # issue/PR we just claimed is still valid — catching a sibling that
            # flipped its bead in_progress between selection and now. A rejection
            # is pure drift: release the claim we took and cleanly re-pick (no
            # self._pending, no skip row, no RL sample).
            #
            # Reverse failsafe is the explicit escape hatch that DELIBERATELY
            # lifts the A-type validity gates to break an all-masked deadlock, so
            # confirm is bypassed while it's active (the failsafe params already
            # carry the precondition-bypass contract downstream).
            if not reverse_failsafe and not params.bypass_preconditions:
                decision = await authority.confirm(play_type, params, state)
                if not decision.valid:
                    await self._resolver.release_claim(state, params)
                    remaining_mask[action] = False
                    self._last_select_repick_count += 1
                    self._log_clean_repick(
                        "confirm_repick",
                        play_type=play_type,
                        attempt=attempt,
                        reason=decision.reason,
                    )
                    continue

            self._pending = _PendingStep(
                obs=obs,
                action=action,
                log_prob=log_prob,
                value=value,
                mask=mask,
                config_action=cfg_action,
                config_log_prob=cfg_log_prob,
                config_mask=cfg_mask_arr,
            )
            self._no_available_play_ticks = 0
            # A play dispatched → the all-masked streak (if any) ended; clear the
            # dedup digest so the next distinct wedge re-emits its full map.
            self._last_masked_plays_digest = None
            return play_type, params

        self._log_resolver_exhausted(
            state,
            mask,
            attempted_plays=attempted_plays,
            reason="attempt_limit",
            attempt=max_attempts,
            candidate_plan=candidate_plan,
        )
        return None

    def _build_live_graph_loader(self) -> LiveGraphLoader | None:
        """Build the one live read confirm() may perform: a beads-graph reload.

        Bound to the resolver's repo path. Returns None when no path is
        available (tests / non-beads sessions), in which case confirm() falls
        back to the snapshot — still correct, just without fresh-beads drift
        detection.
        """
        project_path = self._resolver.project_path
        if project_path is None:
            return None

        async def _load() -> ProjectGraph | None:
            try:
                # max_age_seconds=0.0: this is the drift check itself — it
                # must observe the live graph, not a TTL-cached one, or it
                # can't catch a sibling that flipped a bead in_progress since
                # selection (see the docstring above ``confirm()``).
                return await load_graph(project_path, max_age_seconds=0.0)
            except GraphReadError:
                return None

        return _load

    def _log_clean_repick(
        self,
        event: str,
        *,
        play_type: PlayType,
        attempt: int,
        reason: MaskReason | None,
    ) -> None:
        """Structured log for a clean re-pick (confirm rejection or lost claim).

        A clean re-pick re-masks the action and resamples; it contributes zero
        RL steps (no self._pending) and is never a plays-table skip row.
        """
        # ``reason`` is typed ``MaskReason | None``; guard against a malformed
        # non-typed reason leaking through so a clean re-pick can never crash the
        # selector (the re-pick must stay a pure resample, not raise).
        classification = reason.classification.value if isinstance(reason, MaskReason) else None
        _logger.info(
            f"ppo_selector.{event}",
            play_type=play_type.value,
            attempt=attempt,
            reason=str(reason) if reason is not None else None,
            reason_classification=classification,
            repicks_this_cycle=self._last_select_repick_count,
        )

    def _auto_reverse_failsafe_should_unmask(
        self,
        state: OrchestratorState,
        mask: NDArray[np.bool_],
    ) -> bool:
        """Return True once repeated idle all-masked ticks should open the failsafe.

        We deliberately do NOT reset the counter on ``state.in_flight_plays``.
        Production sessions hit a soft-deadlock where one play hangs in flight
        for 20+ minutes (calibrate_alignment stall, observed 2026-05-28) while
        every other action is masked and every agent is IDLE. The previous
        guard kept the failsafe permanently disabled in exactly that scenario.
        ``fleet_quiescent`` still gates premature firing during healthy
        short-lived all-masked windows where some agent is doing real work.

        An agent only counts as "doing real work" when it is BUSY on a
        *dispatchable* play. IDLE agents have nothing to do; an ERROR agent is
        stuck (its own recovery never produces forward progress); and an agent
        sleeping in TAKE_BREAK is quiescent by definition. Treating ERROR / break
        agents as busy used to pin the counter at zero forever the moment one
        agent wedged in ERROR — so the failsafe never armed and END_SESSION never
        lifted. Observed on Windows: a transient ``API Error: 529 Overloaded``
        left a Claude agent in ERROR while the rest of the fleet idled with all
        work masked, and the session could not wind itself down.
        """

        threshold = self._cfg.reverse_failsafe_after_idle_ticks
        if threshold <= 0:
            self._no_available_play_ticks = 0
            return False
        active_agents = [
            agent
            for agent in state.agents
            if agent.status in (AgentStatus.IDLE, AgentStatus.BUSY, AgentStatus.ERROR)
        ]
        fleet_quiescent = bool(active_agents) and not any(
            agent.status == AgentStatus.BUSY and agent.current_play_type != PlayType.TAKE_BREAK
            for agent in active_agents
        )
        # A mask that only leaves lifecycle plays (INSTANTIATE_AGENT / END_AGENT)
        # selectable still means "no dispatchable work" — keep counting so the
        # failsafe can arm and lift END_SESSION even while a reap slips through.
        has_real_play = any(
            bool(mask[i]) and INDEX_TO_PLAY[i] not in _LIFECYCLE_PLAY_TYPES
            for i in range(NUM_ACTIONS)
        )
        if has_real_play or not fleet_quiescent:
            self._no_available_play_ticks = 0
            return False

        self._no_available_play_ticks += 1
        return self._no_available_play_ticks >= threshold

    @staticmethod
    def _resolve_reverse_failsafe_end_agent(state: OrchestratorState) -> PlayParams | None:
        idle_agents = [agent for agent in state.agents if agent.status == AgentStatus.IDLE]
        if not idle_agents:
            return None
        agent = min(
            idle_agents,
            key=lambda item: (
                -(item.tasks_completed + item.tasks_failed),
                item.agent_type.value,
                item.agent_id,
            ),
        )
        return PlayParams(agent_id=agent.agent_id)

    def _prepare_reverse_failsafe_params(
        self,
        play_type: PlayType,
        params: PlayParams,
        state: OrchestratorState,
    ) -> PlayParams | None:
        """Annotate failsafe selections and bypass only policy-backpressure gates."""
        try:
            preconditions_met = self._registry.preconditions_met(play_type, state)
        except (KeyError, ValueError, AttributeError, RuntimeError) as exc:
            _logger.debug(
                "reverse_failsafe_precondition_check_failed",
                play_type=play_type.value,
                error=str(exc),
            )
            preconditions_met = False

        extras = {**params.extras, "reverse_failsafe": True}
        if preconditions_met or params.bypass_preconditions:
            return replace(params, extras=extras)
        if play_type in _REVERSE_FAILSAFE_BYPASS_PRECONDITION_PLAYS:
            return replace(
                params,
                bypass_preconditions=True,
                extras={**extras, "reverse_failsafe_bypassed_preconditions": True},
            )
        return None

    def _masked_plays_log_field(self, reasons: dict[PlayType, MaskReason]) -> dict[str, object]:
        """Return the per-play mask-map log field(s), deduped across ticks.

        The full ``play -> reason`` map (``_mask_reasons_by_play``) is what makes a
        wedge diagnosable, but the all-masked diagnostics re-fire every selector
        tick and a frozen wedge would then dump an identical ~20-entry map on each
        one. So the full map is emitted only on the first tick and whenever it
        changes; byte-identical repeats collapse to ``{"masked_plays_unchanged":
        True}``. Net: full detail on every transition, no flooding while stuck.
        """
        mapping = _mask_reasons_by_play(reasons)
        digest = repr(list(mapping.items()))
        if digest == self._last_masked_plays_digest:
            return {"masked_plays_unchanged": True}
        self._last_masked_plays_digest = digest
        return {"masked_plays": mapping}

    # The four methods below are thin wrappers over rl/selector_diagnostics.py
    # (TNQA wave-2 selector.py split): they supply this selector's registry/cfg/
    # config-index and the stateful masked-plays digest cache, and the module
    # does the candidate-plan/mask-reasons/Counter field building and the
    # actual structured log call. Event names and field sets are unchanged.

    def _log_all_masked(self, state: OrchestratorState) -> None:
        """Emit an actionable all-masked diagnostic with severity based on capacity."""
        _diag_log_all_masked(
            state,
            self._registry,
            cfg=self._orchestrator_cfg,
            config_index=self._config_index or None,
            masked_plays_log_field=self._masked_plays_log_field,
        )

    def _log_terminal_no_work(
        self,
        state: OrchestratorState,
        decision: TerminalNoWorkDecision,
    ) -> None:
        """Emit a structured terminal no-work mask diagnostic."""
        _diag_log_terminal_no_work(state, decision)

    def _log_resolver_exhausted(
        self,
        state: OrchestratorState,
        mask: NDArray[np.bool_],
        *,
        attempted_plays: list[str],
        reason: str,
        attempt: int,
        candidate_plan: PlayCandidatePlan | None = None,
    ) -> None:
        """Warn when the policy had legal actions but no resolver produced parameters."""
        _diag_log_resolver_exhausted(
            state,
            self._registry,
            mask,
            cfg=self._orchestrator_cfg,
            config_index=self._config_index or None,
            attempted_plays=attempted_plays,
            reason=reason,
            attempt=attempt,
            candidate_plan=candidate_plan,
            masked_plays_log_field=self._masked_plays_log_field,
        )

    def _log_reverse_failsafe(
        self,
        state: OrchestratorState,
        mask: NDArray[np.bool_],
        *,
        auto_enabled: bool = False,
    ) -> None:
        """Emit diagnostics when the all-masked reverse failsafe opens fallback actions."""
        _diag_log_reverse_failsafe(
            state,
            self._registry,
            mask,
            cfg=self._orchestrator_cfg,
            config_index=self._config_index or None,
            auto_enabled=auto_enabled,
            no_available_play_ticks=self._no_available_play_ticks,
            masked_plays_log_field=self._masked_plays_log_field,
        )

    # ------------------------------------------------------------------
    # Experience collection
    # ------------------------------------------------------------------

    async def on_play_completed(
        self,
        *,
        state_before: OrchestratorState,
        next_state: OrchestratorState,
        reward: float,
        done: bool,
        pending_step: _PendingStep | None = None,
    ) -> None:
        """Record SARS transition into the rollout buffer."""
        step_data = pending_step if pending_step is not None else self._pending
        if step_data is None:
            return

        ctx_next = await self._metrics.snapshot(next_state)
        next_obs = encode_observation(next_state, ctx_next, config_index=self._config_index)

        step = Step(
            state=step_data.obs,
            action=step_data.action,
            reward=reward,
            next_state=next_obs,
            done=done,
            log_prob=step_data.log_prob,
            value=step_data.value,
            mask=step_data.mask,
            config_action=step_data.config_action,
            config_log_prob=step_data.config_log_prob,
            config_mask=step_data.config_mask,
        )
        self._buffer.add(step)
        self._pending = None

    def consume_pending(self) -> _PendingStep | None:
        """Return and clear the pending step (for orchestrator to write experience rows)."""
        p = self._pending
        self._pending = None
        return p

    def consume_repick_count(self) -> int:
        """Return and clear the confirm-repick count from the last ``select()`` call.

        A confirm-repick is a clean re-pick triggered when the EligibilityAuthority's
        one live ``confirm`` rejected a snapshot-eligible play (live drift). The
        orchestrator drains this once per selection cycle and folds it into the
        rolling window that feeds observation slot ``executor_skip_rate_recent_50``.
        Read-and-clear so a cycle with no ``select()`` call reports zero.
        """
        n = self._last_select_repick_count
        self._last_select_repick_count = 0
        return n

    # ------------------------------------------------------------------
    # Update / checkpoint scheduling
    # ------------------------------------------------------------------

    def should_update(self) -> bool:
        return (
            self._policy_mode.ppo_learning_enabled and len(self._buffer) >= self._cfg.update_every
        )

    def should_checkpoint(self, total_plays: int) -> bool:
        return (
            self._policy_mode.ppo_learning_enabled and total_plays % self._cfg.checkpoint_every == 0
        )

    async def update_policy(self, *, next_state_value: float | None = None) -> UpdateStats:
        """Reload latest shared weights, run GAE + PPO update, then clear buffer.

        Reloading before each update lets concurrent AgentShore sessions
        build on each other's improvements — each session applies its
        gradient update on top of the latest shared checkpoint.
        """
        if not self._policy_mode.ppo_learning_enabled or len(self._buffer) == 0:
            return UpdateStats()

        self._reload_shared_weights()

        last_value = next_state_value if next_state_value is not None else 0.0
        self._buffer.compute_advantages(
            last_value,
            gamma=self._cfg.gamma,
            gae_lambda=self._cfg.ppo.gae_lambda,
        )
        stats = self._updater.update(self._buffer)
        self._buffer.clear()
        return stats

    def _reload_shared_weights(self) -> None:
        """Load the latest shared policy from disk, with version safety.

        Thin wrapper over :func:`agentshore.rl.selector_lifecycle.
        reload_shared_weights`; see its docstring for the reload-rejection
        contract that ``save_checkpoint`` depends on.
        """
        outcome = reload_shared_weights(self._policy, _GLOBAL_WEIGHTS_DIR)
        self._reload_rejected = outcome.rejected
        if outcome.reload_base is not None:
            self._reload_base = outcome.reload_base

    async def value_of(self, state: OrchestratorState) -> float:
        """Bootstrap value estimate for GAE truncation."""
        ctx = await self._metrics.snapshot(state)
        obs = encode_observation(state, ctx, config_index=self._config_index)
        return self._policy.value(torch.tensor(obs, dtype=torch.float32))

    def _write_global_canonical_blocking(self, canonical: Path, lock_path: Path) -> None:
        """Apply this session's gradient delta to the global canonical under a lock.

        Thin wrapper around
        :func:`agentshore.rl.checkpoint_store.write_global_canonical_blocking`,
        binding this selector's current policy and reload base. Performs only
        synchronous I/O and must be called via ``asyncio.to_thread`` from the
        async ``save_checkpoint`` path.
        """
        write_global_canonical_blocking(self._policy, self._reload_base, canonical, lock_path)

    async def save_checkpoint(
        self,
        store: DataStore,
        session_id: str,
        weights_dir: Path,
        total_plays: int,
    ) -> None:
        """Write a numbered local checkpoint and update the global canonical.

        Thin wrapper over :func:`agentshore.rl.selector_lifecycle.save_checkpoint`;
        see its docstring for the delta-accumulation and reload-rejection contract.
        """
        await lifecycle_save_checkpoint(
            policy=self._policy,
            reload_base=self._reload_base,
            reload_rejected=self._reload_rejected,
            global_weights_dir=_GLOBAL_WEIGHTS_DIR,
            store=store,
            session_id=session_id,
            weights_dir=weights_dir,
            total_plays=total_plays,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def buffer(self) -> RolloutBuffer:
        return self._buffer

    @property
    def policy(self) -> ActorCritic:
        return self._policy

    @property
    def entropy_coef(self) -> float:
        """Current entropy coefficient used by PPO updates."""
        return self._updater.entropy_coef

    def set_entropy_coef(self, entropy_coef: float) -> None:
        """Update entropy coefficient for subsequent PPO updates."""
        self._updater.set_entropy_coef(entropy_coef)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_cold_start(
        cls,
        *,
        resolver: ParameterResolver,
        registry: PlayRegistry,
        metrics: MetricsEngine,
        cfg: RLConfig,
        policy_mode: PolicyMode = PolicyMode.LEARNING,
        policy_version: str = "ppo-v1",
        config_hash: str = "",
        orchestrator_cfg: RuntimeConfig | None = None,
        config_index: tuple[ConfigKey, ...] = (),
    ) -> PPOSelector:
        """Build a PPOSelector with cold-start bias applied to a fresh policy."""
        policy = ActorCritic(num_configs=len(config_index))
        apply_cold_start_bias(policy)
        if config_index:
            apply_cold_start_config_bias(policy, config_index)
        buffer = RolloutBuffer(capacity=256)
        updater = _build_updater(policy, cfg)
        return cls(
            policy=policy,
            resolver=resolver,
            registry=registry,
            buffer=buffer,
            updater=updater,
            metrics=metrics,
            cfg=cfg,
            policy_mode=policy_mode,
            policy_version=policy_version,
            config_hash=config_hash,
            orchestrator_cfg=orchestrator_cfg,
            config_index=config_index,
        )

    @classmethod
    async def load(
        cls,
        *,
        weights_path: Path,
        resolver: ParameterResolver,
        registry: PlayRegistry,
        metrics: MetricsEngine,
        cfg: RLConfig,
        policy_mode: PolicyMode = PolicyMode.LEARNING,
        policy_version: str = "ppo-v1",
        config_hash: str = "",
        orchestrator_cfg: RuntimeConfig | None = None,
        config_index: tuple[ConfigKey, ...] = (),
    ) -> PPOSelector:
        """Load weights from *weights_path* and build a PPOSelector."""
        # #247: torch.load is a synchronous, multi-second blocking call; run it
        # off the event loop so the TUI startup checklist keeps repainting
        # instead of freezing on a partial frame (the save path already threads).
        policy = await asyncio.to_thread(ActorCritic.load, Path(weights_path))
        buffer = RolloutBuffer(capacity=256)
        updater = _build_updater(policy, cfg)
        return cls(
            policy=policy,
            resolver=resolver,
            registry=registry,
            buffer=buffer,
            updater=updater,
            metrics=metrics,
            cfg=cfg,
            policy_mode=policy_mode,
            policy_version=policy_version,
            config_hash=config_hash,
            orchestrator_cfg=orchestrator_cfg,
            config_index=config_index,
        )
