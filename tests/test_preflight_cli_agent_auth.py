"""Focused tests for the CLI-agent backend-auth preflight gate."""

from __future__ import annotations

from unittest.mock import patch

import pytest

# Import the CLI package first so the start <-> bootstrap module pair initializes
# in dependency order; importing bootstrap in isolation as the first agentshore
# submodule trips a pre-existing circular-import ordering sensitivity.
import agentshore.cli.commands.start  # noqa: F401
from agentshore.agents.auth_probe import (
    AUTH_EXPIRED,
    AUTH_OK,
    AUTH_TIMEOUT,
    AuthProbeResult,
)
from agentshore.config import RuntimeConfig
from agentshore.session.bootstrap import preflight_cli_agent_auth
from agentshore.state import AgentType


def _expired() -> AuthProbeResult:
    return AuthProbeResult(AgentType.CODEX, AUTH_EXPIRED, "run 'codex login'")


def _ok() -> AuthProbeResult:
    return AuthProbeResult(AgentType.CODEX, AUTH_OK, "authenticated")


def _timeout() -> AuthProbeResult:
    return AuthProbeResult(AgentType.CODEX, AUTH_TIMEOUT, "auth probe timed out after 10s")


def test_expired_backend_auth_exits_one() -> None:
    cfg = RuntimeConfig()
    with (
        patch(
            "agentshore.agents.auth_probe.probe_configured_cli_auth",
            return_value=[_expired()],
        ),
        pytest.raises(SystemExit) as excinfo,
    ):
        preflight_cli_agent_auth(cfg)
    assert excinfo.value.code == 1


def test_ok_backend_auth_does_not_exit() -> None:
    cfg = RuntimeConfig()
    with patch(
        "agentshore.agents.auth_probe.probe_configured_cli_auth",
        return_value=[_ok()],
    ):
        preflight_cli_agent_auth(cfg)  # must not raise


def test_non_blocking_status_warns_but_does_not_exit() -> None:
    cfg = RuntimeConfig()
    with patch(
        "agentshore.agents.auth_probe.probe_configured_cli_auth",
        return_value=[_timeout()],
    ):
        preflight_cli_agent_auth(cfg)  # timeout is non-blocking — must not raise


def test_empty_config_is_noop() -> None:
    cfg = RuntimeConfig()
    with patch(
        "agentshore.agents.auth_probe.probe_configured_cli_auth",
        return_value=[],
    ):
        preflight_cli_agent_auth(cfg)  # no CLI agents → return cleanly


def _invoke_start(args: list[str]):
    """Drive `agentshore start` up to (and including) the preflight block, then
    bail out via a sentinel raised from the dispatch import so no orchestrator
    machinery runs. Returns the patched preflight mock for assertions.
    """
    from click.testing import CliRunner

    from agentshore.cli.commands import start as start_mod

    runner = CliRunner()
    with (
        patch.object(start_mod, "maybe_re_exec_under_caffeinate"),
        patch.object(start_mod, "bootstrap_session") as mock_bootstrap,
        patch.object(start_mod, "echo_bootstrap_summary"),
        patch.object(start_mod, "preflight_identities"),
        patch.object(start_mod, "preflight_cli_agent_auth") as mock_auth,
    ):
        # The dispatch block right after the preflight calls dereferences
        # `resolved.cfg_path` (and friends). Make THAT attribute raise so we stop
        # immediately after the preflight block — but `resolved.cfg` (consumed by
        # both preflight calls) must still read cleanly, so leave it as a plain
        # MagicMock attribute.
        type(mock_bootstrap.return_value).cfg_path = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("stop-after-preflight"))
        )
        result = runner.invoke(start_mod.start, args, catch_exceptions=True)
    return result, mock_auth


def test_skip_auth_preflight_flag_bypasses_probe() -> None:
    """--skip-auth-preflight must NOT call preflight_cli_agent_auth."""
    result, mock_auth = _invoke_start(["--skip-auth-preflight", "--project", "."])
    assert isinstance(result.exception, RuntimeError)
    mock_auth.assert_not_called()


def test_without_skip_flag_calls_preflight() -> None:
    """Without the flag, preflight_cli_agent_auth IS called once."""
    result, mock_auth = _invoke_start(["--project", "."])
    assert isinstance(result.exception, RuntimeError)
    mock_auth.assert_called_once()
