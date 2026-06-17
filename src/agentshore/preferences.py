"""Machine-global user preferences.

A single user-level ``preferences.yaml`` (``GLOBAL_PREFERENCES_PATH``, sibling to
``pricing.yaml`` / ``availability.yaml``) holds options that are the same
regardless of which repository a session runs against — currently the set of
non-critical plays the user has turned off. The file is folded into every
project's :class:`~agentshore.config.models.RuntimeConfig` at load time, so a
config reload (SIGHUP or the IPC reload-config) re-reads it and a change takes
effect mid-session. There is deliberately no per-project preferences file.

Play disabling is constrained to :data:`USER_DISABLEABLE_PLAYS` — a curated
allowlist of plays that are genuinely optional to issue delivery. Lifecycle,
delivery, and self-heal plays are never user-disableable, so a preference can
never wedge the orchestrator. The allowlist is enforced both at the write
boundary (CLI / sidecar RPC reject anything outside it) and defensively at the
mask, so a hand-edited file still cannot disable a critical play.
"""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING, Final

import yaml

from agentshore.paths import GLOBAL_PREFERENCES_PATH
from agentshore.state import PlayType

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

# Curated allowlist of plays a user may disable. Every member is optional to
# issue delivery; disabling all of them cannot stall PR throughput. Keep this in
# lockstep with the documented Preferences surface — do NOT add delivery /
# lifecycle / self-heal plays (issue_pickup, code_review, merge_pr,
# reconcile_state, end_session, …).
USER_DISABLEABLE_PLAYS: Final[frozenset[PlayType]] = frozenset(
    {
        PlayType.RUN_QA,
        PlayType.CLEANUP,
        PlayType.PRUNE,
        PlayType.DESIGN_AUDIT,
        PlayType.GROOM_BACKLOG,
    }
)


class PreferencesError(ValueError):
    """A preference value was rejected at the write boundary."""


def disableable_play_values() -> tuple[str, ...]:
    """Return the allowlisted play values, in a stable display order."""
    return tuple(sorted(p.value for p in USER_DISABLEABLE_PLAYS))


def validate_disabled_plays(values: Iterable[object]) -> tuple[str, ...]:
    """Strictly validate *values* against the allowlist for a write.

    Returns a de-duplicated, stably-ordered tuple of play values. Raises
    :class:`PreferencesError` (with the offending entries and the allowed set)
    if any entry is not an allowlisted play — used by the CLI and the sidecar
    ``preferences.set`` RPC so a bad request fails loudly rather than silently
    dropping a play.
    """
    allowed = {p.value for p in USER_DISABLEABLE_PLAYS}
    seen: set[str] = set()
    bad: list[str] = []
    for value in values:
        text = str(value).strip()
        if text in allowed:
            seen.add(text)
        else:
            bad.append(text)
    if bad:
        raise PreferencesError(
            "not user-disableable: "
            + ", ".join(sorted(set(bad)))
            + f" (allowed: {', '.join(disableable_play_values())})"
        )
    return tuple(v for v in disableable_play_values() if v in seen)


def _coerce_disabled_plays(values: object) -> tuple[str, ...]:
    """Leniently coerce an untrusted on-disk value to allowlisted play values.

    Unknown / non-allowlisted / malformed entries are dropped rather than
    raising — the file is hand-editable and may have been written by an older or
    newer build. Strict rejection lives at the write boundary
    (:func:`validate_disabled_plays`).
    """
    if not isinstance(values, (list, tuple)):
        return ()
    allowed = {p.value for p in USER_DISABLEABLE_PLAYS}
    seen = {str(v).strip() for v in values if str(v).strip() in allowed}
    return tuple(v for v in disableable_play_values() if v in seen)


def load_preferences_data(path: Path | None = None) -> dict[str, tuple[str, ...]]:
    """Read the global preferences file into a normalized dict.

    Returns sane defaults on a missing or malformed file (the orchestrator must
    start regardless of preference-file state). The returned shape is the merge
    input consumed by :func:`agentshore.config.load_config`. ``path`` resolves
    to :data:`GLOBAL_PREFERENCES_PATH` when ``None`` (resolved at call time so
    tests can redirect the global path).
    """
    path = path or GLOBAL_PREFERENCES_PATH
    raw: object = None
    try:
        if path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raw = None
    data = raw if isinstance(raw, dict) else {}
    plays = data.get("plays") if isinstance(data.get("plays"), dict) else {}
    disabled = plays.get("disabled") if isinstance(plays, dict) else None
    return {"disabled_plays": _coerce_disabled_plays(disabled)}


def save_preferences_data(data: dict[str, object], path: Path | None = None) -> None:
    """Atomically write the global preferences file from a normalized dict.

    Mirrors the disk shape ``load_preferences_data`` reads. Atomic
    (temp + fsync + replace) so a concurrent reload never observes a torn file.
    ``path`` resolves to :data:`GLOBAL_PREFERENCES_PATH` when ``None``.
    """
    path = path or GLOBAL_PREFERENCES_PATH
    disabled = _coerce_disabled_plays(data.get("disabled_plays"))
    document: dict[str, object] = {"plays": {"disabled": list(disabled)}}
    text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".preferences-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        with __import__("contextlib").suppress(OSError):
            os.unlink(tmp)
        raise
