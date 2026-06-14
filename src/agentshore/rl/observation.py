"""Observation encoder — builds a fixed-size float32 vector from OrchestratorState
and ObservationContext.

Slot layout (OBSERVATION_DIM=246, OBSERVATION_VERSION=13):
  0-1     dependency ( 2)  blocked_task_ratio + ready_task_ratio from beads graph (v13)
  2-7     retired    ( 6)  permanently zero-filled
  8-11    epic       ( 4)  global_closure_ratio + top-3 epic closure ratios (Track 5, beads-native)
  12-16   issue      ( 5)  open, closed, created, net-velocity, scope-completion
  17-32   tier-fleet (16)  3 tiers × 5 (idle,busy,avg_success,avg_context,total) + active-count
  33-36   budget     ( 4)  remaining, spent, avg-cost, sufficiency-flag
  37-52   history    (16)  last-5 play-types + last-5 success-flags + rolling stats + drift
  53-55   time       ( 3)  session-duration, since-calibration, since-seed
  56-58   pr         ( 3)  open-prs, awaiting-review, approved-unmerged
  59-62   health     ( 4)  stagnation, streak, loop-level, agents-in-error
  63-64   handoff    ( 2)  avg-context-loss, avg-rampup-ms
  65-67   trajectory ( 3)  projected-alignment, est-plays, est-cost
  68-70   learnings  ( 3)  count, avg-confidence, injection-rate
  71      churn      ( 1)  issue churn rate over last 10 plays
  72-167  per-config (96)  32×(idle_count, busy_count, success_rate) zero-padded
  168-171 pr-author  ( 4)  open + awaiting-review counts per claude_code/codex authorship
  172     velocity   ( 1)  rolling velocity (issues+PRs closed per play, last K plays)
  173     busy-agents( 1)  normalized busy-agent count
  174     unreviewed ( 1)  fraction of open PRs with unreviewed commits
  175     mergeable  ( 1)  fraction of open PRs with mergeable=MERGEABLE
  176     in-flight  ( 1)  normalized in-flight issue_pickup issue count
  177     skip-rate  ( 1)  fraction of recent selection cycles that hit a clean
                          confirm/claim re-pick (live-drift signal; slot repointed
                          from the removed executor masked-skip path)
  178     pr-pressure( 1)  open_pr_count / SAT_OPEN_PRS_COUNT, clamped to [0, 1]
  179-244 spec       (66)  3 tiers × 22 plays specialization success rates (0.5 default)
  245     reserved   ( 1)  version marker = 1.0 (a stable per-version constant)

Tier order is (small, medium, large) — index 0/1/2 — matching `cheapest-first`
across the tier-fleet block (17-32) and the specialization block (179-244).
Fleet-wide counts saturate at ``_MAX_TOTAL_AGENTS`` to cover typical
multi-cell expansion without making the vector sensitive to very high caps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import numpy as np

from agentshore.play_rules import pr_is_approved
from agentshore.rl.action_space import (
    NUM_ACTIONS,
    PLAY_TO_INDEX,
)
from agentshore.rl.config_head import MAX_CONFIG_INDEX_SIZE
from agentshore.rl.constants import SAT_OPEN_PRS_COUNT
from agentshore.state import AgentStatus, PlayType, loop_level_for_streak

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from agentshore.rl.config_head import ConfigKey
    from agentshore.state import AgentPlaySpecializationSnapshot, AgentSnapshot, OrchestratorState

# Per-config block: 32 configs × 3 metrics (idle, busy, success-rate) = 96 floats.
_CONFIG_FEATURES_PER_SLOT: Final[int] = 3
_CONFIG_BLOCK_SIZE: Final[int] = MAX_CONFIG_INDEX_SIZE * _CONFIG_FEATURES_PER_SLOT
_PR_AUTHOR_FEATURES: Final[int] = 4

_MAX_CLUSTERS: Final[int] = 10
# Saturation points for normalization. ``_MAX_AGENTS`` bounds the
# idle/busy counts *within* a single (agent_type, model_tier) cell in
# the per-config block. It covers the common case (tier ``max`` defaults to 1;
# budgets keep most cells small); a tier ``max`` raised well above 5 will
# saturate this normalization feature, which is acceptable since the absolute
# count past that point carries little policy signal.  ``_MAX_TOTAL_AGENTS``
# bounds fleet-wide counts
# (tier-fleet block, active-count, in-flight, busy-agents) — sized to cover
# a typical multi-cell expansion (e.g. 4 types × 3 tiers × 2 = 24 theoretical
# max, capped at 10 because realistic budgets keep PPO well under that).
_MAX_AGENTS: Final[int] = 5
_MAX_TOTAL_AGENTS: Final[int] = 10
_HIST_LEN: Final[int] = 5

# Tier-keyed encoding: (small, medium, large). Index 0/1/2; matches the
# canonical cheapest-first ordering used in tier-eligibility lists.
_NUM_TIERS: Final[int] = 3
_TIER_INDEX: Final[dict[str, int]] = {"small": 0, "medium": 1, "large": 2}
# 5 features per tier in the fleet block: idle_count, busy_count,
# avg_success_rate, avg_context_ratio, total_count.
_FLEET_FEATURES_PER_TIER: Final[int] = 5
_TIER_FLEET_BLOCK_SIZE: Final[int] = _NUM_TIERS * _FLEET_FEATURES_PER_TIER  # = 15

# Specialization block: 3 tiers × NUM_ACTIONS play actions, success rate per cell.
# Auto-sizes with the action space; at NUM_ACTIONS=22 the block is 66 slots.
_SPEC_BLOCK_SIZE: Final[int] = _NUM_TIERS * NUM_ACTIONS  # = 66 at v0.15
# Neutral prior for cells with no observations yet (matches per-config block).
_SPEC_NEUTRAL: Final[float] = 0.5

# Slot 177 is the executor-skip-rate diagnostic between the in-flight slot
# (176) and the new pr_pressure_ratio slot at 178; slot 178 is the
# desktop-8zzy pressure ratio added in OBSERVATION_VERSION 12. The `+ 7` below
# counts the velocity/busy/unreviewed/mergeable/in-flight/skip-rate/pr-pressure
# block.
OBSERVATION_DIM: Final[int] = (
    73 + _CONFIG_BLOCK_SIZE + _PR_AUTHOR_FEATURES + 7 + _SPEC_BLOCK_SIZE
)  # = 246 with NUM_ACTIONS=22
OBSERVATION_VERSION: Final[int] = 13

# Per-feature saturation points (clip then scale → [0, 1])
_SAT_DOLLAR_PER_PLAY: Final[float] = 2.0
_SAT_SECONDS_PER_PLAY: Final[float] = 600.0
_SAT_PLAYS: Final[float] = 200.0
_SAT_STREAK: Final[float] = 10.0
_SAT_CONTEXT_TOKENS: Final[float] = 200_000.0
_SAT_OPEN_ISSUES: Final[float] = 200.0
_SAT_CLOSED_ISSUES: Final[float] = 100.0
_SAT_CREATED_ISSUES: Final[float] = 50.0
_SAT_MINUTES_ALIGNMENT: Final[float] = 60.0
_SAT_MINUTES_INTAKE: Final[float] = 480.0
_SAT_RAMPUP_MS: Final[float] = 10_000.0
_SAT_ISSUE_VELOCITY: Final[float] = 10.0
_LOOP_LEVEL_MAX: Final[float] = 3.0
_SAT_AGENTS_IN_ERROR: Final[float] = 5.0

# Named slot indices — locked; changing any value bumps OBSERVATION_VERSION
_S_BLOCKED_TASK_RATIO: Final[int] = 0  # beads: blocked / total tasks
_S_READY_TASK_RATIO: Final[int] = 1  # beads: ready / total tasks
# Slots 2-7: retired, permanently zero-filled
# Slots 8-11 repurposed in Track 5 (v7): epic closure ratios from beads graph.
# Previously: top-2 legacy alignment slots plus legacy mean/count.
_S_EPIC_GLOBAL_RATIO: Final[int] = 8  # global_closure_ratio from ProjectGraph
_S_EPIC_TOP0: Final[int] = 9  # top-1 epic closure ratio (largest epic by total_tasks first)
_S_EPIC_TOP1: Final[int] = 10  # top-2 epic closure ratio
_S_EPIC_TOP2: Final[int] = 11  # top-3 epic closure ratio
_S_ISSUE_OPEN: Final[int] = 12
_S_ISSUE_CLOSED: Final[int] = 13
_S_ISSUE_CREATED: Final[int] = 14
_S_ISSUE_NET_VEL: Final[int] = 15
_S_ISSUE_SCOPE: Final[int] = 16
_S_AGENT_START: Final[int] = 17  # 17-31: 3 tiers × 5 features = 15 slots
_S_ACTIVE_AGENTS: Final[int] = 32
_S_BUDGET_REMAINING: Final[int] = 33
_S_BUDGET_SPENT: Final[int] = 34
_S_BUDGET_AVG_COST: Final[int] = 35
_S_BUDGET_SUFFICIENCY: Final[int] = 36
_S_HIST_TYPE_START: Final[int] = 37  # 37-41
_S_HIST_SUCCESS_START: Final[int] = 42  # 42-46
_S_ROLLING_SUCCESS: Final[int] = 47
_S_ROLLING_COST: Final[int] = 48
_S_ROLLING_DURATION: Final[int] = 49
_S_TOTAL_PLAYS: Final[int] = 50
_S_VALID_HIST: Final[int] = 51
_S_CLUSTER_DRIFT: Final[int] = 52
_S_SESSION_DURATION: Final[int] = 53
_S_SINCE_ALIGNMENT: Final[int] = 54
_S_SINCE_INTAKE: Final[int] = 55
_S_OPEN_PRS: Final[int] = 56
_S_PRS_AWAITING: Final[int] = 57
_S_PRS_APPROVED: Final[int] = 58
_S_STAGNATION: Final[int] = 59
_S_STREAK: Final[int] = 60
_S_LOOP_LEVEL: Final[int] = 61
_S_AGENTS_IN_ERROR: Final[int] = 62
_S_AVG_CONTEXT_LOSS: Final[int] = 63
_S_AVG_RAMPUP: Final[int] = 64
_S_PROJ_ALIGNMENT: Final[int] = 65
_S_EST_PLAYS: Final[int] = 66
_S_EST_COST: Final[int] = 67
_S_LEARNING_COUNT: Final[int] = 68
_S_LEARNING_CONFIDENCE: Final[int] = 69
_S_LEARNING_INJECTION: Final[int] = 70
_S_ISSUE_CHURN: Final[int] = 71
# Per-config block: 32 configs × 3 metrics, slots 72..167.
_S_CONFIG_BLOCK_START: Final[int] = 72
_S_CONFIG_BLOCK_END: Final[int] = _S_CONFIG_BLOCK_START + _CONFIG_BLOCK_SIZE  # 168
# PR-author block: 4 features at 168..171.
_S_PR_AUTHOR_CLAUDE_OPEN: Final[int] = 168
_S_PR_AUTHOR_CODEX_OPEN: Final[int] = 169
_S_PR_AUTHOR_CLAUDE_AWAITING: Final[int] = 170
_S_PR_AUTHOR_CODEX_AWAITING: Final[int] = 171
_S_ROLLING_VELOCITY: Final[int] = 172
_S_BUSY_AGENTS: Final[int] = 173
_S_FRAC_UNREVIEWED_PRS: Final[int] = 174
_S_FRAC_MERGEABLE_PRS: Final[int] = 175
_S_INFLIGHT_ISSUES: Final[int] = 176
# Clean confirm/claim re-pick rate over recent selection cycles. Diagnostic
# signal so PPO sees the live-drift rate (a selected play whose live confirm or
# work-claim CAS lost a race); no associated action. Repointed from the removed
# executor masked-skip path — same slot, same [0, 1] range.
_S_EXECUTOR_SKIP_RATE: Final[int] = 177
# desktop-8zzy: open_pr_count / SAT_OPEN_PRS_COUNT, clamped to [0, 1]. Mirrors
# the _PR_PRESSURE_BONUS reward shaping; lets PPO learn "drain harder near the
# cap" from a normalised ratio rather than inferring it from slot 56.
_S_PR_PRESSURE_RATIO: Final[int] = 178
# Specialization block: 3 tiers × NUM_ACTIONS play actions, success-rate cells.
# Auto-sizes with the action space; at NUM_ACTIONS=22 the block runs 179..244.
_S_SPEC_BLOCK_START: Final[int] = 179
_S_SPEC_BLOCK_END: Final[int] = _S_SPEC_BLOCK_START + _SPEC_BLOCK_SIZE  # 245
# version marker
_S_OBS_VERSION: Final[int] = _S_SPEC_BLOCK_END  # 245


@dataclass(frozen=True, slots=True)
class ObservationContext:
    """Pre-computed metrics passed to encode_observation by MetricsEngine.

    All fields are in natural units; the encoder applies saturation + clipping.
    """

    same_type_failure_streak: int
    stagnation_counter: int
    issues_closed_this_session: int
    issues_created_this_session: int
    last_play_types: tuple[PlayType | None, ...]  # length _HIST_LEN, oldest→newest
    last_play_success: tuple[bool | None, ...]  # length _HIST_LEN, oldest→newest
    rolling_success_rate: float
    rolling_avg_cost: float  # dollars per play
    rolling_avg_duration_s: float  # seconds per play
    rolling_avg_context_loss: float  # 0.0–1.0
    rolling_avg_rampup_ms: float
    open_pr_count: int
    prs_awaiting_review: int
    prs_approved_unmerged: int
    minutes_since_last_alignment_check: float
    minutes_since_last_intake: float
    cluster_drift: float  # 0.0–1.0
    learning_count: int
    learning_avg_confidence: float  # 0.0–1.0
    learning_injection_rate: float  # 0.0–1.0
    issue_churn_rate: float = 0.0  # (issues_created + issues_closed) / max(1, total) over last 10
    rolling_velocity: float = 0.0
    busy_agent_count: int = 0
    stagnation_entropy_multiplier: float = 1.0
    # Fraction of recent selection cycles that ended in a clean confirm/claim
    # re-pick — i.e. a snapshot-eligible play whose live confirm or work-claim
    # CAS lost a race, so it was cleanly re-picked (never a skip row).
    # Observation-only diagnostic; no associated PPO action. (Field name kept
    # for checkpoint/back-compat; semantics repointed from the removed
    # executor masked-skip path.)
    executor_skip_rate_recent_50: float = 0.0
    # Per-agent / per-play specialization cells derived from play history. The
    # encoder aggregates these by the agent's ``model_tier`` into the
    # 3-tier × 22-play specialization block (slots 179-244) so the obs shape
    # tracks tier eligibility rather than ephemeral agent_ids. Defaults to ()
    # so existing callers that build a context by-hand still type-check.
    agent_specialization: tuple[AgentPlaySpecializationSnapshot, ...] = ()


def _clamp(val: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, val))


def _norm(val: float, sat: float) -> float:
    return _clamp(val / sat if sat > 0.0 else 0.0)


def _agent_status_float(status_value: str) -> float:
    if status_value == "idle":
        return 0.0
    if status_value == "busy":
        return 0.5
    return 1.0  # error or terminated


def encode_observation(
    state: OrchestratorState,
    ctx: ObservationContext,
    *,
    config_index: tuple[ConfigKey, ...] = (),
) -> NDArray[np.float32]:
    """Return a float32 ndarray of shape (OBSERVATION_DIM,).

    ``config_index`` lists ``(agent_type, model_tier)`` pairs in the order they
    appear in the second policy head. Feeding it here keeps per-config slots in
    the observation aligned with the head outputs. When omitted (legacy
    callers), the per-config block is left zero — the policy still has the
    play head fully usable.

    Pure function: no DB queries, no side effects, <5ms on CPU.
    Same inputs always produce identical bytes (V1_CONTRACT determinism gate).
    """
    obs = np.zeros(OBSERVATION_DIM, dtype=np.float32)

    # ---- DEPENDENCY (0-1): beads blocked/ready task ratios (v13) ----
    if state.graph is not None and state.graph.tasks_total > 0:
        obs[_S_BLOCKED_TASK_RATIO] = _clamp(state.graph.tasks_blocked / state.graph.tasks_total)
        obs[_S_READY_TASK_RATIO] = _clamp(state.graph.tasks_ready / state.graph.tasks_total)
    # Slots 2-7 remain zero-filled (formerly cluster data).

    # ---- EPIC (8-11): live beads closure ratios (Track 5) ----
    # global_closure_ratio (slot 8) + top-3 epic closure ratios sorted by
    # total_tasks desc (slots 9-11). All default to 0.0 when state.graph is None.
    if state.graph is not None:
        obs[_S_EPIC_GLOBAL_RATIO] = _clamp(state.graph.global_closure_ratio)
        top_epics = sorted(state.graph.epics, key=lambda e: e.total_tasks, reverse=True)[:3]
        for _ei, _ep in enumerate(top_epics):
            obs[_S_EPIC_TOP0 + _ei] = _clamp(_ep.closure_ratio)

    # ---- ISSUE (12-16) ----
    open_count = len(state.open_issues)
    closed = ctx.issues_closed_this_session
    created = ctx.issues_created_this_session
    obs[_S_ISSUE_OPEN] = _norm(open_count, _SAT_OPEN_ISSUES)
    obs[_S_ISSUE_CLOSED] = _norm(closed, _SAT_CLOSED_ISSUES)
    obs[_S_ISSUE_CREATED] = _norm(created, _SAT_CREATED_ISSUES)
    obs[_S_ISSUE_NET_VEL] = _clamp(float(closed - created) / _SAT_ISSUE_VELOCITY + 0.5)
    total_issues = closed + open_count
    obs[_S_ISSUE_SCOPE] = _clamp(closed / total_issues if total_issues > 0 else 0.0)

    # ---- TIER-FLEET (17-32) ----
    # Three tier slots (small=0, medium=1, large=2), 5 features each, plus a
    # single active-count at slot 32. Replaces the v8 per-instance encoding so
    # the obs stays stable across spawn/end churn and a fully expanded fleet
    # (per-(type, tier) cap × 4 types × 3 tiers) is represented.
    obs[_S_AGENT_START : _S_AGENT_START + _TIER_FLEET_BLOCK_SIZE] = 0.0
    tier_buckets: dict[int, list[AgentSnapshot]] = {0: [], 1: [], 2: []}
    for a in state.agents:
        tier_idx = _TIER_INDEX.get(a.model_tier or "medium")
        if tier_idx is None:
            continue
        tier_buckets[tier_idx].append(a)
    for tier_idx, agents_in_tier in tier_buckets.items():
        base = _S_AGENT_START + tier_idx * _FLEET_FEATURES_PER_TIER
        if not agents_in_tier:
            # idle=0, busy=0, success=0.5 (neutral), context=0, total=0
            obs[base + 2] = _SPEC_NEUTRAL
            continue
        idle = sum(1 for a in agents_in_tier if a.status == AgentStatus.IDLE)
        busy = sum(1 for a in agents_in_tier if a.status == AgentStatus.BUSY)
        completed = sum(a.tasks_completed for a in agents_in_tier)
        failed = sum(a.tasks_failed for a in agents_in_tier)
        total_tasks = completed + failed
        avg_success = (completed / total_tasks) if total_tasks > 0 else _SPEC_NEUTRAL
        avg_context = sum(a.context_size for a in agents_in_tier) / len(agents_in_tier)
        obs[base] = _norm(idle, float(_MAX_TOTAL_AGENTS))
        obs[base + 1] = _norm(busy, float(_MAX_TOTAL_AGENTS))
        obs[base + 2] = _clamp(avg_success)
        obs[base + 3] = _norm(avg_context, _SAT_CONTEXT_TOKENS)
        obs[base + 4] = _norm(len(agents_in_tier), float(_MAX_TOTAL_AGENTS))
    active = sum(
        1 for a in state.agents if a.status not in (AgentStatus.ERROR, AgentStatus.TERMINATED)
    )
    obs[_S_ACTIVE_AGENTS] = _norm(active, float(_MAX_TOTAL_AGENTS))

    # ---- BUDGET (33-36) ----
    if state.budget is not None and state.budget.enabled:
        b = state.budget
        total_budget = b.total_budget if b.total_budget > 0.0 else 1.0
        obs[_S_BUDGET_REMAINING] = _clamp(b.remaining / total_budget)
        obs[_S_BUDGET_SPENT] = _clamp(b.spent / total_budget)
        obs[_S_BUDGET_AVG_COST] = _norm(b.estimated_cost_per_play, _SAT_DOLLAR_PER_PLAY)
        obs[_S_BUDGET_SUFFICIENCY] = 1.0 if b.remaining > 2.0 * b.estimated_cost_per_play else 0.0
    elif state.budget is not None:
        obs[_S_BUDGET_REMAINING] = 1.0
        obs[_S_BUDGET_SPENT] = 0.0
        obs[_S_BUDGET_AVG_COST] = _norm(state.budget.estimated_cost_per_play, _SAT_DOLLAR_PER_PLAY)
        obs[_S_BUDGET_SUFFICIENCY] = 1.0

    # ---- HISTORY (37-52) ----
    valid_count = 0
    hist_types = (ctx.last_play_types + (None,) * _HIST_LEN)[:_HIST_LEN]
    hist_success = (ctx.last_play_success + (None,) * _HIST_LEN)[:_HIST_LEN]
    for i, (pt, ok) in enumerate(zip(hist_types, hist_success, strict=True)):
        if pt is not None:
            obs[_S_HIST_TYPE_START + i] = PLAY_TO_INDEX[pt] / float(NUM_ACTIONS - 1)
            valid_count += 1
        if ok is not None:
            obs[_S_HIST_SUCCESS_START + i] = 1.0 if ok else 0.0
    obs[_S_ROLLING_SUCCESS] = _clamp(ctx.rolling_success_rate)
    obs[_S_ROLLING_COST] = _norm(ctx.rolling_avg_cost, _SAT_DOLLAR_PER_PLAY)
    obs[_S_ROLLING_DURATION] = _norm(ctx.rolling_avg_duration_s, _SAT_SECONDS_PER_PLAY)
    obs[_S_TOTAL_PLAYS] = _norm(state.total_plays, _SAT_PLAYS)
    obs[_S_VALID_HIST] = valid_count / _HIST_LEN
    obs[_S_CLUSTER_DRIFT] = _clamp(ctx.cluster_drift)

    # ---- TIME (53-55) ----
    session_dur_min = (
        state.total_plays * ctx.rolling_avg_duration_s / 60.0
        if ctx.rolling_avg_duration_s > 0.0
        else 0.0
    )
    obs[_S_SESSION_DURATION] = _norm(session_dur_min, 480.0)
    obs[_S_SINCE_ALIGNMENT] = _norm(ctx.minutes_since_last_alignment_check, _SAT_MINUTES_ALIGNMENT)
    obs[_S_SINCE_INTAKE] = _norm(ctx.minutes_since_last_intake, _SAT_MINUTES_INTAKE)

    # ---- PR (56-58) ----
    obs[_S_OPEN_PRS] = _norm(ctx.open_pr_count, SAT_OPEN_PRS_COUNT)
    obs[_S_PRS_AWAITING] = _norm(ctx.prs_awaiting_review, SAT_OPEN_PRS_COUNT)
    obs[_S_PRS_APPROVED] = _norm(ctx.prs_approved_unmerged, SAT_OPEN_PRS_COUNT)

    # ---- HEALTH (59-62) ----
    obs[_S_STAGNATION] = _norm(ctx.stagnation_counter, _SAT_STREAK)
    obs[_S_STREAK] = _norm(state.same_type_failure_streak, _SAT_STREAK)
    obs[_S_LOOP_LEVEL] = loop_level_for_streak(state.same_type_failure_streak) / _LOOP_LEVEL_MAX
    agents_in_error = sum(1 for a in state.agents if a.status == AgentStatus.ERROR)
    obs[_S_AGENTS_IN_ERROR] = _norm(agents_in_error, _SAT_AGENTS_IN_ERROR)

    # ---- HANDOFF (63-64) ----
    obs[_S_AVG_CONTEXT_LOSS] = _clamp(ctx.rolling_avg_context_loss)
    obs[_S_AVG_RAMPUP] = _norm(ctx.rolling_avg_rampup_ms, _SAT_RAMPUP_MS)

    # ---- TRAJECTORY (65-67) ----
    if state.trajectory is not None:
        t = state.trajectory
        obs[_S_PROJ_ALIGNMENT] = _clamp(t.projected_alignment_at_budget_end)
        obs[_S_EST_PLAYS] = _norm(t.estimated_remaining_plays, _SAT_PLAYS)
        obs[_S_EST_COST] = _norm(t.estimated_remaining_cost, 100.0)

    # ---- LEARNINGS (68-70) ----
    obs[_S_LEARNING_COUNT] = _norm(ctx.learning_count, 50.0)
    obs[_S_LEARNING_CONFIDENCE] = _clamp(ctx.learning_avg_confidence)
    obs[_S_LEARNING_INJECTION] = _clamp(ctx.learning_injection_rate)

    # ---- CHURN (71) ----
    obs[_S_ISSUE_CHURN] = _clamp(ctx.issue_churn_rate)

    # ---- PER-CONFIG (72-167) ----
    # For each (agent_type, model_tier) in config_index, write idle, busy, and
    # success-rate. Unused slots stay zero. We cap iteration at MAX so that an
    # over-long index can't run off the end of the block.
    for slot, key in enumerate(config_index[:MAX_CONFIG_INDEX_SIZE]):
        agent_type, model_tier = key
        idle = 0
        busy = 0
        completed = 0
        failed = 0
        for a in state.agents:
            if a.agent_type.value != agent_type:
                continue
            tier = a.model_tier or "medium"
            if tier != model_tier:
                continue
            if a.status == AgentStatus.IDLE:
                idle += 1
            elif a.status == AgentStatus.BUSY:
                busy += 1
            completed += a.tasks_completed
            failed += a.tasks_failed
        base = _S_CONFIG_BLOCK_START + slot * _CONFIG_FEATURES_PER_SLOT
        obs[base] = _norm(idle, float(_MAX_AGENTS))
        obs[base + 1] = _norm(busy, float(_MAX_AGENTS))
        total = completed + failed
        # Default 0.5 (neutral) when no observations yet — avoids biasing toward
        # untested configs.
        obs[base + 2] = (completed / total) if total > 0 else 0.5

    # ---- PR-AUTHOR (168-171) ----
    # Open-PR counts and "awaiting review" counts split by author agent type.
    # Awaiting review = open and not approved by GitHub or AgentShore's current-head
    # PASS verdict. Gives the policy a direct signal for "claude PRs are stuck
    # -> spawn codex".
    claude_open = 0
    codex_open = 0
    claude_awaiting = 0
    codex_awaiting = 0
    for pr in state.pull_requests:
        if pr.state != "open":
            continue
        author = pr.author_agent_type
        if author == "claude_code":
            claude_open += 1
            if not pr_is_approved(pr):
                claude_awaiting += 1
        elif author == "codex":
            codex_open += 1
            if not pr_is_approved(pr):
                codex_awaiting += 1
    obs[_S_PR_AUTHOR_CLAUDE_OPEN] = _norm(claude_open, SAT_OPEN_PRS_COUNT)
    obs[_S_PR_AUTHOR_CODEX_OPEN] = _norm(codex_open, SAT_OPEN_PRS_COUNT)
    obs[_S_PR_AUTHOR_CLAUDE_AWAITING] = _norm(claude_awaiting, SAT_OPEN_PRS_COUNT)
    obs[_S_PR_AUTHOR_CODEX_AWAITING] = _norm(codex_awaiting, SAT_OPEN_PRS_COUNT)

    # ---- VELOCITY + BUSY-AGENTS (172-173) ----
    obs[_S_ROLLING_VELOCITY] = float(np.clip(ctx.rolling_velocity, 0.0, 1.0))
    obs[_S_BUSY_AGENTS] = float(min(ctx.busy_agent_count, _MAX_TOTAL_AGENTS)) / _MAX_TOTAL_AGENTS

    # ---- PR REVIEW / MERGE READINESS (174-176) ----
    open_prs = [pr for pr in state.pull_requests if pr.state == "open"]
    n_open = max(1, len(open_prs))
    unreviewed = sum(
        1 for pr in open_prs if pr.head_sha is None or pr.head_sha != pr.last_reviewed_sha
    )
    mergeable_count = sum(1 for pr in open_prs if pr.mergeable == "MERGEABLE")
    obs[_S_FRAC_UNREVIEWED_PRS] = unreviewed / n_open
    obs[_S_FRAC_MERGEABLE_PRS] = mergeable_count / n_open
    obs[_S_INFLIGHT_ISSUES] = (
        min(len(state.in_flight_issues), _MAX_TOTAL_AGENTS) / _MAX_TOTAL_AGENTS
    )

    # ---- EXECUTOR SKIP RATE (177) ----
    # Fraction of recent selection cycles ending in a clean confirm/claim
    # re-pick (live-drift signal). Already a rate in [0.0, 1.0]; just clamp.
    obs[_S_EXECUTOR_SKIP_RATE] = _clamp(ctx.executor_skip_rate_recent_50)

    # ---- PR PRESSURE RATIO (178) ----
    # desktop-8zzy: open_pr_count divided by SAT_OPEN_PRS_COUNT (the same
    # saturation point used for the raw open-prs slot at 56). Lets PPO learn
    # "press harder near the cap" from a normalised ratio rather than
    # inferring it from the raw count. Mirrors the _PR_PRESSURE_BONUS shaping
    # in reward.py — both share max_open_prs = 10.0.
    obs[_S_PR_PRESSURE_RATIO] = _norm(ctx.open_pr_count, SAT_OPEN_PRS_COUNT)

    # ---- SPECIALIZATION (179-244 at NUM_ACTIONS=22) ----
    # Per-tier × per-play-type success rate. Cells aggregate every per-agent
    # snapshot whose ``model_tier`` resolves to one of (small, medium, large)
    # — weighted by total plays per (agent, play_type) cell — so a 10-agent
    # fleet collapses into a 3-slot tier axis. Cells with no observations
    # default to ``_SPEC_NEUTRAL`` (0.5) so PPO doesn't penalize untried tiers.
    obs[_S_SPEC_BLOCK_START:_S_SPEC_BLOCK_END] = _SPEC_NEUTRAL
    if ctx.agent_specialization:
        agent_tier: dict[str, int] = {}
        for a in state.agents:
            tier_idx = _TIER_INDEX.get(a.model_tier or "medium")
            if tier_idx is not None:
                agent_tier[a.agent_id] = tier_idx
        # Aggregate per-(tier, play_type) cells. Each (agent_id, play_type)
        # snapshot contributes ``total`` weighted successful count; we then
        # divide weighted-success / weighted-total at the end.
        tier_play_total: dict[tuple[int, int], int] = {}
        tier_play_success: dict[tuple[int, int], int] = {}
        for cell in ctx.agent_specialization:
            cell_tier = agent_tier.get(cell.agent_id)
            if cell_tier is None:
                continue
            if not isinstance(cell.play_type, PlayType):
                continue  # legacy string play types stay out of the fixed block
            action_idx = PLAY_TO_INDEX[cell.play_type]
            spec_key: tuple[int, int] = (cell_tier, action_idx)
            tier_play_total[spec_key] = tier_play_total.get(spec_key, 0) + cell.total
            tier_play_success[spec_key] = tier_play_success.get(spec_key, 0) + cell.successful
        for (cell_tier, action_idx), total in tier_play_total.items():
            if total <= 0:
                continue
            rate = tier_play_success.get((cell_tier, action_idx), 0) / total
            obs[_S_SPEC_BLOCK_START + cell_tier * NUM_ACTIONS + action_idx] = _clamp(rate)

    # ---- RESERVED (245) — version marker ----
    # A stable per-version constant in [0, 1]. Self-normalizing (always 1.0) so
    # an OBSERVATION_VERSION bump can never feed the policy an out-of-range
    # marker it never saw in training.
    obs[_S_OBS_VERSION] = OBSERVATION_VERSION / float(OBSERVATION_VERSION)

    return obs
