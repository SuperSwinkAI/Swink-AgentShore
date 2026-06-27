"""ConPTY-backed spawn for the Antigravity CLI (``agy``) on Windows.

``agy`` in print mode (``-p``) writes a terminal **Device-Attributes query**
(``ESC[c``) on startup and then *blocks waiting for the terminal's reply*
before it produces any output. Over plain pipes — how AgentShore spawns every
agent — no reply ever comes, so ``agy`` deadlocks and emits zero bytes. That is
the root cause of every antigravity dispatch no-op on Windows (the process
stays alive, ``tokens=0 turn_count=0 output="(empty)"``, and only the no-op
detector eventually errors it out). Under a real pseudo-terminal the Windows
pseudo-console (ConPTY) answers the query automatically and ``agy`` proceeds —
proven by running the identical command both ways: plain pipe hangs at 0 bytes,
ConPTY prints the response in seconds.

This module spawns ``agy`` under a ConPTY via ``pywinpty`` and adapts the
blocking PTY handle to the small ``asyncio.subprocess.Process`` surface that the
``cli_agent`` dispatch read-loop and kill path actually use (``stdout`` /
``stderr`` stream readers, ``pid``, ``returncode``, ``wait()``), so the rest of
the dispatch machinery is unchanged. It is inert everywhere except Windows +
``ANTIGRAVITY``; on POSIX, and for every other agent, the plain-pipe path is
used exactly as before.

Claude Code / Codex / Grok stream structured stdout and do not probe the
terminal, so they neither need nor use this path.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from typing import TYPE_CHECKING, Any

from agentshore.logging import get_logger
from agentshore.state import AgentType

if TYPE_CHECKING:
    from collections.abc import Mapping

_logger = get_logger(__name__)

# Chars per blocking PTY read in the reader thread.
_READ_CHUNK = 4096


def _winpty_available() -> bool:
    """True only on Windows with an importable ``winpty`` (pywinpty)."""
    if sys.platform != "win32":
        return False
    try:
        import winpty  # noqa: F401  (probe import only)
    except Exception:  # pragma: no cover - import failure shape varies by env
        return False
    return True


# Resolved once at import. Tests monkeypatch this (and ``sys.platform``) to
# exercise both branches of ``should_use_conpty`` deterministically off-Windows.
_HAS_WINPTY = _winpty_available()


def should_use_conpty(agent_type: AgentType) -> bool:
    """Return True when this dispatch must spawn ``agy`` under a ConPTY.

    Gated tightly: Windows only, ``ANTIGRAVITY`` only, and only when
    ``pywinpty`` is importable. On Windows without pywinpty this logs a warning
    and returns False — the caller falls back to the plain-pipe path, which will
    hang on the Device-Attributes query (the pre-fix behaviour), so the warning
    points operators at the missing dependency.
    """
    if sys.platform != "win32" or agent_type != AgentType.ANTIGRAVITY:
        return False
    if not _HAS_WINPTY:
        _logger.warning(
            "conpty_unavailable",
            detail=(
                "pywinpty is not importable; agy will be spawned over pipes and "
                "will hang on its terminal Device-Attributes query (no-op). "
                "Install pywinpty to enable the Antigravity agent on Windows."
            ),
        )
        return False
    return True


class PtyProcess:
    """Adapt a blocking ``winpty.PtyProcess`` to the async ``Process`` surface.

    Only the members the dispatch path touches are implemented: ``stdout`` /
    ``stderr`` (``asyncio.StreamReader``), ``stdin`` (always ``None`` — ``agy``
    has no stdin prompt mode), ``pid``, ``returncode``, ``wait()``,
    ``kill()`` / ``terminate()``, and ``_transport`` (``None``, so
    ``_close_process_transport`` is a no-op). The Windows ``_kill_process``
    branch tree-kills by ``pid`` via ``subprocess_env.kill_tree_sync`` and then
    awaits ``wait()``, so no signal/process-group support is required here.

    A dedicated daemon thread performs the blocking PTY reads and feeds the
    bytes into ``stdout`` via ``loop.call_soon_threadsafe`` (the only
    thread-safe way to push into an asyncio ``StreamReader``). ``agy`` is a
    single merged PTY stream, so ``stderr`` is created already at EOF; the
    stderr sniffer / auth watcher therefore see a clean immediate EOF.
    """

    def __init__(
        self,
        pty: Any,
        *,
        loop: asyncio.AbstractEventLoop,
        limit: int,
    ) -> None:
        self._pty = pty
        self._loop = loop
        self.stdout: asyncio.StreamReader | None = asyncio.StreamReader(limit=limit)
        merged_stderr = asyncio.StreamReader(limit=limit)
        merged_stderr.feed_eof()
        self.stderr: asyncio.StreamReader | None = merged_stderr
        self.stdin = None
        self._transport = None
        self._returncode: int | None = None
        self._exited = asyncio.Event()
        self._thread = threading.Thread(target=self._pump, name="agy-conpty-reader", daemon=True)
        self._thread.start()

    @property
    def pid(self) -> int | None:
        return getattr(self._pty, "pid", None)

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def _pump(self) -> None:
        """Blocking read loop (own thread): forward PTY bytes to ``stdout``."""
        try:
            while True:
                try:
                    data = self._pty.read(_READ_CHUNK)
                except EOFError:
                    break
                except OSError:
                    break
                if not data:
                    # No data but alive: retry. (pywinpty.read normally blocks
                    # until data/EOF; guard is for alternate backends.)
                    if not self._is_alive():
                        break
                    continue
                payload = data.encode("utf-8", "replace") if isinstance(data, str) else data
                self._loop.call_soon_threadsafe(self._feed, payload)
        finally:
            rc: int | None = None
            with contextlib.suppress(Exception):
                rc = self._pty.exitstatus
            self._loop.call_soon_threadsafe(self._finish, rc)

    def _is_alive(self) -> bool:
        try:
            return bool(self._pty.isalive())
        except Exception:  # pragma: no cover - backend-dependent
            return False

    def _feed(self, payload: bytes) -> None:
        if self.stdout is not None and not self.stdout.at_eof():
            self.stdout.feed_data(payload)

    def _finish(self, rc: int | None) -> None:
        if self._returncode is None:
            self._returncode = rc if rc is not None else 0
        if self.stdout is not None and not self.stdout.at_eof():
            self.stdout.feed_eof()
        self._exited.set()

    async def wait(self) -> int | None:
        await self._exited.wait()
        return self._returncode

    def kill(self) -> None:
        with contextlib.suppress(Exception):
            self._pty.terminate(force=True)

    def terminate(self) -> None:
        with contextlib.suppress(Exception):
            self._pty.terminate(force=False)


async def spawn(
    argv: list[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    limit: int,
) -> PtyProcess:
    """Spawn *argv* under a ConPTY and return a ``Process``-like adapter.

    The ``winpty.PtyProcess.spawn`` call is run in the default executor so it
    never blocks the event loop. A missing executable is normalised to
    ``FileNotFoundError`` so the dispatch path maps it to the same recoverable
    "executable not found" class the plain-pipe spawn already handles.
    """
    import winpty  # local import: only importable on Windows

    loop = asyncio.get_running_loop()

    def _spawn() -> Any:
        return winpty.PtyProcess.spawn(list(argv), cwd=cwd, env=dict(env))

    try:
        pty = await loop.run_in_executor(None, _spawn)
    except FileNotFoundError:
        raise
    except Exception as exc:
        # Normalise pywinpty's launch errors (usually a missing binary) to the
        # recoverable executable-not-found case, not an unexpected play error.
        raise FileNotFoundError(f"failed to spawn {argv[0]!r} under ConPTY: {exc}") from exc
    return PtyProcess(pty, loop=loop, limit=limit)


def run_sync(
    argv: list[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    cwd: str | None = None,
) -> tuple[str, int | None, bool]:
    """Run *argv* under a ConPTY synchronously: ``(stdout, returncode, timed_out)``.

    Used by the synchronous antigravity auth probe. Reads in a daemon thread and
    joins for at most *timeout*; if the process is still alive past the deadline
    it is tree-killed (``subprocess_env.kill_tree_sync``) and ``timed_out`` is
    True. Returned stdout is raw PTY text (terminal escapes included) — the
    caller strips/searches it.
    """
    import winpty  # local import: only importable on Windows

    from agentshore import subprocess_env

    try:
        pty = winpty.PtyProcess.spawn(list(argv), cwd=cwd, env=dict(env))
    except FileNotFoundError as exc:
        raise OSError(f"agy binary not found: {exc}") from exc
    except Exception as exc:
        # Normalise to OSError: the synchronous auth-probe caller guards on it.
        raise OSError(f"failed to spawn {argv[0]!r} under ConPTY: {exc}") from exc
    chunks: list[str] = []

    def _read() -> None:
        try:
            while True:
                try:
                    data = pty.read(_READ_CHUNK)
                except EOFError:
                    break
                except OSError:
                    break
                if not data:
                    if not pty.isalive():
                        break
                    continue
                chunks.append(data if isinstance(data, str) else data.decode("utf-8", "replace"))
        except Exception:  # pragma: no cover - backend-dependent
            pass

    reader = threading.Thread(target=_read, name="agy-conpty-probe", daemon=True)
    reader.start()
    reader.join(timeout=timeout)
    timed_out = reader.is_alive()
    if timed_out:
        pid = getattr(pty, "pid", None)
        if pid:
            subprocess_env.kill_tree_sync(pid)
        with contextlib.suppress(Exception):
            pty.terminate(force=True)
        reader.join(timeout=5.0)
    rc: int | None = None
    with contextlib.suppress(Exception):
        rc = pty.exitstatus
    return "".join(chunks), rc, timed_out
