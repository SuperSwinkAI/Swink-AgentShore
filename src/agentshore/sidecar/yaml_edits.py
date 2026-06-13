"""Atomic per-key YAML writers for agentshore.yaml (sidecar RPC helpers).

Each function accepts a raw YAML string (possibly empty), modifies a single
key via ruamel.yaml (preserving all comments and key ordering), and returns
the updated YAML string. Callers are responsible for atomic persistence via
:func:`agentshore.sidecar.project._atomic_write_text`.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentshore.config.models import BudgetConfig, TimelapseConfig


def write_target_branch(yaml_text: str, branch: str) -> str:
    """Round-trip *yaml_text* and set ``project.target_branch`` to *branch*.

    Preserves comments and key ordering via ruamel.yaml. If the document is
    empty or has no ``project`` key, a minimal one is created.
    """
    from ruamel.yaml import YAML

    rt = YAML()
    rt.preserve_quotes = True
    data = rt.load(yaml_text) if yaml_text.strip() else None
    if data is None:
        data = {}
    project = data.get("project")
    if not isinstance(project, dict):
        project = {}
        data["project"] = project
    project["target_branch"] = branch
    buf = io.StringIO()
    rt.dump(data, buf)
    return buf.getvalue()


def write_seed_paths(yaml_text: str, seed_paths: list[str]) -> str:
    """Round-trip *yaml_text* and set ``intake.seed_paths`` to *seed_paths*.

    Preserves comments and key ordering via ruamel.yaml. Mirrors
    :func:`write_target_branch`; creates the ``intake`` mapping if absent.
    """
    from ruamel.yaml import YAML

    rt = YAML()
    rt.preserve_quotes = True
    data = rt.load(yaml_text) if yaml_text.strip() else None
    if data is None:
        data = {}
    intake = data.get("intake")
    if not isinstance(intake, dict):
        intake = {}
        data["intake"] = intake
    intake["seed_paths"] = list(seed_paths)
    buf = io.StringIO()
    rt.dump(data, buf)
    return buf.getvalue()


def write_budget(yaml_text: str, budget: BudgetConfig) -> str:
    """Round-trip *yaml_text* and set the top-level ``budget`` mapping.

    Delegates to :func:`agentshore.config.budget_writer.render_budget_yaml` so
    the sidecar RPC and the live ``Orchestrator.set_budget`` path share one
    serialiser. Preserves comments / key ordering on every other section.
    """
    from agentshore.config.budget_writer import render_budget_yaml

    return render_budget_yaml(yaml_text, budget)


def write_trusted_issue_enforcement(yaml_text: str, enabled: bool) -> str:
    """Round-trip *yaml_text* and set ``trusted_ids.restrict_issues_to_trusted_authors``.

    Preserves comments and key ordering on every other section via
    ruamel.yaml. Get-or-creates the ``trusted_ids`` mapping if absent.
    """
    from ruamel.yaml import YAML

    rt = YAML()
    rt.preserve_quotes = True
    data = rt.load(yaml_text) if yaml_text.strip() else None
    if data is None:
        data = {}
    trusted_ids = data.get("trusted_ids")
    if not isinstance(trusted_ids, dict):
        trusted_ids = {}
        data["trusted_ids"] = trusted_ids
    trusted_ids["restrict_issues_to_trusted_authors"] = bool(enabled)
    buf = io.StringIO()
    rt.dump(data, buf)
    return buf.getvalue()


def write_timelapse(yaml_text: str, timelapse: TimelapseConfig) -> str:
    """Round-trip *yaml_text* and set the top-level ``timelapse`` mapping.

    Preserves comments / key ordering on every other section via ruamel.yaml,
    matching :func:`write_budget`.
    """
    from ruamel.yaml import YAML

    rt = YAML()
    rt.preserve_quotes = True
    data = rt.load(yaml_text) if yaml_text.strip() else None
    if data is None:
        data = {}
    existing = data.get("timelapse")
    block: dict[str, object] = existing if isinstance(existing, dict) else {}
    block["enabled"] = bool(timelapse.enabled)
    block["installed"] = bool(timelapse.installed)
    data["timelapse"] = block
    buf = io.StringIO()
    rt.dump(data, buf)
    return buf.getvalue()
