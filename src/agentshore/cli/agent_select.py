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


def _interactive_agent_select(
    cfg: RuntimeConfig,
    detected_agents: list[str],
    config_path: Path,
    *,
    force_run: bool = False,
) -> RuntimeConfig:
    """Three-step beaupy wizard: agent selection → tier selection → model selection.

    Writes selections back to agentshore.yaml and returns the updated config.
    Falls back to returning cfg unchanged when stdin is not a TTY.
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

    # ── Step 1/2: Agent selection ────────────────────────────────────────
    click.echo()
    click.echo("=" * 60)
    click.echo("  AgentShore — Agent Setup  (1/2)")
    click.echo(f"  Coding agents detected:  {', '.join(k for k, _ in candidates)}")
    click.echo("  Select agents to enable.  Space to toggle, Enter to confirm.")
    click.echo("=" * 60)
    click.echo()

    agent_keys = [k for k, _ in candidates]
    display_labels = [acfg.binary or k for k, acfg in candidates]
    default_ticked = [i for i, (_, acfg) in enumerate(candidates) if acfg.enabled is not False] or [
        i for i, _ in enumerate(candidates)
    ]

    raw_selected: list[int] = (
        beaupy_select_multiple(
            options=display_labels,
            ticked_indices=default_ticked,
            minimal_count=1,
            tick_style="green",
            cursor_style="cyan",
            return_indices=True,
        )
        or default_ticked
    )
    enabled_keys: set[str] = {agent_keys[i] for i in raw_selected}

    # Pre-fetch model catalogs for enabled agents before step 2 prompts.
    from agentshore.agents.model_catalog import models_for_agent

    click.echo("\n  Fetching available models...", nl=False)
    model_catalogs: dict[str, list[str]] = {
        k: models_for_agent(k, timeout=3.0) for k, _ in candidates if k in enabled_keys
    }
    click.echo(" done.")

    # ── Step 2/2: Tier + model selection ────────────────────────────────
    click.echo()
    click.echo("=" * 60)
    click.echo("  AgentShore — Agent Setup  (2/2)")
    click.echo("  Select tiers and models per enabled agent.")
    click.echo("=" * 60)

    new_agents: dict[str, AgentConfig] = dict(cfg.agents)
    for agent_key, agent_cfg in cfg.agents.items():
        if (
            agent_key in _SUPPORTED_CLI_AGENT_KEYS
            and agent_key not in seen_agent_keys
            and isinstance(agent_cfg, AgentConfig)
        ):
            new_agents[agent_key] = dataclasses.replace(agent_cfg, enabled=False)

    for agent_key, agent_cfg in candidates:
        if agent_key not in enabled_keys:
            new_agents[agent_key] = dataclasses.replace(agent_cfg, enabled=False)
            continue

        try:
            agent_type = AgentType(agent_key)
        except ValueError:
            new_agents[agent_key] = dataclasses.replace(agent_cfg, enabled=True)
            continue

        defaults = default_model_tiers_for(agent_type)
        tier_names = [t for t in MODEL_TIER_ORDER if t in defaults]
        if not tier_names:
            new_agents[agent_key] = dataclasses.replace(agent_cfg, enabled=True)
            continue

        # 2a: Tier selection
        click.echo(f"\n  {agent_key} — select tiers")
        default_tier_indices = [i for i, t in enumerate(tier_names) if defaults[t].enabled]
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

        # 2b: Model selection per enabled tier
        available_models = model_catalogs.get(agent_key, []) + [_CUSTOM_MODEL_SENTINEL]

        model_tiers: dict[str, ModelTierConfig] = {}
        for tier in tier_names:
            dtcfg = defaults[tier]
            if tier not in selected_tier_set:
                model_tiers[tier] = ModelTierConfig(
                    enabled=False,
                    model=dtcfg.model,
                    reasoning_effort=dtcfg.reasoning_effort,
                )
                continue

            click.echo(f"\n  {agent_key} / {tier} — select model")
            default_model = dtcfg.model or ""
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
                    f"  Model for {agent_key}/{tier}: ",
                    initial_value=default_model,
                )
                chosen = (typed or default_model).strip()

            model_tiers[tier] = ModelTierConfig(
                enabled=True,
                model=chosen,
                reasoning_effort=dtcfg.reasoning_effort,
            )

        # Bypass flag applied unconditionally — this is a YOLO-only system.
        extra_flags: list[str] = list(agent_cfg.extra_flags)
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
            agent_cfg,
            enabled=True,
            model=primary.model if primary else agent_cfg.model,
            reasoning_effort=primary.reasoning_effort if primary else agent_cfg.reasoning_effort,
            approved_models=approved,
            model_tiers=model_tiers,
            extra_flags=tuple(extra_flags),
        )

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
