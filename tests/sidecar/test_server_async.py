"""Tests for the async JSON-RPC entry points (DESIGN §1.2)."""

from __future__ import annotations

import asyncio
import io
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentshore import __version__ as agentshore_version
from agentshore.data.models import ArchiveRecord, PlayRecord, SessionRecord
from agentshore.data.store import DataStore
from agentshore.session_path import IpcEndpoint
from agentshore.sidecar.embedded_bridge import EmbeddedBridge
from agentshore.sidecar.handshake import PROTOCOL_VERSION, build_response
from agentshore.sidecar.server import (
    ERR_SESSION_ACTIVE,
    INVALID_PARAMS,
    METHOD_HANDLERS,
    REQUEST_CANCELLED,
    ServerState,
    SessionContext,
    _reader_loop,
    _serve_async,
    build_agent_subprocess_exited_notification,
    build_agent_subprocess_spawned_notification,
    build_session_completed_notification,
    build_sidecar_health_notification,
    handle_request,
    run_async,
    serve_async,
)


@pytest.fixture()
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<html><body>test</body></html>", encoding="utf-8")
    return d


@pytest.mark.asyncio
async def test_serve_async_round_trips_handshake() -> None:
    sidecar_build_id = build_response()["sidecar_build_id"]
    stdin = io.StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "app.handshake",
                "params": {"client": "agentshore-desktop", "client_build_id": sidecar_build_id},
            }
        )
        + "\n"
    )
    stdout = io.StringIO()
    await serve_async(stdin, stdout)
    [reply_line] = [line for line in stdout.getvalue().splitlines() if line.strip()]
    reply = json.loads(reply_line)
    assert reply["id"] == 1
    assert reply["result"]["protocol_version"] == PROTOCOL_VERSION
    assert reply["result"]["agentshore_version"] == agentshore_version


@pytest.mark.asyncio
async def test_serve_async_handles_multiple_requests() -> None:
    sidecar_build_id = build_response()["sidecar_build_id"]
    payloads = [
        {
            "jsonrpc": "2.0",
            "id": "a",
            "method": "app.handshake",
            "params": {"client": "agentshore-desktop", "client_build_id": sidecar_build_id},
        },
        {"jsonrpc": "2.0", "id": "b", "method": "missing"},
    ]
    stdin = io.StringIO("\n".join(json.dumps(p) for p in payloads) + "\n")
    stdout = io.StringIO()
    await serve_async(stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert [r["id"] for r in replies] == ["a", "b"]
    assert "result" in replies[0]
    assert "error" in replies[1]


@pytest.mark.asyncio
async def test_serve_async_returns_on_eof() -> None:
    stdin = io.StringIO("")
    stdout = io.StringIO()
    # ``health_interval_seconds=0`` disables the heartbeat so EOF on empty
    # stdin yields a clean empty stdout (regression for any future heartbeat
    # default-firing surprise on synchronous one-shot tests).
    await asyncio.wait_for(
        serve_async(stdin, stdout, health_interval_seconds=0),
        timeout=1.0,
    )
    assert stdout.getvalue() == ""


@pytest.mark.asyncio
async def test_serve_async_emits_periodic_sidecar_health_notifications() -> None:
    """``sidecar.health`` fires on a fixed interval (DESIGN §5.1).

    Closes part of desktop-8e1: the health notification builder existed but
    was never invoked. Drives ``serve_async`` with a small interval and a
    stdin that briefly stalls so the heartbeat fires before EOF.
    """
    import time

    class _StalledThenEofStdin:
        def __init__(self, stall_seconds: float) -> None:
            self._stall_seconds = stall_seconds
            self._stalled = False

        def readline(self) -> str:
            if not self._stalled:
                self._stalled = True
                time.sleep(self._stall_seconds)
            return ""

    stdin = _StalledThenEofStdin(stall_seconds=0.2)
    stdout = io.StringIO()
    await asyncio.wait_for(
        serve_async(stdin, stdout, health_interval_seconds=0.05),
        timeout=2.0,
    )
    notifications = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    health = [n for n in notifications if n.get("method") == "sidecar.health"]
    assert len(health) >= 1, f"expected at least one sidecar.health, got {notifications!r}"
    params = health[0]["params"]
    assert params["status"] == "ok"
    assert isinstance(params["timestamp"], str)
    # All emitted notifications should carry the documented JSON-RPC 2.0 envelope.
    for note in health:
        assert note["jsonrpc"] == "2.0"
        assert note["method"] == "sidecar.health"


@pytest.mark.asyncio
async def test_serve_async_cancels_health_emitter_on_shutdown() -> None:
    """Heartbeat task must be cancelled and awaited when stdin closes."""
    stdin = io.StringIO("")
    stdout = io.StringIO()
    # Real (positive) interval so the heartbeat task is created; EOF then
    # forces the finally-block to cancel it. ``serve_async`` must return
    # promptly without leaking the task.
    await asyncio.wait_for(
        serve_async(stdin, stdout, health_interval_seconds=10.0),
        timeout=1.0,
    )


@pytest.mark.asyncio
async def test_serve_async_cancel_responds_before_handler_cleanup_completes() -> None:
    """Cancel reply is written immediately; handler cleanup drains in the
    background. Regression for desktop-y4g — never block the serve loop on a
    cancelled handler's `finally` clause.
    """
    cleanup_done = asyncio.Event()
    started = threading.Event()
    cancel_observed_after_cleanup: bool | None = None

    class _ProbeStdout(io.StringIO):
        def write(self, s: str) -> int:
            nonlocal cancel_observed_after_cleanup
            if "request cancelled" in s:
                cancel_observed_after_cleanup = cleanup_done.is_set()
            return super().write(s)

    async def _slow_handler(_payload: object) -> object:
        started.set()
        try:
            await asyncio.sleep(5)
        finally:
            # Hold the event loop briefly inside `finally` to prove the cancel
            # reply lands before this cleanup completes.
            await asyncio.sleep(0.05)
            cleanup_done.set()

    METHOD_HANDLERS["test.slow_cleanup"] = _slow_handler
    try:

        class _ControlledStdin:
            def __iter__(self) -> object:
                yield json.dumps({"jsonrpc": "2.0", "id": 1, "method": "test.slow_cleanup"}) + "\n"
                started.wait(timeout=1.0)
                yield (
                    json.dumps({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": 1}})
                    + "\n"
                )

        stdin = _ControlledStdin()
        stdout = _ProbeStdout()
        await asyncio.wait_for(_serve_async(stdin, stdout), timeout=3.0)
    finally:
        METHOD_HANDLERS.pop("test.slow_cleanup", None)

    replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert replies == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": REQUEST_CANCELLED, "message": "request cancelled"},
        }
    ]
    # Cancel reply must be observed *before* cleanup completes — i.e. the
    # cleanup_done flag is still clear at the moment we wrote "request cancelled".
    assert cancel_observed_after_cleanup is False
    # And cleanup must still run to completion — serve_async drains pending
    # tasks before returning, so the event is set by the time we get here.
    assert cleanup_done.is_set()


@pytest.mark.asyncio
async def test_serve_async_cancel_swallows_non_cancelled_error_from_handler_cleanup() -> None:
    """Handler `finally` that raises a non-CancelledError must not crash the
    serve loop. Regression for desktop-6hd.
    """
    started = threading.Event()

    async def _bad_cleanup_handler(_payload: object) -> object:
        started.set()
        try:
            await asyncio.sleep(5)
        finally:
            raise RuntimeError("cleanup boom")

    METHOD_HANDLERS["test.bad_cleanup"] = _bad_cleanup_handler
    try:

        class _ControlledStdin:
            def __iter__(self) -> object:
                yield json.dumps({"jsonrpc": "2.0", "id": 7, "method": "test.bad_cleanup"}) + "\n"
                started.wait(timeout=1.0)
                yield (
                    json.dumps({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": 7}})
                    + "\n"
                )

        stdin = _ControlledStdin()
        stdout = io.StringIO()
        # The loop must complete cleanly — if the RuntimeError escapes,
        # `_serve_async` would propagate it and this `wait_for` would raise.
        await asyncio.wait_for(_serve_async(stdin, stdout), timeout=3.0)
    finally:
        METHOD_HANDLERS.pop("test.bad_cleanup", None)

    replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert replies == [
        {
            "jsonrpc": "2.0",
            "id": 7,
            "error": {"code": REQUEST_CANCELLED, "message": "request cancelled"},
        }
    ]


@pytest.mark.asyncio
async def test_run_async_hosts_bridge_alongside_stdio(
    monkeypatch: pytest.MonkeyPatch, static_dir: Path
) -> None:
    """``run_async`` starts the bridge, serves a request, then stops the bridge."""
    bridge = EmbeddedBridge(
        IpcEndpoint.tcp("127.0.0.1", 0),
        session_dir=static_dir.parent,
        static_dir=static_dir,
    )

    sidecar_build_id = build_response()["sidecar_build_id"]
    request = {
        "jsonrpc": "2.0",
        "id": 99,
        "method": "app.handshake",
        "params": {"client": "agentshore-desktop", "client_build_id": sidecar_build_id},
    }
    stdin = io.StringIO(json.dumps(request) + "\n")
    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", stdout)

    await asyncio.wait_for(run_async(bridge=bridge), timeout=15.0)

    replies = [
        json.loads(line) for line in stdout.getvalue().splitlines() if line.strip().startswith("{")
    ]
    handshake_replies = [r for r in replies if isinstance(r, dict) and r.get("id") == 99]
    assert len(handshake_replies) == 1, replies
    reply = handshake_replies[0]
    assert reply["result"]["protocol_version"] == PROTOCOL_VERSION
    assert bridge.is_running is False


async def _populated_session(db_path: Path) -> tuple[DataStore, str, str]:
    """Build a tiny DataStore with one session + one archive row."""
    store = DataStore(db_path)
    await store.initialize()
    session_id = "session-1"
    archive_id = "archive-1"
    started = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC).isoformat()
    ended = datetime(2026, 5, 1, 13, 0, 0, tzinfo=UTC).isoformat()
    await store.create_session(
        SessionRecord(
            session_id=session_id,
            project_path="/tmp/proj",
            started_at=started,
            ended_at=ended,
            status="ended",
            final_alignment=0.7,
        )
    )
    await store.record_play(
        PlayRecord(
            session_id=session_id,
            play_type="issue_pickup",
            started_at=started,
            ended_at=ended,
            success=True,
            dollar_cost=1.0,
        )
    )
    await store.create_archive(
        ArchiveRecord(
            archive_id=archive_id,
            session_id=session_id,
            archive_path="/tmp/archive",
            total_cost=1.0,
            final_alignment=0.7,
            total_plays=1,
            created_at=ended,
        )
    )
    return store, session_id, archive_id


@pytest.mark.asyncio
async def test_session_stop_returns_full_esr_payload(tmp_path: Path) -> None:
    store, session_id, _ = await _populated_session(tmp_path / "db.sqlite")
    try:
        state = ServerState(
            session_active=True,
            session_context=SessionContext(
                session_id=session_id,
                store=store,
                archive_path="/tmp/archive",
                report_path="/tmp/archive/report.html",
                log_path="/tmp/archive/session.log",
            ),
        )
        response = await _drive(
            {"jsonrpc": "2.0", "id": 1, "method": "session.stop"},
            state=state,
        )
    finally:
        await store.close()
    assert "result" in response, response
    result = response["result"]
    assert set(result.keys()) == {
        "session_id",
        "exit_reason",
        "exit_code",
        "archive_path",
        "report_path",
        "log_path",
        "esr_summary",
    }
    assert result["session_id"] == session_id
    assert result["archive_path"] == "/tmp/archive"
    assert result["report_path"] == "/tmp/archive/report.html"
    assert result["log_path"] == "/tmp/archive/session.log"
    # exit_reason defaults to a stable value when params omit it.
    assert isinstance(result["exit_reason"], str)
    assert isinstance(result["exit_code"], int)


@pytest.mark.asyncio
async def test_session_stop_returns_paths_emitted_during_orchestrator_stop(
    tmp_path: Path,
) -> None:
    store, session_id, _ = await _populated_session(tmp_path / "db.sqlite")
    generated_report_path = "/tmp/project/.agentshore/reports/end-session-sid.html"
    generated_log_path = "/tmp/project/.agentshore/logs/agentshore-sid.log"

    class _StopOrch:
        def request_drain(self, _reason: str) -> None:
            pass

        async def stop(self) -> None:
            assert state.session_context is not None
            state.session_context.report_path = generated_report_path
            state.session_context.log_path = generated_log_path

    state = ServerState(
        session_active=True,
        session_context=SessionContext(
            session_id=session_id,
            store=store,
            archive_path="/tmp/archive",
            report_path="",
            log_path="/tmp/archive/session.log",
        ),
        orchestrator=_StopOrch(),
    )
    try:
        response = await _drive(
            {"jsonrpc": "2.0", "id": 1, "method": "session.stop"},
            state=state,
        )
    finally:
        await store.close()

    assert "result" in response, response
    result = response["result"]
    assert result["report_path"] == generated_report_path
    assert result["log_path"] == generated_log_path


@pytest.mark.asyncio
async def test_session_stop_without_active_session_returns_session_inactive() -> None:
    response = await _drive(
        {"jsonrpc": "2.0", "id": 2, "method": "session.stop"},
        state=ServerState(),
    )
    assert "error" in response, response
    assert response["error"]["code"] == ERR_SESSION_ACTIVE


@pytest.mark.asyncio
async def test_session_start_then_status_reports_running_state() -> None:
    state = ServerState(ipc_endpoint={"kind": "ws", "url": "ws://127.0.0.1:9473/ws"})
    start = await _drive(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "session.start",
        },
        state=state,
    )
    assert "result" in start
    assert isinstance(start["result"]["session_id"], str)
    assert start["result"]["ipc_endpoint"] == {"kind": "ws", "url": "ws://127.0.0.1:9473/ws"}

    status = await _drive(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "session.status",
        },
        state=state,
    )
    assert "result" in status
    assert status["result"]["state"] == "running"
    assert status["result"]["session_id"] == start["result"]["session_id"]
    assert isinstance(status["result"]["started_at"], str)


@pytest.mark.asyncio
async def test_session_status_idle_without_active_session() -> None:
    status = await _drive(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "session.status",
        },
        state=ServerState(),
    )
    assert "result" in status
    assert status["result"] == {"state": "idle", "session_id": None, "started_at": None}


@pytest.mark.asyncio
async def test_archive_list_returns_rows(tmp_path: Path) -> None:
    store, _, archive_id = await _populated_session(tmp_path / "db.sqlite")
    try:
        state = ServerState(data_store=store)
        response = await _drive(
            {"jsonrpc": "2.0", "id": 3, "method": "archive.list"},
            state=state,
        )
    finally:
        await store.close()
    assert "result" in response, response
    rows = response["result"]
    assert len(rows) == 1
    assert rows[0]["archive_id"] == archive_id


@pytest.mark.asyncio
async def test_archive_fetch_report_returns_html_path_and_sections(tmp_path: Path) -> None:
    store, _, archive_id = await _populated_session(tmp_path / "db.sqlite")
    html_file = tmp_path / "report.html"
    html_file.write_text(
        '<html><body><section id="x"><h2>X</h2></section></body></html>',
        encoding="utf-8",
    )
    # write report path into the archive_path-relative location
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "report.html").write_text(
        '<html><body><section id="x"><h2>X</h2></section></body></html>',
        encoding="utf-8",
    )
    # Point archive at the dir
    await store.create_archive(
        ArchiveRecord(
            archive_id="archive-2",
            session_id="session-1",
            archive_path=str(archive_dir),
            total_cost=0.0,
            final_alignment=0.0,
            total_plays=0,
            created_at=datetime(2026, 5, 1, 13, 0, 0, tzinfo=UTC).isoformat(),
        )
    )
    try:
        state = ServerState(data_store=store)
        response = await _drive(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "archive.fetch_report",
                "params": {"archive_id": "archive-2"},
            },
            state=state,
        )
    finally:
        await store.close()
    assert "result" in response, response
    result = response["result"]
    assert result["sections"] == [{"id": "x", "title": "X"}]
    assert result["html_path"].endswith("report.html")


@pytest.mark.asyncio
async def test_archive_fetch_report_missing_archive_returns_invalid_params(
    tmp_path: Path,
) -> None:
    store, _, _ = await _populated_session(tmp_path / "db.sqlite")
    try:
        state = ServerState(data_store=store)
        response = await _drive(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "archive.fetch_report",
                "params": {"archive_id": "missing"},
            },
            state=state,
        )
    finally:
        await store.close()
    assert "error" in response, response
    assert response["error"]["code"] == INVALID_PARAMS


@pytest.mark.asyncio
async def test_archive_fetch_logs_default_returns_first_200_lines(tmp_path: Path) -> None:
    store, _, archive_id = await _populated_session(tmp_path / "db.sqlite")
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    log_file = archive_dir / "session.log"
    log_file.write_text("\n".join(f"line-{i}" for i in range(1, 501)) + "\n", encoding="utf-8")
    await store.create_archive(
        ArchiveRecord(
            archive_id="archive-with-logs",
            session_id="session-1",
            archive_path=str(archive_dir),
            total_cost=0.0,
            final_alignment=0.0,
            total_plays=0,
            created_at=datetime(2026, 5, 1, 13, 0, 0, tzinfo=UTC).isoformat(),
        )
    )
    try:
        state = ServerState(data_store=store)
        response = await _drive(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "archive.fetch_logs",
                "params": {"archive_id": "archive-with-logs"},
            },
            state=state,
        )
    finally:
        await store.close()
    assert "result" in response, response
    assert len(response["result"]["lines"]) == 200
    assert response["result"]["lines"][0] == "line-1"


@pytest.mark.asyncio
async def test_archive_fetch_logs_malformed_range_returns_invalid_params(tmp_path: Path) -> None:
    store, _, _ = await _populated_session(tmp_path / "db.sqlite")
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "session.log").write_text("a\nb\nc\n", encoding="utf-8")
    await store.create_archive(
        ArchiveRecord(
            archive_id="archive-bad-range",
            session_id="session-1",
            archive_path=str(archive_dir),
            total_cost=0.0,
            final_alignment=0.0,
            total_plays=0,
            created_at=datetime(2026, 5, 1, 13, 0, 0, tzinfo=UTC).isoformat(),
        )
    )
    try:
        state = ServerState(data_store=store)
        response = await _drive(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "archive.fetch_logs",
                "params": {"archive_id": "archive-bad-range", "range": {"start": -1, "end": 50}},
            },
            state=state,
        )
    finally:
        await store.close()
    assert "error" in response, response
    assert response["error"]["code"] == INVALID_PARAMS


def test_build_session_completed_notification_shape() -> None:
    payload = {
        "session_id": "s1",
        "exit_reason": "user_stop",
        "exit_code": 0,
        "archive_path": "/tmp/a",
        "report_path": "/tmp/a/report.html",
        "log_path": "/tmp/a/session.log",
        "esr_summary": {},
    }
    notification = build_session_completed_notification(payload)
    assert notification["jsonrpc"] == "2.0"
    assert notification["method"] == "session.completed"
    assert notification["params"] == payload


def test_build_sidecar_health_notification_shape() -> None:
    notification = build_sidecar_health_notification()
    assert notification["jsonrpc"] == "2.0"
    assert notification["method"] == "sidecar.health"
    assert notification["params"]["status"] == "ok"
    assert isinstance(notification["params"]["timestamp"], str)


def test_build_agent_subprocess_notifications_shape() -> None:
    spawned = build_agent_subprocess_spawned_notification(
        agent_id="a1",
        agent_type="codex",
        pid=1234,
    )
    assert spawned["method"] == "agent.subprocess_spawned"
    assert spawned["params"] == {"agent_id": "a1", "agent_type": "codex", "pid": 1234}

    exited = build_agent_subprocess_exited_notification(
        agent_id="a1",
        agent_type="codex",
        pid=1234,
        exit_code=0,
    )
    assert exited["method"] == "agent.subprocess_exited"
    assert exited["params"] == {
        "agent_id": "a1",
        "agent_type": "codex",
        "pid": 1234,
        "exit_code": 0,
    }


async def _drive(payload: dict[str, object], *, state: ServerState) -> dict[str, object]:
    """Dispatch through handle_request and await coroutine results."""
    response = handle_request(payload, state=state)
    if asyncio.iscoroutine(response):
        response = await response
    assert response is not None
    return response


# ---------------------------------------------------------------------------
# Regression tests for _reader_loop sentinel-on-error guarantee (issue #235)
# ---------------------------------------------------------------------------


class _RaisingImmediateStdin:
    """Raises OSError on the very first __next__ call (e.g. closed pipe at open)."""

    def __iter__(self) -> Iterator[str]:
        return self  # type: ignore[return-value]

    def __next__(self) -> str:
        raise OSError("simulated stdin close")


class _RaisingAfterLinesStdin:
    """Yields two blank lines then raises, simulating a mid-stream pipe break."""

    def __init__(self) -> None:
        self._count = 0

    def __iter__(self) -> Iterator[str]:
        return self  # type: ignore[return-value]

    def __next__(self) -> str:
        self._count += 1
        if self._count > 2:
            raise OSError("simulated mid-stream stdin close")
        return "\n"


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_reader_loop_enqueues_sentinel_on_immediate_stdin_error() -> None:
    """_reader_loop pushes None even when stdin raises before yielding any line.

    Uses a raw Thread (not run_in_executor) to match production caller pattern.
    The OSError from the thread is intentionally unhandled — the finally block
    guarantees the sentinel is still enqueued so _serve_async can exit.
    """
    loop = asyncio.new_event_loop()
    try:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        thread = threading.Thread(
            target=_reader_loop, args=(_RaisingImmediateStdin(), loop, queue), daemon=True
        )
        thread.start()
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "reader thread did not exit within timeout"
        # The sentinel is scheduled via call_soon_threadsafe; run one iteration
        # so the pending callback fires before we check the queue.
        loop.run_until_complete(asyncio.sleep(0))
        sentinel = queue.get_nowait()
        assert sentinel is None
    finally:
        loop.close()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_reader_loop_enqueues_sentinel_on_mid_stream_stdin_error() -> None:
    """_reader_loop pushes None after enqueuing partial lines when stdin raises mid-stream."""
    loop = asyncio.new_event_loop()
    try:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        thread = threading.Thread(
            target=_reader_loop, args=(_RaisingAfterLinesStdin(), loop, queue), daemon=True
        )
        thread.start()
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "reader thread did not exit within timeout"
        loop.run_until_complete(asyncio.sleep(0))
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        assert items, "queue must have at least the sentinel"
        assert items[-1] is None, "sentinel must be the last item enqueued"
    finally:
        loop.close()


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
async def test_serve_async_terminates_when_stdin_raises_immediately() -> None:
    """_serve_async does not deadlock when stdin raises before yielding any line."""
    stdout = io.StringIO()
    await asyncio.wait_for(_serve_async(_RaisingImmediateStdin(), stdout), timeout=2.0)


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
async def test_serve_async_terminates_when_stdin_raises_mid_stream() -> None:
    """_serve_async drains the queue and exits cleanly after a mid-stream stdin error."""
    stdout = io.StringIO()
    await asyncio.wait_for(_serve_async(_RaisingAfterLinesStdin(), stdout), timeout=2.0)
