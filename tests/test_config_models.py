"""Schema-level pins for new RuntimeConfig fields.

Today this only covers ``play_timeouts`` (desktop-3fiu); future fields
should extend this module rather than scattering schema pins across the
test suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.config import ConfigError, RuntimeConfig, load_config


def test_play_timeouts_defaults_to_empty_mapping() -> None:
    cfg = RuntimeConfig()
    # Frozen by __post_init__ via MappingProxyType.
    assert dict(cfg.play_timeouts) == {}


def test_play_timeouts_yaml_round_trip(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "\n".join(
            [
                "agent_timeout: 1800",
                "play_timeouts:",
                "  issue_pickup: 3600",
                "  unblock_pr: 5400",
            ]
        ),
        encoding="utf-8",
    )
    cfg = load_config(yaml_path)
    assert dict(cfg.play_timeouts) == {"issue_pickup": 3600, "unblock_pr": 5400}
    assert cfg.effective_play_timeout("issue_pickup") == 3600
    assert cfg.effective_play_timeout("unblock_pr") == 5400
    assert cfg.effective_play_timeout("merge_pr") == 1800
    assert cfg.effective_play_timeout(None) == 1800


def test_play_timeouts_rejects_non_mapping(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text("play_timeouts: 3600\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="play_timeouts must be a mapping"):
        load_config(yaml_path)


def test_play_timeouts_rejects_non_positive_seconds(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "play_timeouts:\n  issue_pickup: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="positive number of seconds"):
        load_config(yaml_path)


def test_play_timeouts_rejects_boolean(tmp_path: Path) -> None:
    """``bool`` is a subclass of int — explicitly reject so misconfigured
    YAML (``play_timeouts: {issue_pickup: true}``) fails loudly."""
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "play_timeouts:\n  issue_pickup: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="positive number of seconds"):
        load_config(yaml_path)


def test_play_timeouts_rejects_non_string_keys(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "play_timeouts:\n  42: 3600\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="play_timeouts keys must be strings"):
        load_config(yaml_path)


def test_play_timeouts_mapping_is_immutable() -> None:
    cfg = RuntimeConfig(play_timeouts={"issue_pickup": 3600})
    with pytest.raises(TypeError):
        cfg.play_timeouts["unblock_pr"] = 5400  # type: ignore[index]


def test_effective_play_timeout_with_no_override() -> None:
    cfg = RuntimeConfig(agent_timeout=900)
    assert cfg.effective_play_timeout("issue_pickup") == 900
    assert cfg.effective_play_timeout("merge_pr") == 900
    assert cfg.effective_play_timeout(None) == 900


def test_agent_timeout_stays_at_1800_when_only_play_timeouts_set(tmp_path: Path) -> None:
    """Loading a config with ``play_timeouts`` but no explicit ``agent_timeout``
    must keep the historical 3600s loader default. We want the per-play
    override to be additive, not a backdoor change to the global."""
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "play_timeouts:\n  issue_pickup: 3600\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path)
    assert cfg.agent_timeout == 3600
    assert cfg.effective_play_timeout("issue_pickup") == 3600
    assert cfg.effective_play_timeout("merge_pr") == 3600


# ---------------------------------------------------------------------------
# Agent-spawn caps (desktop-ty04)
# ---------------------------------------------------------------------------


def test_agent_spawn_default_max_per_config_is_2() -> None:
    """Default per-(type, tier) cap is 2 (desktop-ty04)."""
    cfg = RuntimeConfig()
    assert cfg.agent_spawn.max_per_config == 2
    assert not hasattr(cfg.agent_spawn, "max_total")


def test_agent_spawn_max_per_config_round_trips_from_yaml(tmp_path: Path) -> None:
    """A user-set ``agent_spawn.max_per_config`` is honoured."""
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text("agent_spawn:\n  max_per_config: 4\n", encoding="utf-8")
    cfg = load_config(yaml_path)
    assert cfg.agent_spawn.max_per_config == 4


def test_legacy_max_total_field_parses_with_deprecation_warning(tmp_path: Path) -> None:
    """Old agentshore.yaml files with ``max_total`` still parse — value is ignored.

    Back-compat: users may have existing configs from before desktop-ty04
    that include ``agent_spawn.max_total: 10``. The parser should accept
    the field (no ConfigError) but emit a DeprecationWarning and ignore
    the value entirely. The per-(type, tier) cap is now the sole ceiling.
    """
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "agent_spawn:\n  max_per_config: 3\n  max_total: 10\n",
        encoding="utf-8",
    )
    with pytest.warns(DeprecationWarning, match="max_total is deprecated"):
        cfg = load_config(yaml_path)
    assert cfg.agent_spawn.max_per_config == 3
