"""Tests for PlayRegistry."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agentshore.plays.registry import PlayRegistry
from agentshore.state import OrchestratorState, PlayType, SessionState

if TYPE_CHECKING:
    from agentshore.plays.base import Play

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_play(play_type: PlayType, preconditions: list[str] | None = None) -> Play:
    """Return a minimal mock satisfying the Play protocol."""

    class _P:
        pass

    p = _P()
    p.play_type = play_type  # type: ignore[attr-defined]
    p.skill_name = None  # type: ignore[attr-defined]
    p.capability = None  # type: ignore[attr-defined]
    p.preconditions = MagicMock(return_value=preconditions or [])  # type: ignore[attr-defined]
    p.estimated_cost = MagicMock(return_value=0.05)  # type: ignore[attr-defined]
    p.execute = MagicMock()  # type: ignore[attr-defined]
    return p  # type: ignore[return-value]


def _make_state() -> OrchestratorState:
    return OrchestratorState(
        session_id="sess-test",
        session_state=SessionState.RUNNING,
        total_plays=0,
        total_cost=0.0,
    )


# ---------------------------------------------------------------------------
# Basic register / get
# ---------------------------------------------------------------------------


def test_register_and_get_play() -> None:
    registry = PlayRegistry()
    play = _make_play(PlayType.ISSUE_PICKUP)
    registry.register(play)
    assert registry.get(PlayType.ISSUE_PICKUP) is play


def test_get_unregistered_raises_key_error() -> None:
    registry = PlayRegistry()
    with pytest.raises(KeyError):
        registry.get(PlayType.ISSUE_PICKUP)


def test_covered_returns_registered_types() -> None:
    registry = PlayRegistry()
    registry.register(_make_play(PlayType.ISSUE_PICKUP))
    registry.register(_make_play(PlayType.CODE_REVIEW))
    assert registry.covered() == {PlayType.ISSUE_PICKUP, PlayType.CODE_REVIEW}


# ---------------------------------------------------------------------------
# Duplicate registration
# ---------------------------------------------------------------------------


def test_duplicate_registration_raises() -> None:
    registry = PlayRegistry()
    registry.register(_make_play(PlayType.ISSUE_PICKUP))
    with pytest.raises(ValueError, match="Duplicate"):
        registry.register(_make_play(PlayType.ISSUE_PICKUP))


# ---------------------------------------------------------------------------
# Freeze
# ---------------------------------------------------------------------------


def test_register_after_freeze_raises() -> None:
    registry = PlayRegistry()
    registry.freeze()
    with pytest.raises(RuntimeError, match="frozen"):
        registry.register(_make_play(PlayType.ISSUE_PICKUP))


def test_freeze_is_idempotent() -> None:
    registry = PlayRegistry()
    registry.freeze()
    registry.freeze()  # second freeze should not raise


# ---------------------------------------------------------------------------
# preconditions_met
# ---------------------------------------------------------------------------


def test_preconditions_met_returns_true_when_empty() -> None:
    registry = PlayRegistry()
    registry.register(_make_play(PlayType.ISSUE_PICKUP, preconditions=[]))
    assert registry.preconditions_met(PlayType.ISSUE_PICKUP, _make_state()) is True


def test_preconditions_met_returns_false_when_unmet() -> None:
    registry = PlayRegistry()
    registry.register(_make_play(PlayType.ISSUE_PICKUP, preconditions=["no open issues"]))
    assert registry.preconditions_met(PlayType.ISSUE_PICKUP, _make_state()) is False


def test_preconditions_met_returns_false_for_unregistered_play() -> None:
    registry = PlayRegistry()
    assert registry.preconditions_met(PlayType.CODE_REVIEW, _make_state()) is False


# ---------------------------------------------------------------------------
# Coverage (xfail until 2M wires all 20 plays)
# ---------------------------------------------------------------------------


def test_default_registry_covers_all_play_types() -> None:
    from agentshore.plays.registry import build_default_registry

    registry = build_default_registry()
    missing = set(PlayType) - registry.covered()
    assert not missing, f"Missing plays: {missing}"
