# Reports — Functional Design

## Responsibility

Generate self-contained HTML reports from session data. Reports are the shareable, archival artifact of a AgentShore session. They require no server — just open the `.html` file in a browser.

## Report Types

| Report | Description |
|--------|-------------|
| **Session Summary** | Full session artifact covering play timeline, cost breakdown, agent performance, RL decisions, epic closure trajectory, scope drift, anti-confirmation bias audit, issue inflation, trajectory analysis, learnings, loop detection, and code review patterns. Generated automatically on session end or on demand. |
| **Progress** | Lightweight mid-session snapshot: epic closure ratios, issue throughput, recent plays, budget status, and active agents. |
| **Archive Comparison** | Side-by-side comparison of two archived sessions: cost, epic closure trajectories, issue throughput, play distribution, and accumulated learnings. Generated via `agentshore archive compare <id1> <id2>`. |
