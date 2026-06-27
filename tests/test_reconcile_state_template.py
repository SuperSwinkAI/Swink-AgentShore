"""Static checks against the agentshore-reconcile-state skill template.

Guards the canonical safety contract: the template must enumerate every
forbidden mutation we rely on the skill never performing (no GitHub
mutations against the target project, no git stash, no git worktree
add/remove/prune, no CI-config touches), and the result block must be
parseable by ``agentshore.result_parser`` so a successful dispatch isn't
recorded as a failure.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentshore.result_parser import parse_skill_result

_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agentshore"
    / "skills"
    / "templates"
    / "agentshore-reconcile-state"
    / "SKILL.md"
)


@pytest.fixture(scope="module")
def template_text() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def test_template_exists() -> None:
    assert _TEMPLATE_PATH.is_file()


def test_frontmatter_metadata(template_text: str) -> None:
    """Frontmatter must name the skill, disable model invocation, and declare tools."""
    assert template_text.startswith("---\n")
    head = template_text.split("---\n", 2)[1]
    assert "name: agentshore-reconcile-state" in head
    assert "disable-model-invocation: true" in head
    assert "allowed-tools:" in head


def test_forbidden_github_mutations(template_text: str) -> None:
    """No GitHub mutations at all — fully local diagnosis + remediation."""
    assert "Never `git push`" in template_text
    assert "gh pr" in template_text  # appears in the forbidden list
    # The skill no longer files upstream telemetry issues — no outbound gh calls.
    assert "gh issue create" not in template_text
    assert "SuperSwinkAI/Swink-AgentShore" not in template_text


def test_forbidden_no_git_stash(template_text: str) -> None:
    """Per ``feedback_no_git_stash`` — stash entries leak across sessions."""
    assert "Never `git stash`" in template_text


def test_forbidden_worktree_add(template_text: str) -> None:
    """The skill never creates worktrees — ``git worktree add`` stays forbidden.

    ``git worktree remove --force`` + ``prune`` are now permitted, but ONLY for
    orphan-worktree remediation (#33): a registered worktree with no active
    session row otherwise leaves a leak that DB-only remediation can't clear and
    that fails Verify forever. The add ban is what remains load-bearing.
    """
    assert re.search(r"Never `git worktree add`", template_text), (
        "reconcile-state: missing the forbidden `git worktree add` clause"
    )


def test_orphan_remediation_removes_registration(template_text: str) -> None:
    """Orphan remediation must actually de-register the worktree, not just mark stale.

    A git-registered orphan can only be cleared with ``git worktree remove
    --force`` + ``git worktree prune``; a DB UPDATE alone leaves the
    registration and Verify can never pass (#33).
    """
    assert "git worktree remove --force" in template_text
    assert "git worktree prune" in template_text
    # The old DB-only-and-defer instruction must be gone.
    assert "do NOT** call `git worktree remove`" not in template_text
    assert "worktree remove --force:*" in template_text  # allow-listed in frontmatter


def test_worktree_removal_age_and_claim_guard(template_text: str) -> None:
    """Destructive worktree ops must honour the young/active guards (#218).

    reconcile diagnoses then removes minutes later, so a worktree allocated to a
    freshly dispatched agent in that gap must be protected: by age
    (``young_worktree_paths`` / ``worktree_min_age_hours``), by live claim
    (``active_worktree_paths``), and by a re-read of the claim set at remediation.
    """
    assert "young_worktree_paths" in template_text
    assert "active_worktree_paths" in template_text
    assert "worktree_min_age_hours" in template_text
    assert "Worktree-removal safety guard" in template_text
    # The guard must be re-validated at remediation time, not only at diagnosis.
    assert "re-read the live claim set" in template_text


def test_forbidden_ci_configs(template_text: str) -> None:
    """Per ``project_skill_templates_no_ci_workflows`` memory."""
    assert ".github/workflows/**" in template_text
    assert ".gitlab-ci.yml" in template_text
    assert ".circleci/**" in template_text


def test_result_block_parses_with_agentshore_result_parser(template_text: str) -> None:
    """The example JSON in the Result section must round-trip through the parser.

    Otherwise a successful dispatch by the agent would be recorded as a
    failed play with 'no valid result block' (the contract the skill
    template explicitly warns about in its closing paragraph).
    """
    fences = re.findall(r"```json\n(.*?)\n```", template_text, flags=re.DOTALL)
    assert fences, "no fenced JSON block found in the skill template"
    payload = json.loads(fences[-1])
    assert payload["success"] is True
    assert "remediation" in payload
    assert "verification_evidence" in payload
    # Wrap in trailing-block form so the parser's tail extraction is exercised.
    wrapped = "diagnostic output …\n\n```json\n" + json.dumps(payload) + "\n```\n"
    result = parse_skill_result(wrapped)
    assert result is not None
    assert result.success is True


def test_step_structure_matches_design(template_text: str) -> None:
    """The diagnose → remediate → verify flow is present.

    Section concepts must appear (in any heading/bold style). After skill-template
    compression, sections may be bold inline labels rather than ``##`` headers and
    the Forbidden block may move to wherever reads best — what's load-bearing is
    that each concept is present, not the exact prose ordering.

    The diagnose → remediate → verify subset, however, IS load-bearing in order:
    you can't remediate before diagnosing or verify before remediating.
    """
    # Each entry: (display_name, regex matching either a heading or **bold** lead-in).
    concepts = (
        ("Forbidden", r"(?:#+\s*Forbidden|\*\*Forbidden)"),
        ("Pre-flight", r"(?:#+\s*(?:Step\s*1[^A-Za-z]*)?Pre-flight|\*\*Pre-flight)"),
        ("Diagnose", r"(?:#+\s*(?:Step\s*2[^A-Za-z]*)?Diagnose|\*\*Diagnose)"),
        ("Remediate", r"(?:#+\s*(?:Step\s*3[^A-Za-z]*)?Remediate|\*\*Remediate)"),
        ("Verify", r"(?:#+\s*(?:Step\s*4[^A-Za-z]*)?Verify|\*\*Verify)"),
        # Renamed from "Optional follow-up" when upstream issue-filing was
        # removed — the section now just surfaces unrecognized pathologies.
        ("Unrecognized", r"\*\*Unrecognized pattern"),
        ("Result", r"(?:#+\s*Result|\*\*(?:Report|Result))"),
    )
    positions: dict[str, int] = {}
    for name, pattern in concepts:
        match = re.search(pattern, template_text)
        assert match is not None, f"missing concept: {name}"
        positions[name] = match.start()
    # The flow ordering that genuinely matters.
    for earlier, later in (
        ("Pre-flight", "Diagnose"),
        ("Diagnose", "Remediate"),
        ("Remediate", "Verify"),
        ("Verify", "Result"),
    ):
        assert positions[earlier] < positions[later], (
            f"out-of-order: {earlier} must precede {later}"
        )
