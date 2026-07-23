"""Equivalence tests for the `_defaults_only`-driven config parsers (TNQA wave-2).

Covers every parser that was converted from hand-written
``FooConfig(field=raw.get(...))`` boilerplate to the shared
``agentshore.config._parsers._defaults_only`` helper: `_parse_auto`,
`_parse_circuit_breaker`, `_parse_health`, `_parse_data_integrity`,
`_parse_ppo`, `_parse_stagnation`, `_parse_loop_detection`, `_parse_session`,
`_parse_feedback`, `_parse_timelapse`, `_parse_skills`,
`_parse_task_validation`. Asserts every YAML-supplied value is honored and
every field left absent from YAML falls back to the dataclass default —
the exact contract the old explicit parsers implemented by hand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.config import load_config

if TYPE_CHECKING:
    from pathlib import Path

_YAML = """
auto:
  detect_agents: false
  detect_github: true
  detect_api_keys: false
  generate_config: true

circuit_breaker:
  failures: 7
  window_seconds: 120
  cooldown_seconds: 45

health:
  poll_interval_seconds: 15
  stale_context_play_threshold: 9

data_integrity:
  enabled: false
  canary_interval_seconds: 111
  snapshot_interval_seconds: 222
  snapshot_ring_size: 4
  wal_checkpoint_interval_seconds: 15

rl:
  ppo:
    clip_epsilon: 0.3
    gae_lambda: 0.9
    ppo_epochs: 6
    mini_batch_size: 8
    value_loss_coef: 0.4
    max_grad_norm: 1.0
    reward_clip_low: -5.0
    reward_clip_high: 5.0
  stagnation:
    warn_after: 2
    alert_after: 4
    pause_after: 6
  loop_detection:
    warn_after: 1
    force_switch_after: 2
    escalate_after: 3
    fleet_idle_threshold: 10

session:
  max_plays: 50
  auto_alignment_check_every: 9
  auto_archive: false
  archive_dir: custom/archives
  break_duration_minutes: 15

feedback:
  cadence_plays: 3
  cadence_minutes: 20
  on_stagnation: false
  on_budget_exhaustion: false
  on_loop_escalation: false
  on_ambiguous_intake: false
  unanswered_timeout_seconds: 30.0
  loop_liveness_timeout_seconds: 90.0
  graceful_drain_timeout_seconds: 60.0

timelapse:
  enabled: true
  installed: true

skills:
  install_on_start: false
  path: custom/skills/
  context_file: custom/context.json

task_validation:
  max_files_per_task: 12
  max_estimated_minutes: 99
  enforce: false
"""


def test_defaults_only_parsers_honor_every_yaml_field(tmp_path: Path) -> None:
    (tmp_path / "agentshore.yaml").write_text(_YAML, encoding="utf-8")
    config = load_config(tmp_path / "agentshore.yaml")

    assert config.auto.detect_agents is False
    assert config.auto.detect_github is True
    assert config.auto.detect_api_keys is False
    assert config.auto.generate_config is True

    assert config.circuit_breaker.failures == 7
    assert config.circuit_breaker.window_seconds == 120
    assert config.circuit_breaker.cooldown_seconds == 45

    assert config.health.poll_interval_seconds == 15
    assert config.health.stale_context_play_threshold == 9

    assert config.data_integrity.enabled is False
    assert config.data_integrity.canary_interval_seconds == 111
    assert config.data_integrity.snapshot_interval_seconds == 222
    assert config.data_integrity.snapshot_ring_size == 4
    assert config.data_integrity.wal_checkpoint_interval_seconds == 15

    assert config.rl.ppo.clip_epsilon == 0.3
    assert config.rl.ppo.gae_lambda == 0.9
    assert config.rl.ppo.ppo_epochs == 6
    assert config.rl.ppo.mini_batch_size == 8
    assert config.rl.ppo.value_loss_coef == 0.4
    assert config.rl.ppo.max_grad_norm == 1.0
    assert config.rl.ppo.reward_clip_low == -5.0
    assert config.rl.ppo.reward_clip_high == 5.0

    assert config.rl.stagnation.warn_after == 2
    assert config.rl.stagnation.alert_after == 4
    assert config.rl.stagnation.pause_after == 6

    assert config.rl.loop_detection.warn_after == 1
    assert config.rl.loop_detection.force_switch_after == 2
    assert config.rl.loop_detection.escalate_after == 3
    assert config.rl.loop_detection.fleet_idle_threshold == 10

    assert config.session.max_plays == 50
    assert config.session.auto_alignment_check_every == 9
    assert config.session.auto_archive is False
    assert config.session.archive_dir == "custom/archives"
    assert config.session.break_duration_minutes == 15

    assert config.feedback.cadence_plays == 3
    assert config.feedback.cadence_minutes == 20
    assert config.feedback.on_stagnation is False
    assert config.feedback.on_budget_exhaustion is False
    assert config.feedback.on_loop_escalation is False
    assert config.feedback.on_ambiguous_intake is False
    assert config.feedback.unanswered_timeout_seconds == 30.0
    assert config.feedback.loop_liveness_timeout_seconds == 90.0
    assert config.feedback.graceful_drain_timeout_seconds == 60.0

    assert config.timelapse.enabled is True
    assert config.timelapse.installed is True

    assert config.skills.install_on_start is False
    assert config.skills.path == "custom/skills/"
    assert config.skills.context_file == "custom/context.json"

    assert config.task_validation.max_files_per_task == 12
    assert config.task_validation.max_estimated_minutes == 99
    assert config.task_validation.enforce is False


def test_defaults_only_parsers_fall_back_to_dataclass_defaults_when_absent() -> None:
    """No YAML sections at all → every converted parser must produce the
    same defaults the hand-written versions used to hard-code."""
    config = load_config(None)

    assert config.auto.detect_agents is True
    assert config.auto.generate_config is True
    assert config.circuit_breaker.failures == 3
    assert config.circuit_breaker.window_seconds == 300
    assert config.circuit_breaker.cooldown_seconds == 60
    assert config.health.poll_interval_seconds == 30
    assert config.health.stale_context_play_threshold == 5
    assert config.data_integrity.enabled is True
    assert config.data_integrity.snapshot_ring_size == 3
    assert config.rl.ppo.clip_epsilon == 0.2
    assert config.rl.ppo.reward_clip_high == 10.0
    assert config.rl.stagnation.pause_after == 5
    assert config.rl.loop_detection.fleet_idle_threshold == 30
    assert config.session.max_plays is None
    assert config.session.archive_dir == ".agentshore/archives"
    assert config.feedback.cadence_plays is None
    assert config.feedback.graceful_drain_timeout_seconds is None
    assert config.feedback.unanswered_timeout_seconds == 120.0
    assert config.timelapse.enabled is False
    assert config.skills.path == ".agents/skills/"
    assert config.task_validation.max_files_per_task == 5
