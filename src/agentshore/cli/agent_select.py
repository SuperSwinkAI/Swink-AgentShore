"""Interactive agent / model-tier selection wizard."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import click

from agentshore.cli.constants import (
    _AGENT_KEY_BY_BINARY,
    _BYPASS_FLAGS,
    _CUSTOM_MODEL_SENTINEL,
    _SUPPORTED_CLI_AGENT_KEYS,
)
from agentshore.config.models import AgentConfig

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.config import RuntimeConfig
    from agentshore.config._parsers import _RawAgent


def _needs_interactive_agent_selection(cfg: RuntimeConfig, *, config_created: bool) -> bool:
    """Return whether the start command should offer first-run agent setup."""
    return config_created or any(
        isinstance(v, AgentConfig) and v.enabled and not v.model_tiers for v in cfg.agents.values()
    )


def _load_config_for_agent_setup(config_path: Path) -> RuntimeConfig:
    """Load enough config for the agent setup wizard.

    ``init --force`` should still offer the agent picker when unrelated config
    sections need repair, such as duplicate identity keys. The full loader is
    tried first; if it rejects the file, fall back to parsing only ``agents:``.
    """
    import yaml

    from agentshore.config import load_config
    from agentshore.config._parsers import _parse_agent
    from agentshore.config.models import RuntimeConfig
    from agentshore.errors import ConfigError

    try:
        return load_config(config_path)
    except ConfigError as exc:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise
        raw_agents = raw.get("agents") or {}
        if not isinstance(raw_agents, dict):
            raise

        agents: dict[str, AgentConfig] = {}
        for agent_key, agent_raw in raw_agents.items():
            if agent_key in {"fresh_start", "preferences"} or not isinstance(agent_raw, dict):
                continue
            agents[str(agent_key)] = _parse_agent(str(agent_key), cast("_RawAgent", agent_raw))

        if not agents:
            raise

        click.echo(
            "  (Config validation issue outside agent setup; continuing with "
            f"agent selection: {exc})",
            err=True,
        )
        return RuntimeConfig(agents=agents)


def _agent_key_for_detected_binary(binary: str) -> str | None:
    """Map an available CLI binary name to AgentShore's config agent key."""
    return _AGENT_KEY_BY_BINARY.get(binary)


_AGENT_LABELS: dict[str, str] = {
    "claude_code": "claude",
    "codex": "codex",
    "gemini": "gemini",
    "grok": "grok",
}

_TIER_INITIALS: dict[str, str] = {
    "small": "S",
    "medium": "M",
    "large": "L",
}


def _interactive_agent_select(
    cfg: RuntimeConfig,
    detected_agents: list[str],
    config_path: Path,
    *,
    force_run: bool = False,
) -> RuntimeConfig:
    """Hub-and-spoke agent/tier/model/max wizard.

    Displays a review grid, then loops through a hub menu allowing per-agent
    edits until the user confirms. Writes selections back to agentshore.yaml
    and returns the updated config. Falls back to returning cfg unchanged when
    stdin is not a TTY.
    """
    import os
    import sys

    from agentshore.agents.model_tiers import (
        DEFAULT_MODEL_TIER,
        MODEL_TIER_ORDER,
        MODEL_TIER_PRIORITY,
        default_model_tiers_for,
    )
    from agentshore.config.models import ModelTierConfig
    from agentshore.state import AgentType

    if os.environ.get("AGENTSHORE_NONINTERACTIVE"):
        click.echo(
            "  (Agent setup wizard skipped — AGENTSHORE_NONINTERACTIVE is set. "
            "Edit agentshore.yaml manually or unset the variable.)"
        )
        return cfg

    if not sys.stdin.isatty():
        if force_run:
            click.echo(
                "  (Agent setup wizard requested but stdin is not a TTY; "
                "skipping. Run `agentshore configure` from an interactive shell.)"
            )
        return cfg

    import dataclasses

    import yaml
    from beaupy import prompt as beaupy_prompt
    from beaupy import select as beaupy_select
    from beaupy import select_multiple as beaupy_select_multiple

    # Build the candidate list from supported agent binaries detected on PATH.
    # Existing config provides defaults; missing detected agents are added so
    # older agentshore.yaml files still offer every available supported agent.
    candidates: list[tuple[str, AgentConfig]] = []
    seen_agent_keys: set[str] = set()
    for detected_binary in detected_agents:
        agent_key = _agent_key_for_detected_binary(detected_binary)
        if agent_key is None or agent_key in seen_agent_keys:
            continue
        seen_agent_keys.add(agent_key)
        existing_cfg = cfg.agents.get(agent_key)
        if isinstance(existing_cfg, AgentConfig):
            agent_cfg = dataclasses.replace(
                existing_cfg,
                binary=existing_cfg.binary or detected_binary,
            )
        else:
            agent_cfg = AgentConfig(enabled=True, binary=detected_binary)
        candidates.append((agent_key, agent_cfg))

    if not candidates:
        if force_run:
            click.echo(
                "  (Agent setup wizard requested but no supported coding agents "
                "were detected on PATH.)"
            )
        return cfg

    # Pre-fetch model catalogs for all candidate agents up front.
    from agentshore.agents.model_catalog import models_for_agent

    click.echo("\n  Fetching available models...", nl=False)
    model_catalogs: dict[str, list[str]] = {
        k: models_for_agent(k, timeout=3.0) for k, _ in candidates
    }
    click.echo(" done.")

    # Working state: mutable dict of AgentConfig, starts from existing config.
    new_agents: dict[str, AgentConfig] = dict(cfg.agents)
    # Seed any candidate agents that aren't already in new_agents.
    for agent_key, agent_cfg in candidates:
        if agent_key not in new_agents:
            new_agents[agent_key] = agent_cfg

    def _tier_status_label(agent_key: str) -> str:
        """Build the S/M/L status string for one agent in the review grid."""
        acfg = new_agents.get(agent_key)
        if not isinstance(acfg, AgentConfig) or not acfg.enabled:
            return "disabled"
        parts: list[str] = []
        for tier_full, initial in _TIER_INITIALS.items():
            tc = acfg.model_tiers.get(tier_full) if acfg.model_tiers else None
            if tc is not None and tc.enabled:
                parts.append(f"{initial}x{tc.max}")
            else:
                parts.append(f"{initial}:off")
        return " ".join(parts) if parts else "enabled"

    def _print_review_grid() -> None:
        click.echo()
        click.echo("=" * 60)
        click.echo("  AgentShore — Agent Setup")
        click.echo("=" * 60)
        label_width = max((len(_AGENT_LABELS.get(k, k)) for k, _ in candidates), default=10)
        for agent_key, _ in candidates:
            label = _AGENT_LABELS.get(agent_key, agent_key)
            status = _tier_status_label(agent_key)
            click.echo(f"  {label:<{label_width}}  {status}")
        click.echo("=" * 60)

    def _edit_agent(agent_key: str, agent_cfg: AgentConfig) -> None:
        """Hub spoke: interactively edit one agent's tier/model/max settings."""
        try:
            agent_type = AgentType(agent_key)
        except ValueError:
            # Non-standard agent type — just toggle enabled.
            acfg = new_agents.get(agent_key, agent_cfg)
            new_agents[agent_key] = dataclasses.replace(acfg, enabled=True)
            return

        defaults = default_model_tiers_for(agent_type)
        tier_names = [t for t in MODEL_TIER_ORDER if t in defaults]

        current_cfg = new_agents.get(agent_key, agent_cfg)
        label = _AGENT_LABELS.get(agent_key, agent_key)

        click.echo(f"\n  Editing: {label}")

        # Enable/disable the agent first.
        enable_choice: str | None = beaupy_select(
            options=["Enable", "Disable"],
            cursor_index=0 if (current_cfg.enabled is not False) else 1,
            cursor_style="cyan",
        )
        agent_enabled = (enable_choice or "Enable") == "Enable"

        if not agent_enabled:
            new_agents[agent_key] = dataclasses.replace(current_cfg, enabled=False)
            return

        if not tier_names:
            new_agents[agent_key] = dataclasses.replace(current_cfg, enabled=True)
            return

        # Tier enable/disable via multi-select.
        click.echo(f"\n  {label} — select tiers to enable  (Space to toggle, Enter to confirm)")
        existing_tiers = current_cfg.model_tiers if current_cfg.model_tiers else {}
        default_tier_indices = [
            i
            for i, t in enumerate(tier_names)
            if (tc.enabled if (tc := existing_tiers.get(t)) is not None else defaults[t].enabled)
        ]
        if not default_tier_indices:
            med = tier_names.index(DEFAULT_MODEL_TIER) if DEFAULT_MODEL_TIER in tier_names else 0
            default_tier_indices = [med]

        raw_tiers: list[int] = (
            beaupy_select_multiple(
                options=tier_names,
                ticked_indices=default_tier_indices,
                minimal_count=1,
                tick_style="green",
                cursor_style="cyan",
                return_indices=True,
            )
            or default_tier_indices
        )
        selected_tier_set = {tier_names[i] for i in raw_tiers}

        # Model + max per enabled tier.
        available_models = model_catalogs.get(agent_key, []) + [_CUSTOM_MODEL_SENTINEL]
        model_tiers: dict[str, ModelTierConfig] = {}

        for tier in tier_names:
            dtcfg = defaults[tier]
            existing_tc = existing_tiers.get(tier)

            if tier not in selected_tier_set:
                model_tiers[tier] = ModelTierConfig(
                    enabled=False,
                    model=existing_tc.model if existing_tc else dtcfg.model,
                    reasoning_effort=(
                        existing_tc.reasoning_effort if existing_tc else dtcfg.reasoning_effort
                    ),
                    max=existing_tc.max if existing_tc else dtcfg.max,
                )
                continue

            click.echo(f"\n  {label} / {tier} — select model")
            default_model = (
                existing_tc.model if existing_tc and existing_tc.model else dtcfg.model or ""
            )
            cursor_idx = (
                available_models.index(default_model) if default_model in available_models else 0
            )

            chosen: str | None = beaupy_select(
                options=available_models,
                cursor_index=cursor_idx,
                cursor_style="cyan",
            )
            if not chosen:
                chosen = default_model
            elif chosen == _CUSTOM_MODEL_SENTINEL:
                typed = beaupy_prompt(
                    f"  Model for {label}/{tier}: ",
                    initial_value=default_model,
                )
                chosen = (typed or default_model).strip()

            current_max = existing_tc.max if existing_tc else dtcfg.max
            new_max: int = click.prompt(
                f"  Max agents [{tier} tier]",
                default=current_max,
                type=click.IntRange(1, 20),
                show_default=True,
            )

            model_tiers[tier] = ModelTierConfig(
                enabled=True,
                model=chosen,
                reasoning_effort=(
                    existing_tc.reasoning_effort if existing_tc else dtcfg.reasoning_effort
                ),
                max=new_max,
            )

        # Bypass flag applied unconditionally — this is a YOLO-only system.
        extra_flags: list[str] = list(current_cfg.extra_flags)
        for flag in _BYPASS_FLAGS.get(agent_key, ()):
            if flag not in extra_flags:
                extra_flags.append(flag)

        primary_tier = next(
            (t for t in MODEL_TIER_PRIORITY if t in model_tiers and model_tiers[t].enabled),
            None,
        )
        primary = model_tiers[primary_tier] if primary_tier else None
        approved = tuple(
            dict.fromkeys(tc.model for tc in model_tiers.values() if tc.enabled and tc.model)
        )

        new_agents[agent_key] = dataclasses.replace(
            current_cfg,
            enabled=True,
            model=primary.model if primary else current_cfg.model,
            reasoning_effort=(
                primary.reasoning_effort if primary else current_cfg.reasoning_effort
            ),
            approved_models=approved,
            model_tiers=model_tiers,
            extra_flags=tuple(extra_flags),
        )

    # Unconfigured agents detected on PATH but not yet in new_agents.
    unconfigured_keys = [k for k, _ in candidates if not isinstance(new_agents.get(k), AgentConfig)]

    # ── Hub-and-spoke loop ───────────────────────────────────────────────
    while True:
        _print_review_grid()

        # Build hub menu options.
        hub_options: list[str] = ["✓ Confirm & continue"]
        for agent_key, _ in candidates:
            label = _AGENT_LABELS.get(agent_key, agent_key)
            hub_options.append(f"Edit {label}")
        for agent_key in unconfigured_keys:
            label = _AGENT_LABELS.get(agent_key, agent_key)
            hub_options.append(f"+ add: {label}")

        click.echo()
        chosen_hub: str | None = beaupy_select(
            options=hub_options,
            cursor_index=0,
            cursor_style="cyan",
        )
        if not chosen_hub or chosen_hub == "✓ Confirm & continue":
            break

        # Resolve which agent was chosen.
        selected_agent_key: str | None = None
        for agent_key, agent_cfg in candidates:
            label = _AGENT_LABELS.get(agent_key, agent_key)
            if chosen_hub in (f"Edit {label}", f"+ add: {label}"):
                selected_agent_key = agent_key
                _edit_agent(agent_key, agent_cfg)
                if agent_key in unconfigured_keys and isinstance(
                    new_agents.get(agent_key), AgentConfig
                ):
                    unconfigured_keys.remove(agent_key)
                break

        if selected_agent_key is None:
            # Fallthrough — shouldn't happen; treat as confirm.
            break

    # Disable supported CLI agents that weren't in the candidate list.
    for agent_key, agent_cfg in cfg.agents.items():
        if (
            agent_key in _SUPPORTED_CLI_AGENT_KEYS
            and agent_key not in seen_agent_keys
            and isinstance(agent_cfg, AgentConfig)
        ):
            new_agents[agent_key] = dataclasses.replace(agent_cfg, enabled=False)

    cfg = dataclasses.replace(cfg, agents=new_agents)

    # Write selections back to agentshore.yaml.
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if "agents" not in raw:
            raw["agents"] = {}
        for key, acfg in new_agents.items():
            if not isinstance(acfg, AgentConfig):
                continue
            if key not in raw["agents"]:
                raw["agents"][key] = {}
            raw["agents"][key]["enabled"] = acfg.enabled
            if acfg.model:
                raw["agents"][key]["model"] = acfg.model
            if acfg.reasoning_effort:
                raw["agents"][key]["reasoning_effort"] = acfg.reasoning_effort
            if acfg.approved_models:
                raw["agents"][key]["approved_models"] = list(acfg.approved_models)
            if acfg.model_tiers:
                raw["agents"][key]["model_tiers"] = {
                    tier: {
                        "enabled": tier_cfg.enabled,
                        "model": tier_cfg.model,
                        "max": tier_cfg.max,
                        **(
                            {"reasoning_effort": tier_cfg.reasoning_effort}
                            if tier_cfg.reasoning_effort
                            else {}
                        ),
                    }
                    for tier, tier_cfg in acfg.model_tiers.items()
                }
            if acfg.extra_flags:
                raw["agents"][key]["extra_flags"] = list(acfg.extra_flags)
        config_path.write_text(yaml.dump(raw, default_flow_style=False), encoding="utf-8")
        click.echo(f"\n  Saved to {config_path}")
    except (OSError, yaml.YAMLError) as exc:
        click.echo(f"  Warning: could not save config ({exc})", err=True)

    click.echo("=" * 60)
    click.echo()
    return cfg
