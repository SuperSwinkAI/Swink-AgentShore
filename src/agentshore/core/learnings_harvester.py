"""Learnings reinforcement and harvesting after play completion."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from agentshore.config import LearningsConfig
    from agentshore.state import PlayOutcome, PlayType


DEFAULT_LEARNING_CONFIDENCE = 0.5


class LearningsHarvester:
    """Reinforces and harvests learnings after play completions.

    Extracted from ``CompletionProcessor`` — all behaviour is verbatim.
    Constructed inside ``CompletionProcessor.__init__`` from the already-
    injected deps; ``CompletionProcessor.update_learnings`` delegates here.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        learnings_cfg: LearningsConfig,
    ) -> None:
        self._repo_root = repo_root
        self._learnings_cfg = learnings_cfg

    async def update_learnings(self, outcome: PlayOutcome, play_type: PlayType) -> None:
        """Harvest agent-emitted learnings on success; reinforce on re-harvest.

        Every successful skill-backed play emits a top-level ``learnings`` array
        (normalized by ``result_parser`` before reaching here). A new pattern is
        appended; re-emitting an existing exact-match pattern reinforces it in
        place — bumping confidence and resetting recency — so a repeatedly-useful
        insight survives session-start decay instead of being silently dropped.
        """
        from agentshore.learnings import (  # noqa: PLC0415
            Learning,
            load,
            save_atomic,
            top_k,
        )

        learnings_path = self._repo_root / self._learnings_cfg.file
        entries = await asyncio.to_thread(load, learnings_path)
        changed = False

        if outcome.success and outcome.play_id is not None and outcome.learnings:
            import uuid as _uuid  # noqa: PLC0415
            from dataclasses import replace  # noqa: PLC0415
            from datetime import UTC, datetime  # noqa: PLC0415

            for raw_entry in outcome.learnings:
                if not isinstance(raw_entry, dict):
                    continue
                pattern = raw_entry.get("pattern", "")
                if not isinstance(pattern, str) or not pattern:
                    continue
                existing = next((i for i, e in enumerate(entries) if e.pattern == pattern), None)
                if existing is not None:
                    # Re-harvest of a known pattern: reinforce in place.
                    prior = entries[existing]
                    entries[existing] = replace(
                        prior,
                        confidence=min(1.0, prior.confidence + 0.1),
                        sessions_since_use=0,
                        last_reinforced_play_id=outcome.play_id,
                    )
                    changed = True
                    continue
                raw_conf = raw_entry.get("confidence", DEFAULT_LEARNING_CONFIDENCE)
                entries.append(
                    Learning(
                        id=str(_uuid.uuid4()),
                        pattern=pattern,
                        confidence=float(raw_conf)
                        if isinstance(raw_conf, (int, float))
                        else DEFAULT_LEARNING_CONFIDENCE,  # noqa: E501
                        sessions_since_use=0,
                        source_play_id=outcome.play_id,
                        last_reinforced_play_id=outcome.play_id,
                        created_at=datetime.now(UTC).isoformat(),
                        category=str(raw_entry.get("category", "general")),
                    )
                )
                changed = True

        # Trim to max_entries keeping highest confidence
        if len(entries) > self._learnings_cfg.max_entries:
            entries = top_k(entries, k=self._learnings_cfg.max_entries)
            changed = True

        if changed:
            await asyncio.to_thread(save_atomic, learnings_path, entries)
