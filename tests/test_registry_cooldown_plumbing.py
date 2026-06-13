"""Pin: the configured standard cooldown reaches each cooldown play's CooldownGate.

The blocky #344 session ran cleanup/prune well inside the configured 42-play
window because the live build's *effective* cooldown matched the old hardcoded
~20, not the configured value — i.e. the config value was not reaching the gate
that enforces it. The config layer itself is covered in ``test_config_models``;
this pins the registry → gate plumbing so a future refactor can't silently drop
the value (or revert to a hardcoded constant) before it reaches the gate.
"""

from __future__ import annotations

import pytest

from agentshore.config import RuntimeConfig
from agentshore.config.models import PlayPacingConfig
from agentshore.play_pacing import STANDARD_PLAY_COOLDOWN_PLAYS
from agentshore.plays.registry import build_default_registry
from agentshore.plays.skill_backed.gates import CooldownGate
from agentshore.state import PlayType

# Every play the registry constructs with ``cooldown_plays=standard_cooldown_plays``.
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
