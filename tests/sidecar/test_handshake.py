"""Tests for the sidecar JSON-RPC handshake (DESIGN §2.6)."""

from __future__ import annotations

import asyncio
import io
import json
import time
from collections.abc import Iterator
from typing import IO, cast

import pytest

from agentshore import __version__ as agentshore_version
from agentshore.sidecar.handshake import PROTOCOL_VERSION, build_response, capabilities
from agentshore.sidecar.server import (
    INVALID_REQUEST,
    METHOD_HANDLERS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    REQUEST_CANCELLED,
    ServerState,
    handle_request,
    serve,
)

VALID_TIERED_CONFIG = """\
project: {}
identities:
  alpha:
    git_user_name: Alpha
    git_user_email: alpha@example.com
    gh_token_login: alpha
  beta:
    git_user_name: Beta
    git_user_email: beta@example.com
    gh_token_login: beta
agents:
  claude_code:
    enabled: true
    binary: agentshore-missing-claude
    identity: alpha
    model_tiers:
      small:
        enabled: true
      medium:
        enabled: true
      large:
        enabled: true
  codex:
    enabled: true
    binary: agentshore-missing-codex
    identity: beta
    model_tiers:
      small:
        enabled: true
      medium:
        enabled: true
      large:
        enabled: true
"""


def test_build_response_carries_required_fields() -> None:
    response = build_response()
    assert response["protocol_version"] == PROTOCOL_VERSION
    assert response["agentshore_version"] == agentshore_version
    assert isinstance(response["sidecar_build_id"], str)
    assert response["sidecar_build_id"]
    assert response["capabilities"] == capabilities()


def test_capabilities_advertises_handshake() -> None:
    assert "app.handshake" in capabilities()
    assert "$/cancelRequest" in capabilities()
    assert "session.start" in capabilities()
    assert "session.status" in capabilities()
    assert "session.stop" in capabilities()


def test_capabilities_advertises_agents_methods() -> None:
    caps = capabilities()
    assert "agents.list" in caps
    assert "agents.configure" in caps
    assert "agents.check_auth" in caps


def test_capabilities_advertises_session_lifecycle() -> None:
    assert "session.start" in capabilities()
    assert "session.stop" in capabilities()


def test_capabilities_advertises_notification_methods() -> None:
    """Notification methods sidecar emits to the shell are advertised so the
    Rust supervisor can feature-detect support (DESIGN §5.1, desktop-8e1)."""
    caps = capabilities()
    assert "session.completed" in caps
    assert "sidecar.health" in caps
    assert "agent.subprocess_spawned" in caps
    assert "agent.subprocess_exited" in caps


def test_capabilities_has_no_duplicates() -> None:
    """Guard against the duplicate session.start / session.stop entries that
    used to sit at the bottom of the list."""
    caps = capabilities()
    assert len(caps) == len(set(caps)), f"duplicates: {[c for c in caps if caps.count(c) > 1]}"


def test_capabilities_advertises_project_lifecycle_methods() -> None:
    caps = capabilities()
    assert "project.select" in caps
    assert "project.inspect" in caps
    assert "project.deselect" in caps


def test_handshake_request_returns_result_envelope() -> None:
    sidecar_build_id = build_response()["sidecar_build_id"]
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "app.handshake",
            "params": {"client": "agentshore-desktop", "client_build_id": sidecar_build_id},
        }
    )
    assert response is not None
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 1
    assert "result" in response
    assert "error" not in response
    result = cast("dict[str, object]", response["result"])
    assert result["protocol_version"] == PROTOCOL_VERSION


def test_unknown_method_returns_method_not_found() -> None:
    response = handle_request({"jsonrpc": "2.0", "id": 7, "method": "no.such.method"})
    assert response is not None
    assert "error" in response
    error = response["error"]
    assert error["code"] == METHOD_NOT_FOUND
    assert "no.such.method" in error["message"]


def test_invalid_jsonrpc_version_is_rejected() -> None:
    response = handle_request(
        {
            "jsonrpc": "1.0",
            "id": 2,
            "method": "app.handshake",
            "params": {"client": "agentshore-desktop", "client_build_id": "dev"},
        }
    )
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == INVALID_REQUEST


def test_non_object_payload_is_rejected() -> None:
    response = handle_request(["not", "an", "object"])
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == INVALID_REQUEST


def test_notification_returns_none() -> None:
    # No "id" field => notification per JSON-RPC 2.0 — no response written.
    assert handle_request({"jsonrpc": "2.0", "method": "app.handshake"}) is None
    assert handle_request({"jsonrpc": "2.0", "method": "no.such.method"}) is None


def test_handshake_rejects_missing_params() -> None:
    response = handle_request({"jsonrpc": "2.0", "id": 3, "method": "app.handshake"})
    assert response is not None
    assert response["error"]["code"] == INVALID_REQUEST
    assert "params" in response["error"]["message"]


def test_handshake_rejects_build_id_mismatch() -> None:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "app.handshake",
            "params": {"client": "agentshore-desktop", "client_build_id": "wrong-build"},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_REQUEST
    assert response["error"]["message"] == "build_id mismatch"


def test_handshake_rejects_whitespace_only_client() -> None:
    sidecar_build_id = build_response()["sidecar_build_id"]
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "app.handshake",
            "params": {"client": "   ", "client_build_id": sidecar_build_id},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_REQUEST
    assert response["error"]["message"] == "params.client must be a non-empty string"


def test_handshake_rejects_whitespace_only_client_build_id() -> None:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "app.handshake",
            "params": {"client": "agentshore-desktop", "client_build_id": "   "},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_REQUEST
    assert response["error"]["message"] == "params.client_build_id must be a non-empty string"


def test_serve_round_trips_handshake_over_stdio() -> None:
    sidecar_build_id = build_response()["sidecar_build_id"]
    request = {
        "jsonrpc": "2.0",
        "id": 42,
        "method": "app.handshake",
        "params": {"client": "agentshore-desktop", "client_build_id": sidecar_build_id},
    }
    stdin = io.StringIO(json.dumps(request) + "\n")
    stdout = io.StringIO()
    serve(stdin, stdout)
    [reply_line] = [line for line in stdout.getvalue().splitlines() if line.strip()]
    reply = json.loads(reply_line)
    assert reply["id"] == 42
    assert reply["result"]["protocol_version"] == PROTOCOL_VERSION
    assert reply["result"]["agentshore_version"] == agentshore_version


def test_serve_reports_parse_error_on_invalid_json() -> None:
    stdin = io.StringIO("not json\n")
    stdout = io.StringIO()
    serve(stdin, stdout)
    [reply_line] = [line for line in stdout.getvalue().splitlines() if line.strip()]
    reply = json.loads(reply_line)
    assert reply["id"] is None
    assert reply["error"]["code"] == PARSE_ERROR


def test_serve_handles_blank_lines() -> None:
    stdin = io.StringIO("\n   \n")
    stdout = io.StringIO()
    serve(stdin, stdout)
    assert stdout.getvalue() == ""


def test_serve_handles_multiple_requests_in_order() -> None:
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
    serve(stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert [r["id"] for r in replies] == ["a", "b"]
    assert "result" in replies[0]
    assert replies[1]["error"]["code"] == METHOD_NOT_FOUND


def _resolve(result: object) -> dict[str, object]:
    """Await ``handle_request``'s coroutine return if necessary.

    ``session.start`` returns an awaitable so the runner can start an
    EmbeddedBridge in the event loop. Tests that don't otherwise care
    about async wrap the call so they keep their synchronous shape.
    """
    import inspect as _inspect

    if _inspect.isawaitable(result):
        return cast("dict[str, object]", asyncio.run(result))
    return cast("dict[str, object]", result)


def test_session_start_emits_per_phase_progress_notifications() -> None:
    """``session.start`` emits running+ok per DESIGN §10.2 phase (gh-335).

    Seven phases × two notifications each = 14 ``$/progress`` events, in this
    exact order: config_merge → check_agent_auth → install_skills → init_beads
    → bind_ipc → start_bridge → first_snapshot. The final phase reaches
    percent=100 so the desktop Screen 8 checklist can auto-advance to
    /session/dashboard.
    """
    notifications: list[dict[str, object]] = []
    response = _resolve(
        handle_request(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "session.start",
                "params": {"progress_token": "tok-1"},
            },
            notify=notifications.append,
        )
    )
    assert response["id"] == 99
    assert "result" in response

    expected_steps = [
        "config_merge",
        "check_agent_auth",
        "install_skills",
        "init_beads",
        "bind_ipc",
        "start_bridge",
        "first_snapshot",
    ]
    assert len(notifications) == 2 * len(expected_steps)

    for idx, step in enumerate(expected_steps):
        running = cast("dict[str, object]", notifications[idx * 2])
        ok = cast("dict[str, object]", notifications[idx * 2 + 1])
        assert running["method"] == "$/progress"
        assert ok["method"] == "$/progress"
        running_params = cast("dict[str, object]", running["params"])
        ok_params = cast("dict[str, object]", ok["params"])
        assert running_params["token"] == "tok-1"
        assert ok_params["token"] == "tok-1"
        assert running_params["step"] == step
        assert ok_params["step"] == step
        assert running_params["percent"] == 0
        assert ok_params["percent"] == 100
        # "running" message is the "ok" phase label plus a "…" suffix.
        assert running_params["message"] == f"{ok_params['message']}…"


def test_session_start_fails_when_config_missing_with_active_project(tmp_path: object) -> None:
    """With an active project but no ``agentshore.yaml``, session.start emits a
    ``failed`` $/progress for ``config_merge`` and returns INVALID_PARAMS
    (desktop-0vc.11.2, gh-307)."""
    from pathlib import Path

    project_path = Path(tmp_path) / "empty-project"  # type: ignore[arg-type]
    project_path.mkdir()
    notifications: list[dict[str, object]] = []
    response = _resolve(
        handle_request(
            {
                "jsonrpc": "2.0",
                "id": 70,
                "method": "session.start",
                "params": {"progress_token": "tok-fail"},
            },
            notify=notifications.append,
            state=ServerState(active_project_path=str(project_path)),
        )
    )
    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == -32602  # INVALID_PARAMS
    assert "agentshore.yaml" in cast("str", error["message"])

    # The first phase fires running then failed; later phases never emit.
    assert len(notifications) == 2
    running = cast("dict[str, object]", notifications[0])
    failed = cast("dict[str, object]", notifications[1])
    running_params = cast("dict[str, object]", running["params"])
    failed_params = cast("dict[str, object]", failed["params"])
    assert running_params["step"] == "config_merge"
    assert failed_params["step"] == "config_merge"
    assert "error" in failed_params


def test_session_start_fails_when_beads_init_fails(tmp_path: object) -> None:
    """When ``bd init`` cannot run (bd binary absent), session.start halts at
    the ``init_beads`` phase with a structured error."""
    from pathlib import Path
    from unittest.mock import AsyncMock, patch

    project_path = Path(tmp_path) / "no-beads"  # type: ignore[arg-type]
    project_path.mkdir()
    (project_path / "agentshore.yaml").write_text(VALID_TIERED_CONFIG, encoding="utf-8")
    notifications: list[dict[str, object]] = []

    # Simulate bd binary not found so the automatic init fails.
    with patch(
        "agentshore.beads.setup.bd_init_project",
        new=AsyncMock(side_effect=RuntimeError("bd binary not found")),
    ):
        response = _resolve(
            handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 71,
                    "method": "session.start",
                    "params": {"progress_token": "tok-beads"},
                },
                notify=notifications.append,
                state=ServerState(active_project_path=str(project_path)),
            )
        )
    error = cast("dict[str, object]", response["error"])
    assert error["code"] == -32602
    assert ".beads" in cast("str", error["message"])

    # First three phases emit ok; init_beads emits running then failed.
    # (check_agent_auth still emits its running/ok pair with no CLI agents.)
    steps_seen = [cast("dict[str, object]", n["params"])["step"] for n in notifications]
    assert steps_seen == [
        "config_merge",
        "config_merge",
        "check_agent_auth",
        "check_agent_auth",
        "install_skills",
        "install_skills",
        "init_beads",
        "init_beads",
    ]
    last_params = cast("dict[str, object]", notifications[-1]["params"])
    assert "error" in last_params


def test_session_start_succeeds_with_valid_project(tmp_path: object) -> None:
    """With agentshore.yaml and ``.beads/`` both present, session.start runs all
    six phases, returns a real session_id, and allocates a TCP IPC endpoint."""
    from pathlib import Path

    project_path = Path(tmp_path) / "valid-project"  # type: ignore[arg-type]
    project_path.mkdir()
    (project_path / "agentshore.yaml").write_text(VALID_TIERED_CONFIG, encoding="utf-8")
    (project_path / ".beads").mkdir()
    state = ServerState(active_project_path=str(project_path))
    # start_bridge=False skips real uvicorn binding; an async test covers it.
    from agentshore.sidecar.session_lifecycle import run_session_start

    outcome = asyncio.run(run_session_start(state, start_bridge=False))
    state.session_active = True
    state.session_id = outcome.session_id
    state.started_at = outcome.started_at
    response = {"result": {"session_id": outcome.session_id, "ipc_endpoint": outcome.ipc_endpoint}}
    result = cast("dict[str, object]", response)["result"]
    assert isinstance(result, dict)
    assert isinstance(result["session_id"], str)
    assert result["session_id"]
    ipc_endpoint = cast("dict[str, object]", result["ipc_endpoint"])
    assert ipc_endpoint["kind"] == "tcp"
    assert ipc_endpoint["host"] == "127.0.0.1"
    assert isinstance(ipc_endpoint["port"], int)
    assert cast("int", ipc_endpoint["port"]) > 0
    assert state.session_active is True


def test_session_start_runs_bd_hooks_after_init(tmp_path: object) -> None:
    """When beads init succeeds, session.start also runs bd hooks install
    for enabled agent types (CLI/desktop parity)."""
    from pathlib import Path
    from unittest.mock import AsyncMock, patch

    from agentshore.state import AgentType

    project_path = Path(tmp_path) / "hooks-project"  # type: ignore[arg-type]
    project_path.mkdir()
    (project_path / "agentshore.yaml").write_text(
        "project: {}\nagents:\n  claude_code:\n    enabled: true\n  codex:\n    enabled: true\n",
        encoding="utf-8",
    )
    (project_path / ".beads").mkdir()

    mock_setup = AsyncMock(return_value=["claude", "codex"])
    # Neutralise the check_agent_auth gate (dd7fcb3 — requires two distinct
    # identities) so the bd-hooks phase is what's under test.
    with (
        patch("agentshore.beads.setup.bd_setup_for_agent_types", mock_setup),
        patch("agentshore.agents.identity.require_two_distinct_gh_identities"),
        patch("agentshore.agents.auth_probe.probe_configured_cli_auth", return_value=[]),
    ):
        from agentshore.sidecar.session_lifecycle import run_session_start

        state = ServerState(active_project_path=str(project_path))
        outcome = asyncio.run(run_session_start(state, start_bridge=False))
        assert outcome.session_id

    mock_setup.assert_called_once()
    call_args = mock_setup.call_args
    assert call_args[0][0] == project_path
    enabled_types = call_args[0][1]
    assert AgentType.CLAUDE_CODE in enabled_types
    assert AgentType.CODEX in enabled_types


def test_session_start_succeeds_when_bd_hooks_fail(tmp_path: object) -> None:
    """bd hooks install failure does not block session.start — it is
    best-effort, matching CLI behaviour."""
    from pathlib import Path
    from unittest.mock import AsyncMock, patch

    project_path = Path(tmp_path) / "hooks-fail"  # type: ignore[arg-type]
    project_path.mkdir()
    (project_path / "agentshore.yaml").write_text(VALID_TIERED_CONFIG, encoding="utf-8")
    (project_path / ".beads").mkdir()

    mock_setup = AsyncMock(side_effect=RuntimeError("bd hooks install failed"))
    with patch(
        "agentshore.beads.setup.bd_setup_for_agent_types",
        mock_setup,
    ):
        from agentshore.sidecar.session_lifecycle import run_session_start

        state = ServerState(active_project_path=str(project_path))
        outcome = asyncio.run(run_session_start(state, start_bridge=False))
        assert outcome.session_id

    mock_setup.assert_called_once()


def test_session_start_warns_but_succeeds_on_bd_version_mismatch(tmp_path: object) -> None:
    """A resolved bd that doesn't match AgentShore's pinned version logs a
    session-start warning instead of only surfacing later as a mid-session
    play failure (#315) — and, like bd-hooks, is best-effort and never
    blocks session.start."""
    from pathlib import Path
    from unittest.mock import patch

    import structlog

    project_path = Path(tmp_path) / "version-mismatch"  # type: ignore[arg-type]
    project_path.mkdir()
    (project_path / "agentshore.yaml").write_text(VALID_TIERED_CONFIG, encoding="utf-8")
    (project_path / ".beads").mkdir()

    with (
        patch("agentshore.beads.resolve_bd_binary", return_value="/usr/local/bin/bd"),
        patch(
            "agentshore.beads.setup._check_bd_version",
            side_effect=RuntimeError("bd version '1.0.4' does not match pinned '1.1.0'"),
        ),
        structlog.testing.capture_logs() as captured,
    ):
        from agentshore.sidecar.session_lifecycle import run_session_start

        state = ServerState(active_project_path=str(project_path))
        outcome = asyncio.run(run_session_start(state, start_bridge=False))
        assert outcome.session_id

    matching = [e for e in captured if e.get("event") == "bd_version_mismatch_at_session_start"]
    assert len(matching) == 1, captured
    assert "1.0.4" in matching[0]["error"]


def test_session_start_skips_version_check_when_bd_unresolvable(tmp_path: object) -> None:
    """No bd resolvable at all: the version check is skipped without error —
    the pre-existing "bd binary not found" failure mode (surfaced elsewhere,
    e.g. an actual bd() call) still applies; this check only guards a
    resolved-but-wrong-version binary."""
    from pathlib import Path
    from unittest.mock import patch

    import structlog

    project_path = Path(tmp_path) / "no-bd-resolvable"  # type: ignore[arg-type]
    project_path.mkdir()
    (project_path / "agentshore.yaml").write_text(VALID_TIERED_CONFIG, encoding="utf-8")
    (project_path / ".beads").mkdir()

    with (
        patch("agentshore.beads.resolve_bd_binary", return_value=None),
        structlog.testing.capture_logs() as captured,
    ):
        from agentshore.sidecar.session_lifecycle import run_session_start

        state = ServerState(active_project_path=str(project_path))
        outcome = asyncio.run(run_session_start(state, start_bridge=False))
        assert outcome.session_id

    assert [e for e in captured if e.get("event") == "bd_version_mismatch_at_session_start"] == []


def test_session_start_recovers_stale_remote_clone_via_bootstrap(tmp_path: object) -> None:
    """A clone stuck behind a remote-backed store's schema (#316) is caught up
    with `bd bootstrap` at session start, before anything else touches the
    store — the resulting graph probe succeeds and session.start still
    reports success (this is a session-start convenience, not a gate)."""
    from pathlib import Path
    from unittest.mock import patch

    import structlog

    from agentshore.beads import BdError

    project_path = Path(tmp_path) / "stale-remote-clone"  # type: ignore[arg-type]
    project_path.mkdir()
    (project_path / "agentshore.yaml").write_text(VALID_TIERED_CONFIG, encoding="utf-8")
    (project_path / ".beads").mkdir()

    stale_error = (
        "bd list --all --json --limit 0 failed (rc=1): Warning: refusing to auto-apply 21 "
        "pending schema migrations to a remote-backed database (v32 -> v53): migrating clones "
        "independently forks the schema (#4259)\nError: search count issues: Error 1105: "
        'column "depends_on_issue_id" could not be found in any table in scope'
    )
    calls: list[tuple[str, ...]] = []

    async def _fake_bd(*args: str, cwd: object, stdin_data: object = None) -> str:
        if args and args[0] == "list":
            calls.append(args)
            if len(calls) == 1:
                raise BdError(stale_error)
            return "[]"
        if args and args[0] == "bootstrap":
            calls.append(args)
            return ""
        return ""  # benign no-op for unrelated init/hooks/config calls

    with (
        patch("agentshore.beads.setup.bd", side_effect=_fake_bd),
        structlog.testing.capture_logs() as captured,
    ):
        from agentshore.sidecar.session_lifecycle import run_session_start

        state = ServerState(active_project_path=str(project_path))
        outcome = asyncio.run(run_session_start(state, start_bridge=False))
        assert outcome.session_id

    events = [e.get("event") for e in captured]
    assert "beads_stale_remote_clone_detected" in events
    assert "beads_bootstrap_recovery_ran" in events
    assert calls == [
        ("list", "--all", "--json", "--limit", "0"),
        ("bootstrap",),
        ("list", "--all", "--json", "--limit", "0"),
    ]


def test_session_start_succeeds_when_stale_clone_recovery_raises(tmp_path: object) -> None:
    """An unexpected error inside the stale-remote-clone recovery is logged and
    swallowed — it is best-effort, matching bd-hooks and the version check,
    and must never block session.start."""
    from pathlib import Path
    from unittest.mock import AsyncMock, patch

    import structlog

    project_path = Path(tmp_path) / "stale-clone-recovery-raises"  # type: ignore[arg-type]
    project_path.mkdir()
    (project_path / "agentshore.yaml").write_text(VALID_TIERED_CONFIG, encoding="utf-8")
    (project_path / ".beads").mkdir()

    with (
        patch(
            "agentshore.beads.setup.reconcile_stale_remote_clone",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
        structlog.testing.capture_logs() as captured,
    ):
        from agentshore.sidecar.session_lifecycle import run_session_start

        state = ServerState(active_project_path=str(project_path))
        outcome = asyncio.run(run_session_start(state, start_bridge=False))
        assert outcome.session_id

    matching = [
        e for e in captured if e.get("event") == "beads_stale_remote_clone_recovery_skipped"
    ]
    assert len(matching) == 1, captured
    assert "boom" in matching[0]["error"]


def test_session_stop_rejects_unknown_mode() -> None:
    """``session.stop`` returns ``INVALID_PARAMS`` for any mode other than
    "drain" or "hard" (DESIGN §5.1, desktop-pgs)."""
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 60,
            "method": "session.stop",
            "params": {"mode": "graceful"},
        },
        state=ServerState(session_active=True),
    )
    assert response is not None
    error = cast("dict[str, object]", response)["error"]
    assert isinstance(error, dict)
    assert error["code"] == -32602  # INVALID_PARAMS
    assert "mode" in cast("str", error["message"])


def test_session_stop_drain_mode_emits_per_phase_progress() -> None:
    """Active-session drain stop fires running+ok for each drain phase
    (cancel_pending, await_inflight, archive_session, generate_report)."""
    notifications: list[dict[str, object]] = []
    handle_request(
        {
            "jsonrpc": "2.0",
            "id": 61,
            "method": "session.stop",
            "params": {"mode": "drain", "progress_token": "drain-tok"},
        },
        notify=notifications.append,
        state=ServerState(session_active=True),
    )
    expected_steps = [
        "cancel_pending",
        "await_inflight",
        "archive_session",
        "generate_report",
    ]
    assert len(notifications) == 2 * len(expected_steps)
    for idx, step in enumerate(expected_steps):
        running = cast("dict[str, object]", notifications[idx * 2])
        ok = cast("dict[str, object]", notifications[idx * 2 + 1])
        running_params = cast("dict[str, object]", running["params"])
        ok_params = cast("dict[str, object]", ok["params"])
        assert running_params["token"] == "drain-tok"
        assert ok_params["token"] == "drain-tok"
        assert running_params["step"] == step
        assert ok_params["step"] == step
        assert running_params["percent"] == 0
        assert ok_params["percent"] == 100


def test_session_stop_hard_mode_skips_drain_phases() -> None:
    """Hard mode emits a single ``lifecycle`` event instead of the four
    drain phases — there is no graceful wait to narrate."""
    notifications: list[dict[str, object]] = []
    handle_request(
        {
            "jsonrpc": "2.0",
            "id": 62,
            "method": "session.stop",
            "params": {"mode": "hard", "progress_token": "hard-tok"},
        },
        notify=notifications.append,
        state=ServerState(session_active=True),
    )
    assert len(notifications) == 1
    only = cast("dict[str, object]", notifications[0])
    params = cast("dict[str, object]", only["params"])
    assert params["step"] == "lifecycle"
    assert params["percent"] == 100


def test_session_stop_defaults_mode_to_drain() -> None:
    """Omitting ``mode`` is equivalent to ``mode=drain`` — both should emit
    the same drain-phase notification stream."""
    notifications_default: list[dict[str, object]] = []
    handle_request(
        {
            "jsonrpc": "2.0",
            "id": 63,
            "method": "session.stop",
            "params": {"progress_token": "default-tok"},
        },
        notify=notifications_default.append,
        state=ServerState(session_active=True),
    )
    notifications_explicit: list[dict[str, object]] = []
    handle_request(
        {
            "jsonrpc": "2.0",
            "id": 64,
            "method": "session.stop",
            "params": {"mode": "drain", "progress_token": "default-tok"},
        },
        notify=notifications_explicit.append,
        state=ServerState(session_active=True),
    )
    assert len(notifications_default) == len(notifications_explicit) == 8


def test_serve_writes_progress_before_lifecycle_result() -> None:
    """``session.stop`` against an inactive session emits progress + ERR_SESSION_ACTIVE."""
    request = {
        "jsonrpc": "2.0",
        "id": "stop-1",
        "method": "session.stop",
        "params": {"progress_token": "tok-2"},
    }
    stdin = io.StringIO(json.dumps(request) + "\n")
    stdout = io.StringIO()
    serve(stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert replies[0]["method"] == "$/progress"
    assert replies[0]["params"]["token"] == "tok-2"
    assert replies[1]["id"] == "stop-1"
    # No active session → ERR_SESSION_ACTIVE per DESIGN §5.2 ESR contract.
    assert replies[1]["error"]["code"] == -32010


@pytest.mark.parametrize(
    ("method", "params", "state"),
    [
        ("session.start", {"reason": "user"}, None),
        ("session.stop", {}, ServerState(session_active=True)),
    ],
)
def test_session_lifecycle_without_progress_token_emits_no_notification(
    method: str, params: dict[str, object], state: ServerState | None
) -> None:
    notifications: list[dict[str, object]] = []
    response = _resolve(
        handle_request(
            {"jsonrpc": "2.0", "id": 50, "method": method, "params": params},
            notify=notifications.append,
            state=state,
        )
    )
    assert response["id"] == 50
    assert "result" in response
    assert notifications == []


@pytest.mark.parametrize("method", ["session.start", "session.stop"])
def test_session_lifecycle_rejects_non_dict_params(method: str) -> None:
    notifications: list[dict[str, object]] = []
    response = handle_request(
        {"jsonrpc": "2.0", "id": 60, "method": method, "params": 42},
        notify=notifications.append,
    )
    assert response is not None
    assert "error" in response
    error = cast("dict[str, object]", response["error"])
    assert error["code"] == INVALID_REQUEST
    assert error["message"] == "params must be an object"
    assert notifications == []


class _ThrottledStdin:
    """File-like stdin that sleeps before yielding each line after the first.

    The sidecar's reader thread iterates ``for line in stdin`` and forwards
    each line to the asyncio queue. Sleeping inside ``__next__`` deterministically
    leaves the queue empty long enough for the scheduled request task to run
    to completion before the next line is delivered.
    """

    def __init__(self, lines: list[str], *, gap: float = 0.05) -> None:
        self._lines = lines
        self._idx = 0
        self._gap = gap

    def __iter__(self) -> Iterator[str]:
        return self

    def __next__(self) -> str:
        if self._idx >= len(self._lines):
            raise StopIteration
        if self._idx > 0:
            time.sleep(self._gap)
        line = self._lines[self._idx]
        self._idx += 1
        return line


def _serve_payloads(payloads: list[dict[str, object]], *, gap: float = 0.05) -> list[dict]:
    lines = [json.dumps(payload) + "\n" for payload in payloads]
    stdin = cast("IO[str]", _ThrottledStdin(lines, gap=gap))
    stdout = io.StringIO()
    serve(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def test_cancel_request_cancels_inflight_call(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_handler(_payload: dict[str, object]) -> dict[str, object]:
        await asyncio.sleep(0.2)
        return {"ok": True}

    monkeypatch.setitem(METHOD_HANDLERS, "test.slow", slow_handler)
    payloads = [
        {"jsonrpc": "2.0", "id": "req-1", "method": "test.slow"},
        {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": "req-1"}},
    ]
    [reply] = _serve_payloads(payloads)
    assert reply["id"] == "req-1"
    assert reply["error"]["code"] == REQUEST_CANCELLED


def test_cancel_request_of_completed_task_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling a request that already finished writes nothing extra to stdout."""

    async def fast_handler(_payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    monkeypatch.setitem(METHOD_HANDLERS, "test.fast", fast_handler)
    payloads = [
        {"jsonrpc": "2.0", "id": "done-1", "method": "test.fast"},
        {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": "done-1"}},
    ]
    lines = [json.dumps(p) + "\n" for p in payloads]
    stdin = cast("IO[str]", _ThrottledStdin(lines))
    stdout = io.StringIO()
    serve(stdin, stdout)

    replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(replies) == 1
    assert replies[0]["id"] == "done-1"
    assert replies[0]["result"] == {"ok": True}
    assert "error" not in replies[0]


def test_cancel_request_for_unknown_id_is_noop() -> None:
    """A cancel for an id that was never in flight produces no stdout output."""
    payload = {
        "jsonrpc": "2.0",
        "method": "$/cancelRequest",
        "params": {"id": "never-existed"},
    }
    stdin = io.StringIO(json.dumps(payload) + "\n")
    stdout = io.StringIO()
    serve(stdin, stdout)
    assert stdout.getvalue() == ""


def test_cancel_targets_only_the_specified_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """With two concurrent in-flight requests, cancelling one leaves the other intact."""

    async def slow_handler(_payload: dict[str, object]) -> dict[str, object]:
        await asyncio.sleep(0.1)
        return {"ok": True}

    monkeypatch.setitem(METHOD_HANDLERS, "test.slow", slow_handler)
    payloads = [
        {"jsonrpc": "2.0", "id": "keep", "method": "test.slow"},
        {"jsonrpc": "2.0", "id": "drop", "method": "test.slow"},
        {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": "drop"}},
    ]
    replies = _serve_payloads(payloads)
    by_id = {r["id"]: r for r in replies}
    assert set(by_id) == {"keep", "drop"}
    assert by_id["drop"]["error"]["code"] == REQUEST_CANCELLED
    assert by_id["keep"]["result"] == {"ok": True}
    assert "error" not in by_id["keep"]


def test_cancel_request_shaped_with_outer_id_writes_no_response_for_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `$/cancelRequest` carrying its own outer ``id`` still targets ``params.id``.

    The server treats the message purely as a cancellation signal — it cancels
    the request named in ``params.id`` and writes no reply addressed to the
    cancel message's own id.
    """

    async def slow_handler(_payload: dict[str, object]) -> dict[str, object]:
        await asyncio.sleep(0.2)
        return {"ok": True}

    monkeypatch.setitem(METHOD_HANDLERS, "test.slow", slow_handler)
    payloads = [
        {"jsonrpc": "2.0", "id": "req-1", "method": "test.slow"},
        {
            "jsonrpc": "2.0",
            "id": "cancel-1",
            "method": "$/cancelRequest",
            "params": {"id": "req-1"},
        },
    ]
    replies = _serve_payloads(payloads)
    assert len(replies) == 1
    assert replies[0]["id"] == "req-1"
    assert replies[0]["error"]["code"] == REQUEST_CANCELLED
    assert all(r.get("id") != "cancel-1" for r in replies)
