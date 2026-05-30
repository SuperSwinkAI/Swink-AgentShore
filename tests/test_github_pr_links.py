"""Tests for pull-request to issue-link inference."""

from __future__ import annotations

from types import SimpleNamespace

from agentshore.github.pr_links import infer_pr_issue_links, issue_numbers_for_pr


def test_infer_pr_issue_links_dedupes_explicit_and_linked_values() -> None:
    links = infer_pr_issue_links(issue_number="#12", linked_issue_numbers=[12, "13", 0, True])

    assert links.issue_numbers == (12, 13)
    assert links.primary_issue_number == 12
    assert links.provenance[12] == ("explicit_field", "linked_issue_numbers")


def test_infer_pr_issue_links_reads_github_closing_references() -> None:
    links = infer_pr_issue_links(
        closing_issue_references=[
            {"number": 109},
            {"issue": {"number": "110"}},
            {"url": "https://github.com/acme/repo/issues/111"},
        ]
    )

    assert links.issue_numbers == (109, 110, 111)
    assert links.provenance[109] == ("github_closing_reference",)


def test_infer_pr_issue_links_reads_multi_issue_closing_body_lines() -> None:
    links = infer_pr_issue_links(
        body="\n".join(
            [
                "Related: #50",
                "Closes #109, #110, and fixes #111",
                "This is just discussion of #112.",
            ]
        )
    )

    assert links.issue_numbers == (109, 110, 111)


def test_infer_pr_issue_links_reads_agentshore_branch_prefix() -> None:
    links = infer_pr_issue_links(branch="codex/agentshore/109-fix-cache-loop")

    assert links.issue_numbers == (109,)
    assert links.provenance[109] == ("agentshore_branch_prefix",)


def test_issue_numbers_for_pr_combines_primary_and_link_tuple() -> None:
    pr = SimpleNamespace(issue_number=109, linked_issue_numbers=(109, 110))

    assert issue_numbers_for_pr(pr) == (109, 110)
