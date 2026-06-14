"""Static guardrails for the trunk-scoped planning skill templates.

``write-plan`` and ``refine-tasks`` run their agent in the **main checkout**
(``TrunkAllocation``). Their contract is read-only-git + update GitHub/beads —
never create/switch a branch, which would move the main checkout's HEAD and
wedge the orchestrator (the root cause behind the #175 wedge). These tests pin
that contract into the canonical templates so a future edit can't silently
re-open the door (the gap that caused the contamination: ``refine-tasks`` had
no branch prohibition and a broad ``git:*`` grant).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "agentshore" / "skills" / "templates"

# Trunk-scoped planning plays that must never touch the working tree / move HEAD.
_GUARDED_SKILLS = ("agentshore-write-plan", "agentshore-refine-tasks")


def _template(name: str) -> str:
    return (_TEMPLATES_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter(text: str) -> str:
    assert text.startswith("---\n"), "template must start with YAML frontmatter"
    return text.split("---\n", 2)[1]


@pytest.mark.parametrize("name", _GUARDED_SKILLS)
def test_allowed_tools_grant_read_only_git(name: str) -> None:
    """The broad ``git:*`` grant is gone — only read-only git subcommands remain.

    ``allowed-tools`` mechanically enforces this for Claude Code; combined with
    the prose guard below it covers agents that ignore ``allowed-tools``.
    """
    head = _frontmatter(_template(name))
    allowed_line = next((ln for ln in head.splitlines() if ln.startswith("allowed-tools:")), None)
    assert allowed_line is not None, f"{name}: no allowed-tools line"
    assert "git:*" not in allowed_line, (
        f"{name}: broad Bash(git:*) grant must be removed — it lets the agent "
        "run git checkout/switch/branch in the main checkout"
    )
    # Read-only git the planning plays legitimately use must still be granted.
    assert "git fetch:*" in allowed_line
    assert "git grep:*" in allowed_line
    # Branch-moving subcommands must NOT be allow-listed.
    for forbidden in ("git checkout:*", "git switch:*", "git branch:*", "git worktree:*"):
        assert forbidden not in allowed_line, f"{name}: {forbidden} must not be allow-listed"


@pytest.mark.parametrize("name", _GUARDED_SKILLS)
def test_prose_forbids_branch_mutation(name: str) -> None:
    """The body must explicitly forbid branch creation/switching in the main checkout."""
    text = _template(name)
    lowered = text.lower()
    assert "git switch -c" in lowered or "git checkout -b" in lowered, (
        f"{name}: must name the branch-creation commands it forbids"
    )
    assert "main checkout" in lowered, f"{name}: must explain it runs in the main checkout"
    # The mechanism it must call out: a branch op here moves the main HEAD.
    assert "head" in lowered and "read-only" in lowered, (
        f"{name}: must state git is read-only and a branch op moves the main HEAD"
    )
