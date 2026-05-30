"""Recents store for the desktop sidecar.

Persists AgentShore-domain project metadata at
``platformdirs.user_data_dir("agentshore")/recents.json`` per
``docs/design/desktop/DESIGN.md`` §4.2, so the recents list survives
restarts and is reusable by future AgentShore frontends.

The store is a small JSON document — the operations are infrequent
(opening or closing a project), so we re-read and re-write the whole
file. Writes go through a temp file and ``os.replace`` for crash
safety, matching the pattern in ``agentshore.agents.context_writer``.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

import yaml
from platformdirs import user_data_dir

STORE_VERSION = 1


class RecentEntry(TypedDict):
    path: str
    label: str
    last_started: str
    last_exit_reason: str | None
    has_valid_config: bool


def _has_valid_config(project_path: str) -> bool:
    """Return True when ``<project_path>/agentshore.yaml`` parses as a mapping
    with a ``project`` key.

    Used to drive the desktop "Ready" badge per DESIGN.md §10.1. The check
    is intentionally tolerant: any I/O error, parse error, or unexpected
    shape returns ``False`` so the badge degrades to "Known" instead of
    crashing the recents list. The result is recomputed on every read so
    the badge reflects the current disk state.
    """
    candidate = Path(project_path) / "agentshore.yaml"
    try:
        with candidate.open("r", encoding="utf-8") as fh:
            parsed = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(parsed, dict) and "project" in parsed


def recents_path() -> Path:
    """Return the canonical recents.json location for this OS."""
    return Path(user_data_dir("agentshore")) / "recents.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize(path: str) -> str:
    return str(Path(path).expanduser())


def _coerce_entry(raw: object) -> RecentEntry | None:
    if not isinstance(raw, dict):
        return None
    path = raw.get("path")
    label = raw.get("label")
    last_started = raw.get("last_started")
    last_exit_reason = raw.get("last_exit_reason")
    if not (isinstance(path, str) and isinstance(label, str) and isinstance(last_started, str)):
        return None
    if last_exit_reason is not None and not isinstance(last_exit_reason, str):
        last_exit_reason = None
    return {
        "path": path,
        "label": label,
        "last_started": last_started,
        "last_exit_reason": last_exit_reason,
        "has_valid_config": _has_valid_config(path),
    }


def _read_store(store_path: Path) -> list[RecentEntry]:
    if not store_path.is_file():
        return []
    try:
        raw = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    entries = raw.get("entries")
    if not isinstance(entries, list):
        return []
    out: list[RecentEntry] = []
    for item in entries:
        entry = _coerce_entry(item)
        if entry is not None:
            out.append(entry)
    return out


def _write_store(store_path: Path, entries: list[RecentEntry]) -> None:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=store_path.parent, prefix=".recents_", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump({"version": STORE_VERSION, "entries": entries}, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, store_path)
    except (OSError, TypeError, ValueError):
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def list_recents(store_path: Path | None = None) -> list[RecentEntry]:
    """Return all recent entries, newest first."""
    target = store_path or recents_path()
    entries = _read_store(target)
    entries.sort(key=lambda e: e["last_started"], reverse=True)
    return entries


def touch_recent(path: str, store_path: Path | None = None) -> None:
    """Insert or refresh the entry for *path*.

    Sets ``last_started`` to now and computes ``label`` from the path's
    basename when adding. Preserves ``last_exit_reason`` on existing
    entries so it survives a fresh ``recents.touch`` between sessions.
    """
    target = store_path or recents_path()
    normalized = _normalize(path)
    entries = _read_store(target)
    now = _now_iso()
    basename = Path(normalized).name or normalized
    for entry in entries:
        if entry["path"] == normalized:
            # Keep label stable so user-visible names don't flicker.
            entry["last_started"] = now
            _write_store(target, entries)
            return
    entries.append(
        {
            "path": normalized,
            "label": basename,
            "last_started": now,
            "last_exit_reason": None,
            "has_valid_config": _has_valid_config(normalized),
        }
    )
    _write_store(target, entries)


def remove_recent(path: str, store_path: Path | None = None) -> None:
    """Drop the entry for *path*. No-op when the entry is absent."""
    target = store_path or recents_path()
    normalized = _normalize(path)
    entries = _read_store(target)
    filtered = [entry for entry in entries if entry["path"] != normalized]
    if len(filtered) == len(entries):
        return
    _write_store(target, filtered)
