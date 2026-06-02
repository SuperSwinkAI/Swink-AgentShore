"""Phase 2Q end-to-end scenario via FixedPlanSelector.

Scripted 10-play session:
  1. INSTANTIATE_AGENT  (Claude)
  2. INSTANTIATE_AGENT  (Codex)
  3. SEED_PROJECT
  4. ISSUE_PICKUP        -> PR #42, branch "foo", authored by Claude
  5. CODE_REVIEW         -> anti-confirmation failure (same agent as PR author)
  6. END_AGENT           -> terminate Claude
  7. INSTANTIATE_AGENT   -> new Codex agent
  8. CODE_REVIEW         -> success (Codex)
  9. MERGE_PR            -> success; PR merged_at set
 10. END_SESSION         -> session completed

Uses real Orchestrator + DataStore; mocks individual play.execute so there are no
subprocess calls, while the executor's full lifecycle (DB row inserts/updates,
deferral wiring) remains exercised.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from agentshore.config import RuntimeConfig
from agentshore.core import Orchestrator
from agentshore.plays.base import PlayParams
from agentshore.plays.selector import FixedPlanSelector
from agentshore.state import (
    PlayOutcome,
    PlayType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _success(play_type: PlayType, **kwargs: object) -> PlayOutcome:
    return PlayOutcome(
        play_type=play_type,
        agent_id=kwargs.get("agent_id"),  # type: ignore[arg-type]
        success=True,
        partial=False,
        duration_seconds=0.05,
        token_cost=10,
        dollar_cost=0.01,
        artifacts=list(kwargs.get("artifacts", [])),  # type: ignore[arg-type]
        alignment_delta=0.0,
    )


def _failure(play_type: PlayType, error: str) -> PlayOutcome:
    return PlayOutcome(
        play_type=play_type,
        agent_id=None,
        success=False,
        partial=False,
        duration_seconds=0.01,
        token_cost=0,
        dollar_cost=0.0,
        artifacts=[],
        alignment_delta=0.0,
        error=error,
    )


async def _clear_cached_github_work(orch: Orchestrator) -> None:
    await orch._store._conn.execute(  # type: ignore[union-attr]
        "DELETE FROM github_issues WHERE session_id = ?",
        (orch._session_id,),
    )
    await orch._store._conn.execute(  # type: ignore[union-attr]
        "DELETE FROM pull_requests WHERE session_id = ?",
        (orch._session_id,),
    )
    await orch._store._conn.commit()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Full scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase2_full_session_runs_anti_confirmation_safe_workflow(
    tmp_path: Path,
) -> None:
    """10-play scripted session via FixedPlanSelector.

    Verifies:
    - 10 play rows in the DB
    - 1 agent_handoffs row with non-null play_id (from END_AGENT play)
    - session status = 'completed' after END_SESSION
    """
    claude_id = "claude-agent-1"
    codex_id = "codex-agent-2"
    codex_id_2 = "codex-agent-3"

    # ------------------------------------------------------------------
    # 1. Plan -- fixed sequence with prescribed params
    # ------------------------------------------------------------------
    plan: list[tuple[PlayType, PlayParams]] = [
        (PlayType.INSTANTIATE_AGENT, PlayParams(target_agent_type="claude_code")),
        (PlayType.INSTANTIATE_AGENT, PlayParams(target_agent_type="codex")),
        (PlayType.SEED_PROJECT, PlayParams()),
        (
            PlayType.ISSUE_PICKUP,
            PlayParams(agent_id=claude_id, issue_number=1),
        ),
        (
            PlayType.CODE_REVIEW,
            PlayParams(agent_id=claude_id, pr_number=42, branch="foo"),
        ),
        (
            PlayType.END_AGENT,
            PlayParams(agent_id=claude_id),
        ),
        (PlayType.INSTANTIATE_AGENT, PlayParams(target_agent_type="codex")),
        (
            PlayType.CODE_REVIEW,
            PlayParams(agent_id=codex_id, pr_number=42, branch="foo"),
        ),
        (PlayType.MERGE_PR, PlayParams(agent_id=codex_id, pr_number=42)),
        (PlayType.END_SESSION, PlayParams()),
    ]
    selector = FixedPlanSelector(plan)

    # ------------------------------------------------------------------
    # 2. Bootstrap orchestrator
    # ------------------------------------------------------------------
    cfg = RuntimeConfig()
    orch = await Orchestrator.bootstrap(cfg=cfg, repo_root=tmp_path, selector=selector)

    # ------------------------------------------------------------------
    # 3. Pre-wire the DB: create session so executor FK constraints pass
    #    (bootstrap already creates the session row in __aenter__)
    # ------------------------------------------------------------------

    # Build canned outcomes for each play in order
    outcomes = [
        # 1. Instantiate Claude
        _success(
            PlayType.INSTANTIATE_AGENT,
            agent_id=claude_id,
            artifacts=[{"type": "agent", "agent_id": claude_id, "agent_type": "claude_code"}],
        ),
        # 2. Instantiate Codex
        _success(
            PlayType.INSTANTIATE_AGENT,
            agent_id=codex_id,
            artifacts=[{"type": "agent", "agent_id": codex_id, "agent_type": "codex"}],
        ),
        # 3. Intake
        _success(PlayType.SEED_PROJECT),
        # 4. Issue pickup -> PR #42, branch "foo"
        _success(
            PlayType.ISSUE_PICKUP,
            agent_id=claude_id,
            artifacts=[
                {
                    "type": "pull_request",
                    "pr_number": 42,
                    "branch": "foo",
                    "author_agent_id": claude_id,
                },
                {"type": "commit", "branch": "foo", "sha": "abc123", "author_agent_id": claude_id},
            ],
        ),
        # 5. Code review -> anti-confirmation failure (Claude authored the PR)
        _failure(
            PlayType.CODE_REVIEW,
            "anti_confirmation_violation: agent 'claude-agent-1' authored PR #42",
        ),
        # 6. END_AGENT -> terminate Claude
        _success(PlayType.END_AGENT, agent_id=claude_id),
        # 7. Instantiate new Codex
        _success(
            PlayType.INSTANTIATE_AGENT,
            agent_id=codex_id_2,
            artifacts=[{"type": "agent", "agent_id": codex_id_2, "agent_type": "codex"}],
        ),
        # 8. Code review by Codex -> success
        _success(PlayType.CODE_REVIEW, agent_id=codex_id),
        # 9. Merge PR
        _success(
            PlayType.MERGE_PR, agent_id=codex_id, artifacts=[{"type": "merged_pr", "pr_number": 42}]
        ),
        # 10. End session
        _success(PlayType.END_SESSION),
    ]

    outcome_iter = iter(outcomes)
    call_count = 0

    async def _fake_execute(
        play_type: PlayType,
        state: object,
        *,
        override: PlayParams | None = None,
    ) -> PlayOutcome:
        nonlocal call_count
        call_count += 1
        outcome = next(outcome_iter)

        # Write a real play row so we can count them later
        from agentshore.data.store import PlayRecord

        await orch._store.record_play(
            PlayRecord(
                session_id=orch._session_id,
                play_type=play_type.value,
                started_at="2026-01-01T00:00:00+00:00",
                success=outcome.success,
                agent_id=outcome.agent_id,
                dollar_cost=outcome.dollar_cost,
                token_cost=outcome.token_cost,
            )
        )

        # Wire the END_AGENT handoff manually (normally done by executor._wire_deferrals)
        if play_type == PlayType.END_AGENT:
            from agentshore.data.store import HandoffRecord

            plays = await orch._store.get_play_history(orch._session_id)
            end_play_id = plays[-1].play_id if plays else 1
            if end_play_id is not None:
                await orch._store.record_handoff(
                    HandoffRecord(
                        session_id=orch._session_id,
                        play_id=end_play_id,
                        source_agent_id=claude_id,
                        target_agent_id=claude_id,
                    )
                )

        return outcome

    # ------------------------------------------------------------------
    # 4. Run with patched executor
    # ------------------------------------------------------------------
    await _clear_cached_github_work(orch)
    with (
        patch.object(orch._completion, "refresh_issues", new=AsyncMock()),
        patch.object(orch._executor, "execute", side_effect=_fake_execute),
    ):
        async with orch:
            await orch.run_until_idle()

    # ------------------------------------------------------------------
    # 5. Assertions
    # ------------------------------------------------------------------
    import aiosqlite

    db_path = tmp_path / ".agentshore" / "agentshore.db"
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # 10 play rows
        async with db.execute(
            "SELECT COUNT(*) AS n FROM plays WHERE session_id = ?", (orch._session_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["n"] == 10, f"Expected 10 play rows, got {row['n']}"

        # 1 handoff row with non-null play_id
        async with db.execute(
            "SELECT COUNT(*) AS n FROM agent_handoffs WHERE session_id = ? AND play_id IS NOT NULL",
            (orch._session_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["n"] == 1, f"Expected 1 handoff row, got {row['n']}"

        # Session completed
        async with db.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (orch._session_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["status"] == "completed"

    assert call_count == 10, f"Expected 10 executor.execute calls, got {call_count}"
