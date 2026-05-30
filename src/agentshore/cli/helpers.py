"""Shared CLI helpers (used across multiple subcommands).

Distinct from :mod:`agentshore.cli_helpers`, which holds project-setup utilities
(`_find_repo_root`, `_detect_agents`, `_generate_default_config`, etc.) used by
``agentshore init`` and tests. This module hosts orchestration-time helpers
shared between command bodies.
"""

from __future__ import annotations

import asyncio
import shutil
import signal
from pathlib import Path
from typing import TYPE_CHECKING

import click
import structlog

from agentshore.cli.constants import (
    _DRAIN_WAIT_TIMEOUT_S,
    _START_MODE_AGENT,
    _START_MODE_TUI,
)
from agentshore.config.models import PolicyMode, RunMode

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from agentshore.agents.identity import RepoAccessStatus

_logger = structlog.get_logger(__name__)


def _check_ssh_signing_key_loaded() -> tuple[bool, str]:
    """Run ``ssh-add -l`` and report whether at least one key is loaded.

    Returns (loaded, detail). desktop-l7i: when commit signing is configured
    (typical AgentShore setup uses SSH-signed commits via the macOS keychain),
    a fresh terminal session can have an empty ssh-agent — and the resulting
    ``git commit`` failures during merge_pr plays are unrecoverable mid-run
    (subprocess can't satisfy the keychain passphrase prompt).

    A non-zero exit from ssh-add typically means "no identities" (exit 1) or
    "cannot connect to authentication agent" (exit 2). We treat both as
    "not loaded" and let the caller decide whether to warn or refuse.
    """
    import subprocess

    ssh_add = shutil.which("ssh-add")
    if ssh_add is None:
        return False, "ssh-add not found on PATH"
    try:
        result = subprocess.run(  # nosec B603
            [ssh_add, "-l"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"ssh-add probe failed: {exc}"
    if result.returncode != 0:
        # exit 1 = no identities; exit 2 = agent unreachable
        return False, result.stderr.strip() or "no identities loaded"
    first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    return True, first_line


def _echo_repo_access_rows(repo_access_rows: Sequence[RepoAccessStatus]) -> None:
    """Pretty-print repository access preflight rows."""

    if not repo_access_rows:
        return
    click.echo()
    click.echo("Repository access")
    click.echo("─────────────────")
    width = max(len(row.agent_key) for row in repo_access_rows)
    for row in repo_access_rows:
        identity = row.identity_name or "(no identity)"
        if row.ok:
            click.echo(f"  {row.agent_key:<{width}}  →  {identity}  [repo: ok]")
        else:
            detail = " ".join(row.detail.split())
            click.echo(f"  {row.agent_key:<{width}}  →  {identity}  [repo: BLOCKED — {detail}]")


def _drain_wait_timeout_label() -> str:
    minutes = int(_DRAIN_WAIT_TIMEOUT_S / 60)
    if minutes * 60 == _DRAIN_WAIT_TIMEOUT_S:
        return f"{minutes} min"
    return f"{_DRAIN_WAIT_TIMEOUT_S:.0f}s"


def _str_or_none(d: dict[str, object], key: str) -> str | None:
    """Narrow ``d.get(key)`` to ``str | None``.

    Returns ``None`` when the key is absent or the value is ``None``; otherwise
    coerces the value to ``str``. Centralises the ``Any``-coercion at a single,
    testable boundary so callers stay free of ``# type: ignore`` suppressions.
    """
    value = d.get(key)
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _int_or_none(d: dict[str, object], key: str) -> int | None:
    """Narrow ``d.get(key)`` to ``int | None``.

    Returns ``None`` when the key is absent or the value is ``None``. Accepts
    ``int`` values as-is and attempts to coerce other values via ``int(...)``;
    a ``ValueError`` or ``TypeError`` from coercion is propagated to the caller.
    """
    value = d.get(key)
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"Cannot coerce value of type {type(value).__name__} for key {key!r} to int")


def _resolve_policy_mode_override(
    *, policy_mode: str | None, legacy_deterministic: bool
) -> PolicyMode | None:
    if legacy_deterministic:
        click.echo(
            "Warning: --deterministic is deprecated; use --policy-mode audit-replay.",
            err=True,
        )
    if policy_mode is not None:
        resolved = PolicyMode(policy_mode)
        if legacy_deterministic and resolved != PolicyMode.AUDIT_REPLAY:
            raise click.BadParameter(
                "--deterministic conflicts with --policy-mode learning",
                param_hint="--policy-mode",
            )
        return resolved
    if legacy_deterministic:
        return PolicyMode.AUDIT_REPLAY
    return None


def _resolve_start_run_mode(
    mode: str,
    *,
    tui: bool,
    dashboard: bool,
    headless: bool,
) -> RunMode:
    """Resolve public start-mode flags to AgentShore's internal run modes."""
    if tui and mode == _START_MODE_AGENT:
        raise click.UsageError("--tui cannot be combined with --mode agent")
    if tui and dashboard:
        raise click.UsageError("--tui cannot be combined with --dashboard")
    if tui and headless:
        raise click.UsageError("--tui cannot be combined with --headless")

    run_mode = RunMode.AGENT if mode == _START_MODE_AGENT else RunMode.SOLO
    if dashboard and run_mode == RunMode.SOLO:
        run_mode = RunMode.AGENT
    return run_mode


def _display_run_mode(run_mode: RunMode) -> str:
    """Return the user-facing label for a resolved start mode."""
    return _START_MODE_TUI if run_mode == RunMode.SOLO else run_mode.value


def _track_background_task(
    tasks: set[asyncio.Task[None]],
    coro: Coroutine[None, None, None],
    *,
    name: str,
) -> asyncio.Task[None]:
    """Create a task and log any exception it raises."""
    task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
    tasks.add(task)

    def _on_done(done: asyncio.Task[None]) -> None:
        tasks.discard(done)
        if done.cancelled():
            return
        try:
            exc = done.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            _logger.error("background_task_failed", task=name, error=str(exc))

    task.add_done_callback(_on_done)
    return task


def _install_loop_signal_handler(
    loop: asyncio.AbstractEventLoop,
    sig: signal.Signals,
    callback: Callable[[], object],
) -> None:
    """Install an asyncio signal handler with a Windows-compatible fallback."""
    try:
        loop.add_signal_handler(sig, callback)
        return
    except (NotImplementedError, RuntimeError):
        pass

    def _handler(_signum: int, _frame: object | None) -> None:
        loop.call_soon_threadsafe(callback)

    try:
        signal.signal(sig, _handler)
    except (ValueError, OSError):
        _logger.warning("signal_handler_unavailable", signal=str(sig))


def _prepare_session_discovery_paths(
    *,
    well_known_socket: Path,
    ipc_endpoint: object,
    allow_metadata_failure: bool,
) -> bool:
    """Create global session metadata paths when available.

    Windows runs use TCP IPC, so the global ``~/.config/swink/agentshore/sessions`` metadata is
    useful for discovery but not required to bind the control channel. Unix
    socket runs still need these directories before the socket server starts.
    """
    try:
        well_known_socket.parent.mkdir(parents=True, exist_ok=True)
        if getattr(ipc_endpoint, "kind", None) == "unix":
            raw_path = getattr(ipc_endpoint, "path", None)
            if raw_path is not None:
                explicit = Path(raw_path)
                explicit.parent.mkdir(parents=True, exist_ok=True)
                if explicit.resolve() != well_known_socket.resolve():
                    try:
                        if well_known_socket.exists() or well_known_socket.is_symlink():
                            well_known_socket.unlink()
                        well_known_socket.symlink_to(explicit.resolve())
                    except OSError:
                        pass
        return True
    except OSError as exc:
        if allow_metadata_failure:
            click.echo(f"Warning: session metadata unavailable: {exc}", err=True)
            return False
        click.echo(f"Error: could not prepare IPC path: {exc}", err=True)
        raise SystemExit(1) from exc
