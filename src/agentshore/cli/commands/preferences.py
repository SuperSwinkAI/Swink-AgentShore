"""``agentshore preferences`` subcommand group.

Reads/writes the machine-global ``preferences.yaml`` (sibling to ``pricing.yaml``
under the user config dir) — the same file the Desktop Preferences pane and the
``preferences.*`` sidecar RPCs use. These options are global, not per-project, so
this command takes no ``--project``. A running session picks up a change on its
next config reload (``agentshore reload-config`` or SIGHUP); the Desktop toggle
applies live.
"""

from __future__ import annotations

import click

from agentshore.preferences import (
    PreferencesError,
    disableable_play_values,
    load_preferences_data,
    save_preferences_data,
    validate_disabled_plays,
)


def _current_disabled() -> tuple[str, ...]:
    data = load_preferences_data()
    disabled = data.get("disabled_plays", ())
    return tuple(disabled) if isinstance(disabled, (list, tuple)) else ()


def _print_status() -> None:
    disabled = set(_current_disabled())
    click.echo("Disableable plays (global preferences):")
    for play in disableable_play_values():
        mark = "off" if play in disabled else "on "
        click.echo(f"  [{mark}] {play}")


@click.group()
def preferences() -> None:
    """View and edit machine-global AgentShore preferences."""


@preferences.command("list")
def list_cmd() -> None:
    """Show every disableable play and whether it is on or off."""
    _print_status()


@preferences.command("disable")
@click.argument("plays", nargs=-1, required=True)
def disable_cmd(plays: tuple[str, ...]) -> None:
    """Disable one or more non-critical plays (e.g. run_qa cleanup)."""
    try:
        requested = validate_disabled_plays(plays)
    except PreferencesError as exc:
        raise click.ClickException(str(exc)) from exc
    merged = set(_current_disabled()) | set(requested)
    save_preferences_data({"disabled_plays": validate_disabled_plays(merged)})
    _print_status()


@preferences.command("enable")
@click.argument("plays", nargs=-1, required=True)
def enable_cmd(plays: tuple[str, ...]) -> None:
    """Re-enable one or more previously disabled plays."""
    try:
        requested = validate_disabled_plays(plays)
    except PreferencesError as exc:
        raise click.ClickException(str(exc)) from exc
    remaining = set(_current_disabled()) - set(requested)
    save_preferences_data({"disabled_plays": validate_disabled_plays(remaining)})
    _print_status()


@preferences.command("reset")
def reset_cmd() -> None:
    """Re-enable all plays (clear the disabled set)."""
    save_preferences_data({"disabled_plays": ()})
    _print_status()
