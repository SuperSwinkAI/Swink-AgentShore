"""Typed override-queue entries that preserve enqueue-time intent."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentshore.plays.base import PlayParams
    from agentshore.rl.mask_reason import MaskClassification
    from agentshore.state import PlayType


class OverrideKind(StrEnum):
    """Producer kind for queued override entries."""

    BOOTSTRAP = "bootstrap"
    EXECUTOR_REQUEUE = "executor_requeue"
    RETRY = "retry"
    MASK_REQUEUE = "mask_requeue"
    RATE_LIMIT_RECOVERY = "rate_limit_recovery"
    UNKNOWN_ERROR_RECOVERY = "unknown_error_recovery"


@dataclass(frozen=True, slots=True)
class OverrideEntry:
    """Queued override plus retry and optional sequencing metadata."""

    play_type: PlayType
    params: PlayParams
    kind: OverrideKind
    enqueue_classification: MaskClassification | None = None
    requeue_attempts: int = 0
    wait_for_play_type: PlayType | None = field(default=None)

    def with_bumped_attempts(self) -> OverrideEntry:
        """Return a copy with ``requeue_attempts`` incremented by 1."""
        bumped_extras = {
            **self.params.extras,
            "mask_requeue_attempts": self.requeue_attempts + 1,
        }
        bumped_params = dataclasses.replace(self.params, extras=bumped_extras)
        return dataclasses.replace(
            self,
            params=bumped_params,
            requeue_attempts=self.requeue_attempts + 1,
        )
