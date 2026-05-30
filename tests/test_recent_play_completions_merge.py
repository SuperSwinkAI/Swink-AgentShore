"""Regression tests for the recent-completions WAL-lag shadow (desktop-65bg).

When two same-tick ``instantiate_agent`` dispatches landed back-to-back in
session ba744eef on 2026-05-19, the cooldown mask didn't fire because the
SQLite write from the first dispatch wasn't yet visible to the second
dispatch's ``get_play_history`` read. ``_merge_recent_completions`` overlays
the in-memory shadow on top of the DB read so recency math always sees the
fresh view.
"""

from __future__ import annotations

import collections

from agentshore.core.mixins.state import _merge_recent_completions
from agentshore.data.models import PlayRecord


def _play(play_id: int, play_type: str = "instantiate_agent") -> PlayRecord:
    return PlayRecord(
        session_id="s1",
        play_type=play_type,
        started_at="2026-05-20T00:00:00+00:00",
        success=True,
        play_id=play_id,
        ended_at="2026-05-20T00:00:01+00:00",
    )


def test_returns_db_history_unchanged_when_shadow_is_empty() -> None:
    db = [_play(1), _play(2)]
    out = _merge_recent_completions(db, [])
    assert out is db
    assert [p.play_id for p in out] == [1, 2]


def test_appends_shadow_plays_missing_from_db() -> None:
    # Simulates the WAL-lag race: DB read returned plays 1..3 but play 4
    # was recorded just before this state-build and hasn't flushed yet.
    db = [_play(1), _play(2), _play(3)]
    shadow = collections.deque([_play(4)])
    out = _merge_recent_completions(db, shadow)
    assert [p.play_id for p in out] == [1, 2, 3, 4]


def test_skips_shadow_plays_already_in_db() -> None:
    # Once the WAL flush catches up, the shadow still holds the play but the
    # DB is now authoritative — no duplication.
    db = [_play(1), _play(2), _play(3)]
    shadow = collections.deque([_play(2), _play(3)])
    out = _merge_recent_completions(db, shadow)
    assert [p.play_id for p in out] == [1, 2, 3]


def test_deduplicates_repeated_play_ids_in_shadow() -> None:
    # Production play_ids are unique per record_play; this guards against
    # test fixtures that reuse a single play_id across mock dispatches.
    db = [_play(1)]
    shadow = collections.deque([_play(2), _play(2), _play(2)])
    out = _merge_recent_completions(db, shadow)
    assert [p.play_id for p in out] == [1, 2]


def test_drops_shadow_entries_with_null_play_id() -> None:
    db = [_play(1)]
    null_play = PlayRecord(
        session_id="s1",
        play_type="instantiate_agent",
        started_at="2026-05-20T00:00:00+00:00",
        success=True,
        play_id=None,
    )
    shadow = collections.deque([null_play, _play(2)])
    out = _merge_recent_completions(db, shadow)
    assert [p.play_id for p in out] == [1, 2]
