"""bd subprocess core: reader/writer lock, binary resolution, and ``bd()``.

Every other beads module funnels its ``bd`` CLI calls through this module's
``bd()`` coroutine, which serialises writes against reads via
``_ReadersWriterLock`` (C5) and classifies subprocess failures into
``BdError`` / ``BdTimeoutError`` / ``BeadsSchemaDriftError``.
``resolve_bd_binary()`` and the agent-dispatch PATH shim
(``ensure_bd_on_agent_path``) keep AgentShore's own writes and agent-driven
CLI ``bd`` calls pointed at the same binary.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

from agentshore.command import CommandTimeoutError, run_command
from agentshore.logging import get_logger

_logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# bd subcommands that only read the store. Everything else is treated as a
# mutation for both locking (write-exclusive) and graph-cache invalidation.
# ``dep`` covers read-only subcommands like ``bd dep cycles``; this codebase
# never calls a mutating ``bd dep add``/``bd dep remove`` today, so keying
# off the first arg alone is safe — revisit if that changes.
# ---------------------------------------------------------------------------

READ_COMMANDS: frozenset[str] = frozenset(
    {"list", "query", "ready", "show", "dep", "stats", "export", "--version"}
)


class _ReadersWriterLock:
    """Writer-preferring reader/writer lock guarding bd subprocess calls (C5).

    Reads (``bd list`` / ``bd query`` / ...) run concurrently with each
    other — verified empirically against a live bd 1.1.0 embedded store:
    four concurrent ``bd list --all --json --limit 0`` processes all exited
    0 with identical output. External agent processes already read the same
    store concurrently today; this only stops AgentShore's own reads from
    queuing behind each other. A write acquires exclusive access against
    both reads and other writes, preserving the old single-lock
    serialisation guarantee for mutations. Writer-preferring: once a writer
    is waiting, newly arriving readers queue behind it so a steady stream of
    reads cannot starve a pending write indefinitely.
    """

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._active_readers = 0
        self._active_writer = False
        self._waiting_writers = 0

    @contextlib.asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._cond:
            while self._active_writer or self._waiting_writers > 0:
                await self._cond.wait()
            self._active_readers += 1
        try:
            yield
        finally:
            async with self._cond:
                self._active_readers -= 1
                if self._active_readers == 0:
                    self._cond.notify_all()

    @contextlib.asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        async with self._cond:
            self._waiting_writers += 1
            try:
                while self._active_writer or self._active_readers > 0:
                    await self._cond.wait()
                self._active_writer = True
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            async with self._cond:
                self._active_writer = False
                self._cond.notify_all()


_BD_LOCK = _ReadersWriterLock()
BD_TIMEOUT_SECONDS = 120.0


class BdError(RuntimeError):
    """Raised when a bd subcommand exits with a non-zero return code."""


class BdTimeoutError(BdError):
    """Raised when a bd subcommand exceeds its timeout.

    Distinct from the generic ``BdError`` so callers can tell a "too big / too
    slow" timeout apart from a transient, retry-worthy failure (e.g. lock
    contention). Retrying a timeout cannot help — the command was already given
    its full budget — so the graph reader fails fast on this rather than
    re-paying the timeout N times (#237).
    """


class GraphReadError(BdError):
    """Raised when load_graph exhausts all retries and cannot return a fresh graph.

    Callers must handle this explicitly; returning stale data silently is not
    acceptable because it hides permanent failures (uninstalled bd binary,
    corrupted store, wedged lock) from the RL loop.
    """


# Stable coordination-decision markers from bd's own "refusing to auto-apply
# ... to a remote-backed database" guard (bd issue #4259). A clone stuck
# behind its remote's schema surfaces this on every read/write until either
# the designated migrator runs `bd migrate` and pushes, or a non-designated
# clone runs `bd bootstrap` to catch up without forking the schema. Matched
# against the stable coordination-decision text rather than the downstream
# "column ... could not be found"-style error, which varies by command and
# column and isn't guaranteed stable across bd releases. Lives here (rather
# than in beads/setup.py, the only prior user) so the low-level subprocess
# and retry layer can classify a schema-drift failure without a circular
# import — setup.py imports this instead of keeping its own copy.
SCHEMA_DRIFT_MARKERS = ("refusing to auto-apply", "remote-backed", "#4259")


def is_schema_drift_error(error_text: str) -> bool:
    """True when *error_text* carries bd's schema-drift/remote-migration-gate signature."""
    return any(marker in error_text for marker in SCHEMA_DRIFT_MARKERS)


class BeadsSchemaDriftError(GraphReadError):
    """Raised when bd refuses to read/write because this clone's schema has
    drifted from a shared remote-backed store (bd's #4259 coordination guard).

    A ``GraphReadError`` subclass — not a plain sibling — on purpose: every
    existing ``except GraphReadError`` call site in the codebase (per-tick
    alignment reload, issue sync, the RL selector's live-drift confirm, the
    reports collector) already treats a failed read as "beads temporarily
    unavailable, degrade gracefully" — the correct behavior for schema drift
    too, everywhere except the one place that used the failure to decide
    "the graph is empty, so seed it". Subclassing means those call sites need
    no changes at all: they keep working exactly as before. Only the
    bootstrap path (``agentshore.core.orchestrator`` /
    ``agentshore.core.phases``) adds a specific ``except
    BeadsSchemaDriftError`` *before* its ``except GraphReadError``, so it can
    tell "unreadable" apart from "genuinely empty" instead of collapsing both
    into ``graph_has_epics=False`` and silently re-running the seed-project
    bootstrap play over a project that already had real epics/tasks — the
    live bug this type exists to prevent. See
    ``agentshore.beads.setup.reconcile_beads_schema`` for the preflight that
    heals what it safely can before any of this is ever reached.
    """


def resolve_bd_binary() -> str | None:
    """Resolve the bd binary path from env override first, then PATH."""
    env_value = os.environ.get("AGENTSHORE_BD_BIN")
    if env_value:
        env_path = Path(env_value)
        if env_path.is_file() and os.access(env_path, os.X_OK):
            return str(env_path.resolve())
        _logger.warning("agentshore_bd_bin_invalid", env_path=env_value)
    return shutil.which("bd")


def _bd_shim_dir() -> Path:
    """Per-user cache dir for the agent-dispatch bd shim (see ``ensure_bd_on_agent_path``)."""
    import platformdirs

    return Path(platformdirs.user_cache_dir("agentshore", "agentshore")) / "bd-agent-shim"


def _write_bd_shim(shim_path: Path, bd_binary: str) -> None:
    """Create/refresh the shim at *shim_path* so bare ``bd`` resolves to *bd_binary*.

    POSIX: a symlink (falls back to a copy if the filesystem rejects symlinks,
    e.g. some network/FAT mounts). Windows: a batch wrapper, since ``bd``
    resolution there goes through PATHEXT and symlinks need Developer Mode or
    admin privileges that can't be assumed.
    """
    if sys.platform == "win32":
        wrapper = f'@echo off\r\n"{bd_binary}" %*\r\n'
        if shim_path.is_file() and shim_path.read_text(encoding="utf-8") == wrapper:
            return
        shim_path.write_text(wrapper, encoding="utf-8")
        return

    if shim_path.is_symlink() and os.readlink(shim_path) == bd_binary:
        return
    with contextlib.suppress(FileNotFoundError):
        shim_path.unlink()
    try:
        os.symlink(bd_binary, shim_path)
    except OSError:
        shutil.copy2(bd_binary, shim_path)
        shim_path.chmod(0o755)


def ensure_bd_on_agent_path(env: dict[str, str]) -> dict[str, str]:
    """Pin ``bd`` on *env*'s ``PATH`` to the same binary the orchestrator uses.

    Agent-dispatched subprocesses (skill templates instruct Claude Code, Codex,
    Grok, and Antigravity to run literal ``bd ...`` commands) resolve ``bd``
    from their own inherited ``PATH`` — independently of
    ``resolve_bd_binary()``, which every one of AgentShore's *own* writes goes
    through. When the two disagree (e.g. the desktop app pins a bundled
    sidecar bd via ``AGENTSHORE_BD_BIN`` while the user's ambient ``PATH``
    resolves a different, older standalone install), an agent's literal ``bd``
    calls silently run a version-skewed binary against the same embedded Dolt
    store the orchestrator just wrote with a different version — schema
    migrations between bd releases can then make agent-side writes fail (or
    worse, corrupt the store).

    If bare ``bd`` already resolves to the same file as ``resolve_bd_binary()``
    under *env*'s ``PATH``, *env* is returned unchanged. Otherwise a small,
    reusable shim directory containing a ``bd``/``bd.cmd`` pointing at the
    resolved binary is created/refreshed and prepended to ``PATH`` so it wins
    resolution ahead of any homebrew/user install. Best-effort: any failure to
    create the shim leaves *env* unchanged rather than breaking dispatch.
    """
    bd_binary = resolve_bd_binary()
    if bd_binary is None:
        return env

    path_value = env.get("PATH", "")
    on_path = shutil.which("bd", path=path_value)
    if on_path is not None:
        with contextlib.suppress(OSError):
            if os.path.samefile(on_path, bd_binary):
                return env

    try:
        shim_dir = _bd_shim_dir()
        shim_dir.mkdir(parents=True, exist_ok=True)
        shim_path = shim_dir / ("bd.cmd" if sys.platform == "win32" else "bd")
        _write_bd_shim(shim_path, bd_binary)
    except OSError:
        _logger.warning("bd_shim_create_failed", bd_binary=bd_binary)
        return env

    new_env = dict(env)
    new_env["PATH"] = str(shim_dir) + os.pathsep + path_value
    return new_env


async def bd(
    *args: str,
    cwd: Path,
    stdin_data: bytes | None = None,
    timeout_seconds: float = BD_TIMEOUT_SECONDS,
    env_overlay: Mapping[str, str] | None = None,
) -> str:
    """Run a bd subcommand in *cwd* and return stdout as a string.

    Raises ``BdTimeoutError`` when the command exceeds *timeout_seconds* and
    ``BdError`` on any other failure (non-zero exit, OSError, missing binary).

    Reads (first arg in ``READ_COMMANDS``) take ``_BD_LOCK``'s reader side
    and run concurrently with each other; anything else is a write and takes
    the exclusive writer side (C5). A successful mutation also drops any
    cached graph snapshot for *cwd* so the next ``load_graph`` call re-reads
    instead of serving data that predates this write.

    *env_overlay* merges on top of the current process environment for this
    call only (e.g. ``{"BD_ALLOW_REMOTE_MIGRATE": "1"}`` for the one-shot,
    consent-gated schema-migration command in ``beads.setup`` — never set as
    ambient process/session env, since that would leave the dangerous flag
    live for every subsequent bd call).
    """
    bd_binary = resolve_bd_binary()
    if bd_binary is None:
        raise BdError("bd binary not found; set AGENTSHORE_BD_BIN or install bd on PATH")

    is_read = bool(args) and args[0] in READ_COMMANDS
    lock_cm = _BD_LOCK.read() if is_read else _BD_LOCK.write()
    async with lock_cm:
        try:
            result = await run_command(
                bd_binary,
                *args,
                cwd=cwd,
                stdin_data=stdin_data,
                timeout_seconds=timeout_seconds,
                resolve_executable=False,
                env={**os.environ, **env_overlay} if env_overlay else None,
            )
        except CommandTimeoutError as exc:
            raise BdTimeoutError(f"bd {' '.join(args)} timed out: {exc}") from exc
        except OSError as exc:
            raise BdError(f"bd {' '.join(args)} failed: {exc}") from exc
    if result.returncode != 0:
        raise BdError(
            f"bd {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    if not is_read:
        # Deferred import: graph.py imports ``bd`` from this module, so a
        # top-level import here would be circular. This only runs after a
        # successful write, well after both modules have finished loading.
        from agentshore.beads.graph import _invalidate_graph_cache  # noqa: PLC0415

        await _invalidate_graph_cache(cwd)
    return result.stdout
