"""Tests for ensure_bd_on_agent_path — pinning agent-dispatched ``bd`` to the
same binary AgentShore's own orchestrator resolves (#315).

Regression coverage: agent subprocesses (Claude Code, Codex, Grok,
Antigravity) run literal ``bd ...`` commands from skill templates. Those
resolve via the subprocess's own PATH, independent of resolve_bd_binary(),
which the orchestrator's own writes go through. A live session hit this: the
desktop app pinned bd 1.1.0 via AGENTSHORE_BD_BIN for its own writes while an
agent's inherited PATH still resolved the user's stale 1.0.4 install,
producing "schema defects in the dependencies and events tables" the moment
an agent tried a bd write against a store already migrated by 1.1.0.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog

from agentshore.beads import ensure_bd_on_agent_path


def _make_executable(path: Path, marker: str) -> Path:
    path.write_text(f"#!/bin/sh\necho {marker}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_returns_env_unchanged_when_bd_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTSHORE_BD_BIN", raising=False)
    with patch("shutil.which", return_value=None):
        env = {"PATH": "/usr/bin"}
        assert ensure_bd_on_agent_path(env) is env


def test_returns_env_unchanged_when_already_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No mismatch, no shim: bare ``bd`` already resolves to the same binary."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    bd_path = _make_executable(bin_dir / "bd", "real-bd")
    monkeypatch.setenv("AGENTSHORE_BD_BIN", str(bd_path))

    env = {"PATH": str(bin_dir)}
    with patch.object(shutil, "which", wraps=shutil.which) as which_spy:
        result = ensure_bd_on_agent_path(env)

    assert result is env  # unchanged object — no shim work happened
    which_spy.assert_called_once_with("bd", path=str(bin_dir))


def test_creates_shim_and_prepends_path_on_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar_dir = tmp_path / "sidecar"
    sidecar_dir.mkdir()
    orchestrator_bd = _make_executable(sidecar_dir / "agentshore-bd", "1.1.0")
    stale_bin_dir = tmp_path / "stale-bin"
    stale_bin_dir.mkdir()
    stale_bd = _make_executable(stale_bin_dir / "bd", "1.0.4")

    monkeypatch.setenv("AGENTSHORE_BD_BIN", str(orchestrator_bd))
    shim_dir = tmp_path / "shim"
    monkeypatch.setattr("agentshore.beads._bd_shim_dir", lambda: shim_dir)

    env = {"PATH": str(stale_bin_dir)}
    new_env = ensure_bd_on_agent_path(env)

    assert new_env is not env
    assert new_env["PATH"].startswith(str(shim_dir) + os.pathsep)
    assert str(stale_bin_dir) in new_env["PATH"]

    resolved = shutil.which("bd", path=new_env["PATH"])
    assert resolved is not None
    assert os.path.samefile(resolved, orchestrator_bd)
    # The stale binary is still reachable further down PATH, just not first.
    assert stale_bd.exists()


def test_idempotent_does_not_recreate_correct_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator_bd = _make_executable(tmp_path / "agentshore-bd", "1.1.0")
    stale_bin_dir = tmp_path / "stale-bin"
    stale_bin_dir.mkdir()
    _make_executable(stale_bin_dir / "bd", "1.0.4")

    monkeypatch.setenv("AGENTSHORE_BD_BIN", str(orchestrator_bd))
    shim_dir = tmp_path / "shim"
    monkeypatch.setattr("agentshore.beads._bd_shim_dir", lambda: shim_dir)

    env = {"PATH": str(stale_bin_dir)}
    ensure_bd_on_agent_path(env)

    with patch("os.symlink") as symlink_spy, patch("shutil.copy2") as copy_spy:
        ensure_bd_on_agent_path(env)

    symlink_spy.assert_not_called()
    copy_spy.assert_not_called()


def test_windows_shim_is_a_batch_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator_bd = _make_executable(tmp_path / "agentshore-bd.exe", "1.1.0")
    stale_bin_dir = tmp_path / "stale-bin"
    stale_bin_dir.mkdir()
    stale_bd = _make_executable(stale_bin_dir / "bd", "1.0.4")

    monkeypatch.setenv("AGENTSHORE_BD_BIN", str(orchestrator_bd))
    shim_dir = tmp_path / "shim"
    monkeypatch.setattr("agentshore.beads._bd_shim_dir", lambda: shim_dir)
    monkeypatch.setattr(sys, "platform", "win32")

    env = {"PATH": str(stale_bd.parent)}
    # shutil.which's real Windows-only branch needs _winapi, unavailable when
    # sys.platform is spoofed on a non-Windows test host — stub the lookup
    # result directly rather than exercising that OS-specific internal path.
    with patch("shutil.which", return_value=str(stale_bd)):
        new_env = ensure_bd_on_agent_path(env)

    shim_file = shim_dir / "bd.cmd"
    assert shim_file.is_file()
    content = shim_file.read_text(encoding="utf-8")
    assert str(orchestrator_bd) in content
    assert new_env["PATH"].startswith(str(shim_dir) + os.pathsep)


def test_best_effort_returns_env_unchanged_when_shim_dir_uncreatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator_bd = _make_executable(tmp_path / "agentshore-bd", "1.1.0")
    stale_bin_dir = tmp_path / "stale-bin"
    stale_bin_dir.mkdir()
    _make_executable(stale_bin_dir / "bd", "1.0.4")

    monkeypatch.setenv("AGENTSHORE_BD_BIN", str(orchestrator_bd))
    monkeypatch.setattr("agentshore.beads._bd_shim_dir", lambda: tmp_path / "shim")

    env = {"PATH": str(stale_bin_dir)}
    with (
        structlog.testing.capture_logs() as captured,
        patch.object(Path, "mkdir", side_effect=OSError("read-only filesystem")),
    ):
        result = ensure_bd_on_agent_path(env)

    assert result is env
    assert [e for e in captured if e.get("event") == "bd_shim_create_failed"]
