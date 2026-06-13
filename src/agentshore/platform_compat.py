from __future__ import annotations

import asyncio
import contextlib
import sys


def ensure_windows_event_loop_policy() -> None:
    """Install the Proactor event loop policy on Windows.

    asyncio subprocess pipes (used by every CLI-agent spawn via
    ``create_subprocess_exec``) require the ProactorEventLoop on Windows.
    Idempotent; a no-op on non-Windows platforms.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def force_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8 on Windows.

    When AgentShore runs detached (the ``--dashboard`` background orchestrator
    and the dashboard bridge), stdout/stderr are redirected to a log file
    rather than a console, so Python falls back to the locale codec (cp1252 on
    Windows). The box-drawing ``─``, arrow ``→`` and ``⚠`` characters in the
    bootstrap/identity report can't be encoded in cp1252 and raise
    ``UnicodeEncodeError``, killing the process at boot (the orchestrator never
    reaches the run loop, so the dashboard hangs on "INITIALIZING"). Force
    UTF-8 with ``errors='replace'`` so output can never crash. No-op off
    Windows, and idempotent.
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")
