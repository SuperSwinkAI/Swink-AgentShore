"""Persistence helpers for the ``preferences.*`` sidecar RPCs.

These read/write the machine-global ``preferences.yaml`` (not a project file),
mirroring the role :mod:`agentshore.sidecar.config` plays for ``agentshore.yaml``.
The dispatcher lives in :mod:`agentshore.sidecar.server`; the live-reload of an
active session is triggered there via the orchestrator handle.
"""

from __future__ import annotations

from agentshore.preferences import (
    disableable_play_values,
    load_preferences_data,
    save_preferences_data,
    validate_disabled_plays,
)


def get_preferences() -> dict[str, object]:
    """Return the current global preferences plus the disableable-play menu.

    ``disableable`` is included so the Desktop pane / CLI can render the full
    allowlist (with checkmarks for the disabled subset) without a second call.
    """
    data = load_preferences_data()
    disabled = data.get("disabled_plays", ())
    return {
        "disabled_plays": list(disabled),
        "disableable_plays": list(disableable_play_values()),
    }


def set_preferences(disabled_plays: object) -> dict[str, object]:
    """Validate + persist the disabled-play set, returning the new view.

    Raises :class:`agentshore.preferences.PreferencesError` if any entry is not
    an allowlisted play — the dispatcher maps that to an INVALID_PARAMS error.
    """
    if not isinstance(disabled_plays, (list, tuple)):
        from agentshore.preferences import PreferencesError

        raise PreferencesError("disabled_plays must be an array of play names")
    validated = validate_disabled_plays(disabled_plays)
    save_preferences_data({"disabled_plays": validated})
    return get_preferences()
