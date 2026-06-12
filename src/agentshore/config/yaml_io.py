"""Comment-preserving ``agentshore.yaml`` round-trip helpers.

These wrap the ruamel.yaml round-trip boilerplate (``preserve_quotes``, load
existing or start fresh, dump back to the same path) so the CLI ``init`` wizard
prompts that persist a single nested key — e.g. ``project.target_branch`` —
share one implementation instead of re-spelling the same load/mutate/dump
dance per key.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def ruamel_set_nested(config_path: Path, keys: Sequence[str], value: object) -> None:
    """Set the nested *keys* path to *value* in *config_path*, preserving comments.

    Loads *config_path* (treating a missing or blank file as an empty mapping),
    walks/creates the intermediate mappings named by *keys*, assigns *value* to
    the final key, and writes the document back. Comments and key ordering on
    untouched entries survive the round-trip. *keys* must be non-empty.
    """
    from ruamel.yaml import YAML

    if not keys:
        msg = "keys must contain at least one key"
        raise ValueError(msg)

    rt = YAML()
    rt.preserve_quotes = True
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    data = rt.load(existing) if existing.strip() else None
    if data is None:
        data = {}

    node = data
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[keys[-1]] = value

    buf = io.StringIO()
    rt.dump(data, buf)
    config_path.write_text(buf.getvalue(), encoding="utf-8")


def ruamel_get_nested(config_path: Path, keys: Sequence[str]) -> object | None:
    """Return the value at the nested *keys* path in *config_path*, or ``None``.

    Returns ``None`` when the file is missing, unparseable, not a mapping, or
    any key along *keys* is absent or maps to a non-mapping intermediate.
    """
    if not config_path.exists():
        return None
    from ruamel.yaml import YAML

    rt = YAML()
    rt.preserve_quotes = True
    try:
        data = rt.load(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return None
    node: object = data
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node
