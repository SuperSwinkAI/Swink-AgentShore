"""Override-dispatch state: the override queue + its single-consume latches.

``OverrideQueue`` owns the orchestrator's override-dispatch state:

* the FIFO ``asyncio.Queue`` of :class:`~agentshore.plays.override.OverrideEntry`
  (bootstrap recipe, human requests, executor requeues, rate-limit recovery),
* ``first_play_override`` — the seed/first-play override that wins over the
  queue,
* ``pending_override_kind`` — the **single-consume** latch set by
  ``_consume_override`` and read exactly once by ``_dispatch_play`` to mark the
  resulting ``_DispatchContext`` so the loop detector skips override dispatches,
* ``dispatched_play_ids`` — play_ids dispatched from an override, excluded from
  PPO-collapse streak math.

It is a thin collaborator (mirroring :class:`agentshore.core.github_syncer.GitHubSyncer`):
constructed in ``phases.py`` (well — in ``_OrchestratorBase.__init__``) and held
on the orchestrator as ``_overrides``. The queue methods delegate to the
internal ``asyncio.Queue`` so call sites read identically; the latches are plain
grouped attributes whose set/read/clear protocol is preserved byte-for-byte.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentshore.plays.base import PlayParams
    from agentshore.plays.override import OverrideEntry, OverrideKind
    from agentshore.state import PlayType


class OverrideQueue:
    """Owns the override FIFO plus the first-play / pending-kind / dispatched-id latches."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[OverrideEntry] = asyncio.Queue()
        # Seed / first-play override (set during bootstrap); wins over the queue.
        self.first_play_override: tuple[PlayType, PlayParams] | None = None
        # OverrideKind of the most-recently consumed override, set by
        # ``_consume_override`` and read once by ``_dispatch_play``. None means
        # the next dispatch is PPO-selected (not an override).
        self.pending_override_kind: OverrideKind | None = None
        # play_id values dispatched from the override queue (bootstrap recipe,
        # user request, retry). ``compute_play_streaks`` skips them — they are
        # not PPO-collapse, so they should not contribute to same_type_streak /
        # same_type_failure_streak. Unbounded; sessions have <10k plays.
        self.dispatched_play_ids: set[int] = set()

    # ------------------------------------------------------------------
    # FIFO delegation (identical method names to asyncio.Queue)
    # ------------------------------------------------------------------

    def put_nowait(self, entry: OverrideEntry) -> None:
        self._queue.put_nowait(entry)

    def get_nowait(self) -> OverrideEntry:
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()
