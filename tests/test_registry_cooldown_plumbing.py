"""Pin: the configured standard cooldown reaches each cooldown play's CooldownGate."""

from __future__ import annotations

import pytest

from agentshore.config import RuntimeConfig
from agentshore.config.models import PlayPacingConfig
from agentshore.play_pacing import STANDARD_PLAY_COOLDOWN_PLAYS
from agentshore.plays.registry import build_default_registry
from agentshore.plays.skill_backed.gates import CooldownGate
from agentshore.state import PlayType

_COOLDOWN_PLAYS = (
    PlayType.RUN_QA,
    PlayType.DESIGN_AUDIT,
    PlayType.CLEANUP,
    PlayType.GROOM_BACKLOG,
    PlayType.CALIBRATE_ALIGNMENT,
    PlayType.PRUNE,
)


def _cooldown_window(registry: object, play_type: PlayType) -> int:
    play = registry.get(play_type)  # type: ignore[attr-defined]
    gates = [g for g in play.gates if isinstance(g, CooldownGate)]
    assert len(gates) == 1, f"{play_type.value} should declare exactly one CooldownGate"
    return gates[0].plays


@pytest.mark.parametrize("play_type", _COOLDOWN_PLAYS)
def test_configured_cooldown_reaches_gate(play_type: PlayType) -> None:
    cfg = RuntimeConfig(play_pacing=PlayPacingConfig(standard_cooldown_plays=7))
    registry = build_default_registry(cfg)
    assert _cooldown_window(registry, play_type) == 7


@pytest.mark.parametrize("play_type", _COOLDOWN_PLAYS)
def test_default_cooldown_is_standard_constant(play_type: PlayType) -> None:
    registry = build_default_registry(None)
    assert _cooldown_window(registry, play_type) == STANDARD_PLAY_COOLDOWN_PLAYS
