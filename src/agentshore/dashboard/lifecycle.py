"""Shared dashboard-bridge lifecycle: pid ownership, supersede/reap, ports.

Three call sites start a dashboard bridge — the standalone ``agentshore
dashboard`` command, the ``agentshore start --dashboard`` background launcher,
and the desktop sidecar's in-process ``EmbeddedBridge``. The first two need
identical pid/supersede semantics that had diverged into per-call-site copies.
This module is the single home for them so the Windows uv-trampoline
self-kill guard and the reap-before-spawn ordering live in exactly one place.

The sidecar runs the bridge in-process (no pid file, no subprocess to reap) and
selects an ephemeral port advertised back to the shell, so it does not use the
pid/port helpers here — only the reset-before-prime ordering, via
:func:`agentshore.ipc.state_writer.reset_session_files`.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from agentshore import session_path

if TYPE_CHECKING:
    from pathlib import Path


def supersede_prior_bridge(project_path: Path, *, own_lineage: set[int] | None = None) -> bool:
    r"""Reap a prior dashboard bridge recorded for *project_path*.

    Returns True if a reap was issued. ``own_lineage`` (default
    ``{getpid, getppid}``) is excluded so the guard never kills the caller's own
    process tree: on Windows the uv-tool launcher is a ``Scripts\python.exe``
    trampoline that spawns the bridge as a grandchild, so a launcher pid — or a
    stale pid the OS has since reused for our trampoline — read back from
    ``dashboard.pid`` would otherwise be reaped with ``taskkill /T``, killing us
    before the server binds. ``getppid()`` is always our live parent, so it can
    only collide with a dead/reused stale entry where skipping the reap is a
    harmless no-op. The read pid is pinned into ``stop_dashboard_process`` so a
    concurrently-written new pid is never reaped by mistake.
    """
    lineage = own_lineage if own_lineage is not None else {os.getpid(), os.getppid()}
    prior_pid = session_path.read_dashboard_pid(project_path)
    if prior_pid is None or prior_pid in lineage:
        return False
    return session_path.stop_dashboard_process(project_path, pid=prior_pid)


def reap_before_orchestrator_spawn(project_path: Path) -> bool:
    """Reap any prior bridge before the launcher spawns the orchestrator.

    The dead bridge holds ``dashboard_events.ndjson`` open (no
    FILE_SHARE_DELETE on Windows), so the orchestrator's ``StateWriter`` reset
    can't unlink it — leaving a prior session's ``session_ended`` in place for
    the new bridge to ingest and self-exit on. Reaping first releases the file
    and frees the port. The caller here is the launcher (not the bridge), so
    there is no own-lineage to exclude: read the pid from disk and stop it.
    """
    return session_path.stop_dashboard_process(project_path)


def claim_bridge_pid(project_path: Path) -> None:
    """Record the *current* process as the dashboard bridge of record.

    Must be the bridge's real ``os.getpid()`` — see
    :func:`supersede_prior_bridge` for why the launcher must not pre-write a
    trampoline pid.
    """
    session_path.write_dashboard_pid(project_path, os.getpid())


def select_dashboard_port(preferred: int | None = None) -> int:
    """Return *preferred* if given, else the first free port in the 9400 range.

    For the CLI bridge, whose port the user reaches at a stable URL. The sidecar
    deliberately uses an ephemeral free port instead (advertised back to the
    shell), so it does not call this.
    """
    return preferred if preferred is not None else session_path.find_dashboard_port()
