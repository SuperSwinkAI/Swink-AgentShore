"""ESR in-app rendering: drain.py skips ``webbrowser.open`` in embedded mode.

Issue #561 — desktop sessions render the End-Session Report inside the Tauri
shell. The orchestrator's drain loop used to call ``webbrowser.open`` on the
static HTML file regardless of who was hosting the session; that yanked
desktop users out of the app the moment they were most likely to start a
follow-up run. These tests pin the post-fix behavior:

* CLI / TUI sessions (``_embedded_mode=False``) keep the old behavior —
  ``webbrowser.open`` fires with the resolved file URI.
* Desktop sidecar sessions (``_embedded_mode=True``) skip
  ``webbrowser.open`` and invoke the registered ``_esr_ready_callback``
  with ``(session_id, report_path)`` instead.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentshore.core.mixins.drain import DrainController
from agentshore.core.orchestrator import Orchestrator
from agentshore.state import OrchestratorState, SessionState


def _state(state: SessionState) -> OrchestratorState:
    return OrchestratorState(
        session_id="sess-test",
        session_state=state,
        total_plays=0,
        total_cost=0.0,
    )


def _minimal_orch(tmp_path: Path) -> Orchestrator:
    """Build a stub Orchestrator instance for drain testing.

    Mirrors the construction pattern used in tests/test_orchestrator_drain.py
    so the surface stays in lockstep when one is updated.
    """
    orch = Orchestrator.__new__(Orchestrator)
    orch._session_id = "sess-test"
    orch._repo_root = tmp_path
    orch._stop_reason = "ppo_selected"
    orch._in_flight = {}
    orch._dispatch_ctx = {}
    orch._manager = MagicMock()
    orch._manager.handles = {}
    orch._health = None
    orch._integrity = None
    orch._power_assertion = None
    orch._loop = MagicMock()
    orch._end_session_report_requested = True
    orch._end_session_report_open_browser = True
    orch._state_builder = MagicMock()
    orch._state_builder.build_state = AsyncMock(return_value=_state(SessionState.DRAINING))
    orch._completion = MagicMock()
    orch._completion.refresh_issues = AsyncMock()
    orch._store = AsyncMock()
    orch._store.complete_session = AsyncMock()
    orch._store.close = AsyncMock()
    orch._state_provider = MagicMock()
    orch._state_provider.on_session_ended = AsyncMock()
    orch._drain = DrainController(
        host=orch,
        store=orch._store,
        manager=orch._manager,
        session_id=orch._session_id,
        repo_root=orch._repo_root,
        state_builder=orch._state_builder,
    )
    return orch


@pytest.mark.asyncio
async def test_non_embedded_mode_opens_browser(tmp_path: Path) -> None:
    """CLI/TUI path: ``webbrowser.open`` runs and no esr_ready callback fires."""
    report_path = tmp_path / ".agentshore" / "reports" / "end-session-sess-test.html"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.touch()

    orch = _minimal_orch(tmp_path)
    orch._embedded_mode = False
    callback_calls: list[tuple[str, str, str | None]] = []
    orch._esr_ready_callback = lambda sid, path, log_path: callback_calls.append(
        (sid, path, log_path)
    )
    orch._drain.generate_end_session_report = AsyncMock(return_value=report_path)

    with patch("webbrowser.open") as mock_open:
        await orch._drain.stop_inner(0.0)

    mock_open.assert_called_once_with(report_path.resolve().as_uri())
    # The callback is only fired in embedded mode — keeps the CLI path
    # free of unexpected side-channels.
    assert callback_calls == []


@pytest.mark.asyncio
async def test_embedded_mode_skips_browser_and_emits_callback(tmp_path: Path) -> None:
    """Desktop path: ``webbrowser.open`` is NOT called; callback receives report path."""
    report_path = tmp_path / ".agentshore" / "reports" / "end-session-sess-test.html"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.touch()

    orch = _minimal_orch(tmp_path)
    orch._embedded_mode = True
    log_path = tmp_path / ".agentshore" / "logs" / "agentshore-sess-test.log"
    orch._log_path = log_path
    callback_calls: list[tuple[str, str, str | None]] = []
    orch._esr_ready_callback = lambda sid, path, log: callback_calls.append((sid, path, log))
    orch._drain.generate_end_session_report = AsyncMock(return_value=report_path)

    with patch("webbrowser.open") as mock_open:
        await orch._drain.stop_inner(0.0)

    mock_open.assert_not_called()
    assert callback_calls == [("sess-test", str(report_path.resolve()), str(log_path.resolve()))]


@pytest.mark.asyncio
async def test_embedded_mode_without_callback_is_no_op(tmp_path: Path) -> None:
    """No esr_ready callback registered → silently skip browser; log only.

    This is the early-boot / mis-wired case — the orchestrator was put into
    embedded mode but no callback was registered. We must still avoid
    ``webbrowser.open`` (the whole point of embedded mode) and not raise.
    """
    report_path = tmp_path / ".agentshore" / "reports" / "end-session-sess-test.html"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.touch()

    orch = _minimal_orch(tmp_path)
    orch._embedded_mode = True
    orch._esr_ready_callback = None
    orch._drain.generate_end_session_report = AsyncMock(return_value=report_path)

    with patch("webbrowser.open") as mock_open:
        await orch._drain.stop_inner(0.0)

    mock_open.assert_not_called()


@pytest.mark.asyncio
async def test_embedded_callback_exception_does_not_fail_drain(tmp_path: Path) -> None:
    """A throwing callback must not propagate — drain has to complete cleanly."""
    report_path = tmp_path / ".agentshore" / "reports" / "end-session-sess-test.html"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.touch()

    orch = _minimal_orch(tmp_path)
    orch._embedded_mode = True

    def _boom(_sid: str, _path: str, _log_path: str | None) -> None:
        raise RuntimeError("notify pipe closed")

    orch._esr_ready_callback = _boom
    orch._drain.generate_end_session_report = AsyncMock(return_value=report_path)

    with patch("webbrowser.open") as mock_open:
        await orch._drain.stop_inner(0.0)

    mock_open.assert_not_called()


def test_register_esr_ready_callback_stores_handler(tmp_path: Path) -> None:
    """The register_* helper exists on the orchestrator and survives None reset."""
    orch = _minimal_orch(tmp_path)
    orch._esr_ready_callback = None

    received: list[tuple[str, str, str | None]] = []
    orch.register_esr_ready_callback(lambda sid, p, log: received.append((sid, p, log)))
    assert orch._esr_ready_callback is not None
    orch._esr_ready_callback("sid-1", "/tmp/r.html", "/tmp/session.log")
    assert received == [("sid-1", "/tmp/r.html", "/tmp/session.log")]

    orch.register_esr_ready_callback(None)
    assert orch._esr_ready_callback is None


def test_build_esr_ready_notification_shape() -> None:
    """``$/esr_ready`` JSON-RPC envelope carries the ESR file locators."""
    from agentshore.sidecar.server import build_esr_ready_notification

    note = build_esr_ready_notification(
        session_id="sess-abc",
        archive_path="/tmp/.agentshore/archives/sess-abc",
        report_path="/tmp/.agentshore/reports/end-session-sess-abc.html",
        log_path="/tmp/.agentshore/logs/agentshore-sess-abc.log",
    )
    assert note["jsonrpc"] == "2.0"
    assert note["method"] == "$/esr_ready"
    assert note["params"] == {
        "session_id": "sess-abc",
        "archive_path": "/tmp/.agentshore/archives/sess-abc",
        "report_path": "/tmp/.agentshore/reports/end-session-sess-abc.html",
        "log_path": "/tmp/.agentshore/logs/agentshore-sess-abc.log",
    }


def test_build_esr_ready_emitter_dispatches_through_notify() -> None:
    """The emitter forwards (session_id, report_path) into the JSON-RPC envelope."""
    from agentshore.sidecar.notification_emitters import build_esr_ready_emitter

    seen: list[dict[str, object]] = []
    emit = build_esr_ready_emitter(lambda envelope: seen.append(dict(envelope)))
    emit("sess-xyz", "/path/to/archive", "/path/to/report.html", "/path/to/session.log")

    assert len(seen) == 1
    note = seen[0]
    assert note["method"] == "$/esr_ready"
    assert note["params"] == {
        "session_id": "sess-xyz",
        "archive_path": "/path/to/archive",
        "report_path": "/path/to/report.html",
        "log_path": "/path/to/session.log",
    }
