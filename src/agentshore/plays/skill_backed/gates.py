"""Declarative precondition gates for skill-backed plays.

Each ``Gate`` is a callable returning ``MaskReason | None``. A play declares
``gates = (Gate1(...), Gate2(...), ...)`` and the base class walks the tuple,
collecting non-None reasons. This centralises the per-play state-walking that
previously lived in 17 bespoke ``preconditions()`` bodies — new plays declare
gates instead of re-deriving them, mask reason text stays consistent across
plays that share semantics, and gate behavior is unit-tested in one place.

The three legacy helpers on ``SkillBackedPlay`` (``_capability_check``,
``_in_flight_check``, ``_cooldown_check``) are kept as thin wrappers around
their gate equivalents so heavy plays that still override ``preconditions()``
continue to work without change. Heavy plays can ALSO opt into ``gates`` and
``super().preconditions(state)`` for the standard set, then append bespoke
checks afterward.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from agentshore.agents.capabilities import AGENT_CAPABILITIES
from agentshore.errors import ErrorClass
from agentshore.rl.mask_reason import MaskClassification, MaskReason, MaskSource
from agentshore.state import AgentStatus, PlayType, is_agent_circuit_broken

if TYPE_CHECKING:
    from agentshore.state import OrchestratorState


class Gate(Protocol):
    """A precondition gate. Returns ``None`` to pass; ``MaskReason`` to mask."""

    def __call__(self, state: OrchestratorState) -> MaskReason | None: ...


class CapabilityGate:
    """Mask unless an IDLE non-rate-limited agent has the named capability.

    Lifted verbatim from ``SkillBackedPlay._capability_check`` so behavior and
    mask reason text stay byte-identical across the migration.
    """

    __slots__ = ("capability",)

    def __init__(self, capability: str) -> None:
        self.capability = capability

    def __call__(self, state: OrchestratorState) -> MaskReason | None:
        rate_limited: set[str] = {
            a.agent_type.value
            for a in state.agents
            if a.status == AgentStatus.ERROR and a.last_error_class == ErrorClass.RATE_LIMIT
        }
        capable = [
            a
            for a in state.agents
            if a.status == AgentStatus.IDLE
            and a.agent_type.value not in rate_limited
            # Circuit breaker (#22): a known-dead agent (0 successes + a timeout
            # or repeated failures) is masked from work selection until it
            # succeeds, so plays aren't routed to it.
            and not is_agent_circuit_broken(
                tasks_completed=a.tasks_completed,
                tasks_failed=a.tasks_failed,
                timeout_count=a.timeout_count,
                consecutive_timeouts=a.consecutive_timeouts,
            )
            and bool(AGENT_CAPABILITIES.get(a.agent_type, {}).get(self.capability, False))
        ]
        if capable:
            return None
        return MaskReason(
            text=f"no IDLE agent with {self.capability} capability",
            classification=MaskClassification.TRANSIENT,
            source=MaskSource.ELIGIBILITY,
        )


class InFlightGate:
    """Mask while the named play type is already executing somewhere.

    Lifted from ``SkillBackedPlay._in_flight_check``.
    """

    __slots__ = ("play_type",)

    def __init__(self, play_type: PlayType) -> None:
        self.play_type = play_type

    def __call__(self, state: OrchestratorState) -> MaskReason | None:
        if self.play_type not in state.in_flight_plays:
            return None
        return MaskReason(
            text=f"{self.play_type.value} already in flight",
            classification=MaskClassification.TRANSIENT,
            source=MaskSource.PRECONDITION,
        )


class CooldownGate:
    """Mask for ``plays`` plays after the named play type last completed.

    Lifted from ``SkillBackedPlay._cooldown_check``.

    Counts only *completed* occurrences: ``plays_since_last_play_type`` is built
    from ended plays, and a just-completed play is bridged into it via
    ``_recent_play_completions`` (closing the WAL-flush persist race that let
    same-tick duplicates slip the cooldown). A same-type play that is still
    *in flight* is owned by the sibling ``InFlightGate`` — every standard-cooldown
    play declares both — so this gate intentionally does not re-mask it (doing so
    would only emit a duplicate reason). The effective cooldown reaching this gate
    is pinned by ``test_registry_cooldown_plumbing`` after the #344-session
    finding that a stale build enforced ~20 instead of the configured value.
    """

    __slots__ = ("play_type", "plays")

    def __init__(self, play_type: PlayType, plays: int) -> None:
        self.play_type = play_type
        self.plays = plays

    def __call__(self, state: OrchestratorState) -> MaskReason | None:
        cooldown = state.plays_since_last_play_type.get(self.play_type)
        if cooldown is None or cooldown >= self.plays:
            return None
        return MaskReason(
            text=f"{self.play_type.value} cooldown ({cooldown}/{self.plays} plays since last)",
            classification=MaskClassification.INDEFINITE_WAIT,
            source=MaskSource.PRECONDITION,
        )


class ArmedByFailureGate:
    """Armed/consumed gate for self-heal plays.

    State machine: ``closed`` → ``armed`` (any non-self play type's latest
    outcome is failure, that failure happened more recently than this play's
    last completion) → ``consumed`` (this play completes — success OR
    failure) → ``closed`` until a new non-self failure re-arms.

    Self-failures cannot self-arm: ``age < own_age`` is strict, and the gated
    play's own ``age == own_age`` exactly, so it's filtered out implicitly.

    Designed for plays that should run when SOMETHING ELSE is wedged — e.g.
    RECONCILE_STATE should fire when a recent merge_pr/cleanup/etc. failed.
    The gate does NOT track per-tick decay: armed stays armed across any
    number of intervening successes, until the gated play runs.
    """

    __slots__ = ("play_type",)

    def __init__(self, play_type: PlayType) -> None:
        self.play_type = play_type

    def __call__(self, state: OrchestratorState) -> MaskReason | None:
        own_age = state.plays_since_last_play_type.get(self.play_type, float("inf"))
        # Arm only on a GENUINE failure since this play last ran. A no-op
        # ``skip:*`` outcome is recorded success=False but is not a wedge —
        # arming on it lets a write_impl skip ↔ reconcile arm/run pair spin
        # forever making zero progress (the no-op spin root).
        armed = any(
            age < own_age
            and state.last_play_success_by_type.get(pt) is False
            and not state.last_play_skipped_by_type.get(pt, False)
            for pt, age in state.plays_since_last_play_type.items()
        )
        if armed:
            return None
        return MaskReason(
            text=f"no observable wedge since last {self.play_type.value}",
            classification=MaskClassification.TRANSIENT,
            source=MaskSource.PRECONDITION,
        )


class WarmupGate:
    """Mask until ``state.total_plays`` reaches ``threshold``.

    Optional ``prerequisite``: when set, the warmup floor is only enforced
    once that prerequisite play type has completed at least once this
    session (otherwise the floor is a permanent "never" on projects whose
    prerequisite has been masked out). Mirrors the seed_project-aware
    warmup currently in ``CleanupPlay`` (issue #564).
    """

    __slots__ = ("threshold", "prerequisite")

    def __init__(self, threshold: int, prerequisite: PlayType | None = None) -> None:
        self.threshold = threshold
        self.prerequisite = prerequisite

    def __call__(self, state: OrchestratorState) -> MaskReason | None:
        if state.total_plays >= self.threshold:
            return None
        if (
            self.prerequisite is not None
            and self.prerequisite not in state.plays_since_last_play_type
        ):
            return None
        return MaskReason(
            text=f"warmup floor ({state.total_plays}/{self.threshold} plays)",
            classification=MaskClassification.INDEFINITE_WAIT,
            source=MaskSource.PRECONDITION,
        )


class BeadsInitializedGate:
    """Mask unless the beads graph exists and has at least one epic.

    Used by plays that operate on the beads project graph (calibrate_alignment,
    design_audit, groom_backlog). The ``no_epics_hint`` parameter lets each
    play customise the no-epics reason text to match its prior diagnostic
    message.
    """

    __slots__ = ("_no_epics_hint",)

    def __init__(self, no_epics_hint: str = "run seed_project first") -> None:
        self._no_epics_hint = no_epics_hint

    def __call__(self, state: OrchestratorState) -> MaskReason | None:
        if state.graph is None:
            return MaskReason(
                text="beads not initialised — run seed_project first",
                classification=MaskClassification.INDEFINITE_WAIT,
                source=MaskSource.PRECONDITION,
            )
        if not state.graph.has_epics:
            return MaskReason(
                text=f"no epics in beads graph — {self._no_epics_hint}",
                classification=MaskClassification.INDEFINITE_WAIT,
                source=MaskSource.PRECONDITION,
            )
        return None


class DependenciesResolvedGate:
    """Mask if the selected candidate issue has unresolved blocked_by_ids.

    When the beads graph is present, each ``GraphTask`` carries a
    ``blocked_by_ids`` frozenset — the IDs of dependency tasks that are not yet
    closed.  A non-empty set means the task is blocked and the issue linked to
    it must not be dispatched: the agent would spend real API budget (typically
    ~$0.27) before discovering the dependency is still open.

    Gate behaviour:
    - If ``state.graph`` is ``None`` (beads not initialised), pass — other
      gates / candidate validity handle the no-graph case.
    - If the graph has no tasks with ``blocked_by_ids``, pass.
    - If *every* open issue is linked to a task with unresolved
      ``blocked_by_ids``, mask — the play cannot make progress this tick.
    - If at least one open issue has no unresolved dep, pass — the candidate
      selector will pick that issue.

    This is a play-level gate (state-only, no per-candidate view), so it masks
    the whole play type only when every candidate would be dep-blocked.  The
    per-candidate ``bead_blocked_issue_numbers`` filter in
    ``PlayCandidateAnalyzer.issue_available_for_pickup / issue_available_for_plan``
    is the fine-grained filter; this gate is the coarse-grained early exit that
    prevents the play from being selected at all when nothing is workable.
    """

    __slots__ = ()

    def __call__(self, state: OrchestratorState) -> MaskReason | None:
        graph = state.graph
        if graph is None or not graph.has_epics:
            return None

        # Build the set of issue numbers whose linked bead task has unresolved deps.
        blocked_issue_numbers: set[int] = {
            task.issue_number
            for task in graph.tasks
            if task.issue_number is not None and bool(task.blocked_by_ids)
        }
        if not blocked_issue_numbers:
            return None  # no blocked tasks → nothing to gate

        open_numbers = {i.issue_number for i in state.open_issues if i.state.upper() == "OPEN"}
        if not open_numbers:
            return None  # no open issues → CapabilityGate / candidate validity handles this

        # If there is at least one open issue NOT in the blocked set, the play can proceed.
        if open_numbers - blocked_issue_numbers:
            return None

        # Every open issue is dep-blocked.
        return MaskReason(
            text=(
                f"all {len(open_numbers)} open issue(s) blocked by unresolved beads dependencies"
                " — dispatch would discover this post-launch and waste agent budget"
            ),
            classification=MaskClassification.TRANSIENT,
            source=MaskSource.PRECONDITION,
        )


class FirstRunWarmupGate:
    """Mask on the play type's first run until ``total_plays`` reaches ``threshold``.

    Unlike ``WarmupGate`` (which masks unconditionally below the threshold),
    this gate only fires when the play has never run this session
    (``play_type not in state.plays_since_last_play_type``). After the first
    execution the gate passes permanently.

    Optional ``prerequisite``: the warmup floor is only enforced when the
    prerequisite play type has actually run this session. On projects where
    the prerequisite is permanently masked (e.g. seed_project on an existing
    project), skipping the warmup lets the play fire immediately.
    """

    __slots__ = ("play_type", "threshold", "prerequisite")

    def __init__(
        self,
        play_type: PlayType,
        threshold: int,
        prerequisite: PlayType | None = None,
    ) -> None:
        self.play_type = play_type
        self.threshold = threshold
        self.prerequisite = prerequisite

    def __call__(self, state: OrchestratorState) -> MaskReason | None:
        first_run = self.play_type not in state.plays_since_last_play_type
        if not first_run:
            return None
        if (
            self.prerequisite is not None
            and self.prerequisite not in state.plays_since_last_play_type
        ):
            return None
        if state.total_plays >= self.threshold:
            return None
        return MaskReason(
            text=f"warmup floor ({state.total_plays}/{self.threshold} plays)",
            classification=MaskClassification.INDEFINITE_WAIT,
            source=MaskSource.PRECONDITION,
        )
