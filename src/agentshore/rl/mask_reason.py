"""Typed mask reasons.

Replaces free-text mask reason strings with a typed record that carries a
classification at the source. Substring matching in
``Orchestrator._mask_reason_is_indefinite_wait`` /
``_mask_reason_is_transient`` collapses to an attribute lookup, and the
override queue can preserve enqueue-time intent through arbitrary requeue
cycles without re-classifying.

Three classifications:

* ``TRANSIENT`` — clears on staffing/quota/state shift. Staffing gaps,
  rate-limit pauses, "temporarily forced off" loop-prevention masks.
* ``INDEFINITE_WAIT`` — clears deterministically by elapsed time, sequencing,
  or play count. Bootstrap "waiting for seed_project", instantiate cooldown,
  drain mode, terminal-no-work evidence windows.
* ``HARD`` — action is structurally invalid. No candidate set, reserved
  action slot, capability missing, drain mode excludes this action class.

The free-text ``text`` field is preserved for logs and UI; ``__str__``
returns it so existing log emission sites keep working without change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class MaskClassification(StrEnum):
    """Why a mask is set and how it can clear."""

    TRANSIENT = "transient"
    INDEFINITE_WAIT = "indefinite_wait"
    HARD = "hard"


class MaskSource(StrEnum):
    """Which gate emitted the mask. Diagnostic, not behaviour-bearing."""

    PRECONDITION = "precondition"
    ELIGIBILITY = "eligibility"
    CANDIDATE = "candidate"
    CONFIG = "config"
    CONTROL = "control"
    DRAIN = "drain"
    TERMINAL = "terminal"
    RESERVED = "reserved"
    CIRCUIT_BREAKER = "circuit_breaker"


@dataclass(frozen=True, slots=True, eq=False)
class MaskReason:
    """A typed mask reason emitted at the source gate.

    ``text`` is the human-readable reason carried through logs and the UI.
    ``classification`` drives override-queue handling and policy decisions
    (e.g. whether to re-queue without bumping a retry counter).
    ``source`` is diagnostic — it identifies which gate within the mask
    pipeline emitted the reason and is useful for the new
    ``play_skipped_at_executor`` metric.

    String-compat: ``MaskReason`` compares equal to a bare ``str`` matching
    ``text``, supports ``in`` substring tests on ``text``, and provides
    ``.lower()`` / ``.upper()`` returning lowercased / uppercased text.
    Tests, log inspection, and UI string formatting all work without
    reaching into ``.text`` explicitly.
    """

    text: str
    classification: MaskClassification
    source: MaskSource

    def __str__(self) -> str:
        return self.text

    def __eq__(self, other: object) -> bool:
        if isinstance(other, MaskReason):
            return (
                self.text == other.text
                and self.classification == other.classification
                and self.source == other.source
            )
        if isinstance(other, str):
            return self.text == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.text, self.classification, self.source))

    def __contains__(self, substring: object) -> bool:
        """Allow ``"substring" in mask_reason`` to test the human text."""
        if not isinstance(substring, str):
            return False
        return substring in self.text

    def lower(self) -> str:
        """Return ``text.lower()`` for ``str``-style comparisons."""
        return self.text.lower()

    def upper(self) -> str:
        """Return ``text.upper()`` for ``str``-style comparisons."""
        return self.text.upper()


# Common pre-allocated instances for hot paths. Reuse where the reason text
# is fixed so we don't allocate per-tick.
SESSION_DRAINING: Final = MaskReason(
    text="Session draining: only end_agent permitted",
    classification=MaskClassification.INDEFINITE_WAIT,
    source=MaskSource.DRAIN,
)
RESERVED_SLOT: Final = MaskReason(
    text="Reserved action slot",
    classification=MaskClassification.HARD,
    source=MaskSource.RESERVED,
)
NOT_AVAILABLE: Final = MaskReason(
    text="Not currently available",
    classification=MaskClassification.HARD,
    source=MaskSource.PRECONDITION,
)
ACTION_MASKED: Final = MaskReason(
    text="action masked",
    classification=MaskClassification.HARD,
    source=MaskSource.CONTROL,
)
SELECTED_CANDIDATE_NO_LONGER_AVAILABLE: Final = MaskReason(
    text="selected candidate no longer available",
    classification=MaskClassification.HARD,
    source=MaskSource.CANDIDATE,
)
# Main-repo dispatch-pause latch is set: only end_agent / reconcile_state are
# permitted until the trunk is healed. Transient — clears when the pause lifts.
MAIN_REPO_DISPATCH_PAUSED: Final = MaskReason(
    text="main repo dispatch paused: only end_agent / reconcile_state permitted",
    classification=MaskClassification.TRANSIENT,
    source=MaskSource.CONTROL,
)
# END_SESSION is already started or in-flight. Transient — clears when that
# dispatch resolves.
END_SESSION_IN_FLIGHT: Final = MaskReason(
    text="end_session already in flight",
    classification=MaskClassification.TRANSIENT,
    source=MaskSource.CONTROL,
)
