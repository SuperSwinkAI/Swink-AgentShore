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

#: Pinned: the driver depends on indefinite capture and status.outputPath JSON
#: compatibility. Bump only after doctor/status verification. Split name/version
#: so the installer can pin the npm spec and verify the binary (see
#: ``_verify_pinned_version``).
_CLI_NAME = "timelapse-capture"
_CLI_VERSION = "0.5.0"
_CLI_PACKAGE = f"{_CLI_NAME}@{_CLI_VERSION}"

#: Public alias of the pinned version for callers that compare an installed CLI
#: against the expected pin (e.g. the installer's update-if-installed step).
EXPECTED_CLI_VERSION = _CLI_VERSION
_MIN_NODE_MAJOR = 24
_WINGET_NODE_ID = "OpenJS.NodeJS"
_WINGET_FFMPEG_ID = "Gyan.FFmpeg"
_HOMEBREW_URL = "https://brew.sh"

# winget exits non-zero when the package is already present at the latest
# version. Not a failure — the caller re-verifies the tool afterwards — so
# these exit codes/markers are treated as a no-op.
#: 0x8A15002B APPINSTALLER_CLI_ERROR_UPDATE_NOT_APPLICABLE, as both the unsigned
#: DWORD and the signed-int form a subprocess return code may surface as.
_WINGET_NOOP_EXIT_CODES = frozenset({2316632107, -1978335189})
_WINGET_NOOP_OUTPUT_MARKERS = (
    "no available upgrade found",
    "no newer package versions are available",
    "no applicable upgrade",
    "already installed",
)

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
    requested: list[str] = []
    for path in entries:
        if not path:
            continue
        text = str(path)
        if text not in requested:
            requested.append(text)
    if not requested:
        return
    # Move requested dirs to the front even when already on PATH: winget appends
    # a new Node to the *end*, so an older Program Files Node would keep
    # shadowing it. Reordering guarantees the preferred entries win.
    requested_set = set(requested)
    remaining = [
        p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p not in requested_set
    ]
    os.environ["PATH"] = os.pathsep.join([*requested, *remaining])


def _node_dir_version(name: str) -> tuple[int, int, int] | None:
    """Parse ``(major, minor, patch)`` from a ``node-vX.Y.Z-win-x64`` dir name."""
    match = re.search(r"node-v(\d+)\.(\d+)\.(\d+)", name)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _winget_node_bin_dirs() -> list[Path]:
    r"""Locate winget-installed Node dirs meeting the minimum version.

    winget unpacks ``OpenJS.NodeJS`` under
    ``%LOCALAPPDATA%\Microsoft\WinGet\Packages\OpenJS.NodeJS*\node-v*-win-x64``,
    a directory that is not placed ahead of an existing Program Files Node on
    PATH — so a freshly-installed (newer) Node stays invisible and the older
    one keeps failing the version gate. Returns matching ``node.exe``
    directories, newest version first, filtered to ``>= _MIN_NODE_MAJOR``.
    """
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return []
    packages = Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"
    if not packages.is_dir():
        return []
    versioned: list[tuple[tuple[int, int, int], Path]] = []
    for package_dir in packages.glob("OpenJS.NodeJS*"):
        for node_dir in package_dir.glob("node-v*-win-x64"):
            version = _node_dir_version(node_dir.name)
            if version is None or version[0] < _MIN_NODE_MAJOR:
                continue
            if (node_dir / "node.exe").is_file():
                versioned.append((version, node_dir))
    versioned.sort(key=lambda item: item[0], reverse=True)
    return [node_dir for _, node_dir in versioned]


def _clean_command_output(text: str) -> str:
    """Collapse winget's carriage-return progress bars into readable text.

    winget renders download progress as a ``\\r``-updated bar made of
    block-drawing glyphs (U+2580–U+259F). Folding that raw stream into an error
    message floods logs and — on a legacy code page — used to crash structlog's
    ``print`` with ``UnicodeEncodeError``. Keep only the final state of each
    line and drop box-drawing/geometric runs (U+2500–U+25FF).
    """
    cleaned_lines: list[str] = []
    for raw_line in text.split("\n"):
        latest = raw_line.split("\r")[-1]
        filtered = "".join(ch for ch in latest if ch.isprintable() and not ("─" <= ch <= "◿"))
        collapsed = " ".join(filtered.split())
        if collapsed:
            cleaned_lines.append(collapsed)
    return " ".join(cleaned_lines)


def _refresh_windows_tool_paths() -> None:
    candidates: list[Path] = []
    # A winget Node lives under WinGet\Packages (not ahead of Program Files on
    # PATH) and must win over an older Program Files Node, so list the
    # newest-qualifying winget Node dirs first.
    candidates.extend(_winget_node_bin_dirs())
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
        combined = _clean_command_output("\n".join(filter(None, (result.stdout, result.stderr))))
        lowered = combined.lower()
        benign = result.returncode in _WINGET_NOOP_EXIT_CODES or any(
            marker in lowered for marker in _WINGET_NOOP_OUTPUT_MARKERS
        )
        if benign:
            # Already present at the latest version. Not a failure: the caller
            # re-verifies the tool afterwards and surfaces a clean error if the
            # requirement is still unmet.
            _logger.info(
                "timelapse_winget_no_change",
                package_id=package_id,
                detail=combined or "no change",
            )
            _refresh_windows_tool_paths()
            return
        raise TimelapseError(f"`winget install {package_id}` failed: {combined or 'unknown error'}")
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


async def _verify_pinned_version(cwd: Path) -> None:
    """Fail the install unless the resolved binary is exactly the pinned version.

    ``npm install -g <pkg>@<version>`` lands the pin, but the *resolved* binary
    on PATH can still be a different, older global that shadows ours (a
    machine-wide install ahead of the user one, a stale winget shim, etc.). The
    doctor check only proves the toolchain *works*, not that it is the version
    the driver was written against — so we assert the version explicitly and
    surface drift as an actionable failure rather than silently running an
    unpinned CLI.
    """
    binary = resolve_timelapse_binary()
    if binary is None:
        raise TimelapseError("timelapse-capture not on PATH after install")
    result = await _run([binary, "--version"], cwd=cwd, timeout=15.0)
    installed = result.stdout.strip()
    if result.returncode != 0 or installed != _CLI_VERSION:
        raise TimelapseError(
            f"timelapse-capture resolves to {installed or 'an unknown version'} but the "
            f"pinned version is {_CLI_VERSION}; the install did not land the pin "
            f"(another global may be shadowing it on PATH)"
        )


#: The daemon-spawn options block in the pinned CLI source — these exact options
#: and *no* ``windowsHide`` (see ``harden_installed_cli``). Pinned text, stable
#: for the pinned version.
_DAEMON_SPAWN_ANCHOR = '    stdio: "ignore",\n    env: process.env,\n'
_DAEMON_SPAWN_PATCHED = _DAEMON_SPAWN_ANCHOR + "    windowsHide: true,\n"


def _patch_text_for_windows_hide(source: str) -> str | None:
    """Return *source* with ``windowsHide: true`` on the daemon spawn, or None.

    None means "no change needed/possible": either the spawn is already hardened
    (idempotent re-run) or the anchor is absent (an unexpected CLI layout — we
    refuse to blind-patch rather than risk corrupting the file).
    """
    if _DAEMON_SPAWN_PATCHED in source:
        return None
    if _DAEMON_SPAWN_ANCHOR not in source:
        return None
    return source.replace(_DAEMON_SPAWN_ANCHOR, _DAEMON_SPAWN_PATCHED, 1)


async def _resolve_installed_cli_source(cwd: Path) -> Path | None:
    """Locate the installed CLI's main ``.mjs`` under the global npm root."""
    try:
        result = await _run(["npm", "root", "-g"], cwd=cwd, timeout=30.0)
    except TimelapseError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    candidate = Path(result.stdout.strip()) / _CLI_NAME / "src" / "timelapse-capture.mjs"
    return candidate if candidate.is_file() else None


async def harden_installed_cli(cwd: Path) -> None:
    """Patch the installed CLI so its capture daemon spawns without a console (#158).

    ``timelapse-capture start`` is a launcher: it spawns a long-lived *detached*
    node capture daemon. On Windows, Node spawns a ``detached: true`` child via
    ``DETACHED_PROCESS`` and — absent ``windowsHide: true`` — Windows hands that
    console app a fresh, empty console window for the life of the session. The
    daemon is a grandchild of AgentShore's own ``CREATE_NO_WINDOW`` spawn, and
    that flag does not propagate across the detach, so we cannot suppress the
    window from the parent side. Patch the installed source in place to add
    ``windowsHide: true`` to the daemon spawn.

    Called both at install time *and* on every capture start (see
    ``agentshore.timelapse.start_capture``): an install predating this patch — or
    one the user has not re-run since — would otherwise keep the empty window
    forever, because the desktop only re-offers the installer when the feature is
    not yet marked installed. Re-running here repairs such installs before the
    daemon spawns. The patch is idempotent (a no-op on already-patched source).

    Best-effort and Windows-only: the empty window is cosmetic, so a missing
    anchor or an unwritable global install must warn, never raise.

    This is a stopgap for upstream Open-Agent-Tools/timelapse-capture#408 — once
    a fixed release ships, bump ``_CLI_VERSION`` and delete this patch step.
    """
    if not sys.platform.startswith("win"):
        return
    cli_path = await _resolve_installed_cli_source(cwd)
    if cli_path is None:
        _logger.warning("timelapse_windows_hide_patch_skipped", reason="cli source not found")
        return
    try:
        source = cli_path.read_text(encoding="utf-8")
    except OSError as exc:
        _logger.warning("timelapse_windows_hide_patch_skipped", reason=str(exc))
        return
    patched = _patch_text_for_windows_hide(source)
    if patched is None:
        _logger.info("timelapse_windows_hide_noop", path=str(cli_path))
        return
    try:
        cli_path.write_text(patched, encoding="utf-8")
    except OSError as exc:
        _logger.warning("timelapse_windows_hide_patch_failed", reason=str(exc))
        return
    _logger.info("timelapse_windows_hide_patched", path=str(cli_path))


async def installed_cli_version(cwd: Path) -> str | None:
    """Return the installed ``timelapse-capture`` version, or None if absent.

    Best-effort: returns None when the binary is not on PATH or ``--version``
    fails, so callers can treat None as "needs install".
    """
    binary = resolve_timelapse_binary()
    if binary is None:
        return None
    try:
        result = await _run([binary, "--version"], cwd=cwd, timeout=15.0)
    except TimelapseError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


async def _install_timelapse_steps(work_dir: Path) -> None:
    # If the installed CLI already matches the pin, skip the heavy
    # ffmpeg/node/npm provisioning and just re-assert Windows hardening +
    # toolchain health. If missing/stale, run the full install — npm install -g
    # <pin> upgrades a stale global in place. Keeps a re-run cheap when current.
    current = await installed_cli_version(work_dir)
    if current == _CLI_VERSION:
        _logger.info("timelapse_already_current", version=current)
        await harden_installed_cli(work_dir)
        try:
            await _verify_doctor(work_dir)
            return
        except TimelapseError as exc:
            # Pinned version present but an unhealthy toolchain (e.g. ffmpeg was
            # removed): fall through to a full (re)install to repair it.
            _logger.warning("timelapse_current_but_unhealthy", error=str(exc))
    elif current is not None:
        _logger.info("timelapse_version_drift", installed=current, expected=_CLI_VERSION)

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
    await _verify_pinned_version(work_dir)
    await harden_installed_cli(work_dir)
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


async def update_timelapse_if_installed(cwd: Path | None = None) -> InstallResult:
    """Bring an *existing* timelapse-capture install up to the expected pin.

    The installer runs this on every install so a previously-installed CLI is
    kept current without the user re-selecting the optional Timelapse component.
    It is a no-op when the CLI is not installed (a fresh install is an explicit,
    opt-in action via :func:`install_timelapse`) and when the installed version
    already matches the pin; when the installed version has drifted from the
    pin, it upgrades in place. Best-effort: never raises — step failures surface
    as a failed :class:`InstallResult`.
    """
    if resolve_timelapse_binary() is None:
        _logger.info("timelapse_update_skipped", reason="not_installed")
        return InstallResult(
            success=True, message="timelapse-capture is not installed; nothing to update."
        )
    return await install_timelapse(cwd)
