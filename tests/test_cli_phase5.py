"""Phase 5 W3.3: CLI mode dispatch (TUI vs. agent) and command lifecycle."""

from __future__ import annotations

import asyncio
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

import agentshore.session_path as sp
from agentshore.agents.identity import IdentityStatus, RepoAccessStatus
from agentshore.cli import main
from agentshore.cli.agent_select import _needs_interactive_agent_selection
from agentshore.cli.commands.stop import _wait_for_session_exit
from agentshore.cli.constants import _DRAIN_WAIT_POLL_INTERVAL_S
from agentshore.cli.runtime import _dispatch_command, _run_agent_mode
from agentshore.config.models import (
    AgentConfig,
    BudgetConfig,
    GitHubIdentity,
    ModelTierConfig,
    PolicyMode,
    RuntimeConfig,
)

# POSIX: force /tmp so AF_UNIX socket paths stay under the ~104-char sun_path
# limit (macOS). Windows has neither AF_UNIX nor /tmp -> use the system temp.
_TMP_ROOT = None if sys.platform.startswith("win") else "/tmp"


def _close_asyncio_run_arg(coro: object) -> None:
    """Close coroutine objects passed to a mocked asyncio.run."""
    close = getattr(coro, "close", None)
    if callable(close):
        close()


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _skip_startup_tier_guard_for_mocked_config() -> None:
    with patch("agentshore.session.bootstrap.require_startup_model_tier_coverage"):
        yield


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with agentshore.yaml."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "agentshore.yaml").write_text("budget:\n  enabled: true\n  total: 20.0\n")
    return tmp_path


def _mock_cfg() -> MagicMock:
    """Return a MagicMock that quacks like RuntimeConfig."""
    cfg = MagicMock()
    cfg.logging.level = "info"
    cfg.logging.file = False
    cfg.budget.total = 20.0
    cfg.scope.strict_mode = False
    cfg.rl.policy_mode = PolicyMode.LEARNING
    cfg.rl.policy_path = None
    cfg.agents = {}
    cfg.mode = "solo"
    cfg.socket = None
    return cfg


def _create_unix_socket(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
    finally:
        sock.close()


def test_newly_generated_config_prompts_agent_selection() -> None:
    cfg = MagicMock()
    cfg.agents = {
        "codex": AgentConfig(
            enabled=True,
            model_tiers={"sonnet": ModelTierConfig(model="gpt-5.5")},
        )
    }

    assert _needs_interactive_agent_selection(cfg, config_created=True)


def test_existing_config_with_model_tiers_skips_agent_selection() -> None:
    cfg = MagicMock()
    cfg.agents = {
        "codex": AgentConfig(
            enabled=True,
            model_tiers={"sonnet": ModelTierConfig(model="gpt-5.5")},
        )
    }

    assert not _needs_interactive_agent_selection(cfg, config_created=False)


def test_existing_config_without_model_tiers_prompts_agent_selection() -> None:
    cfg = MagicMock()
    cfg.agents = {"codex": AgentConfig(enabled=True)}

    assert _needs_interactive_agent_selection(cfg, config_created=False)


# ---------------------------------------------------------------------------
# 1. Agent mode auto-selects a socket
# ---------------------------------------------------------------------------


def test_agent_mode_auto_selects_socket(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_git_repo(tmp_path)
    cfg = _mock_cfg()
    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(sp, "_SESSIONS_DIR", sessions_dir)

    with (
        patch("agentshore.cli_helpers._find_repo_root", return_value=repo),
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.cli_helpers._detect_api_keys", return_value={}),
        patch("agentshore.config.load_config", return_value=cfg),
        patch("dataclasses.replace", return_value=cfg),
        patch("agentshore.skills.install_skills", return_value=[]) as install_skills,
        patch("agentshore.cli.commands.start.uuid.uuid4", return_value="session-visible"),
        patch("asyncio.run", side_effect=_close_asyncio_run_arg),
    ):
        result = runner.invoke(main, ["start", "--project", str(repo), "--mode", "agent"])

    assert result.exit_code == 0, result.output
    install_skills.assert_called_once_with(repo, force=False)
    assert "Session ID     : session-visible" in result.output
    assert "Policy mode    : learning (PPO learning on)" in result.output
    assert "Deterministic" not in result.output
    assert (
        f"Project key    : {sp.session_socket_path(repo).parent.name} (stable path hash)"
        in result.output
    )
    if sp.sys.platform.startswith("win"):
        assert "IPC            : tcp://127.0.0.1:" in result.output
    else:
        assert f"Socket         : {sp.session_socket_path(repo)}" in result.output
    assert not sp.session_pid_path(repo).exists()


def test_start_fails_when_skill_install_fails(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_git_repo(tmp_path)
    cfg = _mock_cfg()
    monkeypatch.setattr(sp, "_SESSIONS_DIR", tmp_path / "sessions")

    with (
        patch("agentshore.cli_helpers._find_repo_root", return_value=repo),
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.cli_helpers._detect_api_keys", return_value={}),
        patch("agentshore.config.load_config", return_value=cfg),
        patch("dataclasses.replace", return_value=cfg),
        patch("agentshore.skills.install_skills", side_effect=OSError("permission denied")),
        patch("asyncio.run", side_effect=AssertionError("orchestrator should not start")),
    ):
        result = runner.invoke(main, ["start", "--project", str(repo), "--mode", "agent"])

    assert result.exit_code != 0
    assert "failed to install skill templates: permission denied" in result.stderr
    assert not sp.session_pid_path(repo).exists()


def test_start_validation_failure_does_not_write_session_metadata(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "not-a-repo"
    project.mkdir()
    monkeypatch.setattr(
        sp, "_SESSIONS_DIR", Path(tempfile.mkdtemp(prefix="fm_sessions_", dir=_TMP_ROOT))
    )

    result = runner.invoke(main, ["start", "--project", str(project), "--headless"])

    assert result.exit_code != 0
    assert "No git repository found" in result.stderr
    assert not sp.session_pid_path(project).exists()
    assert not sp.session_socket_path(project).exists()


def test_start_fails_fast_when_identity_token_lacks_repo_access(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_git_repo(tmp_path)
    cfg = RuntimeConfig(
        budget=BudgetConfig(enabled=True, total=20.0),
        identities={
            "bot-user": GitHubIdentity(
                git_user_name="Bot-User",
                git_user_email="bot@example.com",
                gh_token_keychain="agentshore/bot-user",
            ),
        },
        agents={"codex": AgentConfig(enabled=True, identity="bot-user")},
    )
    monkeypatch.setattr(sp, "_SESSIONS_DIR", tmp_path / "sessions")

    with (
        patch("agentshore.cli_helpers._find_repo_root", return_value=repo),
        patch(
            "agentshore.cli_helpers._detect_gh_remote",
            return_value={"url": "https://github.com/o/r"},
        ),
        patch("agentshore.cli_helpers._detect_agents", return_value=["codex"]),
        patch("agentshore.cli_helpers._detect_api_keys", return_value={}),
        patch("agentshore.config.load_config", return_value=cfg),
        patch(
            "agentshore.agents.identity.report_identities",
            return_value=[
                IdentityStatus(
                    agent_key="codex",
                    identity_name="bot-user",
                    token_source="keychain",
                    token_resolved=True,
                    token_valid=True,
                    detail="keychain agentshore/bot-user (Bot-User)",
                    resolved_login="Bot-User",
                )
            ],
        ),
        patch(
            "agentshore.agents.identity.report_identity_repo_access",
            return_value=[
                RepoAccessStatus(
                    agent_key="codex",
                    identity_name="bot-user",
                    ok=False,
                    detail=(
                        "GitHub repository access preflight failed for the assigned "
                        "identity token: GraphQL: Could not resolve to a Repository"
                    ),
                )
            ],
        ),
        patch("asyncio.run", side_effect=AssertionError("orchestrator should not start")),
    ):
        result = runner.invoke(main, ["start", "--project", str(repo), "--mode", "agent"])

    assert result.exit_code != 0
    assert "Repository access" in result.output
    assert "codex" in result.output
    assert "[repo: BLOCKED" in result.output
    assert "cannot access this repository" in result.stderr
    assert not sp.session_pid_path(repo).exists()


def test_start_rejects_budget_below_floor(runner: CliRunner, tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)

    result = runner.invoke(main, ["start", "--project", str(repo), "--budget", "19.99"])

    assert result.exit_code != 0
    assert "Budget must be at least $20.00" in result.output


def test_start_rejects_existing_session_without_overwriting_pid(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_git_repo(tmp_path)
    monkeypatch.setattr(
        sp, "_SESSIONS_DIR", Path(tempfile.mkdtemp(prefix="fm_sessions_", dir=_TMP_ROOT))
    )
    pid_path = sp.session_pid_path(repo)
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text("12345", encoding="utf-8")

    # Liveness goes through _process_alive (not a bare os.kill, which is
    # CTRL_C_EVENT on Windows) — mock it so the recorded PID reads as alive.
    monkeypatch.setattr(sp, "_process_alive", lambda pid: pid == 12345)

    result = runner.invoke(main, ["start", "--project", str(repo), "--headless"])

    assert result.exit_code != 0
    assert "An AgentShore session is already running" in result.stderr
    assert pid_path.read_text(encoding="utf-8") == "12345"


def test_start_runtime_error_cleans_session_metadata(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_git_repo(tmp_path)
    cfg = _mock_cfg()
    monkeypatch.setattr(
        sp, "_SESSIONS_DIR", Path(tempfile.mkdtemp(prefix="fm_sessions_", dir=_TMP_ROOT))
    )

    def fail_asyncio_run(coro: object) -> None:
        _close_asyncio_run_arg(coro)
        raise RuntimeError("boom")

    with (
        patch("agentshore.cli_helpers._find_repo_root", return_value=repo),
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.cli_helpers._detect_api_keys", return_value={}),
        patch("agentshore.config.load_config", return_value=cfg),
        patch("dataclasses.replace", return_value=cfg),
        patch("asyncio.run", side_effect=fail_asyncio_run),
    ):
        result = runner.invoke(main, ["start", "--project", str(repo), "--mode", "agent"])

    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert not sp.session_pid_path(repo).exists()
    assert not sp.session_socket_path(repo).exists()


# ---------------------------------------------------------------------------
# 2. Agent mode creates IPC server (test _run_agent_mode directly)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_agent_mode_creates_server(tmp_path: Path) -> None:
    sock = str(tmp_path / "test.sock")

    mock_server = MagicMock()
    mock_server.start = AsyncMock()
    mock_server.stop = AsyncMock()
    mock_server.command_queue = asyncio.Queue()

    mock_orch = MagicMock()
    mock_orch.__aenter__ = AsyncMock(return_value=mock_orch)
    mock_orch.__aexit__ = AsyncMock(return_value=False)
    mock_orch.run_until_idle = AsyncMock()
    mock_orch.stop = AsyncMock()

    with (
        patch("agentshore.ipc.IpcServer", return_value=mock_server) as ipc_cls,
        patch("agentshore.ipc.IpcStateProvider"),
        patch("agentshore.core.Orchestrator.bootstrap", new_callable=AsyncMock) as mock_boot,
    ):
        mock_boot.return_value = mock_orch
        await _run_agent_mode(
            cfg=_mock_cfg(),
            repo_root=tmp_path,
            socket_path=sock,
            seed_path=None,
            policy_path=None,
            policy_mode=PolicyMode.LEARNING,
            session_id="sess-cli",
        )

    ipc_cls.assert_called_once()
    endpoint = ipc_cls.call_args.args[0]
    assert endpoint.kind == "unix"
    assert str(endpoint.path) == sock
    mock_server.start.assert_awaited_once()
    mock_server.stop.assert_awaited_once()
    assert mock_boot.await_args.kwargs["session_id"] == "sess-cli"


# ---------------------------------------------------------------------------
# 3. TUI mode creates OrchestratorApp (via CLI dispatch)
# ---------------------------------------------------------------------------


def test_tui_mode_creates_app(runner: CliRunner, tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    cfg = _mock_cfg()

    with (
        patch("agentshore.cli_helpers._find_repo_root", return_value=repo),
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.cli_helpers._detect_api_keys", return_value={}),
        patch("agentshore.config.load_config", return_value=cfg),
        patch("dataclasses.replace", return_value=cfg),
        patch("agentshore.cli.commands.start._run_solo_mode") as mock_solo,
    ):
        result = runner.invoke(main, ["start"])

    assert result.exit_code == 0, result.output
    assert "Mode           : tui" in result.output
    mock_solo.assert_called_once()


def test_tui_flag_creates_app(runner: CliRunner, tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    cfg = _mock_cfg()

    with (
        patch("agentshore.cli_helpers._find_repo_root", return_value=repo),
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.cli_helpers._detect_api_keys", return_value={}),
        patch("agentshore.config.load_config", return_value=cfg),
        patch("dataclasses.replace", return_value=cfg),
        patch("agentshore.cli.commands.start._run_solo_mode") as mock_solo,
    ):
        result = runner.invoke(main, ["start", "--project", str(repo), "--tui"])

    assert result.exit_code == 0, result.output
    assert "Mode           : tui" in result.output
    mock_solo.assert_called_once()


def test_tui_flag_conflicts_with_agent_mode(runner: CliRunner) -> None:
    result = runner.invoke(main, ["start", "--mode", "agent", "--tui"])

    assert result.exit_code != 0
    assert "--tui cannot be combined with --mode agent" in result.output


# ---------------------------------------------------------------------------
# 4-6. Command dispatch tests via _dispatch_command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_dispatch_command_pause() -> None:
    orch = MagicMock()
    orch.pause = AsyncMock()
    await _dispatch_command({"command": "pause"}, orch)
    orch.pause.assert_awaited_once_with("ipc_request")


@pytest.mark.asyncio()
async def test_dispatch_command_resume() -> None:
    orch = MagicMock()
    orch.resume = AsyncMock()
    await _dispatch_command({"command": "resume"}, orch)
    orch.resume.assert_awaited_once()


@pytest.mark.asyncio()
async def test_dispatch_command_shutdown() -> None:
    orch = MagicMock()
    orch.stop = AsyncMock()
    await _dispatch_command({"command": "shutdown"}, orch)
    orch.stop.assert_awaited_once()


@pytest.mark.asyncio()
async def test_dispatch_command_feedback_response_continue_resumes_session() -> None:
    """Regression for desktop-4hp — Continue button must actually resume.

    Before this fix the handler logged "obsolete" and did nothing, leaving
    the orchestrator paused after the user dismissed a loop_detected modal.
    """
    orch = MagicMock()
    orch.resume = AsyncMock()
    orch.begin_drain = AsyncMock()

    await _dispatch_command({"command": "feedback_response", "action": "continue"}, orch)

    orch.resume.assert_awaited_once()
    orch.begin_drain.assert_not_called()


@pytest.mark.asyncio()
async def test_dispatch_command_feedback_response_pause_does_not_resume() -> None:
    orch = MagicMock()
    orch.resume = AsyncMock()
    orch.begin_drain = AsyncMock()

    await _dispatch_command({"command": "feedback_response", "action": "pause"}, orch)

    orch.resume.assert_not_called()
    orch.begin_drain.assert_not_called()


@pytest.mark.asyncio()
async def test_dispatch_command_feedback_response_stop_begins_drain() -> None:
    orch = MagicMock()
    orch.resume = AsyncMock()
    orch.begin_drain = AsyncMock()

    await _dispatch_command({"command": "feedback_response", "action": "stop"}, orch)

    orch.begin_drain.assert_awaited_once_with("user_request")
    orch.resume.assert_not_called()


@pytest.mark.asyncio()
async def test_dispatch_feedback_pause() -> None:
    orch = MagicMock()
    orch.pause = AsyncMock()
    await _dispatch_command({"command": "feedback_response", "action": "pause"}, orch)
    orch.pause.assert_not_awaited()


@pytest.mark.asyncio()
async def test_dispatch_feedback_stop_calls_begin_drain() -> None:
    orch = MagicMock()
    orch.begin_drain = AsyncMock()
    await _dispatch_command({"command": "feedback_response", "action": "stop"}, orch)
    orch.begin_drain.assert_awaited_once_with("user_request")


@pytest.mark.asyncio()
async def test_dispatch_drain_can_request_end_session_report() -> None:
    orch = MagicMock()
    orch.request_end_session_report = MagicMock()
    orch.begin_drain = AsyncMock()

    await _dispatch_command(
        {
            "command": "drain",
            "reason": "cli_request",
            "end_session_report": True,
            "open_report": False,
        },
        orch,
    )

    orch.request_end_session_report.assert_called_once_with(open_browser=False)
    orch.begin_drain.assert_awaited_once_with("cli_request")


def test_stop_requests_managed_esr_for_clean_drain(runner: CliRunner, tmp_path: Path) -> None:
    project = _make_git_repo(tmp_path)

    with (
        patch("agentshore.session_path.is_session_running", return_value=True),
        patch("agentshore.session_path.request_drain", return_value="sent") as request_drain,
        patch("agentshore.cli.commands.stop._wait_for_session_exit", return_value=True),
        patch("agentshore.cli.commands.stop._generate_end_session_report_cli") as generate_report,
    ):
        result = runner.invoke(main, ["stop", "--project", str(project)])

    assert result.exit_code == 0, result.output
    request_drain.assert_called_once_with(project, end_session_report=True, open_report=True)
    generate_report.assert_not_called()


def test_wait_for_session_exit_waits_indefinitely_for_clean_drain(
    tmp_path: Path,
) -> None:
    """No deadline: the wait polls across many ticks until the process exits."""
    with (
        patch("agentshore.session_path.read_pid", return_value=1234),
        # Liveness is probed via _process_alive (never os.kill(pid, 0), which is
        # CTRL_C_EVENT on Windows): alive for several polls, then it exits.
        patch(
            "agentshore.session_path._process_alive",
            side_effect=[True, True, True, False],
        ) as alive,
        patch("time.sleep") as sleep,
        patch("agentshore.session_path.hard_stop_session") as hard_stop,
    ):
        clean_exit = _wait_for_session_exit(tmp_path)

    assert clean_exit is True
    assert alive.call_count == 4
    sleep.assert_called_with(_DRAIN_WAIT_POLL_INTERVAL_S)
    hard_stop.assert_not_called()


def test_wait_for_session_exit_escalates_on_keyboard_interrupt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl+C during the drain wait escalates to a hard stop (the only deadline)."""
    with (
        patch("agentshore.session_path.read_pid", return_value=1234),
        patch("agentshore.session_path._process_alive", return_value=True),
        patch("time.sleep", side_effect=KeyboardInterrupt),
        patch("agentshore.session_path.hard_stop_session", return_value=True) as hard_stop,
    ):
        clean_exit = _wait_for_session_exit(tmp_path)

    assert clean_exit is False
    hard_stop.assert_called_once_with(tmp_path)
    assert "Force-stop requested; escalating to hard stop..." in capsys.readouterr().out


def test_wait_for_session_exit_returns_none_when_hard_stop_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the escalated hard stop can't kill the process, return None (#31)."""
    with (
        patch("agentshore.session_path.read_pid", return_value=1234),
        patch("agentshore.session_path._process_alive", return_value=True),
        patch("time.sleep", side_effect=KeyboardInterrupt),
        patch("agentshore.session_path.hard_stop_session", return_value=False) as hard_stop,
    ):
        outcome = _wait_for_session_exit(tmp_path)

    assert outcome is None
    hard_stop.assert_called_once_with(tmp_path)
    assert "hard stop failed" in capsys.readouterr().err


def test_stop_reports_failure_when_session_survives_hard_stop(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The graceful stop path must exit non-zero — not print 'stopped' — when the
    process is still alive after the hard-stop escalation (#31)."""
    project = _make_git_repo(tmp_path)

    with (
        patch("agentshore.session_path.is_session_running", return_value=True),
        patch("agentshore.session_path.request_drain", return_value="sent"),
        patch("agentshore.cli.commands.stop._wait_for_session_exit", return_value=None),
    ):
        result = runner.invoke(main, ["stop", "--project", str(project)])

    assert result.exit_code == 1, result.output
    assert "still running after hard stop" in result.output
    assert "AgentShore session stopped." not in result.output


def test_stop_hard_skips_end_session_report(runner: CliRunner, tmp_path: Path) -> None:
    project = _make_git_repo(tmp_path)

    with (
        patch("agentshore.session_path.is_session_running", return_value=True),
        patch("agentshore.session_path.hard_stop_session", return_value=True),
        patch("agentshore.cli.commands.stop._generate_end_session_report_cli") as generate_report,
    ):
        result = runner.invoke(main, ["stop", "--project", str(project), "--hard"])

    assert result.exit_code == 0, result.output
    generate_report.assert_not_called()
    assert "AgentShore session force-stopped." in result.output


# ---------------------------------------------------------------------------
# 7. Server stopped on exit (finally block)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_agent_mode_server_stopped_on_exit(tmp_path: Path) -> None:
    sock = str(tmp_path / "test.sock")

    mock_server = MagicMock()
    mock_server.start = AsyncMock()
    mock_server.stop = AsyncMock()
    mock_server.command_queue = asyncio.Queue()

    mock_orch = MagicMock()
    mock_orch.__aenter__ = AsyncMock(return_value=mock_orch)
    mock_orch.__aexit__ = AsyncMock(return_value=False)
    # Simulate an error during run_until_idle
    mock_orch.run_until_idle = AsyncMock(side_effect=RuntimeError("boom"))
    mock_orch.stop = AsyncMock()

    with (
        patch("agentshore.ipc.IpcServer", return_value=mock_server),
        patch("agentshore.ipc.IpcStateProvider"),
        patch("agentshore.core.Orchestrator.bootstrap", new_callable=AsyncMock) as mock_boot,
    ):
        mock_boot.return_value = mock_orch
        with pytest.raises(RuntimeError, match="boom"):
            await _run_agent_mode(
                cfg=_mock_cfg(),
                repo_root=tmp_path,
                socket_path=sock,
                seed_path=None,
                policy_path=None,
                policy_mode=PolicyMode.LEARNING,
            )

    # Even though run_until_idle raised, server.stop must be called
    mock_server.stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# 8. Phase 2 warning removed
# ---------------------------------------------------------------------------


def test_phase2_warning_removed(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_git_repo(tmp_path)
    sock = str(tmp_path / "test.sock")
    cfg = _mock_cfg()
    monkeypatch.setattr(
        sp, "_SESSIONS_DIR", Path(tempfile.mkdtemp(prefix="fm_sessions_", dir=_TMP_ROOT))
    )

    with (
        patch("agentshore.cli_helpers._find_repo_root", return_value=repo),
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.cli_helpers._detect_api_keys", return_value={}),
        patch("agentshore.config.load_config", return_value=cfg),
        patch("dataclasses.replace", return_value=cfg),
        patch("asyncio.run", side_effect=_close_asyncio_run_arg),
    ):
        result = runner.invoke(main, ["start", "--mode", "agent", "--socket", sock])

    combined = result.output
    assert "not implemented in Phase 2" not in combined
    assert "Warning: agent IPC" not in combined


def test_start_cleanup_stops_recorded_dashboard_process(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_git_repo(tmp_path)
    cfg = _mock_cfg()
    monkeypatch.setattr(
        sp, "_SESSIONS_DIR", Path(tempfile.mkdtemp(prefix="fm_sessions_", dir=_TMP_ROOT))
    )

    with (
        patch("agentshore.cli_helpers._find_repo_root", return_value=repo),
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.cli_helpers._detect_api_keys", return_value={}),
        patch("agentshore.config.load_config", return_value=cfg),
        patch("dataclasses.replace", return_value=cfg),
        patch("asyncio.run", side_effect=_close_asyncio_run_arg),
        patch("agentshore.session_path.stop_dashboard_process", return_value=True) as stop_dash,
    ):
        result = runner.invoke(main, ["start", "--project", str(repo), "--mode", "agent"])

    assert result.exit_code == 0, result.output
    stop_dash.assert_called_once_with(repo)


# ---------------------------------------------------------------------------
# 9. Auto-discovery for `agentshore dashboard` and `--socket` registration
# ---------------------------------------------------------------------------


def test_explicit_socket_override_writes_session_info_and_symlink(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agentshore start --socket PATH` should register info.json + a symlink at
    the well-known path so `agentshore dashboard` can auto-discover it.

    The check captures session_info during the run via a custom asyncio.run
    stub, because the start command's finally block cleans up afterward.
    """
    repo = _make_git_repo(tmp_path)
    cfg = _mock_cfg()
    monkeypatch.setattr(sp, "_SESSIONS_DIR", tmp_path / "sessions")

    explicit_socket = tmp_path / "custom.sock"

    captured: dict[str, object] = {}

    def capture_during_run(coro: object) -> None:
        # Mid-run snapshot — info.json should already exist by now.
        info = sp.read_session_info(repo)
        captured["info"] = info
        captured["well_known_is_symlink"] = sp.session_socket_path(repo).is_symlink()
        _close_asyncio_run_arg(coro)

    with (
        patch("agentshore.cli_helpers._find_repo_root", return_value=repo),
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.cli_helpers._detect_api_keys", return_value={}),
        patch("agentshore.config.load_config", return_value=cfg),
        patch("dataclasses.replace", return_value=cfg),
        patch("agentshore.cli.commands.start.uuid.uuid4", return_value="session-info-test"),
        patch("asyncio.run", side_effect=capture_during_run),
    ):
        result = runner.invoke(
            main,
            ["start", "--project", str(repo), "--mode", "agent", "--socket", str(explicit_socket)],
        )

    assert result.exit_code == 0, result.output
    info = captured.get("info")
    assert isinstance(info, dict), "info.json should be written even with --socket override"
    assert info["socket"] == str(explicit_socket)
    assert info["session_id"] == "session-info-test"
    assert info["project_key"] == sp.session_socket_path(repo).parent.name
    # Symlink creation is best-effort; Windows often requires elevated privileges.
    if not sp.sys.platform.startswith("win"):
        assert captured["well_known_is_symlink"] is True


def test_explicit_socket_matching_well_known_path_does_not_self_symlink(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `--socket` is the well-known path itself (as the backgrounded
    dashboard launcher re-passes it to its child), `start` must not create
    socket.sock -> socket.sock — that self-loop fails bind() with ELOOP.
    """
    repo = _make_git_repo(tmp_path)
    cfg = _mock_cfg()
    monkeypatch.setattr(sp, "_SESSIONS_DIR", tmp_path / "sessions")

    well_known = sp.session_socket_path(repo)

    captured: dict[str, object] = {}

    def capture_during_run(coro: object) -> None:
        captured["is_symlink"] = well_known.is_symlink()
        captured["exists"] = well_known.exists()
        _close_asyncio_run_arg(coro)

    with (
        patch("agentshore.cli_helpers._find_repo_root", return_value=repo),
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.cli_helpers._detect_api_keys", return_value={}),
        patch("agentshore.config.load_config", return_value=cfg),
        patch("dataclasses.replace", return_value=cfg),
        patch("asyncio.run", side_effect=capture_during_run),
    ):
        result = runner.invoke(
            main,
            ["start", "--project", str(repo), "--mode", "agent", "--socket", str(well_known)],
        )

    assert result.exit_code == 0, result.output
    assert captured["is_symlink"] is False, "must not create a self-referential symlink"
    assert captured["exists"] is False, "no socket file should exist before bind()"


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="AF_UNIX is POSIX-only; Windows uses TCP discovery"
)
def test_dashboard_auto_discovers_socket_for_project(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agentshore dashboard --project DIR` resolves the socket via the hash
    convention without needing --socket."""
    from agentshore.cli import main as cli_main

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        sp, "_SESSIONS_DIR", Path(tempfile.mkdtemp(prefix="fm_sessions_", dir=_TMP_ROOT))
    )

    # Pretend a session is running by writing a live PID and the socket file.
    monkeypatch.setattr(sp.os, "kill", lambda pid, sig: None)
    sp.write_pid(project)
    sock_path = sp.session_socket_path(project)
    _create_unix_socket(sock_path)

    bridge = MagicMock()
    bridge.start = AsyncMock()

    with patch("agentshore.dashboard.DashboardBridge", return_value=bridge) as bridge_cls:
        result = runner.invoke(cli_main, ["dashboard", "--project", str(project), "--no-open"])

    assert result.exit_code == 0, result.output
    assert "Discovered session IPC:" in result.output
    bridge_cls.assert_called_once()
    kwargs = bridge_cls.call_args.kwargs
    assert kwargs["ipc_endpoint"].kind == "unix"
    assert kwargs["ipc_endpoint"].path == sock_path


def test_dashboard_does_not_reap_its_own_parent_pid(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""Regression: the startup self-guard must never reap the bridge's own
    lineage. On Windows the uv-tool launcher is a Scripts\python.exe trampoline
    that spawns the bridge as a grandchild, so dashboard.pid can hold the
    bridge's *parent* pid; reaping it with taskkill /T would kill the bridge's
    own process tree before the server binds (the Windows dashboard never
    showed). The guard excludes both os.getpid() and os.getppid()."""
    import os

    from agentshore.cli import main as cli_main

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        sp, "_SESSIONS_DIR", Path(tempfile.mkdtemp(prefix="fm_sessions_", dir=_TMP_ROOT))
    )

    # Simulate a supervisor/trampoline having recorded our *parent's* pid.
    sp.write_dashboard_pid(project, os.getppid())

    reaped: list[int | None] = []

    def fake_stop(_project: Path, *, pid: int | None = None) -> bool:
        reaped.append(pid)
        return True

    bridge = MagicMock()
    bridge.start = AsyncMock()

    with (
        patch("agentshore.session_path.stop_dashboard_process", side_effect=fake_stop),
        patch("agentshore.dashboard.DashboardBridge", return_value=bridge),
    ):
        result = runner.invoke(
            cli_main,
            [
                "dashboard",
                "--project",
                str(project),
                "--ipc-host",
                "127.0.0.1",
                "--ipc-port",
                "49999",
                "--no-open",
            ],
        )

    assert result.exit_code == 0, result.output
    # The parent pid must NOT have been reaped (no self-kill)...
    assert reaped == []
    # ...and the bridge must record its OWN real pid, overwriting the parent's.
    assert sp.read_dashboard_pid(project) == os.getpid()


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="AF_UNIX is POSIX-only; Windows uses TCP discovery"
)
def test_dashboard_reports_no_session_when_socket_stale(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the recorded PID is gone, dashboard should report 'no running session'
    rather than connecting to a dead socket."""
    from agentshore.cli import main as cli_main

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        sp, "_SESSIONS_DIR", Path(tempfile.mkdtemp(prefix="fm_sessions_", dir=_TMP_ROOT))
    )

    _create_unix_socket(sp.session_socket_path(project))
    sp.session_pid_path(project).write_text("424242", encoding="utf-8")

    def dead(pid: int, sig: int) -> None:
        raise OSError

    monkeypatch.setattr(sp.os, "kill", dead)

    result = runner.invoke(cli_main, ["dashboard", "--project", str(project), "--no-open"])

    assert result.exit_code != 0
    assert "No running AgentShore session" in result.stderr
    # The stale socket and PID should have been cleaned up by discover_socket.
    assert not sp.session_socket_path(project).exists()
    assert not sp.session_pid_path(project).exists()


def test_dashboard_ipc_host_requires_ipc_port(runner: CliRunner, tmp_path: Path) -> None:
    """`--ipc-host` without `--ipc-port` is rejected rather than silently ignored."""
    from agentshore.cli import main as cli_main

    project = tmp_path / "project"
    project.mkdir()

    result = runner.invoke(
        cli_main,
        ["dashboard", "--project", str(project), "--ipc-host", "10.0.0.1", "--no-open"],
    )

    assert result.exit_code != 0
    assert "--ipc-host requires --ipc-port" in result.output


# ---------------------------------------------------------------------------
# New drain-audit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_dispatch_verification_response_uses_passed_field() -> None:
    orch = MagicMock()
    orch.resume = AsyncMock()
    await _dispatch_command(
        {"command": "verification_response", "checkpoint_id": "c1", "passed": True},
        orch,
    )
    orch.resume.assert_awaited_once()


@pytest.mark.asyncio()
async def test_dispatch_reload_config_calls_orchestrator() -> None:
    """reload_config IPC command calls orch.reload_config() exactly once."""
    orch = MagicMock()
    orch.reload_config = AsyncMock()
    await _dispatch_command({"command": "reload_config"}, orch)
    orch.reload_config.assert_awaited_once_with()
