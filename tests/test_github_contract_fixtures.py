"""Parser contracts backed by sanitized responses captured from GitHub/gh."""

from __future__ import annotations

import json
from pathlib import Path

from agentshore.github.adapter import _issue_record_from_json, _pr_record_from_json

FIXTURES = Path(__file__).parent / "fixtures" / "github"


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_rest_issue_capture_maps_to_record_and_filters_pull_requests() -> None:
    payload = _fixture("issues-rest.json")
    assert isinstance(payload, list)

    issue = _issue_record_from_json("fixture-session", payload[0])
    pull_request = _issue_record_from_json("fixture-session", payload[1])

    assert issue is not None
    assert issue.issue_number == 416
    assert issue.title == "Captured contract issue"
    assert issue.state == "closed"
    assert issue.labels == ["bug", "priority:medium"]
    assert issue.github_author == "fixture-user"
    assert pull_request is None


def test_gh_pr_list_capture_maps_to_cache_record() -> None:
    payload = _fixture("pr-list.json")
    assert isinstance(payload, list)

    record = _pr_record_from_json("fixture-session", payload[0])

    assert record is not None
    assert record.pr_number == 433
    assert record.state == "merged"
    assert record.github_author == "fixture-bot"
    assert record.labels == ["dependencies", "python:uv"]
    assert record.status_check_summary == "passed"


def test_gh_pr_view_capture_preserves_linked_issue_contract() -> None:
    payload = _fixture("pr-view.json")
    assert isinstance(payload, dict)

    record = _pr_record_from_json("fixture-session", payload)

    assert record is not None
    assert record.pr_number == 417
    assert record.issue_number == 415
    assert record.linked_issue_numbers == (415, 416)
    assert record.branch == "fix/415-swink-empty-result-deltas"
    assert record.base_ref == "integration"
    assert record.status_check_summary == "passed"
