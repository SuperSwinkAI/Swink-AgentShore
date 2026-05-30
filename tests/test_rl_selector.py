"""Tests for rl/selector.py — PPOSelector."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

from agentshore.config.models import PolicyMode
from agentshore.plays.base import PlayParams
from agentshore.rl.action_space import NUM_ACTIONS, PLAY_TO_INDEX
from agentshore.rl.experience import RolloutBuffer
from agentshore.rl.observation import OBSERVATION_DIM
from agentshore.rl.policy import ActorCritic
from agentshore.rl.selector import PPOSelector, _only_capacity_waiting, _PendingStep
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


def test_capacity_only_reasons_include_allowed_tier_waits():
    assert _only_capacity_waiting(
        [
            {"reason": "No IDLE agent of allowed tier (large)", "count": 1},
            {"reason": "Reserved action slot", "count": 2},
        ]
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
    sel = _build_selector(
        all_preconds=False,
        resolver_params=PlayParams(issue_number=234),
    )
    sel._policy.act = MagicMock(  # type: ignore[method-assign]
        return_value=(PLAY_TO_INDEX[PlayType.ISSUE_PICKUP], -0.1, 0.0)
    )
    state = _state(open_issues=[_issue()], agents=[_agent()])

    with patch("agentshore.rl.selector._logger"):
        assert asyncio.run(sel.select(state)) is None
        assert asyncio.run(sel.select(state)) is None
        result = asyncio.run(sel.select(state))

    assert result is not None
    play_type, params = result
    assert play_type == PlayType.ISSUE_PICKUP
    assert params.extras["reverse_failsafe"] is True
    assert params.extras["reverse_failsafe_bypassed_preconditions"] is True


def test_select_auto_reverse_failsafe_opens_dead_end_controls():
    sel = _build_selector(
        all_preconds=False,
        resolver_params=PlayParams(),
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
    """_reload_shared_weights loads policy_v{POLICY_VERSION}.pt, ignores policy.pt."""
    import unittest.mock as mock

    from agentshore.rl.action_space import POLICY_VERSION

    sel = _build_selector()

    # Build the expected directory structure under a fake home.
    new_home = tmp_path / "home"
    weights_dir = new_home / ".agentshore" / "weights"
    weights_dir.mkdir(parents=True, exist_ok=True)

    # Save a real compatible checkpoint at the versioned path.
    versioned_path = weights_dir / f"policy_v{POLICY_VERSION}.pt"
    sel._policy.save(versioned_path)

    # Ensure the legacy policy.pt does NOT exist (we want to confirm it is ignored).
    legacy_path = weights_dir / "policy.pt"
    assert not legacy_path.exists()

    with mock.patch("pathlib.Path.home", return_value=new_home):
        sel._reload_shared_weights()  # should succeed silently, no exception


def test_reload_shared_weights_skips_incompatible(tmp_path: Path) -> None:
    """_reload_shared_weights skips checkpoints that raise IncompatibleCheckpointError."""
    import unittest.mock as mock

    import torch

    from agentshore.rl.action_space import ACTION_SPACE_VERSION, POLICY_VERSION

    sel = _build_selector()
    original_state = {k: v.clone() for k, v in sel._policy.state_dict().items()}

    weights_dir = tmp_path / "weights"
    weights_dir.mkdir(parents=True, exist_ok=True)

    stale_path = weights_dir / f"policy_v{POLICY_VERSION}.pt"
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

    logger = mock.Mock()
    with mock.patch("agentshore.rl.selector._logger", logger):
        cleanup_stale_canonical_weights(weights_dir)

    logger.warning.assert_any_call(
        "stale_canonical_checkpoint_renamed",
        from_path=str(legacy_path),
        to_path=mock.ANY,
    )
