"""Install or upgrade the *system* bd (beads CLI) through its own package manager.

AgentShore never ships, downloads, or stages a private copy of bd. It uses the
one on PATH (or ``AGENTSHORE_BD_BIN``), exactly like the user's shell does.
When bd is missing, or older than :data:`agentshore.beads.setup.SUPPORTED_BD_VERSION`,
:func:`ensure_system_bd` can install or upgrade it through Homebrew or npm —
the two beads distribution channels that can be driven non-interactively.
Anything else gets manual instructions.

Consent invariant (unchanged from the old downloader): headless runs never
touch the system without ``assume_yes`` or ``AGENTSHORE_AUTO_INSTALL_BD=1``;
interactive runs are asked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import NoReturn

import structlog

_logger = structlog.get_logger(__name__)

_AUTO_INSTALL_ENV_VAR = "AGENTSHORE_AUTO_INSTALL_BD"

_INSTALL_INSTRUCTIONS = (
    "{need}. To resolve:\n"
    "  1. Install or upgrade bd to {version} or newer:\n"
    "       brew install beads            (macOS/Linux)\n"
    "       npm install -g @beads/bd@{version}\n"
    "     or see https://github.com/gastownhall/beads for other channels.\n"
    "  2. Ensure `bd` is on PATH, or set AGENTSHORE_BD_BIN to the binary path.\n"
    "  3. Re-run `agentshore init`.\n"
    "\n"
    "For non-interactive / CI environments that need automatic install, "
    "set {env_var}=1 to opt in explicitly."
)


@dataclass(frozen=True)
class PackageManager:
    """A package manager AgentShore can drive to install/upgrade bd."""

    name: str
    executable: str
    owns_bd: bool

    def command(self, version: str) -> list[str]:
        if self.name == "npm":
            return [self.executable, "install", "-g", f"@beads/bd@{version}"]
        # Homebrew cannot pin a version; `upgrade` only works for a brew-owned bd.
        return [self.executable, "upgrade" if self.owns_bd else "install", "beads"]


def detect_package_manager(bd_binary: str | None) -> PackageManager | None:
    """Pick the manager that owns *bd_binary*, else the first one available.

    Ownership is inferred from the resolved install path: npm globals live
    under ``node_modules``/``npm``, Homebrew under ``Cellar``/``homebrew``/
    ``linuxbrew``.
    """
    brew = shutil.which("brew")
    npm = shutil.which("npm")
    if bd_binary:
        real = os.path.realpath(bd_binary).replace("\\", "/").lower()
        if npm and ("/node_modules/" in real or "/npm/" in real):
            return PackageManager("npm", npm, owns_bd=True)
        if brew and any(mark in real for mark in ("/cellar/", "/homebrew/", "/linuxbrew/")):
            return PackageManager("brew", brew, owns_bd=True)
    if brew:
        return PackageManager("brew", brew, owns_bd=False)
    if npm:
        return PackageManager("npm", npm, owns_bd=False)
    return None


def ensure_system_bd(supported_version: str, *, assume_yes: bool = False) -> str:
    """Return the path of a bd at or above *supported_version*, installing/upgrading if needed.

    1. bd resolves and is at least *supported_version* (or its version cannot
       be probed — the caller's version check reports that precisely): return it.
    2. Otherwise find a package manager (see :func:`detect_package_manager`);
       none → raise with manual instructions.
    3. With consent (``assume_yes``, ``AGENTSHORE_AUTO_INSTALL_BD=1``, or an
       interactive yes) run the manager and return the re-resolved bd.
    4. Without consent raise with instructions; never touch the system.

    Honours the ``AGENTSHORE_BD_VERSION`` override of the supported version.
    """
    from agentshore.beads import resolve_bd_binary
    from agentshore.beads.setup import bd_version, parse_bd_version

    version = (
        os.environ.get("AGENTSHORE_BD_VERSION", supported_version).strip() or supported_version
    )
    bd_binary = resolve_bd_binary()
    if bd_binary is not None:
        try:
            found = bd_version(bd_binary)
        except RuntimeError:
            return bd_binary
        found_parsed, wanted_parsed = parse_bd_version(found), parse_bd_version(version)
        if found_parsed is None or wanted_parsed is None or found_parsed >= wanted_parsed:
            return bd_binary
        need = f"bd {found} at {bd_binary} is older than AgentShore's supported version {version}"
    else:
        need = f"bd {version} was not found"
    _logger.warning("bd_install_needed", reason=need)

    manager = detect_package_manager(bd_binary)
    if manager is None:
        _raise_install_instructions(need, version)
    command = manager.command(version)
    if not _consent(f"\n{need}. Run `{' '.join(command)}` now? [y/N] ", version, assume_yes):
        if not sys.stdin.isatty():
            _logger.warning(
                "bd_install_headless_no_opt_in",
                supported_version=version,
                hint=f"Set {_AUTO_INSTALL_ENV_VAR}=1 to enable non-interactive install",
            )
        _raise_install_instructions(need, version)

    _logger.info("bd_install_running", manager=manager.name, command=command)
    try:
        subprocess.run(command, check=True, stdin=subprocess.DEVNULL, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"`{' '.join(command)}` failed: {exc}") from exc
    installed = resolve_bd_binary()
    if installed is None:
        raise RuntimeError(
            f"`{' '.join(command)}` succeeded but `bd` is still not on PATH; open a new shell "
            "or set AGENTSHORE_BD_BIN."
        )
    _logger.info("bd_installed", manager=manager.name, path=installed)
    return installed


def _consent(prompt: str, version: str, assume_yes: bool) -> bool:
    """True when the caller, env, or interactive user consents to run the manager."""
    if assume_yes or os.environ.get(_AUTO_INSTALL_ENV_VAR, "").strip() == "1":
        _logger.info(
            "bd_install_consented",
            via="assume_yes" if assume_yes else _AUTO_INSTALL_ENV_VAR,
            supported_version=version,
        )
        return True
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer in ("y", "yes"):
        _logger.info("bd_install_user_confirmed", supported_version=version)
        return True
    _logger.info("bd_install_user_declined", supported_version=version)
    return False


def _raise_install_instructions(need: str, version: str) -> NoReturn:
    raise RuntimeError(
        _INSTALL_INSTRUCTIONS.format(need=need, version=version, env_var=_AUTO_INSTALL_ENV_VAR)
    )
