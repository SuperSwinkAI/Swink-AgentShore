"""Typed override-queue entries.

Replaces the legacy ``tuple[PlayType, PlayParams]`` queue payload with a
typed record that carries enqueue-time intent. The consumer
(``_handle_masked_override``) reads ``kind`` and ``enqueue_classification``
instead of substring-matching the mask reason at dequeue time — so an
override's "why am I here" survives any number of mask cycles.

Five producer kinds, distinct retry/drop policies:

* ``BOOTSTRAP`` — sequencing-dependent bootstrap fleet entries. Never drop on
  mask; re-queue indefinitely until the awaited condition lifts.
* ``EXECUTOR_REQUEUE`` — anti-confirmation or transient staffing race from
  the executor. Bounded retries; transient classification.
* ``RETRY`` — automatic failure retry queued by the play-completion handler.
  Retry budget already enforced by the producer; consumer just dispatches.
* ``MASK_REQUEUE`` — re-queued by ``_handle_masked_override`` itself.
  Preserves the original producer's classification so the chain doesn't
  flatten back to substring matching.
* ``RATE_LIMIT_RECOVERY`` — loop-side ``take_break`` enqueued when an agent
  enters ``AgentStatus.ERROR`` with ``last_error_class == "rate_limit"``.
  Bypasses PPO entirely; PPO never picks ``take_break`` itself.
* ``UNKNOWN_ERROR_RECOVERY`` — loop-side ``take_break`` for a genuinely
  unclassified ERROR (``last_error_class in {unknown, codex_rollout}``). Same
  bypass-PPO take-a-break behavior as rate-limit recovery, but a distinct kind
  + telemetry so a true rate limit is never conflated with an unknown failure
  (#23/#24). Carries its own enqueue latch so the two paths don't clobber.
"""

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
    """Producer kind. Identifies why the entry was enqueued."""

    BOOTSTRAP = "bootstrap"
    EXECUTOR_REQUEUE = "executor_requeue"
    RETRY = "retry"
    MASK_REQUEUE = "mask_requeue"
    RATE_LIMIT_RECOVERY = "rate_limit_recovery"
    UNKNOWN_ERROR_RECOVERY = "unknown_error_recovery"


@dataclass(frozen=True, slots=True)
class OverrideEntry:
    """A queued override carrying enqueue-time intent through any number
    of mask-re-queue cycles.

    ``kind`` identifies the producer. ``enqueue_classification`` is the
    classification at enqueue time (e.g. BOOTSTRAP entries are usually
    ``INDEFINITE_WAIT``). ``requeue_attempts`` is the transient-retry
    counter, bumped only when the entry is re-queued for a TRANSIENT mask.

    ``wait_for_play_type`` is a targeted sequencing gate that is *additive*
    to ``params.bypass_preconditions``. When set, the consumer must hold the
    entry until that PlayType appears in ``state.plays_since_last_play_type``
    (i.e. has completed at least once). This lets bootstrap entries skip the
    instantiate cooldown while still waiting for the first-play (cleanup /
    seed_project) to finish before they spawn — closing the race documented
    in issue #569 where the medium agent appeared 8s after cleanup started
    and immediately raced trunk via PR-scoped plays.
    """

    play_type: PlayType
    params: PlayParams
    kind: OverrideKind
    enqueue_classification: MaskClassification | None = None
    requeue_attempts: int = 0
    wait_for_play_type: PlayType | None = field(default=None)

    def with_bumped_attempts(self) -> OverrideEntry:
        """Return a copy with ``requeue_attempts`` incremented by 1.

        Used by the consumer when a TRANSIENT-classified mask drove a
        re-queue. The ``params.extras["mask_requeue_attempts"]`` field is
        also bumped for backward compatibility with any consumer that still
        reads from ``extras`` directly.
        """
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
