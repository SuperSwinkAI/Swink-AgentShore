"""Typed outcomes of a single RL-loop tick.

``LoopRunner._run_loop_body`` historically returned a bare ``bool`` (True =
break the loop, False = re-iterate) and performed every terminal side effect
inline at each of its ~15 return sites. That made the most order-sensitive
method in the codebase hard to reason about: the decision of *what* a tick
should do was tangled with *doing* it.

This module models the tick decision as a small closed union. ``_resolve_tick``
performs the reads/harvests it needs and returns one of these actions;
``_apply_tick_action`` performs the single terminal effect and collapses back to
the break/continue ``bool``. Behavior is identical — the actions simply name the
six terminal shapes the old method already had.

The union members:

* :class:`Break` — stop the loop (old ``return True``).
* :class:`Continue` — re-iterate immediately (old ``return False``).
* :class:`Pause` — ``pause_with_reason(reason)`` then re-iterate.
* :class:`WaitInFlight` — back off on in-flight work, then re-iterate. Carries
  the ``idle_backoff`` wait class and, for the selector-idle path, the state to
  emit a structured ``play_skipped`` for before waiting.
* :class:`WaitIdle` — resolve the truly-idle case via
  ``continue_if_selector_idle_work_remains`` (break unless idle-work remains).
* :class:`Dispatch` — create the play task, then wait on the resulting in-flight
  work (or break if the fleet went idle).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentshore.plays.base import PlayParams
    from agentshore.state import OrchestratorState, PlayType


@dataclass(frozen=True, slots=True)
class Break:
    """The loop should stop this iteration (old ``return True``)."""


@dataclass(frozen=True, slots=True)
class Continue:
    """The loop should re-iterate immediately (old ``return False``)."""


@dataclass(frozen=True, slots=True)
class Pause:
    """Enter a reason-gated pause, then re-iterate.

    ``_apply_tick_action`` calls ``LifecycleController.pause_with_reason(reason)``
    and returns False so the loop blocks at the pause gate next iteration.
    """

    reason: str


@dataclass(frozen=True, slots=True)
class WaitInFlight:
    """Back off on in-flight work, then re-iterate (returns False).

    ``wait_class`` is forwarded to ``idle_backoff`` for the wait timeout. When
    ``emit_skipped_state`` is set (the selector-idle path), a structured
    ``play_skipped`` event is emitted for that state *before* the wait, exactly
    as the old inline path did.
    """

    wait_class: str
    emit_skipped_state: OrchestratorState | None = None


@dataclass(frozen=True, slots=True)
class WaitIdle:
    """Resolve the truly-idle case (no in-flight work, nothing to dispatch).

    ``_apply_tick_action`` returns ``not continue_if_selector_idle_work_remains(
    state, reason)`` — i.e. break unless idle-work remains.
    """

    state: OrchestratorState
    reason: str


@dataclass(frozen=True, slots=True)
class Dispatch:
    """Dispatch the selected play, then wait on the resulting in-flight work.

    ``_apply_tick_action`` calls ``Dispatcher.dispatch_play`` and, if the play
    was actually dispatched, waits on in-flight work (returns False) or breaks
    when the fleet is idle.
    """

    play_type: PlayType
    params: PlayParams
    state: OrchestratorState


TickAction = Break | Continue | Pause | WaitInFlight | WaitIdle | Dispatch
