"""PPOSelector — RL-driven play selector implementing PlaySelector Protocol.

Wraps ActorCritic + RolloutBuffer + PPOUpdater + MetricsEngine into the
PlaySelector interface the Orchestrator expects.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import structlog
import torch

from agentshore.beads import load_graph
from agentshore.config.models import PolicyMode
from agentshore.paths import GLOBAL_WEIGHTS_DIR as _GLOBAL_WEIGHTS_DIR
from agentshore.plays.base import PlayParams
from agentshore.plays.candidates import PlayCandidatePlan, build_candidate_plan
from agentshore.rl.action_space import INDEX_TO_PLAY, NUM_ACTIONS, POLICY_VERSION, V1_ACTION_ORDER
from agentshore.rl.cold_start import apply_cold_start_bias, apply_cold_start_config_bias
from agentshore.rl.eligibility import EligibilityAuthority
from agentshore.rl.experience import RolloutBuffer, Step
from agentshore.rl.mask import (
    TerminalNoWorkDecision,
    compute_action_mask,
    compute_config_mask,
    compute_mask_reasons,
    compute_reverse_failsafe_mask,
    compute_terminal_no_work_config_mask,
    compute_terminal_no_work_decision,
    reverse_failsafe_should_unmask,
)
from agentshore.rl.mask_reason import MaskReason
from agentshore.rl.observation import encode_observation
from agentshore.rl.policy import ActorCritic
from agentshore.rl.training import PPOUpdater, UpdateStats
from agentshore.state import AgentStatus, PlayType, SessionState

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import BinaryIO

    import numpy as np
    from numpy.typing import NDArray

    from agentshore.beads import ProjectGraph
    from agentshore.config import RLConfig
    from agentshore.config.models import RuntimeConfig
    from agentshore.data.store import DataStore
    from agentshore.plays.registry import PlayRegistry
    from agentshore.plays.resolver import ParameterResolver
    from agentshore.rl.action_space import ConfigKey
    from agentshore.rl.eligibility import LiveGraphLoader
    from agentshore.rl.metrics import MetricsEngine
    from agentshore.state import OrchestratorState


_logger = structlog.get_logger(__name__)


class _WindowsLockingModule(Protocol):
    LK_LOCK: int
    LK_UNLCK: int

    def locking(self, fd: int, mode: int, nbytes: int, /) -> int: ...


_LOCAL_CHECKPOINT_KEEP = 2
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


def _prune_local_checkpoints(weights_dir: Path, keep: int = _LOCAL_CHECKPOINT_KEEP) -> None:
    """Delete numbered local checkpoints beyond the most recent `keep` files."""
    numbered = sorted(weights_dir.glob("policy_[0-9][0-9][0-9][0-9][0-9][0-9].pt"))
    for stale in numbered[:-keep]:
        with contextlib.suppress(OSError):
            stale.unlink()


def _archive_old_canonicals(weights_dir: Path) -> None:
    """Rename policy_v{N}.pt files where N != current POLICY_VERSION to policy_legacy_v{N}.pt.

    Sibling to cleanup_stale_canonical_weights (which handles the legacy unnamed policy.pt).
    Never deletes — renames so the user can inspect.
    """
    current = weights_dir / f"policy_v{POLICY_VERSION}.pt"
    for f in sorted(weights_dir.glob("policy_v*.pt")):
        if f == current or f.name.startswith("policy_legacy_v"):
            continue
        stem = f.stem  # e.g. "policy_v2"
        if stem.startswith("policy_v") and stem[len("policy_v") :].isdigit():
            dest = weights_dir / f"policy_legacy_{stem[len('policy_') :]}.pt"
            with contextlib.suppress(OSError):
                f.rename(dest)


def cleanup_stale_canonical_weights(weights_dir: Path) -> None:
    """Rename policy.pt to policy_legacy_v{N}.pt if it's version-incompatible.

    Called at session start. Never deletes — just renames so the user can inspect.
    """
    legacy = weights_dir / "policy.pt"
    if not legacy.exists():
        return
    try:
        from agentshore.rl.policy import IncompatibleCheckpointError

        ActorCritic.load(legacy)
        # Compatible — leave it alone.
    except IncompatibleCheckpointError:
        try:
            payload = torch.load(legacy, map_location="cpu", weights_only=True)
            saved_ver = (
                payload.get("policy_version", "unknown") if isinstance(payload, dict) else "unknown"
            )
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            _logger.warning("legacy_checkpoint_load_failed", path=str(legacy), error=str(exc))
            saved_ver = "unknown"
        dest = weights_dir / f"policy_legacy_v{saved_ver}.pt"
        legacy.rename(dest)
        _logger.warning(
            "stale_canonical_checkpoint_renamed",
            from_path=str(legacy),
            to_path=str(dest),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _logger.warning("cleanup_stale_canonical_failed", error=str(exc))


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock for ``path`` on POSIX and Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        _lock_file(lock_file)
        try:
            yield
        finally:
            _unlock_file(lock_file)


def _lock_file(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        _prepare_windows_lock_byte(lock_file)
        win_lock = cast("_WindowsLockingModule", msvcrt)
        win_lock.locking(lock_file.fileno(), win_lock.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file, fcntl.LOCK_EX)


def _unlock_file(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        win_lock = cast("_WindowsLockingModule", msvcrt)
        win_lock.locking(lock_file.fileno(), win_lock.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file, fcntl.LOCK_UN)


def _prepare_windows_lock_byte(lock_file: BinaryIO) -> None:
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)


def _only_capacity_waiting(reason_counts: list[dict[str, object]]) -> bool:
    """Return True when all reported blockers are staffing/capacity waits."""
    if not reason_counts:
        return False
    capacity_markers = (
        "No IDLE",
        "Idle agent",
        "allowed tier",
        "No eligible agent configuration",
        "instantiate_agent cooldown",
        "max_per_config",
    )
    actionable_ignores = {"Reserved action slot"}
    saw_capacity = False
    for item in reason_counts:
        reason = str(item.get("reason", ""))
        if reason in actionable_ignores:
            continue
        if any(marker in reason for marker in capacity_markers):
            saw_capacity = True
            continue
        return False
    return saw_capacity


@dataclass(slots=True)
class _PendingStep:
    """State captured at select() time; consumed by on_play_completed()."""

    obs: NDArray[np.float32]
    action: int
    log_prob: float
    value: float
    mask: NDArray[np.bool_]
    # Config-head state, only set when the selected play was INSTANTIATE_AGENT.
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
        # Confirm-repick telemetry. Counts the confirm-rejection clean re-picks
        # that occurred during the most recent ``select()`` call. The orchestrator
        # drains this via ``consume_repick_count()`` once per selection cycle and
        # folds it into the rolling divergence window that feeds observation slot
        # ``executor_skip_rate_recent_50``. Reset at the top of every ``select()``.
        self._last_select_repick_count: int = 0
        # Snapshot of global weights at last reload — used to compute the
        # gradient delta for concurrent-safe global canonical updates.
        self._reload_base: dict[str, torch.Tensor] | None = None

    # ------------------------------------------------------------------
    # PlaySelector protocol
    # ------------------------------------------------------------------

    async def select(self, state: OrchestratorState) -> tuple[PlayType, PlayParams] | None:
        # Fresh confirm-repick tally for this selection cycle (drained by the
        # orchestrator into the executor_skip_rate_recent_50 window).
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
        auto_reverse_failsafe = self._auto_reverse_failsafe_should_unmask(state, mask)
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

        # Single source of truth for play validity. Built from the snapshot-only
        # candidate plan we already computed above; confirm() does the one live
        # read (a fresh beads-graph reload) against the *resolved* target after
        # the resolver has picked and claimed it.
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
        # Every non-returning iteration masks its sampled action (resolve-None,
        # confirm-reject, failsafe-retry all set remaining_mask[action]=False), so
        # the loop is self-bounded by the number of valid actions. Cap at
        # NUM_ACTIONS so a run of clean re-picks keeps trying the remaining valid
        # plays instead of idling early — it can never livelock.
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
        project_path = getattr(self._resolver, "project_path", None)
        if project_path is None:
            return None

        async def _load() -> ProjectGraph | None:
            return await load_graph(project_path)

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
        classification = (
            reason.classification.value if isinstance(reason, MaskReason) else None
        )
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
        ``all_agents_idle`` still gates premature firing during healthy
        short-lived all-masked windows where some agent is doing real work.
        """

        threshold = getattr(self._cfg, "reverse_failsafe_after_idle_ticks", 0)
        if not isinstance(threshold, int) or threshold <= 0:
            self._no_available_play_ticks = 0
            return False
        active_agents = [
            agent
            for agent in state.agents
            if agent.status in (AgentStatus.IDLE, AgentStatus.BUSY, AgentStatus.ERROR)
        ]
        all_agents_idle = bool(active_agents) and all(
            agent.status == AgentStatus.IDLE for agent in active_agents
        )
        if mask.any() or not all_agents_idle:
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

    def _log_all_masked(self, state: OrchestratorState) -> None:
        """Emit an actionable all-masked diagnostic with severity based on capacity."""
        idle_count = sum(1 for a in state.agents if a.status == AgentStatus.IDLE)
        busy_count = sum(1 for a in state.agents if a.status == AgentStatus.BUSY)
        error_count = sum(1 for a in state.agents if a.status == AgentStatus.ERROR)
        terminated_count = sum(1 for a in state.agents if a.status == AgentStatus.TERMINATED)
        candidate_plan = build_candidate_plan(state)
        terminal_no_work = compute_terminal_no_work_decision(
            state,
            self._registry,
            cfg=self._orchestrator_cfg,
            config_index=self._config_index or None,
            candidate_plan=candidate_plan,
        )
        work = (
            terminal_no_work.availability
            if terminal_no_work is not None
            else candidate_plan.work_availability
        )

        reasons = compute_mask_reasons(
            state,
            self._registry,
            cfg=self._orchestrator_cfg,
            config_index=self._config_index or None,
            candidate_plan=candidate_plan,
        )
        reason_counts = [
            {"reason": reason, "count": count}
            for reason, count in Counter(reasons.values()).most_common(5)
        ]

        spawnable_config_count: int | None = None
        if self._orchestrator_cfg is not None and self._config_index:
            spawnable_config_count = int(
                compute_config_mask(state, self._orchestrator_cfg, self._config_index).sum()
            )

        log = (
            _logger.debug
            if _only_capacity_waiting(reason_counts) or (idle_count == 0 and busy_count > 0)
            else _logger.warning
        )
        log(
            "ppo_selector.all_masked",
            idle_agents=idle_count,
            busy_agents=busy_count,
            error_agents=error_count,
            terminated_agents=terminated_count,
            tracked_issues=work.tracked_issue_count,
            github_open_issues=work.github_open_issue_count,
            workable_issues=work.workable_issue_count,
            implementation_eligible=work.implementation_eligible_count,
            bead_in_progress_issues=work.bead_in_progress_issue_count,
            ready_tasks=work.ready_task_count,
            backlog_sync_work=work.backlog_sync_work_count,
            beads_blocks_issue_pickup=work.beads_blocks_issue_pickup,
            actionable_pr_work=work.actionable_pr_work_count,
            open_issues=len(state.open_issues),
            open_prs=len(state.pull_requests),
            spawnable_config_count=spawnable_config_count,
            top_mask_reasons=reason_counts,
        )

    def _log_terminal_no_work(
        self,
        state: OrchestratorState,
        decision: TerminalNoWorkDecision,
    ) -> None:
        """Emit a structured terminal no-work mask diagnostic."""

        availability = decision.availability
        _logger.info(
            "ppo_selector.terminal_no_work",
            terminal_reason="no_workable_work_remaining",
            terminal_mask_mode=decision.mode,
            tracked_issues=availability.tracked_issue_count,
            github_open_issues=availability.github_open_issue_count,
            workable_issues=availability.workable_issue_count,
            implementation_eligible=availability.implementation_eligible_count,
            bead_in_progress_issues=availability.bead_in_progress_issue_count,
            ready_tasks=availability.ready_task_count,
            backlog_sync_work=availability.backlog_sync_work_count,
            actionable_pr_work=availability.actionable_pr_work_count,
            qa_plays_since_last=decision.qa_plays_since_last,
            in_flight_plays=len(state.in_flight_plays),
        )

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

        candidate_plan = candidate_plan or build_candidate_plan(state)
        work = candidate_plan.work_availability
        reasons = compute_mask_reasons(
            state,
            self._registry,
            cfg=self._orchestrator_cfg,
            config_index=self._config_index or None,
            apply_reverse_failsafe=False,
            candidate_plan=candidate_plan,
        )
        reason_counts = [
            {"reason": reason, "count": count}
            for reason, count in Counter(reasons.values()).most_common(5)
        ]
        allowed_plays = [pt.value for i, pt in enumerate(V1_ACTION_ORDER) if bool(mask[i])]
        # During drain the only legal action is end_agent; if no agent qualifies
        # (e.g. one is still finishing its last play) the resolver harmlessly
        # cycles every selector tick. Emit at info-level so the drain doesn't
        # produce a warning storm.
        is_draining = state.session_state in (
            SessionState.DRAINING,
            SessionState.SHUTTING_DOWN,
        )
        log_fn = _logger.info if is_draining else _logger.warning
        log_fn(
            "ppo_selector.resolver_exhausted",
            reason=reason,
            attempt=attempt,
            attempted_plays=attempted_plays,
            mask_allowed_plays=allowed_plays,
            tracked_issues=work.tracked_issue_count,
            github_open_issues=work.github_open_issue_count,
            workable_issues=work.workable_issue_count,
            implementation_eligible=work.implementation_eligible_count,
            bead_in_progress_issues=work.bead_in_progress_issue_count,
            ready_tasks=work.ready_task_count,
            backlog_sync_work=work.backlog_sync_work_count,
            actionable_pr_work=work.actionable_pr_work_count,
            top_mask_reasons=reason_counts,
        )

    def _log_reverse_failsafe(
        self,
        state: OrchestratorState,
        mask: NDArray[np.bool_],
        *,
        auto_enabled: bool = False,
    ) -> None:
        """Emit diagnostics when the all-masked reverse failsafe opens fallback actions."""
        reasons = compute_mask_reasons(
            state,
            self._registry,
            cfg=self._orchestrator_cfg,
            config_index=self._config_index or None,
            apply_reverse_failsafe=False,
            candidate_plan=build_candidate_plan(state),
        )
        reason_counts = [
            {"reason": reason, "count": count}
            for reason, count in Counter(reasons.values()).most_common(5)
        ]
        unmasked = [pt.value for i, pt in enumerate(V1_ACTION_ORDER) if mask[i]]
        _logger.warning(
            "ppo_selector.reverse_failsafe_unmasked",
            idle_agents=sum(1 for a in state.agents if a.status == AgentStatus.IDLE),
            busy_agents=sum(1 for a in state.agents if a.status == AgentStatus.BUSY),
            error_agents=sum(1 for a in state.agents if a.status == AgentStatus.ERROR),
            open_issues=len(state.open_issues),
            open_prs=len(state.pull_requests),
            top_mask_reasons=reason_counts,
            unmasked_plays=unmasked,
            auto_enabled=auto_enabled,
            no_available_play_ticks=self._no_available_play_ticks,
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
        """Load the latest shared policy from disk, with version safety."""
        from agentshore.rl.policy import ActorCritic, IncompatibleCheckpointError

        shared = _GLOBAL_WEIGHTS_DIR / f"policy_v{POLICY_VERSION}.pt"
        if not shared.exists():
            return
        try:
            new_policy = ActorCritic.load(shared)
        except IncompatibleCheckpointError as exc:
            _logger.warning(
                "ppo_selector.checkpoint_incompatible", path=str(shared), error=str(exc)
            )
            return
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            _logger.warning("ppo_selector.reload_failed", path=str(shared), error=str(exc))
            return
        if new_policy.num_configs != self._policy.num_configs:
            _logger.warning(
                "ppo_selector.config_index_changed",
                saved=new_policy.num_configs,
                current=self._policy.num_configs,
            )
            return
        new_sd = new_policy.state_dict()
        self._reload_base = {k: v.clone() for k, v in new_sd.items()}
        self._policy.load_state_dict(new_sd)
        _logger.debug("ppo_selector.weights_reloaded", path=str(shared))

    async def value_of(self, state: OrchestratorState) -> float:
        """Bootstrap value estimate for GAE truncation."""
        ctx = await self._metrics.snapshot(state)
        obs = encode_observation(state, ctx, config_index=self._config_index)
        return self._policy.value(torch.tensor(obs, dtype=torch.float32))

    def _write_global_canonical_blocking(self, canonical: Path, lock_path: Path) -> None:
        """Apply this session's gradient delta to the global canonical under a lock.

        Three sessions writing simultaneously each read the current global,
        add their own delta, and write back.  The exclusive flock serialises the
        read-modify-write so no session's update is lost.

        This method performs only synchronous I/O and must be called via
        ``asyncio.to_thread`` from the async ``save_checkpoint`` path.
        """
        import tempfile

        from agentshore.rl.policy import ActorCritic, IncompatibleCheckpointError

        current_sd = self._policy.state_dict()

        if self._reload_base is not None:
            # Compute what this PPO update actually changed.
            try:
                delta = {k: current_sd[k] - self._reload_base[k] for k in self._reload_base}
            except (KeyError, RuntimeError):
                # Shape mismatch — architecture changed mid-session; full write.
                delta = None
        else:
            delta = None  # No base snapshot; write full weights.

        with _exclusive_file_lock(lock_path):
            if delta is not None and canonical.exists():
                try:
                    base = ActorCritic.load(canonical)
                    if base.num_configs == self._policy.num_configs:
                        merged_sd = {k: base.state_dict()[k] + delta[k] for k in delta}
                        base.load_state_dict(merged_sd)
                        to_save = base
                    else:
                        to_save = self._policy  # config index mismatch; full write
                except (IncompatibleCheckpointError, KeyError, RuntimeError):
                    to_save = self._policy  # incompatible global; full write
            else:
                to_save = self._policy  # no delta or no existing global; full write

            canonical.parent.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(dir=canonical.parent, suffix=".pt.tmp")
            try:
                os.close(tmp_fd)
                to_save.save(Path(tmp_path))
                os.replace(tmp_path, canonical)
            except (OSError, RuntimeError):
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()
                raise

        _logger.debug(
            "ppo_selector.global_canonical_updated",
            path=str(canonical),
            mode="delta" if (delta is not None and canonical.exists()) else "full",
        )

    async def save_checkpoint(
        self,
        store: DataStore,
        session_id: str,
        weights_dir: Path,
        total_plays: int,
    ) -> None:
        """Write a numbered local checkpoint and update the global canonical.

        Local numbered checkpoints provide crash recovery; only the last
        _LOCAL_CHECKPOINT_KEEP are kept. The canonical policy_v{N}.pt is
        written to the global ~/.config/swink/agentshore/weights/ directory so all projects
        contribute to and benefit from a shared policy.
        """
        from datetime import UTC, datetime

        from agentshore.data.store import CheckpointRecord

        weights_dir = Path(weights_dir)
        weights_dir.mkdir(parents=True, exist_ok=True)

        # Numbered local checkpoint for crash recovery.
        weights_path = weights_dir / f"policy_{total_plays:06d}.pt"
        self._policy.save(weights_path)

        # Update the global canonical with delta accumulation so concurrent
        # sessions from different projects don't overwrite each other's learning.
        # Each session computes delta = (post-update weights) - (pre-update base),
        # then under an exclusive file lock reads the current global, adds the
        # delta, and writes back atomically.  If no reload base exists (first
        # update of this session, or reload was skipped), fall back to a full
        # overwrite — equivalent to the old behaviour and still correct since
        # delta == full weights when base == zero.
        _GLOBAL_WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
        global_canonical = _GLOBAL_WEIGHTS_DIR / f"policy_v{POLICY_VERSION}.pt"
        lock_path = _GLOBAL_WEIGHTS_DIR / f"policy_v{POLICY_VERSION}.lock"
        try:
            await asyncio.to_thread(
                self._write_global_canonical_blocking,
                global_canonical,
                lock_path,
            )
        except OSError as exc:
            _logger.warning(
                "ppo_selector.global_canonical_update_failed",
                path=str(global_canonical),
                error=str(exc),
            )

        # Keep local dir lean — crash recovery only needs the last few.
        _prune_local_checkpoints(weights_dir)

        record = CheckpointRecord(
            session_id=session_id,
            created_at=datetime.now(UTC).isoformat(),
            play_count=total_plays,
            weights_path=str(weights_path),
        )
        await store.save_checkpoint(record)
        _logger.info(
            "ppo_selector.checkpoint_saved",
            path=str(weights_path),
            global_canonical=str(global_canonical),
            total_plays=total_plays,
            delta_merge=self._reload_base is not None,
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
        updater = PPOUpdater(
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
        policy = ActorCritic.load(Path(weights_path))
        buffer = RolloutBuffer(capacity=256)
        updater = PPOUpdater(
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
