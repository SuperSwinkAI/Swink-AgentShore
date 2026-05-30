"""Tests for robust skill result parsing."""

from __future__ import annotations

from agentshore.result_parser import parse_skill_result


def test_parse_pretty_printed_result_with_object_issue_refs() -> None:
    output = """
    Agent notes before the final result.

    ```json
    {
      "schema_version": 1,
      "success": true,
      "artifacts": [
        {"type": "pull_request", "number": 47, "url": "https://github.com/o/r/pull/47"}
      ],
      "issues_created": [
        {"number": 53, "title": "Follow-up", "url": "https://github.com/o/r/issues/53"}
      ],
      "error": null
    }
    ```
    """

    result = parse_skill_result(output)

    assert result.success is True
    assert result.error is None
    assert result.artifacts == [
        {"type": "pull_request", "number": 47, "url": "https://github.com/o/r/pull/47"}
    ]
    assert result.issues_created == [
        {"number": 53, "title": "Follow-up", "url": "https://github.com/o/r/issues/53"}
    ]


def test_parse_uses_last_valid_result_block() -> None:
    output = """
    Example:
    {"success": false, "artifacts": [], "issues_created": [], "error": "example"}

    Final:
    {
      "success": true,
      "artifacts": ["PR #47"],
      "issues_created": [53],
      "error": null
    }
    """

    result = parse_skill_result(output)

    assert result.success is True
    assert result.artifacts == ["PR #47"]
    assert result.issues_created == [53]
    assert result.error is None


def test_parse_reports_missing_result_block() -> None:
    """desktop-zzt: error now includes a tail/length diagnostic to distinguish
    'agent crashed' from 'agent ran fine but skipped the JSON contract'.
    """
    result = parse_skill_result("no json here")

    assert result.success is False
    assert result.error is not None
    assert "no valid result block" in result.error
    # Short output (< 100 chars) is reported as "only N chars"
    assert "only 12 chars" in result.error
    assert "no json here" in result.error


def test_parse_reports_empty_agent_output() -> None:
    """Zero-byte output gets a distinct diagnostic from short-but-non-empty."""
    result = parse_skill_result("")

    assert result.success is False
    assert result.error is not None
    assert "agent produced no output" in result.error


def test_parse_reports_long_output_with_no_json() -> None:
    """desktop-zzt regression: agents that emit a lot of text without the
    JSON result block produce a 'tail:' diagnostic so the operator can see
    what the agent said instead of complying with the contract.
    """
    output = (
        "I considered the issue and decided to ask a clarification first. "
        "How would you like me to proceed with the uv.lock change?\n"
        "1. Include uv.lock in this branch.\n"
        "2. Discard uv.lock and continue.\n"
        "3. Inspect the uv.lock diff first.\n"
    ) * 3  # >100 chars
    result = parse_skill_result(output)

    assert result.success is False
    assert result.error is not None
    assert f"{len(output)} chars but no JSON result block" in result.error
    assert "tail:" in result.error
    # Tail of the agent output should appear in the error so operators can
    # diagnose the failure mode from the play_completed log line alone.
    assert "Inspect the uv.lock diff first" in result.error


def test_parse_extracts_requested_mutations() -> None:
    output = """
    {
      "success": true,
      "artifacts": [],
      "issues_created": [],
      "requested_mutations": [
        {"type": "merge_pr", "pr_number": 42},
        {"type": "close_issue", "issue_number": 17}
      ],
      "error": null
    }
    """
    result = parse_skill_result(output)
    assert result.success is True
    assert len(result.requested_mutations) == 2
    assert result.requested_mutations[0] == {"type": "merge_pr", "pr_number": 42}
    assert result.requested_mutations[1] == {"type": "close_issue", "issue_number": 17}


def test_parse_defaults_requested_mutations_to_empty() -> None:
    output = '{"success": true, "artifacts": [], "issues_created": []}'
    result = parse_skill_result(output)
    assert result.requested_mutations == []


def test_parse_extracts_spec_compliance_and_blocking_findings() -> None:
    """code_review skill returns spec_compliance + findings_count.blocking;
    these must surface as fields on SkillResult so the play can derive a
    verdict for merge_pr gating."""
    output = """
    {
      "success": true,
      "artifacts": [{"type": "pr", "number": 42, "head_sha": "abc"}],
      "issues_created": [],
      "spec_compliance": "PASS",
      "findings_count": {"blocking": 0, "non_blocking": 2},
      "error": null
    }
    """
    result = parse_skill_result(output)
    assert result.spec_compliance == "PASS"
    assert result.blocking_findings == 0


def test_parse_extracts_block_with_blocking_findings() -> None:
    output = """
    {
      "success": true,
      "artifacts": [],
      "issues_created": [],
      "spec_compliance": "BLOCK",
      "findings_count": {"blocking": 3, "non_blocking": 1}
    }
    """
    result = parse_skill_result(output)
    assert result.spec_compliance == "BLOCK"
    assert result.blocking_findings == 3


def test_parse_handles_missing_review_fields() -> None:
    """Skills other than code_review don't emit these fields; defaults stay
    None so the verdict mapping treats them as 'no fresh verdict'."""
    output = '{"success": true, "artifacts": [], "issues_created": []}'
    result = parse_skill_result(output)
    assert result.spec_compliance is None
    assert result.blocking_findings is None


def test_parse_skip_verdict_extracted() -> None:
    output = """
    {
      "success": true,
      "artifacts": [],
      "issues_created": [],
      "spec_compliance": "SKIP",
      "error": "zero-diff PR; no review needed"
    }
    """
    result = parse_skill_result(output)
    assert result.spec_compliance == "SKIP"
    assert result.blocking_findings is None


def test_parse_extracts_prior_verdict_on_dedup_skip() -> None:
    """When the agentshore-code-review skill dedup-skips, it surfaces the
    verdict from the existing AGENTSHORE_CODE_REVIEW comment so the play can
    backfill last_review_status instead of leaving the column NULL."""
    output = """
    {
      "success": true,
      "verdict": "SKIP",
      "head_sha": "abc",
      "artifacts": [{"type": "pr", "number": 42, "head_sha": "abc"}],
      "issues_created": [],
      "prior_verdict": "PASS",
      "prior_findings_count": {"blocking": 0},
      "error": "already reviewed at abc"
    }
    """
    result = parse_skill_result(output)
    assert result.prior_verdict == "PASS"
    assert result.prior_blocking_findings == 0


def test_parse_extracts_prior_block_with_findings() -> None:
    output = """
    {
      "success": true,
      "verdict": "SKIP",
      "artifacts": [],
      "issues_created": [],
      "prior_verdict": "BLOCK",
      "prior_findings_count": {"blocking": 3, "non_blocking": 1},
      "error": "already reviewed at def"
    }
    """
    result = parse_skill_result(output)
    assert result.prior_verdict == "BLOCK"
    assert result.prior_blocking_findings == 3


def test_parse_handles_missing_prior_verdict_fields() -> None:
    """A non-dedup skill response (or a dedup that couldn't parse the prior
    comment) must leave prior_verdict / prior_blocking_findings as None."""
    output = '{"success": true, "artifacts": [], "issues_created": []}'
    result = parse_skill_result(output)
    assert result.prior_verdict is None
    assert result.prior_blocking_findings is None


def test_parse_extracts_issues_closed_from_merge_pr() -> None:
    """agentshore-merge-pr emits a top-level ``issues_closed`` list of issue
    numbers referenced by Closes/Fixes/Resolves on the merged PR. The merge_pr
    play uses this to write through closed-state to the SQLite cache so the
    dashboard's DONE column populates promptly."""
    output = """
    {
      "success": true,
      "artifacts": [{"type": "merge", "pr": 42, "merge_method": "squash"}],
      "issues_created": [],
      "issues_closed": [17, 23],
      "error": null
    }
    """
    result = parse_skill_result(output)
    assert result.issues_closed == [17, 23]


def test_parse_issues_closed_coerces_strings() -> None:
    """Numeric strings are coerced to int; non-numeric values are dropped."""
    output = """
    {
      "success": true,
      "artifacts": [],
      "issues_closed": [10, "20", "not-a-number", 30]
    }
    """
    result = parse_skill_result(output)
    assert result.issues_closed == [10, 20, 30]


def test_parse_issues_closed_default_empty() -> None:
    """Skills that don't emit ``issues_closed`` (everything except merge_pr)
    leave the list empty rather than None."""
    output = '{"success": true, "artifacts": []}'
    result = parse_skill_result(output)
    assert result.issues_closed == []


def test_parse_issues_closed_rejects_non_list() -> None:
    """If ``issues_closed`` is malformed (e.g. a string instead of a list),
    fall back to empty rather than crashing the parser."""
    output = '{"success": true, "artifacts": [], "issues_closed": "17,23"}'
    result = parse_skill_result(output)
    assert result.issues_closed == []


def test_parse_issue_pickup_publish_reconciliation_fields() -> None:
    output = """
    {
      "success": false,
      "artifacts": [],
      "issues_created": [],
      "issue_picked_up": "225",
      "branch": "agentshore/225-fix-auth",
      "tests_passed": true,
      "verification_evidence": [
        {"command": "pytest tests/test_auth.py -v", "exit_code": 0, "summary": "passed"}
      ],
      "error": "gh pr create failed: HTTP 401 Bad credentials"
    }
    """
    result = parse_skill_result(output)
    assert result.issue_picked_up == 225
    assert result.branch == "agentshore/225-fix-auth"
    assert result.tests_passed is True
    assert result.verification_evidence == [
        {"command": "pytest tests/test_auth.py -v", "exit_code": 0, "summary": "passed"}
    ]


def test_parse_extracts_review_patterns() -> None:
    output = """
    {
      "success": true,
      "artifacts": [],
      "review_patterns": [
        {"pattern": "missing regression test", "category": "testing", "frequency": 2},
        {"pattern": "tighten type annotations", "category": "typing"}
      ]
    }
    """
    result = parse_skill_result(output)
    assert result.review_patterns == [
        {"pattern": "missing regression test", "category": "testing", "frequency": 2},
        {"pattern": "tighten type annotations", "category": "typing"},
    ]
