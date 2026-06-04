"""Driver for the optional ``timelapse-capture`` CLI.

The desktop app can record a timelapse of the dashboard for a session. The
sidecar owns the capture lifecycle:

* :func:`start_capture` runs ``timelapse-capture start <url> --json`` once the
  dashboard bridge is up. With no ``--duration`` the tool captures until it is
  stopped (or a 12h cap) and auto-renders on stop.
* :func:`stop_capture` runs ``timelapse-capture stop <run-id>`` on session end,
  which finalises capture and triggers the render.
* :func:`await_output` polls ``timelapse-capture status <run-id>`` until the
  rendered MP4 path (``outputPath``) is available.

Runs are addressed by their **run-id** (a deterministic 3-word alias the CLI
emits, e.g. ``swift-otter-042``). Aliases resolve against the ``timelapse-runs``
directory relative to the process cwd, so every call here must use the *same*
``runs_cwd`` and ``start`` must not pass ``--out`` — see ``docs``/the feature
plan for why. The absolute run directory is also captured as a fallback.

All callers treat timelapse work as best-effort: a failure here must never block
session start or stop.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from agentshore.command import CommandTimeoutError, run_command

if TYPE_CHECKING:
    from pathlib import Path

_logger = structlog.get_logger(__name__)

#: Default CLI binary name. Override with ``AGENTSHORE_TIMELAPSE_BIN`` (e.g. to
#: point at a bundled binary or an alternate install path).
_DEFAULT_BIN = "timelapse-capture"

# Generous timeouts: ``start`` spawns a detached capture and returns promptly,
# but the npm-installed CLI cold-start (Playwright launch) can be slow.
_START_TIMEOUT_SECONDS = 60.0
_STOP_TIMEOUT_SECONDS = 30.0
_STATUS_TIMEOUT_SECONDS = 15.0


class TimelapseError(Exception):
    """Raised when a ``timelapse-capture`` invocation fails."""


@dataclass(frozen=True, slots=True)
class TimelapseRun:
    """Handle for an in-progress capture.

    ``run_id`` is the CLI alias used to address the run on stop/status;
    ``run_dir`` is the absolute run directory (a CWD-independent fallback).
    """

    run_id: str
    run_dir: str


def resolve_timelapse_binary() -> str | None:
    """Return the path to the ``timelapse-capture`` binary, or None if absent."""
    override = os.environ.get("AGENTSHORE_TIMELAPSE_BIN")
    if override:
        return override if shutil.which(override) or os.path.isfile(override) else None
    return shutil.which(_DEFAULT_BIN)


async def start_capture(dashboard_url: str, runs_cwd: Path) -> TimelapseRun:
    """Start a detached capture of *dashboard_url* and return its handle.

    *runs_cwd* is the working directory the capture (and later stop/status)
    runs in; the ``timelapse-runs/`` tree is created beneath it. Raises
    :class:`TimelapseError` on any failure.
    """
    binary = resolve_timelapse_binary()
    if binary is None:
        raise TimelapseError(
            "timelapse-capture not found; install it via the desktop "
            "'Timelapse capture' setup option or set AGENTSHORE_TIMELAPSE_BIN"
        )
    runs_cwd.mkdir(parents=True, exist_ok=True)
    try:
        result = await run_command(
            binary,
            "start",
            dashboard_url,
            "--json",
            cwd=runs_cwd,
            timeout_seconds=_START_TIMEOUT_SECONDS,
            resolve_executable=False,
        )
    except (CommandTimeoutError, OSError) as exc:
        raise TimelapseError(f"timelapse start failed: {exc}") from exc
    if result.returncode != 0:
        raise TimelapseError(f"timelapse start exited {result.returncode}: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TimelapseError(f"could not parse timelapse start output: {exc}") from exc
    run_id = data.get("alias")
    run_dir = data.get("runDir")
    if not isinstance(run_id, str) or not run_id:
        raise TimelapseError("timelapse start output missing 'alias' (run-id)")
    if not isinstance(run_dir, str):
        run_dir = ""
    _logger.info("timelapse_started", run_id=run_id, run_dir=run_dir, url=dashboard_url)
    return TimelapseRun(run_id=run_id, run_dir=run_dir)


async def stop_capture(run_id: str, runs_cwd: Path) -> None:
    """Stop the capture for *run_id*, which triggers the auto-render.

    Raises :class:`TimelapseError` on failure.
    """
    binary = resolve_timelapse_binary()
    if binary is None:
        raise TimelapseError("timelapse-capture not found")
    try:
        result = await run_command(
            binary,
            "stop",
            run_id,
            "--json",
            cwd=runs_cwd,
            timeout_seconds=_STOP_TIMEOUT_SECONDS,
            resolve_executable=False,
        )
    except (CommandTimeoutError, OSError) as exc:
        raise TimelapseError(f"timelapse stop failed: {exc}") from exc
    if result.returncode != 0:
        raise TimelapseError(f"timelapse stop exited {result.returncode}: {result.stderr.strip()}")
    _logger.info("timelapse_stopped", run_id=run_id)


async def await_output(
    run_id: str,
    runs_cwd: Path,
    *,
    max_polls: int = 60,
    poll_interval_seconds: float = 1.0,
) -> str | None:
    """Poll ``status`` until the rendered MP4 path is available.

    Returns the ``outputPath`` string once render completes, or None if it
    does not finish within ``max_polls`` (best-effort — never raises so a stuck
    render cannot wedge session shutdown).
    """
    binary = resolve_timelapse_binary()
    if binary is None:
        return None
    for _ in range(max_polls):
        try:
            result = await run_command(
                binary,
                "status",
                run_id,
                "--json",
                cwd=runs_cwd,
                timeout_seconds=_STATUS_TIMEOUT_SECONDS,
                resolve_executable=False,
            )
        except (CommandTimeoutError, OSError) as exc:
            _logger.warning("timelapse_status_failed", run_id=run_id, error=str(exc))
            await asyncio.sleep(poll_interval_seconds)
            continue
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                data = {}
            # timelapse-capture >=0.3.1 nests the run state under a top-level
            # ``status`` key, so ``outputPath`` lives at ``data["status"]
            # ["outputPath"]``. Fall back to the flat top-level key for older
            # builds that emitted the status object directly.
            status = data.get("status")
            status_obj = status if isinstance(status, dict) else data
            output_path = status_obj.get("outputPath")
            if isinstance(output_path, str) and output_path:
                _logger.info("timelapse_rendered", run_id=run_id, output_path=output_path)
                return output_path
        await asyncio.sleep(poll_interval_seconds)
    _logger.warning("timelapse_render_timeout", run_id=run_id, max_polls=max_polls)
    return None
