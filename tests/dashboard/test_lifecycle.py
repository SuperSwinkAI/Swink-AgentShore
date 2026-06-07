"""Tests for src/agentshore/dashboard/lifecycle.py — shared bridge lifecycle.

Consolidates the pid/supersede/port logic that had diverged across the
standalone ``agentshore dashboard`` command and the ``start --dashboard``
launcher. The Windows uv-trampoline self-kill guard and the
reap-before-orchestrator-spawn ordering live here now.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.dashboard import lifecycle


def test_supersede_excludes_own_lineage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A pid in our own lineage (getpid/getppid) is never reaped."""
    reaped: list[int] = []
    # dashboard.pid records *our own* pid — reaping it would kill our tree.
    monkeypatch.setattr(lifecycle.session_path, "read_dashboard_pid", lambda _p: 4321)
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 4321)
    monkeypatch.setattr(lifecycle.os, "getppid", lambda: 1)
    monkeypatch.setattr(
        lifecycle.session_path,
        "stop_dashboard_process",
        lambda _p, *, pid=None: reaped.append(pid) or True,
    )

    assert lifecycle.supersede_prior_bridge(tmp_path) is False
    assert reaped == []


def test_supersede_reaps_foreign_pid_with_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A prior bridge owned by another pid is reaped, pinning that exact pid."""
    reaped: list[int | None] = []
    monkeypatch.setattr(lifecycle.session_path, "read_dashboard_pid", lambda _p: 9999)
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 4321)
    monkeypatch.setattr(lifecycle.os, "getppid", lambda: 1)
    monkeypatch.setattr(
        lifecycle.session_path,
        "stop_dashboard_process",
        lambda _p, *, pid=None: reaped.append(pid) or True,
    )

    assert lifecycle.supersede_prior_bridge(tmp_path) is True
    assert reaped == [9999]


def test_supersede_noop_when_no_prior_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lifecycle.session_path, "read_dashboard_pid", lambda _p: None)
    called = False

    def _stop(_p: Path, *, pid: int | None = None) -> bool:
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(lifecycle.session_path, "stop_dashboard_process", _stop)
    assert lifecycle.supersede_prior_bridge(tmp_path) is False
    assert called is False


def test_reap_before_orchestrator_spawn_reads_disk_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launcher reap has no own-lineage exclusion — it stops the disk pid."""
    calls: list[tuple[Path, int | None]] = []
    monkeypatch.setattr(
        lifecycle.session_path,
        "stop_dashboard_process",
        lambda p, *, pid=None: calls.append((p, pid)) or True,
    )
    assert lifecycle.reap_before_orchestrator_spawn(tmp_path) is True
    assert calls == [(tmp_path, None)]


def test_claim_bridge_pid_writes_own_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    written: list[tuple[Path, int]] = []
    monkeypatch.setattr(lifecycle.os, "getpid", lambda: 7777)
    monkeypatch.setattr(
        lifecycle.session_path,
        "write_dashboard_pid",
        lambda p, pid: written.append((p, pid)),
    )
    lifecycle.claim_bridge_pid(tmp_path)
    assert written == [(tmp_path, 7777)]


def test_select_dashboard_port_prefers_explicit() -> None:
    assert lifecycle.select_dashboard_port(8123) == 8123


def test_select_dashboard_port_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle.session_path, "find_dashboard_port", lambda: 9407)
    assert lifecycle.select_dashboard_port() == 9407
