"""Reward function computation — weighted sum, clipped to [-10, 10].

All components are pure functions of RewardSignals; no DB queries.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from agentshore.rl.constants import SAT_OPEN_PRS_COUNT
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.config import RewardConfig

_logger = structlog.get_logger(__name__)

# "Progress plays" — those that directly move issues toward completion.
# On these plays we waive cost/time penalties (so a long, expensive code
# review or merge isn't punished) and grant a small per-play bonus on
# success to bias the policy toward execution over planning.
_PROGRESS_PLAYS: frozenset[PlayType] = frozenset(
    {
        PlayType.ISSUE_PICKUP,
        PlayType.CODE_REVIEW,
        PlayType.MERGE_PR,
        PlayType.RUN_QA,
    }
)

# Small flat success bonus for SYSTEMATIC_DEBUGGING. Debugging is a *negative*
# scenario — something broke and someone has to fix it — so we deliberately do
# NOT promote it to a progress play (cost/time penalties still apply). Instead
# we grant a small flat reward on success so PPO has *some* learning signal
# that successful debugging beats no debugging, while still keeping the
# cost-penalty large enough that over-selecting debug is net-negative.
# Sized 10× smaller than progress_play_bonus (0.5) and 2× smaller than
# concurrent_agent_bonus (0.1) so the cost penalty dominates if debug is
# dispatched habitually.
_DEBUG_SUCCESS_BONUS: float = 0.05

# Small flat success bonus for RECONCILE_STATE. Like SYSTEMATIC_DEBUGGING, this
# is a *negative* scenario — AgentShore noticed it wedged itself and has to self-
# heal — so we do NOT promote it to a progress play. The bonus exists so PPO
# learns "successful reconcile beats no reconcile" during the tight ~40s
# selection window (streak 5→7 against the loop-detector ladder, see AgentShore
# #593 timing analysis). Cost penalties still apply; the reward floor for an
# *unsuccessful* reconcile stays negative so PPO doesn't farm the play.
_RECONCILE_STATE_SUCCESS_BONUS: float = 0.05

# Small spawn bonus: enough to learn spawning when work is queued, below normal
# play reward noise.
_INSTANTIATE_SUCCESS_BONUS: float = 0.01

# Cleanup moves project state forward but still pays cost/time penalties and
# cooldown, so the bonus cannot be farmed cheaply.
_CLEANUP_SUCCESS_BONUS: float = 0.05

# Tiebreaker for draining PR pressure through review/merge near the open-PR cap.
_PR_PRESSURE_THRESHOLD: float = 0.7
_PR_PRESSURE_BONUS_SCALE: float = 0.05
_PR_PRESSURE_PLAYS: frozenset[PlayType] = frozenset({PlayType.MERGE_PR, PlayType.CODE_REVIEW})

# "Dispatch plays" — skill-backed plays that dispatch work to an agent.
# Multi-agent bonuses (concurrent, diversity, velocity) only apply to these.
# Internal/lifecycle plays are excluded.
_DISPATCH_PLAYS: frozenset[PlayType] = frozenset(
    {
        PlayType.ISSUE_PICKUP,
        PlayType.CODE_REVIEW,
        PlayType.MERGE_PR,
        PlayType.RUN_QA,
        PlayType.WRITE_IMPLEMENTATION_PLAN,
        PlayType.UNBLOCK_PR,
        PlayType.SYSTEMATIC_DEBUGGING,
        PlayType.REFINE_TASK_BREAKDOWN,
        PlayType.CLEANUP,
        PlayType.GROOM_BACKLOG,
        PlayType.SEED_PROJECT,
        PlayType.DESIGN_AUDIT,
        PlayType.CALIBRATE_ALIGNMENT,
    }
)


@dataclass(slots=True)
class RewardSignals:
    """Raw signals collected by the Orchestrator after a play completes.

    All values are in natural units; the reward function handles weighting and
    normalization.
    """

    # Which play just completed — controls per-play shaping (progress bonus,
    # cost/time penalty waivers).
    play_type: PlayType | None = None
    # Issue throughput
    issues_closed_this_play: int = 0
    issues_created_this_play: int = 0
    issues_open_before: int = 0
    # Alignment — None means beads not yet seeded (distinct from 0.0 no-progress)
    alignment_delta: float | None = None
    # Outcome
    success: bool = False
    partial: bool = False
    inflation_raised: bool = False
    # Anti-confirmation (play-level)
    anti_confirmation_satisfied: bool = False  # reviewer != author
    anti_confirmation_play: bool = False  # True only for CODE_REVIEW
    # Cost / time
    dollar_cost: float = 0.0
    duration_seconds: float = 0.0
    avg_dollar_cost: float = 0.05  # rolling average from prior plays
    avg_duration_seconds: float = 60.0
    # Stagnation / loop
    stagnation_counter: int = 0
    same_type_failure_streak: int = 0
    same_type_streak: int = 0  # any-outcome streak; catches free-reward collapse
    # Project graph completion (legacy field name retained for compatibility)
    cluster_just_completed: bool = False
    # Sliding window for inflation detection
    issues_created_in_window: int = 0
    issues_closed_in_window: int = 0
    window_play_count: int = 0
    # Multi-agent + velocity (populated by Orchestrator; defaults preserve old behavior)
    busy_agent_count: int = 0
    live_agent_count: int = 0
    type_diversity_in_window: int = 1
    rolling_velocity: float = 0.0
    # Drain-pressure (desktop-8zzy): open_pr_count near the soft cap should
    # encourage MERGE_PR / CODE_REVIEW. ``max_open_prs`` defaults to the shared
    # ``SAT_OPEN_PRS_COUNT`` so existing callers that don't set it still get a
    # sensible ratio that matches the observation PR-pressure features.
    open_pr_count: int = 0
    max_open_prs: float = SAT_OPEN_PRS_COUNT


@dataclass(slots=True)
class RewardBreakdown:
    """Per-component reward values — logged at DEBUG for diagnosis."""

    issue_throughput: float = 0.0
    alignment_delta: float = 0.0
    cost_penalty: float = 0.0
    time_penalty: float = 0.0
    completion_bonus: float = 0.0
    stagnation_penalty: float = 0.0
    failure_penalty: float = 0.0
    issue_inflation_penalty: float = 0.0
    anti_confirmation_bonus: float = 0.0
    loop_penalty: float = 0.0
    progress_play_bonus: float = 0.0
    debug_success_bonus: float = 0.0
    reconcile_state_success_bonus: float = 0.0
    instantiate_success_bonus: float = 0.0
    cleanup_success_bonus: float = 0.0
    pr_pressure_bonus: float = 0.0
    concurrent_agent_bonus: float = 0.0
    concurrent_agent_utilization: float = 0.0
    concurrent_agent_multiplier: float = 0.0
    type_diversity_bonus: float = 0.0
    velocity_bonus: float = 0.0
    raw_total: float = 0.0
    clipped_total: float = 0.0


# Additive reward-term fields whose values sum to ``raw_total``. Deriving the
# sum from this tuple (rather than a hand-written 19-term addition) means a
# dropped term can never silently vanish from the total. Excludes the
# diagnostic-only fields: ``concurrent_agent_utilization`` /
# ``concurrent_agent_multiplier`` (inputs to ``concurrent_agent_bonus``, not
# themselves added) and the ``raw_total`` / ``clipped_total`` outputs.
_SUMMED_TERMS: tuple[str, ...] = (
    "issue_throughput",
    "alignment_delta",
    "cost_penalty",
    "time_penalty",
    "completion_bonus",
    "stagnation_penalty",
    "failure_penalty",
    "issue_inflation_penalty",
    "anti_confirmation_bonus",
    "loop_penalty",
    "progress_play_bonus",
    "debug_success_bonus",
    "reconcile_state_success_bonus",
    "instantiate_success_bonus",
    "cleanup_success_bonus",
    "pr_pressure_bonus",
    "concurrent_agent_bonus",
    "type_diversity_bonus",
    "velocity_bonus",
)


def compute_reward(
    signals: RewardSignals,
    cfg: RewardConfig,
    *,
    reward_clip_low: float = -10.0,
    reward_clip_high: float = 10.0,
) -> tuple[float, RewardBreakdown]:
    """Return (clipped_reward, breakdown).

    NaN/inf inputs produce (0.0, zeros) + an ERROR log; never raises.
    """
    bd = RewardBreakdown()

    # ---- issue throughput ----
    if signals.issues_open_before > 0:
        close_ratio = signals.issues_closed_this_play / signals.issues_open_before
    else:
        close_ratio = 0.0  # No issues to close → no throughput signal
    bd.issue_throughput = cfg.issue_throughput_weight * close_ratio

    # ---- alignment delta ----
    # None means beads not yet seeded. Grant a small flat bonus for SEED_PROJECT
    # (to encourage seeding when the graph is uninitialised); treat as 0.0 for
    # all other plays so PPO isn't penalised for legitimate work before seeding.
    seed_no_beads_bonus: float = 0.05
    if signals.alignment_delta is None:
        bd.alignment_delta = (
            seed_no_beads_bonus if signals.play_type == PlayType.SEED_PROJECT else 0.0
        )
    else:
        bd.alignment_delta = cfg.alignment_weight * signals.alignment_delta

    is_progress_play = signals.play_type in _PROGRESS_PLAYS

    # ---- cost penalty ----
    # Waived for progress plays — we don't want PPO to avoid productive work
    # because it costs money or takes time.
    if is_progress_play:
        bd.cost_penalty = 0.0
    else:
        avg_cost = signals.avg_dollar_cost if signals.avg_dollar_cost > 0.0 else 0.05
        cost_ratio = signals.dollar_cost / avg_cost
        bd.cost_penalty = -cfg.cost_weight * min(cost_ratio, cfg.cost_clip_ratio)

    # ---- time penalty ----
    if is_progress_play:
        bd.time_penalty = 0.0
    else:
        avg_dur = signals.avg_duration_seconds if signals.avg_duration_seconds > 0.0 else 60.0
        time_ratio = signals.duration_seconds / avg_dur
        bd.time_penalty = -cfg.time_weight * min(time_ratio, cfg.time_clip_ratio)

    # ---- completion bonus ----
    if signals.cluster_just_completed:
        bd.completion_bonus = cfg.completion_bonus

    # ---- stagnation penalty ----
    if signals.stagnation_counter >= cfg.stagnation_threshold:
        bd.stagnation_penalty = -cfg.stagnation_penalty

    # ---- failure penalty ----
    if not signals.success:
        bd.failure_penalty = -cfg.failure_penalty * (0.5 if signals.partial else 1.0)

    # ---- issue inflation penalty ----
    if (
        signals.window_play_count >= cfg.inflation_window_min_plays
        and signals.issues_created_in_window > 2 * signals.issues_closed_in_window
    ):
        ratio = signals.issues_created_in_window / max(signals.issues_closed_in_window, 1)
        bd.issue_inflation_penalty = -cfg.issue_inflation_penalty * (ratio - 2.0)

    # ---- anti-confirmation bonus ----
    if signals.anti_confirmation_play:
        sign = 1.0 if signals.anti_confirmation_satisfied else -1.0
        bd.anti_confirmation_bonus = sign * cfg.anti_confirmation_bonus

    # ---- progress play bonus ----
    # Small reward for issue_pickup / code_review on success;
    # larger reward for QA (gated by the standard play cooldown);
    # largest reward for merge_pr (the terminal-win signal).
    if signals.success and is_progress_play:
        if signals.play_type == PlayType.RUN_QA:
            bd.progress_play_bonus = cfg.qa_success_bonus
        elif signals.play_type == PlayType.MERGE_PR:
            bd.progress_play_bonus = cfg.merge_pr_bonus
        else:
            bd.progress_play_bonus = cfg.progress_play_bonus

    # ---- systematic debugging success bonus ----
    # Tiny flat reward when debug succeeds. Debug is intentionally NOT a
    # progress play (cost/time penalties still apply, since debug means
    # something broke), but PPO still needs some positive signal to prefer
    # successful debugs over wasted dispatches.
    if signals.success and signals.play_type == PlayType.SYSTEMATIC_DEBUGGING:
        bd.debug_success_bonus = _DEBUG_SUCCESS_BONUS

    # ---- reconcile_state success bonus ----
    # Flat reward when self-heal succeeds. Like debug, RECONCILE_STATE is a
    # *negative* scenario — AgentShore noticed it wedged itself and dispatched
    # remediation. Cost/time penalties stay active so PPO doesn't habituate
    # to picking the play, but the success bonus gives a positive learning
    # signal during the tight ~40s selection window (streak 5→7 against
    # loop-detection's force_switch / escalate ladder).
    if signals.success and signals.play_type == PlayType.RECONCILE_STATE:
        bd.reconcile_state_success_bonus = _RECONCILE_STATE_SUCCESS_BONUS

    # ---- instantiate_agent success bonus ----
    # Faint flat reward (0.01) for spawning an agent successfully. Cost/time
    # penalties still apply (instantiate is not a progress play and not a
    # dispatch play). desktop-lyfb tuned this 0.05 → 0.01 so the new 5-play
    # end_agent floor cannot be farmed by recycling — a 0.01 / 6-play cycle
    # ≈ +0.0017/play sits well below the noise floor of normal play rewards.
    # Now the net signal is mildly positive — enough to learn "spawn when work
    # is queueing" without encouraging fleet inflation, while cleanup (0.05)
    # outranks it as a value signal.
    if signals.success and signals.play_type == PlayType.INSTANTIATE_AGENT:
        bd.instantiate_success_bonus = _INSTANTIATE_SUCCESS_BONUS

    # ---- cleanup success bonus ----
    if signals.success and signals.play_type == PlayType.CLEANUP:
        bd.cleanup_success_bonus = _CLEANUP_SUCCESS_BONUS

    # ---- pr_pressure_bonus (desktop-8zzy) ----
    # Reward MERGE_PR / CODE_REVIEW more when the open PR queue is filling up.
    # pressure = max(0, open_pr_count / max_open_prs - threshold); bonus =
    # pressure * scale. At open_pr_count / max == threshold (default 0.7) the
    # bonus is 0; at full saturation (>=1.0) it caps at
    # (1 - threshold) * scale = 0.015 by default. Symmetric with the
    # cleanup/instantiate machinery — see _PR_PRESSURE_* constants above.
    if signals.success and signals.play_type in _PR_PRESSURE_PLAYS:
        max_prs = signals.max_open_prs if signals.max_open_prs > 0.0 else SAT_OPEN_PRS_COUNT
        ratio = signals.open_pr_count / max_prs
        pressure = max(0.0, ratio - _PR_PRESSURE_THRESHOLD)
        bd.pr_pressure_bonus = pressure * _PR_PRESSURE_BONUS_SCALE

    # ---- multi-agent + velocity bonuses (dispatch plays only) ----
    if signals.play_type in _DISPATCH_PLAYS and signals.success:
        live_agents = max(0, signals.live_agent_count)
        busy_agents = min(max(0, signals.busy_agent_count), live_agents)
        if live_agents > 0:
            bd.concurrent_agent_utilization = busy_agents / live_agents

        if bd.concurrent_agent_utilization >= 1.0:
            bd.concurrent_agent_multiplier = 4.0
        elif bd.concurrent_agent_utilization >= 0.75:
            bd.concurrent_agent_multiplier = 2.0
        elif bd.concurrent_agent_utilization >= 0.50:
            bd.concurrent_agent_multiplier = 1.0

        bd.concurrent_agent_bonus = (
            cfg.concurrent_agent_bonus * max(0, busy_agents - 1) * bd.concurrent_agent_multiplier
        )
        bd.type_diversity_bonus = (
            cfg.type_diversity_bonus if signals.type_diversity_in_window >= 2 else 0.0
        )
        bd.velocity_bonus = (
            cfg.velocity_bonus if signals.rolling_velocity > cfg.velocity_bonus_threshold else 0.0
        )

    # ---- loop penalty ----
    # Failure streaks penalize from streak >= 3 (each extra failure compounds).
    # Any-outcome streaks penalize from streak >= 6 with half-weight, catching
    # PPO collapse onto free-reward plays where the failure streak stays at 0.
    bd.loop_penalty = 0.0
    fail_streak = signals.same_type_failure_streak
    if fail_streak >= 3:
        bd.loop_penalty += -cfg.loop_penalty * (fail_streak - 2)
    any_streak = signals.same_type_streak
    if any_streak >= 6:
        bd.loop_penalty += -0.5 * cfg.loop_penalty * (any_streak - 5)

    raw = float(sum(getattr(bd, field) for field in _SUMMED_TERMS))

    if not math.isfinite(raw):
        _logger.error("reward_non_finite", raw=raw)
        bd.clipped_total = 0.0
        bd.raw_total = 0.0
        return 0.0, bd

    bd.raw_total = raw
    clipped = max(reward_clip_low, min(reward_clip_high, raw))
    bd.clipped_total = clipped

    # Log every breakdown field by name via ``asdict`` (so a new field is logged
    # automatically), minus the two output fields that the call surfaces under
    # the legacy ``raw`` / ``clipped`` keys instead of ``raw_total`` /
    # ``clipped_total``.
    fields = dataclasses.asdict(bd)
    del fields["raw_total"]
    del fields["clipped_total"]
    _logger.debug("reward_breakdown", **fields, raw=raw, clipped=clipped)

    return clipped, bd
