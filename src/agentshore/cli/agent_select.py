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
    from agentshore.config.models import ModelTierConfig


def _needs_interactive_agent_selection(cfg: RuntimeConfig, *, config_created: bool) -> bool:
    """Return whether the start command should offer first-run agent setup."""
    return config_created or any(
        isinstance(v, AgentConfig) and v.enabled and not v.model_tiers for v in cfg.agents.values()
    )


# Config blocks where defaulting to empty/permissive on parse failure would
# weaken the trust boundary (an unparseable trusted_ids must not become "trust
# everyone"): re-raise instead of falling back to the agents-only wizard.
_SECURITY_RELEVANT_BLOCKS: tuple[str, ...] = ("identities", "trusted_ids")


def _load_config_for_agent_setup(config_path: Path) -> RuntimeConfig:
    """Load enough config for the agent setup wizard.

    ``init --force`` should still offer the agent picker when unrelated config
    sections need repair, such as duplicate identity keys. The full loader is
    tried first; if it rejects the file, fall back to parsing only ``agents:`` —
    but only for errors *outside* the security-relevant blocks
    (:data:`_SECURITY_RELEVANT_BLOCKS`). An ``identities``/``trusted_ids`` parse
    error is re-raised so the caller surfaces it rather than silently continuing
    with empty (permissive) trust state.
    """
    import yaml

    from agentshore.config import load_config
    from agentshore.config._parsers import _parse_agent
    from agentshore.config.models import RuntimeConfig
    from agentshore.errors import ConfigError

    try:
        return load_config(config_path)
    except ConfigError as exc:
        message = str(exc)
        # init swallows ConfigError to stay resilient, so without this echo a
        # broken identities/trusted_ids block would vanish silently. Surface it
        # loudly but still fall through — session start re-enforces the trust
        # boundary (load_config re-raises), so the empty-trust fallback never runs.
        if any(block in message for block in _SECURITY_RELEVANT_BLOCKS):
            click.secho(
                f"  ERROR: security-relevant config block failed validation: {message}",
                err=True,
                fg="red",
            )
            click.secho(
                "  Continuing with agent selection only — fix the trust block above "
                "before starting a session (a session will refuse to start until it parses).",
                err=True,
                fg="red",
            )

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

        # Real config problem the user must fix — surface prominently.
        click.secho(
            f"  WARNING: agentshore.yaml failed full validation: {message}",
            err=True,
            fg="yellow",
        )
        click.secho(
            "  Continuing with agent selection only; fix the error above and "
            "re-run `agentshore configure` to validate the whole file.",
            err=True,
            fg="yellow",
        )
        return RuntimeConfig(agents=agents)


def _agent_key_for_detected_binary(binary: str) -> str | None:
    """Map an available CLI binary name to AgentShore's config agent key."""
    return _AGENT_KEY_BY_BINARY.get(binary)


_AGENT_LABELS: dict[str, str] = {
    "claude_code": "claude",
    "codex": "codex",
    "grok": "grok",
    "antigravity": "agy",
    "swink_coding": "swink-coding",
}


class AgentTierEditor:
    """Pure, I/O-free state for the agent-setup wizard's tier edits.

    Holds the working ``{agent_key: AgentConfig}`` map and exposes the model's
    state transitions (``toggle`` an agent on/off, ``set_tier`` one model-tier
    cell) without any terminal prompting or rendering. The TUI in
    :func:`_interactive_agent_select` gathers user input and drives these
    methods, so the wizard's decision logic is unit-testable in isolation.
    """

    def __init__(self, agents: dict[str, AgentConfig]) -> None:
        self.agents: dict[str, AgentConfig] = agents

    def commit_tiers(
        self, agent_key: str, tiers: dict[str, ModelTierConfig], *, enabled: bool
    ) -> None:
        """Write *tiers* back, refreshing primary/approved/bypass-flag fields."""
        import dataclasses

        from agentshore.agents.model_tiers import MODEL_TIER_PRIORITY

        acfg = self.agents.get(agent_key)
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
        self.agents[agent_key] = dataclasses.replace(
            base,
            enabled=enabled,
            model=primary.model if primary else base.model,
            reasoning_effort=primary.reasoning_effort if primary else base.reasoning_effort,
            approved_models=approved,
            model_tiers=tiers,
            extra_flags=tuple(extra_flags),
        )

    def toggle(self, agent_key: str) -> None:
        """Flip an agent on/off, materializing default tiers on enable."""
        import dataclasses

        from agentshore.agents.model_tiers import MODEL_TIER_ORDER, default_model_tiers_for
        from agentshore.state import AgentType

        acfg = self.agents.get(agent_key)
        if isinstance(acfg, AgentConfig) and acfg.enabled:
            self.agents[agent_key] = dataclasses.replace(acfg, enabled=False)
            return
        try:
            agent_type = AgentType(agent_key)
        except ValueError:
            base = acfg if isinstance(acfg, AgentConfig) else AgentConfig()
            self.agents[agent_key] = dataclasses.replace(base, enabled=True)
            return
        if isinstance(acfg, AgentConfig) and acfg.model_tiers:
            tiers = dict(acfg.model_tiers)
        else:
            defaults = default_model_tiers_for(agent_type)
            tiers = {t: defaults[t] for t in MODEL_TIER_ORDER if t in defaults}
        self.commit_tiers(agent_key, tiers, enabled=True)

    def set_tier(self, agent_key: str, tier: str, tier_cfg: ModelTierConfig) -> None:
        """Set one model-tier cell for *agent_key* and re-commit derived fields."""
        acfg = self.agents.get(agent_key)
        tiers = dict(acfg.model_tiers) if isinstance(acfg, AgentConfig) and acfg.model_tiers else {}
        tiers[tier] = tier_cfg
        self.commit_tiers(agent_key, tiers, enabled=True)


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

    from agentshore.agents.model_tiers import (
        MODEL_TIER_ORDER,
        default_model_tiers_for,
        reasoning_efforts_for,
    )
    from agentshore.config.models import ModelTierConfig
    from agentshore.state import AgentType
    from agentshore.subprocess_env import NONINTERACTIVE_ENV, is_interactive

    if not is_interactive():
        if os.environ.get(NONINTERACTIVE_ENV):
            click.echo(
                "  (Agent setup wizard skipped — AGENTSHORE_NONINTERACTIVE is set. "
                "Edit agentshore.yaml manually or unset the variable.)"
            )
        elif force_run:
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

    # Candidates = supported agents detected on PATH; existing config provides
    # defaults, missing ones are added so older yaml still offers every agent.
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

    from agentshore.agents.model_catalog import models_for_agent

    click.echo("\n  Fetching available models...", nl=False)
    model_catalogs: dict[str, list[str]] = {
        k: models_for_agent(k, timeout=3.0) for k, _ in candidates
    }
    click.echo(" done.")

    # Renderers below read ``new_agents`` — the same dict AgentTierEditor
    # mutates in place.
    new_agents: dict[str, AgentConfig] = dict(cfg.agents)
    for agent_key, agent_cfg in candidates:
        if agent_key not in new_agents:
            new_agents[agent_key] = agent_cfg
    editor = AgentTierEditor(new_agents)

    # Accelerators assigned once over the fixed candidate list so they stay put
    # across redraws (muscle memory): number per agent toggle, letter per tier.
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

    def _edit_tier_cell(agent_key: str, tier: str) -> None:
        """[a-l] key: sequential 3-prompt edit (enabled → model → max) for one cell.

        Gathers the user's choices via prompts, then delegates the state
        mutation to :meth:`AgentTierEditor.set_tier` (the testable core).
        """
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

        efforts = reasoning_efforts_for(agent_type)
        is_grok = agent_type == AgentType.GROK
        # swink-coding's `--model` flag accepts the small/medium/large tier
        # aliases directly, or a `provider:model[@endpoint]` value dispatched
        # via `--tier-map` (both surface through the model catalog below), so
        # the free-form custom-model entry must still not be offered here —
        # a hand-typed value skips `classify_swink_model` validation and could
        # be an invalid CLI value.
        is_swink_coding = agent_type == AgentType.SWINK_CODING
        # total prompt count: enable + model (skipped for grok) + effort (if any) + max.
        total = 2 + (0 if is_grok else 1) + (1 if efforts else 0)
        n = 0

        # tier enabled?
        n += 1
        click.echo(f"  [{n}/{total}] tier enabled?")
        enable_choice: str | None = beaupy_select(
            options=["Enable", "Disable"],
            cursor_index=0 if (cur is None or cur.enabled) else 1,
            cursor_style="cyan",
        )
        if (enable_choice or "Enable") == "Disable":
            editor.set_tier(
                agent_key,
                tier,
                ModelTierConfig(
                    enabled=False,
                    model=cur.model if cur else dtcfg.model,
                    reasoning_effort=cur.reasoning_effort if cur else dtcfg.reasoning_effort,
                    max=cur.max if cur else dtcfg.max,
                ),
            )
            return

        # model — grok is hard-pinned to grok-4.5, so skip the picker entirely.
        if is_grok:
            chosen = "grok-4.5"
            click.echo("  model · grok-4.5 (fixed)")
        else:
            n += 1
            catalog_models = model_catalogs.get(agent_key, [])
            available_models = (
                catalog_models if is_swink_coding else catalog_models + [_CUSTOM_MODEL_SENTINEL]
            )
            default_model = cur.model if cur and cur.model else dtcfg.model or ""
            cursor_idx = (
                available_models.index(default_model) if default_model in available_models else 0
            )
            click.echo(f"  [{n}/{total}] model")
            chosen_sel: str | None = beaupy_select(
                options=available_models, cursor_index=cursor_idx, cursor_style="cyan"
            )
            if not chosen_sel:
                chosen = default_model
            elif chosen_sel == _CUSTOM_MODEL_SENTINEL:
                typed = beaupy_prompt(f"  Model for {label}/{tier}: ", initial_value=default_model)
                chosen = (typed or default_model).strip()
            else:
                chosen = chosen_sel

        # reasoning effort — only agents whose CLI exposes one.
        if efforts:
            n += 1
            default_effort = (
                cur.reasoning_effort if cur and cur.reasoning_effort else dtcfg.reasoning_effort
            ) or efforts[0]
            effort_cursor = list(efforts).index(default_effort) if default_effort in efforts else 0
            click.echo(f"  [{n}/{total}] reasoning effort")
            effort_sel: str | None = beaupy_select(
                options=list(efforts), cursor_index=effort_cursor, cursor_style="cyan"
            )
            chosen_effort: str | None = effort_sel or default_effort
        else:
            chosen_effort = cur.reasoning_effort if cur else dtcfg.reasoning_effort

        # max
        n += 1
        current_max = cur.max if cur else dtcfg.max
        new_max: int = click.prompt(
            f"  [{n}/{total}] Max agents",
            default=current_max,
            type=click.IntRange(1, 20),
            show_default=True,
        )

        editor.set_tier(
            agent_key,
            tier,
            ModelTierConfig(
                enabled=True,
                model=chosen,
                reasoning_effort=chosen_effort,
                max=new_max,
            ),
        )

    # Boxed accelerator picker loop.
    letter_keys = sorted(cell_by_letter)
    key_hint = f"[{letter_keys[0]}-{letter_keys[-1]}] edit tier · " if letter_keys else ""
    while True:
        _print_agent_boxes()
        click.echo(f"\n  {key_hint}[1-{len(candidates)}] toggle agent · [Enter] confirm")
        key = click.prompt("  ›", default="", show_default=False).strip().lower()
        if key == "":
            break
        if key in agent_by_number:
            editor.toggle(agent_by_number[key])
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

    # Merge serialized fields into each raw entry (never replace) so unmanaged
    # user fields like ``binary`` survive the round-trip.
    from agentshore.config.yaml_io import agent_config_to_yaml_dict

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if "agents" not in raw:
            raw["agents"] = {}
        for key, acfg in new_agents.items():
            if not isinstance(acfg, AgentConfig):
                continue
            if key not in raw["agents"]:
                raw["agents"][key] = {}
            raw["agents"][key].update(agent_config_to_yaml_dict(acfg))
        config_path.write_text(yaml.dump(raw, default_flow_style=False), encoding="utf-8")
        click.echo(f"\n  Saved to {config_path}")
    except (OSError, yaml.YAMLError) as exc:
        click.echo(f"  Warning: could not save config ({exc})", err=True)

    click.echo("=" * 60)
    click.echo()
    return cfg
