"""Tests for the master availability record at ~/.config/swink/agentshore/availability.yaml.

Covers schema round-trip (load/save) and the ``refresh()`` composer.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agentshore import availability
from agentshore.config import (
    AgentTypeAvailability,
    AvailabilityRecord,
    GhAccountAvailability,
)


def _make_record() -> AvailabilityRecord:
    return AvailabilityRecord(
        last_refreshed="2026-05-07T13:00:00+00:00",
        agent_types=(
            AgentTypeAvailability(
                agent_type="claude_code",
                binary="/usr/local/bin/claude",
                available_tiers=("small", "medium", "large"),
                available=True,
            ),
            AgentTypeAvailability(
                agent_type="codex",
                binary=None,
                available_tiers=(),
                available=False,
            ),
        ),
        github_accounts=(
            GhAccountAvailability(login="alice", active=True, token_via="gh_token_login"),
            GhAccountAvailability(login="bob", active=False, token_via="gh_token_login"),
        ),
    )


def test_load_returns_empty_when_file_missing(tmp_path: Path) -> None:
    record = availability.load(tmp_path / "missing.yaml")
    assert record.agent_types == ()
    assert record.github_accounts == ()
    assert record.last_refreshed


def test_save_then_load_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "availability.yaml"
    original = _make_record()
    availability.save(original, path)

    loaded = availability.load(path)
    assert loaded == original


def test_save_creates_parent_dir(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "availability.yaml"
    availability.save(_make_record(), path)
    assert path.exists()


def test_load_handles_malformed_yaml(tmp_path: Path) -> None:
    path = tmp_path / "availability.yaml"
    path.write_text("this is: : : not valid yaml: [")
    record = availability.load(path)
    assert record.agent_types == ()


def test_load_handles_non_dict_root(tmp_path: Path) -> None:
    path = tmp_path / "availability.yaml"
    path.write_text("- just\n- a\n- list\n")
    record = availability.load(path)
    assert record.agent_types == ()


def test_refresh_persists_to_disk(tmp_path: Path) -> None:
    path = tmp_path / "availability.yaml"

    with (
        patch("agentshore.availability.detect_agent_binaries", return_value=("claude",)),
        patch("agentshore.availability.detect_gh_accounts", return_value=[]),
        patch("agentshore.availability.resolve_executable", return_value="/usr/local/bin/claude"),
    ):
        record = availability.refresh(path)

    assert path.exists()
    loaded = availability.load(path)
    assert loaded == record
    assert any(a.agent_type == "claude_code" and a.available for a in record.agent_types)


def test_refresh_marks_undetected_agents_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "availability.yaml"

    with (
        patch("agentshore.availability.detect_agent_binaries", return_value=("claude",)),
        patch("agentshore.availability.detect_gh_accounts", return_value=[]),
        patch("agentshore.availability.resolve_executable", return_value="/usr/local/bin/claude"),
    ):
        record = availability.refresh(path)

    by_type = {a.agent_type: a for a in record.agent_types}
    assert by_type["claude_code"].available is True
    assert by_type["codex"].available is False
    assert by_type["codex"].binary is None
    assert by_type["codex"].available_tiers == ()


def test_refresh_picks_up_gh_accounts(tmp_path: Path) -> None:
    from agentshore.identity_wizard.gh_accounts import GhAccount

    accounts = [
        GhAccount(login="alice", active=True),
        GhAccount(login="bob", active=False),
    ]

    with (
        patch("agentshore.availability.detect_agent_binaries", return_value=()),
        patch("agentshore.availability.detect_gh_accounts", return_value=accounts),
    ):
        record = availability.refresh(tmp_path / "availability.yaml")

    logins = [a.login for a in record.github_accounts]
    assert logins == ["alice", "bob"]
    assert record.github_accounts[0].active is True


@pytest.mark.parametrize(
    "binary, expected_type",
    [
        ("claude", "claude_code"),
        ("codex", "codex"),
        ("grok", "grok"),
        ("grok-build", "grok"),
        ("swink-coding", "swink_coding"),
    ],
)
def test_binary_to_agent_type_mapping(tmp_path: Path, binary: str, expected_type: str) -> None:
    """All known CLI binaries map to a known AgentType."""
    with (
        patch("agentshore.availability.detect_agent_binaries", return_value=(binary,)),
        patch("agentshore.availability.detect_gh_accounts", return_value=[]),
        patch("agentshore.availability.resolve_executable", return_value=f"/usr/bin/{binary}"),
    ):
        record = availability.refresh(tmp_path / "availability.yaml")

    by_type = {a.agent_type: a for a in record.agent_types}
    assert by_type[expected_type].available is True


def test_grok_binary_preferred_over_grok_build(tmp_path: Path) -> None:
    def resolve(binary: str) -> str:
        return f"/usr/bin/{binary}"

    with (
        patch("agentshore.availability.detect_agent_binaries", return_value=("grok", "grok-build")),
        patch("agentshore.availability.detect_gh_accounts", return_value=[]),
        patch("agentshore.availability.resolve_executable", side_effect=resolve),
    ):
        record = availability.refresh(tmp_path / "availability.yaml")

    by_type = {a.agent_type: a for a in record.agent_types}
    assert by_type["grok"].available is True
    assert by_type["grok"].binary == "/usr/bin/grok"
