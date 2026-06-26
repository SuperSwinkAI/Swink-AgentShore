"""Module-level helpers shared across the orchestrator package.

This module defines pure utility helpers, the bootstrap-phase publisher
ContextVar, the ``_step`` timing context manager, the loop-detection bucket
predicate, reward signal assembly, and cluster-completion detection.

These names are re-exported from :mod:`agentshore.core` for backwards compatibility
with tests and external callers.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING

import structlog

from agentshore.rl.action_space import ACTION_SPACE_VERSION
from agentshore.rl.observation import OBSERVATION_VERSION
from agentshore.state import PlayType

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from agentshore.config import RuntimeConfig
    from agentshore.plays.base import PlayParams
    from agentshore.rl.observation import ObservationContext
    from agentshore.rl.reward import RewardSignals
    from agentshore.rl.selector import PPOSelector
    from agentshore.state import OrchestratorState, PlayOutcome


# Package-wide logger shared by every core module. Tests patch it at the
# call-site module (whole-object) or via ``helpers._logger.<method>`` (the
# shared object's attribute, observed by every module's binding).
_logger = structlog.get_logger("agentshore.core")

MIN_COST_PER_PLAY = 0.05
MIN_DURATION_SECONDS = 1.0

# Warn-threshold multipliers at which `loop_detected` fires — geometric spacing
# on long streaks instead of one emission per increment. With warn_after=3:
# streak ∈ {3, 6, 15, 30, 60, 150, 300}, deduped by the `_last_warned_*` memo.
_LOOP_DETECTED_MULTIPLIERS: tuple[int, ...] = (1, 2, 5, 10, 20, 50, 100)


def _is_loop_bucket(streak: int, threshold: int) -> bool:
    """Return True iff *streak* is one of the geometric milestones of *threshold*."""
    return any(streak == m * threshold for m in _LOOP_DETECTED_MULTIPLIERS)


def _ppo_selector_cls() -> type[PPOSelector]:
    """Resolve the real PPOSelector class lazily so cold-start stays torch-free.

    Tests that need ``isinstance(selector, _ppo_selector_cls())`` to accept a
    stub patch this function at the call-site module (e.g.
    ``patch("agentshore.core.mixins.loop._ppo_selector_cls", ...)``).
    """
    from agentshore.rl.selector import PPOSelector

    return PPOSelector


def _str_extra(params: PlayParams, key: str) -> str | None:
    value = params.extras.get(key)
    return value if isinstance(value, str) and value else None


def _emit_weights_dir_inventory(weights_dir: Path, *, phase: str) -> None:
    """Emit a ``weights_dir_inventory`` log event describing the per-project PPO weights dir.

    Fires once at session start and once during shutdown so incidents that
    wipe ``.agentshore/weights/`` are diagnosable from the log alone.
    """
    exists = weights_dir.is_dir()
    files = list(weights_dir.glob("*.pt")) if exists else []
    _logger.info(
        "weights_dir_inventory",
        phase=phase,
        path=str(weights_dir),
        exists=exists,
        file_count=len(files),
        total_bytes=sum(f.stat().st_size for f in files),
    )


def _log_task_exception(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _logger.warning("background_task_failed", error=str(exc), exc_type=type(exc).__name__)


_bootstrap_phase_publisher: ContextVar[Callable[[str, str, float], Awaitable[None]] | None] = (
    ContextVar("agentshore_bootstrap_phase_publisher", default=None)
)
"""Per-bootstrap async callback that fires once on phase start and once on
completion. Set by ``Orchestrator.bootstrap`` to forward events to the
session's StateProvider; unset everywhere else (so ``_step`` is a pure
timing/logging no-op in tests that don't wire a provider).
"""


@asynccontextmanager
async def _step(name: str) -> AsyncIterator[None]:
    """Time a bootstrap step and log its duration at INFO level.

    If a bootstrap-phase publisher is installed in the current context
    (see ``_bootstrap_phase_publisher``), fire ``(phase, "started", 0.0)``
    before yielding and ``(phase, "completed", elapsed_ms)`` after. Publisher
    failures are swallowed — bootstrap must never fail because a dashboard
    listener went away.
    """
    publisher = _bootstrap_phase_publisher.get()
    if publisher is not None:
        with suppress(Exception):
            await publisher(name, "started", 0.0)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        rounded = round(elapsed_ms, 1)
        _logger.info("bootstrap_step", step=name, elapsed_ms=rounded)
        if publisher is not None:
            with suppress(Exception):
                await publisher(name, "completed", rounded)


def _compute_config_hash(cfg: RuntimeConfig) -> str:
    payload = {
        "obs_version": OBSERVATION_VERSION,
        "action_space_version": ACTION_SPACE_VERSION,
        "gamma": cfg.rl.gamma,
        "entropy_coef": cfg.rl.entropy_coef,
        "reward": dataclasses.asdict(cfg.rl.reward),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _build_reward_signals(
    state_before: OrchestratorState,
    outcome: PlayOutcome,
    next_state: OrchestratorState,
    ctx_after: ObservationContext,
    *,
    busy_agent_count: int | None = None,
    live_agent_count: int | None = None,
    rolling_velocity: float = 0.0,
    type_diversity_in_window: int = 1,
) -> RewardSignals:
    from agentshore.rl.reward import RewardSignals
    from agentshore.state import AgentStatus

    issues_before = len(state_before.open_issues)
    issues_after = len(next_state.open_issues)
    live_agents = [
        agent for agent in next_state.agents if agent.status in (AgentStatus.IDLE, AgentStatus.BUSY)
    ]
    if live_agent_count is None:
        live_agent_count = len(live_agents)
    if busy_agent_count is None:
        busy_agent_ids = {
            agent.agent_id for agent in next_state.agents if agent.status == AgentStatus.BUSY
        }
        busy_agent_count = len(busy_agent_ids)
        # Reward runs after the completed agent usually returned to IDLE; count it
        # busy for this just-finished window, but not if state still marks it BUSY.
        if outcome.agent_id is not None and outcome.agent_id not in busy_agent_ids:
            completed_agent_live = any(agent.agent_id == outcome.agent_id for agent in live_agents)
            if completed_agent_live:
                busy_agent_count += 1
    return RewardSignals(
        play_type=outcome.play_type,
        issues_closed_this_play=max(0, issues_before - issues_after),
        issues_created_this_play=max(0, issues_after - issues_before),
        issues_open_before=issues_before,
        alignment_delta=outcome.alignment_delta,
        success=outcome.success,
        partial=outcome.partial,
        inflation_raised=outcome.inflation_raised,
        # Only CODE_REVIEW carries the anti-confirmation invariant now (RUN_QA
        # runs on merged trunk, any can_test agent qualifies). Identity check is
        # enforced at dispatch; this signal is just for reward shaping on success.
        anti_confirmation_play=outcome.play_type == PlayType.CODE_REVIEW,
        anti_confirmation_satisfied=outcome.play_type == PlayType.CODE_REVIEW,
        dollar_cost=outcome.dollar_cost,
        duration_seconds=outcome.duration_seconds,
        avg_dollar_cost=max(MIN_COST_PER_PLAY, ctx_after.rolling_avg_cost),
        avg_duration_seconds=max(MIN_DURATION_SECONDS, ctx_after.rolling_avg_duration_s),
        stagnation_counter=ctx_after.stagnation_counter,
        same_type_failure_streak=next_state.same_type_failure_streak,
        same_type_streak=next_state.same_type_streak,
        cluster_just_completed=_cluster_just_completed(state_before, next_state),
        busy_agent_count=busy_agent_count,
        live_agent_count=live_agent_count,
        type_diversity_in_window=type_diversity_in_window,
        rolling_velocity=rolling_velocity,
        # desktop-8zzy: PR-pressure signal so MERGE_PR / CODE_REVIEW earn a
        # small bonus when the queue fills up.
        open_pr_count=ctx_after.open_pr_count,
    )


def _cluster_just_completed(state_before: OrchestratorState, next_state: OrchestratorState) -> bool:
    """True if any epic (or the global project) just crossed 1.0 closure on this play.

    Drives the ``completion_bonus`` (5.0) terminal-win signal. Fires on the
    first tick where global closure crosses 1.0, OR when any individual epic's
    closure_ratio crosses 1.0 while it wasn't complete before.
    """
    if next_state.graph is None or state_before.graph is None:
        return False
    was_complete = state_before.graph.global_closure_ratio >= 1.0
    now_complete = next_state.graph.global_closure_ratio >= 1.0
    if now_complete and not was_complete:
        return True
    before_ids = {e.bead_id for e in state_before.graph.epics if e.closure_ratio >= 1.0}
    for epic in next_state.graph.epics:
        if epic.closure_ratio >= 1.0 and epic.bead_id not in before_ids:
            return True
    return False
