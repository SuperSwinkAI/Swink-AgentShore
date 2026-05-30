"""Phase 4D: CLI --config flag, hard-fail logic, PPOSelector wiring into bootstrap."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from agentshore.cli import main
from agentshore.config import PolicyMode


def _close_asyncio_run_arg(coro: object) -> None:
    """Close coroutine objects passed to a mocked asyncio.run."""
    close = getattr(coro, "close", None)
    if callable(close):
        close()


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with agentshore.yaml."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "agentshore.yaml").write_text("budget:\n  enabled: true\n  total: 20.0\n")
    return tmp_path


# ---------------------------------------------------------------------------
# --config flag
# ---------------------------------------------------------------------------


def test_start_config_flag_loads_specified_file(tmp_path: Path) -> None:
    """--config <path> should load that file instead of the default agentshore.yaml."""
    repo = _make_git_repo(tmp_path)
    custom_cfg = tmp_path / "custom.yaml"
    custom_cfg.write_text("budget:\n  enabled: true\n  total: 99.0\n")

    loaded_paths: list[Path] = []

    def capture_load(path: Any) -> MagicMock:
        if path:
            loaded_paths.append(Path(path))
        mock_cfg = MagicMock()
        mock_cfg.logging.level = "info"
        mock_cfg.logging.file = False
        mock_cfg.budget.total = 20.0
        mock_cfg.scope.strict_mode = False
        mock_cfg.rl.policy_mode = PolicyMode.LEARNING
        mock_cfg.rl.policy_path = None
        mock_cfg.agents = {}
        mock_cfg.mode = "solo"
        mock_cfg.socket = None
        return mock_cfg

    runner = CliRunner()
    with (
        patch("agentshore.cli._find_repo_root", return_value=repo),
        patch("agentshore.cli._detect_gh_remote", return_value={}),
        patch("agentshore.cli._detect_agents", return_value=["claude"]),
        patch("agentshore.cli._detect_api_keys", return_value={}),
        patch("agentshore.session_path.is_session_running", return_value=False),
        patch("agentshore.config.load_config", side_effect=capture_load),
        patch("agentshore.core.Orchestrator.bootstrap", new_callable=AsyncMock) as mock_bootstrap,
        patch("asyncio.run", side_effect=_close_asyncio_run_arg),
    ):
        mock_orch = MagicMock()
        mock_orch.__aenter__ = AsyncMock(return_value=mock_orch)
        mock_orch.__aexit__ = AsyncMock(return_value=False)
        mock_orch.run_until_idle = AsyncMock()
        mock_bootstrap.return_value = mock_orch

        runner.invoke(main, ["start", "--config", str(custom_cfg)])

    # The custom path should appear in the load calls
    assert any(str(custom_cfg) in str(p) for p in loaded_paths), (
        f"Custom config not loaded. Loaded: {loaded_paths}"
    )


# ---------------------------------------------------------------------------
# Hard-fail: no agents AND no API keys
# ---------------------------------------------------------------------------


def test_start_hard_fail_no_agents_no_keys(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    runner = CliRunner()

    with (
        patch("agentshore.cli._find_repo_root", return_value=repo),
        patch("agentshore.cli._detect_gh_remote", return_value={}),
        patch("agentshore.cli._detect_agents", return_value=[]),
        patch("agentshore.cli._detect_api_keys", return_value={}),
        patch("agentshore.session_path.is_session_running", return_value=False),
    ):
        result = runner.invoke(main, ["start"])

    assert result.exit_code != 0
    assert "ANTHROPIC_API_KEY" in result.output
    assert "OPENAI_API_KEY" in result.output
    assert "Gemini CLI" in result.output


def test_start_no_fail_when_api_key_present(tmp_path: Path) -> None:
    """Should not hard-fail if no CLI agents but an API key is set."""
    repo = _make_git_repo(tmp_path)
    runner = CliRunner()

    with (
        patch("agentshore.cli._find_repo_root", return_value=repo),
        patch("agentshore.cli._detect_gh_remote", return_value={}),
        patch("agentshore.cli._detect_agents", return_value=[]),
        patch("agentshore.cli._detect_api_keys", return_value={"ANTHROPIC_API_KEY": True}),
        patch("agentshore.config.load_config") as mock_load,
        patch("asyncio.run", side_effect=_close_asyncio_run_arg),
    ):
        mock_cfg = MagicMock()
        mock_cfg.logging.level = "info"
        mock_cfg.logging.file = False
        mock_cfg.budget.total = 5.0
        mock_cfg.scope.strict_mode = False
        mock_cfg.rl.policy_mode = PolicyMode.LEARNING
        mock_cfg.rl.policy_path = None
        mock_cfg.agents = {}
        mock_cfg.mode = "solo"
        mock_cfg.socket = None
        mock_load.return_value = mock_cfg

        result = runner.invoke(main, ["start"])

    # Should not see the hard-fail error.
    assert "No coding agents found" not in result.output


def test_start_no_fail_when_cli_agent_present(tmp_path: Path) -> None:
    """Should not hard-fail if a CLI agent is present, even without API keys."""
    repo = _make_git_repo(tmp_path)
    runner = CliRunner()

    with (
        patch("agentshore.cli._find_repo_root", return_value=repo),
        patch("agentshore.cli._detect_gh_remote", return_value={}),
        patch("agentshore.cli._detect_agents", return_value=["claude"]),
        patch("agentshore.cli._detect_api_keys", return_value={}),
        patch("agentshore.config.load_config") as mock_load,
        patch("asyncio.run", side_effect=_close_asyncio_run_arg),
    ):
        mock_cfg = MagicMock()
        mock_cfg.logging.level = "info"
        mock_cfg.logging.file = False
        mock_cfg.budget.total = 5.0
        mock_cfg.scope.strict_mode = False
        mock_cfg.rl.policy_mode = PolicyMode.LEARNING
        mock_cfg.rl.policy_path = None
        mock_cfg.agents = {}
        mock_cfg.mode = "solo"
        mock_cfg.socket = None
        mock_load.return_value = mock_cfg

        result = runner.invoke(main, ["start"])

    # Should not see the hard-fail message
    assert "No coding agents found" not in result.output
