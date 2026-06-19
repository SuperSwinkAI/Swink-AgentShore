"""Tests for the upgraded session.start phases (gh-385).

Covers the bits of issue 385 that don't require booting a real
Orchestrator end-to-end:

* ``config_merge`` actually parses ``agentshore.yaml`` via ``load_config``.
* ``install_skills`` actually drops bundled templates into ``.agents/skills/``.
* ``run_session_start`` records ``session_id``/``started_at`` on state.
* ``session.stop`` drain mode awaits a registered orchestrator task and
  invokes ``request_drain`` + ``stop`` on the orchestrator.
* ``session.stop`` hard mode cancels the supervised task and invokes
  ``stop`` on the orchestrator.

End-to-end "drive a real Orchestrator to natural completion and assert
session.completed lands on stdio" is left for a follow-on once the
agentless minimal-project bootstrap path is ergonomic enough to drive
in a test (the current bootstrap loads PPO weights and skill templates,
which makes a fully isolated test setup heavy).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentshore.agents.auth_probe import (
    AUTH_EXPIRED,
    AUTH_OK,
    AUTH_TIMEOUT,
    AuthProbeResult,
)
from agentshore.sidecar.server import ServerState, SessionContext, handle_request
from agentshore.sidecar.session_lifecycle import (
    STEP_CHECK_AGENT_AUTH,
    STEP_CONFIG_MERGE,
    STEP_INSTALL_SKILLS,
    SessionStartError,
    _make_bridge,
    run_session_start,
)
from agentshore.state import AgentType

VALID_TIERED_CONFIG = """
project: {}
identities:
  alpha:
    git_user_name: Alpha Agent
    git_user_email: alpha@example.com
    gh_token_login: alpha-agent
  beta:
    git_user_name: Beta Agent
    git_user_email: beta@example.com
    gh_token_login: beta-agent
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
  codex:
    enabled: true
    binary: agentshore-missing-codex
    identity: beta
    model_tiers:
      large:
        enabled: true
"""


def _write_valid_project(root: Path, *, agentshore_yaml: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    yaml_body = agentshore_yaml if agentshore_yaml is not None else "project: {}\n"
    (root / "agentshore.yaml").write_text(yaml_body, encoding="utf-8")
    (root / ".beads").mkdir(exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# Phase: config_merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_merge_missing_yaml_raises_structured_error(tmp_path: Path) -> None:
    project = tmp_path / "no-yaml"
    project.mkdir()
    state = ServerState(active_project_path=str(project))
    with pytest.raises(SessionStartError) as excinfo:
        await run_session_start(state, start_bridge=False, start_orchestrator=False)
    assert excinfo.value.step == STEP_CONFIG_MERGE
    assert excinfo.value.code == -32602


@pytest.mark.asyncio
async def test_config_merge_blocks_missing_model_tier_coverage(tmp_path: Path) -> None:
    project = _write_valid_project(
        tmp_path / "missing-tier-coverage",
        agentshore_yaml="""
project: {}
agents:
  claude_code:
    enabled: true
    model_tiers:
      small:
        enabled: true
      medium:
        enabled: true
""",
    )
    state = ServerState(active_project_path=str(project))
    notifications: list[dict[str, object]] = []

    with (
        patch("agentshore.agents.auth_probe.probe_configured_cli_auth") as auth_probe,
        pytest.raises(SessionStartError) as excinfo,
    ):
        await run_session_start(
            state,
            progress_token="tok-tier-coverage",
            notify=notifications.append,
            start_bridge=False,
            start_orchestrator=True,
        )

    assert excinfo.value.step == STEP_CONFIG_MERGE
    assert excinfo.value.code == -32602
    assert "missing required model tier coverage: large" in str(excinfo.value)
    auth_probe.assert_not_called()
    steps = [n["params"]["step"] for n in notifications]  # type: ignore[index]
    assert steps == [STEP_CONFIG_MERGE, STEP_CONFIG_MERGE]


@pytest.mark.asyncio
async def test_install_skills_drops_templates_into_project(tmp_path: Path) -> None:
    """The install_skills phase copies bundled templates into
    ``.agents/skills/``."""
    project = _write_valid_project(tmp_path / "valid")
    state = ServerState(active_project_path=str(project))

    outcome = await run_session_start(state, start_bridge=False, start_orchestrator=False)
    assert outcome.session_id == state.session_id

    skills_dir = project / ".agents" / "skills"
    assert skills_dir.is_dir(), "install_skills did not create .agents/skills/"
    # Bundled templates include at least one skill (the issue_pickup play
    # uses ``agentshore-issue-pickup``); presence checks the install ran.
    installed = sorted(d.name for d in skills_dir.iterdir() if d.is_dir())
    assert installed, f"no skill templates installed under {skills_dir}"


# ---------------------------------------------------------------------------
# Phase: check_agent_auth (backend CLI-agent auth gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_agent_auth_blocks_on_expired_backend(tmp_path: Path) -> None:
    """An expired CLI-agent backend session short-circuits session.start at the
    check_agent_auth phase with a structured error naming the failing agent."""
    project = _write_valid_project(tmp_path / "expired-auth")
    state = ServerState(active_project_path=str(project))

    expired = [AuthProbeResult(AgentType.CODEX, AUTH_EXPIRED, "run `codex login`")]
    with (
        patch(
            "agentshore.agents.auth_probe.probe_configured_cli_auth",
            return_value=expired,
        ),
        pytest.raises(SessionStartError) as excinfo,
    ):
        await run_session_start(state, start_bridge=False, start_orchestrator=False)

    assert excinfo.value.step == STEP_CHECK_AGENT_AUTH
    assert excinfo.value.code == -32603
    # The message names the failing agent type + remediation.
    assert "codex" in str(excinfo.value)


@pytest.mark.asyncio
async def test_check_agent_auth_emits_failed_progress_for_expired(tmp_path: Path) -> None:
    """The blocking probe fires running then failed for check_agent_auth and
    halts before install_skills runs."""
    project = _write_valid_project(tmp_path / "expired-progress")
    state = ServerState(active_project_path=str(project))
    notifications: list[dict[str, object]] = []

    expired = [AuthProbeResult(AgentType.CODEX, AUTH_EXPIRED, "session expired")]
    with (
        patch(
            "agentshore.agents.auth_probe.probe_configured_cli_auth",
            return_value=expired,
        ),
        pytest.raises(SessionStartError),
    ):
        await run_session_start(
            state,
            progress_token="tok-auth",
            notify=notifications.append,
            start_bridge=False,
            start_orchestrator=False,
        )

    steps = [n["params"]["step"] for n in notifications]  # type: ignore[index]
    statuses = [n["params"].get("message") for n in notifications]  # type: ignore[union-attr]
    # config_merge ok, then check_agent_auth running + failed; nothing after.
    assert steps == [
        STEP_CONFIG_MERGE,
        STEP_CONFIG_MERGE,
        STEP_CHECK_AGENT_AUTH,
        STEP_CHECK_AGENT_AUTH,
    ]
    assert "install_skills" not in steps
    last = notifications[-1]["params"]  # type: ignore[index]
    assert "error" in last  # type: ignore[operator]
    assert statuses  # silence unused


@pytest.mark.asyncio
async def test_check_agent_auth_passes_on_clean_probe(tmp_path: Path) -> None:
    """A clean probe (all ok) clears the phase and session.start continues."""
    project = _write_valid_project(tmp_path / "clean-auth")
    state = ServerState(active_project_path=str(project))
    notifications: list[dict[str, object]] = []

    clean = [AuthProbeResult(AgentType.CODEX, AUTH_OK, "authenticated")]
    with patch(
        "agentshore.agents.auth_probe.probe_configured_cli_auth",
        return_value=clean,
    ):
        outcome = await run_session_start(
            state,
            progress_token="tok-clean",
            notify=notifications.append,
            start_bridge=False,
            start_orchestrator=False,
        )

    assert outcome.session_id == state.session_id
    steps = [n["params"]["step"] for n in notifications]  # type: ignore[index]
    # check_agent_auth ran (running+ok) and later phases proceeded.
    assert STEP_CHECK_AGENT_AUTH in steps
    assert "install_skills" in steps
    assert "first_snapshot" in steps


@pytest.mark.asyncio
async def test_check_agent_auth_tolerates_non_blocking_status(tmp_path: Path) -> None:
    """A non-blocking non-ok status (timeout) is logged but does not block the
    launch — session.start completes."""
    project = _write_valid_project(tmp_path / "timeout-auth")
    state = ServerState(active_project_path=str(project))

    timed_out = [AuthProbeResult(AgentType.CODEX, AUTH_TIMEOUT, "probe timed out")]
    with patch(
        "agentshore.agents.auth_probe.probe_configured_cli_auth",
        return_value=timed_out,
    ):
        outcome = await run_session_start(state, start_bridge=False, start_orchestrator=False)

    assert outcome.session_id == state.session_id


@pytest.mark.asyncio
async def test_check_agent_auth_blocks_when_enabled_agents_share_one_github_login(
    tmp_path: Path,
) -> None:
    """Desktop session.start must enforce the same identity-diversity invariant
    as the CLI start path.

    A disabled second identity does not help: code review / merge need two
    distinct GitHub logins among enabled agents.
    """
    project = _write_valid_project(
        tmp_path / "same-identity",
        agentshore_yaml="""
project: {}
identities:
  jwesleye:
    git_user_name: Jwesleye
    git_user_email: jwesleye@users.noreply.github.com
    gh_token_login: jwesleye
  unseriousai:
    git_user_name: unseriousAI
    git_user_email: unseriousai@users.noreply.github.com
    gh_token_login: unseriousai
agents:
  claude_code:
    enabled: false
    identity: jwesleye
  codex:
    enabled: true
    identity: unseriousai
  grok:
    enabled: true
    identity: unseriousai
""",
    )
    state = ServerState(active_project_path=str(project))
    notifications: list[dict[str, object]] = []

    with pytest.raises(SessionStartError) as excinfo:
        await run_session_start(
            state,
            progress_token="tok-identity",
            notify=notifications.append,
            start_bridge=False,
            start_orchestrator=False,
        )

    assert excinfo.value.step == STEP_CHECK_AGENT_AUTH
    assert excinfo.value.code == -32603
    assert "≥2 distinct GitHub identities" in str(excinfo.value)
    assert "unseriousai" in str(excinfo.value)
    steps = [n["params"]["step"] for n in notifications]  # type: ignore[index]
    assert steps == [
        STEP_CONFIG_MERGE,
        STEP_CONFIG_MERGE,
        STEP_CHECK_AGENT_AUTH,
        STEP_CHECK_AGENT_AUTH,
    ]
    assert STEP_INSTALL_SKILLS not in steps


# ---------------------------------------------------------------------------
# session.stop with a registered orchestrator (drain / hard semantics)
# ---------------------------------------------------------------------------


class _FakeOrch:
    """Minimal stand-in for agentshore.core.Orchestrator used to verify the
    session.stop wiring without paying the PPO bootstrap cost."""

    def __init__(self) -> None:
        self.drained: list[str] = []
        self.stopped = False
        self._loop_event = asyncio.Event()

    def request_drain(self, reason: str) -> None:
        self.drained.append(reason)
        # In real orch, request_drain wakes the loop and the loop exits
        # after the drain completes. The fake immediately releases the
        # supervised task to mirror that exit semantics.
        self._loop_event.set()

    async def stop(self) -> None:
        self.stopped = True

    async def run_until_completion(self) -> None:
        await self._loop_event.wait()


async def _drive(payload: dict[str, object], state: ServerState) -> dict[str, object]:
    response = handle_request(payload, state=state)
    if asyncio.iscoroutine(response) or hasattr(response, "__await__"):
        response = await response  # type: ignore[misc]
    assert response is not None
    return response  # type: ignore[return-value]


async def _populated_state_with_orch(
    tmp_path: Path,
) -> tuple[ServerState, _FakeOrch, object]:
    """Construct a ServerState that looks like one returned by a real
    session.start (with orchestrator + supervised task + ESR context),
    plus the fake orchestrator and the mock data store the ESR builder
    reads from."""
    from agentshore.data.store import DataStore

    db_path = tmp_path / "db.sqlite"
    store = DataStore(db_path)
    await store.initialize()

    fake = _FakeOrch()
    state = ServerState(
        session_active=True,
        session_id="sess-fake",
        session_context=SessionContext(
            session_id="sess-fake",
            store=store,
            archive_path=str(tmp_path / "archive"),
            report_path=str(tmp_path / "archive" / "report.html"),
            log_path=str(tmp_path / "logs" / "agentshore-sess-fake.log"),
        ),
        orchestrator=fake,
        orchestrator_task=asyncio.create_task(fake.run_until_completion()),
    )

    # Insert a minimal session row so the ESR collector has something to
    # summarise. The DataStore initialisation already created the schema.
    from agentshore.data.models import SessionRecord

    await store.create_session(
        SessionRecord(
            session_id="sess-fake",
            project_path=str(tmp_path),
            started_at="2026-05-17T00:00:00Z",
            status="running",
            seed_path="",
        )
    )
    await store.complete_session("sess-fake", 0.5)
    return state, fake, store  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_session_stop_drain_drains_and_stops_orchestrator(tmp_path: Path) -> None:
    state, fake, store = await _populated_state_with_orch(tmp_path)
    try:
        resp = await _drive(
            {"jsonrpc": "2.0", "id": 1, "method": "session.stop", "params": {"mode": "drain"}},
            state=state,
        )
    finally:
        await store.close()

    assert "result" in resp, resp
    assert fake.drained == ["session_stop_drain"]
    assert fake.stopped is True
    assert state.orchestrator is None
    assert state.orchestrator_task is None


@pytest.mark.asyncio
async def test_session_stop_hard_cancels_task_and_stops_orchestrator(
    tmp_path: Path,
) -> None:
    state, fake, store = await _populated_state_with_orch(tmp_path)
    try:
        resp = await _drive(
            {"jsonrpc": "2.0", "id": 1, "method": "session.stop", "params": {"mode": "hard"}},
            state=state,
        )
    finally:
        await store.close()

    assert "result" in resp, resp
    # Hard mode does NOT call request_drain — it cancels the task and
    # invokes orchestrator.stop() directly.
    assert fake.drained == []
    assert fake.stopped is True
    assert state.orchestrator is None
    assert state.orchestrator_task is None


# ---------------------------------------------------------------------------
# session.stop drain timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_stop_drain_respects_timeout(tmp_path: Path) -> None:
    """Drain mode falls back to hard-cancel when drain_timeout_seconds elapses."""

    class _HangOrch:
        """Fake orchestrator whose run_until_completion never returns."""

        def __init__(self) -> None:
            self.drained: list[str] = []
            self.stopped = False
            self._hang = asyncio.Event()

        def request_drain(self, reason: str) -> None:
            self.drained.append(reason)
            # Intentionally do NOT set self._hang — task hangs until cancelled.

        async def stop(self) -> None:
            self.stopped = True

        async def run_until_completion(self) -> None:
            await self._hang.wait()

    state, _, store = await _populated_state_with_orch(tmp_path)
    # Replace the real fake orchestrator with the hanging one
    hang = _HangOrch()
    hang_task = asyncio.create_task(hang.run_until_completion())
    state.orchestrator = hang
    state.orchestrator_task = hang_task

    try:
        # Use a very short timeout so the test doesn't actually wait 300s.
        resp = await _drive(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.stop",
                "params": {"mode": "drain", "drain_timeout_seconds": 0.05},
            },
            state=state,
        )
    finally:
        await store.close()

    assert "result" in resp, resp
    assert hang.drained == ["session_stop_drain"]
    assert hang.stopped is True
    assert state.orchestrator is None
    assert state.orchestrator_task is None


# ---------------------------------------------------------------------------
# End-to-end: natural orchestrator exit emits session.completed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_natural_exit_emits_session_completed_notification(tmp_path: Path) -> None:
    """session.start boots the orchestrator; natural exit emits session.completed.

    Drives the full _start_orchestrator → _supervise pipeline (gh-385 AC):
    boots via run_session_start with start_orchestrator=True, waits for the
    orchestrator to exit naturally, then asserts session.completed was emitted
    with the same payload shape session.stop returns.
    """
    from agentshore.data.models import SessionRecord
    from agentshore.data.store import DataStore

    project = _write_valid_project(tmp_path / "project", agentshore_yaml=VALID_TIERED_CONFIG)

    session_id = "sess-natural-exit-e2e"
    db_path = tmp_path / "db.sqlite"
    store = DataStore(db_path)
    await store.initialize()
    await store.create_session(
        SessionRecord(
            session_id=session_id,
            project_path=str(project),
            started_at="2026-05-17T00:00:00Z",
            status="running",
            seed_path="",
        )
    )
    await store.complete_session(session_id, 0.0)
    generated_report_path = str(
        project / ".agentshore" / "reports" / f"end-session-{session_id}.html"
    )
    generated_log_path = str(project / ".agentshore" / "logs" / f"agentshore-{session_id}.log")

    class _NaturalOrch:
        """Fake orchestrator that exits naturally after registering the hook."""

        _natural_exit_reason: str | None = None
        _natural_exit_callback: object = None
        _esr_ready_callback: object = None
        _store = store
        _log_path = Path(generated_log_path)

        def on_natural_exit(self, cb: object) -> None:
            self._natural_exit_callback = cb

        def register_esr_ready_callback(self, cb: object) -> None:
            # Issue #561: the lifecycle wires an esr_ready emitter so drain.py
            # can skip ``webbrowser.open`` and instead notify the desktop
            # shell. Accept-and-store keeps this fake passive while still
            # exercising the contract the lifecycle expects.
            self._esr_ready_callback = cb

        async def publish_initial_state(self) -> None:
            pass

        async def run_until_idle(self) -> None:
            self._natural_exit_reason = "max_plays"
            if callable(self._natural_exit_callback):
                await self._natural_exit_callback(self._natural_exit_reason)  # type: ignore[misc]

        async def stop(self) -> None:
            if callable(self._esr_ready_callback):
                self._esr_ready_callback(  # type: ignore[misc]
                    session_id,
                    generated_report_path,
                    generated_log_path,
                )

    fake_orch = _NaturalOrch()
    emitted: list[dict[str, object]] = []

    def notify(notif: dict[str, object]) -> None:
        emitted.append(notif)

    state = ServerState(active_project_path=str(project), session_id=session_id)

    with (
        patch("agentshore.core.Orchestrator") as mock_orch_cls,
        patch("agentshore.ipc.state_writer.StateWriter"),
        patch("agentshore.ipc.provider.IpcStateProvider"),
    ):
        mock_orch_cls.bootstrap = AsyncMock(return_value=fake_orch)

        await run_session_start(
            state,
            start_bridge=False,
            start_orchestrator=True,
            notify=notify,
            progress_token="tok",
        )

        assert state.orchestrator_task is not None
        await asyncio.wait_for(state.orchestrator_task, timeout=5.0)

    await store.close()

    completed = [n for n in emitted if n.get("method") == "session.completed"]
    assert len(completed) == 1, f"expected 1 session.completed, got {emitted}"
    params = completed[0]["params"]
    assert isinstance(params, dict)
    assert params["session_id"] == session_id
    assert params["exit_reason"] == "max_plays"
    assert params["exit_code"] == 0
    assert "esr_summary" in params
    assert "archive_path" in params
    assert params["report_path"] == generated_report_path
    assert params["log_path"] == generated_log_path

    ready = [n for n in emitted if n.get("method") == "$/esr_ready"]
    assert len(ready) == 1, f"expected 1 $/esr_ready, got {emitted}"
    ready_params = ready[0]["params"]
    assert isinstance(ready_params, dict)
    assert ready_params["archive_path"] == str(project / ".agentshore" / "archives" / session_id)
    assert ready_params["report_path"] == generated_report_path
    assert ready_params["log_path"] == generated_log_path


class _NoopOrch:
    """Minimal orchestrator stub for the seed_path forwarding tests (#5)."""

    _natural_exit_callback: object = None
    _natural_exit_reason: str | None = None
    _esr_ready_callback: object = None
    _store = None
    _log_path = None

    def on_natural_exit(self, cb: object) -> None:
        self._natural_exit_callback = cb

    def register_esr_ready_callback(self, cb: object) -> None:
        self._esr_ready_callback = cb

    async def publish_initial_state(self) -> None:
        pass

    async def run_until_idle(self) -> None:
        return None

    async def stop(self) -> None:
        pass


@pytest.mark.asyncio
async def test_seed_path_forwarded_to_bootstrap(tmp_path: Path) -> None:
    """A seed_path on session.start reaches Orchestrator.bootstrap (#5).

    Without this the sidecar dropped the wizard's seed selection and the
    orchestrator always took the open-start path (no_seed_input)."""
    project = _write_valid_project(tmp_path / "project", agentshore_yaml=VALID_TIERED_CONFIG)
    state = ServerState(active_project_path=str(project))
    with (
        patch("agentshore.core.Orchestrator") as mock_orch_cls,
        patch("agentshore.ipc.state_writer.StateWriter"),
        patch("agentshore.ipc.provider.IpcStateProvider"),
    ):
        mock_orch_cls.bootstrap = AsyncMock(return_value=_NoopOrch())
        await run_session_start(
            state,
            start_bridge=False,
            start_orchestrator=True,
            seed_path="docs/spec.md",
        )
        if state.orchestrator_task is not None:
            await asyncio.wait_for(state.orchestrator_task, timeout=5.0)
    assert mock_orch_cls.bootstrap.await_args.kwargs["seed_path"] == Path("docs/spec.md")


@pytest.mark.asyncio
async def test_no_seed_path_passes_none_to_bootstrap(tmp_path: Path) -> None:
    project = _write_valid_project(tmp_path / "project2", agentshore_yaml=VALID_TIERED_CONFIG)
    state = ServerState(active_project_path=str(project))
    with (
        patch("agentshore.core.Orchestrator") as mock_orch_cls,
        patch("agentshore.ipc.state_writer.StateWriter"),
        patch("agentshore.ipc.provider.IpcStateProvider"),
    ):
        mock_orch_cls.bootstrap = AsyncMock(return_value=_NoopOrch())
        await run_session_start(state, start_bridge=False, start_orchestrator=True)
        if state.orchestrator_task is not None:
            await asyncio.wait_for(state.orchestrator_task, timeout=5.0)
    assert mock_orch_cls.bootstrap.await_args.kwargs["seed_path"] is None


def test_make_bridge_binds_to_advertised_port(tmp_path: Path) -> None:
    """Regression: the bridge must listen on the port advertised in ipc_endpoint.

    Reproduces desktop-vlx1 — if _make_bridge lets EmbeddedBridge re-roll a
    free port, the desktop dials a port nothing is listening on and the
    WebSocket loop reconnect-thrashes forever.
    """
    project = _write_valid_project(tmp_path / "advertised-port")
    ipc_endpoint = {"kind": "tcp", "host": "127.0.0.1", "port": 54321}

    bridge = _make_bridge(project, ipc_endpoint)
    try:
        assert bridge.host == "127.0.0.1"
        assert bridge.port == 54321, (
            "EmbeddedBridge must bind to the host/port advertised in "
            "session.start's ipc_endpoint, not re-roll a free port."
        )
    finally:
        # Bridge isn't started — nothing to clean up beyond letting the
        # asyncio.Event go out of scope. Assertion is enough.
        del bridge
