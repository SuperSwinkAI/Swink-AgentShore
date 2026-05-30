"""Phase 4K: top-K learnings injected into context.json payload."""

from __future__ import annotations

import uuid
from pathlib import Path

from agentshore.learnings import Learning, save_atomic
from agentshore.plays.dispatch import serialize_state_for_skill
from agentshore.state import PlayType


def _make(pattern: str, confidence: float) -> Learning:
    return Learning(
        id=str(uuid.uuid4()),
        pattern=pattern,
        confidence=confidence,
        sessions_since_use=0,
        source_play_id=None,
        last_reinforced_play_id=None,
    )


def _base_kwargs(top_learnings: list[dict[str, object]]) -> dict[str, object]:

    from agentshore.plays.base import PlayParams

    return dict(
        session_id="sess",
        play_id=1,
        play_type=PlayType.ISSUE_PICKUP,
        skill_name="agentshore-issue-pickup",
        params=PlayParams(agent_id="agent-1", issue_number=5),
        open_issues=[],
        budget_enabled=True,
        budget_total=10.0,
        budget_spent=1.0,
        learnings_count=len(top_learnings),
        top_learnings=top_learnings,
        mode="solo",
    )


def test_top_learnings_in_payload() -> None:
    top = [{"pattern": "use typed params", "confidence": 0.9}]
    payload = serialize_state_for_skill(**_base_kwargs(top))  # type: ignore[arg-type]
    assert payload["learnings"] == top
    assert payload["learnings_count"] == 1


def test_empty_learnings_in_payload() -> None:
    payload = serialize_state_for_skill(**_base_kwargs([]))  # type: ignore[arg-type]
    assert payload["learnings"] == []
    assert payload["learnings_count"] == 0


def test_learnings_file_roundtrip_and_inject(tmp_path: Path) -> None:
    """Write 15 learnings, load top-10, verify payload contains ≤10."""
    path = tmp_path / ".agentshore" / "learnings.json"
    entries = [_make(f"pattern-{i}", confidence=i / 15) for i in range(15)]
    save_atomic(path, entries)

    from agentshore.learnings import load, top_k

    loaded = load(path)
    top = top_k(loaded, k=10)
    top_dicts = [{"pattern": e.pattern, "confidence": round(e.confidence, 2)} for e in top]

    payload = serialize_state_for_skill(**_base_kwargs(top_dicts))  # type: ignore[arg-type]
    assert len(payload["learnings"]) == 10  # type: ignore[arg-type]
    assert payload["learnings_count"] == 10
