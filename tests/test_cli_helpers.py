"""Tests for CLI helper functions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from agentshore.cli_helpers import _detect_gh_remote


def test_detect_gh_remote_runs_in_requested_directory(tmp_path: Path) -> None:
    completed = MagicMock()
    completed.stdout = '{"url":"https://github.com/o/r","nameWithOwner":"o/r"}'

    with (
        patch("agentshore.cli_helpers.resolve_executable", return_value="/usr/bin/gh"),
        patch("agentshore.cli_helpers.subprocess.run", return_value=completed) as mock_run,
    ):
        result = _detect_gh_remote(tmp_path)

    assert result == {"url": "https://github.com/o/r", "nameWithOwner": "o/r"}
    assert mock_run.call_args.args[0][0] == "/usr/bin/gh"
    assert mock_run.call_args.kwargs["cwd"] == tmp_path
