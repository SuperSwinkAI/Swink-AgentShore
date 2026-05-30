"""Tests for the non-destructive _render_or_merge_agentshore_yaml writer.

Regression for the 2026-05-07 init-wizard bug: `init --force` previously
overwrote agentshore.yaml wholesale, wiping user-edited budget/intake/scope/
identities. The merge writer replaces only the `agents:` skeleton.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agentshore.cli_helpers import _render_or_merge_agentshore_yaml


def test_writes_fresh_template_when_path_missing(tmp_path: Path) -> None:
    path = tmp_path / "agentshore.yaml"

    written = _render_or_merge_agentshore_yaml(
        path,
        name_with_owner="owner/repo",
        agents=["claude"],
        budget=200.0,
        strict=False,
    )

    assert written is True
    assert path.exists()
    data = yaml.safe_load(path.read_text())
    assert "agents" in data
    assert "claude_code" in data["agents"]


def test_writes_empty_agents_when_no_cli_agents_detected(tmp_path: Path) -> None:
    path = tmp_path / "agentshore.yaml"

    written = _render_or_merge_agentshore_yaml(
        path,
        name_with_owner="owner/repo",
        agents=[],
        budget=200.0,
        strict=False,
    )

    assert written is True
    data = yaml.safe_load(path.read_text())
    assert data["agents"] == {}


def test_preserves_user_edited_top_level_keys(tmp_path: Path) -> None:
    """budget, intake, scope, identities must survive the merge."""
    path = tmp_path / "agentshore.yaml"
    existing = """\
project:
  path: .
budget:
  enabled: true
  total: 999.0
intake:
  seed_paths:
    - my_custom_seed.md
identities:
  jane:
    git_user_name: Jane Doe
    git_user_email: jane@example.com
    gh_token_login: jane
agents:
  claude_code:
    enabled: true
    binary: claude
    identity: jane
"""
    path.write_text(existing)

    _render_or_merge_agentshore_yaml(
        path,
        name_with_owner="owner/repo",
        agents=["claude"],
        budget=50.0,  # different from the user's 999
        strict=True,
    )

    merged = yaml.safe_load(path.read_text())
    # User keys preserved verbatim
    assert merged["budget"]["total"] == 999.0
    assert merged["intake"]["seed_paths"] == ["my_custom_seed.md"]
    assert merged["identities"]["jane"]["git_user_email"] == "jane@example.com"
    # Agents skeleton was re-rendered (still has claude_code, identity binding lost
    # because we replace the whole `agents:` block — that's expected)
    assert "claude_code" in merged["agents"]


def test_preserves_unknown_user_keys(tmp_path: Path) -> None:
    """User-added keys outside the schema also survive."""
    path = tmp_path / "agentshore.yaml"
    path.write_text(
        "my_custom_section:\n  some_key: some_value\nagents:\n  claude_code:\n    enabled: true\n"
    )

    _render_or_merge_agentshore_yaml(
        path,
        name_with_owner="owner/repo",
        agents=["claude"],
        budget=200.0,
        strict=False,
    )

    merged = yaml.safe_load(path.read_text())
    assert merged["my_custom_section"]["some_key"] == "some_value"


def test_preserves_comments(tmp_path: Path) -> None:
    """ruamel.yaml round-trip keeps comments on user-edited sections."""
    path = tmp_path / "agentshore.yaml"
    path.write_text(
        "# Top-level comment that must survive\n"
        "budget:\n"
        "  # inline comment in budget\n"
        "  enabled: true\n"
        "  total: 999.0\n"
        "agents:\n"
        "  claude_code:\n"
        "    enabled: true\n"
    )

    _render_or_merge_agentshore_yaml(
        path,
        name_with_owner="owner/repo",
        agents=["claude"],
        budget=50.0,
        strict=False,
    )

    text = path.read_text()
    assert "Top-level comment that must survive" in text
    assert "inline comment in budget" in text
