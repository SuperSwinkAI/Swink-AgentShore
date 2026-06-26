"""Tests for LearningsHarvester — new outcome.learnings harvest path."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.config.models import LearningsConfig
from agentshore.core.learnings_harvester import LearningsHarvester
from agentshore.learnings import Learning, load, save_atomic
from agentshore.state import PlayOutcome, PlayType


def _harvester(repo_root: Path) -> LearningsHarvester:
    cfg = LearningsConfig(file=".agentshore/learnings.json")
    return LearningsHarvester(repo_root=repo_root, learnings_cfg=cfg)


def _outcome(
    *,
    success: bool = True,
    play_id: int | None = 1,
    learnings: list[dict[str, object]] | None = None,
    learnings_compacted: list[dict[str, object]] | None = None,
    play_type: PlayType = PlayType.ISSUE_PICKUP,
) -> PlayOutcome:
    return PlayOutcome(
        play_type=play_type,
        agent_id="agent-1",
        success=success,
        partial=False,
        duration_seconds=1.0,
        token_cost=100,
        dollar_cost=0.001,
        artifacts=[],
        alignment_delta=0.0,
        play_id=play_id,
        learnings=list(learnings) if learnings else [],
        learnings_compacted=list(learnings_compacted) if learnings_compacted else [],
    )


async def test_harvest_writes_learning_to_file(tmp_path: Path) -> None:
    """A single valid learning in outcome.learnings should land in learnings.json."""
    h = _harvester(tmp_path)
    outcome = _outcome(learnings=[{"pattern": "X", "confidence": 0.6, "category": "general"}])
    await h.update_learnings(outcome, PlayType.ISSUE_PICKUP)

    entries = load(tmp_path / ".agentshore/learnings.json")
    assert len(entries) == 1
    assert entries[0].pattern == "X"
    assert entries[0].confidence == pytest.approx(0.6)
    assert entries[0].category == "general"
    assert entries[0].source_play_id == 1
    assert entries[0].last_reinforced_play_id == 1


async def test_harvest_no_op_on_failure(tmp_path: Path) -> None:
    """A failed outcome must not write any learnings."""
    h = _harvester(tmp_path)
    outcome = _outcome(
        success=False,
        learnings=[{"pattern": "should not appear", "confidence": 0.9, "category": "general"}],
    )
    await h.update_learnings(outcome, PlayType.ISSUE_PICKUP)

    path = tmp_path / ".agentshore/learnings.json"
    assert not path.exists()


async def test_harvest_no_op_when_learnings_empty(tmp_path: Path) -> None:
    """A successful outcome with no learnings list must not create the file."""
    h = _harvester(tmp_path)
    outcome = _outcome(learnings=[])
    await h.update_learnings(outcome, PlayType.ISSUE_PICKUP)

    path = tmp_path / ".agentshore/learnings.json"
    assert not path.exists()


async def test_dedup_same_pattern_twice_yields_one_entry(tmp_path: Path) -> None:
    """Calling update_learnings twice with the same pattern should store only one entry."""
    h = _harvester(tmp_path)
    learning = {"pattern": "validate inputs", "confidence": 0.7, "category": "testing"}
    outcome = _outcome(learnings=[learning])

    await h.update_learnings(outcome, PlayType.ISSUE_PICKUP)
    await h.update_learnings(outcome, PlayType.ISSUE_PICKUP)

    entries = load(tmp_path / ".agentshore/learnings.json")
    patterns = [e.pattern for e in entries]
    assert patterns.count("validate inputs") == 1


async def test_reharvest_reinforces_existing_entry(tmp_path: Path) -> None:
    """Re-emitting an existing pattern reinforces it in place (no duplicate):
    the id is preserved, confidence is bumped, recency is reset, and the
    reinforcing play is recorded."""
    path = tmp_path / ".agentshore/learnings.json"
    existing = Learning(
        id="existing-1",
        pattern="pre-existing",
        confidence=0.7,
        sessions_since_use=4,
        source_play_id=None,
        last_reinforced_play_id=None,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    save_atomic(path, [existing])

    h = _harvester(tmp_path)
    outcome = _outcome(
        play_id=42,
        learnings=[{"pattern": "pre-existing", "confidence": 0.5, "category": "general"}],
    )
    await h.update_learnings(outcome, PlayType.CODE_REVIEW)

    entries = load(path)
    matched = [e for e in entries if e.pattern == "pre-existing"]
    assert len(matched) == 1
    assert matched[0].id == "existing-1"
    assert matched[0].confidence == pytest.approx(0.8)  # 0.7 + 0.1
    assert matched[0].sessions_since_use == 0
    assert matched[0].last_reinforced_play_id == 42


async def test_reharvest_caps_confidence_at_1_0(tmp_path: Path) -> None:
    """Reinforcing a near-maxed pattern clamps confidence at 1.0."""
    path = tmp_path / ".agentshore/learnings.json"
    existing = Learning(
        id="existing-1",
        pattern="near-max",
        confidence=0.95,
        sessions_since_use=0,
        source_play_id=None,
        last_reinforced_play_id=None,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    save_atomic(path, [existing])

    h = _harvester(tmp_path)
    outcome = _outcome(learnings=[{"pattern": "near-max", "confidence": 0.5}])
    await h.update_learnings(outcome, PlayType.ISSUE_PICKUP)

    entries = load(path)
    assert len(entries) == 1
    assert entries[0].confidence <= 1.0


async def test_max_entries_trim(tmp_path: Path) -> None:
    """Entries exceeding max_entries are trimmed to the highest-confidence set."""
    cfg = LearningsConfig(file=".agentshore/learnings.json", max_entries=3)
    h = LearningsHarvester(repo_root=tmp_path, learnings_cfg=cfg)

    path = tmp_path / ".agentshore/learnings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_atomic(
        path,
        [
            Learning(
                id="a",
                pattern="alpha",
                confidence=0.9,
                sessions_since_use=0,
                source_play_id=None,
                last_reinforced_play_id=None,
            ),
            Learning(
                id="b",
                pattern="beta",
                confidence=0.8,
                sessions_since_use=0,
                source_play_id=None,
                last_reinforced_play_id=None,
            ),
            Learning(
                id="c",
                pattern="gamma",
                confidence=0.7,
                sessions_since_use=0,
                source_play_id=None,
                last_reinforced_play_id=None,
            ),
        ],
    )

    outcome = _outcome(
        learnings=[{"pattern": "delta-low", "confidence": 0.3, "category": "general"}]
    )
    await h.update_learnings(outcome, PlayType.ISSUE_PICKUP)

    entries = load(path)
    assert len(entries) == 3
    patterns = {e.pattern for e in entries}
    assert "delta-low" not in patterns
    assert "alpha" in patterns


async def test_multiple_learnings_in_one_outcome(tmp_path: Path) -> None:
    """All valid entries in a single outcome's learnings list are stored."""
    h = _harvester(tmp_path)
    outcome = _outcome(
        learnings=[
            {"pattern": "one", "confidence": 0.5, "category": "general"},
            {"pattern": "two", "confidence": 0.6, "category": "testing"},
            {"pattern": "three", "confidence": 0.7, "category": "security"},
        ]
    )
    await h.update_learnings(outcome, PlayType.ISSUE_PICKUP)

    entries = load(tmp_path / ".agentshore/learnings.json")
    assert len(entries) == 3
    patterns = {e.pattern for e in entries}
    assert patterns == {"one", "two", "three"}


async def test_malformed_entries_in_outcome_dropped(tmp_path: Path) -> None:
    """Non-dict entries and dicts without a pattern are dropped; valid ones still land."""
    h = _harvester(tmp_path)
    outcome = _outcome(
        learnings=[
            "not a dict",  # type: ignore[list-item]
            {"confidence": 0.5},  # missing pattern
            {"pattern": "", "confidence": 0.5},  # empty pattern
            {"pattern": "valid-entry", "confidence": 0.8, "category": "general"},
        ]
    )
    await h.update_learnings(outcome, PlayType.ISSUE_PICKUP)

    entries = load(tmp_path / ".agentshore/learnings.json")
    assert len(entries) == 1
    assert entries[0].pattern == "valid-entry"


def _seed(tmp_path: Path, entries: list[Learning]) -> Path:
    path = tmp_path / ".agentshore/learnings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_atomic(path, entries)
    return path


def _learning(
    id: str,
    pattern: str,
    *,
    confidence: float = 0.5,
    sessions_since_use: int = 0,
    last_reinforced_play_id: int | None = None,
    created_at: str = "2025-01-01T00:00:00+00:00",
    category: str = "general",
) -> Learning:
    return Learning(
        id=id,
        pattern=pattern,
        confidence=confidence,
        sessions_since_use=sessions_since_use,
        source_play_id=None,
        last_reinforced_play_id=last_reinforced_play_id,
        created_at=created_at,
        category=category,
    )


async def test_compacted_replaces_store_and_folds_metadata(tmp_path: Path) -> None:
    """A compacted entry tagged with merged_from replaces its sources, folding
    metadata deterministically; un-carried entries are dropped."""
    path = _seed(
        tmp_path,
        [
            _learning(
                "a",
                "run tests before PR",
                confidence=0.6,
                sessions_since_use=4,
                last_reinforced_play_id=3,
                created_at="2025-02-01T00:00:00+00:00",
            ),
            _learning(
                "b",
                "always run tests before opening PR",
                confidence=0.8,
                sessions_since_use=1,
                last_reinforced_play_id=9,
                created_at="2025-01-01T00:00:00+00:00",
            ),
            _learning("c", "unrelated obsolete note", confidence=0.7),
        ],
    )
    h = _harvester(tmp_path)
    outcome = _outcome(
        play_id=42,
        learnings_compacted=[
            {
                "pattern": "run the tests before opening a PR",
                "category": "general",
                "merged_from": ["a", "b"],
            }
        ],
    )
    await h.update_learnings(outcome, PlayType.GROOM_BACKLOG)

    entries = load(path)
    assert len(entries) == 1  # c was dropped (not carried forward)
    merged = entries[0]
    assert merged.pattern == "run the tests before opening a PR"
    assert merged.id == "a"  # first source id preserved
    assert merged.confidence == pytest.approx(0.8)  # max
    assert merged.sessions_since_use == 1  # min
    assert merged.last_reinforced_play_id == 9  # most recent
    assert merged.created_at == "2025-01-01T00:00:00+00:00"  # earliest


async def test_compacted_entry_without_merged_from_is_fresh(tmp_path: Path) -> None:
    """A compacted entry with no resolvable provenance becomes a fresh learning."""
    _seed(tmp_path, [_learning("a", "old", confidence=0.9)])
    h = _harvester(tmp_path)
    outcome = _outcome(
        play_id=7,
        learnings_compacted=[
            {"pattern": "brand new synthesis", "category": "x", "merged_from": []}
        ],
    )
    await h.update_learnings(outcome, PlayType.GROOM_BACKLOG)

    entries = load(tmp_path / ".agentshore/learnings.json")
    assert len(entries) == 1
    assert entries[0].pattern == "brand new synthesis"
    assert entries[0].confidence == pytest.approx(0.5)  # DEFAULT
    assert entries[0].sessions_since_use == 0
    assert entries[0].source_play_id == 7


async def test_compacted_empty_result_for_nonempty_store_is_rejected(tmp_path: Path) -> None:
    """A payload that would empty a non-empty store is rejected; store is kept."""
    path = _seed(tmp_path, [_learning("a", "keep me", confidence=0.9)])
    h = _harvester(tmp_path)
    # Only an invalid (empty-pattern) entry → rebuilt would be empty.
    outcome = _outcome(play_id=5, learnings_compacted=[{"pattern": "", "merged_from": []}])
    await h.update_learnings(outcome, PlayType.GROOM_BACKLOG)

    entries = load(path)
    assert len(entries) == 1
    assert entries[0].pattern == "keep me"
    assert entries[0].confidence == pytest.approx(0.9)


async def test_compacted_then_incremental_appends_on_top(tmp_path: Path) -> None:
    """After a replace, the incremental ``learnings`` array still appends."""
    _seed(tmp_path, [_learning("a", "old pattern", confidence=0.6)])
    h = _harvester(tmp_path)
    outcome = _outcome(
        play_id=11,
        learnings_compacted=[{"pattern": "compacted pattern", "merged_from": ["a"]}],
        learnings=[{"pattern": "fresh from this run", "confidence": 0.7}],
    )
    await h.update_learnings(outcome, PlayType.GROOM_BACKLOG)

    entries = load(tmp_path / ".agentshore/learnings.json")
    patterns = {e.pattern for e in entries}
    assert patterns == {"compacted pattern", "fresh from this run"}


async def test_compacted_ignored_when_kill_switch_off(tmp_path: Path) -> None:
    """With redistill_in_groom=False the compacted payload is ignored."""
    path = _seed(tmp_path, [_learning("a", "keep me", confidence=0.9)])
    cfg = LearningsConfig(file=".agentshore/learnings.json", redistill_in_groom=False)
    h = LearningsHarvester(repo_root=tmp_path, learnings_cfg=cfg)
    outcome = _outcome(
        play_id=5,
        learnings_compacted=[{"pattern": "would replace", "merged_from": ["a"]}],
    )
    await h.update_learnings(outcome, PlayType.GROOM_BACKLOG)

    entries = load(path)
    assert len(entries) == 1
    assert entries[0].pattern == "keep me"


async def test_compacted_noop_on_failure(tmp_path: Path) -> None:
    """A failed outcome never replaces the store."""
    path = _seed(tmp_path, [_learning("a", "keep me", confidence=0.9)])
    h = _harvester(tmp_path)
    outcome = _outcome(
        success=False,
        play_id=5,
        learnings_compacted=[{"pattern": "would replace", "merged_from": ["a"]}],
    )
    await h.update_learnings(outcome, PlayType.GROOM_BACKLOG)

    entries = load(path)
    assert len(entries) == 1
    assert entries[0].pattern == "keep me"
