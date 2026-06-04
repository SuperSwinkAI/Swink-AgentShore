"""Tests for agentshore.skills — skill installation and versioning helpers."""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

import agentshore.skills as skills_module
from agentshore.plays.dispatch import PLAY_SKILL_MAP
from agentshore.rl.action_space import PLAY_TO_INDEX
from agentshore.skills import (
    _parse_agentshore_version,
    _should_overwrite,
    _stamp_version,
    install_skills,
    uninstall_skills,
)

_TEMPLATE_ROOT = Path(__file__).parent.parent / "src" / "agentshore" / "skills" / "templates"


class TestInstallSkills:
    """Exercise install_skills() on a temporary project directory."""

    def test_install_skills_creates_files(self, tmp_path: Path) -> None:
        """Calling on a real project copies bundled skill dirs/files."""
        installed = install_skills(tmp_path)
        # The repo has bundled skills; install_skills should copy at least one.
        assert isinstance(installed, list)
        assert len(installed) > 0

        skills_dir = tmp_path / ".agents" / "skills"
        assert skills_dir.is_dir()
        assert not (tmp_path / ".claude" / "skills").exists()
        assert not (tmp_path / ".codex" / "skills").exists()

    def test_install_skills_idempotent(self, tmp_path: Path) -> None:
        """Calling twice does not raise and re-installs same set."""
        install_skills(tmp_path)
        second = install_skills(tmp_path)
        # Second call should return empty (already up-to-date, same version).
        assert isinstance(second, list)
        # No error either way.

    def test_install_skills_returns_list_of_skill_names(self, tmp_path: Path) -> None:
        """Return value is a sorted list of skill name strings."""
        installed = install_skills(tmp_path)
        assert isinstance(installed, list)
        for name in installed:
            assert isinstance(name, str)
        # Sorted invariant.
        assert installed == sorted(installed)

    def test_uninstall_removes_installed_skills(self, tmp_path: Path) -> None:
        """uninstall_skills removes what install_skills created."""
        install_skills(tmp_path)
        removed = uninstall_skills(tmp_path)
        assert isinstance(removed, list)
        assert len(removed) > 0
        assert not any((tmp_path / ".agents" / "skills").iterdir())


def _make_fake_template(
    root: Path,
    name: str,
    *,
    skill_body: str = "Body",
    references: dict[str, str] | None = None,
    scripts: dict[str, str] | None = None,
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n{skill_body}",
        encoding="utf-8",
    )
    if references:
        ref_dir = skill_dir / "references"
        ref_dir.mkdir()
        for fname, body in references.items():
            (ref_dir / fname).write_text(body, encoding="utf-8")
    if scripts:
        script_dir = skill_dir / "scripts"
        script_dir.mkdir()
        for fname, body in scripts.items():
            path = script_dir / fname
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return skill_dir


class TestInstallSkillFolders:
    """Skill installs must ship references/ and scripts/ siblings, not just SKILL.md."""

    def test_install_copies_references_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_root = tmp_path / "templates"
        fake_root.mkdir()
        _make_fake_template(
            fake_root,
            "fake-skill",
            references={"patterns.md": "# patterns\n", "guide.md": "# guide\n"},
        )
        monkeypatch.setattr(skills_module, "_BUNDLED_TEMPLATES", fake_root)

        project = tmp_path / "project"
        installed = install_skills(project)

        assert "fake-skill" in installed
        ref_dir = project / ".agents" / "skills" / "fake-skill" / "references"
        assert (ref_dir / "patterns.md").read_text(encoding="utf-8") == "# patterns\n"
        assert (ref_dir / "guide.md").read_text(encoding="utf-8") == "# guide\n"

    def test_install_copies_scripts_with_executable_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_root = tmp_path / "templates"
        fake_root.mkdir()
        _make_fake_template(
            fake_root,
            "fake-skill",
            scripts={"detect.sh": "#!/usr/bin/env bash\necho hi\n"},
        )
        monkeypatch.setattr(skills_module, "_BUNDLED_TEMPLATES", fake_root)

        project = tmp_path / "project"
        install_skills(project)

        script = project / ".agents" / "skills" / "fake-skill" / "scripts" / "detect.sh"
        assert script.is_file()
        assert script.stat().st_mode & stat.S_IXUSR, "exec bit not preserved"

    def test_user_customised_skill_blocks_reference_overwrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_root = tmp_path / "templates"
        fake_root.mkdir()
        _make_fake_template(
            fake_root,
            "fake-skill",
            skill_body="new body",
            references={"patterns.md": "# new\n"},
        )
        monkeypatch.setattr(skills_module, "_BUNDLED_TEMPLATES", fake_root)

        project = tmp_path / "project"
        install_skills(project)

        # User edits SKILL.md (drops the agentshore_version stamp).
        target_skill = project / ".agents" / "skills" / "fake-skill" / "SKILL.md"
        target_skill.write_text("---\nname: fake-skill\n---\ncustom\n", encoding="utf-8")
        target_ref = project / ".agents" / "skills" / "fake-skill" / "references" / "patterns.md"
        target_ref.write_text("# user edit\n", encoding="utf-8")

        # Bump source content; install_skills should NOT overwrite either file.
        (fake_root / "fake-skill" / "references" / "patterns.md").write_text(
            "# upstream\n", encoding="utf-8"
        )
        install_skills(project)

        assert target_skill.read_text(encoding="utf-8") == ("---\nname: fake-skill\n---\ncustom\n")
        assert target_ref.read_text(encoding="utf-8") == "# user edit\n"

    def test_force_overwrites_references(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_root = tmp_path / "templates"
        fake_root.mkdir()
        _make_fake_template(
            fake_root,
            "fake-skill",
            references={"patterns.md": "# v1\n"},
        )
        monkeypatch.setattr(skills_module, "_BUNDLED_TEMPLATES", fake_root)

        project = tmp_path / "project"
        install_skills(project)

        # User edits the reference; bump source; force=True must replace it.
        ref_path = project / ".agents" / "skills" / "fake-skill" / "references" / "patterns.md"
        ref_path.write_text("# user edit\n", encoding="utf-8")
        (fake_root / "fake-skill" / "references" / "patterns.md").write_text(
            "# v2\n", encoding="utf-8"
        )
        install_skills(project, force=True)

        assert ref_path.read_text(encoding="utf-8") == "# v2\n"

    def test_uninstall_removes_full_skill_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_root = tmp_path / "templates"
        fake_root.mkdir()
        _make_fake_template(
            fake_root,
            "fake-skill",
            references={"patterns.md": "# patterns\n"},
            scripts={"detect.sh": "echo hi\n"},
        )
        monkeypatch.setattr(skills_module, "_BUNDLED_TEMPLATES", fake_root)

        project = tmp_path / "project"
        install_skills(project)
        skill_dir = project / ".agents" / "skills" / "fake-skill"
        assert (skill_dir / "references" / "patterns.md").exists()

        uninstall_skills(project)
        assert not skill_dir.exists()


class TestVersionHelpers:
    """Unit tests for the internal versioning helpers."""

    def test_parse_agentshore_version_present(self) -> None:
        text = "---\nagentshore_version: 1.2.3\n---\nBody"
        assert _parse_agentshore_version(text) == "1.2.3"

    def test_parse_agentshore_version_absent(self) -> None:
        text = "---\ntitle: Hello\n---\nBody"
        assert _parse_agentshore_version(text) is None

    def test_should_overwrite_newer_source(self) -> None:
        existing = "---\nagentshore_version: 0.0.1\n---\n"
        source = "---\nagentshore_version: 0.1.0\n---\n"
        assert _should_overwrite(existing, source) is True

    def test_should_overwrite_older_source(self) -> None:
        existing = "---\nagentshore_version: 1.0.0\n---\n"
        source = "---\nagentshore_version: 0.1.0\n---\n"
        assert _should_overwrite(existing, source) is False

    def test_should_overwrite_equal_version_when_source_changed(self) -> None:
        existing = "---\nagentshore_version: 1.0.0\n---\nold"
        source = "---\nagentshore_version: 1.0.0\n---\nnew"
        assert _should_overwrite(existing, source) is True

    def test_should_overwrite_no_existing_version(self) -> None:
        """If existing has no version field, treat as user-modified: don't overwrite."""
        existing = "---\ntitle: Custom\n---\n"
        source = "---\nagentshore_version: 0.1.0\n---\n"
        assert _should_overwrite(existing, source) is False

    def test_stamp_version_inserts_into_frontmatter(self) -> None:
        text = "---\ntitle: Hello\n---\nBody"
        stamped = _stamp_version(text)
        assert "agentshore_version:" in stamped

    def test_stamp_version_replaces_existing(self) -> None:
        text = "---\nagentshore_version: 0.0.1\n---\nBody"
        stamped = _stamp_version(text)
        ver = _parse_agentshore_version(stamped)
        assert ver is not None
        # Should now be the current package version.
        from agentshore import __version__

        assert ver == __version__

    def test_stamp_version_no_frontmatter(self) -> None:
        text = "Just body text"
        stamped = _stamp_version(text)
        assert stamped.startswith("---\n")
        assert "agentshore_version:" in stamped


def test_alignment_related_templates_use_all_beads_graph_scan() -> None:
    skill_names = [
        "agentshore-calibrate-alignment",
        "agentshore-design-audit",
        "agentshore-seed-project",
        "agentshore-groom-backlog",
    ]
    for skill_name in skill_names:
        text = (_TEMPLATE_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
        assert "bd list --all --json --limit 0" in text
        assert "bd task list" not in text
        assert "bd story list" not in text
        assert "bd epic status --json" not in text


def test_calibrate_alignment_template_uses_authoritative_issue_state() -> None:
    text = (_TEMPLATE_ROOT / "agentshore-calibrate-alignment" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "gh issue list --state closed" in text
    assert 'bd close <bead_id> --reason "GitHub issue #N is closed"' in text
    assert "bd update <bead_id> --status in_progress" in text
    assert "bd set-state" not in text


def test_calibrate_alignment_template_resets_orphan_in_progress_beads() -> None:
    """Calibrate must clear an in_progress bead whose PR was closed/abandoned.

    Symmetric to the open->in_progress promotion: an in_progress task with no
    open PR and an un-closed issue is orphaned and must be reset to open, else
    it wedges plan/pickup/refine/debug dispatch for that issue forever.
    """
    text = (_TEMPLATE_ROOT / "agentshore-calibrate-alignment" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    # The orphan-reset action and its trigger condition are present.
    assert "bd update <bead_id> --status open" in text
    assert "orphaned" in text
    assert "no open" in text.lower()
    # The validation rule must carve out the in_progress -> open downgrade.
    assert "in_progress → open" in text
    # Closure ratio is argued to be unaffected so the monotonic invariant holds.
    assert "only `closed` tasks" in text


def test_design_audit_template_creates_tracking_work_for_gaps() -> None:
    text = (_TEMPLATE_ROOT / "agentshore-design-audit" / "SKILL.md").read_text(encoding="utf-8")

    assert "Do not implement code" in text
    assert "gh issue list --state open" in text
    assert "gh issue list --state closed" in text
    assert "bd create task" in text
    assert '"type": "design_audit"' in text
    assert "unresolved_gaps" in text


def test_groom_backlog_template_clears_resolved_dependency_blocks() -> None:
    """groom-backlog must remove sticky blocked labels once all deps resolve (gh#35 follow-up).

    A GH ``blocked`` / ``agentshore/blocked`` label does not auto-clear when its
    blocker closes, so the issue stays out of the issue_pickup pool forever. Groom
    is the periodic sweep that reconciles the (already self-healed) beads state to
    the sticky GH label — but only on concrete evidence, never an opaque block.
    """
    text = (_TEMPLATE_ROOT / "agentshore-groom-backlog" / "SKILL.md").read_text(encoding="utf-8")

    # The clearing action and its conservative gate are present.
    assert "blocks_cleared" in text
    assert "--remove-label" in text
    assert "agentshore/blocked" in text
    # Evidence-gated: requires an identifiable, fully-resolved dependency.
    assert "identifiable dependency" in text
    # Opaque/manual blocks and human-review gates must be left alone.
    assert "needs-human-review" in text


def test_skill_template_descriptions_match_action_slots() -> None:
    """Skill descriptions should not carry stale play numbers."""
    for play_type, skill_name in PLAY_SKILL_MAP.items():
        text = (_TEMPLATE_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
        expected = f'description: "Action slot {PLAY_TO_INDEX[play_type]} '
        assert expected in text


def test_code_review_template_forbids_main_worktree_checkout() -> None:
    """agentshore-code-review must explicitly forbid mutating the main worktree.

    Reviewers were checking out PR branches in the shared main worktree,
    pinning every concurrent play on the wrong branch. The template now
    carries a Forbidden mutations section that names the offending commands.
    """
    text = (_TEMPLATE_ROOT / "agentshore-code-review" / "SKILL.md").read_text(encoding="utf-8")
    # Forbidden section exists in some form (heading or bold inline) — style-agnostic.
    assert re.search(r"(?:^|\n)(?:#+\s*Forbidden|\*\*Forbidden)", text), (
        "agentshore-code-review: missing a Forbidden section"
    )
    assert "git checkout" in text
    assert "gh pr checkout" in text
    assert "gh pr diff $ARGUMENTS" in text
    assert ".github/workflows" in text


_PATH_GUARD_TEMPLATES = (
    "agentshore-issue-pickup",
    "agentshore-unblock-pr",
    "agentshore-merge-pr",
    "agentshore-cleanup",
    "agentshore-systematic-debugging",
    "agentshore-run-qa",
)


def test_mutation_templates_forbid_git_worktree_calls() -> None:
    """The 6 mutation-capable templates must forbid `git worktree add/remove/prune`.

    AgentShore now owns worktree lifecycle (allocate-before-dispatch, finalize-after,
    session-start sweep, PR-close TTL reaper). The agent inherits a cwd already
    pointed at the right worktree via dispatch_cli's cwd_override — skill-side
    worktree mutation would clobber that contract. Replaces the older
    skill-internal `MAIN_REPO=${AGENTSHORE_PROJECT_PATH:-$(pwd)}` + backslash-guard
    pattern, which is now obsolete because AgentShore injects the cwd directly.
    """
    # Accept the comma-separated form ("git worktree add, git worktree remove, ...")
    # OR the slash form ("git worktree add/remove/prune"). The intent — all three
    # operations forbidden in the same clause — is what matters; the prose style
    # is not load-bearing.
    long_form = re.compile(
        r"`git worktree add`.*`git worktree remove`.*`git worktree prune`",
        re.DOTALL,
    )
    short_form = re.compile(r"`?git worktree add/remove/prune`?")
    for skill in _PATH_GUARD_TEMPLATES:
        text = (_TEMPLATE_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
        assert long_form.search(text) or short_form.search(text), (
            f"{skill}: missing the forbidden git worktree add/remove/prune clause"
        )
        # Accept either "AgentShore owns worktree lifecycle" or the compressed
        # "AgentShore owns lifecycle" (when the worktree context is clear from
        # the adjacent forbidden-worktree clause). Both convey the same rule.
        assert re.search(r"AgentShore owns (?:worktree )?lifecycle", text), (
            f"{skill}: missing the 'AgentShore owns (worktree) lifecycle' statement"
        )


def test_mutation_templates_have_no_bare_main_repo_pwd() -> None:
    """Regression guard: the bare `MAIN_REPO=$(pwd)` form must not creep back in.

    Even though the new worktree model makes MAIN_REPO unnecessary (AgentShore
    sets cwd directly), reverting to `MAIN_REPO=$(pwd)` in any skill would
    silently reintroduce the pwd-drift class of bug that motivated the
    original guard.
    """
    for skill in _PATH_GUARD_TEMPLATES:
        text = (_TEMPLATE_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
        assert "MAIN_REPO=$(pwd)" not in text, (
            f"{skill}: bare `MAIN_REPO=$(pwd)` snuck back in — see worktree refactor"
        )


def test_pr_create_resolves_base_inline() -> None:
    """Every `gh pr create` must resolve its --base in the same command block (#8).

    Regression guard: the issue-pickup skill used to set `$TARGET_BRANCH` in a
    pre-flight step and reference it at `gh pr create` ~28 lines later. Those run
    as separate shell invocations, so the var was empty at create time and `gh`
    silently defaulted the PR base to the repo default branch — every PR then
    stranded as `wrong_base_branch` in merge_pr. The base must be resolved
    adjacent to (and in the same command as) the create. The commit 2e7a051
    skills-compression introduced exactly this split; this test would have
    caught it. Scans ALL templates so a new PR-creating skill is covered too.
    """
    create_re = re.compile(r"gh pr create\b[^\n`]*")
    inline_subst_re = re.compile(r'--base\s+"\$\([^)]*target_branch[^)]*\)"')
    found_any = False
    for skill_dir in sorted(_TEMPLATE_ROOT.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        text = skill_md.read_text(encoding="utf-8")
        for m in create_re.finditer(text):
            cmd = m.group(0)
            # Skip flagless prose mentions like "as `gh pr create`" — only real
            # invocations (which always carry flags) are subject to the rule.
            if "--" not in cmd:
                continue
            found_any = True
            assert "--base" in cmd, f"{skill_dir.name}: `gh pr create` is missing --base"
            # Base must be resolved inline: either a command substitution that
            # reads target_branch in the --base arg, or a BASE= assignment within
            # the preceding 400 chars (same command block) — NOT a distant
            # pre-flight $TARGET_BRANCH that won't survive a fresh shell.
            window = text[max(0, m.start() - 400) : m.start()]
            local_assign = "BASE=" in window and "target_branch" in window
            assert inline_subst_re.search(cmd) or local_assign, (
                f"{skill_dir.name}: `gh pr create` --base is not resolved inline/adjacent "
                "(regression #8 — base must not rely on a distant $TARGET_BRANCH var)"
            )
    assert found_any, "expected at least one `gh pr create` across skill templates"
