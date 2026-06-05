# Metrics — Functional Design

## Responsibility

Metrics turn raw session state and history into the derived signals that drive RL observations and rewards, the human-facing TUI/dashboard/report summaries, and debug diagnosis. The `MetricsEngine` (`src/agentshore/rl/metrics.py`) is the single producer: each call to `snapshot()` fetches session history from the DataStore and builds an `ObservationContext` consumed by the observation encoder.

Cross-references: [HLD](../HLD.md) lists this component; consumers are the [RL](../rl/DESIGN.md) engine and the [reports](../reports/DESIGN.md)/dashboard surfaces.

## Design Choices

- **Recomputed per snapshot, never stored.** There is no metrics table. Every snapshot fully recomputes from the play history, PR/issue/handoff/learning rows, and the live beads graph. This keeps metrics consistent with current state with no cache-invalidation surface, at the cost of a per-call query fan-out (budgeted to complete in well under the tick window). Query failures degrade gracefully — a failed fetch logs `metrics_query_failed` and falls back to an empty set rather than crashing the tick.
- **Alignment is beads-native.** Progress is measured against the beads epic/story/task graph (closure ratios), not against AgentShore's own SQLite. This makes the canonical project graph the source of truth for "are we done" rather than session-local bookkeeping.
- **Rolling windows over lifetime totals.** Velocity, success, cost, and duration are averaged over a fixed recent-play window so the policy reacts to current behaviour rather than being anchored by early-session history.

## Metric Categories

| Category | Examples | Purpose |
|----------|----------|---------|
| Alignment | `global_closure_ratio`, per-epic `closure_ratio` (top-3 by task count feed the observation), `alignment_delta`, `tasks_ready` | Measure progress toward project completion; drive reward. |
| Velocity / throughput | rolling velocity, issues closed/created, issue churn rate | Signal whether work is actually advancing. |
| Rolling play stats | rolling success rate, rolling cost, rolling duration | Recent agent/play performance. |
| Health / stagnation | idle-minute stagnation, success streak, agents-in-error | Detect stalls and degraded fleets. |
| Cluster drift | std-dev of per-epic closure ratios | Flag uneven progress across epics. |
| PR pipeline | open / awaiting-review / approved-unmerged counts, PR pressure ratio | Track review backlog and pressure near the open-PR cap. |
| Handoff / specialization | avg context-loss, per-(agent, play-type) success rates | Inform routing and agent selection. |

`alignment_delta` is `None` when the beads graph is unavailable, `0.0` on the first tick or no change, and otherwise the `global_closure_ratio` delta since the prior tick.
