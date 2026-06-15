"""Phase 6 Wave 2 Agent 2B: CLI polish and error UX tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from agentshore.cli import main
from agentshore.cli.agent_select import _interactive_agent_select
from agentshore.cli.helpers import _resolve_policy_mode_override
from agentshore.cli.identity_helpers import _agent_keys_from_yaml
from agentshore.config.models import AgentConfig, PolicyMode, RuntimeConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with agentshore.yaml."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "agentshore.yaml").write_text("budget:\n  enabled: true\n  total: 20.0\n")
    return tmp_path


# ---------------------------------------------------------------------------
# 0agentshore start --help lists all options
# ---------------------------------------------------------------------------


def test_start_help_lists_all_options() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["start", "--help"])
    assert result.exit_code == 0
    output = result.output
    for flag in (
        "--seed",
        "--budget",
        "--mode",
        "--tui",
        "--socket",
        "--policy-mode",
        "--policy",
        "--strict",
        "--project",
        "--config",
    ):
        assert flag in output, f"Missing {flag} in start --help"
    assert "--deterministic" not in output
    assert "[tui|agent]" in output
    assert "solo" not in output


def test_legacy_deterministic_flag_maps_to_audit_replay(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _resolve_policy_mode_override(policy_mode=None, legacy_deterministic=True)
    captured = capsys.readouterr()

    assert result == PolicyMode.AUDIT_REPLAY
    assert "--deterministic is deprecated" in captured.err


def test_legacy_deterministic_conflicts_with_learning_policy_mode() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["start", "--policy-mode", "learning", "--deterministic"])

    assert result.exit_code != 0
    assert "conflicts with --policy-mode learning" in result.output


# ---------------------------------------------------------------------------
# 0agentshore init --help mentions --force and --install-skills
# ---------------------------------------------------------------------------


def test_init_help_text() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--help"])
    assert result.exit_code == 0
    assert "--force" in result.output
    assert "--install-skills" in result.output
    assert "--target-branch" in result.output


# ---------------------------------------------------------------------------
# 2b. agentshore init --target-branch persists the value (desktop-3t62)
# ---------------------------------------------------------------------------


def test_init_target_branch_flag_writes_value_and_skips_prompt(tmp_path: Path) -> None:
    """``agentshore init --target-branch <name>`` persists the value under
    ``project.target_branch`` and never prompts (non-interactive parity with
    the desktop setup wizard).
    """
    import yaml as _yaml

    repo = _make_git_repo(tmp_path)

    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
        patch("agentshore.cli.commands.init._interactive_agent_select"),
        patch("agentshore.identity_wizard.run_identity_wizard"),
    ):
        result = runner.invoke(
            main,
            ["init", "--project", str(repo), "--force", "--target-branch", "feature/x"],
        )

    assert result.exit_code == 0, result.output
    cfg_text = (repo / "agentshore.yaml").read_text()
    cfg = _yaml.safe_load(cfg_text)
    assert cfg["project"]["target_branch"] == "feature/x"
    assert "Set project.target_branch = feature/x" in result.output


def test_init_target_branch_flag_rejects_empty(tmp_path: Path) -> None:
    """An empty ``--target-branch`` value is a CLI usage error."""
    repo = _make_git_repo(tmp_path)
    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
        patch("agentshore.cli.commands.init._interactive_agent_select"),
        patch("agentshore.identity_wizard.run_identity_wizard"),
    ):
        result = runner.invoke(
            main,
            ["init", "--project", str(repo), "--force", "--target-branch", "   "],
        )
    assert result.exit_code != 0
    assert "--target-branch must not be empty" in result.output


def test_init_without_target_branch_flag_in_non_tty_leaves_yaml_alone(tmp_path: Path) -> None:
    """Scripted / CI ``agentshore init`` (no TTY, no flag) does not write the key.

    Tests that omit the flag in non-interactive contexts must remain
    deterministic - no surprise YAML mutations.
    """
    import yaml as _yaml

    repo = _make_git_repo(tmp_path)
    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
        patch("agentshore.cli.commands.init._interactive_agent_select"),
        patch("agentshore.identity_wizard.run_identity_wizard"),
    ):
        # CliRunner.invoke without input keeps stdin non-TTY.
        result = runner.invoke(main, ["init", "--project", str(repo), "--force"])

    assert result.exit_code == 0, result.output
    cfg = _yaml.safe_load((repo / "agentshore.yaml").read_text())
    # project block exists, target_branch absent.
    assert "target_branch" not in (cfg.get("project") or {})


# ---------------------------------------------------------------------------
# Removed CLI commands (report, train, configure, archive) are not registered
# ---------------------------------------------------------------------------


def test_removed_commands_not_registered() -> None:
    runner = CliRunner()
    for cmd in ("report", "train", "configure", "archive"):
        result = runner.invoke(main, [cmd, "--help"])
        assert result.exit_code != 0, f"removed command '{cmd}' still resolves"
        assert "No such command" in result.output


# ---------------------------------------------------------------------------
# 0agentshore init --force overwrites existing config
# ---------------------------------------------------------------------------


def test_init_force_merges_config_preserving_user_keys(tmp_path: Path) -> None:
    """``init --force`` now merges fresh template defaults into existing config
    rather than replacing wholesale. User-edited keys + comments survive.

    Regression for the 2026-05-07 init-wizard rework: previous behavior wiped
    user customizations on every reinit.
    """
    repo = _make_git_repo(tmp_path)
    original = "# original config\nbudget:\n  enabled: true\n  total: 1.0\n"
    (repo / "agentshore.yaml").write_text(original)

    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
    ):
        result = runner.invoke(main, ["init", "--project", str(repo), "--force"])

    assert result.exit_code == 0
    new_content = (repo / "agentshore.yaml").read_text()
    # User-edited budget total + comment must survive the merge.
    assert "# original config" in new_content
    assert "total: 1.0" in new_content
    # The agents skeleton was re-rendered.
    assert "claude_code" in new_content
    assert "Merging fresh template" in result.output


def test_init_passes_force_run_to_agent_setup_wizard(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    captured: dict[str, object] = {}

    def fake_agent_select(
        cfg: RuntimeConfig,
        detected_agents: list[str],
        config_path: Path,
        *,
        force_run: bool = False,
    ) -> RuntimeConfig:
        captured["detected_agents"] = detected_agents
        captured["config_path"] = config_path
        captured["force_run"] = force_run
        return cfg

    monkeypatch.setattr("agentshore.cli.commands.init._interactive_agent_select", fake_agent_select)
    monkeypatch.setattr("agentshore.identity_wizard.run_identity_wizard", lambda *_a, **_k: None)

    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude", "codex"]),
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
    ):
        result = runner.invoke(main, ["init", "--project", str(repo)])

    assert result.exit_code == 0, result.output
    assert captured["force_run"] is True
    assert captured["detected_agents"] == ["claude", "codex"]
    assert captured["config_path"] == repo / "agentshore.yaml"


def test_init_does_not_assume_cli_agents_when_none_detected(tmp_path: Path, monkeypatch) -> None:
    import yaml

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    captured: dict[str, object] = {}

    def fake_agent_select(
        cfg: RuntimeConfig,
        detected_agents: list[str],
        config_path: Path,
        *,
        force_run: bool = False,
    ) -> RuntimeConfig:
        captured["detected_agents"] = detected_agents
        captured["force_run"] = force_run
        return cfg

    def fake_identity_wizard(*_args: object, **_kwargs: object) -> None:
        captured["identity_wizard_called"] = True

    monkeypatch.setattr("agentshore.cli.commands.init._interactive_agent_select", fake_agent_select)
    monkeypatch.setattr("agentshore.identity_wizard.run_identity_wizard", fake_identity_wizard)

    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=[]),
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
    ):
        result = runner.invoke(main, ["init", "--project", str(repo)])

    assert result.exit_code == 0, result.output
    assert captured["force_run"] is True
    assert captured["detected_agents"] == []
    assert "identity_wizard_called" not in captured
    data = yaml.safe_load((repo / "agentshore.yaml").read_text(encoding="utf-8"))
    assert data["agents"] == {}


def test_init_agent_setup_runs_when_non_agent_config_is_invalid(
    tmp_path: Path, monkeypatch
) -> None:
    """Invalid identity config must not skip the agent selection wizard."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "agentshore.yaml").write_text(
        """\
agents:
  claude_code:
    enabled: true
    binary: claude
  codex:
    enabled: true
    binary: codex
identities:
  Bot-User:
    git_user_name: Bot-User
    git_user_email: bot-user@users.noreply.github.com
  bot-user:
    git_user_name: Bot-User
    git_user_email: bot-user@users.noreply.github.com
""",
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    def fake_agent_select(
        cfg: RuntimeConfig,
        detected_agents: list[str],
        config_path: Path,
        *,
        force_run: bool = False,
    ) -> RuntimeConfig:
        captured["agent_keys"] = list(cfg.agents)
        captured["detected_agents"] = detected_agents
        captured["config_path"] = config_path
        captured["force_run"] = force_run
        return cfg

    monkeypatch.setattr("agentshore.cli.commands.init._interactive_agent_select", fake_agent_select)
    monkeypatch.setattr("agentshore.identity_wizard.run_identity_wizard", lambda *_a, **_k: None)

    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude", "codex", "gemini"]),
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
    ):
        result = runner.invoke(main, ["init", "--project", str(repo), "--force"])

    assert result.exit_code == 0, result.output
    assert captured["force_run"] is True
    assert captured["detected_agents"] == ["claude", "codex", "gemini"]
    assert captured["agent_keys"] == ["claude_code", "codex", "gemini"]
    assert captured["config_path"] == repo / "agentshore.yaml"


def test_agent_setup_wizard_force_run_non_tty_prints_notice(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    cfg = RuntimeConfig(agents={"claude_code": AgentConfig(enabled=True, binary="claude")})
    config_path = tmp_path / "agentshore.yaml"
    config_path.write_text("agents:\n  claude_code:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.delenv("AGENTSHORE_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    returned = _interactive_agent_select(
        cfg,
        ["claude"],
        config_path,
        force_run=True,
    )

    assert returned is cfg
    assert "Agent setup wizard requested but stdin is not a TTY" in capsys.readouterr().out


_FAKE_MODELS = [
    "haiku",
    "sonnet",
    "opus",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.5",
    "flash-lite",
    "auto",
    "pro",
]


def test_agent_setup_wizard_renders_boxes_and_confirms(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Wizard renders a 2-up box grid with letter/number accelerators, exits on Enter."""
    import click

    cfg = RuntimeConfig(agents={"claude_code": AgentConfig(enabled=True, binary="claude")})
    config_path = tmp_path / "agentshore.yaml"
    config_path.write_text(
        "agents:\n  claude_code:\n    enabled: true\n    binary: claude\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("AGENTSHORE_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "agentshore.agents.model_catalog.models_for_agent", lambda *_a, **_k: _FAKE_MODELS
    )
    # Press Enter immediately → confirm & write.
    monkeypatch.setattr(click, "prompt", lambda *_a, **_k: "")

    updated = _interactive_agent_select(
        cfg, ["claude", "codex", "gemini"], config_path, force_run=True
    )

    out = capsys.readouterr().out
    assert "AgentShore — Agent Setup" in out
    # Box frames + letter/number accelerators rendered.
    assert "┌" in out and "│" in out
    assert "[a]" in out  # first tier cell letter
    assert "[1]" in out  # first agent toggle number
    assert "toggle agent" in out and "confirm" in out
    # All three detected agents appear and round-trip into config with binaries.
    for label in ("claude", "codex", "gemini"):
        assert label in out
    assert updated.agents["codex"].binary == "codex"
    assert updated.agents["gemini"].binary == "gemini"


def test_agent_setup_wizard_edit_tier_cell_sets_max(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Pressing a tier letter runs the 3-prompt edit and persists the new max."""
    import beaupy
    import click

    cfg = RuntimeConfig(agents={"claude_code": AgentConfig(enabled=True, binary="claude")})
    config_path = tmp_path / "agentshore.yaml"
    config_path.write_text(
        "agents:\n  claude_code:\n    enabled: true\n    binary: claude\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("AGENTSHORE_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "agentshore.agents.model_catalog.models_for_agent", lambda *_a, **_k: _FAKE_MODELS
    )
    # beaupy.select: enable toggle → "Enable", model picker → first model.
    monkeypatch.setattr(beaupy, "select", lambda options, **_k: options[0])
    monkeypatch.setattr(beaupy, "prompt", lambda *_a, **_k: "")
    # click.prompt sequence: keystroke 'a' (claude·small) → max 7 → Enter (confirm).
    inputs = iter(["a", 7, ""])
    monkeypatch.setattr(click, "prompt", lambda *_a, **_k: next(inputs))

    updated = _interactive_agent_select(cfg, ["claude"], config_path, force_run=True)

    small = updated.agents["claude_code"].model_tiers["small"]
    assert small.enabled is True
    assert small.max == 7
    # Persisted to YAML too.
    saved = config_path.read_text(encoding="utf-8")
    assert "max: 7" in saved


def test_agent_identity_keys_filter_to_detected_enabled_agents(tmp_path: Path) -> None:
    config_path = tmp_path / "agentshore.yaml"
    config_path.write_text(
        """\
agents:
  claude_code:
    enabled: true
    binary: claude
  codex:
    enabled: true
    binary: codex
  gemini:
    enabled: false
    binary: gemini
""",
        encoding="utf-8",
    )

    assert _agent_keys_from_yaml(config_path, detected_agents=["claude", "gemini"]) == [
        "claude_code"
    ]


def test_init_force_resets_database_files_only(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    agentshore_dir = repo / ".agentshore"
    logs_dir = agentshore_dir / "logs"
    logs_dir.mkdir(parents=True)
    for name in ("agentshore.db", "agentshore.db-wal", "agentshore.db-shm"):
        (agentshore_dir / name).write_text("old database content")
    keep_file = logs_dir / "agentshore.log"
    keep_file.write_text("keep me")

    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
    ):
        result = runner.invoke(main, ["init", "--project", str(repo), "--force"])

    assert result.exit_code == 0
    assert not (agentshore_dir / "agentshore.db").exists()
    assert not (agentshore_dir / "agentshore.db-wal").exists()
    assert not (agentshore_dir / "agentshore.db-shm").exists()
    assert keep_file.read_text() == "keep me"
    assert "Reset AgentShore database" in result.output


def test_init_without_force_preserves_database(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    db_path = repo / ".agentshore" / "agentshore.db"
    db_path.parent.mkdir()
    db_path.write_text("old database content")

    runner = CliRunner()
    with (
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
    ):
        result = runner.invoke(main, ["init", "--project", str(repo)])

    assert result.exit_code == 0
    assert db_path.read_text() == "old database content"
    assert "Reset AgentShore database" not in result.output


# ---------------------------------------------------------------------------
# 0agentshore init without --force warns
# ---------------------------------------------------------------------------


def test_init_without_force_preserves_config_and_offers_force(tmp_path: Path) -> None:
    """When agentshore.yaml exists and --force is absent, init re-runs the
    setup wizards without rewriting the file, and points the user at
    ``agentshore init --force`` for a fresh-template merge."""
    repo = _make_git_repo(tmp_path)
    original = "# original config\nbudget:\n  enabled: true\n  total: 1.0\n"
    (repo / "agentshore.yaml").write_text(original)

    runner = CliRunner()
    with (
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
    ):
        result = runner.invoke(main, ["init", "--project", str(repo)])

    assert "agentshore init --force" in result.output
    # Original file should be preserved
    assert (repo / "agentshore.yaml").read_text() == original


# ---------------------------------------------------------------------------
# 0agentshore init --install-skills skips config generation
# ---------------------------------------------------------------------------


def test_init_install_skills_only(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    # Remove agentshore.yaml to verify it's NOT created when --install-skills is used
    (repo / "agentshore.yaml").unlink()

    runner = CliRunner()
    with patch("agentshore.skills.install_skills", return_value=["skill-a"]) as mock_install:
        result = runner.invoke(main, ["init", "--project", str(repo), "--install-skills"])

    assert result.exit_code == 0
    # Config should NOT have been created
    assert not (repo / "agentshore.yaml").exists()
    # Skills should have been installed
    mock_install.assert_called_once_with(repo, force=False)
    assert "skill-a" in result.output


def test_init_install_skills_force_passes_force(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)

    runner = CliRunner()
    with patch("agentshore.skills.install_skills", return_value=["skill-a"]) as mock_install:
        result = runner.invoke(
            main,
            ["init", "--project", str(repo), "--install-skills", "--force"],
        )

    assert result.exit_code == 0
    mock_install.assert_called_once_with(repo, force=True)


def test_init_install_skills_rejects_target_branch(tmp_path: Path) -> None:
    """`--target-branch` with `--install-skills` errors rather than being silently ignored."""
    repo = _make_git_repo(tmp_path)

    runner = CliRunner()
    with patch("agentshore.skills.install_skills", return_value=[]):
        result = runner.invoke(
            main,
            ["init", "--project", str(repo), "--install-skills", "--target-branch", "develop"],
        )

    assert result.exit_code != 0
    assert "--target-branch has no effect with --install-skills" in result.output


# ---------------------------------------------------------------------------
# 8.5. agentshore init manages .gitignore for .agentshore/ runtime artifacts
# ---------------------------------------------------------------------------


def test_init_creates_gitignore_when_missing(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    assert not (repo / ".gitignore").exists()

    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
    ):
        result = runner.invoke(main, ["init", "--project", str(repo), "--force"])

    assert result.exit_code == 0
    contents = (repo / ".gitignore").read_text()
    assert ".agentshore/" in contents
    assert ".agents/" in contents
    assert ".beads/" in contents
    assert "Created" in result.output and ".gitignore" in result.output


def test_init_appends_to_existing_gitignore(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    (repo / ".gitignore").write_text("*.pyc\n__pycache__/\n")

    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
    ):
        result = runner.invoke(main, ["init", "--project", str(repo), "--force"])

    assert result.exit_code == 0
    contents = (repo / ".gitignore").read_text()
    assert "*.pyc" in contents
    assert ".agentshore/" in contents
    assert "Added .agentshore/ to" in result.output


def test_init_idempotent_when_agentshore_already_ignored(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    (repo / ".gitignore").write_text("*.pyc\n.agentshore/\n.agents/\n.beads/\n")
    original = (repo / ".gitignore").read_text()

    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
    ):
        result = runner.invoke(main, ["init", "--project", str(repo), "--force"])

    assert result.exit_code == 0
    assert (repo / ".gitignore").read_text() == original
    assert "Added .agentshore/" not in result.output


def test_init_recognises_agentshore_without_trailing_slash(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    (repo / ".gitignore").write_text(".agentshore\n.agents\n.beads\n")

    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
    ):
        result = runner.invoke(main, ["init", "--project", str(repo), "--force"])

    assert result.exit_code == 0
    contents = (repo / ".gitignore").read_text()
    lines = contents.splitlines()
    # Should not duplicate - .agentshore and .agentshore/ are equivalent for git
    assert lines.count(".agentshore") == 1
    assert lines.count(".agents") == 1
    assert lines.count(".beads") == 1


def test_init_handles_gitignore_without_trailing_newline(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    (repo / ".gitignore").write_text("*.pyc")  # no trailing newline

    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
    ):
        result = runner.invoke(main, ["init", "--project", str(repo), "--force"])

    assert result.exit_code == 0
    contents = (repo / ".gitignore").read_text()
    assert "*.pyc\n" in contents
    assert ".agentshore/" in contents
    assert ".agents/" in contents
    assert ".beads/" in contents


def test_init_skips_gitignore_when_not_a_git_repo(tmp_path: Path) -> None:
    # No .git directory created
    (tmp_path / "agentshore.yaml").write_text("budget:\n  enabled: true\n  total: 20.0\n")

    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.skills.install_skills", return_value=[]),
        patch("agentshore.cli.commands.init._run_beads_init"),
    ):
        result = runner.invoke(main, ["init", "--project", str(tmp_path), "--force"])

    assert result.exit_code == 0
    assert not (tmp_path / ".gitignore").exists()


# ---------------------------------------------------------------------------
# 9. No agents error message includes installation instructions
# ---------------------------------------------------------------------------


def test_no_agents_error_message(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._find_repo_root", return_value=repo),
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=[]),
        patch("agentshore.cli_helpers._detect_api_keys", return_value={}),
    ):
        result = runner.invoke(main, ["start", "--project", str(repo)])

    assert result.exit_code != 0
    stderr = result.stderr
    assert "No coding agents found" in stderr
    assert "npm install -g @anthropic-ai/claude-code" in stderr
    assert "pip install codex-cli" in stderr
    assert "ANTHROPIC_API_KEY" in stderr


# ---------------------------------------------------------------------------
# 10. Invalid YAML error shows line number
# ---------------------------------------------------------------------------


def test_invalid_yaml_error_message(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    bad_yaml = "budget:\n  total: [invalid\n"
    (repo / "agentshore.yaml").write_text(bad_yaml)

    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._find_repo_root", return_value=repo),
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=["claude"]),
        patch("agentshore.cli_helpers._detect_api_keys", return_value={}),
        patch("agentshore.cli.commands.start._run_solo_mode"),  # prevent TUI launch on fallback
    ):
        result = runner.invoke(main, ["start", "--project", str(repo)])

    # The error should mention the YAML problem with a line reference
    stderr = result.stderr
    combined = result.output + stderr
    assert "Invalid YAML" in combined or "line" in combined.lower()


# ---------------------------------------------------------------------------
# 11. Error messages go to stderr
# ---------------------------------------------------------------------------


def test_errors_on_stderr(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path)
    runner = CliRunner()
    with (
        patch("agentshore.cli_helpers._find_repo_root", return_value=repo),
        patch("agentshore.cli_helpers._detect_gh_remote", return_value={"nameWithOwner": "o/r"}),
        patch("agentshore.cli_helpers._detect_agents", return_value=[]),
        patch("agentshore.cli_helpers._detect_api_keys", return_value={}),
    ):
        result = runner.invoke(main, ["start", "--project", str(repo)])

    assert result.exit_code != 0
    # Error text should be on stderr, not stdout
    assert "No coding agents found" in result.stderr
    # stdout should NOT contain the error (it goes to stderr only)
    assert "No coding agents found" not in result.stdout


# ---------------------------------------------------------------------------
# 0agentshore --help lists all subcommands
# ---------------------------------------------------------------------------


def test_all_subcommands_listed() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for cmd in ("start", "init", "identity", "dashboard", "stop"):
        assert cmd in result.output, f"Missing subcommand '{cmd}' in --help"
    # Removed commands' absence is asserted in test_removed_commands_not_registered.
