"""Tests for DataStore persistence contracts."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from agentshore.data.store import (
    AgentRecord,
    DataStore,
    ExperienceRecord,
    ExternalMutationRecord,
    GitHubIssueRecord,
    HandoffRecord,
    PlayRecord,
    PullRequestRecord,
    SessionRecord,
)


@pytest.mark.asyncio
async def test_play_artifacts_preserve_structured_objects(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await store.create_session(
            SessionRecord(
                session_id="session-1",
                project_path=str(tmp_path),
                started_at="2026-04-27T00:00:00+00:00",
            )
        )
        artifact = {
            "type": "pull_request",
            "number": 47,
            "url": "https://github.com/org/repo/pull/47",
        }

        await store.record_play(
            PlayRecord(
                session_id="session-1",
                play_type="issue_pickup",
                started_at="2026-04-27T00:01:00+00:00",
                success=True,
                artifacts=[artifact, "merge-sha"],
            )
        )

        history = await store.get_play_history("session-1")

        assert len(history) == 1
        assert history[0].artifacts == [artifact, "merge-sha"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_abandon_unfinished_plays_closes_open_rows(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        play_id = await store.record_play(
            PlayRecord(
                session_id="s1",
                play_type="unblock_pr",
                started_at="2026-04-27T00:01:00+00:00",
                success=False,
            )
        )

        await store.abandon_unfinished_plays("s1", reason="test recovery")

        history = await store.get_play_history("s1")
        play = next(p for p in history if p.play_id == play_id)
        assert play.ended_at is not None
        assert play.failure_category == "abandoned"
        assert play.error == "test recovery"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_abandon_work_for_missing_agents_leaves_live_agent_work(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        live_group = await store.acquire_work_claims(
            "s1",
            "merge_pr",
            ["pr:41"],
            status="running",
            agent_id="live-agent",
        )
        dead_group = await store.acquire_work_claims(
            "s1",
            "unblock_pr",
            ["pr:42"],
            status="running",
            agent_id="dead-agent",
        )
        live_play_id = await store.record_play(
            PlayRecord(
                session_id="s1",
                play_type="merge_pr",
                started_at="2026-04-27T00:01:00+00:00",
                success=False,
                agent_id="live-agent",
            )
        )
        dead_play_id = await store.record_play(
            PlayRecord(
                session_id="s1",
                play_type="unblock_pr",
                started_at="2026-04-27T00:02:00+00:00",
                success=False,
                agent_id="dead-agent",
            )
        )

        counts = await store.abandon_work_for_missing_agents(
            "s1",
            {"live-agent"},
            reason="missing agent",
        )

        assert counts == (1, 1)
        assert live_group is not None
        assert dead_group is not None
        live_claims = await store.get_work_claim_group("s1", live_group)
        dead_claims = await store.get_work_claim_group("s1", dead_group)
        assert [claim.status for claim in live_claims] == ["running"]
        assert [claim.status for claim in dead_claims] == ["abandoned"]
        history = await store.get_play_history("s1")
        live_play = next(p for p in history if p.play_id == live_play_id)
        dead_play = next(p for p in history if p.play_id == dead_play_id)
        assert live_play.ended_at is None
        assert dead_play.ended_at is not None
        assert dead_play.failure_category == "abandoned"
        assert dead_play.error == "missing agent"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_release_active_work_claims_for_agents_releases_only_matching_agents(
    tmp_path,
) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        idle_group = await store.acquire_work_claims(
            "s1",
            "issue_pickup",
            ["issue:101"],
            status="running",
            agent_id="idle-agent",
        )
        busy_group = await store.acquire_work_claims(
            "s1",
            "issue_pickup",
            ["issue:102"],
            status="running",
            agent_id="busy-agent",
        )

        active = await store.find_active_work_claims_for_agents("s1", {"idle-agent"})
        assert [claim.resource_key for claim in active] == ["issue:101"]

        released = await store.release_active_work_claims_for_agents("s1", {"idle-agent"})

        assert released == 1
        assert idle_group is not None
        assert busy_group is not None
        idle_claims = await store.get_work_claim_group("s1", idle_group)
        busy_claims = await store.get_work_claim_group("s1", busy_group)
        assert [claim.status for claim in idle_claims] == ["released"]
        assert [claim.status for claim in busy_claims] == ["running"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_add_pull_request_labels_preserves_existing_labels(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        await store.record_pull_request(
            PullRequestRecord(
                pr_number=42,
                session_id="s1",
                state="open",
                created_at="2026-04-27T00:00:00+00:00",
                labels=["agentshore/approved"],
            )
        )

        await store.add_pull_request_labels("s1", 42, ["agentshore/manual-required"])

        pr = await store.get_pull_request("s1", 42)
        assert pr is not None
        assert pr.labels == ["agentshore/approved", "agentshore/manual-required"]
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup_session(store: DataStore, tmp_path: object) -> None:
    await store.create_session(
        SessionRecord(
            session_id="s1",
            project_path=str(tmp_path),
            started_at="2026-04-27T00:00:00+00:00",
        )
    )


@pytest.mark.asyncio
async def test_work_claims_allow_one_active_claim_per_resource(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)

        first = await store.acquire_work_claims("s1", "merge_pr", ["pr:210"])
        second = await store.acquire_work_claims("s1", "unblock_pr", ["pr:210"])

        assert isinstance(first, str)
        assert second is None
        active = await store.find_active_work_claims("s1", ["pr:210"])
        assert [claim.claim_group_id for claim in active] == [first]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_work_claim_release_allows_reclaim(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)

        first = await store.acquire_work_claims("s1", "issue_pickup", ["issue:195"])
        assert isinstance(first, str)
        await store.release_work_claim_group("s1", first)

        second = await store.acquire_work_claims("s1", "write_implementation_plan", ["issue:195"])

        assert isinstance(second, str)
        active = await store.find_active_work_claims("s1", ["issue:195"])
        assert [claim.claim_group_id for claim in active] == [second]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_work_claim_multi_resource_conflict_rolls_back(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)

        existing = await store.acquire_work_claims("s1", "issue_pickup", ["issue:195"])
        assert isinstance(existing, str)
        failed = await store.acquire_work_claims("s1", "merge_pr", ["pr:210", "issue:195"])

        assert failed is None
        assert await store.find_active_work_claims("s1", ["pr:210"]) == []
        active_issue = await store.find_active_work_claims("s1", ["issue:195"])
        assert [claim.claim_group_id for claim in active_issue] == [existing]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_dispatch_replay_round_trip_and_retry_counter(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        claim_group = await store.acquire_work_claims("s1", "issue_pickup", ["issue:201"])
        assert isinstance(claim_group, str)

        await store.save_dispatch_replay(
            session_id="s1",
            claim_group_id=claim_group,
            play_id=42,
            skill_name="agentshore-issue-pickup",
            params_json='{"issue_number":201,"extras":{"claim_group_id":"x"}}',
            prompt="verbatim prompt",
            branch="agentshore/201-timeout",
        )
        replay = await store.get_dispatch_replay(
            session_id="s1",
            claim_group_id=claim_group,
            play_id=42,
        )
        assert replay is not None
        assert replay.prompt == "verbatim prompt"
        assert replay.branch == "agentshore/201-timeout"
        assert replay.skill_name == "agentshore-issue-pickup"
        assert replay.params_json == '{"issue_number":201,"extras":{"claim_group_id":"x"}}'

        assert await store.get_work_claim_retry_attempts("s1", claim_group) == 0
        assert await store.increment_work_claim_retry("s1", claim_group) == 1
        assert await store.increment_work_claim_retry("s1", claim_group) == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_agent_terminated_timestamp_is_persisted(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        await store.register_agent(
            AgentRecord(agent_id="a1", session_id="s1", agent_type="claude_code", created_at="T0")
        )
        await store.update_agent_terminated("a1", "T1")
        # Verify via a direct query — no high-level reader yet
        async with store._conn.execute(
            "SELECT terminated_at FROM agents WHERE agent_id = 'a1'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["terminated_at"] == "T1"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_increment_agent_tasks(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        await store.register_agent(
            AgentRecord(agent_id="a1", session_id="s1", agent_type="claude_code", created_at="T0")
        )
        await store.increment_agent_tasks("a1", completed=3, failed=1)
        async with store._conn.execute(
            "SELECT tasks_completed, tasks_failed FROM agents WHERE agent_id = 'a1'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["tasks_completed"] == 3
        assert row["tasks_failed"] == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_pr_authorship_round_trip(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        pr = PullRequestRecord(
            pr_number=42,
            session_id="s1",
            state="open",
            created_at="2026-04-27T00:01:00+00:00",
            branch="feat-x",
            author_agent_id="agent-a",
        )
        await store.record_pull_request(pr)
        author = await store.get_pr_author(42, "s1")
        assert author == "agent-a"
        assert await store.get_pr_author(99, "s1") is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_record_pull_request_does_not_overwrite_existing_author(tmp_path) -> None:
    """First-writer-wins: a later upsert must not replace an existing author_agent_id."""
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        first = PullRequestRecord(
            pr_number=7,
            session_id="s1",
            state="open",
            created_at="2026-01-01T00:00:00+00:00",
            author_agent_id="alice",
            github_author="alice-login",
        )
        await store.record_pull_request(first)

        second = PullRequestRecord(
            pr_number=7,
            session_id="s1",
            state="open",
            created_at="2026-01-01T00:01:00+00:00",
            author_agent_id="bob",
            github_author="bob-login",
        )
        await store.record_pull_request(second)

        author = await store.get_pr_author(7, "s1")
        assert author == "alice"
        gh_author = await store.get_pr_github_author(7, "s1")
        assert gh_author == "alice-login"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_record_pull_request_fills_null_author_on_first_write(tmp_path) -> None:
    """A later upsert fills authorship when the first write had NULL."""
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        no_author = PullRequestRecord(
            pr_number=8,
            session_id="s1",
            state="open",
            created_at="2026-01-01T00:00:00+00:00",
        )
        await store.record_pull_request(no_author)
        assert await store.get_pr_author(8, "s1") is None

        with_author = PullRequestRecord(
            pr_number=8,
            session_id="s1",
            state="open",
            created_at="2026-01-01T00:01:00+00:00",
            author_agent_id="carol",
        )
        await store.record_pull_request(with_author)
        assert await store.get_pr_author(8, "s1") == "carol"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cache_pull_requests_seeds_last_reviewed_sha_for_approved_prs(tmp_path) -> None:
    """Pre-existing APPROVED PRs must skip AgentShore's redundant re-review.

    When the GitHub fetch path inserts a new PR row that already has
    review_decision='APPROVED' on GitHub, seed last_reviewed_sha=head_sha so
    the policy routes it straight to merge_pr. On subsequent caches with
    the same NULL last_reviewed_sha from GitHub, the existing DB value
    wins (preservation). AgentShore's explicit writes via
    update_pr_last_reviewed_sha override the seed.
    """
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        approved = PullRequestRecord(
            pr_number=10,
            session_id="s1",
            state="open",
            created_at="2026-05-06T00:00:00+00:00",
            review_decision="APPROVED",
            head_sha="abc123",
        )
        await store.cache_pull_requests("s1", [approved])

        async def _last_reviewed(pr_number: int) -> str | None:
            async with store._conn.execute(
                "SELECT last_reviewed_sha FROM pull_requests "
                "WHERE pr_number = ? AND session_id = 's1'",
                (pr_number,),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            return row["last_reviewed_sha"]

        # 1. Seed on first insert: last_reviewed_sha = head_sha.
        assert await _last_reviewed(10) == "abc123"

        # 2. Re-cache with the same fields (still last_reviewed_sha=None from
        #    the adapter): preserved via the COALESCE.
        await store.cache_pull_requests("s1", [approved])
        assert await _last_reviewed(10) == "abc123"

        # 3. AgentShore's explicit write wins; subsequent caches do not regress.
        await store.update_pr_last_reviewed_sha(10, "s1", "def456")
        assert await _last_reviewed(10) == "def456"
        await store.cache_pull_requests("s1", [approved])
        assert await _last_reviewed(10) == "def456"

        # 4. Non-approved PRs are not seeded — AgentShore should review them.
        unreviewed = PullRequestRecord(
            pr_number=11,
            session_id="s1",
            state="open",
            created_at="2026-05-06T00:00:01+00:00",
            review_decision="REVIEW_REQUIRED",
            head_sha="xyz789",
        )
        await store.cache_pull_requests("s1", [unreviewed])
        assert await _last_reviewed(11) is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_update_pr_last_reviewed_sha_persists_status(tmp_path) -> None:
    """AgentShore's PASS/BLOCK verdict must persist alongside the SHA.

    code_review's broader contract is to persist verdict + SHA atomically so
    merge_pr can gate on AgentShore-internal approval when GitHub
    reviewDecision is unavailable (single-user setups).
    """
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        pr = PullRequestRecord(
            pr_number=20,
            session_id="s1",
            state="open",
            created_at="2026-05-06T00:00:00+00:00",
            head_sha="abc123",
        )
        await store.cache_pull_requests("s1", [pr])

        async def _row(pr_number: int) -> dict[str, str | None]:
            async with store._conn.execute(
                "SELECT last_reviewed_sha, last_review_status FROM pull_requests "
                "WHERE pr_number = ? AND session_id = 's1'",
                (pr_number,),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            return {
                "sha": row["last_reviewed_sha"],
                "status": row["last_review_status"],
            }

        await store.update_pr_last_reviewed_sha(20, "s1", "abc123", status="PASS")
        assert await _row(20) == {"sha": "abc123", "status": "PASS"}

        await store.update_pr_last_reviewed_sha(20, "s1", "def456", status="BLOCK")
        assert await _row(20) == {"sha": "def456", "status": "BLOCK"}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_update_pr_last_reviewed_sha_preserves_status_when_omitted(tmp_path) -> None:
    """Calls without a status (SKIP/dedup paths) must not clobber prior verdict."""
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        pr = PullRequestRecord(
            pr_number=30,
            session_id="s1",
            state="open",
            created_at="2026-05-06T00:00:00+00:00",
            head_sha="abc123",
        )
        await store.cache_pull_requests("s1", [pr])

        await store.update_pr_last_reviewed_sha(30, "s1", "abc123", status="PASS")
        # SKIP path: caller passes only the SHA; verdict must remain.
        await store.update_pr_last_reviewed_sha(30, "s1", "abc123")

        async with store._conn.execute(
            "SELECT last_reviewed_sha, last_review_status FROM pull_requests "
            "WHERE pr_number = 30 AND session_id = 's1'",
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["last_reviewed_sha"] == "abc123"
        assert row["last_review_status"] == "PASS"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_approved_pull_requests_includes_agentshore_internal_pass(tmp_path) -> None:
    """A AgentShore-internally-approved PR is returned.

    This unblocks merge_pr in single-user setups where GitHub blocks
    self-approval and reviewDecision never advances past NULL.
    """
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        pr = PullRequestRecord(
            pr_number=40,
            session_id="s1",
            state="open",
            created_at="2026-05-06T00:00:00+00:00",
            head_sha="abc123",
            mergeable="MERGEABLE",
            review_decision=None,  # GitHub never approved
        )
        await store.cache_pull_requests("s1", [pr])
        await store.update_pr_last_reviewed_sha(40, "s1", "abc123", status="PASS")

        approved = await store.list_approved_pull_requests("s1")
        assert [p.pr_number for p in approved] == [40]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_approved_pull_requests_excludes_stale_agentshore_pass(tmp_path) -> None:
    """A AgentShore PASS bound to an old SHA must not gate merge_pr after a push."""
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        pr = PullRequestRecord(
            pr_number=50,
            session_id="s1",
            state="open",
            created_at="2026-05-06T00:00:00+00:00",
            head_sha="abc123",
            mergeable="MERGEABLE",
            review_decision=None,
        )
        await store.cache_pull_requests("s1", [pr])
        await store.update_pr_last_reviewed_sha(50, "s1", "abc123", status="PASS")

        # New commit lands; head_sha advances. Manually update so the test
        # exercises the stale-approval check (no GitHub mock needed).
        await store._conn.execute(
            "UPDATE pull_requests SET head_sha='def456' WHERE pr_number=50 AND session_id='s1'"
        )
        await store._conn.commit()

        approved = await store.list_approved_pull_requests("s1")
        assert approved == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_approved_pull_requests_excludes_agentshore_block(tmp_path) -> None:
    """A AgentShore BLOCK verdict (even at current head) must not be approved."""
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        pr = PullRequestRecord(
            pr_number=60,
            session_id="s1",
            state="open",
            created_at="2026-05-06T00:00:00+00:00",
            head_sha="abc123",
            mergeable="MERGEABLE",
            review_decision=None,
        )
        await store.cache_pull_requests("s1", [pr])
        await store.update_pr_last_reviewed_sha(60, "s1", "abc123", status="BLOCK")

        approved = await store.list_approved_pull_requests("s1")
        assert approved == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cache_pull_requests_preserves_last_review_status(tmp_path) -> None:
    """Refreshing PRs from GitHub must not clobber AgentShore's verdict.

    `excluded.last_review_status` is always NULL from the adapter; the
    upsert COALESCEs to preserve the existing DB value.
    """
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        pr = PullRequestRecord(
            pr_number=70,
            session_id="s1",
            state="open",
            created_at="2026-05-06T00:00:00+00:00",
            head_sha="abc123",
        )
        await store.cache_pull_requests("s1", [pr])
        await store.update_pr_last_reviewed_sha(70, "s1", "abc123", status="PASS")

        # Simulate a GitHub refresh — same record, no internal verdict.
        await store.cache_pull_requests("s1", [pr])

        async with store._conn.execute(
            "SELECT last_review_status FROM pull_requests "
            "WHERE pr_number = 70 AND session_id = 's1'",
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["last_review_status"] == "PASS"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_branch_activity_upsert(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        await store.update_branch_activity("feat-x", "s1", "agent-a", sha="abc123")
        assert await store.get_last_implementer("feat-x", "s1") == "agent-a"

        # Overwrite with a different agent
        await store.update_branch_activity("feat-x", "s1", "agent-b")
        assert await store.get_last_implementer("feat-x", "s1") == "agent-b"
        assert await store.get_last_implementer("nonexistent", "s1") is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_rebuild_branch_activity_batches_inserts(tmp_path, monkeypatch) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        original_executemany = store._conn.executemany
        calls: list[list[tuple[str, str, str]]] = []

        async def spy_executemany(
            sql: str,
            parameters: Iterable[tuple[str, str, str]],
        ) -> object:
            rows = list(parameters)
            calls.append(rows)
            return await original_executemany(sql, rows)

        monkeypatch.setattr(store._conn, "executemany", spy_executemany)

        await store.rebuild_branch_activity(
            "s1",
            {"feat-a": 101, "feat-b": 102, "feat-c": 103},
        )

        assert len(calls) == 1
        assert [row[0] for row in calls[0]] == ["feat-a", "feat-b", "feat-c"]
        assert {row[1] for row in calls[0]} == {"s1"}
        assert len({row[2] for row in calls[0]}) == 1
        assert await store.get_last_implementer("feat-a", "s1") is None

        await store.update_branch_activity("feat-a", "s1", "agent-a", sha="abc123")
        await store.rebuild_branch_activity("s1", {"feat-a": 101})

        assert await store.get_last_implementer("feat-a", "s1") == "agent-a"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_record_handoff(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        # Create a play row first (needed for FK)
        await store.record_play(
            PlayRecord(
                session_id="s1",
                play_type="end_agent",
                started_at="T0",
                success=True,
            )
        )
        plays = await store.get_play_history("s1")
        play_id = plays[0].play_id
        assert play_id is not None

        handoff = HandoffRecord(
            session_id="s1",
            play_id=play_id,
            source_agent_id="a1",
            target_agent_id="a2",
            context_tokens_transferred=5000,
            ramp_up_duration_ms=1200,
            context_loss_estimate=0.1,
        )
        await store.record_handoff(handoff)

        async with store._conn.execute(
            "SELECT * FROM agent_handoffs WHERE session_id = 's1'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["source_agent_id"] == "a1"
        assert row["target_agent_id"] == "a2"
        assert row["context_tokens_transferred"] == 5000
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_handoffs_returns_recent_rows_oldest_first(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        await store.record_play(
            PlayRecord(
                session_id="s1",
                play_type="end_agent",
                started_at="T0",
                success=True,
            )
        )
        await store.record_play(
            PlayRecord(
                session_id="s1",
                play_type="end_agent",
                started_at="T1",
                success=True,
            )
        )
        plays = await store.get_play_history("s1")
        assert len(plays) == 2
        play_ids = [play.play_id for play in plays]
        assert all(play_id is not None for play_id in play_ids)

        await store.record_handoff(
            HandoffRecord(
                session_id="s1",
                play_id=play_ids[0] or 0,
                source_agent_id="a1",
                target_agent_id="a2",
                context_tokens_transferred=1000,
                ramp_up_duration_ms=500,
                context_loss_estimate=0.2,
            )
        )
        await store.record_handoff(
            HandoffRecord(
                session_id="s1",
                play_id=play_ids[1] or 0,
                source_agent_id="a2",
                target_agent_id="a3",
                context_tokens_transferred=2000,
                ramp_up_duration_ms=900,
                context_loss_estimate=0.4,
            )
        )

        recent = await store.list_handoffs("s1", limit=1)
        assert len(recent) == 1
        assert recent[0].source_agent_id == "a2"
        assert recent[0].target_agent_id == "a3"

        all_rows = await store.list_handoffs("s1", limit=10)
        assert [row.source_agent_id for row in all_rows] == ["a1", "a2"]
        assert [row.target_agent_id for row in all_rows] == ["a2", "a3"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_update_external_mutation_status_transitions_to_queued(tmp_path) -> None:
    """update_external_mutation_status moves a pending row to queued."""
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        await store.record_external_mutation(
            ExternalMutationRecord(
                session_id="s1",
                idempotency_key="key-to-promote",
                mutation_type="create_pr",
                target="pr",
                status="pending",
                created_at="T0",
            )
        )

        before = await store.get_external_mutation("s1", "key-to-promote")
        assert before is not None and before.status == "pending"

        await store.update_external_mutation_status("s1", "key-to-promote", "queued", "{}")

        row = await store.get_external_mutation("s1", "key-to-promote")
        assert row is not None
        assert row.status == "queued"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_external_mutation_idempotency_key_is_unique(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        mutation = ExternalMutationRecord(
            session_id="s1",
            idempotency_key="key-1",
            mutation_type="issue_create",
            target="issues",
            status="success",
            created_at="T0",
        )
        await store.record_external_mutation(mutation)

        # Second insert with the same key is silently ignored (INSERT OR IGNORE)
        await store.record_external_mutation(
            ExternalMutationRecord(
                session_id="s1",
                idempotency_key="key-1",  # duplicate
                mutation_type="issue_create",
                target="issues",
                status="success",
                created_at="T1",
            )
        )
        # Verify the original row is preserved, not overwritten
        original = await store.get_external_mutation("s1", "key-1")
        assert original is not None
        assert original.created_at == "T0"
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# get_agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_agents_empty_for_unknown_session(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        agents = await store.get_agents("no-such-session")
        assert agents == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_get_agents_returns_all_for_session(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        await store.register_agent(
            AgentRecord(agent_id="a1", session_id="s1", agent_type="claude_code", created_at="T0")
        )
        await store.register_agent(
            AgentRecord(agent_id="a2", session_id="s1", agent_type="codex", created_at="T1")
        )
        agents = await store.get_agents("s1")
        assert len(agents) == 2
        ids = {a.agent_id for a in agents}
        assert ids == {"a1", "a2"}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_get_agents_isolates_sessions(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await store.create_session(
            SessionRecord(session_id="s1", project_path=str(tmp_path), started_at="T0")
        )
        await store.create_session(
            SessionRecord(session_id="s2", project_path=str(tmp_path), started_at="T0")
        )
        await store.register_agent(
            AgentRecord(agent_id="a1", session_id="s1", agent_type="claude_code", created_at="T0")
        )
        await store.register_agent(
            AgentRecord(agent_id="a2", session_id="s2", agent_type="codex", created_at="T0")
        )
        assert len(await store.get_agents("s1")) == 1
        assert len(await store.get_agents("s2")) == 1
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# record_experience + iter_experience_for_replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_experience_round_trip(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        play_id = await store.record_play(
            PlayRecord(session_id="s1", play_type="issue_pickup", started_at="T0", success=True)
        )
        sv = b"\x00" * 8
        exp = ExperienceRecord(
            session_id="s1",
            play_id=play_id,
            state_vector=sv,
            action=3,
            reward=1.5,
            next_state=sv,
            done=0,
            action_space_version=1,
            old_log_prob=-0.7,
            step_index=0,
        )
        exp_id = await store.record_experience(exp)
        assert isinstance(exp_id, int) and exp_id > 0

        rows = [r async for r in store.iter_experience_for_replay("s1", action_space_version=1)]
        assert len(rows) == 1
        assert rows[0].action == 3
        assert abs(rows[0].reward - 1.5) < 1e-9
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_record_experience_persists_mask_reason(tmp_path) -> None:
    """mask_reason (v2->v3 column) round-trips through record_experience."""
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        play_id = await store.record_play(
            PlayRecord(
                session_id="s1", play_type="refine_task_breakdown", started_at="T0", success=True
            )
        )
        sv = b"\x00" * 8
        summary = "merge_pr=no eligible reviewer; code_review=anti-bias"
        exp = ExperienceRecord(
            session_id="s1",
            play_id=play_id,
            state_vector=sv,
            action=12,
            reward=-1.0,
            next_state=sv,
            done=0,
            mask_reason=summary,
        )
        await store.record_experience(exp)
        async with store._conn.execute(
            "SELECT mask_reason FROM rl_experience WHERE session_id='s1'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row["mask_reason"] == summary
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_migrate_v2_to_v3_adds_mask_reason_column(tmp_path) -> None:
    """A v2-shaped rl_experience (no mask_reason) gains the column idempotently."""
    import aiosqlite

    from agentshore.data.migrations import migrate_v2_to_v3

    db_path = tmp_path / "legacy.db"
    conn = await aiosqlite.connect(str(db_path))
    try:
        await conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TEXT)")
        # v2-shaped rl_experience: action_mask present, mask_reason absent.
        await conn.execute(
            "CREATE TABLE rl_experience (experience_id INTEGER PRIMARY KEY, action_mask BLOB)"
        )
        await conn.commit()

        await migrate_v2_to_v3(conn)
        async with conn.execute("PRAGMA table_info(rl_experience)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        assert "mask_reason" in cols

        # Idempotent: a second run does not raise (duplicate-column).
        await migrate_v2_to_v3(conn)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_migrate_v3_to_v4_adds_github_author_column(tmp_path) -> None:
    """A v3-shaped github_issues (no github_author) gains the column idempotently."""
    import aiosqlite

    from agentshore.data.migrations import migrate_v3_to_v4

    db_path = tmp_path / "legacy.db"
    conn = await aiosqlite.connect(str(db_path))
    try:
        await conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TEXT)")
        # v3-shaped github_issues: github_author absent.
        await conn.execute(
            "CREATE TABLE github_issues (issue_number INTEGER, session_id TEXT, title TEXT)"
        )
        await conn.commit()

        await migrate_v3_to_v4(conn)
        async with conn.execute("PRAGMA table_info(github_issues)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        assert "github_author" in cols

        # Idempotent: a second run does not raise (duplicate-column).
        await migrate_v3_to_v4(conn)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_iter_experience_empty_for_unknown_session(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        it = store.iter_experience_for_replay("no-session", action_space_version=1)
        rows = [r async for r in it]
        assert rows == []
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# NULL-field coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_play_record_null_fields_round_trip(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        await store.record_play(
            PlayRecord(
                session_id="s1",
                play_type="issue_pickup",
                started_at="T0",
                success=True,
                # nullable optional fields
                ended_at=None,
                agent_id=None,
                duration_ms=None,
                alignment_delta=None,
                alignment_before=None,
                error=None,
            )
        )
        history = await store.get_play_history("s1")
        assert len(history) == 1
        record = history[0]
        assert record.duration_ms is None
        assert record.alignment_delta is None
        assert record.error is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_close_persists_a_self_consistent_db_file(tmp_path) -> None:
    """Regression — DataStore.close() must leave agentshore.db non-empty and
    queryable, even if a concurrent external reader had run a TRUNCATE
    checkpoint mid-session. Symptom from a prior
    session: agentshore.db was 0 bytes after stop. Close now snapshots via the
    SQLite Online Backup API + atomic rename, regardless of WAL state.
    """
    db_path = tmp_path / "agentshore.db"
    store = DataStore(db_path)
    await store.initialize()
    await store.create_session(
        SessionRecord(
            session_id="snap-test",
            project_path=str(tmp_path),
            started_at="2026-05-05T00:00:00+00:00",
        )
    )
    await store.record_play(
        PlayRecord(
            session_id="snap-test",
            play_type="issue_pickup",
            started_at="2026-05-05T00:00:01+00:00",
            success=True,
        )
    )
    await store.close()

    assert db_path.exists()
    assert db_path.stat().st_size > 0, "close() left an empty agentshore.db"

    fresh = DataStore(db_path)
    await fresh.initialize()
    try:
        history = await fresh.get_play_history("snap-test")
        assert len(history) == 1
        assert history[0].play_type == "issue_pickup"
    finally:
        await fresh.close()


# ---------------------------------------------------------------------------
# mark_pr_merged — post-merge cache write-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_pr_merged_writes_state_and_timestamp(tmp_path) -> None:
    """mark_pr_merged transitions the cache row to state='MERGED' and stamps
    merged_at, so the next state snapshot reflects the merge immediately
    instead of waiting for the next github_pull_requests_refreshed cycle."""
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        pr = PullRequestRecord(
            pr_number=42,
            session_id="s1",
            state="open",
            created_at="2026-05-06T00:00:00+00:00",
            head_sha="abc123",
            mergeable="MERGEABLE",
        )
        await store.cache_pull_requests("s1", [pr])

        await store.mark_pr_merged(42, "s1")

        async with store._conn.execute(
            "SELECT state, merged_at FROM pull_requests WHERE pr_number = 42 AND session_id = 's1'",
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["state"] == "MERGED"
        assert row["merged_at"] is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_mark_pr_merged_preserves_existing_merged_at(tmp_path) -> None:
    """If GitHub already populated merged_at on a prior refresh, mark_pr_merged
    must not overwrite it — the GH timestamp is more accurate than now()."""
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        gh_merged_at = "2026-05-06T16:31:06+00:00"
        pr = PullRequestRecord(
            pr_number=43,
            session_id="s1",
            state="open",
            created_at="2026-05-06T00:00:00+00:00",
            head_sha="abc123",
            merged_at=gh_merged_at,
        )
        await store.cache_pull_requests("s1", [pr])

        await store.mark_pr_merged(43, "s1", merged_at="2026-05-06T17:00:00+00:00")

        async with store._conn.execute(
            "SELECT state, merged_at FROM pull_requests WHERE pr_number = 43 AND session_id = 's1'",
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["state"] == "MERGED"
        # COALESCE(merged_at, ?) — existing value wins
        assert row["merged_at"] == gh_merged_at
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# list_recently_closed_issues — Done column window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_github_issues_preserves_issue_url(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        issue = GitHubIssueRecord(
            issue_number=9,
            session_id="s1",
            title="issue with url",
            state="open",
            created_at="2026-05-06T00:00:00+00:00",
            url="https://github.com/example/repo/issues/9",
        )
        await store.cache_github_issues("s1", [issue])

        open_issues = await store.get_open_issues("s1")
        assert open_issues[0].url == "https://github.com/example/repo/issues/9"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_add_issue_labels_updates_cached_issue(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        await store.cache_github_issues(
            "s1",
            [
                GitHubIssueRecord(
                    issue_number=209,
                    session_id="s1",
                    title="CI workflow task",
                    state="open",
                    labels=["priority/high"],
                    created_at="2026-05-06T00:00:00+00:00",
                )
            ],
        )

        await store.add_issue_labels(209, "s1", ["agentshore/disallowed", "priority/high"])

        issue = await store.get_github_issue(209, "s1")
        assert issue is not None
        assert set(issue.labels) == {"priority/high", "agentshore/disallowed"}
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_list_recently_closed_issues_includes_recent_closure(tmp_path) -> None:
    """An issue closed within the last `hours` must be returned so the
    dashboard kanban Done column can render it."""
    from datetime import UTC, datetime, timedelta

    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        recent = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        issue = GitHubIssueRecord(
            issue_number=10,
            session_id="s1",
            title="recently closed",
            state="closed",
            created_at="2026-05-06T00:00:00+00:00",
            closed_at=recent,
        )
        await store.cache_github_issues("s1", [issue])

        recently_closed = await store.list_recently_closed_issues("s1")
        assert [i.issue_number for i in recently_closed] == [10]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_recently_closed_issues_excludes_old_closure(tmp_path) -> None:
    """Issues closed beyond the window must not appear — bounds the Done
    column payload."""
    from datetime import UTC, datetime, timedelta

    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        issue = GitHubIssueRecord(
            issue_number=11,
            session_id="s1",
            title="old closure",
            state="closed",
            created_at="2026-05-06T00:00:00+00:00",
            closed_at=old,
        )
        await store.cache_github_issues("s1", [issue])

        recently_closed = await store.list_recently_closed_issues("s1", hours=24)
        assert recently_closed == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_recently_closed_issues_excludes_open_issues(tmp_path) -> None:
    """Open issues never appear in Done, even if they have a closed_at value
    (which would only happen via a reopen — defensive)."""
    from datetime import UTC, datetime, timedelta

    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        issue = GitHubIssueRecord(
            issue_number=12,
            session_id="s1",
            title="open with stale closed_at",
            state="open",
            created_at="2026-05-06T00:00:00+00:00",
            closed_at=recent,
        )
        await store.cache_github_issues("s1", [issue])

        recently_closed = await store.list_recently_closed_issues("s1")
        assert recently_closed == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_recently_closed_issues_excludes_null_closed_at(tmp_path) -> None:
    """A closed issue with no closed_at must not crash the query and must
    not be returned (we can't establish whether it falls in the window)."""
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        issue = GitHubIssueRecord(
            issue_number=13,
            session_id="s1",
            title="closed but no timestamp",
            state="closed",
            created_at="2026-05-06T00:00:00+00:00",
            closed_at=None,
        )
        await store.cache_github_issues("s1", [issue])

        recently_closed = await store.list_recently_closed_issues("s1")
        assert recently_closed == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_recently_merged_pull_requests_includes_recent_merge(tmp_path) -> None:
    """A PR merged within the window must be returned so dashboard Kanban can
    render it in Done even before issue-close mirroring catches up."""
    from datetime import UTC, datetime, timedelta

    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        recent = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        pr = PullRequestRecord(
            pr_number=30,
            session_id="s1",
            state="MERGED",
            created_at="2026-05-06T00:00:00+00:00",
            issue_number=10,
            merged_at=recent,
        )
        await store.cache_pull_requests("s1", [pr])

        recently_merged = await store.list_recently_merged_pull_requests("s1")

        assert [pr.pr_number for pr in recently_merged] == [30]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_recently_merged_pull_requests_excludes_old_and_unmerged(tmp_path) -> None:
    from datetime import UTC, datetime, timedelta

    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        recent = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        await store.cache_pull_requests(
            "s1",
            [
                PullRequestRecord(
                    pr_number=31,
                    session_id="s1",
                    state="MERGED",
                    created_at="2026-05-06T00:00:00+00:00",
                    issue_number=11,
                    merged_at=old,
                ),
                PullRequestRecord(
                    pr_number=32,
                    session_id="s1",
                    state="open",
                    created_at="2026-05-06T00:00:00+00:00",
                    issue_number=12,
                    merged_at=recent,
                ),
                PullRequestRecord(
                    pr_number=33,
                    session_id="s1",
                    state="MERGED",
                    created_at="2026-05-06T00:00:00+00:00",
                    issue_number=13,
                    merged_at=None,
                ),
            ],
        )

        recently_merged = await store.list_recently_merged_pull_requests("s1", hours=24)

        assert recently_merged == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_update_issues_state_batch_closes_multiple_issues(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        await store.cache_github_issues(
            "s1",
            [
                GitHubIssueRecord(
                    issue_number=41,
                    session_id="s1",
                    title="first",
                    state="open",
                    created_at="2026-05-06T00:00:00+00:00",
                ),
                GitHubIssueRecord(
                    issue_number=42,
                    session_id="s1",
                    title="second",
                    state="open",
                    created_at="2026-05-06T00:00:00+00:00",
                ),
            ],
        )

        await store.update_issues_state_batch([41, 42], "s1", "closed")

        first = await store.get_github_issue(41, "s1")
        second = await store.get_github_issue(42, "s1")
        assert first is not None
        assert second is not None
        assert first.state == "closed"
        assert second.state == "closed"
        assert first.closed_at is not None
        assert second.closed_at is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_update_issues_state_batch_empty_input_is_noop(tmp_path) -> None:
    store = DataStore(tmp_path / "agentshore.db")
    await store.initialize()
    try:
        await _setup_session(store, tmp_path)
        issue = GitHubIssueRecord(
            issue_number=43,
            session_id="s1",
            title="unchanged",
            state="open",
            created_at="2026-05-06T00:00:00+00:00",
        )
        await store.cache_github_issues("s1", [issue])

        await store.update_issues_state_batch([], "s1", "closed")

        fetched = await store.get_github_issue(43, "s1")
        assert fetched is not None
        assert fetched.state == "open"
        assert fetched.closed_at is None
    finally:
        await store.close()
