"""Tests for skill dispatch primitives: render_skill_prompt and write_play_context."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agentshore.plays.base import PlayParams
from agentshore.plays.dispatch import (
    params_to_json_safe_dict,
    play_context_relative_path,
    render_skill_prompt,
    serialize_state_for_skill,
    write_play_context,
)
from agentshore.state import IssueSnapshot, PlayType, PullRequestSnapshot

# ---------------------------------------------------------------------------
# render_skill_prompt — argument handling (with auto-reinstall from bundled)
#
# Note: as of the auto-reinstall fallback, render_skill_prompt no longer
# returns a literal slash-command string. The function now always embeds
# the SKILL.md body. These tests pass a real project_path and rely on the
# bundled templates being copied in by the auto-reinstall path.
# ---------------------------------------------------------------------------


async def test_render_prompt_with_issue_number(tmp_path: Path) -> None:
    params = PlayParams(issue_number=42)
    result = await render_skill_prompt("agentshore-issue-pickup", params, project_path=tmp_path)
    assert result.startswith("$ARGUMENTS: 42")
    assert "## AgentShore Context Discipline" in result
    assert "`.agentshore/context.json`" in result
    assert "Do not unset" in result or "Do not unset them" in result
    assert "gh api user --jq .login" in result
    # The completion contract bookends the prompt: terminal reminder is the last thing
    # the agent reads, after the skill body. See _COMPLETION_CONTRACT_TEMPLATE.
    assert result.rstrip().endswith("including any PR you opened.")
    assert "## Before you stop — required" in result


async def test_render_prompt_with_pr_number(tmp_path: Path) -> None:
    params = PlayParams(pr_number=87)
    result = await render_skill_prompt("agentshore-code-review", params, project_path=tmp_path)
    assert result.startswith("$ARGUMENTS: 87")


async def test_render_prompt_unblock_pr_pr_number(tmp_path: Path) -> None:
    params = PlayParams(pr_number=87)
    result = await render_skill_prompt("agentshore-unblock-pr", params, project_path=tmp_path)
    assert result.startswith("$ARGUMENTS: 87")


async def test_render_prompt_no_args_emits_none_marker(tmp_path: Path) -> None:
    params = PlayParams()
    result = await render_skill_prompt(
        "agentshore-calibrate-alignment", params, project_path=tmp_path
    )
    assert result.startswith("$ARGUMENTS: (none)")


async def test_render_prompt_seed_project_seed_path_arg(tmp_path: Path) -> None:
    params = PlayParams(seed_path="/some/path")
    result = await render_skill_prompt("agentshore-seed-project", params, project_path=tmp_path)
    assert result.startswith("$ARGUMENTS: /some/path")


async def test_render_prompt_branch_arg(tmp_path: Path) -> None:
    params = PlayParams(branch="feature/my-branch")
    result = await render_skill_prompt("agentshore-run-qa", params, project_path=tmp_path)
    assert result.startswith("$ARGUMENTS: feature/my-branch")


async def test_render_prompt_systematic_debugging_issue_and_branch_args(tmp_path: Path) -> None:
    params = PlayParams(issue_number=17, branch="feature/my-branch")
    result = await render_skill_prompt(
        "agentshore-systematic-debugging", params, project_path=tmp_path
    )
    assert result.startswith("$ARGUMENTS: 17 feature/my-branch")


async def test_render_prompt_quotes_arg_with_spaces(tmp_path: Path) -> None:
    params = PlayParams(branch="main branch")
    result = await render_skill_prompt("agentshore-run-qa", params, project_path=tmp_path)
    assert '"main branch"' in result


async def test_render_prompt_points_to_play_specific_context(tmp_path: Path) -> None:
    params = PlayParams(issue_number=42)
    result = await render_skill_prompt(
        "agentshore-write-plan",
        params,
        project_path=tmp_path,
        context_path=".agentshore/contexts/play-7.json",
    )

    assert "`.agentshore/contexts/play-7.json` immediately before this play" in result
    assert "`assigned_github_identity`" in result
    assert "lowercasing/casefolding both strings" in result
    assert "legacy `.agentshore/context.json` file is only a latest-context/debug copy" in result


async def test_render_prompt_injects_worktree_cwd_block(tmp_path: Path) -> None:
    """When a dispatch cwd is supplied, the preamble names the worktree so the
    agent stops guessing absolute/stale paths (the P1 cwd-contract fix)."""
    params = PlayParams(issue_number=42)
    cwd = "/tmp/agentshore-worktrees/issue-42"
    result = await render_skill_prompt(
        "agentshore-issue-pickup",
        params,
        project_path=tmp_path,
        dispatch_cwd=cwd,
    )

    assert "Your working directory" in result
    assert cwd in result
    assert "worktree reclaimed mid-play" in result
    # The cwd block sits inside the discipline preamble, ahead of the context line.
    assert result.index("Your working directory") < result.index("immediately before this play")


async def test_render_prompt_omits_cwd_block_when_no_allocation(tmp_path: Path) -> None:
    """Legacy / internal plays with no worktree allocation get no cwd block (and
    no leftover ``{cwd_block}`` placeholder)."""
    params = PlayParams(issue_number=42)
    result = await render_skill_prompt(
        "agentshore-issue-pickup", params, project_path=tmp_path, dispatch_cwd=None
    )

    assert "Your working directory" not in result
    assert "{cwd_block}" not in result
    assert "## AgentShore Context Discipline" in result


# ---------------------------------------------------------------------------
# render_skill_prompt — auto-reinstall fallback
# ---------------------------------------------------------------------------


async def test_render_skill_prompt_uses_existing_skill_md(tmp_path: Path) -> None:
    """Happy path: when SKILL.md is already present, its body is embedded directly."""
    skill_dir = tmp_path / ".agents" / "skills" / "agentshore-seed-project"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: agentshore-seed-project
agentshore_version: 0.0.1
---

# agentshore-seed-project

Pre-existing custom body.
""",
        encoding="utf-8",
    )

    result = await render_skill_prompt(
        "agentshore-seed-project",
        PlayParams(seed_path="/seed"),
        project_path=tmp_path,
    )

    assert result.startswith("$ARGUMENTS: /seed")
    assert "Pre-existing custom body." in result
    # Confirm we did *not* re-install over the existing file.
    assert "Pre-existing custom body." in (skill_dir / "SKILL.md").read_text()


async def test_render_skill_prompt_auto_reinstalls_when_missing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When SKILL.md is missing, render self-heals from bundled templates."""
    # Project has an empty .agents/skills/ — no agentshore-seed-project subdir.
    (tmp_path / ".agents" / "skills").mkdir(parents=True)

    skill_file = tmp_path / ".agents" / "skills" / "agentshore-seed-project" / "SKILL.md"
    assert not skill_file.exists()

    with caplog.at_level(logging.INFO):
        result = await render_skill_prompt(
            "agentshore-seed-project",
            PlayParams(seed_path="/seed.md"),
            project_path=tmp_path,
        )

    # The skill file should now exist (bundled templates were reinstalled).
    assert skill_file.exists(), "auto-reinstall must materialise the SKILL.md"

    # The returned prompt embeds the SKILL.md body, not a slash-command string.
    assert result.startswith("$ARGUMENTS: /seed.md")
    assert "## AgentShore Context Discipline" in result
    assert not result.startswith("/agentshore-seed-project"), (
        "must not return literal slash-command fallback"
    )

    # The auto-reinstall log event was emitted. structlog routing varies by
    # test ordering — accept either capsys (PrintLogger) or caplog (stdlib).
    captured = capsys.readouterr()
    in_capsys = "skill_template_auto_reinstalled" in (captured.out + captured.err)
    in_caplog = any("skill_template_auto_reinstalled" in rec.getMessage() for rec in caplog.records)
    assert in_capsys or in_caplog, (
        f"missing skill_template_auto_reinstalled event "
        f"(capsys={captured.out + captured.err!r}, caplog={caplog.records!r})"
    )


@pytest.mark.parametrize(
    "skill",
    [
        "agentshore-issue-pickup",
        "agentshore-unblock-pr",
        "agentshore-cleanup",
        "agentshore-write-plan",
        "agentshore-run-qa",
        "agentshore-code-review",
    ],
)
async def test_render_skill_prompt_includes_forbidden_mutations(skill: str, tmp_path: Path) -> None:
    """The 6 mutating skill templates must render their no-CI-workflows guard.

    Relies on the bundled-template auto-reinstall path: an empty project_path
    causes render_skill_prompt to install the bundled SKILL.md, then we
    inspect the rendered output.
    """
    import re as _re

    rendered = await render_skill_prompt(skill, PlayParams(), project_path=tmp_path)

    # Forbidden section exists in some form (## heading or **bold** inline) — style-agnostic.
    assert _re.search(r"(?:^|\n)(?:#+\s*Forbidden|\*\*Forbidden)", rendered), (
        f"{skill}: missing 'Forbidden' section in rendered prompt"
    )
    assert ".github/workflows" in rendered, (
        f"{skill}: missing '.github/workflows' path glob in rendered prompt"
    )
    # agentshore-run-qa's escape clause is "file an issue instead" — the other
    # five mutating skills must surface the policy-error string verbatim so
    # a downstream agent emits the canonical failure code.
    if skill != "agentshore-run-qa":
        assert "ci-change requested but forbidden by skill policy" in rendered, (
            f"{skill}: missing canonical policy-error string in rendered prompt"
        )


async def test_render_skill_prompt_raises_when_template_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If bundled templates are also missing, FileNotFoundError propagates."""
    empty_templates = tmp_path / "fake_templates"
    empty_templates.mkdir()
    monkeypatch.setattr("agentshore.skills._BUNDLED_TEMPLATES", empty_templates)

    project_path = tmp_path / "proj"
    project_path.mkdir()

    with pytest.raises(FileNotFoundError, match="skill template not installed"):
        await render_skill_prompt(
            "agentshore-seed-project",
            PlayParams(seed_path="/x"),
            project_path=project_path,
        )


async def test_render_prompt_embedded_skill_adds_context_discipline(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".agents" / "skills" / "agentshore-code-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: agentshore-code-review
---

# agentshore-code-review

Read `.agentshore/learnings.json` if it exists.
""",
        encoding="utf-8",
    )

    result = await render_skill_prompt(
        "agentshore-code-review",
        PlayParams(pr_number=87),
        project_path=tmp_path,
    )

    assert result.startswith("$ARGUMENTS: 87")
    assert "## AgentShore Context Discipline" in result
    assert "Use its `learnings` field" in result
    assert "do not\nread `.agentshore/learnings.json`" in result
    assert "Read `.agentshore/learnings.json` if it exists." not in result


async def test_render_prompt_groom_backlog_allows_full_learnings_read(tmp_path: Path) -> None:
    """agentshore-groom-backlog gets the re-distillation carve-out: it MAY read the
    full learnings.json (allowance directive), and its learnings.json read lines are
    NOT stripped — unlike every other skill."""
    skill_dir = tmp_path / ".agents" / "skills" / "agentshore-groom-backlog"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: agentshore-groom-backlog
---

# agentshore-groom-backlog

Read the full `.agentshore/learnings.json` and re-distill it.
""",
        encoding="utf-8",
    )

    result = await render_skill_prompt(
        "agentshore-groom-backlog",
        PlayParams(),
        project_path=tmp_path,
    )

    assert "## AgentShore Context Discipline" in result
    assert "Use its `learnings` field" in result
    # Allowance directive, NOT the prohibition.
    assert "MAY\nadditionally read the full `.agentshore/learnings.json`" in result
    assert "do not\nread `.agentshore/learnings.json`" not in result
    # The skill's own full-store read line survives (strip carve-out for groom).
    assert "Read the full `.agentshore/learnings.json` and re-distill it." in result


# ---------------------------------------------------------------------------
# serialize_state_for_skill
# ---------------------------------------------------------------------------


def test_serialize_state_json_round_trips() -> None:
    prs = [
        PullRequestSnapshot(
            pr_number=42,
            title="Fix blocked flow",
            state="open",
            branch="fix-blocked-flow",
            issue_number=3,
            labels=["changes-requested"],
            review_decision="CHANGES_REQUESTED",
            status_check_summary="failed",
            is_draft=False,
            blocked=True,
            blocked_reasons=["changes_requested", "ci_failed"],
        )
    ]
    payload = serialize_state_for_skill(
        session_id="sess-abc",
        play_id=7,
        play_type=PlayType.MERGE_PR,
        skill_name="agentshore-merge-pr",
        params=PlayParams(pr_number=42),
        open_issues=[],
        budget_enabled=True,
        budget_total=5.0,
        budget_spent=1.0,
        learnings_count=2,
        pull_requests=prs,
    )
    raw = json.dumps(payload)
    data = json.loads(raw)

    assert data["schema_version"] == 1
    assert data["session_id"] == "sess-abc"
    assert data["current_play"] == "merge_pr"
    assert data["assigned_github_identity"] is None
    assert data["params"]["pr_number"] == 42
    assert data["pull_requests"][0]["blocked"] is True
    assert data["pull_requests"][0]["blocked_reasons"] == [
        "changes_requested",
        "ci_failed",
    ]
    assert data["budget"]["remaining"] == pytest.approx(4.0)
    assert data["learnings_count"] == 2


def test_serialize_state_with_trunk_allocation_in_extras_round_trips() -> None:
    """Regression: TrunkAllocation in params.extras must not break json.dumps.

    Issue #563: ``core/mixins/dispatch.py`` stamps a ``TrunkAllocation`` /
    ``WorktreeAllocation`` dataclass onto ``extras["worktree_allocation"]``.
    The serializer must scrub it (or convert to a plain dict) so the bare
    ``json.dump`` in ``write_context_file`` doesn't raise.
    """
    from pathlib import Path

    from agentshore.agents.worktree import TrunkAllocation

    allocation = TrunkAllocation(path=Path("/tmp/agentshore/repo"))
    params = PlayParams(
        issue_number=42,
        extras={
            "worktree_allocation": allocation,
            "worktree_path": "/tmp/agentshore/repo",
            "worktree_scope": "trunk",
            "claim_group_id": "cg-xyz",
        },
    )
    payload = serialize_state_for_skill(
        session_id="sess-abc",
        play_id=7,
        play_type=PlayType.WRITE_IMPLEMENTATION_PLAN,
        skill_name="agentshore-write-plan",
        params=params,
        open_issues=[],
        budget_enabled=False,
        budget_total=0.0,
        budget_spent=0.0,
        learnings_count=0,
    )
    raw = json.dumps(payload)
    data = json.loads(raw)

    assert data["params"]["extras"]["worktree_allocation"] == {"path": "/tmp/agentshore/repo"}
    assert data["params"]["extras"]["worktree_path"] == "/tmp/agentshore/repo"
    assert data["params"]["extras"]["claim_group_id"] == "cg-xyz"


def test_params_to_json_safe_dict_handles_path_and_allocation() -> None:
    """params_to_json_safe_dict must produce a json.dumps-able dict even when
    extras carries Path and dataclass values (issue #563)."""
    from pathlib import Path

    from agentshore.agents.worktree import TrunkAllocation

    params = PlayParams(
        pr_number=11,
        extras={
            "worktree_allocation": TrunkAllocation(path=Path("/tmp/x")),
            "raw_path": Path("/tmp/y"),
            "nested": {"deep_path": Path("/tmp/z")},
        },
    )
    payload = params_to_json_safe_dict(params)
    raw = json.dumps(payload)
    data = json.loads(raw)

    assert data["pr_number"] == 11
    assert data["extras"]["worktree_allocation"] == {"path": "/tmp/x"}
    assert data["extras"]["raw_path"] == "/tmp/y"
    assert data["extras"]["nested"] == {"deep_path": "/tmp/z"}


def test_params_to_json_safe_dict_handles_worktree_allocation_with_playtype() -> None:
    """Regression for the PlayType enum leaking through asdict recursion.

    ``WorktreeAllocation`` has a ``play_type: PlayType`` field. After the
    initial #563 fix scrubbed ``TrunkAllocation``, ``json.dumps`` still
    blew up on the enum inside ``WorktreeAllocation`` — failing every
    branch-creating play (issue_pickup, etc.). The serializer must
    convert Enum values to their wire value.
    """
    from pathlib import Path

    from agentshore.agents.worktree import WorktreeAllocation

    allocation = WorktreeAllocation(
        worktree_id=17,
        path=Path("/tmp/agentshore/wt-17"),
        branch_name="fix-issue-181",
        pre_branch_key=None,
        play_type=PlayType.ISSUE_PICKUP,
        scope="branch_creating",
    )
    params = PlayParams(
        issue_number=181,
        extras={"worktree_allocation": allocation},
    )
    payload = params_to_json_safe_dict(params)
    raw = json.dumps(payload)
    data = json.loads(raw)

    assert data["issue_number"] == 181
    assert data["extras"]["worktree_allocation"] == {
        "worktree_id": 17,
        "path": "/tmp/agentshore/wt-17",
        "branch_name": "fix-issue-181",
        "pre_branch_key": None,
        "play_type": "issue_pickup",
        "scope": "branch_creating",
    }


# ---------------------------------------------------------------------------
# Issue #565 (Track 4): _runtime_allocation is runtime-only and never
# leaks across the JSON boundary.
# ---------------------------------------------------------------------------


def test_runtime_allocation_is_not_serialized_by_params_to_json_safe_dict() -> None:
    """``_runtime_allocation`` is a private runtime-only handle.

    The structural fix for the JSON-serializer onion (issue #565) moves the
    worktree allocation off ``params.extras`` (which crosses the JSON boundary)
    onto a private ``PlayParams._runtime_allocation`` field. The serializer
    must omit it — present in memory for the executor's finalize path,
    invisible to disk.
    """
    from pathlib import Path

    from agentshore.agents.worktree import WorktreeAllocation

    allocation = WorktreeAllocation(
        worktree_id=42,
        path=Path("/tmp/wt-42"),
        branch_name="issue-101",
        pre_branch_key="pickup-bd-101",
        play_type=PlayType.ISSUE_PICKUP,
        scope="branch_creating",
    )
    params = PlayParams(
        issue_number=101,
        _runtime_allocation=allocation,
    )
    payload = params_to_json_safe_dict(params)
    raw = json.dumps(payload)
    data = json.loads(raw)

    # Top-level keys must not include _runtime_allocation (or any spelling).
    assert "_runtime_allocation" not in data
    assert "runtime_allocation" not in data
    # And the executor's runtime read site still works.
    assert params._runtime_allocation is allocation


def test_serialize_state_for_skill_omits_runtime_allocation() -> None:
    """The context.json payload must not include ``_runtime_allocation``.

    ``serialize_state_for_skill`` is the canonical context-payload builder
    invoked before every skill dispatch. With the Track 4 refactor the
    allocation is on a private ``PlayParams`` field, never ``extras`` —
    this test confirms the boundary holds even when the allocator stamps
    the dataclass on params before the serializer runs.
    """
    from pathlib import Path

    from agentshore.agents.worktree import WorktreeAllocation

    allocation = WorktreeAllocation(
        worktree_id=3,
        path=Path("/tmp/wt-3"),
        branch_name="fix-z",
        pre_branch_key=None,
        play_type=PlayType.ISSUE_PICKUP,
        scope="branch_creating",
    )
    params = PlayParams(
        issue_number=3,
        _runtime_allocation=allocation,
        extras={"worktree_path": "/tmp/wt-3", "worktree_scope": "branch_creating"},
    )
    payload = serialize_state_for_skill(
        session_id="sess-565",
        play_id=1,
        play_type=PlayType.ISSUE_PICKUP,
        skill_name="agentshore-issue-pickup",
        params=params,
        open_issues=[],
        budget_enabled=False,
        budget_total=0.0,
        budget_spent=0.0,
        learnings_count=0,
    )
    raw = json.dumps(payload)
    data = json.loads(raw)

    assert "_runtime_allocation" not in data["params"]
    assert "worktree_allocation" not in data["params"]["extras"]
    # The two string-shaped views skills actually read stay present.
    assert data["params"]["extras"]["worktree_path"] == "/tmp/wt-3"
    assert data["params"]["extras"]["worktree_scope"] == "branch_creating"


def test_serialize_state_includes_assigned_github_identity() -> None:
    payload = serialize_state_for_skill(
        session_id="sess-abc",
        play_id=7,
        play_type=PlayType.ISSUE_PICKUP,
        skill_name="agentshore-issue-pickup",
        params=PlayParams(issue_number=42),
        open_issues=[],
        budget_enabled=False,
        budget_total=0.0,
        budget_spent=0.0,
        learnings_count=0,
        assigned_github_identity="bot-user",
    )
    assert payload["assigned_github_identity"] == "bot-user"


def test_serialize_state_includes_target_branch_when_set() -> None:
    """desktop-53m0: configured ``project.target_branch`` is surfaced to skills."""
    payload = serialize_state_for_skill(
        session_id="sess-abc",
        play_id=7,
        play_type=PlayType.ISSUE_PICKUP,
        skill_name="agentshore-issue-pickup",
        params=PlayParams(issue_number=42),
        open_issues=[],
        budget_enabled=False,
        budget_total=0.0,
        budget_spent=0.0,
        learnings_count=0,
        target_branch="develop",
    )
    assert payload["target_branch"] == "develop"


def test_serialize_state_target_branch_defaults_to_none() -> None:
    """Skills must see ``target_branch: null`` (not missing) when unset, so
    a `jq -r '.target_branch // empty'` lookup always returns empty without
    erroring on the missing key.
    """
    payload = serialize_state_for_skill(
        session_id="sess-abc",
        play_id=7,
        play_type=PlayType.ISSUE_PICKUP,
        skill_name="agentshore-issue-pickup",
        params=PlayParams(issue_number=42),
        open_issues=[],
        budget_enabled=False,
        budget_total=0.0,
        budget_spent=0.0,
        learnings_count=0,
    )
    assert "target_branch" in payload
    assert payload["target_branch"] is None


def test_serialize_state_includes_project_path() -> None:
    """The skill payload must carry the absolute project root.

    Skill agents need a canonical anchor for `MAIN_REPO` rather than relying
    on `$(pwd)`, which has been picking up leftover worktree paths.
    """
    payload = serialize_state_for_skill(
        session_id="sess-abc",
        play_id=11,
        play_type=PlayType.ISSUE_PICKUP,
        skill_name="agentshore-issue-pickup",
        params=PlayParams(issue_number=99),
        open_issues=[],
        budget_enabled=False,
        budget_total=0.0,
        budget_spent=0.0,
        learnings_count=0,
        project_path="/abs/projects/example-repo",
    )
    assert payload["project_path"] == "/abs/projects/example-repo"


def test_serialize_state_project_path_defaults_to_none() -> None:
    """Callers that do not supply project_path should still get the key, set to None."""
    payload = serialize_state_for_skill(
        session_id="sess-abc",
        play_id=11,
        play_type=PlayType.ISSUE_PICKUP,
        skill_name="agentshore-issue-pickup",
        params=PlayParams(issue_number=99),
        open_issues=[],
        budget_enabled=False,
        budget_total=0.0,
        budget_spent=0.0,
        learnings_count=0,
    )
    assert "project_path" in payload
    assert payload["project_path"] is None


# ---------------------------------------------------------------------------
# write_play_context
# ---------------------------------------------------------------------------


def test_write_play_context_creates_context_file(tmp_path: Path) -> None:
    payload = {"schema_version": 1, "test": True}
    write_play_context(tmp_path, payload)

    ctx_file = tmp_path / ".agentshore" / "context.json"
    assert ctx_file.exists()
    data = json.loads(ctx_file.read_text())
    assert data["test"] is True


def test_write_play_context_is_atomic_no_partial_file(tmp_path: Path) -> None:
    """Verify the temp-file pattern: even if we watch closely, only the final file exists."""
    payload = {"schema_version": 1, "value": 42}
    write_play_context(tmp_path, payload)

    ctx_file = tmp_path / ".agentshore" / "context.json"
    tmp_files = list((tmp_path / ".agentshore").glob(".context_*.tmp"))
    assert ctx_file.exists()
    assert len(tmp_files) == 0, "No .tmp files should remain after atomic rename"


def test_write_play_context_returns_byte_count(tmp_path: Path) -> None:
    payload = {"schema_version": 1, "value": 42}
    bytes_written = write_play_context(tmp_path, payload)
    ctx_file = tmp_path / ".agentshore" / "context.json"
    assert bytes_written == ctx_file.stat().st_size > 0


def test_write_play_context_creates_play_specific_and_latest_copy(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "play_id": 7,
        "current_play": "write_implementation_plan",
        "open_issues": [],
        "pull_requests": [],
    }
    context_path = play_context_relative_path(7)

    bytes_written = write_play_context(
        tmp_path,
        payload,
        context_relative_path=context_path,
    )

    play_file = tmp_path / ".agentshore" / "contexts" / "play-7.json"
    latest_file = tmp_path / ".agentshore" / "context.json"
    assert bytes_written == play_file.stat().st_size > 0
    assert latest_file.exists()

    play_data = json.loads(play_file.read_text())
    latest_data = json.loads(latest_file.read_text())
    assert play_data["context_file"] == context_path
    assert latest_data["context_file"] == context_path


def test_play_context_relative_path_can_scope_by_session() -> None:
    assert (
        play_context_relative_path(7, session_id="sess:abc/123")
        == ".agentshore/contexts/sess_abc_123/play-7.json"
    )


def test_write_play_context_preserves_prior_play_context(tmp_path: Path) -> None:
    first_path = play_context_relative_path(7)
    second_path = play_context_relative_path(8)

    write_play_context(
        tmp_path,
        {
            "schema_version": 1,
            "play_id": 7,
            "assigned_github_identity": "example-user",
            "open_issues": [],
            "pull_requests": [],
        },
        context_relative_path=first_path,
    )
    write_play_context(
        tmp_path,
        {
            "schema_version": 1,
            "play_id": 8,
            "assigned_github_identity": "bot-user",
            "open_issues": [],
            "pull_requests": [],
        },
        context_relative_path=second_path,
    )

    first_data = json.loads((tmp_path / first_path).read_text())
    latest_data = json.loads((tmp_path / ".agentshore" / "context.json").read_text())
    assert first_data["assigned_github_identity"] == "example-user"
    assert first_data["context_file"] == first_path
    assert latest_data["assigned_github_identity"] == "bot-user"
    assert latest_data["context_file"] == second_path


# ---------------------------------------------------------------------------
# Per-play context scoping
# ---------------------------------------------------------------------------


def _issues(*nums: int) -> list[IssueSnapshot]:
    return [
        IssueSnapshot(
            issue_number=n,
            title=f"issue {n}",
            state="open",
            priority=None,
            labels=[],
            source=None,
        )
        for n in nums
    ]


def _prs(*nums: int) -> list[PullRequestSnapshot]:
    return [
        PullRequestSnapshot(
            pr_number=n,
            title=f"pr {n}",
            state="open",
            branch=f"branch-{n}",
            issue_number=None,
            labels=[],
            review_decision=None,
            status_check_summary=None,
            is_draft=False,
            blocked=False,
            blocked_reasons=[],
        )
        for n in nums
    ]


def _serialize(
    play_type: PlayType,
    params: PlayParams,
    *,
    open_issues: list[IssueSnapshot] | None = None,
    pull_requests: list[PullRequestSnapshot] | None = None,
) -> dict[str, object]:
    return serialize_state_for_skill(
        session_id="s",
        play_id=1,
        play_type=play_type,
        skill_name=None,
        params=params,
        open_issues=open_issues or [],
        budget_enabled=False,
        budget_total=0.0,
        budget_spent=0.0,
        learnings_count=0,
        pull_requests=pull_requests or [],
    )


def test_scope_targeted_pr_play_filters_to_one_pr() -> None:
    """A PR-targeted play sees only its PR, not the whole list."""
    payload = _serialize(
        PlayType.MERGE_PR,
        PlayParams(pr_number=42),
        pull_requests=_prs(1, 42, 99),
        open_issues=_issues(5, 10),
    )
    assert [pr["pr_number"] for pr in payload["pull_requests"]] == [42]  # type: ignore[index, union-attr]
    assert payload["open_issues"] == []


def test_scope_targeted_issue_play_filters_to_one_issue() -> None:
    """An issue-targeted play sees only its issue, not the whole list."""
    payload = _serialize(
        PlayType.WRITE_IMPLEMENTATION_PLAN,
        PlayParams(issue_number=10),
        open_issues=_issues(5, 10, 99),
        pull_requests=_prs(1, 2),
    )
    assert [i["issue_number"] for i in payload["open_issues"]] == [10]  # type: ignore[index, union-attr]
    assert payload["pull_requests"] == []


def test_scope_full_issues_play_keeps_all_issues() -> None:
    """Cross-cutting plays keep the full open_issues list."""
    payload = _serialize(
        PlayType.GROOM_BACKLOG,
        PlayParams(),
        open_issues=_issues(1, 2, 3, 4),
    )
    assert [i["issue_number"] for i in payload["open_issues"]] == [1, 2, 3, 4]  # type: ignore[index, union-attr]


def test_scope_full_prs_play_keeps_all_prs() -> None:
    """Cross-cutting plays keep the full pull_requests list."""
    payload = _serialize(
        PlayType.CALIBRATE_ALIGNMENT,
        PlayParams(),
        pull_requests=_prs(1, 2, 3),
    )
    assert [pr["pr_number"] for pr in payload["pull_requests"]] == [1, 2, 3]  # type: ignore[index, union-attr]


def test_scope_unrelated_play_omits_lists() -> None:
    """A play that needs neither issues nor PRs gets empty lists."""
    payload = _serialize(
        PlayType.SYSTEMATIC_DEBUGGING,
        PlayParams(branch="feature/x"),
        open_issues=_issues(1, 2),
        pull_requests=_prs(7, 8),
    )
    assert payload["open_issues"] == []
    assert payload["pull_requests"] == []


def test_scope_issue_pickup_with_arg_filters_to_one() -> None:
    """ISSUE_PICKUP with an explicit issue number scopes down rather than dumping all."""
    payload = _serialize(
        PlayType.ISSUE_PICKUP,
        PlayParams(issue_number=5),
        open_issues=_issues(1, 5, 99),
    )
    assert [i["issue_number"] for i in payload["open_issues"]] == [5]  # type: ignore[index, union-attr]


def test_scope_issue_pickup_without_arg_keeps_all_issues() -> None:
    """ISSUE_PICKUP in selection mode (no $ARGUMENTS) sees the full list."""
    payload = _serialize(
        PlayType.ISSUE_PICKUP,
        PlayParams(),
        open_issues=_issues(1, 5, 99),
    )
    assert [i["issue_number"] for i in payload["open_issues"]] == [1, 5, 99]  # type: ignore[index, union-attr]
