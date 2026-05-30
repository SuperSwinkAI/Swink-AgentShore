"""Derived work-availability counts shared by masks, logs, and IPC."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshore.plays.candidates import (
    WorkAvailability,
    build_candidate_plan,
    design_audit_is_fresh,
    qa_ran_within_terminal_window,
    seed_audit_is_fresh,
    terminal_audits_are_fresh,
)

if TYPE_CHECKING:
    from agentshore.state import OrchestratorState

__all__ = [
    "WorkAvailability",
    "design_audit_is_fresh",
    "qa_ran_within_terminal_window",
    "seed_audit_is_fresh",
    "summarize_work_availability",
    "terminal_audits_are_fresh",
]


def summarize_work_availability(state: OrchestratorState) -> WorkAvailability:
    """Return derived availability counts for the current state snapshot."""

    return build_candidate_plan(state).work_availability
