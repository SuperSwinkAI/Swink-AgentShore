"""Tests for the review_queue DataStore methods."""

from __future__ import annotations

import pytest

from agentshore.data.store import (
    DataStore,
    ExternalMutationRecord,
    ReviewQueueRecord,
    SessionRecord,
)


async def _make_store(tmp_path: object) -> DataStore:
    """Create and initialize a fresh DataStore for testing."""
    from pathlib import Path

    store = DataStore(Path(str(tmp_path)) / "agentshore.db")
    await store.initialize()
    await store.create_session(
        SessionRecord(
            session_id="s1",
            project_path=str(tmp_path),
            started_at="2026-05-07T00:00:00+00:00",
        )
    )
    return store


@pytest.mark.asyncio
async def test_enqueue_review_creates_row(tmp_path) -> None:
    store = await _make_store(tmp_path)
    try:
        qid = await store.enqueue_review(
            ReviewQueueRecord(
                pr_number=42,
                session_id="s1",
                enqueued_at="2026-05-07T00:01:00+00:00",
                author_label="agent-a",
            )
        )
        assert qid > 0

        pending = await store.list_pending_reviews("s1")
        assert len(pending) == 1
        row = pending[0]
        assert row.pr_number == 42
        assert row.session_id == "s1"
        assert row.author_label == "agent-a"
        assert row.status == "pending"
        assert row.enqueued_at == "2026-05-07T00:01:00+00:00"
        assert row.claimed_by is None
        assert row.claimed_at is None
        assert row.completed_at is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_enqueue_review_idempotent(tmp_path) -> None:
    store = await _make_store(tmp_path)
    try:
        qid1 = await store.enqueue_review(
            ReviewQueueRecord(
                pr_number=42,
                session_id="s1",
                enqueued_at="2026-05-07T00:01:00+00:00",
            )
        )
        assert qid1 > 0

        # Second enqueue for same PR+session should be silently ignored.
        qid2 = await store.enqueue_review(
            ReviewQueueRecord(
                pr_number=42,
                session_id="s1",
                enqueued_at="2026-05-07T00:02:00+00:00",
            )
        )
        assert qid2 == 0

        pending = await store.list_pending_reviews("s1")
        assert len(pending) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_claim_review_transitions_status(tmp_path) -> None:
    store = await _make_store(tmp_path)
    try:
        qid = await store.enqueue_review(
            ReviewQueueRecord(
                pr_number=42,
                session_id="s1",
                enqueued_at="2026-05-07T00:01:00+00:00",
            )
        )

        ok = await store.claim_review(qid, "agent-b")
        assert ok is True

        pending = await store.list_pending_reviews("s1")
        assert len(pending) == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_enqueue_review_deduplicates_claimed_review(tmp_path) -> None:
    store = await _make_store(tmp_path)
    try:
        qid = await store.enqueue_review(
            ReviewQueueRecord(
                pr_number=42,
                session_id="s1",
                enqueued_at="2026-05-07T00:01:00+00:00",
            )
        )
        assert qid > 0
        assert await store.claim_review(qid, "agent-b")

        duplicate = await store.enqueue_review(
            ReviewQueueRecord(
                pr_number=42,
                session_id="s1",
                enqueued_at="2026-05-07T00:02:00+00:00",
            )
        )

        assert duplicate == 0
        assert await store.list_pending_reviews("s1") == []
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_claim_review_fails_if_already_claimed(tmp_path) -> None:
    store = await _make_store(tmp_path)
    try:
        qid = await store.enqueue_review(
            ReviewQueueRecord(
                pr_number=42,
                session_id="s1",
                enqueued_at="2026-05-07T00:01:00+00:00",
            )
        )

        first = await store.claim_review(qid, "agent-b")
        assert first is True

        second = await store.claim_review(qid, "agent-c")
        assert second is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_complete_review_sets_done(tmp_path) -> None:
    store = await _make_store(tmp_path)
    try:
        qid = await store.enqueue_review(
            ReviewQueueRecord(
                pr_number=42,
                session_id="s1",
                enqueued_at="2026-05-07T00:01:00+00:00",
            )
        )
        await store.claim_review(qid, "agent-b")
        await store.complete_review(qid)

        pending = await store.list_pending_reviews("s1")
        assert len(pending) == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_pending_reviews_ordered(tmp_path) -> None:
    store = await _make_store(tmp_path)
    try:
        await store.enqueue_review(
            ReviewQueueRecord(
                pr_number=10,
                session_id="s1",
                enqueued_at="2026-05-07T00:03:00+00:00",
            )
        )
        await store.enqueue_review(
            ReviewQueueRecord(
                pr_number=20,
                session_id="s1",
                enqueued_at="2026-05-07T00:01:00+00:00",
            )
        )
        await store.enqueue_review(
            ReviewQueueRecord(
                pr_number=30,
                session_id="s1",
                enqueued_at="2026-05-07T00:02:00+00:00",
            )
        )

        pending = await store.list_pending_reviews("s1")
        assert len(pending) == 3
        # Ordered by enqueued_at ASC.
        assert [r.pr_number for r in pending] == [20, 30, 10]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_pending_reviews_excludes_claimed_and_done(tmp_path) -> None:
    store = await _make_store(tmp_path)
    try:
        qid1 = await store.enqueue_review(
            ReviewQueueRecord(
                pr_number=1,
                session_id="s1",
                enqueued_at="2026-05-07T00:01:00+00:00",
            )
        )
        qid2 = await store.enqueue_review(
            ReviewQueueRecord(
                pr_number=2,
                session_id="s1",
                enqueued_at="2026-05-07T00:02:00+00:00",
            )
        )
        await store.enqueue_review(
            ReviewQueueRecord(
                pr_number=3,
                session_id="s1",
                enqueued_at="2026-05-07T00:03:00+00:00",
            )
        )

        await store.claim_review(qid1, "agent-x")
        await store.claim_review(qid2, "agent-y")
        await store.complete_review(qid2)

        pending = await store.list_pending_reviews("s1")
        assert len(pending) == 1
        assert pending[0].pr_number == 3
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_external_mutation_idempotency_no_error(tmp_path) -> None:
    store = await _make_store(tmp_path)
    try:
        mutation = ExternalMutationRecord(
            session_id="s1",
            idempotency_key="key-abc",
            mutation_type="label_issue",
            target="issue/99",
            status="success",
            created_at="2026-05-07T00:01:00+00:00",
        )
        await store.record_external_mutation(mutation)

        # Second insert with same idempotency_key should NOT raise.
        await store.record_external_mutation(mutation)

        existing = await store.get_external_mutation("s1", "key-abc")
        assert existing is not None
        assert existing.idempotency_key == "key-abc"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_mark_pull_request_absent_evicts_and_drains(tmp_path) -> None:
    """mark_pull_request_absent (the #279 reaper / #278 backstop primitive) sets
    the PR's state to 'absent' so it drops out of the open/active eligibility
    filters, and drains its pending review-queue rows so the resolver stops
    re-offering it."""
    from agentshore.data.store import PullRequestRecord

    store = await _make_store(tmp_path)
    try:
        await store.record_pull_request(
            PullRequestRecord(
                pr_number=2665,
                session_id="s1",
                state="open",
                created_at="2026-05-07T00:00:00+00:00",
                branch="feature/phantom",
            )
        )
        await store.enqueue_review(
            ReviewQueueRecord(
                pr_number=2665,
                session_id="s1",
                enqueued_at="2026-05-07T00:01:00+00:00",
            )
        )
        # Precondition: the PR is open and a review is pending.
        assert [pr.pr_number for pr in await store.list_open_pull_requests("s1")] == [2665]
        assert len(await store.list_pending_reviews("s1")) == 1

        await store.mark_pull_request_absent("s1", 2665)

        # The PR no longer matches the open/active eligibility filters...
        assert await store.list_open_pull_requests("s1") == []
        assert await store.list_active_pull_requests("s1") == []
        # ...the row is marked absent (not deleted — auditable)...
        evicted = await store.get_pull_request("s1", 2665)
        assert evicted is not None
        assert evicted.state == "absent"
        # ...and the pending review row was drained.
        assert await store.list_pending_reviews("s1") == []
    finally:
        await store.close()
