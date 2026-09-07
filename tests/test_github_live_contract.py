"""Opt-in, read-only schema check against the current GitHub repository."""

from __future__ import annotations

import json
import os

import pytest

from agentshore.github.adapter import (
    _PR_JSON_FIELDS,
    _issue_record_from_json,
    _pr_record_from_json,
    _run_gh,
)

pytestmark = [
    pytest.mark.github_contract,
    pytest.mark.skipif(
        os.environ.get("AGENTSHORE_GITHUB_CONTRACT") != "1",
        reason="set AGENTSHORE_GITHUB_CONTRACT=1 for live read-only GitHub checks",
    ),
]


@pytest.mark.asyncio
async def test_live_github_rest_and_cli_shapes_match_parsers() -> None:
    issue_rc, issue_stdout, issue_stderr = await _run_gh(
        ["api", "repos/{owner}/{repo}/issues?state=all&per_page=1&page=1"],
        timeout=30,
    )
    assert issue_rc == 0, issue_stderr
    issues = json.loads(issue_stdout)
    assert isinstance(issues, list) and issues
    assert {"number", "title", "state", "labels", "created_at", "html_url"} <= issues[0].keys()
    issue_record = _issue_record_from_json("live-contract", issues[0])
    assert issue_record is not None or "pull_request" in issues[0]

    pr_rc, pr_stdout, pr_stderr = await _run_gh(
        [
            "pr",
            "list",
            "--state",
            "all",
            "--json",
            _PR_JSON_FIELDS,
            "--limit",
            "1",
        ],
        timeout=30,
    )
    assert pr_rc == 0, pr_stderr
    pull_requests = json.loads(pr_stdout)
    assert isinstance(pull_requests, list) and pull_requests
    assert _pr_record_from_json("live-contract", pull_requests[0]) is not None
