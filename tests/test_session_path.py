"""Tests for session path discovery and PID helpers."""

from __future__ import annotations

import shutil
import socket
import tempfile
from pathlib import Path

import pytest

import agentshore.session_path as sp


def _create_unix_socket_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
    finally:
        sock.close()


@pytest.fixture(autouse=True)
def isolated_sessions_dir(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect session metadata into a temp directory."""
    sessions_dir = Path(tempfile.mkdtemp(prefix="fm_sessions_", dir="/tmp"))
    monkeypatch.setattr(sp, "_SESSIONS_DIR", sessions_dir)
    try:
        yield sessions_dir
    finally:
        shutil.rmtree(sessions_dir, ignore_errors=True)


def test_project_hash_is_stable_and_short(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()

    first = sp._project_hash(project.resolve())
    second = sp._project_hash(project.resolve())

    assert first == second
    assert len(first) == 16
    assert all(ch in "0123456789abcdef" for ch in first)


def test_session_paths_use_resolved_project_hash(
    tmp_path: Path,
    isolated_sessions_dir: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()

    expected_dir = isolated_sessions_dir / sp._project_hash(project.resolve())

    assert sp.session_dir(project) == expected_dir
    assert sp.session_socket_path(project) == expected_dir / "socket.sock"
    assert sp.session_pid_path(project) == expected_dir / "agentshore.pid"


def test_discover_socket_finds_existing_socket_path(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    socket_path = sp.session_socket_path(project)
    _create_unix_socket_file(socket_path)

    assert sp.discover_socket(project) == socket_path


def test_discover_socket_returns_none_when_missing(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()

    assert sp.discover_socket(project) is None


def test_pid_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    monkeypatch.setattr(sp.os, "getpid", lambda: 12345)

    sp.write_pid(project)

    assert sp.read_pid(project) == 12345


def test_read_pid_returns_none_for_invalid_file(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    pid_path = sp.session_pid_path(project)
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text("not-a-pid", encoding="utf-8")

    assert sp.read_pid(project) is None


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

    monkeypatch.setattr(sp.os, "kill", raise_os_error)

    assert sp.is_session_running(project) is False
    assert not sp.session_pid_path(project).exists()
    assert not sp.session_socket_path(project).exists()


def test_cleanup_session_removes_pid_and_socket(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    pid_path = sp.session_pid_path(project)
    dash_pid_path = sp.dashboard_pid_path(project)
    socket_path = sp.session_socket_path(project)
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text("12345", encoding="utf-8")
    dash_pid_path.write_text("12346", encoding="utf-8")
    _create_unix_socket_file(socket_path)

    sp.cleanup_session(project)

    assert not pid_path.exists()
    assert not dash_pid_path.exists()
    assert not socket_path.exists()


def test_cleanup_session_preserves_socket_with_live_listener(tmp_path: Path) -> None:
    """Regression for desktop-6e1.

    If a process is still listening on the session socket — e.g. the
    orchestrator is alive but its session.pid was never written — cleanup
    must NOT unlink the socket, otherwise agentshore dashboard becomes unable
    to reconnect and the running session is effectively orphaned.
    """
    project = tmp_path / "repo"
    project.mkdir()
    socket_path = sp.session_socket_path(project)
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    try:
        sp.cleanup_session(project)
        assert sp.is_unix_socket_path(socket_path), (
            "cleanup_session unlinked a socket that still has a live listener"
        )
    finally:
        listener.close()
        # After closing the listener, no-listener cleanup is now valid.
        if sp.is_unix_socket_path(socket_path):
            socket_path.unlink()


def test_cleanup_session_unlinks_socket_with_no_listener(tmp_path: Path) -> None:
    """The defensive listener check must still allow cleanup of orphaned sockets."""
    project = tmp_path / "repo"
    project.mkdir()
    socket_path = sp.session_socket_path(project)
    _create_unix_socket_file(socket_path)  # bound + closed, no listener

    sp.cleanup_session(project)

    assert not socket_path.exists()


def test_cleanup_session_preserves_regular_socket_path_file(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    socket_path = sp.session_socket_path(project)
    socket_path.parent.mkdir(parents=True)
    socket_path.write_text("not a socket", encoding="utf-8")

    sp.cleanup_session(project)

    assert socket_path.read_text(encoding="utf-8") == "not a socket"


def test_dashboard_pid_round_trip(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    sp.write_dashboard_pid(project, 99999)
    assert sp.read_dashboard_pid(project) == 99999


def test_stop_dashboard_process_signals_recorded_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal as _signal

    monkeypatch.setattr(sp.sys, "platform", "linux")
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

    monkeypatch.setattr(sp.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(sp.os, "kill", fake_kill)

    assert sp.stop_dashboard_process(project) is True
    assert (8001, _signal.SIGTERM) in killed


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

    monkeypatch.setattr(sp, "stop_dashboard_process", fake_stop_dashboard)

    assert sp.is_session_running(project) is False
    assert stopped == [project]
    assert not sp.dashboard_pid_path(project).exists()
    assert not sp.session_socket_path(project).exists()


def test_stop_session_no_pid_returns_false(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    assert sp.stop_session(project) is False


def test_stop_session_signals_both_groups_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM both PIDs' process groups, wait for exit, clean up files."""
    import signal as _signal

    monkeypatch.setattr(sp.sys, "platform", "linux")
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

    monkeypatch.setattr(sp.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(sp.os, "kill", fake_kill)

    assert sp.stop_session(project) is True
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

    monkeypatch.setattr(sp.sys, "platform", "linux")
    monkeypatch.setattr(_signal, "SIGKILL", 9, raising=False)
    project = tmp_path / "repo"
    project.mkdir()
    sp.session_pid_path(project).parent.mkdir(parents=True)
    sp.session_pid_path(project).write_text("5001", encoding="utf-8")

    # Cap the grace windows so the test stays quick.
    monkeypatch.setattr(sp, "_STOP_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(sp, "_STOP_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(sp, "_DASHBOARD_STOP_GRACE_SECONDS", 0.05)

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

    monkeypatch.setattr(sp.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(sp.os, "kill", fake_kill)

    assert sp.stop_session(project) is True
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

    monkeypatch.setattr(sp.sys, "platform", "linux")
    monkeypatch.setattr(_signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(sp, "_STOP_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(sp, "_STOP_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(sp, "_DASHBOARD_STOP_GRACE_SECONDS", 0.05)
    project = tmp_path / "repo"
    project.mkdir()
    sp.session_pid_path(project).parent.mkdir(parents=True)
    sp.session_pid_path(project).write_text("5101", encoding="utf-8")

    def fake_killpg(pid: int, sig: int) -> None:
        pass  # process is unkillable in this scenario

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            return  # always alive

    monkeypatch.setattr(sp.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(sp.os, "kill", fake_kill)

    assert sp.stop_session(project) is False


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

    monkeypatch.setattr(sp.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(sp.os, "kill", fake_kill)

    sp.SessionProcessController._signal_group(9999, _signal.SIGTERM)
    assert individual == [(9999, _signal.SIGTERM)]


def test_stop_session_handles_already_dead_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ProcessLookupError on killpg shouldn't fail stop_session."""
    monkeypatch.setattr(sp.sys, "platform", "linux")
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

    monkeypatch.setattr(sp.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(sp.os, "kill", fake_kill)

    assert sp.stop_session(project) is True
    assert not sp.session_pid_path(project).exists()


def test_stop_session_uses_taskkill_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    sp.session_pid_path(project).parent.mkdir(parents=True)
    sp.session_pid_path(project).write_text("7001", encoding="utf-8")
    monkeypatch.setattr(sp.sys, "platform", "win32")
    monkeypatch.setattr(sp, "_STOP_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(sp, "_DASHBOARD_STOP_GRACE_SECONDS", 0.05)

    calls: list[list[str]] = []
    alive = {7001}

    def fake_run(args: list[str], **_kwargs: object) -> object:
        calls.append(args)
        if "/F" in args:
            alive.discard(7001)  # force kill takes effect
        return object()

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            if pid not in alive:
                raise OSError
            return

    monkeypatch.setattr(sp.subprocess, "run", fake_run)
    monkeypatch.setattr(sp.os, "kill", fake_kill)

    assert sp.stop_session(project) is True
    assert ["taskkill", "/PID", "7001", "/T"] in calls
    assert ["taskkill", "/PID", "7001", "/T", "/F"] in calls


def test_project_hash_differs_for_different_paths(tmp_path: Path) -> None:
    """Different project paths must produce distinct hashes."""
    a = tmp_path / "repo-a"
    b = tmp_path / "repo-b"
    a.mkdir()
    b.mkdir()
    assert sp._project_hash(a.resolve()) != sp._project_hash(b.resolve())


def test_project_hash_independent_of_relative_form(tmp_path: Path) -> None:
    """The hash depends on the absolute resolved path, not the cwd-relative form."""
    project = tmp_path / "repo"
    project.mkdir()

    via_resolve = sp._project_hash(project.resolve())
    via_absolute = sp._project_hash(Path(str(project)).resolve())

    assert via_resolve == via_absolute


def test_session_info_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """write_session_info should record pid/started_at/project/socket as JSON."""
    project = tmp_path / "repo"
    project.mkdir()
    monkeypatch.setattr(sp.os, "getpid", lambda: 7777)

    info_path = sp.write_session_info(project, extra={"mode": "agent"})
    assert info_path == sp.session_info_path(project)

    info = sp.read_session_info(project)
    assert info is not None
    assert info["pid"] == 7777
    assert info["project_path"] == str(project.resolve())
    assert info["socket"] == str(sp.session_socket_path(project))
    assert info["mode"] == "agent"
    assert isinstance(info.get("started_at"), str)


def test_session_info_records_explicit_socket_override(tmp_path: Path) -> None:
    """When --socket is overridden, info.json records the explicit path."""
    project = tmp_path / "repo"
    project.mkdir()
    explicit = tmp_path / "custom.sock"

    sp.write_session_info(project, socket_path=explicit)
    info = sp.read_session_info(project)
    assert info is not None
    assert info["socket"] == str(explicit)
    assert info["ipc"] == {"kind": "unix", "path": str(explicit)}


def test_session_info_records_tcp_endpoint(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    endpoint = sp.IpcEndpoint.tcp("127.0.0.1", 54321)

    sp.write_session_info(project, ipc_endpoint=endpoint)

    info = sp.read_session_info(project)
    assert info is not None
    assert info["ipc"] == {"kind": "tcp", "host": "127.0.0.1", "port": 54321}
    assert sp.discover_ipc_endpoint(project) == endpoint


def test_read_session_info_returns_none_for_missing_or_corrupt(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    assert sp.read_session_info(project) is None

    info_path = sp.session_info_path(project)
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info_path.write_text("{not json", encoding="utf-8")
    assert sp.read_session_info(project) is None


def test_discover_socket_falls_back_to_well_known_path(tmp_path: Path) -> None:
    """No PID file → no liveness check; the socket file's existence is enough."""
    project = tmp_path / "repo"
    project.mkdir()
    sock = sp.session_socket_path(project)
    _create_unix_socket_file(sock)

    assert sp.discover_socket(project) == sock


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

    monkeypatch.setattr(sp.os, "kill", dead)

    assert sp.discover_socket(project) is None
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

    discovered = sp.discover_socket(project)
    assert discovered == explicit


def test_discover_socket_ignores_info_json_when_target_missing(tmp_path: Path) -> None:
    """If info.json points at a missing socket, fall back to the well-known path."""
    project = tmp_path / "repo"
    project.mkdir()
    explicit = tmp_path / "vanished.sock"  # never created

    sp.write_session_info(project, socket_path=explicit)
    well_known = sp.session_socket_path(project)
    _create_unix_socket_file(well_known)

    assert sp.discover_socket(project) == well_known


def test_cleanup_session_removes_info_and_empty_dir(tmp_path: Path) -> None:
    """cleanup_session also removes info.json and empties the per-project dir."""
    project = tmp_path / "repo"
    project.mkdir()

    sp.write_pid(project)
    sp.write_session_info(project)
    _create_unix_socket_file(sp.session_socket_path(project))

    sp.cleanup_session(project)

    assert not sp.session_pid_path(project).exists()
    assert not sp.session_socket_path(project).exists()
    assert not sp.session_info_path(project).exists()
    assert not sp.session_dir(project).exists()


def test_cleanup_session_unlinks_external_socket_recorded_in_info(
    tmp_path: Path, isolated_sessions_dir: Path
) -> None:
    """If info.json points at a socket outside the session dir, unlink it too."""
    project = tmp_path / "repo"
    project.mkdir()
    external = isolated_sessions_dir / "outside.sock"
    _create_unix_socket_file(external)

    sp.write_session_info(project, socket_path=external)
    sp.cleanup_session(project)

    assert not external.exists()
    assert not sp.session_dir(project).exists()


def test_cleanup_session_preserves_dir_with_other_files(tmp_path: Path) -> None:
    """Don't delete the session dir if it still holds non-tracked files."""
    project = tmp_path / "repo"
    project.mkdir()

    sd = sp.session_dir(project)
    sd.mkdir(parents=True, exist_ok=True)
    log = sd / "agentshore.log"
    log.write_text("ongoing log\n", encoding="utf-8")
    _create_unix_socket_file(sp.session_socket_path(project))

    sp.cleanup_session(project)

    assert log.exists()
    assert sd.exists()
    assert not sp.session_socket_path(project).exists()
