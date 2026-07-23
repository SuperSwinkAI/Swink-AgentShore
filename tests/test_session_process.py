"""Tests for process lifecycle control and the synchronous IPC client.

Split out of ``test_session_path.py`` alongside the ``session_process``
module: PID liveness, SIGTERM/SIGKILL escalation, dashboard-process
stop/reap, and stale-endpoint discovery. Pure path/endpoint-resolution tests
(hashing, pid files, ``cleanup_session``, session info sidecar) stay in
``test_session_path.py``.
"""

from __future__ import annotations

import shutil
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import agentshore.session_path as sp
import agentshore.session_process as spr


def _create_unix_socket_file(path: Path) -> None:
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("AF_UNIX sockets are POSIX-only")
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
    finally:
        sock.close()


@pytest.fixture(autouse=True)
def isolated_sessions_dir(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect session metadata into a temp directory.

    ``_SESSIONS_DIR`` lives on ``session_path`` (the path-resolution module);
    ``session_process``'s functions call back into ``session_path``'s
    ``session_dir``/``session_socket_path`` etc., so patching it there is
    what actually redirects them.
    """
    # POSIX: force /tmp so AF_UNIX socket paths stay under the ~104-char
    # sun_path limit (macOS). Windows has neither AF_UNIX nor /tmp.
    tmp_root = None if sys.platform.startswith("win") else "/tmp"
    sessions_dir = Path(tempfile.mkdtemp(prefix="fm_sessions_process_", dir=tmp_root))
    monkeypatch.setattr(sp, "_SESSIONS_DIR", sessions_dir)
    try:
        yield sessions_dir
    finally:
        shutil.rmtree(sessions_dir, ignore_errors=True)


def test_discover_socket_finds_existing_socket_path(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    socket_path = sp.session_socket_path(project)
    _create_unix_socket_file(socket_path)

    assert spr.discover_socket(project) == socket_path


def test_discover_socket_returns_none_when_missing(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()

    assert spr.discover_socket(project) is None


def test_is_session_running_cleans_up_stale_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    sp.session_pid_path(project).parent.mkdir(parents=True)
    sp.session_pid_path(project).write_text("12345", encoding="utf-8")
    _create_unix_socket_file(sp.session_socket_path(project))

    def raise_os_error(pid: int, signal_number: int) -> None:
        assert pid == 12345
        assert signal_number == 0
        raise OSError

    monkeypatch.setattr(spr.os, "kill", raise_os_error)

    assert spr.is_session_running(project) is False
    assert not sp.session_pid_path(project).exists()
    assert not sp.session_socket_path(project).exists()


def test_stop_dashboard_process_signals_recorded_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal as _signal

    monkeypatch.setattr(spr.sys, "platform", "linux")
    project = tmp_path / "repo"
    project.mkdir()
    sp.write_dashboard_pid(project, 8001)

    killed: list[tuple[int, int]] = []
    alive = {8001}

    def fake_killpg(pid: int, sig: int) -> None:
        killed.append((pid, sig))
        if sig == _signal.SIGTERM:
            alive.discard(pid)

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            if pid not in alive:
                raise OSError
            return
        killed.append((pid, sig))

    monkeypatch.setattr(spr.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(spr.os, "kill", fake_kill)

    assert spr.stop_dashboard_process(project) is True
    assert (8001, _signal.SIGTERM) in killed


def test_stop_dashboard_process_pinned_pid_ignores_overwritten_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pinned ``pid`` terminates that process, not whatever dashboard.pid now
    holds — the supersede path must not kill the freshly-written (self) pid."""
    import signal as _signal

    monkeypatch.setattr(spr.sys, "platform", "linux")
    project = tmp_path / "repo"
    project.mkdir()
    # dashboard.pid has already been overwritten with the new/self pid (9999),
    # but we want to reap the prior dashboard (8001) we validated earlier.
    sp.write_dashboard_pid(project, 9999)

    killed: list[tuple[int, int]] = []
    alive = {8001, 9999}

    def fake_killpg(pid: int, sig: int) -> None:
        killed.append((pid, sig))
        if sig == _signal.SIGTERM:
            alive.discard(pid)

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            if pid not in alive:
                raise OSError
            return
        killed.append((pid, sig))

    monkeypatch.setattr(spr.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(spr.os, "kill", fake_kill)

    assert spr.stop_dashboard_process(project, pid=8001) is True
    assert (8001, _signal.SIGTERM) in killed
    # The self pid in dashboard.pid was never touched.
    assert all(pid != 9999 for pid, _ in killed)


def test_is_session_running_stops_orphan_dashboard_when_pid_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    sp.write_dashboard_pid(project, 8002)
    _create_unix_socket_file(sp.session_socket_path(project))

    stopped: list[Path] = []

    def fake_stop_dashboard(project_path: Path) -> bool:
        stopped.append(project_path)
        return True

    monkeypatch.setattr(spr, "stop_dashboard_process", fake_stop_dashboard)

    assert spr.is_session_running(project) is False
    assert stopped == [project]
    assert not sp.dashboard_pid_path(project).exists()
    assert not sp.session_socket_path(project).exists()


def test_stop_session_no_pid_returns_false(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    assert spr.hard_stop_session(project) is False


def test_stop_session_signals_both_groups_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM both PIDs' process groups, wait for exit, clean up files."""
    import signal as _signal

    monkeypatch.setattr(spr.sys, "platform", "linux")
    monkeypatch.setattr(_signal, "SIGKILL", 9, raising=False)
    project = tmp_path / "repo"
    project.mkdir()
    sp.session_pid_path(project).parent.mkdir(parents=True)
    sp.session_pid_path(project).write_text("4001", encoding="utf-8")
    sp.dashboard_pid_path(project).write_text("4002", encoding="utf-8")
    _create_unix_socket_file(sp.session_socket_path(project))

    killed: list[tuple[int, int]] = []
    alive = {4001, 4002}

    def fake_killpg(pid: int, sig: int) -> None:
        killed.append((pid, sig))
        # Simulate process exit on SIGTERM
        if sig == _signal.SIGTERM:
            alive.discard(pid)

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            if pid not in alive:
                raise OSError
            return
        killed.append((pid, sig))

    monkeypatch.setattr(spr.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(spr.os, "kill", fake_kill)

    assert spr.hard_stop_session(project) is True
    assert (4001, _signal.SIGTERM) in killed
    assert (4002, _signal.SIGTERM) in killed
    # No SIGKILL needed because the SIGTERM "killed" them in our fake
    assert all(sig != _signal.SIGKILL for _pid, sig in killed)
    assert not sp.session_pid_path(project).exists()
    assert not sp.dashboard_pid_path(project).exists()
    assert not sp.session_socket_path(project).exists()


def test_stop_session_escalates_to_sigkill_on_straggler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If SIGTERM doesn't take effect within the grace window, escalate to SIGKILL."""
    import signal as _signal

    monkeypatch.setattr(spr.sys, "platform", "linux")
    monkeypatch.setattr(_signal, "SIGKILL", 9, raising=False)
    project = tmp_path / "repo"
    project.mkdir()
    sp.session_pid_path(project).parent.mkdir(parents=True)
    sp.session_pid_path(project).write_text("5001", encoding="utf-8")

    # Cap the grace windows so the test stays quick.
    monkeypatch.setattr(spr, "_STOP_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(spr, "_STOP_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(spr, "_DASHBOARD_STOP_GRACE_SECONDS", 0.05)

    killed: list[tuple[int, int]] = []
    alive = {5001}

    def fake_killpg(pid: int, sig: int) -> None:
        killed.append((pid, sig))
        if sig == _signal.SIGKILL:
            alive.discard(pid)  # SIGKILL finally takes effect

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            if pid not in alive:
                raise OSError
            return
        killed.append((pid, sig))

    monkeypatch.setattr(spr.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(spr.os, "kill", fake_kill)

    assert spr.hard_stop_session(project) is True
    assert (5001, _signal.SIGTERM) in killed
    assert (5001, _signal.SIGKILL) in killed


def test_hard_stop_returns_false_when_process_survives_sigkill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process still alive after SIGKILL must report failure, not success (#31).

    The desktop-spawned sidecar bug: agentshore stop printed 'session stopped'
    and exited 0 while the orchestrator kept running. hard_stop now confirms the
    PID is gone before returning True.
    """
    import signal as _signal

    monkeypatch.setattr(spr.sys, "platform", "linux")
    monkeypatch.setattr(_signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(spr, "_STOP_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(spr, "_STOP_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(spr, "_DASHBOARD_STOP_GRACE_SECONDS", 0.05)
    project = tmp_path / "repo"
    project.mkdir()
    sp.session_pid_path(project).parent.mkdir(parents=True)
    sp.session_pid_path(project).write_text("5101", encoding="utf-8")

    def fake_killpg(pid: int, sig: int) -> None:
        pass  # process is unkillable in this scenario

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            return  # always alive

    monkeypatch.setattr(spr.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(spr.os, "kill", fake_kill)

    assert spr.hard_stop_session(project) is False


def test_signal_group_falls_back_to_bare_pid_when_not_group_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-group-leader PID (desktop-spawned sidecar) must still be signalled (#31).

    killpg(pid) raises ProcessLookupError when pid doesn't lead a group; the
    controller must fall back to os.kill(pid) instead of giving up.
    """
    import signal as _signal

    def fake_killpg(pid: int, sig: int) -> None:
        raise ProcessLookupError  # pid is not a group leader

    individual: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        individual.append((pid, sig))

    monkeypatch.setattr(spr.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(spr.os, "kill", fake_kill)

    spr._signal_group(9999, _signal.SIGTERM)
    assert individual == [(9999, _signal.SIGTERM)]


def test_stop_session_handles_already_dead_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ProcessLookupError on killpg shouldn't fail stop_session."""
    monkeypatch.setattr(spr.sys, "platform", "linux")
    project = tmp_path / "repo"
    project.mkdir()
    sp.session_pid_path(project).parent.mkdir(parents=True)
    sp.session_pid_path(project).write_text("6001", encoding="utf-8")

    def fake_killpg(pid: int, sig: int) -> None:
        raise ProcessLookupError

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            raise OSError  # already dead
        return

    monkeypatch.setattr(spr.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(spr.os, "kill", fake_kill)

    assert spr.hard_stop_session(project) is True
    assert not sp.session_pid_path(project).exists()


def test_stop_session_uses_taskkill_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    sp.session_pid_path(project).parent.mkdir(parents=True)
    sp.session_pid_path(project).write_text("7001", encoding="utf-8")
    monkeypatch.setattr(spr.sys, "platform", "win32")
    monkeypatch.setattr(spr, "_STOP_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(spr, "_DASHBOARD_STOP_GRACE_SECONDS", 0.05)

    calls: list[list[str]] = []
    alive = {7001}

    class _Completed:
        returncode = 0

    def fake_run(args: list[str], **_kwargs: object) -> _Completed:
        calls.append(args)
        if "/F" in args:
            alive.discard(7001)  # force kill takes effect
        return _Completed()

    # Drive liveness through _process_alive: on Windows the probe is the Win32
    # OpenProcess path, not os.kill(pid, 0), so the test must mock the helper.
    monkeypatch.setattr(spr.subprocess, "run", fake_run)
    monkeypatch.setattr(spr, "_process_alive", lambda pid: pid in alive)

    assert spr.hard_stop_session(project) is True
    assert ["taskkill", "/PID", "7001", "/T"] in calls
    assert ["taskkill", "/PID", "7001", "/T", "/F"] in calls


def test_terminate_process_tree_warns_when_process_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero taskkill returncode for a process that is STILL alive is a
    genuine failure — logged, not silently swallowed or raised."""
    monkeypatch.setattr(spr.sys, "platform", "win32")

    class _Completed:
        returncode = 128

    def fake_run(args: list[str], **_kwargs: object) -> _Completed:
        return _Completed()

    mock_logger = MagicMock()
    monkeypatch.setattr(spr.subprocess, "run", fake_run)
    monkeypatch.setattr(spr, "_logger", mock_logger)
    monkeypatch.setattr(spr, "_process_alive", lambda _pid: True)  # kill failed, still running

    # Must not raise despite the taskkill failure.
    spr._terminate_process_tree(7001, force=False)

    warnings = [
        c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "taskkill_failed"
    ]
    assert len(warnings) == 1
    assert warnings[0].kwargs["pid"] == 7001
    assert warnings[0].kwargs["returncode"] == 128
    assert warnings[0].kwargs["force"] is False


def test_terminate_process_tree_quiet_when_process_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero taskkill exit (e.g. 128 'process not found') for an
    already-dead PID is benign and must not be logged as a failure."""
    monkeypatch.setattr(spr.sys, "platform", "win32")

    class _Completed:
        returncode = 128

    monkeypatch.setattr(spr.subprocess, "run", lambda args, **_kw: _Completed())
    mock_logger = MagicMock()
    monkeypatch.setattr(spr, "_logger", mock_logger)
    monkeypatch.setattr(spr, "_process_alive", lambda _pid: False)  # already gone

    spr._terminate_process_tree(7001, force=False)

    assert [
        c for c in mock_logger.warning.call_args_list if c.args and c.args[0] == "taskkill_failed"
    ] == []


def test_discover_socket_falls_back_to_well_known_path(tmp_path: Path) -> None:
    """No PID file → no liveness check; the socket file's existence is enough."""
    project = tmp_path / "repo"
    project.mkdir()
    sock = sp.session_socket_path(project)
    _create_unix_socket_file(sock)

    assert spr.discover_socket(project) == sock


def test_discover_socket_removes_stale_socket_when_pid_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale socket detection: PID is dead -> cleanup and return None."""
    project = tmp_path / "repo"
    project.mkdir()
    pid_path = sp.session_pid_path(project)
    sock_path = sp.session_socket_path(project)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("31337", encoding="utf-8")
    _create_unix_socket_file(sock_path)

    def dead(pid: int, signum: int) -> None:
        if signum == 0:
            raise OSError
        return

    monkeypatch.setattr(spr.os, "kill", dead)

    assert spr.discover_socket(project) is None
    assert not sock_path.exists()
    assert not pid_path.exists()


def test_discover_socket_uses_info_json_override_when_present(
    tmp_path: Path, isolated_sessions_dir: Path
) -> None:
    """If info.json records a custom socket path that exists, prefer it."""
    project = tmp_path / "repo"
    project.mkdir()
    explicit = isolated_sessions_dir / "custom.sock"
    _create_unix_socket_file(explicit)

    sp.write_session_info(project, socket_path=explicit)

    discovered = spr.discover_socket(project)
    assert discovered == explicit


def test_discover_socket_ignores_info_json_when_target_missing(tmp_path: Path) -> None:
    """If info.json points at a missing socket, fall back to the well-known path."""
    project = tmp_path / "repo"
    project.mkdir()
    explicit = tmp_path / "vanished.sock"  # never created

    sp.write_session_info(project, socket_path=explicit)
    well_known = sp.session_socket_path(project)
    _create_unix_socket_file(well_known)

    assert spr.discover_socket(project) == well_known


# ---------------------------------------------------------------------------
# Windows liveness probe (#71 follow-up): os.kill(pid, 0) is CTRL_C_EVENT on
# Windows, not a null-signal probe, so _process_alive must use the Win32 API.
# ---------------------------------------------------------------------------


def _fake_kernel32(*, open_returns: int, wait_returns: int | None = None) -> MagicMock:
    kernel32 = MagicMock()
    kernel32.OpenProcess.return_value = open_returns
    if wait_returns is not None:
        kernel32.WaitForSingleObject.return_value = wait_returns
    return kernel32


def test_process_alive_windows_running(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    kernel32 = _fake_kernel32(open_returns=1234, wait_returns=0x00000102)  # WAIT_TIMEOUT
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel32, raising=False)
    assert spr._process_alive_windows(4321) is True
    kernel32.CloseHandle.assert_called_once()


def test_process_alive_windows_exited(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    kernel32 = _fake_kernel32(open_returns=1234, wait_returns=0x00000000)  # WAIT_OBJECT_0
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel32, raising=False)
    assert spr._process_alive_windows(4321) is False
    kernel32.CloseHandle.assert_called_once()


def test_process_alive_windows_no_such_pid_is_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    kernel32 = _fake_kernel32(open_returns=0)  # NULL handle
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel32, raising=False)
    monkeypatch.setattr(
        ctypes, "get_last_error", lambda: 87, raising=False
    )  # ERROR_INVALID_PARAMETER
    assert spr._process_alive_windows(4321) is False
    kernel32.CloseHandle.assert_not_called()


def test_process_alive_windows_access_denied_is_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live process we lack rights to open must never be discarded as dead."""
    import ctypes

    kernel32 = _fake_kernel32(open_returns=0)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel32, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)  # ERROR_ACCESS_DENIED
    assert spr._process_alive_windows(4321) is True


def test_process_alive_dispatches_to_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """On win32, _process_alive routes to the Win32 probe, not os.kill."""
    monkeypatch.setattr(spr.sys, "platform", "win32")

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("os.kill must not be used as a probe on Windows")

    monkeypatch.setattr(spr.os, "kill", _boom)
    monkeypatch.setattr(spr, "_process_alive_windows", lambda pid: pid == 4321)
    assert spr._process_alive(4321) is True
    assert spr._process_alive(9999) is False


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows-only Win32 probe")
def test_process_alive_windows_real_roundtrip() -> None:
    """End-to-end: a detached, no-window child is seen alive, then dead, by an
    unrelated probe — the exact path `agentshore stop` exercises."""
    import subprocess
    import time

    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=flags,
    )
    try:
        assert spr._process_alive(proc.pid) is True
    finally:
        proc.kill()
        proc.wait(timeout=10)
    time.sleep(0.3)
    assert spr._process_alive(proc.pid) is False
