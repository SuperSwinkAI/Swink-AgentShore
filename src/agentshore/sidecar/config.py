"""Atomic agentshore.yaml read/write helpers for the sidecar.

``read_config`` returns the raw YAML text plus a parsed dict.
``write_config`` deep-merges a patch dict into the existing file and writes
atomically (temp file in the same directory → fsync → os.replace) so the
shell never sees a partially-written config.

Deep-merge semantics:
- Nested mappings merge key-by-key.
- Non-mapping values overwrite (including lists — lists are scalar replacements,
  not concatenations).
- Setting a key to ``None`` in the patch removes it from the merged result,
  giving the shell a single method to clear fields.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path

_CONFIG_FILENAME = "agentshore.yaml"


def read_config(project_path: Path) -> dict[str, object]:
    """Return ``{raw: str, parsed: dict}`` for ``<project_path>/agentshore.yaml``.

    If the file is absent, returns ``{"raw": "", "parsed": {}}``.
    """
    config_path = project_path / _CONFIG_FILENAME
    if not config_path.exists():
        return {"raw": "", "parsed": {}}
    raw = config_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw)
    if parsed is None:
        parsed = {}
    return {"raw": raw, "parsed": parsed}


def write_config(project_path: Path, patch: dict[str, object]) -> None:
    """Deep-merge *patch* into ``<project_path>/agentshore.yaml`` and write atomically.

    Raises ``TypeError`` if *patch* is not a mapping.
    """
    if not isinstance(patch, dict):
        raise TypeError(f"patch must be a dict, got {type(patch).__name__!r}")

    config_path = project_path / _CONFIG_FILENAME
    if config_path.exists():
        existing_raw = config_path.read_text(encoding="utf-8")
        existing: dict[str, object] = yaml.safe_load(existing_raw) or {}
    else:
        existing = {}

    merged = _deep_merge(existing, patch)
    text = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True)

    fd, tmp_path = tempfile.mkstemp(dir=project_path, prefix=".agentshore_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, config_path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _deep_merge(base: dict[str, object], patch: dict[str, object]) -> dict[str, object]:
    """Return a new dict that is *base* deep-merged with *patch*.

    Keys whose patched value is ``None`` are removed from the result.
    """
    result: dict[str, object] = dict(base)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)  # type: ignore[arg-type]
        else:
            result[key] = value
    return result
