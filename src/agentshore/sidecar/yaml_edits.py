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
    from collections.abc import Callable

    from agentshore.config.models import BudgetConfig, TimelapseConfig


def _patch_yaml_key(
    yaml_text: str,
    key: str,
    mutate: Callable[[dict[str, object]], None],
) -> str:
    """Round-trip *yaml_text*, get-or-create the mapping at top-level *key*,
    apply *mutate* in place, and return the dumped text.

    Shared by the ``write_*`` helpers below: they differ only in which key
    they touch and how they mutate the nested mapping. Preserves comments
    and key ordering via ruamel.yaml's round-trip representer. If the
    document is empty or has no mapping at *key*, a minimal one is created.
    """
    from ruamel.yaml import YAML

    rt = YAML()
    rt.preserve_quotes = True
    data = rt.load(yaml_text) if yaml_text.strip() else None
    if data is None:
        data = {}
    block = data.get(key)
    if not isinstance(block, dict):
        block = {}
        data[key] = block
    mutate(block)
    buf = io.StringIO()
    rt.dump(data, buf)
    return buf.getvalue()


def write_target_branch(yaml_text: str, branch: str) -> str:
    """Round-trip *yaml_text* and set ``project.target_branch`` to *branch*."""

    def _mutate(project: dict[str, object]) -> None:
        project["target_branch"] = branch

    return _patch_yaml_key(yaml_text, "project", _mutate)


def write_seed_paths(yaml_text: str, seed_paths: list[str]) -> str:
    """Round-trip *yaml_text* and set ``intake.seed_paths`` to *seed_paths*."""

    def _mutate(intake: dict[str, object]) -> None:
        intake["seed_paths"] = list(seed_paths)

    return _patch_yaml_key(yaml_text, "intake", _mutate)


def write_budget(yaml_text: str, budget: BudgetConfig) -> str:
    """Round-trip *yaml_text* and set the top-level ``budget`` mapping.

    Delegates to :func:`agentshore.config.budget_writer.render_budget_yaml` so
    the sidecar RPC and the live ``Orchestrator.set_budget`` path share one
    serialiser. Preserves comments / key ordering on every other section.
    """
    from agentshore.config.budget_writer import render_budget_yaml

    return render_budget_yaml(yaml_text, budget)


def write_trusted_issue_enforcement(yaml_text: str, enabled: bool) -> str:
    """Round-trip *yaml_text* and set ``trusted_ids.restrict_issues_to_trusted_authors``."""

    def _mutate(trusted_ids: dict[str, object]) -> None:
        trusted_ids["restrict_issues_to_trusted_authors"] = bool(enabled)

    return _patch_yaml_key(yaml_text, "trusted_ids", _mutate)


def write_timelapse(yaml_text: str, timelapse: TimelapseConfig) -> str:
    """Round-trip *yaml_text* and set the top-level ``timelapse`` mapping.

    Matches :func:`write_budget`'s comment/key-ordering preservation on every
    other section.
    """

    def _mutate(block: dict[str, object]) -> None:
        block["enabled"] = bool(timelapse.enabled)
        block["installed"] = bool(timelapse.installed)

    return _patch_yaml_key(yaml_text, "timelapse", _mutate)
