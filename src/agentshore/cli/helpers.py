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
from contextlib import asynccontextmanager
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
from agentshore.seed_input import SeedInputError, resolve_seed_input

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine

    from agentshore.data.store import DataStore

_logger = structlog.get_logger(__name__)


@asynccontextmanager
async def open_store(db_path: Path) -> AsyncIterator[DataStore]:
    """Open an initialised :class:`DataStore` for *db_path*, closing on exit.

    Raises :class:`click.ClickException` (exit code 1, ``Error: …`` on stderr)
    when no database exists at *db_path*, so the DB-backed read commands
    (``archive``, ``report``, ``stop``, ``train``) share one existence-check
    and one guaranteed-``close`` lifecycle instead of hand-rolling the same
    try/finally per ``asyncio.run`` site.
    """
    if not db_path.exists():
        raise click.ClickException(f"No database found at {db_path}")
    from agentshore.data.store import DataStore

    store = DataStore(db_path)
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


async def resolve_session_id(store: DataStore, explicit: str | None) -> str:
    """Return *explicit* if given, else the most recent session in *store*.

    Raises :class:`click.ClickException` when no explicit id is given and the
    store holds no sessions. Centralises the "default to last session" rule
    shared by ``archive create``, ``report``, and ``stop``'s ESR generation.
    """
    if explicit is not None:
        return explicit
    sessions = await store.list_sessions()
    if not sessions:
        raise click.ClickException("No sessions found.")
    return sessions[0].session_id


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


def _drain_wait_timeout_label() -> str:
    minutes = int(_DRAIN_WAIT_TIMEOUT_S / 60)
    if minutes * 60 == _DRAIN_WAIT_TIMEOUT_S:
        return f"{minutes} min"
    return f"{_DRAIN_WAIT_TIMEOUT_S:.0f}s"


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


def _resolve_seed_input_path(seed: str, repo_root: Path) -> tuple[Path, str]:
    """Resolve --seed to a file path, expanding directories into a capped bundle.

    Thin CLI wrapper over :func:`agentshore.seed_input.resolve_seed_input`;
    converts :class:`SeedInputError` to ``click.BadParameter`` so usage errors
    surface with the ``--seed`` hint.
    """
    try:
        return resolve_seed_input(seed, repo_root)
    except SeedInputError as exc:
        raise click.BadParameter(str(exc), param_hint="--seed") from exc


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
