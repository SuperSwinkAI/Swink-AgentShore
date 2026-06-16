"""Tests for the v1 config contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agentshore.cli_helpers import _generate_default_config
from agentshore.config import ConfigError, PolicyMode, RunMode, generate_default_config, load_config


def test_generated_cli_config_round_trips(tmp_path: Path) -> None:
    config_text = _generate_default_config(
        name_with_owner="owner/repo",
        agents=["claude", "codex", "gemini", "grok"],
        budget=25.0,
        strict=True,
    )
    assert "  policy_mode: learning\n" in config_text
    config_path = tmp_path / "agentshore.yaml"
    config_path.write_text(config_text, encoding="utf-8")

    config = load_config(config_path)

    assert config.budget.enabled is True
    assert config.budget.total == 25.0
    assert config.scope.strict_mode is True
    assert config.rl.policy_mode == PolicyMode.LEARNING
    assert config.rl.reverse_failsafe_enabled is False
    assert config.rl.reverse_failsafe_after_idle_ticks == 3
    assert config.rl.stale_idle_claim_release_ticks == 3
    assert config.rl.update_every == 16
    assert config.play_pacing.standard_cooldown_plays == 42
    assert set(config.agents) == {"claude_code", "codex", "gemini", "grok"}
    assert config.agents["claude_code"].binary == "claude"
    assert config.agents["codex"].binary == "codex"
    assert config.agents["gemini"].binary == "gemini"
    assert config.agents["grok"].binary == "grok"
    assert config.agents["claude_code"].model_tiers["small"].reasoning_effort == "low"
    assert config.agents["claude_code"].model_tiers["medium"].reasoning_effort == "medium"
    assert config.agents["claude_code"].model_tiers["large"].reasoning_effort == "high"
    assert config.agents["codex"].model_tiers["small"].model == "gpt-5.4-mini"
    assert config.agents["codex"].model_tiers["medium"].model == "gpt-5.4"
    assert config.agents["codex"].model_tiers["medium"].reasoning_effort == "medium"
    assert config.agents["codex"].max_context == 400_000
    # Token rates now live in the pricebook (pricing.yaml), not on AgentConfig.
    codex_price = config.pricebook.resolve("codex", None)
    assert codex_price.cost_per_1k_input == 0.00175
    assert codex_price.cost_per_1k_output == 0.014
    assert codex_price.cost_per_1k_cached_input == 0.000175
    assert config.agents["gemini"].model_tiers["small"].enabled is True
    assert config.agents["gemini"].model == "auto"
    assert config.agents["gemini"].model_tiers["small"].model == "flash-lite"
    assert config.agents["gemini"].model_tiers["medium"].model == "auto"
    assert config.agents["gemini"].model_tiers["large"].model == "pro"
    assert config.agents["grok"].model_tiers["small"].model == "grok-build"
    assert config.agents["grok"].model_tiers["small"].reasoning_effort == "low"
    assert config.agents["grok"].model_tiers["medium"].model == "grok-build"
    assert config.agents["grok"].model_tiers["medium"].reasoning_effort == "medium"
    assert config.agents["grok"].model_tiers["large"].model == "grok-build"
    assert config.agents["grok"].model_tiers["large"].reasoning_effort == "high"
    assert config.agents["grok"].max_context == 256_000
    grok_price = config.pricebook.resolve("grok", None)
    assert grok_price.cost_per_1k_input == 0.001
    assert grok_price.cost_per_1k_cached_input == 0.0002
    assert grok_price.cost_per_1k_output == 0.002
    assert config.skills.path == ".agents/skills/"


def test_default_config_uses_v1_reward_names() -> None:
    config = load_config(None)

    assert config.budget.enabled is False
    assert config.budget.total == 0.0
    assert config.rl.policy_mode == PolicyMode.LEARNING
    assert config.rl.reverse_failsafe_enabled is False
    assert config.rl.reverse_failsafe_after_idle_ticks == 3
    assert config.rl.stale_idle_claim_release_ticks == 3
    assert config.rl.update_every == 16
    assert config.play_pacing.standard_cooldown_plays == 42
    assert config.rl.reward.issue_inflation_penalty == 2.0
    assert config.rl.reward.concurrent_agent_bonus == 0.1


def test_enabled_budget_below_floor_raises_config_error(tmp_path: Path) -> None:
    cfg_path = tmp_path / "agentshore.yaml"
    cfg_path.write_text("budget:\n  enabled: true\n  total: 19.99\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="budget.total must be at least 20.00"):
        load_config(cfg_path)


def test_enabled_budget_at_floor_is_valid(tmp_path: Path) -> None:
    cfg_path = tmp_path / "agentshore.yaml"
    cfg_path.write_text("budget:\n  enabled: true\n  total: 20.0\n", encoding="utf-8")

    config = load_config(cfg_path)

    assert config.budget.enabled is True
    assert config.budget.total == 20.0


def test_disabled_budget_below_floor_is_valid(tmp_path: Path) -> None:
    cfg_path = tmp_path / "agentshore.yaml"
    cfg_path.write_text("budget:\n  enabled: false\n  total: 5.0\n", encoding="utf-8")

    config = load_config(cfg_path)

    assert config.budget.enabled is False
    assert config.budget.total == 5.0


def test_default_circuit_breaker_values() -> None:
    config = load_config(None)

    assert config.circuit_breaker.failures == 3
    assert config.circuit_breaker.window_seconds == 300
    assert config.circuit_breaker.cooldown_seconds == 60


def test_reward_concurrency_bonus_parses_from_yaml(tmp_path: Path) -> None:
    yaml_text = """
rl:
  reward:
    concurrent_agent_bonus: 0.25
"""
    (tmp_path / "agentshore.yaml").write_text(yaml_text, encoding="utf-8")
    config = load_config(tmp_path / "agentshore.yaml")

    assert config.rl.reward.concurrent_agent_bonus == 0.25


def test_policy_mode_audit_replay_parses_from_yaml(tmp_path: Path) -> None:
    yaml_text = """
rl:
  policy_mode: audit-replay
"""
    (tmp_path / "agentshore.yaml").write_text(yaml_text, encoding="utf-8")
    config = load_config(tmp_path / "agentshore.yaml")

    assert config.rl.policy_mode == PolicyMode.AUDIT_REPLAY


def test_legacy_deterministic_maps_to_audit_replay(tmp_path: Path) -> None:
    yaml_text = """
rl:
  deterministic: true
"""
    (tmp_path / "agentshore.yaml").write_text(yaml_text, encoding="utf-8")
    with pytest.warns(DeprecationWarning, match="rl.deterministic is deprecated"):
        config = load_config(tmp_path / "agentshore.yaml")

    assert config.rl.policy_mode == PolicyMode.AUDIT_REPLAY


def test_policy_mode_conflicts_with_legacy_deterministic(tmp_path: Path) -> None:
    yaml_text = """
rl:
  policy_mode: learning
  deterministic: true
"""
    (tmp_path / "agentshore.yaml").write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigError, match="rl.policy_mode conflicts"):
        load_config(tmp_path / "agentshore.yaml")


def test_reverse_failsafe_can_be_enabled_from_yaml(tmp_path: Path) -> None:
    yaml_text = """
rl:
  reverse_failsafe_enabled: true
"""
    (tmp_path / "agentshore.yaml").write_text(yaml_text, encoding="utf-8")
    config = load_config(tmp_path / "agentshore.yaml")

    assert config.rl.reverse_failsafe_enabled is True


def test_reverse_failsafe_idle_tick_threshold_parses_from_yaml(tmp_path: Path) -> None:
    yaml_text = """
rl:
  reverse_failsafe_after_idle_ticks: 7
"""
    (tmp_path / "agentshore.yaml").write_text(yaml_text, encoding="utf-8")
    config = load_config(tmp_path / "agentshore.yaml")

    assert config.rl.reverse_failsafe_after_idle_ticks == 7


def test_stale_idle_claim_release_threshold_parses_from_yaml(tmp_path: Path) -> None:
    yaml_text = """
rl:
  stale_idle_claim_release_ticks: 8
"""
    (tmp_path / "agentshore.yaml").write_text(yaml_text, encoding="utf-8")
    config = load_config(tmp_path / "agentshore.yaml")

    assert config.rl.stale_idle_claim_release_ticks == 8


def test_default_health_values() -> None:
    config = load_config(None)

    assert config.health.poll_interval_seconds == 30
    assert config.health.stale_context_play_threshold == 5


def test_agent_config_has_timeout_and_output_size() -> None:
    config = load_config(None)

    for agent_cfg in config.agents.values():
        assert agent_cfg.timeout is None
        assert agent_cfg.stream_idle_timeout == 1800  # bumped from 600s for desktop-awc
        assert agent_cfg.max_output_size == 10_000_000
        assert agent_cfg.line_limit_bytes == 4_194_304
    assert config.agent_timeout == 3600
    assert config.pricebook.resolve("claude_code", None).cost_per_1k_cached_input == 0.0003
    assert config.pricebook.resolve("codex", None).cost_per_1k_cached_input == 0.000175
    assert config.pricebook.resolve("grok", None).cost_per_1k_cached_input == 0.0002
    assert config.skills.path == ".agents/skills/"


def test_agent_line_limit_parses_from_yaml(tmp_path: Path) -> None:
    yaml_text = """
agents:
  claude_code:
    enabled: true
    binary: claude
    line_limit_bytes: 8388608
"""
    (tmp_path / "agentshore.yaml").write_text(yaml_text, encoding="utf-8")
    config = load_config(tmp_path / "agentshore.yaml")

    assert config.agents["claude_code"].line_limit_bytes == 8_388_608


def test_partial_agent_config_uses_agent_specific_defaults(tmp_path: Path) -> None:
    yaml_text = """
agents:
  codex:
    enabled: true
    binary: codex
"""
    (tmp_path / "agentshore.yaml").write_text(yaml_text, encoding="utf-8")
    config = load_config(tmp_path / "agentshore.yaml")

    # max_context default is sourced from the pricebook's agent_defaults.
    assert config.agents["codex"].max_context == 400_000
    # Token rates resolve through the pricebook, not the AgentConfig.
    codex_price = config.pricebook.resolve("codex", None)
    assert codex_price.cost_per_1k_input == 0.00175
    assert codex_price.cost_per_1k_cached_input == 0.000175
    assert codex_price.cost_per_1k_output == 0.014


def test_partial_gemini_config_uses_agent_specific_defaults(tmp_path: Path) -> None:
    yaml_text = """
agents:
  gemini:
    enabled: true
    binary: gemini
"""
    (tmp_path / "agentshore.yaml").write_text(yaml_text, encoding="utf-8")
    config = load_config(tmp_path / "agentshore.yaml")

    assert config.agents["gemini"].max_context == 1_000_000
    gemini_price = config.pricebook.resolve("gemini", None)
    assert gemini_price.cost_per_1k_input == 0.0005
    assert gemini_price.cost_per_1k_output == 0.003


def test_partial_grok_config_uses_agent_specific_defaults(tmp_path: Path) -> None:
    yaml_text = """
agents:
  grok:
    enabled: true
    binary: grok
"""
    (tmp_path / "agentshore.yaml").write_text(yaml_text, encoding="utf-8")
    config = load_config(tmp_path / "agentshore.yaml")

    assert config.agents["grok"].max_context == 256_000
    grok_price = config.pricebook.resolve("grok", None)
    assert grok_price.cost_per_1k_input == 0.001
    assert grok_price.cost_per_1k_cached_input == 0.0002
    assert grok_price.cost_per_1k_output == 0.002


def test_default_config_has_agent_model_tiers() -> None:
    config = load_config(None)

    assert set(config.agents["claude_code"].model_tiers) == {"small", "medium"}
    assert set(config.agents["codex"].model_tiers) == {"small", "medium"}
    assert set(config.agents["gemini"].model_tiers) == {"small", "medium", "large"}
    assert set(config.agents["grok"].model_tiers) == {"small", "medium", "large"}
    assert config.agents["codex"].model_tiers["small"].model == "gpt-5.4-mini"
    assert config.agents["codex"].model_tiers["small"].reasoning_effort == "low"
    assert config.agents["codex"].model_tiers["medium"].model == "gpt-5.4"
    assert config.agents["codex"].model_tiers["medium"].reasoning_effort == "medium"
    assert config.agents["gemini"].model_tiers["small"].enabled is True
    assert config.agents["gemini"].model_tiers["small"].model == "flash-lite"
    assert config.agents["gemini"].model_tiers["medium"].model == "auto"
    assert config.agents["gemini"].model_tiers["large"].model == "pro"
    assert config.agents["grok"].model_tiers["small"].model == "grok-build"
    assert config.agents["grok"].model_tiers["small"].reasoning_effort == "low"
    assert config.agents["grok"].model_tiers["medium"].model == "grok-build"
    assert config.agents["grok"].model_tiers["medium"].reasoning_effort == "medium"
    assert config.agents["grok"].model_tiers["large"].model == "grok-build"
    assert config.agents["grok"].model_tiers["large"].reasoning_effort == "high"


def test_agent_timeout_parses_from_yaml(tmp_path: Path) -> None:
    yaml_text = """
agents:
  claude_code:
    enabled: true
    binary: claude
    timeout: 120
    stream_idle_timeout: 45
    max_output_size: 5000000
circuit_breaker:
  failures: 5
  window_seconds: 120
  cooldown_seconds: 30
health:
  poll_interval_seconds: 60
  stale_context_play_threshold: 3
"""
    (tmp_path / "agentshore.yaml").write_text(yaml_text, encoding="utf-8")
    config = load_config(tmp_path / "agentshore.yaml")

    assert config.agents["claude_code"].timeout == 120
    assert config.agents["claude_code"].stream_idle_timeout == 45
    assert config.agents["claude_code"].max_output_size == 5_000_000
    assert config.circuit_breaker.failures == 5
    assert config.circuit_breaker.window_seconds == 120
    assert config.circuit_breaker.cooldown_seconds == 30
    assert config.health.poll_interval_seconds == 60
    assert config.health.stale_context_play_threshold == 3


def test_default_config_mode_is_solo_runmode() -> None:
    config = load_config(None)
    assert config.mode is RunMode.SOLO
    # StrEnum members compare equal to their plain string value, so existing
    # ``cfg.mode == "solo"`` call sites remain correct.
    assert config.mode == "solo"


@pytest.mark.parametrize(
    ("yaml_value", "expected"),
    [("solo", RunMode.SOLO), ("agent", RunMode.AGENT)],
)
def test_mode_yaml_round_trip(tmp_path: Path, yaml_value: str, expected: RunMode) -> None:
    yaml_text = f"mode: {yaml_value}\n"
    cfg_path = tmp_path / "agentshore.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")

    config = load_config(cfg_path)

    assert config.mode is expected
    assert config.mode == yaml_value


def test_invalid_mode_raises_config_error(tmp_path: Path) -> None:
    cfg_path = tmp_path / "agentshore.yaml"
    cfg_path.write_text("mode: bogus\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="mode must be one of"):
        load_config(cfg_path)


def test_config_nested_containers_are_immutable() -> None:
    config = load_config(None)

    with pytest.raises(TypeError):
        config.agents["new"] = config.agents["codex"]  # type: ignore[index]
    with pytest.raises(TypeError):
        config.agents["codex"].model_tiers["extra"] = config.agents["codex"].model_tiers["small"]  # type: ignore[index]
    with pytest.raises(AttributeError):
        config.intake.seed_paths.append("docs/")  # type: ignore[attr-defined]


def test_gemini_top_level_reasoning_effort_raises_config_error(tmp_path: Path) -> None:
    """Top-level reasoning_effort on a gemini agent must be rejected."""
    cfg_path = tmp_path / "agentshore.yaml"
    cfg_path.write_text(
        """\
agents:
  gemini:
    enabled: true
    binary: gemini
    reasoning_effort: medium
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="reasoning_effort.*gemini.*no effort flag"):
        load_config(cfg_path)


def test_gemini_tier_reasoning_effort_raises_config_error(tmp_path: Path) -> None:
    """Per-tier reasoning_effort on a gemini agent must be rejected."""
    cfg_path = tmp_path / "agentshore.yaml"
    cfg_path.write_text(
        """\
agents:
  gemini:
    enabled: true
    binary: gemini
    model_tiers:
      medium:
        model: auto
        reasoning_effort: high
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="reasoning_effort.*gemini.*no effort flag"):
        load_config(cfg_path)


def test_generated_default_file_matches_runtime_defaults(tmp_path: Path) -> None:
    """Drift guard for the single-sourced defaults (H3).

    ``generate_default_config`` and ``load_config(None)`` must both derive
    from the same canonical ``_DEFAULT_YAML``. Asserting the on-disk default
    file round-trips to the exact config ``load_config(None)`` returns pins
    them together so the written config can never silently disagree with
    runtime behavior — even though the per-field parser fallbacks (the
    "missing key" defaults) intentionally differ from a few YAML values.
    """
    written = generate_default_config(tmp_path)
    from_file = load_config(written)
    in_memory = load_config(None)

    assert from_file == in_memory
