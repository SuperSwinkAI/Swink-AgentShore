"""Tests for robust skill result parsing."""

from __future__ import annotations

import pytest

from agentshore.result_parser import find_artifact, normalize_artifact_type, parse_skill_result


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


def test_parse_reports_missing_success_envelope_distinctly() -> None:
    """#229: a balanced JSON object that lacks a top-level boolean ``success`` is a
    near-miss, reported distinctly from a true no-JSON failure (and flagged so the
    resume-retry can pick a defect-specific nudge), while keeping the shared prefix
    the base.py retry trigger keys on.
    """
    # A structurally-complete JSON object (mirrors the live agy design_audit
    # near-miss: prose bucket names serialized in place of the schema keys), but
    # with no top-level boolean ``success``.
    output = '{"artifacts": [{"type": "design_audit"}], "gap_filled": ["Distribution"]}'
    result = parse_skill_result(output)

    assert result.success is False
    assert result.missing_success_envelope is True
    assert result.error is not None
    # Shared prefix preserved so the JSON-retry trigger still fires.
    assert "no valid result block" in result.error
    assert "no top-level boolean 'success' field" in result.error


def test_parse_no_json_at_all_is_not_flagged_missing_success() -> None:
    """Prose / no-JSON output must NOT set the missing-success flag (it's the
    generic no-JSON case, which keeps the original nudge)."""
    result = parse_skill_result("I did the work but forgot the JSON trailer entirely.")

    assert result.success is False
    assert result.missing_success_envelope is False
    assert "no valid result block" in (result.error or "")


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
    # Tail must surface in error so operators can diagnose from the log line alone.
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


def test_parse_verification_failures_surfaced_as_error_when_error_null() -> None:
    """verification_failures synthesised into error when success=False and error=null."""
    output = """
    {
      "success": false,
      "error": null,
      "verification_failures": [
        {
          "type": "conflicting_state_labels",
          "issues": [81, 82, 97],
          "detail": "Open issues carry agentshore/blocked together with priority/* labels."
        },
        "Strict task-bead invariant has non-task bead records for open GH issues: 115, 117."
      ]
    }
    """
    result = parse_skill_result(output)
    assert result.success is False
    assert result.error is not None
    assert "agentshore/blocked together with priority/*" in result.error
    assert "task-bead invariant" in result.error


def test_parse_verification_failures_not_applied_when_error_set() -> None:
    """Explicit error field takes precedence over verification_failures synthesis."""
    output = """
    {
      "success": false,
      "error": "explicit error message",
      "verification_failures": [{"type": "something", "detail": "ignored"}]
    }
    """
    result = parse_skill_result(output)
    assert result.error == "explicit error message"


def test_parse_verification_failures_empty_list_no_synthesis() -> None:
    """Empty verification_failures leaves error as None (no spurious synthesis)."""
    output = '{"success": false, "error": null, "verification_failures": []}'
    result = parse_skill_result(output)
    assert result.error is None


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


# learnings extraction — Bug B


def test_parse_extracts_learnings_list() -> None:
    """Top-level ``learnings`` array is normalized onto SkillResult.learnings."""
    output = """
    {
      "success": true,
      "artifacts": [],
      "learnings": [
        {"pattern": "always run ruff before committing", "confidence": 0.8, "category": "workflow"},
        {"pattern": "prefer asyncio.to_thread for blocking I/O", "confidence": 0.7, "category": "async"}
      ]
    }
    """
    result = parse_skill_result(output)
    assert len(result.learnings) == 2
    assert result.learnings[0] == {
        "pattern": "always run ruff before committing",
        "confidence": 0.8,
        "category": "workflow",
    }
    assert result.learnings[1] == {
        "pattern": "prefer asyncio.to_thread for blocking I/O",
        "confidence": 0.7,
        "category": "async",
    }


def test_parse_learnings_defaults_to_empty() -> None:
    """A result block without a ``learnings`` key yields an empty list, not None."""
    output = '{"success": true, "artifacts": []}'
    result = parse_skill_result(output)
    assert result.learnings == []


def test_parse_learnings_drops_non_dict_entries() -> None:
    """Non-dict items in ``learnings`` are silently dropped."""
    output = """
    {
      "success": true,
      "artifacts": [],
      "learnings": [
        "not a dict",
        42,
        {"pattern": "valid entry", "confidence": 0.6, "category": "general"}
      ]
    }
    """
    result = parse_skill_result(output)
    assert len(result.learnings) == 1
    assert result.learnings[0]["pattern"] == "valid entry"


def test_parse_learnings_drops_entries_without_pattern() -> None:
    """Dict entries missing a ``pattern`` key or with an empty pattern are dropped."""
    output = """
    {
      "success": true,
      "artifacts": [],
      "learnings": [
        {"confidence": 0.5, "category": "general"},
        {"pattern": "", "confidence": 0.5, "category": "general"},
        {"pattern": "keeper", "confidence": 0.5, "category": "general"}
      ]
    }
    """
    result = parse_skill_result(output)
    assert len(result.learnings) == 1
    assert result.learnings[0]["pattern"] == "keeper"


def test_parse_learnings_defaults_confidence_on_bad_value() -> None:
    """A non-numeric confidence falls back to DEFAULT_LEARNING_CONFIDENCE."""
    from agentshore.core.learnings_harvester import DEFAULT_LEARNING_CONFIDENCE

    output = """
    {
      "success": true,
      "artifacts": [],
      "learnings": [
        {"pattern": "test pattern", "confidence": "high", "category": "general"}
      ]
    }
    """
    result = parse_skill_result(output)
    assert len(result.learnings) == 1
    assert result.learnings[0]["confidence"] == DEFAULT_LEARNING_CONFIDENCE


def test_parse_learnings_caps_at_ten() -> None:
    """At most 10 learnings are extracted even if the agent emits more."""
    import json as _json

    learning_entries = [
        {"pattern": f"pattern-{i}", "confidence": 0.5, "category": "general"} for i in range(15)
    ]
    output = _json.dumps({"success": True, "artifacts": [], "learnings": learning_entries})
    result = parse_skill_result(output)
    assert len(result.learnings) == 10


def test_parse_learnings_non_list_treated_as_empty() -> None:
    """A ``learnings`` value that is not a list is safely ignored."""
    output = '{"success": true, "artifacts": [], "learnings": "not a list"}'
    result = parse_skill_result(output)
    assert result.learnings == []


# learnings_compacted (groom re-distillation, wholesale replace)


def test_parse_learnings_compacted_normalizes_entries() -> None:
    """``learnings_compacted`` items normalize to {pattern, category, merged_from};
    the agent's confidence is ignored, merged_from preserved."""
    import json as _json

    output = _json.dumps(
        {
            "success": True,
            "artifacts": [],
            "learnings_compacted": [
                {
                    "pattern": "merged insight",
                    "category": "conventions",
                    "confidence": 0.99,  # ignored
                    "merged_from": ["id-a", "id-b"],
                }
            ],
        }
    )
    result = parse_skill_result(output)
    assert len(result.learnings_compacted) == 1
    entry = result.learnings_compacted[0]
    assert entry == {
        "pattern": "merged insight",
        "category": "conventions",
        "merged_from": ["id-a", "id-b"],
    }
    assert "confidence" not in entry


def test_parse_learnings_compacted_defaults_and_drops() -> None:
    """Missing category defaults to 'general'; non-str merged_from ids and
    pattern-less / non-dict entries are dropped; merged_from defaults to []."""
    import json as _json

    output = _json.dumps(
        {
            "success": True,
            "artifacts": [],
            "learnings_compacted": [
                "not a dict",
                {"category": "x"},  # no pattern
                {"pattern": "", "merged_from": ["a"]},  # empty pattern
                {"pattern": "keeper", "merged_from": ["ok", 7, None]},
                {"pattern": "no-merge"},  # merged_from absent
            ],
        }
    )
    result = parse_skill_result(output)
    assert len(result.learnings_compacted) == 2
    assert result.learnings_compacted[0] == {
        "pattern": "keeper",
        "category": "general",
        "merged_from": ["ok"],
    }
    assert result.learnings_compacted[1] == {
        "pattern": "no-merge",
        "category": "general",
        "merged_from": [],
    }


def test_parse_learnings_compacted_defaults_to_empty() -> None:
    """No ``learnings_compacted`` key yields an empty list, not None."""
    result = parse_skill_result('{"success": true, "artifacts": []}')
    assert result.learnings_compacted == []


# --- artifact `type` folding (#313) -------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("design_audit", "design_audit"),
        ("design-audit-result", "design_audit"),
        ("Design-Audit-Result", "design_audit"),
        ("  design audit  ", "design_audit"),
        ("design__audit", "design_audit"),
        ("design_audit_results", "design_audit"),
        ("seed-audit", "seed_audit"),
        # Folding is narrow on purpose: only case, separators, and ONE trailing
        # _result/_results. Nothing else is rewritten.
        ("pull_request", "pull_request"),
        ("design_audit_summary", "design_audit_summary"),
        ("design_audit_result_result", "design_audit_result"),
        # A bare "result"/"results" type folds to itself — stripping the suffix
        # would leave an empty string, which is never a type.
        ("result", "result"),
        # Non-strings and empties have no canonical form.
        ("", None),
        ("   ", None),
        ("---", None),
        (None, None),
        (42, None),
        ({"type": "design_audit"}, None),
    ],
)
def test_normalize_artifact_type(raw: object, expected: str | None) -> None:
    assert normalize_artifact_type(raw) == expected


def test_find_artifact_matches_exactly() -> None:
    artifact = {"type": "design_audit", "gaps_found": 0}
    assert find_artifact([artifact], "design_audit") is artifact


def test_find_artifact_falls_back_to_folded_match() -> None:
    """#313: a complete payload under a near-miss spelling is still found."""
    artifact = {"type": "design-audit-result", "gaps_found": 3}
    assert find_artifact([artifact], "design_audit") is artifact


def test_find_artifact_prefers_exact_over_folded() -> None:
    """Exact match wins, so folding can only add a match — never redirect one."""
    exact = {"type": "design_audit", "which": "exact"}
    folded = {"type": "design-audit-result", "which": "folded"}
    assert find_artifact([folded, exact], "design_audit") is exact


def test_find_artifact_returns_none_for_unrelated_types() -> None:
    artifacts = [{"type": "seed_audit"}, {"type": "pull_request"}, "design_audit"]
    assert find_artifact(artifacts, "design_audit") is None


def test_find_artifact_ignores_artifacts_without_a_usable_type() -> None:
    assert find_artifact([{"no_type": 1}, {"type": None}, {"type": ""}], "design_audit") is None
