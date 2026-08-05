"""Single owner for the orchestrator's shared mutable session state.

Before this module existed, the eight composed orchestrator components
(:class:`LoopRunner`, :class:`Dispatcher`, :class:`CompletionProcessor`,
:class:`DrainController`, :class:`LifecycleController`, :class:`StateBuilder`,
:class:`SnapshotProjector`) each reached ~40 orchestrator latches through
``self._host.<attr>`` — a typed-but-shared mutable surface indexed by six
``_*Host`` Protocols, with ``getattr(self._host, "_x", default)`` guards
sprinkled in because tests built the host via ``__new__`` (skipping
``__init__``, so the attributes were absent).

:class:`SessionRuntime` collapses that surface into one owned dataclass. Every
latch now has a single greppable owner; ``Orchestrator`` constructs exactly one
``SessionRuntime`` and passes it to each component as ``runtime=``. Because every
field has a default, a fresh ``SessionRuntime()`` is always fully initialised —
which is what lets the getattr guards and the class-level ``__new__``-bypass
default wall disappear.

The orchestrator still owns *stable* identity/collaborators (``_store``,
``_manager``, ``_executor``, ``_session_id``, ``_repo_root``, ``_overrides``,
``_velocity``, ``_recovery``, ``_main_repo``, the components themselves) and the
*behaviour* methods (``_safe_call``, ``_initiate_autonomous_stop``,
``_check_stagnation_escalation``, ``effective_budget_caps``, ``resume``,
``_selector_config_index``, ``_weights_dir``, ``stop_loop_liveness_watchdog``).
Components reach behaviour through a narrow ``host`` reference; they reach *state*
only through ``runtime``.

Defaults mirror the values previously set in ``_OrchestratorBase.__init__`` so
behaviour is preserved exactly.
"""

from __future__ import annotations

import asyncio
import collections
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentshore.config import RuntimeConfig
from agentshore.state import NullStateProvider

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agentshore.agents.health import HealthMonitor
    from agentshore.agents.worktree import WorktreeManager
    from agentshore.core.context import _DispatchContext
    from agentshore.core.experience_recorder import ExperienceRecorder
    from agentshore.core.progress_monitor import ForwardProgressMonitor
    from agentshore.data.integrity import IntegrityMonitor
    from agentshore.data.store import PlayRecord
    from agentshore.plays.selector import PlaySelector
    from agentshore.power import PowerAssertion
    from agentshore.rl.metrics import MetricsEngine
    from agentshore.state import PlayOutcome, StateProvider

    NaturalExitCallback = Callable[[str], Awaitable[None]]


def _new_completion_idle() -> asyncio.Event:
    """Return an :class:`asyncio.Event` that starts *set* (no work in flight)."""
    event = asyncio.Event()
    event.set()
    return event


def _new_pause_event() -> asyncio.Event:
    """Return the pause gate, starting *set* (running, not paused)."""
    event = asyncio.Event()
    event.set()
    return event


class LifecyclePhase(StrEnum):
    """Mutually exclusive phases of one orchestrator session."""

    RUNNING = "running"
    DRAINING = "draining"
    STOP_REQUESTED = "stop_requested"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass
class SessionLifecycle:
    """Validated lifecycle transitions and their associated reasons."""

    phase: LifecyclePhase = LifecyclePhase.RUNNING
    stop_reason: str = "unknown"
    drain_reason: str | None = None
    drain_initialized: bool = False

    def request_drain(self, reason: str | None = None) -> bool:
        if self.phase in {
            LifecyclePhase.STOP_REQUESTED,
            LifecyclePhase.STOPPING,
            LifecyclePhase.STOPPED,
        }:
            return False
        if self.phase is LifecyclePhase.RUNNING:
            self.phase = LifecyclePhase.DRAINING
        if reason is not None:
            self.drain_reason = reason
        return True

    def begin_drain(self, reason: str) -> bool:
        if self.drain_initialized or not self.request_drain(reason):
            return False
        self.drain_initialized = True
        self.stop_reason = reason
        return True

    def request_stop(self, reason: str | None = None) -> bool:
        if self.phase in {LifecyclePhase.STOPPING, LifecyclePhase.STOPPED}:
            return False
        self.phase = LifecyclePhase.STOP_REQUESTED
        if reason is not None:
            self.stop_reason = reason
        return True

    def begin_stop(self, default_reason: str = "stop_requested") -> bool:
        if self.phase in {LifecyclePhase.STOPPING, LifecyclePhase.STOPPED}:
            return False
        self.phase = LifecyclePhase.STOPPING
        if not self.drain_initialized:
            self.stop_reason = default_reason
        return True

    def mark_stopped(self) -> None:
        self.phase = LifecyclePhase.STOPPED

    def resume_from_drain(self) -> bool:
        if self.phase is not LifecyclePhase.DRAINING:
            return False
        self.phase = LifecyclePhase.RUNNING
        self.drain_initialized = False
        self.drain_reason = None
        self.stop_reason = ""
        return True


@dataclass
class SessionRuntime:
    """All orchestrator state read or written live by the composed components.

    Field defaults reproduce ``_OrchestratorBase.__init__`` exactly so a fresh
    ``SessionRuntime()`` is indistinguishable from a freshly-constructed
    orchestrator's latch wall — this is what retires the ``__new__``-bypass
    class-level defaults and the ``getattr(self._host, ...)`` guards.
    """

    # --- reload-mutable config (atomically swapped on SIGHUP by lifecycle) ----
    cfg: RuntimeConfig = field(default_factory=RuntimeConfig)

    # --- bootstrap-assigned services (None until phases.py wires them) --------
    selector: PlaySelector | None = None
    state_provider: StateProvider = field(default_factory=NullStateProvider)
    registry: object | None = None
    metrics: MetricsEngine | None = None
    worktrees: WorktreeManager | None = None
    experience_recorder: ExperienceRecorder | None = None
    progress_monitor: ForwardProgressMonitor | None = None
    health: HealthMonitor | None = None
    integrity: IntegrityMonitor | None = None
    power_assertion: PowerAssertion | None = None
    policy_version: str = "ppo-v1"
    config_path: Path | None = None
    log_path: Path | None = None
    embedded_mode: bool = False
    esr_ready_callback: Callable[[str, str, str | None], None] | None = None
    session_draining_callback: Callable[[str, str], None] | None = None
    natural_exit_callback: NaturalExitCallback | None = None

    # --- stop / drain / pause state ------------------------------------------
    lifecycle: SessionLifecycle = field(default_factory=SessionLifecycle)
    stop_done: asyncio.Event = field(default_factory=asyncio.Event)
    pause_event: asyncio.Event = field(default_factory=_new_pause_event)
    pause_reason: str | None = None
    pause_deadline: float | None = None
    natural_exit_reason: str | None = None

    # --- end-session report flags --------------------------------------------
    end_session_dispatch_started: bool = False
    end_session_report_requested: bool = False
    end_session_report_open_browser: bool = False

    # --- live budget/time cap overrides (None ⇒ fall through to cfg.budget) ---
    budget_override_enabled: bool | None = None
    budget_override_total: float | None = None
    time_override_enabled: bool | None = None
    time_override_minutes: int | None = None

    # --- selection / idle / refresh latches ----------------------------------
    idle_streak: int = 0
    last_selection_digest: bytes | None = None
    # Monotonic ts of last _refresh_issues; 0.0 = never (first eligible tick
    # always fires). Bootstrap stamps it after the session-start fetch.
    last_refresh_time: float = 0.0
    last_play_id: int | None = None
    loop_started_at: float = 0.0

    # --- in-flight dispatch bookkeeping (mutated in place) -------------------
    in_flight: dict[str, asyncio.Task[PlayOutcome]] = field(default_factory=dict)
    dispatch_ctx: dict[str, _DispatchContext] = field(default_factory=dict)
    completion_processing_count: int = 0
    completion_processing_idle: asyncio.Event = field(default_factory=_new_completion_idle)

    # --- feedback cadence -----------------------------------------------------
    feedback_cadence_plays_since_ack: int = 0
    feedback_cadence_last_ack_monotonic: float = field(default_factory=time.monotonic)

    # --- per-agent / per-resource shadows & windows --------------------------
    context_pressure_hints: dict[str, float] = field(default_factory=dict)
    recent_play_outcomes: collections.deque[tuple[bool, str]] = field(
        default_factory=lambda: collections.deque(maxlen=50)
    )
    recent_play_completions: collections.deque[PlayRecord] = field(
        default_factory=lambda: collections.deque(maxlen=64)
    )
    recent_applied_labels: collections.deque[tuple[int, str]] = field(
        default_factory=lambda: collections.deque(maxlen=64)
    )
    resource_failure_counts: dict[str, int] = field(default_factory=dict)
    parked_resource_keys: set[str] = field(default_factory=set)
    # Agent types that hit a *transient* launch wedge (Grok first-byte timeout).
    # DECAYING suppression: agent_type -> expiry tick. State-builder seeds
    # (tick + _GROK_WEDGE_COOLDOWN_TICKS) and
    # drops expired ones each snapshot, so a wedged type auto-recovers (#202).
    # last_play_id is the tick reference.
    wedge_cooldown_until: dict[str, int] = field(default_factory=dict)

    # Compatibility accessors keep component reads and test fixtures stable
    # while all production transitions route through ``lifecycle`` methods.
    @property
    def stop_requested(self) -> bool:
        return self.lifecycle.phase in {
            LifecyclePhase.STOP_REQUESTED,
            LifecyclePhase.STOPPING,
            LifecyclePhase.STOPPED,
        }

    @stop_requested.setter
    def stop_requested(self, value: bool) -> None:
        if value:
            self.lifecycle.request_stop()
        elif self.lifecycle.phase is LifecyclePhase.STOP_REQUESTED:
            self.lifecycle.phase = (
                LifecyclePhase.DRAINING
                if self.lifecycle.drain_initialized
                else LifecyclePhase.RUNNING
            )

    @property
    def stopped(self) -> bool:
        # Historical callers use ``stopped`` as the stop-body ownership latch.
        return self.lifecycle.phase in {LifecyclePhase.STOPPING, LifecyclePhase.STOPPED}

    @stopped.setter
    def stopped(self, value: bool) -> None:
        if value:
            self.lifecycle.begin_stop()
        elif self.stopped:
            self.lifecycle.phase = LifecyclePhase.RUNNING

    @property
    def draining(self) -> bool:
        return self.lifecycle.phase is LifecyclePhase.DRAINING

    @draining.setter
    def draining(self, value: bool) -> None:
        if value:
            self.lifecycle.request_drain(self.lifecycle.drain_reason)
        elif self.lifecycle.phase is LifecyclePhase.DRAINING:
            self.lifecycle.resume_from_drain()

    @property
    def drain_initialized(self) -> bool:
        return self.lifecycle.drain_initialized

    @drain_initialized.setter
    def drain_initialized(self, value: bool) -> None:
        if value and self.lifecycle.phase is LifecyclePhase.RUNNING:
            self.lifecycle.phase = LifecyclePhase.DRAINING
        self.lifecycle.drain_initialized = value

    @property
    def stop_reason(self) -> str:
        return self.lifecycle.stop_reason

    @stop_reason.setter
    def stop_reason(self, value: str) -> None:
        self.lifecycle.stop_reason = value

    @property
    def drain_reason(self) -> str | None:
        return self.lifecycle.drain_reason

    @drain_reason.setter
    def drain_reason(self, value: str | None) -> None:
        self.lifecycle.drain_reason = value
