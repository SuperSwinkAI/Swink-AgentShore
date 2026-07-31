"""Tests for the per-play tool-denial policy (``Play.disallowed_tools``).

These guard the *policy* — which plays deny which tools — rather than the argv
plumbing that carries it (``tests/test_cli_swink_coding.py``) or the dispatch
threading (``tests/test_cli_agent.py``).
"""

from __future__ import annotations

import pytest

from agentshore.plays.registry import build_default_registry
from agentshore.state import PlayType

# The swink-coding CLI's full native tool surface, as reported by its own
# startup validator (``--disallowed-tools <unknown>`` prints the valid set).
# Denying a name outside this set exits the dispatch non-zero at startup, so a
# typo is a wedged play, not a silently-ignored policy.
_KNOWN_CLI_TOOLS = frozenset(
    {
        "bash",
        "shell",  # alias of bash
        "edit_file",
        "gh",
        "git",
        "list_files",
        "read_file",
        "search",
        "subagent",
        "write_file",
    }
)


def _play(play_type: PlayType):
    return build_default_registry().get(play_type)


def test_code_review_denies_the_file_mutation_tools() -> None:
    """Anti-confirmation at the tool layer: a reviewer must not be able to
    quietly fix what it was sent to judge."""
    assert set(_play(PlayType.CODE_REVIEW).disallowed_tools) == {"write_file", "edit_file"}


@pytest.mark.parametrize("tool", ["gh", "git", "read_file", "bash"])
def test_code_review_keeps_the_tools_it_needs_to_do_its_job(tool: str) -> None:
    """A review is posted with ``gh``, reads the diff with ``git``/``read_file``,
    and may legitimately run a command to check a claim. Denying any of these
    would break the play rather than harden it.
    """
    assert tool not in _play(PlayType.CODE_REVIEW).disallowed_tools


def test_every_declared_denial_names_a_real_cli_tool() -> None:
    """Unknown tool names fail fast at the CLI's startup, so a rename upstream
    (or a typo here) must surface as a test failure, not a dead policy."""
    registry = build_default_registry()
    for play_type in registry.covered():
        for tool in registry.get(play_type).disallowed_tools:
            assert tool in _KNOWN_CLI_TOOLS, f"{play_type.value} denies unknown tool {tool!r}"


def test_code_review_is_the_only_play_denying_anything() -> None:
    """Scope guard. Every other play gets the agent's full tool surface; adding
    a denial elsewhere is a deliberate policy decision and should show up here
    as a failing assertion rather than riding along unnoticed.
    """
    registry = build_default_registry()
    denying = {
        play_type.value
        for play_type in registry.covered()
        if registry.get(play_type).disallowed_tools
    }
    assert denying == {"code_review"}


def test_internal_plays_declare_no_denials() -> None:
    """Internal plays never dispatch to a CLI, so there is no tool surface to
    deny — the attribute exists only to keep the structural ``Play`` match."""
    registry = build_default_registry()
    for play_type in (PlayType.INSTANTIATE_AGENT, PlayType.END_AGENT, PlayType.TAKE_BREAK):
        assert registry.get(play_type).disallowed_tools == ()
