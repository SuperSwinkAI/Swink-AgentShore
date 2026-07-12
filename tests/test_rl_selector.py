"""Tests for rl/selector.py — PPOSelector."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from agentshore.config.models import PolicyMode
from agentshore.plays.base import PlayParams
from agentshore.plays.registry import build_default_registry
from agentshore.rl.action_space import NUM_ACTIONS, PLAY_TO_INDEX
from agentshore.rl.experience import RolloutBuffer
from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.rl.observation import OBSERVATION_DIM
from agentshore.rl.policy import ActorCritic
from agentshore.rl.selector import (
    PPOSelector,
    _is_capacity_wait,
    _mask_reasons_by_play,
    _only_capacity_waiting,
    _PendingStep,
)
from agentshore.rl.training import PPOUpdater
from agentshore.state import OrchestratorState, PlayType, SessionState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(**kwargs: object) -> OrchestratorState:
    plays_since = kwargs.pop("plays_since_last_play_type", None)
    default_success = {PlayType.SEED_PROJECT: True, PlayType.DESIGN_AUDIT: True}
    last_success = kwargs.get("last_play_success_by_type", default_success)
    if plays_since is None:
        plays_since = {PlayType.SEED_PROJECT: 0, PlayType.DESIGN_AUDIT: 0}
    elif (
        isinstance(plays_since, dict)
        and isinstance(last_success, dict)
        and last_success.get(PlayType.SEED_PROJECT) is True
        and PlayType.SEED_PROJECT not in plays_since
    ):
        plays_since = {PlayType.SEED_PROJECT: 0, **plays_since}
    if (
        isinstance(plays_since, dict)
        and isinstance(last_success, dict)
        and PlayType.RUN_QA in plays_since
        and PlayType.RUN_QA not in last_success
    ):
        last_success = {**last_success, PlayType.RUN_QA: True}
    if (
        isinstance(plays_since, dict)
        and isinstance(last_success, dict)
        and last_success.get(PlayType.DESIGN_AUDIT) is True
        and PlayType.DESIGN_AUDIT not in plays_since
    ):
        plays_since = {PlayType.DESIGN_AUDIT: 0, **plays_since}
    base = dict(
        session_id="s1",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
        plays_since_last_play_type=plays_since,
        last_play_success_by_type=last_success,
    )
    base.update(kwargs)
    return OrchestratorState(**base)  # type: ignore[arg-type]


def _agent(agent_id: str = "codex-1"):
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CODEX,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )


def _issue(issue_number: int = 234):
    from agentshore.state import IssueSnapshot

    return IssueSnapshot(
        issue_number=issue_number,
        title=f"Issue {issue_number}",
        state="open",
        priority=None,
        labels=[],
        source=None,
    )


def _mock_metrics() -> MagicMock:
    from agentshore.rl.observation import ObservationContext

    ctx = ObservationContext(
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
    m = MagicMock()
    m.snapshot = AsyncMock(return_value=ctx)
    return m


def _mock_registry(all_met: bool = True) -> MagicMock:
    reg = MagicMock()
    reg.preconditions_met.return_value = all_met
    play = MagicMock()
    play.capability = None
    play.preconditions.return_value = [] if all_met else ["blocked"]
    reg.get.return_value = play
    return reg


def _mock_resolver(params: PlayParams | None = None) -> MagicMock:
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value=params or PlayParams())
    return resolver


def _make_cfg(*, reverse_failsafe_enabled: bool = False) -> MagicMock:
    from agentshore.config import PPOConfig

    cfg = MagicMock()
    cfg.gamma = 0.99
    cfg.learning_rate = 3e-4
    cfg.entropy_coef = 0.01
    cfg.update_every = 8
    cfg.checkpoint_every = 10
    cfg.reverse_failsafe_enabled = reverse_failsafe_enabled
    cfg.reverse_failsafe_after_idle_ticks = 3
    ppo = PPOConfig()
    cfg.ppo = ppo
    return cfg


def _build_selector(
    *,
    all_preconds: bool = True,
    resolver_params: PlayParams | None = None,
    policy_mode: PolicyMode = PolicyMode.LEARNING,
    reverse_failsafe_enabled: bool = False,
) -> PPOSelector:
    policy = ActorCritic()
    buffer = RolloutBuffer()
    updater = PPOUpdater(policy)
    return PPOSelector(
        policy=policy,
        resolver=_mock_resolver(resolver_params or PlayParams()),
        registry=_mock_registry(all_preconds),
        buffer=buffer,
        updater=updater,
        metrics=_mock_metrics(),
        cfg=_make_cfg(reverse_failsafe_enabled=reverse_failsafe_enabled),
        policy_mode=policy_mode,
    )


# ---------------------------------------------------------------------------
# select()
# ---------------------------------------------------------------------------


def test_select_returns_play_type_and_params():
    sel = _build_selector()
    result = asyncio.run(sel.select(_state()))
    assert result is not None  # type-checker guard
    play_type, params = result
    assert isinstance(play_type, PlayType)
    assert isinstance(params, PlayParams)
    # Selecting must mark the selector with pending experience awaiting completion.
    assert sel._pending is not None
    # Action index round-trips: stored pending action maps back to a valid PlayType.
    assert 0 <= sel._pending.action < NUM_ACTIONS


def test_select_all_masked_returns_none():
    sel = _build_selector(all_preconds=False)
    result = asyncio.run(sel.select(_state()))
    assert result is None


def test_select_all_masked_logs_structured_diagnostics():
    sel = _build_selector(all_preconds=False)
    with patch("agentshore.rl.selector._logger") as logger:
        result = asyncio.run(sel.select(_state()))

    assert result is None
    logger.warning.assert_called_once()
    _, kwargs = logger.warning.call_args
    assert kwargs["idle_agents"] == 0
    assert kwargs["open_issues"] == 0
    assert "top_mask_reasons" in kwargs


def _mask_reason(text: str, source: MaskSource) -> MaskReason:
    return MaskReason(text=text, classification=MaskClassification.TRANSIENT, source=source)


def test_mask_reasons_by_play_is_complete_and_sorted():
    # The full per-play map keeps EVERY masked play (no most_common(5) truncation)
    # and preserves the play->reason mapping the Counter(values) collapse discards,
    # so a wedge's actual blockers (issue_pickup/code_review/merge_pr) stay visible.
    reasons = {
        PlayType.MERGE_PR: MaskReason(
            text="dirty trunk", classification=MaskClassification.HARD, source=MaskSource.CANDIDATE
        ),
        PlayType.CODE_REVIEW: MaskReason(
            text="no reviewable PR candidate",
            classification=MaskClassification.HARD,
            source=MaskSource.CANDIDATE,
        ),
        PlayType.ISSUE_PICKUP: MaskReason(
            text="too many open PRs",
            classification=MaskClassification.TRANSIENT,
            source=MaskSource.CANDIDATE,
        ),
    }
    mapping = _mask_reasons_by_play(reasons)
    assert set(mapping) == {"merge_pr", "code_review", "issue_pickup"}
    assert mapping["code_review"] == "no reviewable PR candidate [hard/candidate]"
    assert mapping["issue_pickup"] == "too many open PRs [transient/candidate]"
    # Deterministic, greppable ordering (sorted by play value).
    assert list(mapping) == sorted(mapping)


def test_masked_plays_log_field_dedups_identical_maps():
    # A frozen all-masked wedge re-logs its diagnostic every selector tick; the
    # verbose map must appear once, then collapse to an "unchanged" sentinel so
    # the log isn't flooded with identical ~20-entry dumps.
    sel = _build_selector()
    reasons = {
        PlayType.CODE_REVIEW: MaskReason(
            text="x", classification=MaskClassification.HARD, source=MaskSource.CANDIDATE
        ),
    }
    first = sel._masked_plays_log_field(reasons)
    assert "masked_plays" in first
    assert "masked_plays_unchanged" not in first

    # Identical map on the next tick → compact sentinel, no re-dump.
    second = sel._masked_plays_log_field(dict(reasons))
    assert second == {"masked_plays_unchanged": True}

    # A changed map re-emits the full field.
    reasons[PlayType.MERGE_PR] = MaskReason(
        text="y", classification=MaskClassification.HARD, source=MaskSource.CANDIDATE
    )
    third = sel._masked_plays_log_field(reasons)
    assert "masked_plays" in third

    # A dispatched play clears the digest so the next wedge re-emits fresh.
    sel._last_masked_plays_digest = None
    fourth = sel._masked_plays_log_field(reasons)
    assert "masked_plays" in fourth


def test_capacity_only_reasons_include_allowed_tier_waits():
    # Eligibility reasons count as capacity waits; RESERVED is an actionable ignore.
    assert _only_capacity_waiting(
        [
            {
                "reason": MaskReason(
                    text="No IDLE agent of allowed tier (large)",
                    classification=MaskClassification.TRANSIENT,
                    source=MaskSource.ELIGIBILITY,
                ),
                "count": 1,
            },
            {
                "reason": MaskReason(
                    text="Reserved action slot",
                    classification=MaskClassification.HARD,
                    source=MaskSource.RESERVED,
                ),
                "count": 2,
            },
        ]
    )


def test_capacity_only_reasons_spawn_cooldown():
    # Instantiate-cooldown reasons (SPAWN source) count as capacity waits.
    assert _only_capacity_waiting(
        [
            {
                "reason": MaskReason(
                    text="instantiate cooldown (1/3 plays since last)",
                    classification=MaskClassification.INDEFINITE_WAIT,
                    source=MaskSource.SPAWN,
                ),
                "count": 3,
            }
        ]
    )


def test_capacity_only_reasons_non_capacity_returns_false():
    # A reason from a non-capacity source (e.g. CONTROL) causes the predicate to
    # return False even if other capacity-wait reasons are present.
    assert not _only_capacity_waiting(
        [
            {
                "reason": MaskReason(
                    text="No IDLE agents",
                    classification=MaskClassification.TRANSIENT,
                    source=MaskSource.ELIGIBILITY,
                ),
                "count": 2,
            },
            {
                "reason": MaskReason(
                    text="session draining",
                    classification=MaskClassification.INDEFINITE_WAIT,
                    source=MaskSource.DRAIN,
                ),
                "count": 1,
            },
        ]
    )


def test_is_capacity_wait_eligibility_and_spawn():
    assert _is_capacity_wait(
        MaskReason(
            text="No IDLE agents",
            classification=MaskClassification.TRANSIENT,
            source=MaskSource.ELIGIBILITY,
        )
    )
    assert _is_capacity_wait(
        MaskReason(
            text="instantiate cooldown (0/3)",
            classification=MaskClassification.INDEFINITE_WAIT,
            source=MaskSource.SPAWN,
        )
    )
    assert not _is_capacity_wait(
        MaskReason(
            text="session draining",
            classification=MaskClassification.INDEFINITE_WAIT,
            source=MaskSource.DRAIN,
        )
    )


def test_select_logs_resolver_exhaustion_when_masked_actions_cannot_resolve():
    sel = _build_selector()
    sel._resolver.resolve = AsyncMock(return_value=None)

    with patch("agentshore.rl.selector._logger") as logger:
        result = asyncio.run(sel.select(_state(open_issues=[_issue()])))

    assert result is None
    calls = [
        call
        for call in logger.warning.call_args_list
        if call.args == ("ppo_selector.resolver_exhausted",)
    ]
    assert calls
    _, kwargs = calls[-1]
    assert kwargs["attempted_plays"]
    assert kwargs["mask_allowed_plays"]
    assert kwargs["github_open_issues"] == 1
    assert "top_mask_reasons" in kwargs


def test_select_logs_terminal_no_work_diagnostic():
    """The selector still surfaces the terminal-no-work diagnostic.

    Item 1 keeps the diagnostic and the ``compute_terminal_no_work_decision``
    helper, but the mask no longer one-hot-forces a play — the PPO chooses
    among whatever is genuinely valid. This asserts only the retained
    behaviour: the diagnostic is emitted when no workable work remains.
    """
    graph = MagicMock()
    graph.has_epics = True
    graph.global_closure_ratio = 0.0
    graph.tasks_total = 0
    graph.tasks_ready = 0
    graph.tasks_blocked = 0
    sel = _build_selector(all_preconds=False)

    with patch("agentshore.rl.selector._logger") as logger:
        asyncio.run(
            sel.select(_state(graph=graph, plays_since_last_play_type={PlayType.RUN_QA: 0}))
        )

    terminal_calls = [
        call
        for call in logger.info.call_args_list
        if call.args[0] == "ppo_selector.terminal_no_work"
    ]
    assert terminal_calls
    _, kwargs = terminal_calls[0]
    assert kwargs["terminal_reason"] == "no_workable_work_remaining"
    assert kwargs["workable_issues"] == 0
    assert kwargs["actionable_pr_work"] == 0


def test_select_reverse_failsafe_disabled_by_default_returns_none():
    sel = _build_selector(
        all_preconds=False,
        resolver_params=PlayParams(issue_number=234),
    )
    sel._policy.act = MagicMock(  # type: ignore[method-assign]
        return_value=(PLAY_TO_INDEX[PlayType.ISSUE_PICKUP], -0.1, 0.0)
    )
    state = _state(open_issues=[_issue()], agents=[_agent()])

    with patch("agentshore.rl.selector._logger"):
        result = asyncio.run(sel.select(state))

    assert result is None
    sel._policy.act.assert_not_called()


def test_select_auto_reverse_failsafe_after_idle_all_masked_ticks():
    """With reverse_failsafe_enabled=True the manual path fires on the first
    all-masked tick (no 3-tick wait needed).  The 3-tick accumulation behaviour
    is an internal property of _auto_reverse_failsafe_should_unmask, tested
    separately by the direct-method tests."""
    sel = _build_selector(
        all_preconds=False,
        resolver_params=PlayParams(issue_number=234),
        reverse_failsafe_enabled=True,
    )
    sel._policy.act = MagicMock(  # type: ignore[method-assign]
        return_value=(PLAY_TO_INDEX[PlayType.ISSUE_PICKUP], -0.1, 0.0)
    )
    state = _state(open_issues=[_issue()], agents=[_agent()])

    with patch("agentshore.rl.selector._logger"):
        result = asyncio.run(sel.select(state))

    assert result is not None
    play_type, params = result
    assert play_type == PlayType.ISSUE_PICKUP
    assert params.extras["reverse_failsafe"] is True
    assert params.extras["reverse_failsafe_bypassed_preconditions"] is True


def test_select_auto_reverse_failsafe_disabled_by_default():
    """reverse_failsafe_enabled defaults to False — the auto path must not fire
    even after the idle-tick threshold is exceeded (#296)."""
    sel = _build_selector(
        all_preconds=False,
        resolver_params=PlayParams(issue_number=234),
    )
    sel._policy.act = MagicMock(  # type: ignore[method-assign]
        return_value=(PLAY_TO_INDEX[PlayType.ISSUE_PICKUP], -0.1, 0.0)
    )
    state = _state(open_issues=[_issue()], agents=[_agent()])

    with patch("agentshore.rl.selector._logger"):
        assert asyncio.run(sel.select(state)) is None  # tick 1
        assert asyncio.run(sel.select(state)) is None  # tick 2
        assert asyncio.run(sel.select(state)) is None  # tick 3 — no fire when disabled
        assert asyncio.run(sel.select(state)) is None  # tick 4 — still no fire


def test_auto_reverse_failsafe_counts_lifecycle_only_mask_as_no_work():
    """#166: a mask leaving only lifecycle plays (END_AGENT here) selectable still
    means 'no dispatchable work' — the failsafe counter must accumulate so it can
    arm and lift END_SESSION, instead of resetting every time a reap slips
    through."""
    sel = _build_selector(all_preconds=False, resolver_params=PlayParams(issue_number=234))
    state = _state(open_issues=[_issue()], agents=[_agent()])  # all agents idle
    mask = np.zeros(NUM_ACTIONS, dtype=bool)
    mask[PLAY_TO_INDEX[PlayType.END_AGENT]] = True  # only a lifecycle play selectable

    assert sel._auto_reverse_failsafe_should_unmask(state, mask) is False  # tick 1
    assert sel._auto_reverse_failsafe_should_unmask(state, mask) is False  # tick 2
    assert sel._auto_reverse_failsafe_should_unmask(state, mask) is True  # tick 3 → arms


def test_auto_reverse_failsafe_resets_on_real_selectable_play():
    """A genuinely selectable work play resets the idle counter (the failsafe must
    not arm while real work is dispatchable)."""
    sel = _build_selector(all_preconds=False, resolver_params=PlayParams(issue_number=234))
    state = _state(open_issues=[_issue()], agents=[_agent()])
    lifecycle_mask = np.zeros(NUM_ACTIONS, dtype=bool)
    lifecycle_mask[PLAY_TO_INDEX[PlayType.END_AGENT]] = True
    work_mask = np.zeros(NUM_ACTIONS, dtype=bool)
    work_mask[PLAY_TO_INDEX[PlayType.ISSUE_PICKUP]] = True

    assert sel._auto_reverse_failsafe_should_unmask(state, lifecycle_mask) is False
    assert sel._auto_reverse_failsafe_should_unmask(state, work_mask) is False  # resets to 0
    # Counter restarted: a single lifecycle tick is well below threshold.
    assert sel._auto_reverse_failsafe_should_unmask(state, lifecycle_mask) is False


def test_select_auto_reverse_failsafe_opens_dead_end_controls():
    sel = _build_selector(
        all_preconds=False,
        resolver_params=PlayParams(),
        reverse_failsafe_enabled=True,
    )
    sel._resolver.resolve = AsyncMock(return_value=None)
    sel._policy.act = MagicMock(  # type: ignore[method-assign]
        side_effect=[
            (PLAY_TO_INDEX[PlayType.END_AGENT], -0.1, 0.0),
        ]
    )
    state = _state(open_issues=[_issue()], agents=[_agent()])
    sel._no_available_play_ticks = 2

    with patch("agentshore.rl.selector._logger"):
        result = asyncio.run(sel.select(state))

    assert result is not None
    play_type, params = result
    assert play_type == PlayType.END_AGENT
    assert params.agent_id == "codex-1"
    assert params.bypass_preconditions is True
    assert params.extras["reverse_failsafe"] is True


def test_auto_failsafe_counter_advances_through_in_flight_plays():
    """In-flight plays must not reset the auto-failsafe counter.

    Production session 08a948ed-2026-05-28 hit a deadlock where one play
    hung in flight for 20+ minutes while every other action was masked
    and every agent was IDLE. The previous reset-on-in-flight logic kept
    the failsafe permanently disabled in exactly that scenario.
    """
    import numpy as np

    sel = _build_selector(all_preconds=False)
    state = _state(
        open_issues=[_issue()],
        agents=[_agent()],
        in_flight_plays=[PlayType.CALIBRATE_ALIGNMENT],
    )
    fully_masked = np.zeros(22, dtype=bool)

    # First tick: counter goes from 0 to 1; threshold (3) not yet hit.
    assert sel._auto_reverse_failsafe_should_unmask(state, fully_masked) is False
    assert sel._no_available_play_ticks == 1

    # Second tick: still in flight, still all-masked, still idle — keep counting.
    assert sel._auto_reverse_failsafe_should_unmask(state, fully_masked) is False
    assert sel._no_available_play_ticks == 2

    # Third tick: threshold met, failsafe opens.
    assert sel._auto_reverse_failsafe_should_unmask(state, fully_masked) is True
    assert sel._no_available_play_ticks == 3


def test_auto_failsafe_counter_resets_when_an_agent_is_busy():
    """Busy agents are real work in progress — reset and wait."""
    import numpy as np

    from agentshore.state import AgentSnapshot, AgentStatus, AgentType

    busy_agent = AgentSnapshot(
        agent_id="codex-1",
        agent_type=AgentType.CODEX,
        status=AgentStatus.BUSY,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
    )
    sel = _build_selector(all_preconds=False)
    sel._no_available_play_ticks = 2  # primed
    state = _state(open_issues=[_issue()], agents=[busy_agent])
    fully_masked = np.zeros(22, dtype=bool)

    assert sel._auto_reverse_failsafe_should_unmask(state, fully_masked) is False
    assert sel._no_available_play_ticks == 0


def _agent_with(agent_id: str, status, *, current_play_type=None):
    from agentshore.state import AgentSnapshot, AgentType

    return AgentSnapshot(
        agent_id=agent_id,
        agent_type=AgentType.CODEX,
        status=status,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        current_play_type=current_play_type,
    )


def test_auto_failsafe_counter_advances_when_only_agent_is_error():
    """An ERROR agent is stuck, not working — it must not pin the counter at zero.

    A transient API failure (e.g. 529 Overloaded) can leave an agent ERROR while
    the rest of the fleet idles with all work masked. Counting ERROR as busy kept
    the failsafe permanently disarmed, so END_SESSION never lifted and the session
    could not wind down.
    """
    import numpy as np

    from agentshore.state import AgentStatus

    sel = _build_selector(all_preconds=False)
    state = _state(
        open_issues=[_issue()],
        agents=[_agent_with("codex-1", AgentStatus.ERROR)],
    )
    fully_masked = np.zeros(22, dtype=bool)

    assert sel._auto_reverse_failsafe_should_unmask(state, fully_masked) is False  # tick 1
    assert sel._auto_reverse_failsafe_should_unmask(state, fully_masked) is False  # tick 2
    assert sel._auto_reverse_failsafe_should_unmask(state, fully_masked) is True  # tick 3 → arms


def test_auto_failsafe_counter_advances_when_agents_idle_and_error_mixed():
    """A mix of IDLE + ERROR agents is still a quiescent fleet (no real work)."""
    import numpy as np

    from agentshore.state import AgentStatus

    sel = _build_selector(all_preconds=False)
    state = _state(
        open_issues=[_issue()],
        agents=[
            _agent_with("idle-1", AgentStatus.IDLE),
            _agent_with("error-1", AgentStatus.ERROR),
        ],
    )
    fully_masked = np.zeros(22, dtype=bool)

    assert sel._auto_reverse_failsafe_should_unmask(state, fully_masked) is False  # tick 1
    assert sel._auto_reverse_failsafe_should_unmask(state, fully_masked) is False  # tick 2
    assert sel._auto_reverse_failsafe_should_unmask(state, fully_masked) is True  # tick 3 → arms


def test_auto_failsafe_counter_advances_when_busy_agent_is_on_a_break():
    """An agent BUSY inside TAKE_BREAK is sleeping, not progressing work."""
    import numpy as np

    from agentshore.state import AgentStatus, PlayType

    sel = _build_selector(all_preconds=False)
    state = _state(
        open_issues=[_issue()],
        agents=[
            _agent_with("codex-1", AgentStatus.BUSY, current_play_type=PlayType.TAKE_BREAK),
        ],
    )
    fully_masked = np.zeros(22, dtype=bool)

    assert sel._auto_reverse_failsafe_should_unmask(state, fully_masked) is False  # tick 1
    assert sel._auto_reverse_failsafe_should_unmask(state, fully_masked) is False  # tick 2
    assert sel._auto_reverse_failsafe_should_unmask(state, fully_masked) is True  # tick 3 → arms


def test_auto_failsafe_counter_resets_when_busy_agent_does_real_work():
    """A BUSY agent on a non-break play is genuine progress — reset and wait.

    Guards the boundary of the ERROR/break carve-out: a real in-flight play must
    still block the failsafe even when another agent is ERROR.
    """
    import numpy as np

    from agentshore.state import AgentStatus, PlayType

    sel = _build_selector(all_preconds=False)
    sel._no_available_play_ticks = 2  # primed
    state = _state(
        open_issues=[_issue()],
        agents=[
            _agent_with("worker-1", AgentStatus.BUSY, current_play_type=PlayType.ISSUE_PICKUP),
            _agent_with("error-1", AgentStatus.ERROR),
        ],
    )
    fully_masked = np.zeros(22, dtype=bool)

    assert sel._auto_reverse_failsafe_should_unmask(state, fully_masked) is False
    assert sel._no_available_play_ticks == 0


def test_select_reverse_failsafe_bypasses_policy_backpressure_when_enabled():
    sel = _build_selector(
        all_preconds=False,
        resolver_params=PlayParams(issue_number=234),
        reverse_failsafe_enabled=True,
    )
    sel._policy.act = MagicMock(  # type: ignore[method-assign]
        return_value=(PLAY_TO_INDEX[PlayType.ISSUE_PICKUP], -0.1, 0.0)
    )
    state = _state(open_issues=[_issue()], agents=[_agent()])

    with patch("agentshore.rl.selector._logger"):
        result = asyncio.run(sel.select(state))

    assert result is not None
    play_type, params = result
    assert play_type == PlayType.ISSUE_PICKUP
    assert params.bypass_preconditions is True
    assert params.extras["reverse_failsafe"] is True
    assert params.extras["reverse_failsafe_bypassed_preconditions"] is True


def test_select_sets_pending():
    sel = _build_selector()
    asyncio.run(sel.select(_state()))
    assert sel._pending is not None  # type-checker guard
    assert isinstance(sel._pending, _PendingStep)
    # The pending step must capture a valid action and matching observation/mask shapes.
    assert 0 <= sel._pending.action < NUM_ACTIONS
    assert sel._pending.obs.shape == (OBSERVATION_DIM,)
    assert sel._pending.mask.shape == (NUM_ACTIONS,)
    # log_prob comes from a categorical sample of a normalised distribution;
    # it must be finite and non-positive.
    assert np.isfinite(sel._pending.log_prob)
    assert sel._pending.log_prob <= 0.0


def test_audit_replay_mode_reproducible():
    """Same policy, same obs -> same action in audit-replay mode."""
    sel = _build_selector(policy_mode=PolicyMode.AUDIT_REPLAY)
    state = _state()
    result1 = asyncio.run(sel.select(state))
    # Reset pending between calls
    sel._pending = None
    result2 = asyncio.run(sel.select(state))
    assert result1 is not None and result2 is not None
    assert result1[0] == result2[0]


def test_resolver_none_retries_and_eventually_returns_none():
    """If resolver always returns None, selector exhausts retries → None."""
    policy = ActorCritic()
    buffer = RolloutBuffer()
    updater = PPOUpdater(policy)

    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value=None)

    sel = PPOSelector(
        policy=policy,
        resolver=resolver,
        registry=_mock_registry(True),
        buffer=buffer,
        updater=updater,
        metrics=_mock_metrics(),
        cfg=_make_cfg(),
    )
    result = asyncio.run(sel.select(_state()))
    assert result is None


def test_resolver_none_on_first_retry_succeeds():
    """Resolver fails for one action but succeeds on second try."""
    policy = ActorCritic()
    buffer = RolloutBuffer()
    updater = PPOUpdater(policy)

    call_count = 0

    async def _resolve(pt: PlayType, state: OrchestratorState, **kw: object) -> PlayParams | None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None  # First action fails
        return PlayParams()

    resolver = MagicMock()
    resolver.resolve = _resolve

    sel = PPOSelector(
        policy=policy,
        resolver=resolver,
        registry=_mock_registry(True),
        buffer=buffer,
        updater=updater,
        metrics=_mock_metrics(),
        cfg=_make_cfg(),
    )
    result = asyncio.run(sel.select(_state()))
    assert result is not None  # type-checker guard
    assert call_count == 2
    play_type, params = result
    assert isinstance(play_type, PlayType)
    assert isinstance(params, PlayParams)
    # A successful retry must leave the selector with a pending experience step.
    assert sel._pending is not None


# ---------------------------------------------------------------------------
# on_play_completed / experience accumulation
# ---------------------------------------------------------------------------


def test_on_play_completed_adds_step_to_buffer():
    sel = _build_selector()
    asyncio.run(sel.select(_state()))
    assert sel._pending is not None

    asyncio.run(
        sel.on_play_completed(
            state_before=_state(),
            next_state=_state(),
            reward=1.0,
            done=False,
        )
    )
    assert len(sel.buffer) == 1
    assert sel._pending is None


def test_on_play_completed_without_select_is_noop():
    sel = _build_selector()
    assert sel._pending is None
    asyncio.run(
        sel.on_play_completed(
            state_before=_state(),
            next_state=_state(),
            reward=0.0,
            done=False,
        )
    )
    assert len(sel.buffer) == 0


def test_experience_round_trip_buffer_grows():
    sel = _build_selector()
    for i in range(3):
        asyncio.run(sel.select(_state()))
        asyncio.run(
            sel.on_play_completed(
                state_before=_state(),
                next_state=_state(),
                reward=float(i),
                done=False,
            )
        )
    assert len(sel.buffer) == 3


# ---------------------------------------------------------------------------
# should_update / should_checkpoint
# ---------------------------------------------------------------------------


def test_should_update_false_when_buffer_small():
    sel = _build_selector()
    assert not sel.should_update()


def test_should_update_true_when_buffer_full():
    sel = _build_selector()
    for _ in range(8):  # update_every = 8
        sel._buffer.add(
            __import__("agentshore.rl.experience", fromlist=["Step"]).Step(
                state=np.zeros(OBSERVATION_DIM, dtype=np.float32),
                action=0,
                reward=0.0,
                next_state=np.zeros(OBSERVATION_DIM, dtype=np.float32),
                done=False,
                log_prob=-1.0,
                value=0.0,
                mask=np.ones(NUM_ACTIONS, dtype=bool),
            )
        )
    assert sel.should_update()


def test_should_update_false_in_audit_replay_mode():
    sel = _build_selector(policy_mode=PolicyMode.AUDIT_REPLAY)
    for _ in range(8):
        sel._buffer.add(
            __import__("agentshore.rl.experience", fromlist=["Step"]).Step(
                state=np.zeros(OBSERVATION_DIM, dtype=np.float32),
                action=0,
                reward=0.0,
                next_state=np.zeros(OBSERVATION_DIM, dtype=np.float32),
                done=False,
                log_prob=-1.0,
                value=0.0,
                mask=np.ones(NUM_ACTIONS, dtype=bool),
            )
        )
    assert not sel.should_update()


def test_should_checkpoint():
    sel = _build_selector()
    assert sel.should_checkpoint(10)
    assert not sel.should_checkpoint(11)
    assert sel.should_checkpoint(20)


def test_should_checkpoint_false_in_audit_replay_mode():
    sel = _build_selector(policy_mode=PolicyMode.AUDIT_REPLAY)
    assert not sel.should_checkpoint(10)


# ---------------------------------------------------------------------------
# update_policy
# ---------------------------------------------------------------------------


def test_update_policy_clears_buffer():
    sel = _build_selector()
    from agentshore.rl.experience import Step

    for _ in range(4):
        sel._buffer.add(
            Step(
                state=np.zeros(OBSERVATION_DIM, dtype=np.float32),
                action=0,
                reward=1.0,
                next_state=np.zeros(OBSERVATION_DIM, dtype=np.float32),
                done=False,
                log_prob=-1.0,
                value=0.0,
                mask=np.ones(NUM_ACTIONS, dtype=bool),
            )
        )
    asyncio.run(sel.update_policy(next_state_value=0.0))
    assert len(sel.buffer) == 0


def test_update_policy_audit_replay_is_noop():
    sel = _build_selector(policy_mode=PolicyMode.AUDIT_REPLAY)
    stats = asyncio.run(sel.update_policy(next_state_value=0.0))
    assert stats.n_epochs == 0


# ---------------------------------------------------------------------------
# from_cold_start / load factory
# ---------------------------------------------------------------------------


def test_from_cold_start_builds_selector():
    sel = PPOSelector.from_cold_start(
        resolver=_mock_resolver(),
        registry=_mock_registry(),
        metrics=_mock_metrics(),
        cfg=_make_cfg(),
    )
    assert isinstance(sel, PPOSelector)
    result = asyncio.run(sel.select(_state()))
    assert result is not None


def test_load_factory(tmp_path: Path):
    policy = ActorCritic()
    weights = tmp_path / "policy.pt"
    policy.save(weights)

    sel = asyncio.run(
        PPOSelector.load(
            weights_path=weights,
            resolver=_mock_resolver(),
            registry=_mock_registry(),
            metrics=_mock_metrics(),
            cfg=_make_cfg(),
        )
    )
    assert isinstance(sel, PPOSelector)


def test_load_runs_torch_load_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#247: the multi-second ActorCritic.load (torch.load) must run via
    asyncio.to_thread so the TUI startup checklist keeps repainting instead of
    freezing on a partial frame while the policy loads."""
    policy = ActorCritic()
    weights = tmp_path / "policy.pt"
    policy.save(weights)

    threaded: list[str] = []
    real_to_thread = asyncio.to_thread

    async def recording_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        threaded.append(getattr(func, "__name__", repr(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", recording_to_thread)

    sel = asyncio.run(
        PPOSelector.load(
            weights_path=weights,
            resolver=_mock_resolver(),
            registry=_mock_registry(),
            metrics=_mock_metrics(),
            cfg=_make_cfg(),
        )
    )
    assert isinstance(sel, PPOSelector)
    # ActorCritic.load was scheduled on a worker thread, not run on the loop.
    assert "load" in threaded


# ---------------------------------------------------------------------------
# consume_pending
# ---------------------------------------------------------------------------


def test_consume_pending_returns_and_clears():
    sel = _build_selector()
    asyncio.run(sel.select(_state()))
    pending = sel.consume_pending()
    assert pending is not None
    assert isinstance(pending, _PendingStep)
    assert sel._pending is None


def test_consume_pending_without_select_returns_none():
    sel = _build_selector()
    assert sel.consume_pending() is None


# ---------------------------------------------------------------------------
# C2 — _reload_shared_weights safe reload (versioned path)
# ---------------------------------------------------------------------------


def test_reload_shared_weights_picks_versioned_path(tmp_path: Path) -> None:
    """_reload_shared_weights loads the version-tagged canonical, ignores policy.pt."""
    import unittest.mock as mock

    from agentshore.rl.checkpoint_store import canonical_weights_filename

    sel = _build_selector()

    weights_dir = tmp_path / "weights"
    weights_dir.mkdir(parents=True, exist_ok=True)

    # Save a real compatible checkpoint at the versioned canonical path.
    versioned_path = weights_dir / canonical_weights_filename()
    sel._policy.save(versioned_path)

    # Ensure the legacy policy.pt does NOT exist (we want to confirm it is ignored).
    legacy_path = weights_dir / "policy.pt"
    assert not legacy_path.exists()

    with mock.patch("agentshore.rl.selector._GLOBAL_WEIGHTS_DIR", weights_dir):
        sel._reload_shared_weights()  # should succeed silently, no exception


def test_reload_shared_weights_skips_incompatible(tmp_path: Path) -> None:
    """_reload_shared_weights skips checkpoints that raise IncompatibleCheckpointError."""
    import unittest.mock as mock

    import torch

    from agentshore.rl.action_space import ACTION_SPACE_VERSION
    from agentshore.rl.checkpoint_store import canonical_weights_filename
    from agentshore.rl.config_head import POLICY_VERSION

    sel = _build_selector()
    original_state = {k: v.clone() for k, v in sel._policy.state_dict().items()}

    weights_dir = tmp_path / "weights"
    weights_dir.mkdir(parents=True, exist_ok=True)

    stale_path = weights_dir / canonical_weights_filename()
    # Write a payload with a wrong policy_version — ActorCritic.load raises
    # IncompatibleCheckpointError on action_space_version mismatch first, so
    # use a wrong action_space_version to guarantee the error path.
    torch.save(
        {
            "state_dict": sel._policy.state_dict(),
            "policy_version": POLICY_VERSION + 99,
            "action_space_version": ACTION_SPACE_VERSION + 99,
            "observation_version": 999,
            "obs_dim": 9999,
            "num_actions": 20,
            "num_configs": 0,
        },
        stale_path,
    )

    with mock.patch("agentshore.rl.selector._GLOBAL_WEIGHTS_DIR", weights_dir):
        sel._reload_shared_weights()  # should log warning, not raise, not load weights

    # Policy weights unchanged — reload was skipped.
    for key, original_tensor in original_state.items():
        assert torch.equal(sel._policy.state_dict()[key], original_tensor), (
            f"Weight {key} changed after incompatible reload"
        )


# ---------------------------------------------------------------------------
# C3 — cleanup_stale_canonical_weights
# ---------------------------------------------------------------------------


def test_cleanup_stale_canonical_renames(tmp_path: Path) -> None:
    """cleanup_stale_canonical_weights renames incompatible policy.pt to policy_legacy_v*.pt."""
    import torch

    from agentshore.rl.action_space import ACTION_SPACE_VERSION
    from agentshore.rl.selector import cleanup_stale_canonical_weights

    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()

    legacy_path = weights_dir / "policy.pt"
    # Write a payload that will fail ActorCritic.load (wrong action_space_version).
    torch.save(
        {
            "state_dict": {},
            "policy_version": 0,
            "action_space_version": ACTION_SPACE_VERSION + 99,
            "observation_version": 1,
            "obs_dim": 10,
            "num_actions": 5,
            "num_configs": 0,
        },
        legacy_path,
    )

    cleanup_stale_canonical_weights(weights_dir)

    # Original policy.pt is gone (renamed).
    assert not legacy_path.exists()
    # A legacy-named file now exists.
    legacy_files = list(weights_dir.glob("policy_legacy_*.pt"))
    assert len(legacy_files) == 1


def test_cleanup_stale_canonical_noop_if_compatible(tmp_path: Path) -> None:
    """cleanup_stale_canonical_weights leaves a compatible policy.pt untouched."""
    from agentshore.rl.selector import cleanup_stale_canonical_weights

    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()

    # Save a real, compatible checkpoint as policy.pt.
    sel = _build_selector()
    compatible_path = weights_dir / "policy.pt"
    sel._policy.save(compatible_path)

    cleanup_stale_canonical_weights(weights_dir)

    # Compatible file must remain; no legacy rename should occur.
    assert compatible_path.exists()
    assert not list(weights_dir.glob("policy_legacy_*.pt"))


def test_cleanup_stale_canonical_noop_if_no_file(tmp_path: Path) -> None:
    """cleanup_stale_canonical_weights is a no-op when policy.pt does not exist."""
    from agentshore.rl.selector import cleanup_stale_canonical_weights

    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()

    # Should not raise and should not create any files.
    cleanup_stale_canonical_weights(weights_dir)

    assert not list(weights_dir.glob("*.pt"))


def test_cleanup_stale_canonical_uses_module_logger_for_rename(tmp_path: Path) -> None:
    """cleanup_stale_canonical_weights logs rename events via module-level _logger."""
    import unittest.mock as mock

    import torch

    from agentshore.rl.action_space import ACTION_SPACE_VERSION
    from agentshore.rl.selector import cleanup_stale_canonical_weights

    weights_dir = tmp_path / "weights"
    weights_dir.mkdir()

    legacy_path = weights_dir / "policy.pt"
    torch.save(
        {
            "state_dict": {},
            "policy_version": 0,
            "action_space_version": ACTION_SPACE_VERSION + 99,
            "observation_version": 1,
            "obs_dim": 10,
            "num_actions": 5,
            "num_configs": 0,
        },
        legacy_path,
    )

    # The checkpoint lifecycle now lives in checkpoint_store; it logs via that
    # module's logger (selector re-exports the function unchanged).
    logger = mock.Mock()
    with mock.patch("agentshore.rl.checkpoint_store._logger", logger):
        cleanup_stale_canonical_weights(weights_dir)

    logger.warning.assert_any_call(
        "stale_canonical_checkpoint_renamed",
        from_path=str(legacy_path),
        to_path=mock.ANY,
    )


# ---------------------------------------------------------------------------
# Parallel-dispatch drained-pool clean re-pick
# ---------------------------------------------------------------------------


def _parallel_state():
    """One eligible write_implementation_plan issue + one mergeable PR."""
    from agentshore.state import AgentSnapshot, AgentStatus, AgentType, PullRequestSnapshot

    agent = AgentSnapshot(
        agent_id="agent-1",
        agent_type=AgentType.CLAUDE_CODE,
        status=AgentStatus.IDLE,
        context_size=0,
        total_cost=0.0,
        total_tokens=0,
        tasks_completed=0,
        tasks_failed=0,
        model_tier="large",
        github_identity="reviewer",
    )
    pr = PullRequestSnapshot(
        pr_number=50,
        title="PR 50",
        state="open",
        branch="feature/50",
        issue_number=None,
        labels=[],
        review_decision="APPROVED",
        status_check_summary=None,
        is_draft=False,
        blocked=False,
        blocked_reasons=[],
        github_author="author",
        mergeable="MERGEABLE",
        head_sha="abc",
        last_reviewed_sha="abc",
        last_review_status="PASS",
    )
    return _state(
        agents=[agent],
        open_issues=[_issue(234)],
        pull_requests=[pr],
        target_branch="main",
    )


def test_parallel_dispatch_drained_pool_clean_repick():
    """A lost write_impl work-claim CAS cleanly re-picks the still-valid merge_pr.

    Setup mirrors a parallel session draining the pool: write_implementation_plan
    is snapshot-eligible (issue #234) and merge_pr is eligible (PR #50). The
    policy picks write_impl first; confirm() passes (no live drift), but the
    resolver loses the work-claim CAS to a sibling and returns None. That is a
    clean re-pick — re-mask the action, resample — and the policy then picks the
    still-valid merge_pr, which dispatches.

    Asserts:
      * the dispatched play is the re-picked MERGE_PR (or a clean idle None,
        never write_impl),
      * exactly one PPO step is pending (the dispatched play only — the lost
        write_impl contributes no _pending, hence no RL experience sample),
      * no skip:* outcome is produced by the selector (it returns a play, not a
        skip row — the orchestrator writes rows, and a clean re-pick never asks
        it to),
      * a clean-re-pick telemetry event is logged and counted.
    """
    from agentshore.rl.action_space import PLAY_TO_INDEX

    policy = ActorCritic()
    buffer = RolloutBuffer()
    updater = PPOUpdater(policy)

    wip_idx = PLAY_TO_INDEX[PlayType.WRITE_IMPLEMENTATION_PLAN]
    merge_idx = PLAY_TO_INDEX[PlayType.MERGE_PR]

    # Force the play head: write_impl first, then merge_pr. (action, log_prob, value)
    act_calls = {"n": 0}

    def _act(obs, mask, *, greedy):  # noqa: ANN001
        act_calls["n"] += 1
        idx = wip_idx if act_calls["n"] == 1 else merge_idx
        # Respect the running mask: once write_impl is re-masked it must be off.
        assert bool(mask[idx]), f"forced action {idx} must be unmasked on call {act_calls['n']}"
        return idx, -0.5, 0.0

    policy.act = _act  # type: ignore[method-assign]

    # Resolver: lose the CAS for write_impl (None), succeed for merge_pr.
    async def _resolve(pt, state, **kw):  # noqa: ANN001
        if pt == PlayType.WRITE_IMPLEMENTATION_PLAN:
            return None  # work-claim CAS lost to a sibling
        return PlayParams(pr_number=50)

    resolver = MagicMock()
    resolver.resolve = _resolve
    resolver.project_path = None  # no live-graph loader; confirm uses the snapshot
    resolver.release_claim = AsyncMock()

    sel = PPOSelector(
        policy=policy,
        resolver=resolver,
        registry=build_default_registry(),
        buffer=buffer,
        updater=updater,
        metrics=_mock_metrics(),
        cfg=_make_cfg(),
    )

    # confirm() runs AFTER resolve+claim on the resolved target; isolate the
    # claim-lost path (write_impl resolve→None) by accepting any confirmed target
    # so the re-picked merge_pr dispatches deterministically.
    async def _confirm_ok(self, play_type, params, state):  # noqa: ANN001
        from agentshore.rl.eligibility import PlayVerdict

        return PlayVerdict(play_type=play_type, valid=True, reason=None, candidates=())

    with (
        patch("agentshore.rl.eligibility.EligibilityAuthority.confirm", new=_confirm_ok),
        patch("agentshore.rl.selector._logger") as logger,
    ):
        result = asyncio.run(sel.select(_parallel_state()))

    assert result is not None, "the still-valid merge_pr should be dispatched"
    play_type, params = result
    assert play_type == PlayType.MERGE_PR
    assert play_type != PlayType.WRITE_IMPLEMENTATION_PLAN

    # Exactly one PPO step pending — the dispatched merge_pr. The lost write_impl
    # CAS produced NO _pending (no RL experience sample for the re-pick).
    assert sel._pending is not None
    assert sel._pending.action == merge_idx

    # The clean re-pick is logged. Losing the work-claim CAS after a passing
    # confirm() is the claim_lost_repick clean re-pick (the confirm-rejection
    # variant is confirm_repick; both re-mask the action and resample). The
    # selector's confirm-repick telemetry counter specifically tracks live-drift
    # confirm rejections, so a lost CAS leaves it at 0 — assert that contract too.
    repick_events = [
        call.args[0]
        for call in logger.info.call_args_list
        if call.args and call.args[0].startswith("ppo_selector.") and "repick" in call.args[0]
    ]
    assert "ppo_selector.claim_lost_repick" in repick_events, (
        f"a clean re-pick event must be logged; got {repick_events}"
    )
    assert sel.consume_repick_count() == 0

    # The selector never emits a skip:* play outcome — a clean re-pick is a pure
    # resample, not a skip row. (Selector returns a play; no resolver_exhausted /
    # all_masked warning fired.)
    skip_warnings = [
        call.args[0]
        for call in logger.warning.call_args_list
        if call.args
        and call.args[0] in ("ppo_selector.resolver_exhausted", "ppo_selector.all_masked")
    ]
    assert not skip_warnings


def test_confirm_drift_repick_increments_telemetry_and_logs_confirm_repick():
    """A live-drift confirm rejection cleanly re-picks and is counted as a repick.

    The policy picks write_implementation_plan for issue #234; confirm() is
    monkeypatched to reject the first selection (live drift) and accept the
    re-pick. The selector re-masks the action, resamples merge_pr, dispatches it,
    logs ``confirm_repick``, and counts exactly one confirm-repick in the
    telemetry the orchestrator drains via consume_repick_count().
    """
    from agentshore.rl.action_space import PLAY_TO_INDEX
    from agentshore.rl.eligibility import PlayVerdict
    from agentshore.rl.mask_reason import SELECTED_CANDIDATE_NO_LONGER_AVAILABLE

    policy = ActorCritic()
    buffer = RolloutBuffer()
    updater = PPOUpdater(policy)

    wip_idx = PLAY_TO_INDEX[PlayType.WRITE_IMPLEMENTATION_PLAN]
    merge_idx = PLAY_TO_INDEX[PlayType.MERGE_PR]
    act_calls = {"n": 0}

    def _act(obs, mask, *, greedy):  # noqa: ANN001
        act_calls["n"] += 1
        idx = wip_idx if act_calls["n"] == 1 else merge_idx
        return idx, -0.5, 0.0

    policy.act = _act  # type: ignore[method-assign]

    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value=PlayParams(pr_number=50))
    resolver.project_path = None  # confirm is patched below; no real live read
    # confirm() now runs after resolve+claim, so a rejection releases the claim.
    resolver.release_claim = AsyncMock()

    sel = PPOSelector(
        policy=policy,
        resolver=resolver,
        registry=build_default_registry(),
        buffer=buffer,
        updater=updater,
        metrics=_mock_metrics(),
        cfg=_make_cfg(),
    )

    # Reject the first confirm (write_impl drifted), accept the merge_pr re-pick.
    async def _confirm(self, play_type, params, state):  # noqa: ANN001
        if play_type == PlayType.WRITE_IMPLEMENTATION_PLAN:
            return PlayVerdict(
                play_type=play_type,
                valid=False,
                reason=SELECTED_CANDIDATE_NO_LONGER_AVAILABLE,
                candidates=(),
            )
        return PlayVerdict(play_type=play_type, valid=True, reason=None, candidates=())

    with (
        patch("agentshore.rl.eligibility.EligibilityAuthority.confirm", new=_confirm),
        patch("agentshore.rl.selector._logger") as logger,
    ):
        result = asyncio.run(sel.select(_parallel_state()))

    assert result is not None
    assert result[0] == PlayType.MERGE_PR
    # The live-drift confirm rejection is counted as a confirm-repick.
    assert sel.consume_repick_count() == 1
    confirm_repicks = [
        call.args[0]
        for call in logger.info.call_args_list
        if call.args and call.args[0] == "ppo_selector.confirm_repick"
    ]
    assert confirm_repicks, "confirm_repick must be logged for a live-drift re-pick"


# ---------------------------------------------------------------------------
# update_orchestrator_cfg — mid-session preference reload
# ---------------------------------------------------------------------------


def test_update_orchestrator_cfg_applies_disabled_play_to_mask() -> None:
    """A play disabled mid-session reaches the selector's action-mask cfg.

    Regression: the selector captured ``orchestrator_cfg`` once at construction
    and never refreshed it on reload, so the user-disabled hard-mask was built
    from the bootstrap config. A play turned off mid-session via Preferences
    stayed selectable until the session restarted (run_qa ran ~8 min after being
    disabled). ``update_orchestrator_cfg`` must swap the reference the mask reads.
    """
    from dataclasses import replace

    from agentshore.config import RuntimeConfig
    from agentshore.config.models import PreferencesConfig
    from agentshore.rl.mask import _resolve_user_disabled_plays

    sel = _build_selector()

    enabled = RuntimeConfig()
    sel.update_orchestrator_cfg(enabled)
    assert PlayType.RUN_QA not in _resolve_user_disabled_plays(sel._orchestrator_cfg)

    disabled = replace(enabled, preferences=PreferencesConfig(disabled_plays=("run_qa",)))
    sel.update_orchestrator_cfg(disabled)
    assert PlayType.RUN_QA in _resolve_user_disabled_plays(sel._orchestrator_cfg)
