# AgentShore — High-Level Design

## Purpose

AgentShore is an RL-based orchestrator that coordinates multiple LLM coding agents to autonomously progress coding projects. It works from GitHub issues and the beads project graph: GitHub is the human conversation surface, while beads is the canonical epic/story/task graph used for alignment. The human steers by triaging issues on GitHub (opening, closing, labeling, reprioritizing), not through AgentShore-specific goal config or approval UIs. GitHub is the shared control surface between the human and AgentShore.

The v1 implementation contract is [V1_CONTRACT.md](V1_CONTRACT.md). When component design docs disagree, the v1 contract wins.

AgentShore operates in four modes:

- **Solo mode**: Standalone Python process with a Textual TUI for terminal-native monitoring. The human sees the TUI but steers via GitHub issues.
- **Embedded agent mode**: Headless process managed by the host process, reporting state over local IPC. The host platform can also interact with GitHub.
- **Dashboard mode**: Browser-based dashboard served through the local bridge process for richer visual monitoring.
- **Policy mode**: `learning` is the default PPO loop. `audit-replay` uses greedy masked policy selection with PPO learning off for debugging, auditing, and regression testing policy choices.

The RL agent selects "plays" (22 discrete actions: 19 active + 3 reserved, action-space version 13) and assigns them to coding agents. It does not generate code.
For agent lifecycle, a busy fleet is an expansion signal rather than a blocker:
`Instantiate Agent` can add another enabled type/tier while caps allow it, but is
masked when every enabled type/tier already has an idle agent available.

## Implementation Status

Phases 1-6 have shipped: the agent layer, play system, RL engine, core orchestrator, Textual TUI, IPC transports, reports, archive, offline training, dashboard bridge, and packaged desktop app (version 0.3.0) are complete. The action space is locked at 22 discrete actions (19 active + 3 reserved, action-space version 13), and the observation vector is 246-dim (observation version 13). Policy version 5. SQLite schema version 4 with 22 tables.

## Tech Stack

| Layer | Choice |
|-------|--------|
| Language | Python 3.12+ |
| Async runtime | asyncio |
| RL framework | PyTorch (custom policy network) |
| State persistence | SQLite |
| Async SQLite driver | aiosqlite |
| Solo UI | Textual (TUI) |
| Reports | Jinja2 → static HTML |
| Report charts | Chart.js (embedded in HTML reports) |
| IPC (embedded mode) | Unix domain socket or TCP, NDJSON protocol |
| Agent communication | asyncio subprocess (CLI agents), httpx (API LLMs) |
| Configuration | YAML |
| Logging | structlog → NDJSON |

## Architecture Overview

GitHub and beads are the external control planes. AgentShore observes them, encodes a state vector, lets the PPO selector choose a play, resolves concrete parameters, dispatches the play to a coding agent or internal lifecycle handler, records the result in SQLite, and updates rewards/checkpoints. Solo TUI, dashboard, desktop, and embedded IPC all sit on top of that same core loop.

### Skill Architecture

AgentShore dispatches pre-built prompt templates ("skills") to coding agents rather than generating natural-language instructions at runtime. Each skill-backed play has a corresponding template under `src/agentshore/skills/templates/` that is rendered with minimal parameters and invoked on a coding agent via its CLI.

Skills ship as project-scoped files in `.agents/skills/`, making them version-controlled, human-invokable, and portable with the project. AgentShore itself is not an LLM — it is a pure RL agent that selects skills by name and passes parameters. All discovery, reasoning, and code execution happen inside the coding agents.

## Component Map

| Component | Responsibility | Design Doc |
|-----------|---------------|------------|
| [Core](core/DESIGN.md) | Orchestrator loop, session lifecycle, play dispatch, auto-configuration, GitHub/beads orchestration, scope validation, feedback checkpoints, loop detection, session knowledge, session archival | `docs/design/core/` |
| [RL Engine](rl/DESIGN.md) | Policy network, state encoding, reward computation, training, masking, policy modes | `docs/design/rl/` |
| [Agent Manager](agents/DESIGN.md) | Agent lifecycle, subprocess management, API adapters, handoff tracking, context enrichment, anti-confirmation enforcement | `docs/design/agents/` |
| [Play System](plays/DESIGN.md) | 22 play slots (19 active + 3 reserved), execution contracts, parameter schemas, parameter resolution | `docs/design/plays/` |
| [Data Layer](data/DESIGN.md) | SQLite schema, persistence, migrations, session archives, learnings store | `docs/design/data/` |
| [Config](config/DESIGN.md) | YAML schema, validation, hot-reload | `docs/design/config/` |
| [UI / TUI](ui/DESIGN.md) | Textual TUI layout, widgets, keybindings | `docs/design/ui/` |
| [IPC](ipc/DESIGN.md) | Local IPC protocol, embedded mode integration | `docs/design/ipc/` |
| [Logging](logging/DESIGN.md) | Structured logging, NDJSON output, correlation IDs | `docs/design/logging/` |
| [Error Handling](errors/DESIGN.md) | Error taxonomy, recovery strategies, escalation | `docs/design/errors/` |
| [Reports](reports/DESIGN.md) | HTML report generation, Chart.js visualizations, session summaries | `docs/design/reports/` |
| [Metrics](metrics/DESIGN.md) | Alignment tracking, cost accounting, observability | `docs/design/metrics/` |
| [Dashboard](dashboard/DESIGN.md) | Browser bridge, pixel-art canvas state rendering, demo transport | `docs/design/dashboard/` |
| [Desktop](desktop/DESIGN.md) | Tauri shell (app version 0.2.1), bd sidecar IPC, start screen, and packaged dashboard stack | `docs/design/desktop/` |

## Data Flow

### Three-Layer Architecture

AgentShore operates across three interlocking layers:

- **BEADS**: the canonical project graph, including epics, stories, and tasks.
- **GitHub**: the human conversation surface, mirrored with `external_ref="gh-N"`.
- **AgentShore SQLite**: session-scoped RL state, including plays, rewards, and policy checkpoints.

SEED_PROJECT is the first play of any new session. It invokes the `agentshore-seed-project` skill, which calls `bd` to build an epic→story→task graph from the open GitHub issues. Subsequent work is driven by `bd ready` output via ISSUE_PICKUP — each ready task maps to a GitHub issue referenced by `external_ref`. GITHUB is therefore the shared conversation layer between the human and AgentShore: the human steers by triaging issues; AgentShore acts by creating/closing issues and PRs; BEADS tracks the structural project graph; and AgentShore SQLite records all RL state (plays, rewards, experience, checkpoints).

The core loop is: auto-detect and seed the BEADS graph from GitHub issues, then loop (observe state, RL forward pass with masking, dispatch play to agent, collect outcome, compute reward, update policy). Issues created by QA and Code Review feed back into the work queue automatically.

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Single process | No microservices | asyncio handles 2-5 agents without service overhead. |
| SQLite | No server process | Single-file, sufficient for single-user workloads. |
| PyTorch | Custom policy network | Novel action space needs custom state/action encoding. |
| Textual TUI | Terminal-native | Users live in the terminal; no browser context switch. |
| YAML config | Standard conventions | Standard daemon config format. |
| NDJSON logging | Machine-parseable | Standard structured logging. |
| Local IPC | Unix socket + TCP fallback | Fast local transport with portable fallback. |
| GitHub as work queue | Shared control surface | Human steers by triaging issues, not AgentShore-specific config. |
| Seeded project graph | BEADS from GH issues | No config ceremony; adapts as issues change. |
| Autonomous by default | Escalation-only approval | Maximizes throughput; human intervenes on ambiguity or failure. |
| YOLO posture | GH writes are normal | Issue creation, PRs, merges are expected autonomous actions with audit records. |
| Skill-based dispatch | Pre-built templates | AgentShore has no LLM; it selects skills by name, agents handle execution. |
| Project-scoped skills | `.agents/skills/` in repo | Version-controlled, human-invokable, portable with the project. |
| Anti-confirmation bias | Hard invariant at executor | Architectural guarantee, not prompt-based convention. |
| Loop detection | 3-tier escalation | Prevents wasted budget on repeated failures while preserving RL learning. |
| Immediate reward | JSON result from skill output | Simple and sufficient; deferred attribution is future work. |

## Security Boundaries

- **Process isolation**: Each coding agent runs as a separate subprocess with its own filesystem access.
- **No secrets in config/DB**: API keys come from environment variables or OS keychain only.
- **No code execution**: AgentShore dispatches plays; agents execute code. Agent output is logged, never eval'd.
- **GitHub writes**: All AgentShore-created issues/PRs are labeled `agentshore/*` for auditability.
- **Per-agent identities**: CLI agents can bind to distinct GitHub identities (git authorship + `GH_TOKEN`) applied as per-subprocess env overlays; tokens never appear in log events.
- **Learnings injection**: Filtered by confidence score and capped at `max_prompt_entries` to limit prompt injection risk.
- **Skill integrity**: Skill templates (`.agents/skills/`) control agent instructions; treat with the same rigor as CI pipeline definitions.
