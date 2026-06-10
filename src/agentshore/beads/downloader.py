"""Binary download/install logic for the bd (beads) CLI tool.

Separated from setup.py so that the version-check and project-init helpers
remain importable without pulling in network I/O or interactive-prompt code.

The key security invariant is:
  * Headless / non-interactive mode **fails with instructions** by default.
    Auto-downloading and executing a third-party binary without user consent
    is unacceptable in CI, agent, or server contexts.
  * Auto-download is only permitted when:
      1. The terminal is interactive (``sys.stdin.isatty()`` is True) AND the
         user explicitly confirms the prompt, OR
      2. The opt-in env var ``AGENTSHORE_AUTO_INSTALL_BD=1`` is set.

The split also keeps ``_drain_terminal_input`` — a CLI raw-mode hack that
belongs in the interactive layer — out of this module entirely. Any
interactive prompt handling lives in the CLI/wizard layer (e.g.
``agentshore.cli`` or ``agentshore.identity_wizard``), not here.
"""

from __future__ import annotations

import os
import sys

import structlog

_logger = structlog.get_logger(__name__)

# Opt-in env var for headless/CI auto-download. Set to "1" to allow
# non-interactive bd binary download. Must be explicitly set — the default
# is conservative (fail with instructions).
_AUTO_INSTALL_ENV_VAR = "AGENTSHORE_AUTO_INSTALL_BD"

_INSTALL_INSTRUCTIONS = (
    "The bd binary was not found. To resolve:\n"
    "  1. Install bd {version} from https://github.com/gastownhall/beads\n"
    "  2. Ensure `bd` is on PATH, or set AGENTSHORE_BD_BIN to the binary path.\n"
    "  3. Re-run `agentshore init`.\n"
    "\n"
    "For non-interactive / CI environments that need automatic install, "
    "set {env_var}=1 to opt in explicitly."
)


def _auto_install_opted_in() -> bool:
    """Return True when the user has explicitly opted in to non-interactive install."""
    return os.environ.get(_AUTO_INSTALL_ENV_VAR, "").strip() == "1"


def provision_bd(required_version: str) -> None:
    """Ensure the bd binary is available, downloading it only when permitted.

    Decision tree:
    1. If bd is already on PATH (or AGENTSHORE_BD_BIN), do nothing — version
       check is the caller's responsibility (see ``_check_bd_version``).
    2. If interactive TTY: prompt the user; proceed only on confirmation.
    3. If headless AND ``AGENTSHORE_AUTO_INSTALL_BD=1``: proceed automatically.
    4. Otherwise: raise ``RuntimeError`` with human-readable install instructions.

    This function does **not** perform the actual download; it enforces the
    consent gate and raises with instructions if consent is absent. The actual
    download implementation (platform-specific curl/gh release fetch) is
    a Wave-2 item. For now, all paths that would download instead raise with
    instructions so operators must install bd manually.

    Parameters
    ----------
    required_version:
        The pinned bd version string (e.g. ``"1.0.4"``). Included in the
        error message so operators know which release to fetch.
    """
    from agentshore.beads import resolve_bd_binary

    bd_binary = resolve_bd_binary()
    if bd_binary is not None:
        # Already installed — nothing to provision.
        return

    # Binary not found. Decide whether we can proceed with auto-install.
    if _auto_install_opted_in():
        _logger.info(
            "bd_auto_install_opted_in",
            env_var=_AUTO_INSTALL_ENV_VAR,
            required_version=required_version,
        )
        # Wave-2: implement actual download here. For now, fail with instructions
        # even on opt-in, since the download mechanism is not yet implemented.
        _raise_install_instructions(required_version)

    if sys.stdin.isatty():
        # Interactive terminal: prompt the user.
        _logger.info("bd_not_found_prompting_user", required_version=required_version)
        try:
            answer = (
                input(
                    f"\nbd {required_version} was not found. "
                    "Allow AgentShore to download and install it? [y/N] "
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer in ("y", "yes"):
            _logger.info(
                "bd_install_user_confirmed",
                required_version=required_version,
            )
            # Wave-2: implement actual download here.
            _raise_install_instructions(required_version)
        else:
            _logger.info("bd_install_user_declined", required_version=required_version)
            _raise_install_instructions(required_version)
    else:
        # Non-interactive and no opt-in: fail conservatively with instructions.
        _logger.warning(
            "bd_not_found_headless_no_opt_in",
            required_version=required_version,
            hint=f"Set {_AUTO_INSTALL_ENV_VAR}=1 to enable non-interactive install",
        )
        _raise_install_instructions(required_version)


def _raise_install_instructions(required_version: str) -> None:
    """Raise RuntimeError with human-readable bd install instructions."""
    raise RuntimeError(
        _INSTALL_INSTRUCTIONS.format(
            version=required_version,
            env_var=_AUTO_INSTALL_ENV_VAR,
        )
    )
