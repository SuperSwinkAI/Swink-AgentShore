"""Configuration loading, validation, and hot-reload."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import yaml

from agentshore.config._parsers import _build_config, _RawConfig
from agentshore.config.models import (
    AgentConfig,
    AgentPreferencesConfig,
    AgentSpawnConfig,
    AgentTypeAvailability,
    AutoDetectConfig,
    AvailabilityRecord,
    BootstrapConfig,
    BudgetConfig,
    CircuitBreakerConfig,
    DataIntegrityConfig,
    FeedbackConfig,
    FreshStartConfig,
    GhAccountAvailability,
    GitHubIdentity,
    HealthConfig,
    IntakeConfig,
    LearningsConfig,
    LoggingConfig,
    LoopDetectionConfig,
    ModelTierConfig,
    PlayPacingConfig,
    PolicyMode,
    PPOConfig,
    ProjectConfig,
    RewardConfig,
    RLConfig,
    RunMode,
    RuntimeConfig,
    ScopeConfig,
    SessionConfig,
    SkillsConfig,
    StagnationConfig,
    TaskValidationConfig,
    TimelapseConfig,
    TrustedIdsConfig,
    UIConfig,
    WorktreeConfig,
)
from agentshore.errors import ConfigError
from agentshore.play_pacing import STANDARD_PLAY_COOLDOWN_PLAYS

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "AgentConfig",
    "AgentPreferencesConfig",
    "AgentSpawnConfig",
    "AgentTypeAvailability",
    "AutoDetectConfig",
    "AvailabilityRecord",
    "BootstrapConfig",
    "BudgetConfig",
    "CircuitBreakerConfig",
    "ConfigError",
    "DataIntegrityConfig",
    "FeedbackConfig",
    "RuntimeConfig",
    "FreshStartConfig",
    "GhAccountAvailability",
    "GitHubIdentity",
    "HealthConfig",
    "IntakeConfig",
    "LearningsConfig",
    "LoggingConfig",
    "LoopDetectionConfig",
    "ModelTierConfig",
    "PPOConfig",
    "PolicyMode",
    "PlayPacingConfig",
    "ProjectConfig",
    "RewardConfig",
    "RLConfig",
    "RunMode",
    "ScopeConfig",
    "SessionConfig",
    "SkillsConfig",
    "StagnationConfig",
    "TaskValidationConfig",
    "TimelapseConfig",
    "TrustedIdsConfig",
    "UIConfig",
    "WorktreeConfig",
    "generate_default_config",
    "load_config",
]

_DEFAULT_YAML = """\
project:
  path: .
  goals: null

auto:
  detect_agents: true
  detect_github: true
  detect_api_keys: true
  generate_config: true

intake:
  # Seed material for the initial seed_project play (file or directory path,
  # relative to the project root). Used as the fallback when no transient
  # --seed / session.start seed is provided, so every start path honors it.
  seed_paths: []
  issue_labels_include: []
  issue_labels_exclude:
    - wontfix
    - duplicate
  label_prefix: "agentshore/"

budget:
  enabled: false
  total: 0.00
  warning_threshold: 0.20
  # Wall-clock soft cap (independent of the dollar cap above). When
  # time_enabled is true, time_total_minutes is validated to 60–4320 (1h–72h).
  # AgentShore stops assigning new plays 20 minutes before the cap and lets
  # in-flight agents finish; the deadline is the hard-stop backstop.
  time_enabled: false
  time_total_minutes: 0

trusted_ids:
  github_logins: []

agents:
  claude_code:
    enabled: true
    binary: claude
    model: sonnet
    model_tiers:
      small:
        enabled: true
        model: haiku
      medium:
        enabled: true
        model: sonnet
    max_context: 200000
    cost_per_1k_input: 0.003
    cost_per_1k_cached_input: 0.0003
    cost_per_1k_cache_write_input: 0.00375
    cost_per_1k_output: 0.015
  codex:
    enabled: true
    binary: codex
    model: gpt-5.4
    reasoning_effort: medium
    model_tiers:
      small:
        enabled: true
        model: gpt-5.4-mini
        reasoning_effort: low
      medium:
        enabled: true
        model: gpt-5.4
        reasoning_effort: medium
    max_context: 400000
    cost_per_1k_input: 0.00175
    cost_per_1k_cached_input: 0.000175
    cost_per_1k_output: 0.014
  gemini:
    enabled: true
    binary: gemini
    model: auto
    model_tiers:
      small:
        enabled: true
        model: flash-lite
      medium:
        enabled: true
        model: auto
      large:
        enabled: true
        model: pro
    max_context: 1000000
    cost_per_1k_input: 0.0005
    cost_per_1k_output: 0.003
  grok:
    enabled: true
    binary: grok
    model: grok-build
    reasoning_effort: medium
    model_tiers:
      small:
        enabled: true
        model: grok-build
        reasoning_effort: low
      medium:
        enabled: true
        model: grok-build
        reasoning_effort: medium
      large:
        enabled: true
        model: grok-build
        reasoning_effort: high
    max_context: 256000
    cost_per_1k_input: 0.001
    cost_per_1k_cached_input: 0.0002
    cost_per_1k_output: 0.002
  fresh_start:
    max_plays_before_reset: 20
    context_threshold: 0.80
    auto_trigger: false
  preferences:
    affinity: {}
    exclude: {}

agent_spawn:
  cooldown_plays: 2
  max_per_config: 2

play_pacing:
  # Standard post-run cooldown for heavyweight skill-backed plays such as
  # cleanup, run_qa, design_audit, groom_backlog, calibrate_alignment, and prune.
  standard_cooldown_plays: $STANDARD_PLAY_COOLDOWN_PLAYS

circuit_breaker:
  failures: 3
  window_seconds: 300
  cooldown_seconds: 60

health:
  poll_interval_seconds: 30
  stale_context_play_threshold: 5

task_validation:
  max_files_per_task: 5
  max_estimated_minutes: 30
  enforce: true

rl:
  policy_mode: learning
  policy_path: null
  reverse_failsafe_enabled: false
  reverse_failsafe_after_idle_ticks: 3
  stale_idle_claim_release_ticks: 3
  learning_rate: 0.0003
  gamma: 0.99
  entropy_coef: 0.01
  update_every: 16
  checkpoint_every: 16
  reward:
    alignment_weight: 1.0
    issue_throughput_weight: 2.0
    cost_weight: 0.1
    time_weight: 0.05
    completion_bonus: 5.0
    stagnation_penalty: 0.5
    failure_penalty: 1.0
    issue_inflation_penalty: 2.0
    anti_confirmation_bonus: 0.3
    loop_penalty: 1.5
    progress_play_bonus: 0.5
    qa_success_bonus: 2.0
    merge_pr_bonus: 2.5
    concurrent_agent_bonus: 0.1
    type_diversity_bonus: 0.3
    velocity_bonus: 0.5
    velocity_bonus_threshold: 0.05
  stagnation:
    # Minutes that all agents must be idle before each escalation. A busy
    # agent resets the counter; per-play counts are not used.
    warn_after: 1
    alert_after: 3
    pause_after: 5
  loop_detection:
    warn_after: 3
    force_switch_after: 5
    escalate_after: 7
    # desktop-85ex: fleet_idle_persistent fires once per state transition
    # after this many consecutive selector-None ticks (no in-flight work).
    fleet_idle_threshold: 30

session:
  max_plays: null
  timeout_minutes: null
  auto_alignment_check_every: 5
  auto_archive: true
  archive_dir: .agentshore/archives

feedback:
  cadence_plays: null
  cadence_minutes: null
  on_stagnation: true
  on_budget_exhaustion: true
  on_loop_escalation: true
  on_ambiguous_intake: true

scope:
  strict_mode: false
  issue_inflation_threshold: 2.0

ui:
  theme: dark
  refresh_rate: 1.0

logging:
  level: info
  file: true
  log_dir: .agentshore/logs

learnings:
  enabled: true
  file: .agentshore/learnings.json
  max_entries: 200
  min_confidence: 0.3
  decay_after_sessions: 5
  inject_into_prompts: true
  max_prompt_entries: 20

skills:
  install_on_start: true
  path: .agents/skills/
  context_file: .agentshore/context.json

mode: solo
socket: null

# Per-play-type timeout overrides (seconds). Falls back to agent_timeout when
# a play is absent. desktop-3fiu pattern: longer headroom for issue_pickup /
# unblock_pr; keep fast plays bounded by the global default.
# play_timeouts:
#   issue_pickup: 3600
#   unblock_pr: 5400
""".replace("$STANDARD_PLAY_COOLDOWN_PLAYS", str(STANDARD_PLAY_COOLDOWN_PLAYS))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(path: Path | None = None) -> RuntimeConfig:
    """Load configuration from a YAML file.

    If *path* is ``None`` or the file does not exist, returns the built-in
    default YAML parsed through the same validation and normalization path as
    project config files.
    """
    if path is None or not path.exists():
        return _build_config(cast("_RawConfig", yaml.safe_load(_DEFAULT_YAML)))

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read config file: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping, got {type(data).__name__}")

    return _build_config(cast("_RawConfig", data))


def generate_default_config(project_path: Path) -> Path:
    """Write a default ``agentshore.yaml`` into *project_path* and return its path."""
    project_path.mkdir(parents=True, exist_ok=True)
    config_path = project_path / "agentshore.yaml"
    config_path.write_text(_DEFAULT_YAML, encoding="utf-8")
    return config_path
