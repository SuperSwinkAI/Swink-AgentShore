"""Tests for the new merge_pr_bonus, instantiate_agent success bonus, and
cluster-completion wiring added in 2026-05-07 reward shaping update.

desktop-lyfb (2026-05-20) retuned _INSTANTIATE_SUCCESS_BONUS 0.05 → 0.01.
desktop-8zzy (2026-05-20) added the pr_pressure_bonus for merge_pr /
code_review.
"""

from __future__ import annotations

import pytest

from agentshore.config import RewardConfig
from agentshore.rl.reward import (
    _INSTANTIATE_SUCCESS_BONUS,
    _PR_PRESSURE_BONUS_SCALE,
    _PR_PRESSURE_THRESHOLD,
    RewardSignals,
    compute_reward,
)
from agentshore.state import PlayType


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


def test_merge_pr_uses_dedicated_bonus():
    sig = _signals(play_type=PlayType.MERGE_PR, success=True)
    _, bd = compute_reward(sig, RewardConfig())
    assert bd.progress_play_bonus == pytest.approx(2.5)


def test_merge_pr_failure_no_bonus():
    sig = _signals(play_type=PlayType.MERGE_PR, success=False)
    _, bd = compute_reward(sig, RewardConfig())
    assert bd.progress_play_bonus == pytest.approx(0.0)


def test_issue_pickup_still_uses_generic_progress_bonus():
    """The merge bonus is targeted; issue_pickup keeps the default 0.5."""
    sig = _signals(play_type=PlayType.ISSUE_PICKUP, success=True)
    _, bd = compute_reward(sig, RewardConfig())
    assert bd.progress_play_bonus == pytest.approx(0.5)


def test_qa_still_uses_qa_success_bonus():
    """qa_success_bonus and merge_pr_bonus are independent."""
    sig = _signals(play_type=PlayType.RUN_QA, success=True)
    _, bd = compute_reward(sig, RewardConfig())
    assert bd.progress_play_bonus == pytest.approx(2.0)


def test_instantiate_agent_success_bonus_fires():
    sig = _signals(play_type=PlayType.INSTANTIATE_AGENT, success=True)
    _, bd = compute_reward(sig, RewardConfig())
    assert bd.instantiate_success_bonus == pytest.approx(_INSTANTIATE_SUCCESS_BONUS)


def test_instantiate_agent_failure_no_bonus():
    sig = _signals(play_type=PlayType.INSTANTIATE_AGENT, success=False)
    _, bd = compute_reward(sig, RewardConfig())
    assert bd.instantiate_success_bonus == pytest.approx(0.0)


def test_instantiate_bonus_in_raw_total():
    """The new bonus must be summed into raw_total / clipped_total."""
    sig = _signals(
        play_type=PlayType.INSTANTIATE_AGENT,
        success=True,
        dollar_cost=0.0,
        duration_seconds=0.0,
        issues_open_before=0,
    )
    reward, bd = compute_reward(sig, RewardConfig())
    assert bd.instantiate_success_bonus == pytest.approx(_INSTANTIATE_SUCCESS_BONUS)
    assert reward >= bd.instantiate_success_bonus - 1e-6


def test_cluster_just_completed_fires_completion_bonus():
    sig = _signals(
        play_type=PlayType.MERGE_PR,
        success=True,
        cluster_just_completed=True,
    )
    _, bd = compute_reward(sig, RewardConfig())
    assert bd.completion_bonus == pytest.approx(5.0)


def test_no_completion_bonus_without_cluster_signal():
    sig = _signals(
        play_type=PlayType.MERGE_PR,
        success=True,
        cluster_just_completed=False,
    )
    _, bd = compute_reward(sig, RewardConfig())
    assert bd.completion_bonus == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# desktop-lyfb: _INSTANTIATE_SUCCESS_BONUS retuned 0.05 → 0.01
# ---------------------------------------------------------------------------


def test_instantiate_success_bonus_pinned_at_001():
    """desktop-lyfb retuned the instantiate bonus 0.05 → 0.01 so that
    recycling (create → 5 plays → end → create) cannot farm reward above the
    noise floor of normal play rewards."""
    assert pytest.approx(0.01) == _INSTANTIATE_SUCCESS_BONUS


# ---------------------------------------------------------------------------
# desktop-8zzy: pr_pressure_bonus for merge_pr / code_review
# ---------------------------------------------------------------------------


def test_pr_pressure_bonus_zero_below_threshold():
    """At open_pr_count / max = 0.3 (below the 0.7 threshold), no bonus fires."""
    sig = _signals(
        play_type=PlayType.MERGE_PR,
        success=True,
        open_pr_count=3,
        max_open_prs=10.0,
    )
    _, bd = compute_reward(sig, RewardConfig())
    assert bd.pr_pressure_bonus == pytest.approx(0.0)


def test_pr_pressure_bonus_zero_at_threshold():
    """At open_pr_count / max = 0.7 exactly, pressure is 0 (no bonus)."""
    sig = _signals(
        play_type=PlayType.MERGE_PR,
        success=True,
        open_pr_count=7,
        max_open_prs=10.0,
    )
    _, bd = compute_reward(sig, RewardConfig())
    assert bd.pr_pressure_bonus == pytest.approx(0.0)


def test_pr_pressure_bonus_positive_above_threshold():
    """At open_pr_count / max = 0.85, merge_pr success yields pr_pressure_bonus > 0.

    Expected: pressure = 0.85 - 0.7 = 0.15; bonus = 0.15 * 0.05 = 0.0075.
    """
    sig = _signals(
        play_type=PlayType.MERGE_PR,
        success=True,
        open_pr_count=85,
        max_open_prs=100.0,
    )
    _, bd = compute_reward(sig, RewardConfig())
    assert bd.pr_pressure_bonus > 0.0
    expected = (0.85 - _PR_PRESSURE_THRESHOLD) * _PR_PRESSURE_BONUS_SCALE
    assert bd.pr_pressure_bonus == pytest.approx(expected, abs=1e-6)


def test_pr_pressure_bonus_at_full_saturation():
    """At open_pr_count >= max, pressure caps at (1 - threshold) * scale."""
    sig = _signals(
        play_type=PlayType.MERGE_PR,
        success=True,
        open_pr_count=12,  # above the cap
        max_open_prs=10.0,
    )
    _, bd = compute_reward(sig, RewardConfig())
    # ratio = 1.2; pressure = 0.5; bonus = 0.5 * 0.05 = 0.025
    expected = (1.2 - _PR_PRESSURE_THRESHOLD) * _PR_PRESSURE_BONUS_SCALE
    assert bd.pr_pressure_bonus == pytest.approx(expected, abs=1e-6)


def test_pr_pressure_bonus_fires_for_code_review():
    """code_review also gets the pressure bonus."""
    sig = _signals(
        play_type=PlayType.CODE_REVIEW,
        success=True,
        open_pr_count=9,
        max_open_prs=10.0,
    )
    _, bd = compute_reward(sig, RewardConfig())
    expected = (0.9 - _PR_PRESSURE_THRESHOLD) * _PR_PRESSURE_BONUS_SCALE
    assert bd.pr_pressure_bonus == pytest.approx(expected, abs=1e-6)


def test_pr_pressure_bonus_does_not_fire_for_other_plays():
    """Only MERGE_PR and CODE_REVIEW receive the pr_pressure_bonus."""
    sig = _signals(
        play_type=PlayType.ISSUE_PICKUP,
        success=True,
        open_pr_count=9,
        max_open_prs=10.0,
    )
    _, bd = compute_reward(sig, RewardConfig())
    assert bd.pr_pressure_bonus == pytest.approx(0.0)


def test_pr_pressure_bonus_no_bonus_on_failure():
    """A failed merge_pr does not earn the pressure bonus."""
    sig = _signals(
        play_type=PlayType.MERGE_PR,
        success=False,
        open_pr_count=9,
        max_open_prs=10.0,
    )
    _, bd = compute_reward(sig, RewardConfig())
    assert bd.pr_pressure_bonus == pytest.approx(0.0)


def test_pr_pressure_bonus_in_raw_total():
    """The pressure bonus is summed into raw_total."""
    sig = _signals(
        play_type=PlayType.MERGE_PR,
        success=True,
        dollar_cost=0.0,
        duration_seconds=0.0,
        issues_open_before=0,
        open_pr_count=9,
        max_open_prs=10.0,
    )
    reward, bd = compute_reward(sig, RewardConfig())
    assert bd.pr_pressure_bonus > 0.0
    # merge_pr_bonus (2.5) + pr_pressure_bonus + no penalties
    assert reward >= bd.pr_pressure_bonus + 2.5 - 1e-6
