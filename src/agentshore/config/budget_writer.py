"""Shared, layering-neutral writer for the ``budget:`` block of agentshore.yaml.

Both the sidecar ``project.set_budget`` RPC (pre-session, project dir) and the
live ``Orchestrator.set_budget``/``add_budget`` control paths (mid-session,
config path) persist budget caps the same way: round-trip the YAML with
ruamel so comments / key ordering on every other section survive, set only the
``budget`` mapping, and write atomically. Keeping this here (under ``config``)
lets ``core`` persist without importing from ``sidecar``.
"""

from __future__ import annotations

import io
import os
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.config.models import BudgetConfig


def render_budget_yaml(yaml_text: str, budget: BudgetConfig) -> str:
    """Round-trip *yaml_text* and set the top-level ``budget`` mapping."""
    from ruamel.yaml import YAML

    rt = YAML()
    rt.preserve_quotes = True
    data = rt.load(yaml_text) if yaml_text.strip() else None
    if data is None:
        data = {}
    existing = data.get("budget")
    budget_block: dict[str, object] = existing if isinstance(existing, dict) else {}
    budget_block["enabled"] = bool(budget.enabled)
    budget_block["total"] = float(budget.total)
    budget_block["warning_threshold"] = float(budget.warning_threshold)
    budget_block["time_enabled"] = bool(budget.time_enabled)
    budget_block["time_total_minutes"] = int(budget.time_total_minutes)
    data["budget"] = budget_block
    buf = io.StringIO()
    rt.dump(data, buf)
    return buf.getvalue()


def write_budget_to_config(config_path: Path, budget: BudgetConfig) -> None:
    """Atomically persist *budget* into the ``budget:`` block of *config_path*.

    Reads the current file (empty string if absent), renders the new YAML, and
    writes via a temp file + ``os.replace`` so a crash mid-write can never leave
    a truncated config. Preserves comments / ordering on all other sections.
    """
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    rendered = render_budget_yaml(existing, budget)
    fd, tmp = tempfile.mkstemp(dir=str(config_path.parent), prefix=config_path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        os.replace(tmp, config_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
