"""Phase 4K: Session learnings — load/save/prune/decay/reinforce/top_k."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

import pytest

from agentshore.learnings import (
    Learning,
    decay,
    load,
    prune,
    reinforce,
    save_atomic,
    top_k,
)


def _make(
    pattern: str,
    confidence: float = 0.5,
    sessions_since_use: int = 0,
) -> Learning:
    return Learning(
        id=str(uuid.uuid4()),
        pattern=pattern,
        confidence=confidence,
        sessions_since_use=sessions_since_use,
        source_play_id=None,
        last_reinforced_play_id=None,
    )


# ---------------------------------------------------------------------------
# load / save_atomic roundtrip
# ---------------------------------------------------------------------------


def test_load_save_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "learnings.json"
    entries = [_make("use typed params", confidence=0.8), _make("always validate scope", 0.6)]
    save_atomic(path, entries)
    loaded = load(path)
    assert len(loaded) == 2
    assert loaded[0].pattern == "use typed params"
    assert loaded[0].confidence == 0.8
    assert loaded[1].pattern == "always validate scope"


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load(tmp_path / "nonexistent.json") == []


def test_load_corrupt_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not valid json", encoding="utf-8")
    assert load(path) == []


def test_save_atomic_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "learnings.json"
    save_atomic(path, [_make("x")])
    assert path.exists()


def test_save_atomic_survives_concurrent_read(tmp_path: Path) -> None:
    """Write completes atomically — concurrent readers never see partial JSON."""
    path = tmp_path / "learnings.json"
    save_atomic(path, [_make("seed")])

    results: list[bool] = []
    started_reading = threading.Event()
    stop = threading.Event()

    def reader() -> None:
        # Loop until the writer signals it has finished its writes. The first
        # successful read flips ``started_reading`` so the writer knows the
        # reader is actively contending for the file.
        while not stop.is_set():
            try:
                content = path.read_text(encoding="utf-8")
                json.loads(content)
                results.append(True)
            except OSError:
                # Windows can report a transient sharing violation while a
                # concurrent os.replace is in progress. That is not a partial
                # JSON read, and the next read should observe either the old
                # file or the new complete file.
                results.append(True)
            except Exception:
                results.append(False)
            started_reading.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    # Wait for the reader to make at least one pass before we start writing,
    # guaranteeing concurrency without relying on sleeps.
    assert started_reading.wait(timeout=5), "reader thread never started"
    for i in range(20):
        save_atomic(path, [_make(f"entry-{i}", confidence=i * 0.05)])
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive(), "reader thread did not exit"
    assert results, "reader recorded no observations"
    assert all(results), "reader saw a corrupt partial file"


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


def test_prune_removes_below_threshold() -> None:
    entries = [_make("a", 0.5), _make("b", 0.29), _make("c", 0.3)]
    result = prune(entries, min_confidence=0.3)
    assert len(result) == 2
    assert all(e.confidence >= 0.3 for e in result)


def test_prune_empty_list() -> None:
    assert prune([], min_confidence=0.3) == []


# ---------------------------------------------------------------------------
# decay
# ---------------------------------------------------------------------------


def test_decay_halves_stale_entries() -> None:
    entries = [_make("stale", confidence=0.8, sessions_since_use=5)]
    result = decay(entries, factor=0.5, threshold_sessions=5)
    assert result[0].confidence == pytest.approx(0.4)


def test_decay_leaves_recent_entries_unchanged() -> None:
    entries = [_make("fresh", confidence=0.8, sessions_since_use=4)]
    result = decay(entries, factor=0.5, threshold_sessions=5)
    assert result[0].confidence == pytest.approx(0.8)


def test_decay_does_not_go_below_zero() -> None:
    entries = [_make("very_stale", confidence=0.1, sessions_since_use=10)]
    result = decay(entries, factor=0.5, threshold_sessions=5)
    assert result[0].confidence >= 0.0


# ---------------------------------------------------------------------------
# reinforce
# ---------------------------------------------------------------------------


def test_reinforce_bumps_matching_entry() -> None:
    entries = [_make("issue_pickup", confidence=0.5)]
    result = reinforce(entries, pattern="issue_pickup", source_play_id=7)
    assert result[0].confidence == pytest.approx(0.6)
    assert result[0].sessions_since_use == 0
    assert result[0].last_reinforced_play_id == 7


def test_reinforce_requires_exact_match() -> None:
    """A stored pattern is only reinforced on an exact match, not a substring."""
    entries = [_make("issue_pickup", confidence=0.5)]
    result = reinforce(entries, pattern="issue_pickup area/backend", source_play_id=7)
    assert result[0].confidence == pytest.approx(0.5)
    assert result[0].last_reinforced_play_id is None


def test_reinforce_caps_at_1_0() -> None:
    entries = [_make("issue_pickup", confidence=0.95)]
    result = reinforce(entries, pattern="issue_pickup", source_play_id=1)
    assert result[0].confidence <= 1.0


def test_reinforce_leaves_non_matching_unchanged() -> None:
    entries = [_make("code_review")]
    result = reinforce(entries, pattern="issue_pickup", source_play_id=1)
    assert result[0].confidence == pytest.approx(0.5)
    assert result[0].last_reinforced_play_id is None


# ---------------------------------------------------------------------------
# top_k
# ---------------------------------------------------------------------------


def test_top_k_returns_highest_confidence() -> None:
    entries = [_make("a", 0.3), _make("b", 0.9), _make("c", 0.6), _make("d", 0.1)]
    result = top_k(entries, k=2)
    assert len(result) == 2
    assert result[0].pattern == "b"
    assert result[1].pattern == "c"


def test_top_k_returns_all_when_fewer_than_k() -> None:
    entries = [_make("x", 0.5)]
    assert len(top_k(entries, k=10)) == 1


def test_top_k_empty_list() -> None:
    assert top_k([], k=5) == []


# ---------------------------------------------------------------------------
# Phase 5 readiness smoke
# ---------------------------------------------------------------------------


def test_import_smoke() -> None:
    from agentshore.learnings import Learning, load, save_atomic, top_k

    assert callable(load)
    assert callable(save_atomic)
    assert callable(top_k)
    assert Learning.__dataclass_fields__  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# scope field — A3
# ---------------------------------------------------------------------------


def test_scope_default_is_project() -> None:
    """Learning created without scope defaults to 'project'."""
    entry = _make("use typed params")
    assert entry.scope == "project"


def test_scope_global_round_trips_json(tmp_path: Path) -> None:
    """A Learning with scope='global' survives a save/load cycle."""
    path = tmp_path / "learnings.json"
    entry = Learning(
        id="g1",
        pattern="global pattern",
        confidence=0.7,
        sessions_since_use=0,
        source_play_id=None,
        last_reinforced_play_id=None,
        scope="global",
    )
    save_atomic(path, [entry])
    loaded = load(path)
    assert len(loaded) == 1
    assert loaded[0].scope == "global"
    assert loaded[0].id == "g1"


def test_scope_defaults_when_absent_from_json(tmp_path: Path) -> None:
    """JSON without a 'scope' key loads as 'project' (backward-compatibility)."""
    import json

    path = tmp_path / "learnings.json"
    # Write JSON that has no 'scope' key.
    path.write_text(
        json.dumps(
            [
                {
                    "id": "legacy-1",
                    "pattern": "old pattern",
                    "confidence": 0.5,
                    "sessions_since_use": 0,
                    "source_play_id": None,
                    "last_reinforced_play_id": None,
                    "created_at": "2025-01-01T00:00:00+00:00",
                }
            ]
        ),
        encoding="utf-8",
    )
    loaded = load(path)
    assert len(loaded) == 1
    assert loaded[0].scope == "project"


# ---------------------------------------------------------------------------
# Global learnings merge logic — A4
# ---------------------------------------------------------------------------


def test_global_entries_added_when_not_in_project(tmp_path: Path) -> None:
    """Global-scope entries not present in the project list are included."""
    global_path = tmp_path / "global_learnings.json"
    global_entry = Learning(
        id="global-1",
        pattern="global tip",
        confidence=0.8,
        sessions_since_use=0,
        source_play_id=None,
        last_reinforced_play_id=None,
        scope="global",
    )
    save_atomic(global_path, [global_entry])

    project_entries = [_make("local tip")]

    # Simulate the merge logic from core.py's load_learnings step.
    global_entries = load(global_path)
    project_ids = {e.id for e in project_entries}
    for ge in global_entries:
        if getattr(ge, "scope", "project") == "global" and ge.id not in project_ids:
            project_entries.append(ge)

    ids = [e.id for e in project_entries]
    assert "global-1" in ids


def test_project_entry_wins_on_id_collision(tmp_path: Path) -> None:
    """When a global entry has the same id as a project entry, the project entry wins."""
    shared_id = "shared-id"
    global_path = tmp_path / "global_learnings.json"
    global_entry = Learning(
        id=shared_id,
        pattern="global version",
        confidence=0.9,
        sessions_since_use=0,
        source_play_id=None,
        last_reinforced_play_id=None,
        scope="global",
    )
    save_atomic(global_path, [global_entry])

    project_entry = Learning(
        id=shared_id,
        pattern="project version",
        confidence=0.5,
        sessions_since_use=0,
        source_play_id=None,
        last_reinforced_play_id=None,
        scope="project",
    )
    project_entries = [project_entry]

    # Simulate merge logic.
    global_entries = load(global_path)
    project_ids = {e.id for e in project_entries}
    for ge in global_entries:
        if getattr(ge, "scope", "project") == "global" and ge.id not in project_ids:
            project_entries.append(ge)

    # Only one entry with this id, and it's the project one.
    matched = [e for e in project_entries if e.id == shared_id]
    assert len(matched) == 1
    assert matched[0].pattern == "project version"


def test_non_global_entries_excluded_from_merge(tmp_path: Path) -> None:
    """Global-file entries with scope != 'global' are not merged into the project."""
    global_path = tmp_path / "global_learnings.json"
    non_global_entry = Learning(
        id="proj-in-global",
        pattern="project-scoped but in global file",
        confidence=0.6,
        sessions_since_use=0,
        source_play_id=None,
        last_reinforced_play_id=None,
        scope="project",
    )
    save_atomic(global_path, [non_global_entry])

    project_entries: list[Learning] = []

    # Simulate merge logic.
    global_entries = load(global_path)
    project_ids = {e.id for e in project_entries}
    for ge in global_entries:
        if getattr(ge, "scope", "project") == "global" and ge.id not in project_ids:
            project_entries.append(ge)

    assert len(project_entries) == 0, "non-global entry should not be merged"


# ---------------------------------------------------------------------------
# category field — #486
# ---------------------------------------------------------------------------


def test_category_default_is_general() -> None:
    """Learning created without category defaults to 'general'."""
    entry = _make("use typed params")
    assert entry.category == "general"


def test_category_round_trips_json(tmp_path: Path) -> None:
    """A Learning with category='security' survives a save/load cycle."""
    path = tmp_path / "learnings.json"
    entry = Learning(
        id="c1",
        pattern="never trust user input",
        confidence=0.9,
        sessions_since_use=0,
        source_play_id=None,
        last_reinforced_play_id=None,
        category="security",
    )
    save_atomic(path, [entry])
    loaded = load(path)
    assert len(loaded) == 1
    assert loaded[0].category == "security"
    assert loaded[0].id == "c1"


def test_category_defaults_when_absent_from_json(tmp_path: Path) -> None:
    """JSON without a 'category' key loads as 'general' (backward-compatibility)."""
    path = tmp_path / "learnings.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "legacy-2",
                    "pattern": "old pattern",
                    "confidence": 0.5,
                    "sessions_since_use": 0,
                    "source_play_id": None,
                    "last_reinforced_play_id": None,
                    "created_at": "2025-01-01T00:00:00+00:00",
                }
            ]
        ),
        encoding="utf-8",
    )
    loaded = load(path)
    assert len(loaded) == 1
    assert loaded[0].category == "general"


def test_decay_preserves_category() -> None:
    """decay() preserves the category of stale entries."""
    entries = [
        Learning(
            id="d1",
            pattern="stale rule",
            confidence=0.8,
            sessions_since_use=5,
            source_play_id=None,
            last_reinforced_play_id=None,
            category="database",
        )
    ]
    result = decay(entries, factor=0.5, threshold_sessions=5)
    assert result[0].category == "database"


def test_reinforce_preserves_category() -> None:
    """reinforce() preserves the category of matched entries."""
    entries = [
        Learning(
            id="r1",
            pattern="validate inputs",
            confidence=0.5,
            sessions_since_use=0,
            source_play_id=None,
            last_reinforced_play_id=None,
            category="testing",
        )
    ]
    result = reinforce(entries, pattern="validate inputs", source_play_id=9)
    assert result[0].category == "testing"


# ---------------------------------------------------------------------------
# consolidate — near-duplicate merge (Tier-1)
# ---------------------------------------------------------------------------


def _make_full(
    pattern: str,
    *,
    confidence: float = 0.5,
    sessions_since_use: int = 0,
    last_reinforced_play_id: int | None = None,
    created_at: str = "2025-01-01T00:00:00+00:00",
    category: str = "general",
) -> Learning:
    return Learning(
        id=str(uuid.uuid4()),
        pattern=pattern,
        confidence=confidence,
        sessions_since_use=sessions_since_use,
        source_play_id=None,
        last_reinforced_play_id=last_reinforced_play_id,
        created_at=created_at,
        category=category,
    )


def test_consolidate_merges_near_duplicates() -> None:
    from agentshore.learnings import consolidate

    entries = [
        _make_full("always run the tests before opening a PR", confidence=0.6),
        _make_full("always run the tests before opening PR", confidence=0.8),
    ]
    result = consolidate(entries, overlap_threshold=0.8)
    assert len(result) == 1
    # Representative is the highest-confidence member's text, confidence = max.
    assert result[0].pattern == "always run the tests before opening PR"
    assert result[0].confidence == pytest.approx(0.8)


def test_consolidate_folds_recency_and_reinforcement() -> None:
    from agentshore.learnings import consolidate

    entries = [
        _make_full(
            "use typed config params everywhere",
            confidence=0.5,
            sessions_since_use=4,
            last_reinforced_play_id=3,
            created_at="2025-02-01T00:00:00+00:00",
        ),
        _make_full(
            "use typed config params always",
            confidence=0.7,
            sessions_since_use=1,
            last_reinforced_play_id=9,
            created_at="2025-01-01T00:00:00+00:00",
        ),
    ]
    result = consolidate(entries, overlap_threshold=0.6)
    assert len(result) == 1
    merged = result[0]
    assert merged.confidence == pytest.approx(0.7)
    assert merged.sessions_since_use == 1  # min
    assert merged.last_reinforced_play_id == 9  # most recent
    assert merged.created_at == "2025-01-01T00:00:00+00:00"  # earliest


def test_consolidate_leaves_distinct_patterns_untouched() -> None:
    from agentshore.learnings import consolidate

    entries = [
        _make_full("validate scope after every play"),
        _make_full("never fork the upstream repository"),
    ]
    result = consolidate(entries, overlap_threshold=0.8)
    assert len(result) == 2


def test_consolidate_does_not_merge_across_categories() -> None:
    from agentshore.learnings import consolidate

    entries = [
        _make_full("always validate the input data", category="security"),
        _make_full("always validate the input data", category="testing"),
    ]
    result = consolidate(entries, overlap_threshold=0.8)
    assert len(result) == 2


def test_consolidate_disabled_when_threshold_non_positive() -> None:
    from agentshore.learnings import consolidate

    entries = [
        _make_full("always run the tests before opening a PR"),
        _make_full("always run the tests before opening PR"),
    ]
    assert len(consolidate(entries, overlap_threshold=0.0)) == 2


# ---------------------------------------------------------------------------
# fold_learnings — shared deterministic metadata fold
# ---------------------------------------------------------------------------


def test_fold_learnings_folds_metadata_with_chosen_identity() -> None:
    from agentshore.learnings import fold_learnings

    sources = [
        _make_full(
            "a",
            confidence=0.6,
            sessions_since_use=4,
            last_reinforced_play_id=3,
            created_at="2025-02-01T00:00:00+00:00",
        ),
        _make_full(
            "b",
            confidence=0.8,
            sessions_since_use=1,
            last_reinforced_play_id=9,
            created_at="2025-01-01T00:00:00+00:00",
        ),
    ]
    folded = fold_learnings(sources, pattern="merged", category="conventions", id="chosen-id")
    assert folded.id == "chosen-id"
    assert folded.pattern == "merged"
    assert folded.category == "conventions"
    assert folded.confidence == pytest.approx(0.8)  # max
    assert folded.sessions_since_use == 1  # min
    assert folded.last_reinforced_play_id == 9  # most recent
    assert folded.created_at == "2025-01-01T00:00:00+00:00"  # earliest


def test_fold_learnings_none_reinforcement_when_no_source_reinforced() -> None:
    from agentshore.learnings import fold_learnings

    sources = [_make_full("a", last_reinforced_play_id=None)]
    folded = fold_learnings(sources, pattern="p", category="general", id="x")
    assert folded.last_reinforced_play_id is None
