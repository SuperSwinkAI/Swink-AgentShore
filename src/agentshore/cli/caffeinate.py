"""macOS launch-level power-assertion fallback for ``agentshore start``.

When the screen locks on macOS, the OS demotes disk I/O priority and
coalesces writes. ``fsync()`` returns before the deferred writes physically
land, which breaks SQLite's WAL durability guarantee and produces silent
DB corruption (desktop-tvsb). The simplest robust mitigation is to re-exec
``agentshore start`` under ``caffeinate -i`` so the OS keeps AgentShore's I/O
at normal priority for the lifetime of the session.

Retained alongside the in-process PowerAssertion path as a launch-level fallback.
"""

from __future__ import annotations

import os
import shutil
import sys

_SENTINEL_ENV = "AGENTSHORE_CAFFEINATED"
_OPT_OUT_ENV = "AGENTSHORE_NO_CAFFEINATE"


def maybe_re_exec_under_caffeinate() -> None:
    """Re-exec the current process under ``caffeinate -i`` on macOS.

    Returns normally (and does nothing) on non-Darwin platforms, when
    caffeinate is missing, when the user opted out, when we are already
    running under caffeinate, or when invoked inside pytest. On success
    this call does not return — ``os.execvp`` replaces the process image.
    """
    if sys.platform != "darwin":
        return
    if os.environ.get(_SENTINEL_ENV) == "1":
        return
    if os.environ.get(_OPT_OUT_ENV) == "1":
        return
    # Tests drive ``start`` in-process via CliRunner; re-execing would
    # replace the pytest worker with caffeinate and detonate the run.
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    caffeinate = shutil.which("caffeinate")
    if caffeinate is None:
        return

    # Pass the sentinel through to the re-exec'd process so the very next
    # call to this function (after caffeinate spawns python) short-circuits.
    os.environ[_SENTINEL_ENV] = "1"
    # ``caffeinate -i`` prevents idle system sleep, which is the I/O
    # throttling that causes the corruption. It does NOT prevent display
    # sleep or screen lock — the user's battery/UX behaviour is unchanged.
    # caffeinate exits when its child (the re-exec'd agentshore) exits, so
    # no -w pid plumbing is needed.
    os.execvp(caffeinate, [caffeinate, "-i", *sys.argv])
