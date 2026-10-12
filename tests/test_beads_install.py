"""ensure_system_bd: use the system bd, upgrade it through its own package manager.

AgentShore ships no bd of its own. These pin the contract: an adequate PATH bd
is used as-is, an older/missing one is upgraded only through brew/npm and only
with consent, and headless runs without opt-in never touch the system.
"""

from __future__ import annotations

import subprocess
import types

import pytest

from agentshore.beads import install
from agentshore.beads.setup import parse_bd_version

_SUPPORTED = "1.1.0"


def _headless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTSHORE_AUTO_INSTALL_BD", raising=False)
    monkeypatch.setattr(install.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))


def test_parse_bd_version() -> None:
    assert parse_bd_version("bd version 1.3.1 (8e4e59d39)") == (1, 3, 1)
    assert parse_bd_version("1.10.0") == (1, 10, 0)
    assert parse_bd_version("garbage") is None


def test_uses_system_bd_when_at_or_above_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentshore.beads.resolve_bd_binary", lambda: "/usr/local/bin/bd")
    monkeypatch.setattr("agentshore.beads.setup.bd_version", lambda _bd: "1.3.1")
    monkeypatch.setattr(
        install.subprocess, "run", lambda *a, **k: pytest.fail("must not run a manager")
    )
    assert install.ensure_system_bd(_SUPPORTED) == "/usr/local/bin/bd"


def test_upgrades_older_npm_bd_with_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An npm-owned bd below the floor is upgraded via npm, pinned to the supported version."""
    npm_bd = r"C:\Users\u\AppData\Roaming\npm\bd.CMD"
    versions = iter(["1.0.4", "1.1.0"])
    monkeypatch.setattr("agentshore.beads.resolve_bd_binary", lambda: npm_bd)
    monkeypatch.setattr("agentshore.beads.setup.bd_version", lambda _bd: next(versions))
    monkeypatch.setattr(
        install.shutil, "which", lambda name: {"npm": "/usr/bin/npm", "brew": None}[name]
    )
    ran: list[list[str]] = []
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda cmd, **_k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )

    assert install.ensure_system_bd(_SUPPORTED, assume_yes=True) == npm_bd
    assert ran == [["/usr/bin/npm", "install", "-g", f"@beads/bd@{_SUPPORTED}"]]


def test_missing_bd_prefers_brew_install(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = iter([None, "/opt/homebrew/bin/bd"])
    monkeypatch.setattr("agentshore.beads.resolve_bd_binary", lambda: next(resolved))
    monkeypatch.setattr(
        install.shutil,
        "which",
        lambda name: {"npm": "/usr/bin/npm", "brew": "/opt/homebrew/bin/brew"}[name],
    )
    monkeypatch.setenv("AGENTSHORE_AUTO_INSTALL_BD", "1")
    ran: list[list[str]] = []
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda cmd, **_k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )

    assert install.ensure_system_bd(_SUPPORTED) == "/opt/homebrew/bin/bd"
    assert ran == [["/opt/homebrew/bin/brew", "install", "beads"]]


def test_headless_without_opt_in_raises_with_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _headless(monkeypatch)
    monkeypatch.setattr("agentshore.beads.resolve_bd_binary", lambda: None)
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/npm")
    monkeypatch.setattr(
        install.subprocess, "run", lambda *a, **k: pytest.fail("must not run a manager")
    )

    with pytest.raises(RuntimeError, match="was not found") as excinfo:
        install.ensure_system_bd(_SUPPORTED)
    assert "AGENTSHORE_AUTO_INSTALL_BD" in str(excinfo.value)


def test_no_package_manager_raises_with_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentshore.beads.resolve_bd_binary", lambda: "/usr/local/bin/bd")
    monkeypatch.setattr("agentshore.beads.setup.bd_version", lambda _bd: "1.0.4")
    monkeypatch.setattr(install.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="older than") as excinfo:
        install.ensure_system_bd(_SUPPORTED, assume_yes=True)
    assert "brew install beads" in str(excinfo.value)
