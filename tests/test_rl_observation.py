"""Tests for rl/observation.py — encode_observation shape, determinism, slot correctness."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from agentshore.rl.action_space import NUM_ACTIONS, PLAY_TO_INDEX
from agentshore.rl.observation import (
    _S_ACTIVE_AGENTS,
    _S_AGENTS_IN_ERROR,
    _S_AVG_CONTEXT_LOSS,
    _S_BLOCKED_TASK_RATIO,
    _S_BUDGET_REMAINING,
    _S_BUDGET_SPENT,
    _S_BUDGET_SUFFICIENCY,
    _S_CLUSTER_DRIFT,
    _S_EPIC_GLOBAL_RATIO,
    _S_EPIC_TOP0,
    _S_EST_COST,
    _S_EST_PLAYS,
    _S_HIST_SUCCESS_START,
    _S_HIST_TYPE_START,
    _S_ISSUE_CLOSED,
    _S_ISSUE_CREATED,
    _S_ISSUE_OPEN,
    _S_LOOP_LEVEL,
    _S_OBS_VERSION,
    _S_OPEN_PRS,
    _S_PROJ_ALIGNMENT,
    _S_READY_TASK_RATIO,
    _S_ROLLING_SUCCESS,
    _S_SINCE_ALIGNMENT,
    _S_STAGNATION,
    _S_STREAK,
    _S_TOTAL_PLAYS,
    _S_VALID_HIST,
    OBSERVATION_DIM,
    OBSERVATION_VERSION,
    ObservationContext,
    encode_observation,
)
from agentshore.state import (
    AgentSnapshot,
    AgentStatus,
    AgentType,
    BudgetSnapshot,
    IssueSnapshot,
    OrchestratorState,
    PlayType,
    SessionState,
    TrajectorySnapshot,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NULL_CTX = ObservationContext(
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


def _ctx(**overrides: object) -> ObservationContext:
    return dataclasses.replace(_NULL_CTX, **overrides)  # type: ignore[arg-type]


def _state(**kwargs: object) -> OrchestratorState:
    base: dict[str, object] = dict(
        session_id="s",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
    )
    base.update(kwargs)
    return OrchestratorState(**base)  # type: ignore[arg-type]


def _agent(agent_id: str, status: AgentStatus = AgentStatus.IDLE) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CLAUDE_CODE,
        status=status,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )


def _budget(
    remaining: float = 8.0,
    spent: float = 2.0,
    avg_cost: float = 0.05,
) -> BudgetSnapshot:
    return BudgetSnapshot(
        total_budget=10.0,
        spent=spent,
        remaining=remaining,
        estimated_cost_per_play=avg_cost,
    )


# ---------------------------------------------------------------------------
# Shape and dtype
# ---------------------------------------------------------------------------


def test_observation_dim_is_246():
    # v0.15 Phase 5: action space grew 20 → 22 (spec block 60 → 66) and
    # added a new executor_skip_rate slot at index 177. 238 → 245.
    # desktop-8zzy: added pr_pressure_ratio at slot 178; spec block slid
    # one slot down. 245 → 246.
    assert OBSERVATION_DIM == 246


def test_encode_returns_correct_shape():
    obs = encode_observation(_state(), _NULL_CTX)
    assert obs.shape == (OBSERVATION_DIM,)


def test_encode_dtype_is_float32():
    obs = encode_observation(_state(), _NULL_CTX)
    assert obs.dtype == np.float32


def test_all_slots_in_range():
    obs = encode_observation(_state(), _NULL_CTX)
    assert obs.min() >= 0.0
    assert obs.max() <= 1.0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_encode_deterministic_same_input():
    s = _state()
    a = encode_observation(s, _NULL_CTX)
    b = encode_observation(s, _NULL_CTX)
    np.testing.assert_array_equal(a, b)


def test_encode_identical_bytes_same_input():
    s = _state()
    a = encode_observation(s, _NULL_CTX).tobytes()
    b = encode_observation(s, _NULL_CTX).tobytes()
    assert a == b


# ---------------------------------------------------------------------------
# Cluster group (slots 0-11)
# goal_clusters removed in v0.10.0; slots 0-7 are always 0.0.
# Epic slots 8-11 are populated from state.graph (beads ProjectGraph).
# ---------------------------------------------------------------------------


def test_dependency_and_retired_slots_zero_without_graph():
    # Without a graph, dependency slots 0-1 and retired slots 2-7 are 0.0.
    obs = encode_observation(_state(), _NULL_CTX)
    assert obs[_S_BLOCKED_TASK_RATIO] == pytest.approx(0.0)
    assert obs[_S_READY_TASK_RATIO] == pytest.approx(0.0)
    assert obs[2:8].sum() == pytest.approx(0.0)


def test_epic_slots_zero_when_no_graph():
    # Without a beads graph (state.graph is None), epic slots 8-11 must be 0.0.
    obs = encode_observation(_state(), _NULL_CTX)
    assert obs[_S_EPIC_GLOBAL_RATIO] == pytest.approx(0.0, abs=1e-5)
    assert obs[_S_EPIC_TOP0] == pytest.approx(0.0, abs=1e-5)


def test_no_graph_zero_in_dependency_and_epic_slots():
    # Without graph: dependency slots 0-1 and epic slots 8-11 are 0.0.
    obs = encode_observation(_state(), _NULL_CTX)
    assert obs[_S_BLOCKED_TASK_RATIO] == pytest.approx(0.0)
    assert obs[_S_READY_TASK_RATIO] == pytest.approx(0.0)
    assert obs[_S_EPIC_GLOBAL_RATIO : _S_EPIC_TOP0 + 3].sum() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Issue group (12-16)
# ---------------------------------------------------------------------------


def test_issue_open_count_slot():
    issues = [
        IssueSnapshot(
            issue_number=i,
            title="x",
            state="open",
            priority=None,
            labels=[],
            source=None,
        )
        for i in range(20)
    ]
    obs = encode_observation(_state(open_issues=issues), _NULL_CTX)
    assert obs[_S_ISSUE_OPEN] == pytest.approx(20 / 200.0, abs=1e-5)


def test_issue_closed_and_created():
    obs = encode_observation(
        _state(), _ctx(issues_closed_this_session=10, issues_created_this_session=5)
    )
    assert obs[_S_ISSUE_CLOSED] == pytest.approx(10 / 100.0, abs=1e-5)
    assert obs[_S_ISSUE_CREATED] == pytest.approx(5 / 50.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Budget group (33-36)
# ---------------------------------------------------------------------------


def test_budget_remaining_and_spent():
    obs = encode_observation(_state(budget=_budget(remaining=3.0, spent=7.0)), _NULL_CTX)
    assert obs[_S_BUDGET_REMAINING] == pytest.approx(0.3, abs=1e-5)
    assert obs[_S_BUDGET_SPENT] == pytest.approx(0.7, abs=1e-5)


def test_budget_sufficiency_flag_true():
    obs = encode_observation(_state(budget=_budget(remaining=8.0, avg_cost=0.05)), _NULL_CTX)
    assert obs[_S_BUDGET_SUFFICIENCY] == 1.0


def test_budget_sufficiency_flag_false():
    obs = encode_observation(
        _state(budget=_budget(remaining=0.05, spent=9.95, avg_cost=0.05)), _NULL_CTX
    )
    assert obs[_S_BUDGET_SUFFICIENCY] == 0.0


def test_encode_observation_reads_trajectory_from_state():
    obs = encode_observation(
        _state(
            trajectory=TrajectorySnapshot(
                projected_alignment_at_budget_end=0.7,
                estimated_remaining_plays=5,
                estimated_remaining_cost=2.5,
            )
        ),
        _NULL_CTX,
    )
    assert obs[_S_PROJ_ALIGNMENT] == pytest.approx(0.7, abs=1e-5)
    assert obs[_S_EST_PLAYS] > 0.0
    assert obs[_S_EST_COST] > 0.0


# ---------------------------------------------------------------------------
# Agent group (17-32)
# ---------------------------------------------------------------------------


def test_tier_fleet_aggregates_per_tier():
    """idle/busy/total counts roll up by ``model_tier`` into the 3-slot tier block."""
    agents = [
        _agent("a0", AgentStatus.IDLE),  # no tier set → medium (idx 1)
        _agent("a1", AgentStatus.BUSY),  # medium (idx 1)
        _agent("a2", AgentStatus.ERROR),  # medium (idx 1), counted in total but not idle/busy
    ]
    obs = encode_observation(_state(agents=agents), _NULL_CTX)
    # tier 0 (small) and tier 2 (large) are empty — their feature slots stay 0
    # except for the avg-success slot, which is the neutral prior 0.5.
    medium_base = 17 + 1 * 5  # = 22 (tier 1, start of medium fleet block)
    assert obs[medium_base] == pytest.approx(1 / 10.0, abs=1e-5)  # idle count
    assert obs[medium_base + 1] == pytest.approx(1 / 10.0, abs=1e-5)  # busy count
    assert obs[medium_base + 2] == pytest.approx(0.5, abs=1e-5)  # success neutral
    assert obs[medium_base + 3] == pytest.approx(0.0, abs=1e-5)  # avg context = 0
    assert obs[medium_base + 4] == pytest.approx(3 / 10.0, abs=1e-5)  # total agents
    # Empty tiers (small=0, large=2) — only the avg-success slot is neutral 0.5.
    small_base = 17
    assert obs[small_base + 2] == pytest.approx(0.5, abs=1e-5)
    large_base = 17 + 2 * 5
    assert obs[large_base + 2] == pytest.approx(0.5, abs=1e-5)


def test_tier_fleet_routes_by_model_tier():
    """``model_tier`` decides which tier slot an agent contributes to."""
    agents = [
        _agent_with_tier("a", AgentType.CLAUDE_CODE, "large", AgentStatus.IDLE),
        _agent_with_tier("b", AgentType.CODEX, "small", AgentStatus.BUSY),
    ]
    obs = encode_observation(_state(agents=agents), _NULL_CTX)
    small_base = 17  # tier 0
    large_base = 17 + 2 * 5  # tier 2 → slot 27
    # small tier has 1 busy agent (codex/small).
    assert obs[small_base] == pytest.approx(0.0, abs=1e-5)  # idle
    assert obs[small_base + 1] == pytest.approx(1 / 10.0, abs=1e-5)  # busy
    # large tier has 1 idle agent (claude/large).
    assert obs[large_base] == pytest.approx(1 / 10.0, abs=1e-5)  # idle
    assert obs[large_base + 1] == pytest.approx(0.0, abs=1e-5)  # busy


def test_active_agents_slot():
    agents = [_agent("a0", AgentStatus.IDLE), _agent("a1", AgentStatus.ERROR)]
    obs = encode_observation(_state(agents=agents), _NULL_CTX)
    # Active = anything not error/terminated; normalized by _MAX_TOTAL_AGENTS (10).
    assert obs[_S_ACTIVE_AGENTS] == pytest.approx(1 / 10.0, abs=1e-5)


# ---------------------------------------------------------------------------
# History group (37-52)
# ---------------------------------------------------------------------------


def test_play_history_type_slots():
    pt0 = PlayType.ISSUE_PICKUP
    pt1 = PlayType.CODE_REVIEW
    obs = encode_observation(
        _state(),
        _ctx(
            last_play_types=(pt0, pt1, None, None, None),
            last_play_success=(True, False, None, None, None),
        ),
    )
    expected_t0 = PLAY_TO_INDEX[pt0] / float(NUM_ACTIONS - 1)
    expected_t1 = PLAY_TO_INDEX[pt1] / float(NUM_ACTIONS - 1)
    assert obs[_S_HIST_TYPE_START] == pytest.approx(expected_t0, abs=1e-5)
    assert obs[_S_HIST_TYPE_START + 1] == pytest.approx(expected_t1, abs=1e-5)
    assert obs[_S_HIST_TYPE_START + 2] == pytest.approx(0.0, abs=1e-5)
    assert obs[_S_HIST_SUCCESS_START] == pytest.approx(1.0, abs=1e-5)
    assert obs[_S_HIST_SUCCESS_START + 1] == pytest.approx(0.0, abs=1e-5)


def test_valid_hist_count_slot():
    obs = encode_observation(
        _state(),
        _ctx(
            last_play_types=(PlayType.ISSUE_PICKUP, PlayType.CODE_REVIEW, None, None, None),
            last_play_success=(True, True, None, None, None),
        ),
    )
    assert obs[_S_VALID_HIST] == pytest.approx(2 / 5.0, abs=1e-5)


def test_rolling_success_rate_slot():
    obs = encode_observation(_state(), _ctx(rolling_success_rate=0.75))
    assert obs[_S_ROLLING_SUCCESS] == pytest.approx(0.75, abs=1e-5)


def test_total_plays_slot():
    obs = encode_observation(_state(total_plays=100), _NULL_CTX)
    assert obs[_S_TOTAL_PLAYS] == pytest.approx(100 / 200.0, abs=1e-5)


def test_total_plays_saturates_at_200():
    obs = encode_observation(_state(total_plays=300), _NULL_CTX)
    assert obs[_S_TOTAL_PLAYS] == pytest.approx(1.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Health group (59-62)
# ---------------------------------------------------------------------------


def test_same_type_streak_slot():
    obs = encode_observation(_state(same_type_failure_streak=5), _NULL_CTX)
    assert obs[_S_STREAK] == pytest.approx(5 / 10.0, abs=1e-5)


def test_streak_clamps_at_1():
    obs = encode_observation(_state(same_type_failure_streak=20), _NULL_CTX)
    assert obs[_S_STREAK] == pytest.approx(1.0, abs=1e-5)


def test_loop_escalation_levels():
    for streak, expected_level in [(0, 0), (2, 0), (3, 1), (5, 2), (7, 3)]:
        obs = encode_observation(_state(same_type_failure_streak=streak), _NULL_CTX)
        assert obs[_S_LOOP_LEVEL] == pytest.approx(expected_level / 3.0, abs=1e-5), (
            f"streak={streak}"
        )


def test_agents_in_error_slot():
    agents = [
        _agent("a0", AgentStatus.ERROR),
        _agent("a1", AgentStatus.ERROR),
        _agent("a2", AgentStatus.IDLE),
    ]
    obs = encode_observation(_state(agents=agents), _NULL_CTX)
    assert obs[_S_AGENTS_IN_ERROR] == pytest.approx(2 / 5.0, abs=1e-5)


def test_stagnation_counter_slot():
    obs = encode_observation(_state(), _ctx(stagnation_counter=3))
    assert obs[_S_STAGNATION] == pytest.approx(3 / 10.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Handoff
# ---------------------------------------------------------------------------


def test_avg_context_loss_slot():
    obs = encode_observation(_state(), _ctx(rolling_avg_context_loss=0.6))
    assert obs[_S_AVG_CONTEXT_LOSS] == pytest.approx(0.6, abs=1e-5)


# ---------------------------------------------------------------------------
# PR group
# ---------------------------------------------------------------------------


def test_pr_slots():
    obs = encode_observation(
        _state(), _ctx(open_pr_count=3, prs_awaiting_review=2, prs_approved_unmerged=1)
    )
    assert obs[_S_OPEN_PRS] == pytest.approx(3 / 10.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Reserved version slot
# ---------------------------------------------------------------------------


def test_obs_version_slot():
    obs = encode_observation(_state(), _NULL_CTX)
    assert obs[_S_OBS_VERSION] == pytest.approx(OBSERVATION_VERSION / 13.0, abs=1e-5)


def test_obs_version_is_nonzero():
    obs = encode_observation(_state(), _NULL_CTX)
    assert obs[_S_OBS_VERSION] > 0.0


# ---------------------------------------------------------------------------
# Saturation / clamp invariants
# ---------------------------------------------------------------------------


def test_retired_slots_are_zero_regardless_of_ctx():
    # Retired slots (2-7) are always 0.0; dependency slots 0-1 are 0.0 without graph.
    obs = encode_observation(_state(), _NULL_CTX)
    assert obs[2:8].sum() == pytest.approx(0.0, abs=1e-5)


def test_budget_remaining_clamps():
    obs = encode_observation(
        _state(
            budget=BudgetSnapshot(
                total_budget=1.0, spent=0.0, remaining=999.0, estimated_cost_per_play=0.1
            )
        ),
        _NULL_CTX,
    )
    assert obs[_S_BUDGET_REMAINING] <= 1.0


def test_all_values_non_negative():
    obs = encode_observation(_state(same_type_failure_streak=15), _NULL_CTX)
    assert (obs >= 0.0).all()


# ---------------------------------------------------------------------------
# Cluster drift slot
# ---------------------------------------------------------------------------


def test_cluster_drift_slot():
    obs = encode_observation(_state(), _ctx(cluster_drift=0.42))
    assert obs[_S_CLUSTER_DRIFT] == pytest.approx(0.42, abs=1e-5)


# ---------------------------------------------------------------------------
# Alignment check time slot
# ---------------------------------------------------------------------------


def test_since_alignment_check_slot():
    obs = encode_observation(_state(), _ctx(minutes_since_last_alignment_check=30.0))
    assert obs[_S_SINCE_ALIGNMENT] == pytest.approx(0.5, abs=1e-5)


# ---------------------------------------------------------------------------
# Per-config block (slots 72..167) and PR-author block (168..171)
# ---------------------------------------------------------------------------


def _agent_with_tier(
    agent_id: str,
    agent_type: AgentType,
    model_tier: str,
    status: AgentStatus = AgentStatus.IDLE,
    *,
    tasks_completed: int = 0,
    tasks_failed: int = 0,
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=agent_type,
        status=status,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=tasks_completed,
        tasks_failed=tasks_failed,
        model_tier=model_tier,
    )


def test_per_config_block_zero_when_index_empty():
    obs = encode_observation(_state(), _NULL_CTX)
    # Slots 72..167 must be zero when no config index is supplied.
    assert np.all(obs[72:168] == 0.0)


def test_per_config_block_counts_idle_and_busy():
    state = _state(
        agents=[
            _agent_with_tier("a", AgentType.CLAUDE_CODE, "medium", AgentStatus.IDLE),
            _agent_with_tier("b", AgentType.CLAUDE_CODE, "medium", AgentStatus.BUSY),
            _agent_with_tier("c", AgentType.CODEX, "medium", AgentStatus.IDLE),
        ]
    )
    config_index = (("claude_code", "medium"), ("codex", "medium"))
    obs = encode_observation(state, _NULL_CTX, config_index=config_index)
    # Config 0 = claude_code/medium: idle=1, busy=1, success_rate=0.5 (default).
    assert obs[72] == pytest.approx(1 / 5.0, abs=1e-5)  # idle/_MAX_AGENTS
    assert obs[73] == pytest.approx(1 / 5.0, abs=1e-5)  # busy
    assert obs[74] == pytest.approx(0.5, abs=1e-5)  # neutral default success
    # Config 1 = codex/medium: idle=1, busy=0.
    assert obs[75] == pytest.approx(1 / 5.0, abs=1e-5)
    assert obs[76] == 0.0
    assert obs[77] == pytest.approx(0.5, abs=1e-5)


def test_per_config_block_success_rate_uses_real_data():
    state = _state(
        agents=[
            _agent_with_tier(
                "a",
                AgentType.CLAUDE_CODE,
                "medium",
                tasks_completed=8,
                tasks_failed=2,
            ),
        ]
    )
    obs = encode_observation(state, _NULL_CTX, config_index=(("claude_code", "medium"),))
    # 8/(8+2) = 0.8
    assert obs[74] == pytest.approx(0.8, abs=1e-5)


def test_pr_author_slots_split_by_author_type():
    from agentshore.state import PullRequestSnapshot

    def _pr(num: int, author: str | None, decision: str | None = None) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            pr_number=num,
            title="t",
            state="open",
            branch=None,
            issue_number=None,
            labels=[],
            review_decision=decision,
            status_check_summary=None,
            is_draft=False,
            blocked=False,
            blocked_reasons=[],
            author_agent_type=author,
        )

    state = _state(
        pull_requests=[
            _pr(1, "claude_code"),
            _pr(2, "claude_code", decision="APPROVED"),
            _pr(3, "codex"),
            _pr(4, "codex"),
        ]
    )
    obs = encode_observation(state, _NULL_CTX)
    # 2 claude_code open, 1 awaiting review (#1 — #2 is APPROVED).
    assert obs[168] == pytest.approx(2 / 10.0, abs=1e-5)
    assert obs[170] == pytest.approx(1 / 10.0, abs=1e-5)
    # 2 codex open, 2 awaiting review.
    assert obs[169] == pytest.approx(2 / 10.0, abs=1e-5)
    assert obs[171] == pytest.approx(2 / 10.0, abs=1e-5)


def test_observation_version_is_13():
    # desktop-rni0: bumped 10 → 11 when IDLE_TICK / RECOVER were demoted.
    # desktop-8zzy: bumped 11 → 12 when pr_pressure_ratio slot was added.
    # Beads dependency: bumped 12 → 13 when blocked/ready task ratios
    # repurposed retired cluster slots 0-1.
    assert OBSERVATION_VERSION == 13


# ---------------------------------------------------------------------------
# Velocity + busy-agents slots (Group B)
# ---------------------------------------------------------------------------


def test_velocity_slot_populated():
    """Rolling velocity maps into [0, 1] in the observation vector."""
    from agentshore.rl.observation import _S_ROLLING_VELOCITY

    obs = encode_observation(_state(), _ctx(rolling_velocity=0.75))
    assert abs(obs[_S_ROLLING_VELOCITY] - 0.75) < 1e-5


def test_busy_agents_slot_populated():
    """Busy-agent count maps into [0, 1] in the observation vector."""
    from agentshore.rl.observation import _S_BUSY_AGENTS

    obs = encode_observation(_state(), _ctx(busy_agent_count=3))
    # Normalized by _MAX_TOTAL_AGENTS (10): 3/10 = 0.3
    assert obs[_S_BUSY_AGENTS] == pytest.approx(3 / 10, abs=0.01)


def test_velocity_slot_clamped():
    """Rolling velocity is clamped to [0, 1] before encoding."""
    from agentshore.rl.observation import _S_ROLLING_VELOCITY

    obs = encode_observation(_state(), _ctx(rolling_velocity=2.5))  # over 1.0
    assert obs[_S_ROLLING_VELOCITY] <= 1.0 + 1e-6


# ---------------------------------------------------------------------------
# Specialization block (177..276) — Issue #333
# ---------------------------------------------------------------------------


def test_specialization_unobserved_cells_default_to_neutral():
    """With no specialization input, every slot defaults to 0.5."""
    from agentshore.rl.observation import (
        _S_SPEC_BLOCK_END,
        _S_SPEC_BLOCK_START,
    )

    obs = encode_observation(_state(), _NULL_CTX)
    block = obs[_S_SPEC_BLOCK_START:_S_SPEC_BLOCK_END]
    assert np.allclose(block, 0.5)


def test_specialization_slots_encode_per_tier_play_rates():
    """Per-agent cells aggregate by ``model_tier`` into the 3-tier × 20-play block."""
    from agentshore.rl.action_space import PLAY_TO_INDEX
    from agentshore.rl.observation import _S_SPEC_BLOCK_START
    from agentshore.state import AgentPlaySpecializationSnapshot

    # One small-tier agent, one large-tier — they should land in tier 0 / tier 2.
    agents = [
        _agent_with_tier("a", AgentType.CLAUDE_CODE, "small"),
        _agent_with_tier("b", AgentType.CLAUDE_CODE, "large"),
    ]
    spec = (
        AgentPlaySpecializationSnapshot(
            agent_id="a",
            play_type=PlayType.ISSUE_PICKUP,
            total=4,
            successful=3,
            failed=1,
            success_rate=0.75,
            rolling_success_rate=0.75,
        ),
        AgentPlaySpecializationSnapshot(
            agent_id="b",
            play_type=PlayType.CODE_REVIEW,
            total=2,
            successful=1,
            failed=1,
            success_rate=0.5,
            rolling_success_rate=0.5,
        ),
        # Legacy string play type — must be skipped from the fixed-shape block.
        AgentPlaySpecializationSnapshot(
            agent_id="a",
            play_type="legacy_play",
            total=1,
            successful=1,
            failed=0,
            success_rate=1.0,
            rolling_success_rate=1.0,
        ),
    )
    obs = encode_observation(_state(agents=agents), _ctx(agent_specialization=spec))

    # tier 0 (small) × ISSUE_PICKUP cell: 3 successful / 4 total = 0.75.
    pickup_slot = _S_SPEC_BLOCK_START + 0 * NUM_ACTIONS + PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]
    # tier 2 (large) × CODE_REVIEW cell: 1 / 2 = 0.5.
    review_slot = _S_SPEC_BLOCK_START + 2 * NUM_ACTIONS + PLAY_TO_INDEX[PlayType.CODE_REVIEW]
    assert obs[pickup_slot] == pytest.approx(0.75, abs=1e-5)
    assert obs[review_slot] == pytest.approx(0.5, abs=1e-5)

    # Untouched slots in the block remain at the neutral prior.
    untouched_slot = _S_SPEC_BLOCK_START + 0 * NUM_ACTIONS + PLAY_TO_INDEX[PlayType.RUN_QA]
    assert obs[untouched_slot] == pytest.approx(0.5, abs=1e-5)
    # tier 1 (medium) has no agents in this scenario — entire row stays neutral.
    medium_pickup = _S_SPEC_BLOCK_START + 1 * NUM_ACTIONS + PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]
    assert obs[medium_pickup] == pytest.approx(0.5, abs=1e-5)


def test_specialization_aggregates_multiple_agents_per_tier():
    """Two agents in the same tier sharing a play_type combine into a weighted rate."""
    from agentshore.rl.action_space import PLAY_TO_INDEX
    from agentshore.rl.observation import _S_SPEC_BLOCK_START
    from agentshore.state import AgentPlaySpecializationSnapshot

    agents = [
        _agent_with_tier("a", AgentType.CLAUDE_CODE, "medium"),
        _agent_with_tier("b", AgentType.CODEX, "medium"),
    ]
    spec = (
        AgentPlaySpecializationSnapshot(
            agent_id="a",
            play_type=PlayType.ISSUE_PICKUP,
            total=4,
            successful=3,
            failed=1,
            success_rate=0.75,
            rolling_success_rate=0.75,
        ),
        AgentPlaySpecializationSnapshot(
            agent_id="b",
            play_type=PlayType.ISSUE_PICKUP,
            total=2,
            successful=1,
            failed=1,
            success_rate=0.5,
            rolling_success_rate=0.5,
        ),
    )
    obs = encode_observation(_state(agents=agents), _ctx(agent_specialization=spec))

    # Aggregate: 4 successful / 6 total = 0.6666… across both medium-tier agents.
    medium_pickup = _S_SPEC_BLOCK_START + 1 * NUM_ACTIONS + PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]
    assert obs[medium_pickup] == pytest.approx(4 / 6, abs=1e-5)


def test_specialization_drops_unknown_tier_agents():
    """Agents with ``model_tier`` outside {small, medium, large} are silently dropped."""
    from agentshore.state import AgentPlaySpecializationSnapshot

    # One agent with an unknown tier — encoder must skip it without raising.
    agents = [_agent_with_tier("x", AgentType.CLAUDE_CODE, "huge")]
    cell = AgentPlaySpecializationSnapshot(
        agent_id="x",
        play_type=PlayType.ISSUE_PICKUP,
        total=10,
        successful=10,
        failed=0,
        success_rate=1.0,
        rolling_success_rate=1.0,
    )
    obs = encode_observation(_state(agents=agents), _ctx(agent_specialization=(cell,)))
    assert obs.shape == (OBSERVATION_DIM,)
    # No cell populated for an unknown tier — full block stays at neutral.
    from agentshore.rl.observation import _S_SPEC_BLOCK_END, _S_SPEC_BLOCK_START

    assert np.allclose(obs[_S_SPEC_BLOCK_START:_S_SPEC_BLOCK_END], 0.5)


# ===========================================================================
# v0.15 Phase 5 — executor_skip_rate_recent_50 slot (177)
# ===========================================================================


def test_executor_skip_rate_default_is_zero():
    """Default ObservationContext has skip_rate=0; encoder writes 0 at slot 177."""
    from agentshore.rl.observation import _S_EXECUTOR_SKIP_RATE

    obs = encode_observation(_state(), _NULL_CTX)
    assert obs[_S_EXECUTOR_SKIP_RATE] == pytest.approx(0.0)


def test_executor_skip_rate_writes_to_slot_177():
    """Non-zero rate is written to slot 177 unchanged (it's already in [0,1])."""
    from agentshore.rl.observation import _S_EXECUTOR_SKIP_RATE

    ctx = _ctx(executor_skip_rate_recent_50=0.42)
    obs = encode_observation(_state(), ctx)
    assert obs[_S_EXECUTOR_SKIP_RATE] == pytest.approx(0.42)


def test_executor_skip_rate_clamps_above_one():
    """Defense-in-depth: an invalid >1.0 value is clamped to 1.0."""
    from agentshore.rl.observation import _S_EXECUTOR_SKIP_RATE

    ctx = _ctx(executor_skip_rate_recent_50=2.5)
    obs = encode_observation(_state(), ctx)
    assert obs[_S_EXECUTOR_SKIP_RATE] == pytest.approx(1.0)


def test_executor_skip_rate_slot_index_is_177():
    """Pin the slot index — Phase 5 contract is that skip-rate sits at 177."""
    from agentshore.rl.observation import _S_EXECUTOR_SKIP_RATE

    assert _S_EXECUTOR_SKIP_RATE == 177


# ===========================================================================
# desktop-8zzy — pr_pressure_ratio slot (178)
# ===========================================================================


def test_pr_pressure_ratio_slot_index_is_178():
    """desktop-8zzy contract: pr_pressure_ratio sits at slot 178, between the
    executor-skip-rate slot (177) and the specialization block (179..244)."""
    from agentshore.rl.observation import _S_PR_PRESSURE_RATIO

    assert _S_PR_PRESSURE_RATIO == 178


def test_pr_pressure_ratio_default_zero():
    """No open PRs → ratio is 0."""
    from agentshore.rl.observation import _S_PR_PRESSURE_RATIO

    obs = encode_observation(_state(), _NULL_CTX)
    assert obs[_S_PR_PRESSURE_RATIO] == pytest.approx(0.0)


def test_pr_pressure_ratio_below_threshold():
    """At open_pr_count = 3 (max 10), ratio is 0.3."""
    from agentshore.rl.observation import _S_PR_PRESSURE_RATIO

    obs = encode_observation(_state(), _ctx(open_pr_count=3))
    assert obs[_S_PR_PRESSURE_RATIO] == pytest.approx(0.3, abs=1e-5)


def test_pr_pressure_ratio_at_saturation():
    """At open_pr_count == max, ratio is 1.0."""
    from agentshore.rl.observation import _S_PR_PRESSURE_RATIO

    obs = encode_observation(_state(), _ctx(open_pr_count=10))
    assert obs[_S_PR_PRESSURE_RATIO] == pytest.approx(1.0, abs=1e-5)


def test_pr_pressure_ratio_clamped_above_one():
    """At open_pr_count > max, ratio caps at 1.0."""
    from agentshore.rl.observation import _S_PR_PRESSURE_RATIO

    obs = encode_observation(_state(), _ctx(open_pr_count=18))
    assert obs[_S_PR_PRESSURE_RATIO] == pytest.approx(1.0, abs=1e-5)
