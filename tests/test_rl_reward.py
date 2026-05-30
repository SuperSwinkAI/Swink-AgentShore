"""Tests for rl/reward.py — component formulas, clipping, NaN guard."""

from __future__ import annotations

import math

import pytest

from agentshore.config import RewardConfig
from agentshore.rl.reward import (
    _CLEANUP_SUCCESS_BONUS,
    _DEBUG_SUCCESS_BONUS,
    RewardSignals,
    compute_reward,
)
from agentshore.state import PlayType


def _cfg(**overrides: object) -> RewardConfig:
    base = RewardConfig()
    # RewardConfig is frozen; build from scratch
    fields = {
        "alignment_weight": base.alignment_weight,
        "issue_throughput_weight": base.issue_throughput_weight,
        "cost_weight": base.cost_weight,
        "time_weight": base.time_weight,
        "completion_bonus": base.completion_bonus,
        "stagnation_penalty": base.stagnation_penalty,
        "failure_penalty": base.failure_penalty,
        "issue_inflation_penalty": base.issue_inflation_penalty,
        "anti_confirmation_bonus": base.anti_confirmation_bonus,
        "loop_penalty": base.loop_penalty,
        "progress_play_bonus": base.progress_play_bonus,
        "qa_success_bonus": base.qa_success_bonus,
        "merge_pr_bonus": base.merge_pr_bonus,
        "inflation_window_size": base.inflation_window_size,
        "inflation_window_min_plays": base.inflation_window_min_plays,
        "stagnation_threshold": base.stagnation_threshold,
        "cost_clip_ratio": base.cost_clip_ratio,
        "time_clip_ratio": base.time_clip_ratio,
    }
    fields.update(overrides)  # type: ignore[arg-type]
    return RewardConfig(**fields)  # type: ignore[arg-type]


def _default_cfg() -> RewardConfig:
    return _cfg()


def _signals(**overrides: object) -> RewardSignals:
    base = RewardSignals(
        success=True,
        avg_dollar_cost=0.10,
        avg_duration_seconds=60.0,
        dollar_cost=0.10,
        duration_seconds=60.0,
    )
    for k, v in overrides.items():
        object.__setattr__(base, k, v)
    return base


# ---------------------------------------------------------------------------
# Individual components
# ---------------------------------------------------------------------------


def test_issue_throughput_component():
    # 1 closed out of 10 open: throughput = 2.0 * (1/10) = 0.2
    sig = _signals(issues_closed_this_play=1, issues_open_before=10)
    reward, bd = compute_reward(sig, _default_cfg())
    assert bd.issue_throughput == pytest.approx(0.2, abs=1e-5)


def test_issue_throughput_zero_open_issues():
    # no open issues: no throughput signal (nothing to close)
    sig = _signals(issues_closed_this_play=1, issues_open_before=0)
    _, bd = compute_reward(sig, _default_cfg())
    assert bd.issue_throughput == pytest.approx(0.0)


def test_alignment_delta_component():
    sig = _signals(alignment_delta=0.3)
    _, bd = compute_reward(sig, _cfg(alignment_weight=1.0))
    assert bd.alignment_delta == pytest.approx(0.3, abs=1e-5)


def test_cost_penalty_at_avg():
    # cost == avg_cost → ratio 1.0 → penalty = -0.1 * 1.0 = -0.1
    sig = _signals(dollar_cost=0.10, avg_dollar_cost=0.10)
    _, bd = compute_reward(sig, _cfg(cost_weight=0.1))
    assert bd.cost_penalty == pytest.approx(-0.1, abs=1e-5)


def test_cost_penalty_clipped():
    # cost = 5× avg, cost_clip_ratio=5 → ratio capped at 5 → penalty = -0.1 * 5 = -0.5
    sig = _signals(dollar_cost=0.50, avg_dollar_cost=0.10)
    _, bd = compute_reward(sig, _cfg(cost_weight=0.1, cost_clip_ratio=5.0))
    assert bd.cost_penalty == pytest.approx(-0.5, abs=1e-5)


def test_time_penalty_at_avg():
    sig = _signals(duration_seconds=60.0, avg_duration_seconds=60.0)
    _, bd = compute_reward(sig, _cfg(time_weight=0.05))
    assert bd.time_penalty == pytest.approx(-0.05, abs=1e-5)


def test_completion_bonus_fires():
    sig = _signals(cluster_just_completed=True)
    _, bd = compute_reward(sig, _cfg(completion_bonus=5.0))
    assert bd.completion_bonus == pytest.approx(5.0, abs=1e-5)


def test_completion_bonus_absent():
    sig = _signals(cluster_just_completed=False)
    _, bd = compute_reward(sig, _default_cfg())
    assert bd.completion_bonus == pytest.approx(0.0, abs=1e-5)


def test_stagnation_penalty_fires_above_threshold():
    sig = _signals(stagnation_counter=6)
    _, bd = compute_reward(sig, _cfg(stagnation_penalty=0.5, stagnation_threshold=5))
    assert bd.stagnation_penalty == pytest.approx(-0.5, abs=1e-5)


def test_stagnation_penalty_absent_below_threshold():
    sig = _signals(stagnation_counter=4)
    _, bd = compute_reward(sig, _cfg(stagnation_penalty=0.5, stagnation_threshold=5))
    assert bd.stagnation_penalty == pytest.approx(0.0, abs=1e-5)


def test_failure_penalty_full():
    sig = _signals(success=False, partial=False)
    _, bd = compute_reward(sig, _cfg(failure_penalty=1.0))
    assert bd.failure_penalty == pytest.approx(-1.0, abs=1e-5)


def test_failure_penalty_partial():
    sig = _signals(success=False, partial=True)
    _, bd = compute_reward(sig, _cfg(failure_penalty=1.0))
    assert bd.failure_penalty == pytest.approx(-0.5, abs=1e-5)


def test_failure_penalty_absent_on_success():
    sig = _signals(success=True)
    _, bd = compute_reward(sig, _cfg(failure_penalty=1.0))
    assert bd.failure_penalty == pytest.approx(0.0, abs=1e-5)


def test_issue_inflation_penalty_fires():
    # 15 created, 5 closed in window (>2× threshold)
    sig = _signals(
        issues_created_in_window=15,
        issues_closed_in_window=5,
        window_play_count=10,
    )
    _, bd = compute_reward(sig, _cfg(issue_inflation_penalty=2.0, inflation_window_min_plays=5))
    # ratio = 15/5 = 3; penalty = -2.0 * (3-2) = -2.0
    assert bd.issue_inflation_penalty == pytest.approx(-2.0, abs=1e-5)


def test_issue_inflation_penalty_absent_below_min_plays():
    sig = _signals(
        issues_created_in_window=15,
        issues_closed_in_window=5,
        window_play_count=3,  # below min_plays=5
    )
    _, bd = compute_reward(sig, _cfg(inflation_window_min_plays=5))
    assert bd.issue_inflation_penalty == pytest.approx(0.0, abs=1e-5)


def test_issue_inflation_penalty_absent_below_threshold():
    # 8 created, 5 closed → ratio 1.6 < 2.0 → no penalty
    sig = _signals(
        issues_created_in_window=8,
        issues_closed_in_window=5,
        window_play_count=10,
    )
    _, bd = compute_reward(sig, _default_cfg())
    assert bd.issue_inflation_penalty == pytest.approx(0.0, abs=1e-5)


def test_anti_confirmation_bonus_fires_positive():
    sig = _signals(anti_confirmation_play=True, anti_confirmation_satisfied=True)
    _, bd = compute_reward(sig, _cfg(anti_confirmation_bonus=0.3))
    assert bd.anti_confirmation_bonus == pytest.approx(0.3, abs=1e-5)


def test_anti_confirmation_bonus_fires_negative():
    sig = _signals(anti_confirmation_play=True, anti_confirmation_satisfied=False)
    _, bd = compute_reward(sig, _cfg(anti_confirmation_bonus=0.3))
    assert bd.anti_confirmation_bonus == pytest.approx(-0.3, abs=1e-5)


def test_anti_confirmation_absent_for_non_review_play():
    sig = _signals(anti_confirmation_play=False)
    _, bd = compute_reward(sig, _default_cfg())
    assert bd.anti_confirmation_bonus == pytest.approx(0.0, abs=1e-5)


def test_loop_penalty_at_streak_3():
    # penalty = -1.5 * (3 - 2) = -1.5
    sig = _signals(success=False, same_type_failure_streak=3)
    _, bd = compute_reward(sig, _cfg(loop_penalty=1.5))
    assert bd.loop_penalty == pytest.approx(-1.5, abs=1e-5)


def test_loop_penalty_at_streak_5():
    # penalty = -1.5 * (5 - 2) = -4.5
    sig = _signals(success=False, same_type_failure_streak=5)
    _, bd = compute_reward(sig, _cfg(loop_penalty=1.5))
    assert bd.loop_penalty == pytest.approx(-4.5, abs=1e-5)


def test_loop_penalty_absent_below_3():
    sig = _signals(success=False, same_type_failure_streak=2)
    _, bd = compute_reward(sig, _default_cfg())
    assert bd.loop_penalty == pytest.approx(0.0, abs=1e-5)


def test_loop_penalty_any_streak_kicks_in_at_6():
    # any-outcome streak fires at 6 with half weight: -0.5 * 1.5 * (6 - 5) = -0.75
    sig = _signals(success=True, same_type_streak=6)
    _, bd = compute_reward(sig, _cfg(loop_penalty=1.5))
    assert bd.loop_penalty == pytest.approx(-0.75, abs=1e-5)


def test_loop_penalty_any_streak_absent_below_6():
    # 5 same-type successes is still tolerated.
    sig = _signals(success=True, same_type_streak=5)
    _, bd = compute_reward(sig, _default_cfg())
    assert bd.loop_penalty == pytest.approx(0.0, abs=1e-5)


def test_loop_penalty_combines_failure_and_any_streak():
    # When both conditions trip, penalties stack: -1.5 * (3-2) + -0.5 * 1.5 * (6-5)
    sig = _signals(success=False, same_type_failure_streak=3, same_type_streak=6)
    _, bd = compute_reward(sig, _cfg(loop_penalty=1.5))
    assert bd.loop_penalty == pytest.approx(-2.25, abs=1e-5)


# ---------------------------------------------------------------------------
# Clipping
# ---------------------------------------------------------------------------


def test_reward_clipped_at_high():
    # Give a huge completion bonus to push past 10
    sig = _signals(cluster_just_completed=True, issues_closed_this_play=50, issues_open_before=1)
    reward, bd = compute_reward(sig, _cfg(completion_bonus=100.0))
    assert reward == pytest.approx(10.0, abs=1e-5)
    assert bd.clipped_total == pytest.approx(10.0, abs=1e-5)
    assert bd.raw_total > 10.0


def test_reward_clipped_at_low():
    sig = _signals(success=False, same_type_failure_streak=20, stagnation_counter=20)
    reward, _ = compute_reward(sig, _cfg(loop_penalty=10.0, stagnation_penalty=10.0))
    assert reward == pytest.approx(-10.0, abs=1e-5)


# ---------------------------------------------------------------------------
# NaN / inf guard
# ---------------------------------------------------------------------------


def test_nan_reward_input_returns_zero():
    sig = _signals(alignment_delta=float("nan"))
    reward, bd = compute_reward(sig, _default_cfg())
    assert reward == 0.0
    assert bd.clipped_total == 0.0


def test_inf_reward_input_returns_zero():
    sig = _signals(alignment_delta=float("inf"))
    reward, _ = compute_reward(sig, _default_cfg())
    assert reward == 0.0


# ---------------------------------------------------------------------------
# Weight change propagates
# ---------------------------------------------------------------------------


def test_weight_change_propagates():
    sig = _signals(issues_closed_this_play=1, issues_open_before=10)
    _, bd1 = compute_reward(sig, _cfg(issue_throughput_weight=1.0))
    _, bd2 = compute_reward(sig, _cfg(issue_throughput_weight=3.0))
    assert bd2.issue_throughput == pytest.approx(3 * bd1.issue_throughput, abs=1e-5)


# ---------------------------------------------------------------------------
# Breakdown fields populated
# ---------------------------------------------------------------------------


def test_breakdown_raw_and_clipped_populated():
    sig = _signals(success=True, alignment_delta=0.1)
    reward, bd = compute_reward(sig, _default_cfg())
    assert math.isfinite(bd.raw_total)
    assert math.isfinite(bd.clipped_total)
    assert reward == bd.clipped_total


# ---------------------------------------------------------------------------
# Progress-play shaping: per-play bonus + waived cost/time penalties
# ---------------------------------------------------------------------------


def test_progress_bonus_fires_on_issue_pickup_success():
    sig = _signals(play_type=PlayType.ISSUE_PICKUP, success=True)
    _, bd = compute_reward(sig, _cfg(progress_play_bonus=0.5))
    assert bd.progress_play_bonus == pytest.approx(0.5)


def test_progress_bonus_fires_on_code_review_success():
    sig = _signals(play_type=PlayType.CODE_REVIEW, success=True)
    _, bd = compute_reward(sig, _cfg(progress_play_bonus=0.5))
    assert bd.progress_play_bonus == pytest.approx(0.5)


def test_merge_pr_uses_merge_pr_bonus_not_progress_bonus():
    """MERGE_PR is the terminal-win signal; it gets its own (larger) bonus."""
    sig = _signals(play_type=PlayType.MERGE_PR, success=True)
    _, bd = compute_reward(sig, _cfg(progress_play_bonus=0.5, merge_pr_bonus=2.5))
    assert bd.progress_play_bonus == pytest.approx(2.5)


def test_qa_bonus_uses_qa_success_bonus_not_progress_bonus():
    sig = _signals(play_type=PlayType.RUN_QA, success=True)
    _, bd = compute_reward(sig, _cfg(progress_play_bonus=0.5, qa_success_bonus=2.0))
    assert bd.progress_play_bonus == pytest.approx(2.0)


def test_systematic_debugging_earns_small_success_reward():
    """Successful debug earns _DEBUG_SUCCESS_BONUS; cost penalty is NOT waived.

    Debugging is a negative scenario — something broke and someone has to fix
    it — so SYSTEMATIC_DEBUGGING is intentionally NOT a progress play. PPO
    still needs *some* positive signal to prefer a successful debug over a
    wasted dispatch, hence the small flat bonus, but the cost penalty must
    still apply so over-selection of debug is net-negative.
    """
    sig = _signals(
        play_type=PlayType.SYSTEMATIC_DEBUGGING,
        success=True,
        dollar_cost=0.10,
        avg_dollar_cost=0.10,
    )
    _, bd = compute_reward(sig, _cfg(cost_weight=0.1, progress_play_bonus=0.5))
    # Bonus paid on success.
    assert bd.debug_success_bonus == pytest.approx(_DEBUG_SUCCESS_BONUS)
    # Progress-play bonus must NOT also fire — debug is not a progress play.
    assert bd.progress_play_bonus == pytest.approx(0.0)
    # Cost penalty still applies (cost == avg_cost → ratio 1.0 → -0.1).
    assert bd.cost_penalty == pytest.approx(-0.1, abs=1e-5)


def test_systematic_debugging_no_bonus_on_failure():
    """Failed debug → no debug_success_bonus; cost/time/failure penalties apply."""
    sig = _signals(
        play_type=PlayType.SYSTEMATIC_DEBUGGING,
        success=False,
        dollar_cost=0.10,
        avg_dollar_cost=0.10,
        duration_seconds=60.0,
        avg_duration_seconds=60.0,
    )
    _, bd = compute_reward(sig, _cfg(cost_weight=0.1, time_weight=0.05, failure_penalty=1.0))
    assert bd.debug_success_bonus == pytest.approx(0.0)
    assert bd.cost_penalty == pytest.approx(-0.1, abs=1e-5)
    assert bd.time_penalty == pytest.approx(-0.05, abs=1e-5)
    assert bd.failure_penalty == pytest.approx(-1.0, abs=1e-5)


def test_cleanup_earns_small_success_bonus():
    """Successful cleanup earns _CLEANUP_SUCCESS_BONUS; cost penalty is NOT waived.

    Cleanup is mechanical (formatter, lint --fix, typecheck, test) and not a
    progress play, so cost/time penalties stay active. desktop-lyfb raised the
    bonus 0.03 → 0.05 because cleanup is now load-bearing in the bootstrap
    recipe (desktop-arph) and mid-session unblock (desktop-hzgb). It is still
    sized at-or-below _DEBUG_SUCCESS_BONUS so PPO cannot spam cleanup as a free
    reward source.
    """
    sig = _signals(
        play_type=PlayType.CLEANUP,
        success=True,
        dollar_cost=0.10,
        avg_dollar_cost=0.10,
    )
    _, bd = compute_reward(sig, _cfg(cost_weight=0.1, progress_play_bonus=0.5))
    assert bd.cleanup_success_bonus == pytest.approx(_CLEANUP_SUCCESS_BONUS)
    assert bd.progress_play_bonus == pytest.approx(0.0)
    assert bd.cost_penalty == pytest.approx(-0.1, abs=1e-5)
    # desktop-lyfb: cleanup (0.05) now equals debug bonus (0.05); both stay
    # well below the progress-play family.
    assert pytest.approx(0.05) == _CLEANUP_SUCCESS_BONUS
    assert _CLEANUP_SUCCESS_BONUS <= _DEBUG_SUCCESS_BONUS


def test_cleanup_bonus_pinned_at_005():
    """desktop-lyfb pins _CLEANUP_SUCCESS_BONUS at 0.05 (was 0.03)."""
    assert pytest.approx(0.05) == _CLEANUP_SUCCESS_BONUS


def test_cleanup_no_bonus_on_failure():
    """Failed cleanup → no cleanup_success_bonus; cost/time/failure penalties apply."""
    sig = _signals(
        play_type=PlayType.CLEANUP,
        success=False,
        dollar_cost=0.10,
        avg_dollar_cost=0.10,
        duration_seconds=60.0,
        avg_duration_seconds=60.0,
    )
    _, bd = compute_reward(sig, _cfg(cost_weight=0.1, time_weight=0.05, failure_penalty=1.0))
    assert bd.cleanup_success_bonus == pytest.approx(0.0)
    assert bd.cost_penalty == pytest.approx(-0.1, abs=1e-5)
    assert bd.time_penalty == pytest.approx(-0.05, abs=1e-5)
    assert bd.failure_penalty == pytest.approx(-1.0, abs=1e-5)


def test_systematic_debugging_net_reward_lower_than_code_review():
    """Same cost/time inputs: debug's net reward < code_review's because debug
    keeps cost/time penalties while code_review (a progress play) waives them.
    """
    cfg = _cfg(cost_weight=0.1, time_weight=0.05, progress_play_bonus=0.5)
    common = dict(
        success=True,
        dollar_cost=0.50,
        avg_dollar_cost=0.10,
        duration_seconds=300.0,
        avg_duration_seconds=60.0,
    )
    debug_reward, _ = compute_reward(
        _signals(play_type=PlayType.SYSTEMATIC_DEBUGGING, **common), cfg
    )
    review_reward, _ = compute_reward(_signals(play_type=PlayType.CODE_REVIEW, **common), cfg)
    assert debug_reward < review_reward


def test_progress_bonus_absent_on_failure():
    sig = _signals(play_type=PlayType.ISSUE_PICKUP, success=False)
    _, bd = compute_reward(sig, _cfg(progress_play_bonus=0.5))
    assert bd.progress_play_bonus == pytest.approx(0.0)


def test_progress_bonus_absent_on_planning_play():
    sig = _signals(play_type=PlayType.END_AGENT, success=True)
    _, bd = compute_reward(sig, _cfg(progress_play_bonus=0.5))
    assert bd.progress_play_bonus == pytest.approx(0.0)


def test_cost_penalty_waived_for_progress_play():
    sig = _signals(play_type=PlayType.CODE_REVIEW, dollar_cost=0.50, avg_dollar_cost=0.10)
    _, bd = compute_reward(sig, _cfg(cost_weight=0.1))
    assert bd.cost_penalty == pytest.approx(0.0)


def test_time_penalty_waived_for_progress_play():
    sig = _signals(play_type=PlayType.MERGE_PR, duration_seconds=300.0, avg_duration_seconds=60.0)
    _, bd = compute_reward(sig, _cfg(time_weight=0.05))
    assert bd.time_penalty == pytest.approx(0.0)


def test_cost_penalty_still_applies_to_planning_play():
    sig = _signals(
        play_type=PlayType.END_AGENT,
        dollar_cost=0.10,
        avg_dollar_cost=0.10,
    )
    _, bd = compute_reward(sig, _cfg(cost_weight=0.1))
    assert bd.cost_penalty == pytest.approx(-0.1, abs=1e-5)


# ---------------------------------------------------------------------------
# Multi-agent + velocity bonuses (Group B)
# ---------------------------------------------------------------------------


def test_concurrent_agent_bonus_fires_on_dispatch_play():
    """Bonus fires when busy agents reach the utilization threshold on a dispatch play."""
    from agentshore.rl.reward import _DISPATCH_PLAYS

    dispatch_play = next(iter(_DISPATCH_PLAYS))  # any dispatch play
    cfg = RewardConfig()
    signals = RewardSignals(
        play_type=dispatch_play,
        success=True,
        busy_agent_count=3,  # 2 agents above the first
        live_agent_count=4,  # 75% utilization => 2x multiplier
        type_diversity_in_window=1,
        rolling_velocity=0.0,
    )
    _, bd = compute_reward(signals, cfg)
    assert bd.concurrent_agent_utilization == pytest.approx(0.75)
    assert bd.concurrent_agent_multiplier == pytest.approx(2.0)
    assert bd.concurrent_agent_bonus == pytest.approx(cfg.concurrent_agent_bonus * 2 * 2)


def test_concurrent_agent_bonus_zero_below_half_utilization():
    """A mostly-idle fleet earns no concurrency bonus."""
    from agentshore.rl.reward import _DISPATCH_PLAYS

    dispatch_play = next(iter(_DISPATCH_PLAYS))
    cfg = RewardConfig()
    signals = RewardSignals(
        play_type=dispatch_play,
        success=True,
        busy_agent_count=2,
        live_agent_count=5,  # 40% utilization => 0x multiplier
    )
    _, bd = compute_reward(signals, cfg)
    assert bd.concurrent_agent_utilization == pytest.approx(0.4)
    assert bd.concurrent_agent_multiplier == pytest.approx(0.0)
    assert bd.concurrent_agent_bonus == 0.0


def test_concurrent_agent_bonus_half_utilization_is_one_x():
    """50% utilization keeps the legacy per-extra-busy-agent reward scale."""
    from agentshore.rl.reward import _DISPATCH_PLAYS

    dispatch_play = next(iter(_DISPATCH_PLAYS))
    cfg = RewardConfig()
    signals = RewardSignals(
        play_type=dispatch_play,
        success=True,
        busy_agent_count=2,
        live_agent_count=4,
    )
    _, bd = compute_reward(signals, cfg)
    assert bd.concurrent_agent_utilization == pytest.approx(0.5)
    assert bd.concurrent_agent_multiplier == pytest.approx(1.0)
    assert bd.concurrent_agent_bonus == pytest.approx(cfg.concurrent_agent_bonus)


def test_concurrent_agent_bonus_full_utilization_is_four_x():
    """A fully busy fleet gets a 4x multiplier on the per-extra-agent base."""
    from agentshore.rl.reward import _DISPATCH_PLAYS

    dispatch_play = next(iter(_DISPATCH_PLAYS))
    cfg = RewardConfig()
    signals = RewardSignals(
        play_type=dispatch_play,
        success=True,
        busy_agent_count=4,
        live_agent_count=4,
    )
    _, bd = compute_reward(signals, cfg)
    assert bd.concurrent_agent_utilization == pytest.approx(1.0)
    assert bd.concurrent_agent_multiplier == pytest.approx(4.0)
    assert bd.concurrent_agent_bonus == pytest.approx(cfg.concurrent_agent_bonus * 3 * 4)


def test_concurrent_agent_bonus_zero_on_internal_play():
    """Bonus must NOT fire on internal plays (e.g., INSTANTIATE_AGENT)."""
    from agentshore.rl.reward import _DISPATCH_PLAYS

    internal_play = PlayType.INSTANTIATE_AGENT
    assert internal_play not in _DISPATCH_PLAYS
    cfg = RewardConfig()
    signals = RewardSignals(
        play_type=internal_play,
        success=True,
        busy_agent_count=5,
    )
    _, bd = compute_reward(signals, cfg)
    assert bd.concurrent_agent_bonus == 0.0


def test_type_diversity_bonus_threshold():
    """type_diversity_bonus fires when >= 2 distinct agent types in window."""
    from agentshore.rl.reward import _DISPATCH_PLAYS

    dispatch_play = next(iter(_DISPATCH_PLAYS))
    cfg = RewardConfig()

    # Two types — bonus fires
    sigs_two = RewardSignals(play_type=dispatch_play, success=True, type_diversity_in_window=2)
    _, bd_two = compute_reward(sigs_two, cfg)
    assert bd_two.type_diversity_bonus == pytest.approx(cfg.type_diversity_bonus)

    # One type — no bonus
    sigs_one = RewardSignals(play_type=dispatch_play, success=True, type_diversity_in_window=1)
    _, bd_one = compute_reward(sigs_one, cfg)
    assert bd_one.type_diversity_bonus == 0.0


def test_velocity_bonus_threshold():
    """velocity_bonus fires when rolling_velocity > velocity_bonus_threshold."""
    from agentshore.rl.reward import _DISPATCH_PLAYS

    dispatch_play = next(iter(_DISPATCH_PLAYS))
    cfg = RewardConfig()

    above = RewardSignals(
        play_type=dispatch_play,
        success=True,
        rolling_velocity=cfg.velocity_bonus_threshold + 0.01,
    )
    _, bd_above = compute_reward(above, cfg)
    assert bd_above.velocity_bonus == pytest.approx(cfg.velocity_bonus)

    below = RewardSignals(
        play_type=dispatch_play,
        success=True,
        rolling_velocity=cfg.velocity_bonus_threshold - 0.01,
    )
    _, bd_below = compute_reward(below, cfg)
    assert bd_below.velocity_bonus == 0.0


def test_bonuses_zero_on_failure():
    """Multi-agent bonuses must be zero when success=False."""
    from agentshore.rl.reward import _DISPATCH_PLAYS

    dispatch_play = next(iter(_DISPATCH_PLAYS))
    cfg = RewardConfig()
    signals = RewardSignals(
        play_type=dispatch_play,
        success=False,  # failed play
        busy_agent_count=5,
        type_diversity_in_window=3,
        rolling_velocity=0.9,
    )
    _, bd = compute_reward(signals, cfg)
    assert bd.concurrent_agent_bonus == 0.0
    assert bd.type_diversity_bonus == 0.0
    assert bd.velocity_bonus == 0.0
