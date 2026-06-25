"""Tests for LearningsHarvester — new outcome.learnings harvest path."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentshore.config.models import LearningsConfig
from agentshore.core.learnings_harvester import LearningsHarvester
from agentshore.learnings import Learning, load, save_atomic
from agentshore.state import PlayOutcome, PlayType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _harvester(repo_root: Path) -> LearningsHarvester:
    cfg = LearningsConfig(file=".agentshore/learnings.json")
    return LearningsHarvester(repo_root=repo_root, learnings_cfg=cfg)


def _outcome(
    *,
    success: bool = True,
    play_id: int | None = 1,
    learnings: list[dict[str, object]] | None = None,
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
    )


# ---------------------------------------------------------------------------
# Basic harvest — outcome.learnings path
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Dedup — same pattern twice → one entry
# ---------------------------------------------------------------------------


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


async def test_dedup_does_not_overwrite_existing_entry(tmp_path: Path) -> None:
    """A pre-existing learning with the same pattern must not be duplicated."""
    path = tmp_path / ".agentshore/learnings.json"
    existing = Learning(
        id="existing-1",
        pattern="pre-existing",
        confidence=0.9,
        sessions_since_use=0,
        source_play_id=None,
        last_reinforced_play_id=None,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    save_atomic(path, [existing])

    h = _harvester(tmp_path)
    outcome = _outcome(
        learnings=[{"pattern": "pre-existing", "confidence": 0.5, "category": "general"}]
    )
    await h.update_learnings(outcome, PlayType.CODE_REVIEW)

    entries = load(path)
    matched = [e for e in entries if e.pattern == "pre-existing"]
    assert len(matched) == 1
    # Original id and confidence preserved (not overwritten by the new entry)
    assert matched[0].id == "existing-1"
    assert matched[0].confidence == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# max_entries trim
# ---------------------------------------------------------------------------


async def test_max_entries_trim(tmp_path: Path) -> None:
    """Entries exceeding max_entries are trimmed to the highest-confidence set."""
    cfg = LearningsConfig(file=".agentshore/learnings.json", max_entries=3)
    h = LearningsHarvester(repo_root=tmp_path, learnings_cfg=cfg)

    # Pre-populate 3 entries at high confidence.
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

    # Outcome adds a 4th entry at low confidence — it should be trimmed away.
    outcome = _outcome(
        learnings=[{"pattern": "delta-low", "confidence": 0.3, "category": "general"}]
    )
    await h.update_learnings(outcome, PlayType.ISSUE_PICKUP)

    entries = load(path)
    assert len(entries) == 3
    patterns = {e.pattern for e in entries}
    assert "delta-low" not in patterns
    assert "alpha" in patterns


# ---------------------------------------------------------------------------
# Multiple learnings in one outcome
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Malformed entries in outcome.learnings are silently dropped
# ---------------------------------------------------------------------------


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
