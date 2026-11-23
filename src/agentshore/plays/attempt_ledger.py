"""Shared no-progress cooldown for targeted plays.

Several plays act on one pinned target (an issue or a PR). When the same play
runs against the same target twice and the target looks exactly as it did
before, a third attempt is provably wasted until something about the target
changes. This ledger is the single place that rule lives, keyed on *state*
rather than on error text:

* A target's **fingerprint** is the part of its snapshot a successful play would
  change (PR head, mergeability, review state, checks, labels; issue labels,
  bead status, plan state).
* Each completed attempt is recorded with the fingerprint the target had when it
  was dispatched. A repeat at an unchanged fingerprint increments the count —
  whether or not the earlier attempt reported success, because a "successful"
  play that leaves its target unchanged is just as unproductive as a failed one.
* At ``NO_PROGRESS_THRESHOLD`` attempts the pair is cooled down for a window of
  plays that doubles each time it re-arms, up to a cap.
* The cooldown ends the moment the fingerprint changes, the target leaves the
  snapshot, or the window elapses. It is a cost breaker, never a permanent park:
  terminal decisions stay with ``TerminalParkPolicy``.

This only withholds a target that cannot currently pay off; which play runs is
still entirely the policy's choice among the targets that remain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from agentshore.errors import FailureKind
from agentshore.github.labels import PLANNED_LABELS
from agentshore.state import PlayType

if TYPE_CHECKING:
    from collections.abc import Hashable

    from agentshore.plays.base import PlayParams
    from agentshore.state import OrchestratorState, PlayOutcome

# Attempts at one unchanged fingerprint before the pair is cooled down. Two, so a
# single transient failure (agent crash, flaky network) still gets one retry.
NO_PROGRESS_THRESHOLD: Final[int] = 2
# First cooldown window, in plays (``state.total_plays``); doubles per re-arm.
BASE_COOLDOWN_PLAYS: Final[int] = 15
MAX_COOLDOWN_PLAYS: Final[int] = 120

_PR_TARGET_PLAYS: Final[frozenset[PlayType]] = frozenset(
    {PlayType.UNBLOCK_PR, PlayType.CODE_REVIEW, PlayType.MERGE_PR}
)
_ISSUE_TARGET_PLAYS: Final[frozenset[PlayType]] = frozenset(
    {
        PlayType.ISSUE_PICKUP,
        PlayType.WRITE_IMPLEMENTATION_PLAN,
        PlayType.SYSTEMATIC_DEBUGGING,
    }
)
TARGETED_PLAY_TYPES: Final[frozenset[PlayType]] = _PR_TARGET_PLAYS | _ISSUE_TARGET_PLAYS

# (play_type.value, resource key such as "pr:42" / "issue:17")
TargetKey = tuple[str, str]


def target_key(play_type: PlayType, params: PlayParams) -> TargetKey | None:
    """The ledger key for a targeted play's pinned issue/PR, or None if untargeted."""
    if play_type in _PR_TARGET_PLAYS and isinstance(params.pr_number, int):
        return (play_type.value, f"pr:{params.pr_number}")
    if play_type in _ISSUE_TARGET_PLAYS and isinstance(params.issue_number, int):
        return (play_type.value, f"issue:{params.issue_number}")
    return None


def _fingerprint(state: OrchestratorState, key: TargetKey) -> Hashable | None:
    """What a productive play would change about the target; None if it is gone."""
    kind, _, raw_number = key[1].partition(":")
    number = int(raw_number)
    if kind == "pr":
        for pr in state.pull_requests:
            if pr.pr_number == number:
                return (
                    pr.state,
                    pr.head_sha,
                    pr.mergeable,
                    pr.review_decision,
                    pr.status_check_summary,
                    pr.is_draft,
                    pr.base_ref,
                    pr.last_reviewed_sha,
                    pr.last_review_status,
                    pr.blocked,
                    tuple(sorted(pr.blocked_reasons)),
                    tuple(sorted(pr.labels)),
                )
        return None
    for issue in state.open_issues:
        if issue.issue_number == number:
            labels = set(issue.labels)
            return (
                issue.state,
                tuple(sorted(labels)),
                issue.bead_status,
                issue.bead_ready,
                number in state.planned_issues or bool(PLANNED_LABELS & labels),
            )
    return None


@dataclass(slots=True)
class _Entry:
    fingerprint: Hashable
    attempts: int = 0
    arms: int = 0
    armed_until: int | None = None


class TargetAttemptLedger:
    """Session-scoped record of repeated attempts that left a target unchanged."""

    def __init__(self) -> None:
        self._entries: dict[TargetKey, _Entry] = {}

    def record(
        self,
        play_type: PlayType,
        params: PlayParams,
        state_at_dispatch: OrchestratorState,
        outcome: PlayOutcome,
    ) -> bool:
        """Record one completed attempt. Returns True when it armed a cooldown.

        Attempts that never exercised the target are ignored: skipped plays,
        plays that asked for a clean retry (lost claim, reclaimed worktree), and
        agent-side auth failures say nothing about whether the target can
        progress.
        """
        key = target_key(play_type, params)
        if key is None:
            return False
        if outcome.skipped or outcome.retry_requested or outcome.failure_kind == FailureKind.AUTH:
            return False
        fingerprint = _fingerprint(state_at_dispatch, key)
        if fingerprint is None:
            return False

        entry = self._entries.get(key)
        if entry is None or entry.fingerprint != fingerprint:
            # First attempt, or the target changed since the last one: progress
            # was possible, so start counting afresh.
            entry = _Entry(fingerprint=fingerprint)
            self._entries[key] = entry
        entry.attempts += 1
        if entry.attempts < NO_PROGRESS_THRESHOLD:
            return False
        window = min(BASE_COOLDOWN_PLAYS * (2**entry.arms), MAX_COOLDOWN_PLAYS)
        entry.armed_until = state_at_dispatch.total_plays + window
        entry.arms += 1
        entry.attempts = 0
        return True

    def cooled_down(self, state: OrchestratorState) -> frozenset[TargetKey]:
        """Pairs to withhold this tick, pruning any whose reason has lapsed."""
        withheld: set[TargetKey] = set()
        for key in list(self._entries):
            entry = self._entries[key]
            current = _fingerprint(state, key)
            if current is None or current != entry.fingerprint:
                # Target closed/merged/left the snapshot, or it changed: the
                # premise of the cooldown no longer holds.
                del self._entries[key]
                continue
            if entry.armed_until is None:
                continue
            if state.total_plays >= entry.armed_until:
                # Window elapsed with the target still unchanged: allow another
                # attempt but remember how often this pair has armed, so the
                # next window is longer.
                entry.armed_until = None
                continue
            withheld.add(key)
        return frozenset(withheld)

    def __len__(self) -> int:
        return len(self._entries)
