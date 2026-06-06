from __future__ import annotations

import asyncio
import sys


def ensure_windows_event_loop_policy() -> None:
    """Install the Proactor event loop policy on Windows.

    asyncio subprocess pipes (used by every CLI-agent spawn via
    ``create_subprocess_exec``) require the ProactorEventLoop on Windows.
    Idempotent; a no-op on non-Windows platforms.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
