"""Tests for agentshore.agents.capabilities — agent capability registry."""

from __future__ import annotations

from agentshore.agents.capabilities import AGENT_CAPABILITIES, get_capability
from agentshore.state import AgentType


class TestAgentCapabilities:
    """Verify the AGENT_CAPABILITIES registry is complete and correct."""

    def test_all_agent_types_have_capabilities(self) -> None:
        """Every member of AgentType must have an entry in the registry."""
        for agent_type in AgentType:
            assert agent_type in AGENT_CAPABILITIES, (
                f"AgentType.{agent_type.name} missing from AGENT_CAPABILITIES"
            )

    def test_claude_code_capabilities(self) -> None:
        """Claude Code should have full capabilities including merge."""
        caps = AGENT_CAPABILITIES[AgentType.CLAUDE_CODE]
        assert caps["can_implement"] is True
        assert caps["can_review"] is True
        assert caps["can_test"] is True
        assert caps["can_create_pr"] is True
        assert caps["can_merge"] is True
        assert caps["can_run_skill"] is True

    def test_antigravity_can_create_pr_and_merge(self) -> None:
        caps = AGENT_CAPABILITIES[AgentType.ANTIGRAVITY]
        assert caps["can_create_pr"] is True
        assert caps["can_merge"] is True

    def test_codex_can_create_pr_and_merge(self) -> None:
        caps = AGENT_CAPABILITIES[AgentType.CODEX]
        assert caps["can_create_pr"] is True
        assert caps["can_merge"] is True

    def test_swink_coding_capabilities(self) -> None:
        caps = AGENT_CAPABILITIES[AgentType.SWINK_CODING]
        assert caps["can_implement"] is True
        assert caps["can_review"] is True
        assert caps["can_test"] is True
        assert caps["can_create_pr"] is True
        assert caps["can_merge"] is True
        assert caps["can_run_skill"] is True
        assert caps["max_context"] == 32_768

    def test_all_agent_types_can_merge(self) -> None:
        """Merge eligibility is independent of provider type."""
        for agent_type, caps in AGENT_CAPABILITIES.items():
            assert caps["can_merge"] is True, f"{agent_type.name} should be able to merge"

    def test_get_capability_returns_value(self) -> None:
        """get_capability() returns the correct value for a known key."""
        result = get_capability(AgentType.CLAUDE_CODE, "can_merge")
        assert result is True

    def test_get_capability_raises_on_unknown_key(self) -> None:
        """get_capability() raises KeyError for an unknown key."""
        import pytest

        with pytest.raises(KeyError):
            get_capability(AgentType.CLAUDE_CODE, "nonexistent_capability")

    def test_all_capabilities_have_required_keys(self) -> None:
        """Every capability dict must contain the standard keys."""
        required_keys = {
            "can_implement",
            "can_review",
            "can_test",
            "can_create_pr",
            "can_merge",
            "can_run_skill",
            "max_context",
        }
        for agent_type, caps in AGENT_CAPABILITIES.items():
            missing = required_keys - set(caps.keys())
            assert not missing, f"AgentType.{agent_type.name} missing keys: {missing}"
