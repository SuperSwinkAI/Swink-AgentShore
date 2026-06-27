"""Integration tests for AgentManager lifecycle operations."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.agents.handle import AgentHandle
from agentshore.agents.manager import AgentManager
from agentshore.config import RuntimeConfig
from agentshore.data.store import DataStore, SessionRecord
from agentshore.errors import PreconditionFailed
from agentshore.state import AgentStatus, AgentType


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


async def _init_store(tmp_path: Path) -> DataStore:
    db_path = tmp_path / ".agentshore" / "agentshore.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = DataStore(db_path)
    await store.initialize()
    return store


@pytest.mark.asyncio
async def test_agent_lifecycle(tmp_path: Path) -> None:
    """Agent goes through instantiate -> clear lifecycle."""
    store = await _init_store(tmp_path)
    try:
        now = _now_iso()
        await store.create_session(
            SessionRecord(
                session_id="lifecycle-session",
                project_path=str(tmp_path),
                started_at=now,
            )
        )

        manager = AgentManager(
            session_id="lifecycle-session",
            store=store,
            cfg=RuntimeConfig(),
            working_dir=tmp_path,
        )

        handle = await manager.instantiate(AgentType.CLAUDE_CODE)
        agent_id = handle.agent_id
        assert isinstance(handle, AgentHandle)
        assert handle.status == AgentStatus.IDLE
        assert agent_id in manager.handles

        agents = await store.get_agents("lifecycle-session")
        assert len(agents) == 1
        assert agents[0].agent_id == agent_id
        assert agents[0].agent_type == "claude_code"

        handle.context_size = 100_000

        # clear() terminates and removes the agent.
        await manager.clear(agent_id)
        assert agent_id not in manager.handles

        agents_after = await store.get_agents("lifecycle-session")
        assert len(agents_after) == 1
        assert agents_after[0].terminated_at is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_clear_unknown_agent_raises(tmp_path: Path) -> None:
    """Clearing a nonexistent agent_id raises PreconditionFailed."""
    store = await _init_store(tmp_path)
    try:
        now = _now_iso()
        await store.create_session(
            SessionRecord(
                session_id="unknown-session",
                project_path=str(tmp_path),
                started_at=now,
            )
        )

        manager = AgentManager(
            session_id="unknown-session",
            store=store,
            cfg=RuntimeConfig(),
            working_dir=tmp_path,
        )

        with pytest.raises(PreconditionFailed, match="Unknown agent_id"):
            await manager.clear("nonexistent-agent-id")
    finally:
        await store.close()
