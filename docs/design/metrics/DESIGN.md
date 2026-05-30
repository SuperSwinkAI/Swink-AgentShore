# Metrics — Functional Design

## Responsibility

Metrics feed RL observations/rewards, human-facing TUI/dashboard/report summaries, and debug/session diagnosis. They are recomputed per snapshot rather than stored in a separate metrics table.

## Alignment Metrics

Alignment is beads-native:

| Metric | Meaning |
|--------|---------|
| `global_closure_ratio` | Fraction of all beads tasks closed across the graph. |
| `epic_closure_ratio` | Per-epic closure ratios. Top-3 largest epics feed the observation. |
| `alignment_delta` | Change in global closure ratio around a play. `None` means the graph was unavailable. |
| `tasks_ready` | Ready beads tasks count, surfaced in graph state and debug logs. |
