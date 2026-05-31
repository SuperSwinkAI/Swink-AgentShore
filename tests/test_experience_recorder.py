"""Tests for ExperienceRecorder — crash containment of the RL experience tail.

The ``sidecar_orchestrator_run_failed`` crash came from an unguarded
``_mask_reason_summary`` / observation-encode running as an *argument* before
``_safe_call`` could catch it. These tests pin the contract: a failure in any
sub-step degrades to a skipped record / skipped update, never a raise.
"""

from __future__ import annotations

import collections
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentshore.core.experience_recorder import ExperienceRecorder
from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.state import PlayType

# ---------------------------------------------------------------------------
# mask_reason_summary
# ---------------------------------------------------------------------------


def test_mask_reason_summary_wellformed() -> None:
    state = types.SimpleNamespace(
        mask_reasons={
            PlayType.MERGE_PR: MaskReason(
                text="no blocked PRs",
                classification=MaskClassification.TRANSIENT,
                source=MaskSource.CANDIDATE,
            )
        }
    )
    out = ExperienceRecorder.mask_reason_summary(state)
    assert out is not None
    assert "merge_pr=no blocked PRs" in out


def test_mask_reason_summary_none_when_empty() -> None:
    assert ExperienceRecorder.mask_reason_summary(types.SimpleNamespace(mask_reasons={})) is None
    assert ExperienceRecorder.mask_reason_summary(types.SimpleNamespace()) is None


def test_mask_reason_summary_malformed_degrades_to_none() -> None:
    # A non-dict (or dict with non-MaskReason values) must NOT raise — this is
    # the exact failure shape that crashed the loop.
    assert ExperienceRecorder.mask_reason_summary(types.SimpleNamespace(mask_reasons=["x"])) is None
    bad = types.SimpleNamespace(mask_reasons={PlayType.MERGE_PR: "not-a-mask-reason"})
    assert ExperienceRecorder.mask_reason_summary(bad) is None


# ---------------------------------------------------------------------------
# record_and_update crash containment
# ---------------------------------------------------------------------------


def _make_recorder(monkeypatch, *, store=None, selector=None):
    import agentshore.core.experience_recorder as mod

    # Stub the reward path so the test focuses on persistence containment.
    monkeypatch.setattr(mod, "_build_reward_signals", lambda *a, **k: {})
    monkeypatch.setattr(mod, "compute_reward", lambda *a, **k: (0.0, None))

    host = types.SimpleNamespace(
        _session_id="s1",
        _policy_version="ppo-v1",
        _config_hash="abc",
        _repo_root=__import__("pathlib").Path("/tmp/proj"),
        _step_index=7,
        _recent_agent_types=collections.deque(["claude_code"]),
        _compute_rolling_velocity=lambda _pid: 0.0,
    )
    metrics = MagicMock()
    metrics.snapshot = AsyncMock(return_value=MagicMock())
    sel = selector or MagicMock()
    if selector is None:
        sel.on_play_completed = AsyncMock()
        sel.should_update = MagicMock(return_value=False)
        sel.should_checkpoint = MagicMock(return_value=False)
    cfg = MagicMock()
    return (
        ExperienceRecorder(
            store=store or MagicMock(record_experience=AsyncMock(), wal_checkpoint=AsyncMock()),
            metrics=metrics,
            selector=sel,
            cfg=cfg,
            host=host,
        ),
        host,
        sel,
    )


@pytest.mark.asyncio
async def test_persist_failure_does_not_propagate_or_advance_step(monkeypatch) -> None:
    import agentshore.core.experience_recorder as mod

    def _boom(*_a, **_k):
        raise ValueError("encode blew up")

    monkeypatch.setattr(mod, "encode_observation", _boom)

    store = MagicMock(record_experience=AsyncMock(), wal_checkpoint=AsyncMock())
    recorder, host, sel = _make_recorder(monkeypatch, store=store)

    outcome = types.SimpleNamespace(play_id=1, success=True, agent_id=None, artifacts=[])
    pending = MagicMock()
    pending.mask.tobytes.return_value = b""
    next_state = types.SimpleNamespace(total_plays=10)

    # Must NOT raise even though encode_observation throws inside persist.
    await recorder.record_and_update(
        state_before=types.SimpleNamespace(),
        next_state=next_state,
        outcome=outcome,
        pending_step=pending,
        done=False,
    )

    store.record_experience.assert_not_awaited()  # encode failed before persist
    assert host._step_index == 7  # not advanced on a failed record
    # Learning still proceeds — a record failure must not skip policy feedback.
    sel.on_play_completed.assert_awaited_once()


@pytest.mark.asyncio
async def test_reward_failure_skips_play_without_crashing(monkeypatch) -> None:
    import agentshore.core.experience_recorder as mod

    def _boom(*_a, **_k):
        raise RuntimeError("reward blew up")

    recorder, host, sel = _make_recorder(monkeypatch)
    # Override the helper's reward stub so reward computation now fails.
    monkeypatch.setattr(mod, "compute_reward", _boom)

    outcome = types.SimpleNamespace(play_id=1, success=True, agent_id=None, artifacts=[])
    await recorder.record_and_update(
        state_before=types.SimpleNamespace(),
        next_state=types.SimpleNamespace(total_plays=10),
        outcome=outcome,
        pending_step=MagicMock(),
        done=False,
    )
    # Reward failure → early, guarded return: no learning, no crash.
    sel.on_play_completed.assert_not_awaited()
    assert host._step_index == 7
