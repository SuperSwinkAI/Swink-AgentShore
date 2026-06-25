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
        """Reinforce learnings on success; harvest new entries after GROOM_BACKLOG."""
        from agentshore.learnings import (  # noqa: PLC0415
            Learning,
            load,
            reinforce,
            save_atomic,
            top_k,
        )
        from agentshore.state import PlayType as _PlayType  # noqa: PLC0415

        learnings_path = self._repo_root / self._learnings_cfg.file
        entries = await asyncio.to_thread(load, learnings_path)
        changed = False

        if outcome.success and outcome.play_id is not None:
            # Build a reinforcement key from skill_name + play_type + artifact paths
            artifact_paths = " ".join(
                str(a.get("path", "")) for a in outcome.artifacts if isinstance(a, dict)
            )
            reinforce_key = f"{play_type.value} {artifact_paths}".strip()
            reinforced = reinforce(entries, reinforce_key, source_play_id=outcome.play_id)
            if any(
                r.last_reinforced_play_id != e.last_reinforced_play_id
                for r, e in zip(reinforced, entries, strict=True)
            ):
                entries = reinforced
                changed = True

        # Harvest learnings carried directly in the outcome (all successful
        # skill-backed plays emit a top-level ``learnings`` array, normalized
        # by result_parser before reaching here).
        if outcome.success and outcome.play_id is not None and outcome.learnings:
            import uuid as _uuid  # noqa: PLC0415
            from datetime import UTC, datetime  # noqa: PLC0415

            for raw_entry in outcome.learnings:
                if not isinstance(raw_entry, dict):
                    continue
                pattern = raw_entry.get("pattern", "")
                if not isinstance(pattern, str) or not pattern:
                    continue
                if any(e.pattern == pattern for e in entries):
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

        # Harvest new learnings from GROOM_BACKLOG artifacts
        if play_type == _PlayType.GROOM_BACKLOG and outcome.success:
            import uuid as _uuid  # noqa: PLC0415
            from datetime import UTC, datetime  # noqa: PLC0415

            for artifact in outcome.artifacts:
                if not isinstance(artifact, dict):
                    continue
                if artifact.get("type") != "learnings":
                    continue
                raw_learnings = artifact.get("learnings", [])
                if not isinstance(raw_learnings, list):
                    continue
                for raw_entry in raw_learnings:
                    if not isinstance(raw_entry, dict):
                        continue
                    pattern = raw_entry.get("pattern", "")
                    if not isinstance(pattern, str) or not pattern:
                        continue
                    if any(e.pattern == pattern for e in entries):
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
