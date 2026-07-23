"""Tests for agentshore.agents.capabilities — agent capability registry."""

from __future__ import annotations

import pytest

from agentshore.agents.capabilities import AGENT_CAPABILITIES, get_capability
from agentshore.state import AgentType


class TestAgentCapabilities:
    """Verify the AGENT_CAPABILITIES registry is complete and correct.

    The five ``can_*`` booleans (``can_implement``, ``can_review``,
    ``can_test``, ``can_create_pr``, ``can_merge``, ``can_run_skill``) were
    removed: every one was ``True`` for every ``AgentType``, so they never
    discriminated capability by provider. ``max_context`` is the only datum
    that ever varied per type.
    """

    def test_all_agent_types_have_capabilities(self) -> None:
        """Every member of AgentType must have an entry in the registry."""
        for agent_type in AgentType:
            assert agent_type in AGENT_CAPABILITIES, (
                f"AgentType.{agent_type.name} missing from AGENT_CAPABILITIES"
            )

    def test_claude_code_max_context(self) -> None:
        caps = AGENT_CAPABILITIES[AgentType.CLAUDE_CODE]
        assert caps["max_context"] == 200_000

    def test_antigravity_max_context(self) -> None:
        caps = AGENT_CAPABILITIES[AgentType.ANTIGRAVITY]
        assert caps["max_context"] == 1_000_000

    def test_codex_max_context(self) -> None:
        caps = AGENT_CAPABILITIES[AgentType.CODEX]
        assert caps["max_context"] == 400_000

    def test_swink_coding_max_context(self) -> None:
        caps = AGENT_CAPABILITIES[AgentType.SWINK_CODING]
        assert caps["max_context"] == 32_768

    def test_get_capability_returns_value(self) -> None:
        """get_capability() returns the correct value for a known key."""
        result = get_capability(AgentType.CLAUDE_CODE, "max_context")
        assert result == 200_000

    def test_get_capability_raises_on_unknown_key(self) -> None:
        """get_capability() raises KeyError for an unknown key."""
        with pytest.raises(KeyError):
            get_capability(AgentType.CLAUDE_CODE, "nonexistent_capability")

    def test_all_capabilities_have_required_keys(self) -> None:
        """Every capability dict must contain the standard keys."""
        required_keys = {"max_context"}
        for agent_type, caps in AGENT_CAPABILITIES.items():
            missing = required_keys - set(caps.keys())
            assert not missing, f"AgentType.{agent_type.name} missing keys: {missing}"
