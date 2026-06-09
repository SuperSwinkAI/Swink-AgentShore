"""Auto-install the optional ``timelapse-capture`` CLI and its dependencies.

Triggered by the desktop "Timelapse capture" setup checkbox. Best-effort:
macOS uses Homebrew for missing system deps, while Windows uses an existing
Node/npm and FFmpeg dependencies through winget before npm plus
``timelapse-capture doctor`` verify the toolchain.

  1. Node.js/npm and FFmpeg/ffprobe.
  2. ``timelapse-capture`` via ``npm install -g`` a pinned npm-registry version
     (which auto-provisions the Playwright Chromium browser).
  3. Verify with ``timelapse-capture doctor --json``.

Each step logs and raises :class:`agentshore.timelapse.TimelapseError` with an
actionable message on failure so the desktop can surface it.
"""

from __future__ import annotations

import json
import os
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

#: Pinned because the driver depends on indefinite capture and
#: ``status.outputPath`` JSON compatibility. Bump only after doctor/status
#: verification.
_CLI_PACKAGE = "timelapse-capture@0.5.0"
_MIN_NODE_MAJOR = 24
_WINGET_NODE_ID = "OpenJS.NodeJS"
_WINGET_FFMPEG_ID = "Gyan.FFmpeg"
_HOMEBREW_URL = "https://brew.sh"

# brew/npm installs can be slow (compiling, downloading Chromium).
_BREW_TIMEOUT_SECONDS = 600.0
_WINGET_TIMEOUT_SECONDS = 900.0
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


def _prepend_path_entries(entries: Sequence[Path]) -> None:
    existing = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    new_entries = [str(path) for path in entries if path and str(path) not in existing]
    if new_entries:
        os.environ["PATH"] = os.pathsep.join([*new_entries, *existing])


def _refresh_windows_tool_paths() -> None:
    candidates: list[Path] = []
    if program_files := os.environ.get("PROGRAMFILES"):
        candidates.append(Path(program_files) / "nodejs")
    if appdata := os.environ.get("APPDATA"):
        candidates.append(Path(appdata) / "npm")
    if local_appdata := os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(local_appdata) / "Microsoft" / "WinGet" / "Links")
    _prepend_path_entries(candidates)


async def _winget_install(package_id: str, *, cwd: Path, label: str) -> None:
    if shutil.which("winget") is None:
        raise TimelapseError(
            f"{label} is required but winget was not found. Install {label}, then retry."
        )
    _logger.info("timelapse_install_windows_dependency", package_id=package_id)
    result = await _run(
        [
            "winget",
            "install",
            "--id",
            package_id,
            "--exact",
            "--source",
            "winget",
            "--scope",
            "user",
            "--silent",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--disable-interactivity",
        ],
        cwd=cwd,
        timeout=_WINGET_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise TimelapseError(f"`winget install {package_id}` failed: {details}")
    _refresh_windows_tool_paths()


async def _ensure_windows_ffmpeg(cwd: Path) -> None:
    _refresh_windows_tool_paths()
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    await _winget_install(_WINGET_FFMPEG_ID, cwd=cwd, label="FFmpeg")
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        raise TimelapseError("ffmpeg/ffprobe still missing after `winget install Gyan.FFmpeg`")


def _node_major(version_output: str) -> int | None:
    match = re.search(r"v?(\d+)\.\d+\.\d+", version_output)
    return int(match.group(1)) if match else None


async def _ensure_node(cwd: Path) -> None:
    if sys.platform.startswith("win"):
        await _ensure_windows_node(cwd)
        return

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


async def _ensure_windows_node(cwd: Path) -> None:
    _refresh_windows_tool_paths()
    node = shutil.which("node")
    if node is not None:
        result = await _run(["node", "--version"], cwd=cwd, timeout=15.0)
        if result.returncode != 0:
            raise TimelapseError(
                f"`node --version` failed: {result.stderr.strip() or 'unknown error'}"
            )
        major = _node_major(result.stdout)
        if major is None:
            raise TimelapseError(
                f"could not determine Node.js version from {result.stdout.strip()!r}"
            )
        if major >= _MIN_NODE_MAJOR:
            if shutil.which("npm") is None:
                raise TimelapseError("npm not found on PATH (expected alongside Node.js)")
            return
        _logger.warning(
            "timelapse_windows_node_below_package_engine",
            found=result.stdout.strip(),
            required=f">={_MIN_NODE_MAJOR}",
        )

    await _winget_install(_WINGET_NODE_ID, cwd=cwd, label=f"Node.js {_MIN_NODE_MAJOR}+")
    node = shutil.which("node")
    if node is None:
        raise TimelapseError(f"node still missing after `winget install {_WINGET_NODE_ID}`")
    check = await _run(["node", "--version"], cwd=cwd, timeout=15.0)
    major = _node_major(check.stdout) if check.returncode == 0 else None
    if major is None or major < _MIN_NODE_MAJOR:
        raise TimelapseError(
            f"Node.js {_MIN_NODE_MAJOR}+ required but found {check.stdout.strip() or 'unknown'}"
        )
    if shutil.which("npm") is None:
        raise TimelapseError("npm not found on PATH after Node.js install")


async def _install_cli(cwd: Path) -> None:
    if shutil.which("npm") is None:
        raise TimelapseError("npm not found on PATH (expected alongside Node.js)")
    _logger.info("timelapse_install_cli", source=_CLI_PACKAGE)
    result = await _run(
        ["npm", "install", "-g", _CLI_PACKAGE], cwd=cwd, timeout=_NPM_TIMEOUT_SECONDS
    )
    if result.returncode != 0:
        raise TimelapseError(f"`npm install -g timelapse-capture` failed: {result.stderr.strip()}")


async def _install_timelapse_steps(work_dir: Path) -> None:
    if sys.platform == "darwin":
        await _ensure_ffmpeg(work_dir)
    elif sys.platform.startswith("win"):
        await _ensure_windows_ffmpeg(work_dir)
    else:
        raise TimelapseError(
            "Timelapse capture auto-install is only supported on macOS and Windows."
        )

    await _ensure_node(work_dir)
    await _install_cli(work_dir)
    await _verify_doctor(work_dir)


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
    """Provision Node/npm and the timelapse-capture CLI.

    Returns an :class:`InstallResult`; raises :class:`TimelapseError` only for
    unexpected internal errors (step failures are converted to a failed result
    with the actionable message).
    """
    work_dir = cwd or Path.home()
    try:
        await _install_timelapse_steps(work_dir)
    except TimelapseError as exc:
        _logger.warning("timelapse_install_failed", error=str(exc))
        return InstallResult(success=False, message=str(exc))
    _logger.info("timelapse_install_ok")
    return InstallResult(success=True, message="Timelapse capture installed and verified.")
