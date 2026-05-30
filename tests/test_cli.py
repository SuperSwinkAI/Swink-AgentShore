"""Smoke tests for CLI entry points."""

from __future__ import annotations

from click.testing import CliRunner

from agentshore.cli import main


def test_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "AgentShore" in result.output


def test_cli_version() -> None:
    from agentshore import __version__

    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output
