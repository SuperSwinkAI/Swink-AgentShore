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


def _interactive_agent_select(
    cfg: RuntimeConfig,
    detected_agents: list[str],
    config_path: Path,
    *,
    force_run: bool = False,
) -> RuntimeConfig:
    """Boxed accelerator agent/tier/model/max wizard.

    Renders detected agents as a 2-up grid of boxes whose cells carry single-key
    accelerators: a number per box toggles that whole agent, a letter per tier
    cell opens a sequential model/max edit. Loops until the user presses Enter,
    then writes selections back to agentshore.yaml and returns the updated
    config. Falls back to returning cfg unchanged when stdin is not a TTY.
    """
    import os
    import sys

    from agentshore.agents.model_tiers import (
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
    import string

    import yaml
    from beaupy import prompt as beaupy_prompt
    from beaupy import select as beaupy_select

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

    # Stable accelerator maps: a number per agent (toggle), a letter per tier
    # cell (edit). Letters/numbers are assigned once over the fixed candidate
    # list so they stay put across redraws (muscle memory).
    agent_num_by_key: dict[str, str] = {}
    agent_by_number: dict[str, str] = {}
    cell_by_letter: dict[str, tuple[str, str]] = {}
    tier_letter_by_cell: dict[tuple[str, str], str] = {}
    _letters = iter(string.ascii_lowercase)
    for _idx, (_ak, _) in enumerate(candidates):
        _num = str(_idx + 1)
        agent_num_by_key[_ak] = _num
        agent_by_number[_num] = _ak
        try:
            _at = AgentType(_ak)
        except ValueError:
            continue
        _defaults = default_model_tiers_for(_at)
        for _t in MODEL_TIER_ORDER:
            if _t in _defaults:
                _ltr = next(_letters)
                cell_by_letter[_ltr] = (_ak, _t)
                tier_letter_by_cell[(_ak, _t)] = _ltr

    box_inner = 24
    box_border = box_inner + 2  # rows render as "│ " + INNER + " │"

    def _tier_names_for(agent_key: str) -> list[str]:
        try:
            defaults = default_model_tiers_for(AgentType(agent_key))
        except ValueError:
            return []
        return [t for t in MODEL_TIER_ORDER if t in defaults]

    def _tier_cell_text(letter: str, tier: str, tc: ModelTierConfig | None) -> str:
        """One box line, e.g. ``[b] Medium  sonnet     ×5`` or ``[d] Small  — off``."""
        label = tier.capitalize()
        if tc is None or not tc.enabled:
            return f"[{letter}] {label:<6} — off"
        max_s = f"×{tc.max}"
        prefix = f"[{letter}] {label:<6} "
        avail = box_inner - len(prefix) - len(max_s) - 1
        model = tc.model or "?"
        if len(model) > avail:
            model = model[: max(1, avail - 1)] + "…"
        return f"{prefix}{model:<{avail}} {max_s}"

    def _render_box(agent_key: str) -> list[str]:
        num = agent_num_by_key.get(agent_key, "?")
        label = _AGENT_LABELS.get(agent_key, agent_key)
        acfg = new_agents.get(agent_key)
        title = f"─ [{num}] {label} "
        top = "┌" + title + "─" * max(0, box_border - len(title)) + "┐"
        bottom = "└" + "─" * box_border + "┘"
        tier_names = _tier_names_for(agent_key)
        rows: list[str] = []
        if not (isinstance(acfg, AgentConfig) and acfg.enabled):
            rows.append(f"disabled — press [{num}]")
        else:
            for t in tier_names:
                letter = tier_letter_by_cell.get((agent_key, t), "?")
                tc = acfg.model_tiers.get(t) if acfg.model_tiers else None
                rows.append(_tier_cell_text(letter, t, tc))
        while len(rows) < max(3, len(tier_names)):
            rows.append("")
        return [top] + [f"│ {r.ljust(box_inner)[:box_inner]} │" for r in rows] + [bottom]

    def _print_agent_boxes() -> None:
        click.echo()
        click.echo("=" * 60)
        click.echo("  AgentShore — Agent Setup")
        click.echo("=" * 60)
        boxes = [_render_box(ak) for ak, _ in candidates]
        for i in range(0, len(boxes), 2):
            left = boxes[i]
            right = boxes[i + 1] if i + 1 < len(boxes) else None
            if right is None:
                for line in left:
                    click.echo("  " + line)
            else:
                for lft, rgt in zip(left, right, strict=True):
                    click.echo("  " + lft + " " + rgt)
        click.echo("=" * 60)

    def _commit_tiers(agent_key: str, tiers: dict[str, ModelTierConfig], *, enabled: bool) -> None:
        """Write tiers back to new_agents, refreshing primary/approved/bypass flags."""
        acfg = new_agents.get(agent_key)
        base = acfg if isinstance(acfg, AgentConfig) else AgentConfig(enabled=enabled)
        extra_flags = list(base.extra_flags)
        for flag in _BYPASS_FLAGS.get(agent_key, ()):
            if flag not in extra_flags:
                extra_flags.append(flag)
        primary_tier = next(
            (t for t in MODEL_TIER_PRIORITY if t in tiers and tiers[t].enabled), None
        )
        primary = tiers[primary_tier] if primary_tier else None
        approved = tuple(
            dict.fromkeys(tc.model for tc in tiers.values() if tc.enabled and tc.model)
        )
        new_agents[agent_key] = dataclasses.replace(
            base,
            enabled=enabled,
            model=primary.model if primary else base.model,
            reasoning_effort=primary.reasoning_effort if primary else base.reasoning_effort,
            approved_models=approved,
            model_tiers=tiers,
            extra_flags=tuple(extra_flags),
        )

    def _toggle_agent(agent_key: str) -> None:
        """[N] key: flip an agent on/off, materializing default tiers on enable."""
        acfg = new_agents.get(agent_key)
        if isinstance(acfg, AgentConfig) and acfg.enabled:
            new_agents[agent_key] = dataclasses.replace(acfg, enabled=False)
            return
        try:
            agent_type = AgentType(agent_key)
        except ValueError:
            base = acfg if isinstance(acfg, AgentConfig) else AgentConfig()
            new_agents[agent_key] = dataclasses.replace(base, enabled=True)
            return
        if isinstance(acfg, AgentConfig) and acfg.model_tiers:
            tiers = dict(acfg.model_tiers)
        else:
            defaults = default_model_tiers_for(agent_type)
            tiers = {t: defaults[t] for t in MODEL_TIER_ORDER if t in defaults}
        _commit_tiers(agent_key, tiers, enabled=True)

    def _edit_tier_cell(agent_key: str, tier: str) -> None:
        """[a-l] key: sequential 3-prompt edit (enabled → model → max) for one cell."""
        label = _AGENT_LABELS.get(agent_key, agent_key)
        try:
            agent_type = AgentType(agent_key)
        except ValueError:
            return
        dtcfg = default_model_tiers_for(agent_type).get(tier, ModelTierConfig())
        acfg = new_agents.get(agent_key)
        tiers = dict(acfg.model_tiers) if isinstance(acfg, AgentConfig) and acfg.model_tiers else {}
        cur = tiers.get(tier)

        click.echo(f"\n  Edit {label} · {tier}")

        # [1/3] tier enabled?
        click.echo("  [1/3] tier enabled?")
        enable_choice: str | None = beaupy_select(
            options=["Enable", "Disable"],
            cursor_index=0 if (cur is None or cur.enabled) else 1,
            cursor_style="cyan",
        )
        if (enable_choice or "Enable") == "Disable":
            tiers[tier] = ModelTierConfig(
                enabled=False,
                model=cur.model if cur else dtcfg.model,
                reasoning_effort=cur.reasoning_effort if cur else dtcfg.reasoning_effort,
                max=cur.max if cur else dtcfg.max,
            )
            _commit_tiers(agent_key, tiers, enabled=True)
            return

        # [2/3] model
        available_models = model_catalogs.get(agent_key, []) + [_CUSTOM_MODEL_SENTINEL]
        default_model = cur.model if cur and cur.model else dtcfg.model or ""
        cursor_idx = (
            available_models.index(default_model) if default_model in available_models else 0
        )
        click.echo("  [2/3] model")
        chosen: str | None = beaupy_select(
            options=available_models, cursor_index=cursor_idx, cursor_style="cyan"
        )
        if not chosen:
            chosen = default_model
        elif chosen == _CUSTOM_MODEL_SENTINEL:
            typed = beaupy_prompt(f"  Model for {label}/{tier}: ", initial_value=default_model)
            chosen = (typed or default_model).strip()

        # [3/3] max
        current_max = cur.max if cur else dtcfg.max
        new_max: int = click.prompt(
            "  [3/3] Max agents",
            default=current_max,
            type=click.IntRange(1, 20),
            show_default=True,
        )

        tiers[tier] = ModelTierConfig(
            enabled=True,
            model=chosen,
            reasoning_effort=cur.reasoning_effort if cur else dtcfg.reasoning_effort,
            max=new_max,
        )
        _commit_tiers(agent_key, tiers, enabled=True)

    # ── Boxed accelerator picker loop ────────────────────────────────────
    letter_keys = sorted(cell_by_letter)
    key_hint = f"[{letter_keys[0]}-{letter_keys[-1]}] edit tier · " if letter_keys else ""
    while True:
        _print_agent_boxes()
        click.echo(f"\n  {key_hint}[1-{len(candidates)}] toggle agent · [Enter] confirm")
        key = click.prompt("  ›", default="", show_default=False).strip().lower()
        if key == "":
            break
        if key in agent_by_number:
            _toggle_agent(agent_by_number[key])
            continue
        if key in cell_by_letter:
            cell_agent, cell_tier = cell_by_letter[key]
            acfg = new_agents.get(cell_agent)
            if not (isinstance(acfg, AgentConfig) and acfg.enabled):
                click.echo(
                    f"  ({_AGENT_LABELS.get(cell_agent, cell_agent)} is disabled — "
                    f"press [{agent_num_by_key.get(cell_agent, '?')}] to enable it first)"
                )
                continue
            _edit_tier_cell(cell_agent, cell_tier)
            continue
        click.echo(f"  (unrecognized key: {key!r})")

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
