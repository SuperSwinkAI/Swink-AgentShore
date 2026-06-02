"""Guarded RL experience recording + policy-update tail.

Extracted from ``CompletionProcessor.process_completion``'s "Phase 3" block so a
failure in any per-play bookkeeping or diagnostics step degrades to a skipped
record / skipped update with a structured ERROR log, instead of propagating out
of ``run_until_idle`` and killing the orchestrator loop.

Background: the original inline block built the ``ExperienceRecord`` (and called
``_mask_reason_summary``, ``encode_observation``, ``compute_reward``,
``snapshot``) as *arguments* to a ``_safe_call``-wrapped coroutine. ``_safe_call``
only guards the ``await`` — the argument expressions run first, unguarded — so any
throw there (observed: a malformed ``mask_reasons`` in the WS2 diagnostics field)
escaped to the loop and crashed the session with ``sidecar_orchestrator_run_failed``.
Here every sub-step is wrapped in its own ``try/except Exception``; the loop can
never die because a single experience row failed to encode or persist.

This is a deterministic safety backstop, not a policy director — it changes
nothing about *which* play the PPO selects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from agentshore.core.helpers import _build_reward_signals, _logger
from agentshore.data.store import ExperienceRecord
from agentshore.rl.action_space import ACTION_SPACE_VERSION
from agentshore.rl.observation import encode_observation
from agentshore.rl.reward import compute_reward

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.config import RuntimeConfig
    from agentshore.core.velocity_tracker import VelocityTracker
    from agentshore.data.store import DataStore
    from agentshore.rl.metrics import MetricsEngine
    from agentshore.rl.observation import ObservationContext
    from agentshore.rl.selector import PPOSelector, _PendingStep
    from agentshore.state import OrchestratorState, PlayOutcome


_MASK_REASON_SUMMARY_MAX_CHARS = 1000


class _RLStateHost(Protocol):
    """The slice of orchestrator state the recorder reads (and ``_step_index`` it mutates).

    ``_step_index`` stays authoritative on the orchestrator (a single source of
    truth, initialised in ``_OrchestratorBase.__init__``); the recorder bumps it
    only after a row is successfully persisted.
    """

    _session_id: str
    _policy_version: str
    _config_hash: str
    _repo_root: Path
    _step_index: int


class ExperienceRecorder:
    """Owns the fragile, crash-prone RL tail of play completion, fully guarded."""

    def __init__(
        self,
        *,
        store: DataStore,
        metrics: MetricsEngine,
        selector: PPOSelector,
        cfg: RuntimeConfig,
        host: _RLStateHost,
        velocity: VelocityTracker,
    ) -> None:
        self._store = store
        self._metrics = metrics
        self._selector = selector
        self._cfg = cfg
        self._host = host
        self._velocity = velocity

    @staticmethod
    def mask_reason_summary(state: object) -> str | None:
        """Serialize a tick's per-play mask reasons into one compact string.

        Persisted to ``rl_experience.mask_reason`` so it is possible to answer,
        post-hoc, why a play (e.g. ``merge_pr``) was not selected on a given tick.
        Format is ``play_type=reason; …`` sorted by play type, truncated to keep
        the row small. Returns ``None`` when no plays were masked.

        Fully defensive: ``mask_reasons`` is *supposed* to be a
        ``dict[PlayType, MaskReason]``, but a diagnostics helper must never be
        able to crash the loop, so a malformed value degrades to ``None`` rather
        than raising (this is the exact failure that took the session down).
        """
        reasons = getattr(state, "mask_reasons", None)
        if not reasons:
            return None
        try:
            parts = [
                f"{pt.value}={reason.text}"
                for pt, reason in sorted(reasons.items(), key=lambda kv: kv[0].value)
            ]
        except (AttributeError, TypeError):
            return None
        summary = "; ".join(parts)
        if not summary:
            return None
        return summary[:_MASK_REASON_SUMMARY_MAX_CHARS]

    async def record_and_update(
        self,
        *,
        state_before: OrchestratorState,
        next_state: OrchestratorState,
        outcome: PlayOutcome,
        pending_step: _PendingStep | None,
        done: bool,
    ) -> None:
        """Persist one experience row and run the policy/checkpoint cadence.

        Each sub-step is independently guarded — a failure logs a structured
        ERROR and continues; nothing propagates to the caller / loop.
        """
        # Step 1 — reward. Cannot proceed without it, so a failure here skips the
        # whole experience+learning for this play (logged) rather than crashing.
        try:
            ctx_after = await self._metrics.snapshot(next_state)
            reward, _ = compute_reward(
                _build_reward_signals(
                    state_before,
                    outcome,
                    next_state,
                    ctx_after,
                    rolling_velocity=self._velocity.compute_rolling_velocity(
                        next_state.total_plays
                    ),
                    type_diversity_in_window=self._velocity.recent_agent_type_diversity(),
                ),
                self._cfg.rl.reward,
                reward_clip_low=self._cfg.rl.ppo.reward_clip_low,
                reward_clip_high=self._cfg.rl.ppo.reward_clip_high,
            )
        except Exception as exc:
            _logger.error(
                "experience_reward_failed",
                session_id=self._host._session_id,
                play_id=getattr(outcome, "play_id", None),
                error=str(exc),
                exc_info=True,
            )
            return

        # Step 2 — persist the experience row (independent of policy learning).
        if outcome.play_id is not None and pending_step is not None:
            await self._persist_experience(
                play_id=outcome.play_id,
                state_before=state_before,
                next_state=next_state,
                ctx_after=ctx_after,
                pending_step=pending_step,
                reward=reward,
                done=done,
            )

        # Step 3 — feed the rollout buffer (runs even when no row was persisted).
        try:
            await self._selector.on_play_completed(
                state_before=state_before,
                next_state=next_state,
                reward=reward,
                done=done,
                pending_step=pending_step,
            )
        except Exception as exc:
            _logger.error(
                "on_play_completed_failed",
                session_id=self._host._session_id,
                error=str(exc),
                exc_info=True,
            )

        # Step 4 — policy-update cadence.
        try:
            if self._selector.should_update():
                bootstrap_v = 0.0 if done else await self._selector.value_of(next_state)
                await self._selector.update_policy(next_state_value=bootstrap_v)
        except Exception as exc:
            _logger.error(
                "policy_update_failed",
                session_id=self._host._session_id,
                error=str(exc),
                exc_info=True,
            )

        # Step 5 — checkpoint + WAL cadence.
        total_plays = next_state.total_plays
        try:
            if self._selector.should_checkpoint(total_plays):
                weights_dir = self._host._repo_root / ".agentshore" / "weights"
                await self._selector.save_checkpoint(
                    self._store, self._host._session_id, weights_dir, total_plays
                )
        except Exception as exc:
            _logger.error(
                "save_checkpoint_failed",
                session_id=self._host._session_id,
                error=str(exc),
                exc_info=True,
            )
        if total_plays % 25 == 0:
            try:
                await self._store.wal_checkpoint()
            except Exception as exc:
                _logger.error(
                    "wal_checkpoint_failed",
                    session_id=self._host._session_id,
                    error=str(exc),
                    exc_info=True,
                )

    async def _persist_experience(
        self,
        *,
        play_id: int,
        state_before: OrchestratorState,
        next_state: OrchestratorState,
        ctx_after: ObservationContext,
        pending_step: _PendingStep,
        reward: float,
        done: bool,
    ) -> None:
        try:
            ctx_before = await self._metrics.snapshot(state_before)
            obs_before = encode_observation(state_before, ctx_before)
            obs_after = encode_observation(next_state, ctx_after)
            record = ExperienceRecord(
                session_id=self._host._session_id,
                play_id=play_id,
                state_vector=obs_before.tobytes(),
                action=pending_step.action,
                reward=reward,
                next_state=obs_after.tobytes(),
                done=int(done),
                old_log_prob=pending_step.log_prob,
                value_estimate=pending_step.value,
                action_mask=pending_step.mask.tobytes(),
                mask_reason=self.mask_reason_summary(state_before),
                policy_version=self._host._policy_version,
                action_space_version=ACTION_SPACE_VERSION,
                config_hash=self._host._config_hash,
                step_index=self._host._step_index,
            )
            await self._store.record_experience(record)
            self._host._step_index += 1
        except Exception as exc:
            _logger.error(
                "experience_record_failed",
                session_id=self._host._session_id,
                play_id=play_id,
                error=str(exc),
                exc_info=True,
            )
