"""State-only audit-freshness predicates for the RL mask hot path.

These read only ``state`` (cooldown counters), so they are genuine
module-level functions rather than analyzer methods — callers on the hot
RL mask path (``rl/mask.py``) must not allocate a full ``PlayCandidateAnalyzer``
(which eagerly computes issue/PR sets) just to read one counter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.play_rules import (
    DESIGN_AUDIT_FRESHNESS_WINDOW_PLAYS,
    SEED_PROJECT_COOLDOWN_PLAYS,
    TERMINAL_SHUTDOWN_EVIDENCE_WINDOW_PLAYS,
)
from agentshore.state import PlayType

if TYPE_CHECKING:
    from agentshore.state import OrchestratorState


def seed_audit_is_fresh(state: OrchestratorState) -> bool:
    """Return True when a successful seed audit is still inside cooldown."""
    if state.last_play_success_by_type.get(PlayType.SEED_PROJECT) is not True:
        return False
    plays_since = state.plays_since_last_play_type.get(PlayType.SEED_PROJECT)
    return plays_since is not None and plays_since < SEED_PROJECT_COOLDOWN_PLAYS


def design_audit_is_fresh(
    state: OrchestratorState, *, window: int = DESIGN_AUDIT_FRESHNESS_WINDOW_PLAYS
) -> bool:
    """Return True when a successful design audit is still inside the requested window."""
    if state.last_play_success_by_type.get(PlayType.DESIGN_AUDIT) is not True:
        return False
    plays_since = state.plays_since_last_play_type.get(PlayType.DESIGN_AUDIT)
    return plays_since is not None and plays_since < window


def terminal_audits_are_fresh(state: OrchestratorState) -> bool:
    """Return True when the audit posture justifies a clean shutdown.

    Two paths into True:
      - Seeded sessions: BOTH seed_project AND design_audit recent
        (the original gate — preserves end_session evidence for the
        normal lifecycle where SEED_PROJECT runs at bootstrap).
      - Open-start sessions: SEED_PROJECT was never run in this
        session at all, so only design_audit recency is required.
        Without this fallback the failsafe is structurally unreachable
        in open-start mode (observed 2026-05-28 session 08a948ed:
        150 plays, $43 spent, end_session permanently masked because
        no seed audit ever fired).
    """
    if not design_audit_is_fresh(state, window=TERMINAL_SHUTDOWN_EVIDENCE_WINDOW_PLAYS):
        return False
    seed_ever_succeeded = state.last_play_success_by_type.get(PlayType.SEED_PROJECT) is True
    if not seed_ever_succeeded:
        return True
    return seed_audit_is_fresh(state)


def qa_ran_within_terminal_window(
    state: OrchestratorState, *, window: int = TERMINAL_SHUTDOWN_EVIDENCE_WINDOW_PLAYS
) -> bool:
    """Return True when successful RUN_QA is recent enough to end a no-work session."""
    if state.last_play_success_by_type.get(PlayType.RUN_QA) is not True:
        return False
    plays_since = state.plays_since_last_play_type.get(PlayType.RUN_QA)
    return plays_since is not None and plays_since < window
