"""Auto-install the optional ``timelapse-capture`` CLI and its dependencies.

Triggered by the desktop "Timelapse capture" setup checkbox. Best-effort,
macOS-only (the desktop build target), and uses Homebrew for system deps:

  1. ``ffmpeg`` / ``ffprobe`` — ``brew install ffmpeg`` if missing.
  2. Node.js 24+ — ``brew install node`` if missing or too old.
  3. ``timelapse-capture`` — ``npm install -g`` the release tarball (which
     auto-provisions the Playwright Chromium browser).
  4. Verify with ``timelapse-capture doctor --json``.

Each step logs and raises :class:`agentshore.timelapse.TimelapseError` with an
actionable message on failure so the desktop can surface it.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from agentshore.command import CommandResult, CommandTimeoutError, run_command
from agentshore.timelapse import TimelapseError, resolve_timelapse_binary

if TYPE_CHECKING:
    from collections.abc import Sequence

_logger = structlog.get_logger(__name__)

#: Release tarball installed globally via npm (auto-provisions Playwright).
_RELEASE_TGZ = (
    "https://github.com/Open-Agent-Tools/timelapse-capture/releases/latest/"
    "download/timelapse-capture.tgz"
)
_MIN_NODE_MAJOR = 24
_HOMEBREW_URL = "https://brew.sh"

# brew/npm installs can be slow (compiling, downloading Chromium).
_BREW_TIMEOUT_SECONDS = 600.0
_NPM_TIMEOUT_SECONDS = 600.0
_DOCTOR_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Outcome of an install attempt, returned to the desktop."""

    success: bool
    message: str


async def _run(args: Sequence[str], *, cwd: Path, timeout: float) -> CommandResult:
    """Run a command, normalising failures to :class:`TimelapseError`."""
    try:
        return await run_command(*args, cwd=cwd, timeout_seconds=timeout)
    except FileNotFoundError as exc:
        raise TimelapseError(f"{args[0]} not found on PATH") from exc
    except (CommandTimeoutError, OSError) as exc:
        raise TimelapseError(f"`{' '.join(args)}` failed: {exc}") from exc


async def _ensure_ffmpeg(cwd: Path) -> None:
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    if shutil.which("brew") is None:
        raise TimelapseError(
            f"ffmpeg/ffprobe are required but Homebrew was not found. "
            f"Install Homebrew from {_HOMEBREW_URL}, then `brew install ffmpeg`."
        )
    _logger.info("timelapse_install_ffmpeg")
    result = await _run(["brew", "install", "ffmpeg"], cwd=cwd, timeout=_BREW_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise TimelapseError(f"`brew install ffmpeg` failed: {result.stderr.strip()}")
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        raise TimelapseError("ffmpeg/ffprobe still missing after `brew install ffmpeg`")


def _node_major(version_output: str) -> int | None:
    match = re.search(r"v?(\d+)\.\d+\.\d+", version_output)
    return int(match.group(1)) if match else None


async def _ensure_node(cwd: Path) -> None:
    node = shutil.which("node")
    if node is not None:
        result = await _run(["node", "--version"], cwd=cwd, timeout=15.0)
        major = _node_major(result.stdout) if result.returncode == 0 else None
        if major is not None and major >= _MIN_NODE_MAJOR:
            return
    if shutil.which("brew") is None:
        raise TimelapseError(
            f"Node.js {_MIN_NODE_MAJOR}+ is required but Homebrew was not found. "
            f"Install Homebrew from {_HOMEBREW_URL}, then `brew install node`."
        )
    _logger.info("timelapse_install_node")
    result = await _run(["brew", "install", "node"], cwd=cwd, timeout=_BREW_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise TimelapseError(f"`brew install node` failed: {result.stderr.strip()}")
    node = shutil.which("node")
    if node is None:
        raise TimelapseError("node still missing after `brew install node`")
    check = await _run(["node", "--version"], cwd=cwd, timeout=15.0)
    major = _node_major(check.stdout) if check.returncode == 0 else None
    if major is None or major < _MIN_NODE_MAJOR:
        raise TimelapseError(
            f"Node.js {_MIN_NODE_MAJOR}+ required but found {check.stdout.strip() or 'unknown'}"
        )


async def _install_cli(cwd: Path) -> None:
    if shutil.which("npm") is None:
        raise TimelapseError("npm not found on PATH (expected alongside Node.js)")
    _logger.info("timelapse_install_cli", source=_RELEASE_TGZ)
    result = await _run(
        ["npm", "install", "-g", _RELEASE_TGZ], cwd=cwd, timeout=_NPM_TIMEOUT_SECONDS
    )
    if result.returncode != 0:
        raise TimelapseError(f"`npm install -g timelapse-capture` failed: {result.stderr.strip()}")


async def _verify_doctor(cwd: Path) -> None:
    binary = resolve_timelapse_binary()
    if binary is None:
        raise TimelapseError("timelapse-capture not on PATH after install")
    result = await _run([binary, "doctor", "--json"], cwd=cwd, timeout=_DOCTOR_TIMEOUT_SECONDS)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TimelapseError(f"could not parse `timelapse-capture doctor` output: {exc}") from exc
    if data.get("ok") is True:
        return
    failed = [
        f"{c.get('name')}: {c.get('fix') or c.get('message') or 'failed'}"
        for c in data.get("checks", [])
        if isinstance(c, dict) and c.get("status") != "pass"
    ]
    raise TimelapseError("timelapse-capture doctor reported failures: " + "; ".join(failed))


async def install_timelapse(cwd: Path | None = None) -> InstallResult:
    """Provision ffmpeg, Node 24+, and the timelapse-capture CLI.

    Returns an :class:`InstallResult`; raises :class:`TimelapseError` only for
    unexpected internal errors (step failures are converted to a failed result
    with the actionable message).
    """
    work_dir = cwd or Path.home()
    if sys.platform != "darwin":
        return InstallResult(
            success=False,
            message="Timelapse capture auto-install is only supported on macOS.",
        )
    try:
        await _ensure_ffmpeg(work_dir)
        await _ensure_node(work_dir)
        await _install_cli(work_dir)
        await _verify_doctor(work_dir)
    except TimelapseError as exc:
        _logger.warning("timelapse_install_failed", error=str(exc))
        return InstallResult(success=False, message=str(exc))
    _logger.info("timelapse_install_ok")
    return InstallResult(success=True, message="Timelapse capture installed and verified.")
