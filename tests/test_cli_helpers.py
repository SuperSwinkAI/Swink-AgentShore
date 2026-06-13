"""Tests for CLI helper functions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agentshore.cli_helpers import _detect_gh_remote
from agentshore.command import CommandResult, CommandStatus


def test_detect_gh_remote_runs_in_requested_directory(tmp_path: Path) -> None:
    result = CommandResult(
        args=("gh", "repo", "view"),
        returncode=0,
        stdout='{"url":"https://github.com/o/r","nameWithOwner":"o/r"}',
        stderr="",
        status=CommandStatus.OK,
    )

    with patch("agentshore.cli_helpers.command.gh_sync", return_value=result) as mock_gh:
        detected = _detect_gh_remote(tmp_path)

    assert detected == {"url": "https://github.com/o/r", "nameWithOwner": "o/r"}
    # The remote is detected by running `gh repo view` in the requested directory.
    assert mock_gh.call_args.args[:2] == ("repo", "view")
    assert mock_gh.call_args.kwargs["cwd"] == tmp_path
