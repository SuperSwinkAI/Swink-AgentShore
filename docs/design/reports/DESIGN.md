# Reports — Functional Design

## Responsibility

Turn session data into self-contained, shareable HTML artifacts of a AgentShore run. Reports require no server — open the `.html` file in a browser. This component owns two concerns: a pure-data **aggregation layer** that pre-computes report content from the DataStore, and a **renderer** that fills Jinja2 templates with that data.

## Design Choices

- **Pure-data aggregation layer.** `ReportDataCollector` (`src/agentshore/reports/collector.py`) queries the DataStore and returns fully pre-computed `TypedDict`s (`types.py`) ready for templating. It has no dependency on Jinja2, the TUI, IPC, or the RL engine, so report logic is testable in isolation and reusable by any consumer. All number-crunching lives in `compute_*` helpers (`_aggregations.py`); loop-incident reconstruction in `_loop_incidents.py`; repo-URL resolution in `_repo_url.py`.
- **Single, self-consistent cost definition.** Every report derives total cost by summing the per-play `dollar_cost` values — the same rows the play log renders — rather than reading `session.total_cost`. This guarantees the session summary, end-of-session report, and comparison all agree on cost.
- **Skips are not failures.** Gated no-op plays are counted separately (`skipped_plays`) and excluded from failure counts and from the ESR play log.
- **Self-contained output.** `ReportGenerator` (`generator.py`) inlines vendored Chart.js for the charted reports; the ESR is deliberately chart-free and static.
- **Derived, not hardcoded.** Play-log column set, total play slots, and unique-agent/plays-in-use counts are computed from the play registry and history, not baked into the template.

## Report Types

| Report | Trigger | Contents |
|--------|---------|----------|
| **Session Summary** | Orchestrator on session end (also the TUI report action) | Full artifact: overview, play timeline, cost breakdown (by play type / by agent / cumulative), agent performance and specialization, failure analysis, scope-drift count, anti-confirmation audit, issue inflation, trajectory snapshots + analysis, learnings count, cleanup/revert count, loop incidents, code-review patterns, recommendations, epic summaries, and epic-closure timeline. Charted (Chart.js). |
| **End-of-Session Report (ESR)** | Orchestrator drain path on shutdown, and `agentshore stop` | Compact static page (no charts): overview, repo URL, per-play-type stats, control rejections (dispatch revalidation / selector rejections), closed issues, and a phased play log (rows per executed play, plus plays-in-use / total-slots and unique-agent counts). |
| **Progress** | Orchestrator mid-session (also the TUI report action) | Lightweight snapshot: overview, recent plays (last 10), remaining-budget estimate, and currently active agents. |
| **Archive Comparison** | Reports engine, on demand (cross-session comparison) | Side-by-side of two sessions: cost / alignment / play-count diffs, cost breakdowns, issue throughput, play distribution, alignment trajectories, and a learnings diff (added / removed / shared). Charted. |

## Data Sources

All reports read exclusively from the AgentShore SQLite DataStore (sessions, plays, agents, issues, scope drift, trajectory snapshots, learnings, review patterns, external mutations). Epic closure data is loaded from the beads project graph via `load_graph`. The collector performs no live IPC or agent calls — reports reflect persisted state only.

## Cross-References

- [HLD](../HLD.md) — component map.
- [Data Layer](../data/DESIGN.md) — SQLite schema backing every report.
- [Core](../core/DESIGN.md) — session lifecycle and the drain path that emits the ESR.
