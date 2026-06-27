"""Tests for Track 5: live alignment delta from beads epic closure ratios.

Covers:
- PlayOutcome.alignment_delta can be None
- PlayOutcome.failed() produces alignment_delta=0.0
- reward function: small bonus when alignment_delta is None and play_type is SEED_PROJECT
- reward function: 0 contribution when alignment_delta is None and play is not seed-related
- observation vector still has exactly OBSERVATION_DIM=278 with the new epic features
- epic closure ratio features are encoded correctly in slots 8-11
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from agentshore.beads import EpicStatus, ProjectGraph
from agentshore.config import RewardConfig
from agentshore.rl.observation import (
    _S_EPIC_GLOBAL_RATIO,
    _S_EPIC_TOP0,
    _S_EPIC_TOP1,
    _S_EPIC_TOP2,
    OBSERVATION_DIM,
    OBSERVATION_VERSION,
    ObservationContext,
    encode_observation,
)
from agentshore.rl.reward import RewardSignals, compute_reward
from agentshore.state import (
    OrchestratorState,
    PlayOutcome,
    PlayType,
    SessionState,
)


def _make_state(**kwargs: object) -> OrchestratorState:
    base: dict[str, object] = dict(
        session_id="test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
    )
    base.update(kwargs)
    return OrchestratorState(**base)  # type: ignore[arg-type]


def _make_outcome(play_type: PlayType = PlayType.ISSUE_PICKUP, **kwargs: object) -> PlayOutcome:
    base: dict[str, object] = dict(
        play_type=play_type,
        agent_id=None,
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=0,
        dollar_cost=0.0,
        artifacts=[],
        alignment_delta=0.0,
    )
    base.update(kwargs)
    return PlayOutcome(**base)  # type: ignore[arg-type]


def _null_ctx() -> ObservationContext:
    return ObservationContext(
        same_type_failure_streak=0,
        stagnation_counter=0,
        issues_closed_this_session=0,
        issues_created_this_session=0,
        last_play_types=(None, None, None, None, None),
        last_play_success=(None, None, None, None, None),
        rolling_success_rate=0.0,
        rolling_avg_cost=0.0,
        rolling_avg_duration_s=0.0,
        rolling_avg_context_loss=0.0,
        rolling_avg_rampup_ms=0.0,
        open_pr_count=0,
        prs_awaiting_review=0,
        prs_approved_unmerged=0,
        minutes_since_last_alignment_check=0.0,
        minutes_since_last_intake=0.0,
        cluster_drift=0.0,
        learning_count=0,
        learning_avg_confidence=0.0,
        learning_injection_rate=0.0,
    )


def _default_cfg(**overrides: object) -> RewardConfig:
    base = RewardConfig()
    fields = dataclasses.asdict(base)
    fields.update(overrides)
    return RewardConfig(**fields)  # type: ignore[arg-type]


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


def test_play_outcome_alignment_delta_can_be_none() -> None:
    outcome = _make_outcome(alignment_delta=None)
    assert outcome.alignment_delta is None


def test_play_outcome_alignment_delta_can_be_float() -> None:
    outcome = _make_outcome(alignment_delta=0.25)
    assert outcome.alignment_delta == pytest.approx(0.25)


def test_play_outcome_alignment_delta_can_be_zero() -> None:
    outcome = _make_outcome(alignment_delta=0.0)
    assert outcome.alignment_delta == 0.0
    assert outcome.alignment_delta is not None  # 0.0 is distinct from None


def test_play_outcome_failed_has_zero_alignment_delta() -> None:
    outcome = PlayOutcome.failed(PlayType.ISSUE_PICKUP, "some error")
    assert outcome.alignment_delta == 0.0
    assert outcome.alignment_delta is not None


def test_play_outcome_failed_fields() -> None:
    outcome = PlayOutcome.failed(PlayType.CODE_REVIEW, "fail", agent_id="a1")
    assert outcome.success is False
    assert outcome.partial is False
    assert outcome.alignment_delta == 0.0
    assert outcome.agent_id == "a1"


def test_reward_signals_alignment_delta_defaults_to_none() -> None:
    sig = RewardSignals()
    assert sig.alignment_delta is None


def test_reward_signals_alignment_delta_can_be_set() -> None:
    sig = RewardSignals(alignment_delta=0.5)
    assert sig.alignment_delta == pytest.approx(0.5)


def test_reward_none_alignment_seed_project_gets_bonus() -> None:
    sig = _signals(play_type=PlayType.SEED_PROJECT, alignment_delta=None)
    _, bd = compute_reward(sig, _default_cfg())
    # SEED_PROJECT with no beads graph earns the small 0.05 seeding bonus.
    assert bd.alignment_delta == pytest.approx(0.05, abs=1e-5)


def test_reward_none_alignment_non_seed_returns_zero() -> None:
    for play in (
        PlayType.ISSUE_PICKUP,
        PlayType.CODE_REVIEW,
        PlayType.MERGE_PR,
        PlayType.REFINE_TASK_BREAKDOWN,
    ):
        sig = _signals(play_type=play, alignment_delta=None)
        _, bd = compute_reward(sig, _default_cfg())
        assert bd.alignment_delta == pytest.approx(0.0, abs=1e-5), (
            f"Expected 0.0 for {play} with None alignment_delta, got {bd.alignment_delta}"
        )


def test_reward_none_alignment_default_signals_returns_zero() -> None:
    # Defaults: alignment_delta=None, play_type=None.
    sig = RewardSignals(success=True, avg_dollar_cost=0.1, avg_duration_seconds=60.0)
    _, bd = compute_reward(sig, _default_cfg())
    assert bd.alignment_delta == pytest.approx(0.0, abs=1e-5)


def test_reward_float_alignment_delta_uses_weight() -> None:
    sig = _signals(alignment_delta=0.4)
    _, bd = compute_reward(sig, _default_cfg(alignment_weight=1.0))
    assert bd.alignment_delta == pytest.approx(0.4, abs=1e-5)


def test_reward_float_alignment_delta_zero() -> None:
    sig = _signals(alignment_delta=0.0)
    _, bd = compute_reward(sig, _default_cfg(alignment_weight=1.0))
    assert bd.alignment_delta == pytest.approx(0.0, abs=1e-5)


def test_observation_vector_size_unchanged() -> None:
    state = _make_state()
    obs = encode_observation(state, _null_ctx())
    assert obs.shape == (OBSERVATION_DIM,)
    # OBSERVATION_DIM provenance: v0.15 Phase 5 238→245 (action space 20→22 + skip-rate);
    # desktop-8zzy 245→246 (pr_pressure_ratio slot 178); #91 246→250 (PR-author 4→8 slots).
    assert OBSERVATION_DIM == 250


def test_observation_version_bumped_to_14() -> None:
    assert OBSERVATION_VERSION == 14


def test_observation_vector_size_with_graph() -> None:
    epics = [
        EpicStatus(bead_id="e1", title="Auth", total_tasks=10, closed_tasks=5, closure_ratio=0.5),
        EpicStatus(bead_id="e2", title="API", total_tasks=8, closed_tasks=4, closure_ratio=0.5),
    ]
    graph = ProjectGraph(
        epics=epics,
        tasks_ready=3,
        tasks_total=18,
        global_closure_ratio=9 / 18,
    )
    state = _make_state(graph=graph)
    obs = encode_observation(state, _null_ctx())
    assert obs.shape == (OBSERVATION_DIM,)


def test_epic_global_ratio_slot_zero_when_no_graph() -> None:
    state = _make_state()
    obs = encode_observation(state, _null_ctx())
    assert obs[_S_EPIC_GLOBAL_RATIO] == pytest.approx(0.0, abs=1e-5)


def test_epic_slots_zero_when_no_graph() -> None:
    state = _make_state()
    obs = encode_observation(state, _null_ctx())
    assert obs[_S_EPIC_GLOBAL_RATIO] == pytest.approx(0.0)
    assert obs[_S_EPIC_TOP0] == pytest.approx(0.0)
    assert obs[_S_EPIC_TOP1] == pytest.approx(0.0)
    assert obs[_S_EPIC_TOP2] == pytest.approx(0.0)


def test_epic_global_ratio_slot_populated() -> None:
    graph = ProjectGraph(
        epics=[],
        tasks_ready=0,
        tasks_total=10,
        global_closure_ratio=0.6,
    )
    state = _make_state(graph=graph)
    obs = encode_observation(state, _null_ctx())
    assert obs[_S_EPIC_GLOBAL_RATIO] == pytest.approx(0.6, abs=1e-5)


def test_epic_top3_closure_ratios_sorted_by_total_tasks_desc() -> None:
    # Largest epic first: total_tasks desc
    epics = [
        EpicStatus(bead_id="small", title="S", total_tasks=2, closed_tasks=1, closure_ratio=0.5),
        EpicStatus(bead_id="large", title="L", total_tasks=20, closed_tasks=18, closure_ratio=0.9),
        EpicStatus(bead_id="med", title="M", total_tasks=10, closed_tasks=3, closure_ratio=0.3),
    ]
    graph = ProjectGraph(
        epics=epics,
        tasks_ready=0,
        tasks_total=32,
        global_closure_ratio=22 / 32,
    )
    state = _make_state(graph=graph)
    obs = encode_observation(state, _null_ctx())
    # Sort maps total_tasks desc → TOP0/1/2: largest 20→0.9, med 10→0.3, small 2→0.5.
    assert obs[_S_EPIC_TOP0] == pytest.approx(0.9, abs=1e-5)
    assert obs[_S_EPIC_TOP1] == pytest.approx(0.3, abs=1e-5)
    assert obs[_S_EPIC_TOP2] == pytest.approx(0.5, abs=1e-5)


def test_epic_top3_padded_when_fewer_than_3_epics() -> None:
    epics = [
        EpicStatus(bead_id="e1", title="E1", total_tasks=5, closed_tasks=3, closure_ratio=0.6),
    ]
    graph = ProjectGraph(epics=epics, tasks_ready=2, tasks_total=5, global_closure_ratio=0.6)
    state = _make_state(graph=graph)
    obs = encode_observation(state, _null_ctx())
    assert obs[_S_EPIC_TOP0] == pytest.approx(0.6, abs=1e-5)
    # Unfilled epic slots stay 0.0.
    assert obs[_S_EPIC_TOP1] == pytest.approx(0.0, abs=1e-5)
    assert obs[_S_EPIC_TOP2] == pytest.approx(0.0, abs=1e-5)


def test_epic_closure_ratio_clamped_to_0_1() -> None:
    # closure_ratio > 1.0 is a data error but must clamp.
    epics = [
        EpicStatus(bead_id="e1", title="E1", total_tasks=5, closed_tasks=5, closure_ratio=1.0),
    ]
    graph = ProjectGraph(epics=epics, tasks_ready=0, tasks_total=5, global_closure_ratio=1.0)
    state = _make_state(graph=graph)
    obs = encode_observation(state, _null_ctx())
    assert obs[_S_EPIC_GLOBAL_RATIO] == pytest.approx(1.0, abs=1e-5)
    assert obs[_S_EPIC_TOP0] == pytest.approx(1.0, abs=1e-5)


def test_epic_features_are_float32() -> None:
    graph = ProjectGraph(global_closure_ratio=0.5)
    state = _make_state(graph=graph)
    obs = encode_observation(state, _null_ctx())
    assert obs.dtype == np.float32


def test_cluster_slots_0_to_7_are_zero_in_v0_10() -> None:
    # goal_clusters removed (v0.10.0); legacy cluster slots 0-7 must be 0.0.
    state = _make_state()
    obs = encode_observation(state, _null_ctx())
    assert obs[0:8].sum() == pytest.approx(0.0, abs=1e-5)
    assert obs[_S_EPIC_GLOBAL_RATIO] == pytest.approx(0.0, abs=1e-5)


def test_dependency_slots_and_retired_cluster_slots() -> None:
    from agentshore.beads import ProjectGraph

    graph = ProjectGraph(global_closure_ratio=0.5, tasks_ready=1, tasks_blocked=1, tasks_total=2)
    state = _make_state(graph=graph)
    obs = encode_observation(state, _null_ctx())
    assert obs[0] == pytest.approx(0.5, abs=1e-5)
    assert obs[1] == pytest.approx(0.5, abs=1e-5)
    assert obs[2:8].sum() == pytest.approx(0.0, abs=1e-5)  # retired slots
    assert obs[_S_EPIC_GLOBAL_RATIO] == pytest.approx(0.5, abs=1e-5)
