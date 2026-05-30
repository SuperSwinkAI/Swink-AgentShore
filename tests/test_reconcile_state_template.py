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


def test_forbidden_worktree_lifecycle_mutations(template_text: str) -> None:
    """AgentShore owns worktree lifecycle; only read-only ``git worktree list`` allowed.

    Accepts either the comma-separated form or the ``add/remove/prune`` slash form —
    the intent (all three forbidden in one clause) is what matters.
    """
    long_form = re.search(
        r"`git worktree add`.*`git worktree remove`.*`git worktree prune`",
        template_text,
        re.DOTALL,
    )
    short_form = re.search(r"`?git worktree add/remove/prune`?", template_text)
    assert long_form or short_form, (
        "reconcile-state: missing the forbidden git worktree add/remove/prune clause"
    )


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
    # Sanity: structure looks right.
    assert payload["success"] is True
    assert "remediation" in payload
    assert "verification_evidence" in payload
    # Round-trip through the parser. parse_skill_result accepts the raw
    # text and extracts the trailing block — feed it a minimal wrapper.
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
