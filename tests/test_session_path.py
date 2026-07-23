"""Tests for session path discovery and PID helpers."""

from __future__ import annotations

import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest

import agentshore.session_path as sp


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
    """Redirect session metadata into a temp directory."""
    # POSIX: force /tmp so AF_UNIX socket paths stay under the ~104-char
    # sun_path limit (macOS). Windows has neither AF_UNIX nor /tmp.
    tmp_root = None if sys.platform.startswith("win") else "/tmp"
    sessions_dir = Path(tempfile.mkdtemp(prefix="fm_sessions_", dir=tmp_root))
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


def test_find_ipc_tcp_port_uses_stable_range() -> None:
    """The IPC port must come from the fixed app range, not an ephemeral port.

    On Windows the ephemeral range (49152+) is camped by loopback-proxying AV
    (Avast), which crashes the orchestrator's pre-resolved bind with WinError
    10013. The fixed range sidesteps that lottery.
    """
    port = sp.find_ipc_tcp_port()

    assert 9411 <= port < 9512
    # And it must be genuinely bindable, not just in-range.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


def test_find_ipc_tcp_port_falls_back_to_ephemeral_when_range_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If every port in the stable range is occupied, fall back to an OS-assigned
    port rather than raising — strictly no worse than the prior behaviour."""
    monkeypatch.setattr(sp, "find_free_tcp_port", lambda host="127.0.0.1": 55555)

    real_socket = socket.socket

    class _BindAlwaysFails:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._sock = real_socket(*args, **kwargs)

        def __enter__(self) -> _BindAlwaysFails:
            return self

        def __exit__(self, *exc: object) -> None:
            self._sock.close()

        def bind(self, addr: tuple[str, int]) -> None:
            raise OSError("simulated: stable range fully occupied")

    monkeypatch.setattr(sp.socket, "socket", _BindAlwaysFails)

    assert sp.find_ipc_tcp_port() == 55555


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


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX is POSIX-only")
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


def test_is_unix_socket_path_returns_false_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows there are no Unix sockets, so this must return False, not raise."""
    monkeypatch.setattr(sp.sys, "platform", "win32")

    existing = tmp_path / "plain_file"
    existing.write_text("not a socket", encoding="utf-8")
    missing = tmp_path / "does_not_exist"

    assert sp.is_unix_socket_path(existing) is False
    assert sp.is_unix_socket_path(missing) is False


def test_has_live_unix_socket_listener_returns_false_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows ``socket.AF_UNIX`` is absent; the helper must short-circuit to
    False before touching it rather than raising ``AttributeError``."""
    monkeypatch.setattr(sp.sys, "platform", "win32")

    existing = tmp_path / "plain_file"
    existing.write_text("not a socket", encoding="utf-8")
    missing = tmp_path / "does_not_exist"

    assert sp._has_live_unix_socket_listener(existing) is False
    assert sp._has_live_unix_socket_listener(missing) is False


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
    # discover_ipc_endpoint lives in session_process now (staleness detection
    # depends on process liveness); import lazily to keep this module free of
    # a hard dependency on it.
    from agentshore.session_process import discover_ipc_endpoint

    assert discover_ipc_endpoint(project) == endpoint


def test_read_session_info_returns_none_for_missing_or_corrupt(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    assert sp.read_session_info(project) is None

    info_path = sp.session_info_path(project)
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info_path.write_text("{not json", encoding="utf-8")
    assert sp.read_session_info(project) is None


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
