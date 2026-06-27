"""Focused tests for the per-identity git-remote auth preflight gate."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# Import the CLI package first so the start <-> bootstrap module pair initializes
# in dependency order; importing bootstrap in isolation as the first agentshore
# submodule trips a pre-existing circular-import ordering sensitivity.
import agentshore.cli.commands.start  # noqa: F401
from agentshore.agents.git_auth_probe import (
    GIT_AUTH_ERROR,
    GIT_AUTH_FAILED,
    GIT_AUTH_OK,
    GIT_AUTH_TIMEOUT,
    GIT_AUTH_UNPROBEABLE,
    GitAuthProbeResult,
    probe_all_identities,
    probe_git_auth,
)
from agentshore.command import CommandResult, CommandStatus
from agentshore.config import RuntimeConfig
from agentshore.config.models import GitHubIdentity
from agentshore.session.bootstrap import preflight_git_auth

_HTTPS_REMOTE = "https://github.com/owner/repo.git"
_SSH_REMOTE = "git@github.com:owner/repo.git"


def _failed(name: str = "alice") -> GitAuthProbeResult:
    return GitAuthProbeResult(name, GIT_AUTH_FAILED, "authentication failed", _HTTPS_REMOTE)


def _ok(name: str = "alice") -> GitAuthProbeResult:
    return GitAuthProbeResult(name, GIT_AUTH_OK, "authenticated", _HTTPS_REMOTE)


def _timeout(name: str = "alice") -> GitAuthProbeResult:
    return GitAuthProbeResult(
        name, GIT_AUTH_TIMEOUT, "git ls-remote timed out after 15s", _HTTPS_REMOTE
    )


def _unprobeable(name: str = "alice") -> GitAuthProbeResult:
    return GitAuthProbeResult(name, GIT_AUTH_UNPROBEABLE, "no origin remote configured", "")


def _cfg_with_identity() -> RuntimeConfig:
    ident = GitHubIdentity(git_user_name="Alice", git_user_email="alice@example.com")
    return RuntimeConfig(identities={"alice": ident})


def test_failed_blocks_launch() -> None:
    r = _failed()
    assert r.blocks_launch is True
    assert r.ok is False


def test_ok_does_not_block() -> None:
    r = _ok()
    assert r.blocks_launch is False
    assert r.ok is True


def test_timeout_surfaced_but_not_blocking() -> None:
    r = _timeout()
    assert r.blocks_launch is False
    assert r.ok is False


def test_unprobeable_is_ok_nonblocking() -> None:
    r = _unprobeable()
    assert r.blocks_launch is False
    assert r.ok is True


def _result(
    returncode: int, stderr: str = "", *, status: CommandStatus = CommandStatus.OK
) -> CommandResult:
    return CommandResult(
        args=("git", "ls-remote"),
        returncode=returncode,
        stdout="",
        stderr=stderr,
        status=status
        if returncode == 0
        else (status if status is not CommandStatus.OK else CommandStatus.NONZERO),
    )


def test_probe_ok_on_clean_ls_remote() -> None:
    with patch(
        "agentshore.agents.git_auth_probe.command.git_sync",
        return_value=_result(0),
    ) as gs:
        r = probe_git_auth("alice", {"GH_TOKEN": "tok"}, remote=_HTTPS_REMOTE)
    assert r.status == GIT_AUTH_OK
    # Token must inject the auth overlay (HTTPS path).
    overlay = gs.call_args.kwargs["env_overlay"]
    assert overlay["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_CONFIG_COUNT" in overlay


def test_probe_auth_failed_on_credential_rejection() -> None:
    with patch(
        "agentshore.agents.git_auth_probe.command.git_sync",
        return_value=_result(
            128, "fatal: Authentication failed for 'https://github.com/owner/repo.git/'"
        ),
    ):
        r = probe_git_auth("alice", {"GH_TOKEN": "tok"}, remote=_HTTPS_REMOTE)
    assert r.status == GIT_AUTH_FAILED
    assert r.blocks_launch is True


def test_probe_nonauth_error_is_nonblocking() -> None:
    with patch(
        "agentshore.agents.git_auth_probe.command.git_sync",
        return_value=_result(128, "fatal: unable to access: Could not resolve host: github.com"),
    ):
        r = probe_git_auth("alice", {"GH_TOKEN": "tok"}, remote=_HTTPS_REMOTE)
    assert r.status == GIT_AUTH_ERROR
    assert r.blocks_launch is False


def test_probe_timeout_is_nonblocking() -> None:
    timed_out = CommandResult(
        args=("git", "ls-remote"),
        returncode=124,
        stdout="",
        stderr="timed out",
        status=CommandStatus.TIMEOUT,
    )
    with patch(
        "agentshore.agents.git_auth_probe.command.git_sync",
        return_value=timed_out,
    ):
        r = probe_git_auth("alice", {"GH_TOKEN": "tok"}, remote=_HTTPS_REMOTE)
    assert r.status == GIT_AUTH_TIMEOUT
    assert r.blocks_launch is False


def test_probe_git_missing_is_nonblocking() -> None:
    missing = CommandResult(
        args=("git",),
        returncode=127,
        stdout="",
        stderr="git not found",
        status=CommandStatus.TOOL_NOT_FOUND,
    )
    with patch(
        "agentshore.agents.git_auth_probe.command.git_sync",
        return_value=missing,
    ):
        r = probe_git_auth("alice", {"GH_TOKEN": "tok"}, remote=_HTTPS_REMOTE)
    assert r.status == GIT_AUTH_ERROR
    assert r.blocks_launch is False


def test_probe_empty_remote_is_unprobeable() -> None:
    r = probe_git_auth("alice", {"GH_TOKEN": "tok"}, remote="")
    assert r.status == GIT_AUTH_UNPROBEABLE
    assert r.blocks_launch is False


def test_ssh_remote_does_not_inject_token_header() -> None:
    with patch(
        "agentshore.agents.git_auth_probe.command.git_sync",
        return_value=_result(0),
    ) as gs:
        probe_git_auth(
            "alice",
            {"GIT_SSH_COMMAND": "ssh -i /key", "GH_TOKEN": "tok"},
            remote=_SSH_REMOTE,
        )
    overlay = gs.call_args.kwargs["env_overlay"]
    # SSH path relies on GIT_SSH_COMMAND, never the token Basic header.
    assert "GIT_CONFIG_COUNT" not in overlay
    assert overlay["GIT_SSH_COMMAND"] == "ssh -i /key"


def test_no_identities_returns_empty() -> None:
    assert probe_all_identities(RuntimeConfig(), project_path=Path(".")) == []


def test_missing_origin_yields_unprobeable_per_identity() -> None:
    cfg = _cfg_with_identity()
    with patch(
        "agentshore.agents.git_auth_probe.resolve_origin_remote",
        return_value=None,
    ):
        results = probe_all_identities(cfg, project_path=Path("."))
    assert len(results) == 1
    assert results[0].status == GIT_AUTH_UNPROBEABLE
    assert results[0].blocks_launch is False


def test_each_identity_probed_independently() -> None:
    ident = GitHubIdentity(git_user_name="X", git_user_email="x@example.com")
    cfg = RuntimeConfig(identities={"alice": ident, "bob": ident})
    with (
        patch(
            "agentshore.agents.identity.resolve_identity_env",
            return_value={"GH_TOKEN": "tok"},
        ),
        patch(
            "agentshore.agents.git_auth_probe.probe_git_auth",
            side_effect=lambda name, env, *, remote: _ok(name),
        ) as probe,
    ):
        results = probe_all_identities(cfg, project_path=Path("."), remote=_HTTPS_REMOTE)
    assert {r.identity_name for r in results} == {"alice", "bob"}
    assert probe.call_count == 2


def test_identity_resolution_failure_is_nonblocking() -> None:
    cfg = _cfg_with_identity()
    with patch(
        "agentshore.agents.identity.resolve_identity_env",
        side_effect=RuntimeError("boom"),
    ):
        results = probe_all_identities(cfg, project_path=Path("."), remote=_HTTPS_REMOTE)
    assert len(results) == 1
    assert results[0].status == GIT_AUTH_UNPROBEABLE
    assert results[0].blocks_launch is False


def test_preflight_blocks_on_auth_failure() -> None:
    cfg = _cfg_with_identity()
    with (
        patch(
            "agentshore.agents.git_auth_probe.probe_all_identities",
            return_value=[_failed()],
        ),
        pytest.raises(SystemExit) as excinfo,
    ):
        preflight_git_auth(cfg, Path("."))
    assert excinfo.value.code == 1


def test_preflight_ok_does_not_exit() -> None:
    cfg = _cfg_with_identity()
    with patch(
        "agentshore.agents.git_auth_probe.probe_all_identities",
        return_value=[_ok()],
    ):
        preflight_git_auth(cfg, Path("."))  # must not raise


def test_preflight_nonblocking_warns_but_does_not_exit() -> None:
    cfg = _cfg_with_identity()
    with patch(
        "agentshore.agents.git_auth_probe.probe_all_identities",
        return_value=[_timeout()],
    ):
        preflight_git_auth(cfg, Path("."))  # timeout is non-blocking — must not raise


def test_preflight_no_identities_is_noop() -> None:
    with patch(
        "agentshore.agents.git_auth_probe.probe_all_identities",
        return_value=[],
    ):
        preflight_git_auth(RuntimeConfig(), Path("."))  # no identities → return cleanly


def _invoke_start(args: list[str]):
    """Drive `agentshore start` up to (and including) the preflight block, then
    bail out via a sentinel raised from the dispatch import so no orchestrator
    machinery runs. Returns the result and the patched git-auth preflight mock.
    """
    from click.testing import CliRunner

    from agentshore.cli.commands import start as start_mod

    runner = CliRunner()
    with (
        patch.object(start_mod, "maybe_re_exec_under_caffeinate"),
        patch.object(start_mod, "bootstrap_session") as mock_bootstrap,
        patch.object(start_mod, "echo_bootstrap_summary"),
        patch.object(start_mod, "preflight_identities"),
        patch.object(start_mod, "preflight_cli_agent_auth"),
        patch.object(start_mod, "preflight_git_auth") as mock_git_auth,
    ):
        # Raise on cfg_path (deref'd just after the preflight block) to stop
        # there; cfg/project_path consumed by the preflights stay clean MagicMocks.
        type(mock_bootstrap.return_value).cfg_path = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("stop-after-preflight"))
        )
        result = runner.invoke(start_mod.start, args, catch_exceptions=True)
    return result, mock_git_auth


def test_skip_git_auth_preflight_flag_bypasses_probe() -> None:
    """--skip-git-auth-preflight must NOT call preflight_git_auth."""
    result, mock_git_auth = _invoke_start(["--skip-git-auth-preflight", "--project", "."])
    assert isinstance(result.exception, RuntimeError)
    mock_git_auth.assert_not_called()


def test_without_skip_flag_calls_git_auth_preflight() -> None:
    """Without the flag, preflight_git_auth IS called once."""
    result, mock_git_auth = _invoke_start(["--project", "."])
    assert isinstance(result.exception, RuntimeError)
    mock_git_auth.assert_called_once()
