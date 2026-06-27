"""Shared pytest fixtures for AgentShore tests."""

from __future__ import annotations

import webbrowser
from pathlib import Path

import pytest

from agentshore.state import PlayOutcome, PlayType


@pytest.fixture(autouse=True)
def _prevent_browser_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep report/dashboard tests from opening the user's browser."""
    monkeypatch.setattr(webbrowser, "open", lambda *_args, **_kwargs: True)


@pytest.fixture
def mock_agent_path() -> Path:
    """Return the path to the mock-agent executable.

    The mock agent is a real Python script invoked as a subprocess so that the
    production adapter code-path (subprocess spawn, stream parsing, signalling)
    is exercised end-to-end.  It is reusable by Phase 2 (play tests) and Phase 3
    (PPO loop tests) via this fixture.
    """
    return Path(__file__).parent / "fixtures" / "mock_agent.py"


@pytest.fixture
def sample_play_outcome() -> PlayOutcome:
    """A minimal successful PlayOutcome for tests that need one."""
    return PlayOutcome(
        play_type=PlayType.ISSUE_PICKUP,
        agent_id="agent-1",
        success=True,
        partial=False,
        duration_seconds=1.0,
        token_cost=100,
        dollar_cost=0.01,
        artifacts=[],
        alignment_delta=0.05,
        play_id=1,
    )
