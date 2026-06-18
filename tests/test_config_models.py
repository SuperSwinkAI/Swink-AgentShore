"""Schema-level pins for new RuntimeConfig fields.

Today this only covers ``play_timeouts`` (desktop-3fiu); future fields
should extend this module rather than scattering schema pins across the
test suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.config import ConfigError, ModelTierConfig, RuntimeConfig, load_config


def test_play_timeouts_defaults_to_empty_mapping() -> None:
    cfg = RuntimeConfig()
    # Frozen by __post_init__ via MappingProxyType.
    assert dict(cfg.play_timeouts) == {}


def test_play_pacing_default_standard_cooldown_is_42() -> None:
    cfg = RuntimeConfig()
    assert cfg.play_pacing.standard_cooldown_plays == 42


def test_play_pacing_yaml_round_trip(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "play_pacing:\n  standard_cooldown_plays: 7\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path)
    assert cfg.play_pacing.standard_cooldown_plays == 7


def test_play_pacing_rejects_negative_cooldown(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "play_pacing:\n  standard_cooldown_plays: -1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="standard_cooldown_plays"):
        load_config(yaml_path)


def test_play_pacing_rejects_boolean_cooldown(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "play_pacing:\n  standard_cooldown_plays: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="standard_cooldown_plays"):
        load_config(yaml_path)


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


def test_agent_timeout_stays_at_default_when_only_play_timeouts_set(tmp_path: Path) -> None:
    """Loading a config with ``play_timeouts`` but no explicit ``agent_timeout``
    must keep the 3h (10800s) loader default. We want the per-play override to be
    additive, not a backdoor change to the global max-runtime backstop."""
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "play_timeouts:\n  issue_pickup: 3600\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path)
    assert cfg.agent_timeout == 10800
    assert cfg.effective_play_timeout("issue_pickup") == 3600
    assert cfg.effective_play_timeout("merge_pr") == 10800


# ---------------------------------------------------------------------------
# Per-tier spawn max (replaces the global agent_spawn.max_per_config)
# ---------------------------------------------------------------------------


def test_model_tier_config_default_max_is_1() -> None:
    """Default per-tier max is 1 when not specified."""
    tier = ModelTierConfig()
    assert tier.max == 1


def test_model_tier_config_max_round_trips_from_yaml(tmp_path: Path) -> None:
    """A per-tier max value in agentshore.yaml is honoured."""
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "agents:\n  claude_code:\n    model_tiers:\n      medium:\n        model: sonnet\n        max: 5\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path)
    tier_cfg = cfg.agents["claude_code"].model_tiers.get("medium")
    assert tier_cfg is not None
    assert tier_cfg.max == 5


def test_legacy_agent_spawn_max_per_config_migrates_to_per_tier_max(tmp_path: Path) -> None:
    """Old agentshore.yaml files with ``agent_spawn.max_per_config`` still parse.

    The value is migrated: every tier that doesn't set its own ``max`` inherits
    the old global cap. A DeprecationWarning is emitted.
    """
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "agents:\n  claude_code:\n    model_tiers:\n      medium:\n        model: sonnet\n"
        "agent_spawn:\n  max_per_config: 4\n",
        encoding="utf-8",
    )
    with pytest.warns(DeprecationWarning, match="agent_spawn is deprecated"):
        cfg = load_config(yaml_path)
    tier_cfg = cfg.agents["claude_code"].model_tiers.get("medium")
    assert tier_cfg is not None
    assert tier_cfg.max == 4


def test_legacy_max_per_config_migrates_to_default_tiers_without_model_tiers(
    tmp_path: Path,
) -> None:
    """An agent relying on default tiers (no ``model_tiers`` block) keeps the cap.

    The legacy global cap must survive the upgrade even when the user never
    wrote an explicit ``model_tiers`` block — otherwise the per-tier ``max``
    would silently fall back to 1 and shrink the fleet. The migration
    materializes each default tier carrying the migrated cap.
    """
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "agents:\n  claude_code:\n    enabled: true\nagent_spawn:\n  max_per_config: 4\n",
        encoding="utf-8",
    )
    with pytest.warns(DeprecationWarning, match="agent_spawn is deprecated"):
        cfg = load_config(yaml_path)
    tiers = cfg.agents["claude_code"].model_tiers
    # Every default tier (small/medium/large) is materialized with the cap.
    assert {"small", "medium", "large"} <= set(tiers)
    for tier_name in ("small", "medium", "large"):
        assert tiers[tier_name].max == 4, tier_name


def test_no_agent_spawn_block_leaves_default_tiers_at_max_1(tmp_path: Path) -> None:
    """Without a legacy block, default-tier agents are not materialized to >1.

    Migration materialization is gated on the legacy ``agent_spawn`` block —
    a modern config must not gain phantom per-tier entries or a non-default cap.
    """
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "agents:\n  claude_code:\n    enabled: true\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path)
    # No explicit model_tiers block and no migration → tiers stay unmaterialized.
    assert cfg.agents["claude_code"].model_tiers == {}


def test_timelapse_defaults_to_disabled_and_uninstalled() -> None:
    cfg = RuntimeConfig()
    assert cfg.timelapse.enabled is False
    assert cfg.timelapse.installed is False


def test_timelapse_yaml_round_trip(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "timelapse:\n  enabled: true\n  installed: true\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path)
    assert cfg.timelapse.enabled is True
    assert cfg.timelapse.installed is True


def test_timelapse_absent_block_uses_defaults(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text("project:\n  path: .\n", encoding="utf-8")
    cfg = load_config(yaml_path)
    assert cfg.timelapse.enabled is False
    assert cfg.timelapse.installed is False


def test_worktrees_disk_knobs_have_conservative_defaults() -> None:
    # Disk-pressure governance ships on by default (#180): a fresh install is
    # protected out of the box, with the floor/high-water/failure-cap active.
    cfg = RuntimeConfig()
    assert cfg.worktrees.min_free_disk_mb == 2048
    assert cfg.worktrees.disk_high_water_mb == 4096
    assert cfg.worktrees.reap_failed_pr_after_n == 2
    assert cfg.worktrees.max_active_worktrees is None


def test_worktrees_disk_knobs_yaml_round_trip(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text(
        "\n".join(
            [
                "worktrees:",
                "  min_free_disk_mb: 2048",
                "  disk_high_water_mb: 4096",
                "  reap_failed_pr_after_n: 2",
                "  max_active_worktrees: 8",
            ]
        ),
        encoding="utf-8",
    )
    cfg = load_config(yaml_path)
    assert cfg.worktrees.min_free_disk_mb == 2048
    assert cfg.worktrees.disk_high_water_mb == 4096
    assert cfg.worktrees.reap_failed_pr_after_n == 2
    assert cfg.worktrees.max_active_worktrees == 8


def test_worktrees_rejects_negative_disk_floor(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text("worktrees:\n  min_free_disk_mb: -1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="min_free_disk_mb"):
        load_config(yaml_path)


def test_worktrees_rejects_nonpositive_max_active(tmp_path: Path) -> None:
    yaml_path = tmp_path / "agentshore.yaml"
    yaml_path.write_text("worktrees:\n  max_active_worktrees: 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="max_active_worktrees"):
        load_config(yaml_path)
