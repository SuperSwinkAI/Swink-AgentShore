"""Tests for the ``identities:`` block and per-agent ``identity:`` field."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.config import GitHubIdentity, load_config
from agentshore.errors import ConfigError

_BASE_AGENTS_YAML = """\
agents:
  claude_code:
    enabled: true
    binary: claude
  codex:
    enabled: true
    binary: codex
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "agentshore.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_identities_block_parses(tmp_path: Path) -> None:
    yaml_text = (
        _BASE_AGENTS_YAML
        + """
identities:
  example-user:
    git_user_name: "Wes Eyer"
    git_user_email: "user@example.com"
    gh_token_login: example-user
  unseriousAI:
    git_user_name: "unseriousAI"
    git_user_email: "bot@example.com"
    gh_token_env: UNSERIOUSAI_GH_TOKEN
    ssh_key_path: ~/.ssh/id_unseriousai
"""
    )
    cfg = load_config(_write(tmp_path, yaml_text))

    assert set(cfg.identities) == {"example-user", "unseriousai"}
    jw = cfg.identities["example-user"]
    assert isinstance(jw, GitHubIdentity)
    assert jw.git_user_email == "user@example.com"
    assert jw.gh_token_login == "example-user"
    assert jw.gh_token_env is None

    bot = cfg.identities["unseriousai"]
    assert bot.gh_token_env == "UNSERIOUSAI_GH_TOKEN"
    assert bot.gh_token_login is None
    assert bot.ssh_key_path == "~/.ssh/id_unseriousai"


def test_agent_identity_field_links_to_identities_block(tmp_path: Path) -> None:
    yaml_text = """\
agents:
  claude_code:
    enabled: true
    binary: claude
    identity: example-user
  codex:
    enabled: true
    binary: codex
    identity: unseriousAI

identities:
  example-user:
    git_user_name: "Wes Eyer"
    git_user_email: "user@example.com"
    gh_token_login: example-user
  unseriousAI:
    git_user_name: "unseriousAI"
    git_user_email: "bot@example.com"
    gh_token_env: UNSERIOUSAI_GH_TOKEN
"""
    cfg = load_config(_write(tmp_path, yaml_text))
    assert cfg.agents["claude_code"].identity == "example-user"
    assert cfg.agents["codex"].identity == "unseriousai"


def test_identity_keys_and_agent_refs_are_casefolded(tmp_path: Path) -> None:
    yaml_text = """\
agents:
  codex:
    enabled: true
    binary: codex
    identity: unseriousAI

identities:
  unseriousAI:
    git_user_name: "unseriousAI"
    git_user_email: "bot@example.com"
    gh_token_keychain: agentshore/unseriousAI
"""
    cfg = load_config(_write(tmp_path, yaml_text))
    assert set(cfg.identities) == {"unseriousai"}
    assert cfg.agents["codex"].identity == "unseriousai"
    assert cfg.identities["unseriousai"].git_user_name == "unseriousAI"


def test_duplicate_casefolded_identity_keys_raise(tmp_path: Path) -> None:
    yaml_text = """\
agents:
  codex:
    enabled: true
    binary: codex

identities:
  unseriousAI:
    git_user_name: "unseriousAI"
    git_user_email: "bot@example.com"
  unseriousai:
    git_user_name: "lower"
    git_user_email: "lower@example.com"
"""
    with pytest.raises(ConfigError, match="duplicate case-insensitive key"):
        load_config(_write(tmp_path, yaml_text))


def test_unknown_identity_reference_raises(tmp_path: Path) -> None:
    yaml_text = """\
agents:
  claude_code:
    enabled: true
    binary: claude
    identity: ghost

identities:
  example-user:
    git_user_name: "Wes Eyer"
    git_user_email: "user@example.com"
"""
    with pytest.raises(ConfigError, match="references an unknown identity"):
        load_config(_write(tmp_path, yaml_text))


def test_both_token_sources_set_raises(tmp_path: Path) -> None:
    yaml_text = (
        _BASE_AGENTS_YAML
        + """
identities:
  example-user:
    git_user_name: "Wes Eyer"
    git_user_email: "user@example.com"
    gh_token_env: GH_TOKEN
    gh_token_login: example-user
"""
    )
    with pytest.raises(ConfigError, match="at most one of gh_token_env"):
        load_config(_write(tmp_path, yaml_text))


def test_missing_required_identity_fields_raise(tmp_path: Path) -> None:
    yaml_text = (
        _BASE_AGENTS_YAML
        + """
identities:
  bad:
    git_user_email: ""
    git_user_name: ""
"""
    )
    with pytest.raises(ConfigError, match="must be a non-empty string"):
        load_config(_write(tmp_path, yaml_text))


def test_identities_default_to_empty(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _BASE_AGENTS_YAML))
    assert cfg.identities == {}
    assert cfg.agents["claude_code"].identity is None


def test_ssh_key_path_benign_value_parses(tmp_path: Path) -> None:
    """A normal ``~/.ssh/...`` path passes validation and is preserved verbatim."""
    yaml_text = (
        _BASE_AGENTS_YAML
        + """
identities:
  bot:
    git_user_name: "bot"
    git_user_email: "bot@example.com"
    ssh_key_path: ~/.ssh/id_ed25519
"""
    )
    cfg = load_config(_write(tmp_path, yaml_text))
    # Validator must not rewrite the stored path (agentshore.yaml is shared
    # across machines and ``~`` expands per-host at dispatch time).
    assert cfg.identities["bot"].ssh_key_path == "~/.ssh/id_ed25519"


@pytest.mark.parametrize(
    ("bad_path", "label"),
    [
        ("~/.ssh/id; rm -rf /", "command separator"),
        ("~/.ssh/id$(whoami)", "command substitution"),
        ("~/.ssh/id with space", "whitespace"),
    ],
)
def test_ssh_key_path_rejects_shell_metacharacters(
    tmp_path: Path, bad_path: str, label: str
) -> None:
    """Shell metacharacters and whitespace are rejected at config parse time.

    The path is interpolated into ``GIT_SSH_COMMAND`` at agent dispatch time,
    so anything that breaks ``ssh -i <path> -o IdentitiesOnly=yes`` —
    whether by injection or by argument splitting — must fail fast at load.
    """
    del label  # purely descriptive; surfaces in the parametrize id
    yaml_text = (
        _BASE_AGENTS_YAML
        + f"""
identities:
  bot:
    git_user_name: "bot"
    git_user_email: "bot@example.com"
    ssh_key_path: "{bad_path}"
"""
    )
    with pytest.raises(ConfigError, match="ssh_key_path contains disallowed character"):
        load_config(_write(tmp_path, yaml_text))


def test_identities_hot_reload_swaps_binding(tmp_path: Path) -> None:
    """Re-loading the same path after the file changes picks up the new
    identity binding — the contract Orchestrator._reload_config relies on."""
    path = tmp_path / "agentshore.yaml"

    v1 = """\
agents:
  claude_code:
    enabled: true
    binary: claude
    identity: example-user
identities:
  example-user:
    git_user_name: "Wes"
    git_user_email: "user@example.com"
    gh_token_login: example-user
"""
    path.write_text(v1, encoding="utf-8")
    cfg_v1 = load_config(path)
    assert cfg_v1.agents["claude_code"].identity == "example-user"
    assert "unseriousAI" not in cfg_v1.identities

    v2 = """\
agents:
  claude_code:
    enabled: true
    binary: claude
    identity: unseriousAI
identities:
  unseriousAI:
    git_user_name: "unseriousAI"
    git_user_email: "bot@example.com"
    gh_token_env: UNSERIOUSAI_GH_TOKEN
"""
    path.write_text(v2, encoding="utf-8")
    cfg_v2 = load_config(path)
    assert cfg_v2.agents["claude_code"].identity == "unseriousai"
    assert "example-user" not in cfg_v2.identities
    assert cfg_v2.identities["unseriousai"].gh_token_env == "UNSERIOUSAI_GH_TOKEN"
