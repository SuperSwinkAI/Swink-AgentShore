# UI / TUI — Functional Design

## Responsibility

The TUI provides real-time monitoring and control for TUI-mode AgentShore via a Textual application. In embedded agent mode, the TUI is not used — state is reported over IPC and rendered by the host UI.

## Framework

[Textual](https://textual.textualize.io/) — Python TUI framework built on Rich.

## Layout

```
┌─ AgentShore ─────────────────────────────────────────────────────────┐
│ ┌─ Agents ──────────────────┐ ┌─ Active Play ──────────────────┐ │
│ │ ● claude-code    BUSY     │ │ ▶ Issue Pickup #47             │ │
│ │   ctx: 45k/200k  $0.42   │ │   Agent: claude-code           │ │
│ │ ● codex-cli      IDLE    │ │   Phase: implementing          │ │
│ │   ctx: 0k/192k   $0.18   │ │   Elapsed: 3m12s               │ │
│ │ ○ gemini-cli     OFF     │ │   Est. cost: ~$0.15            │ │
│ └───────────────────────────┘ └────────────────────────────────┘ │
│ ┌─ Play History ──────────────────────────────────────────────-┐ │
│ │ TIME   PLAY                      AGENT    RESULT  Δ    COST │ │
│ │ 14:02  Calibrate Alignment       claude   ✓      +.08 $0.03│ │
│ │ 14:05  Issue Pickup #46          claude   ✓      +.15 $0.31│ │
│ │ 14:11  Code Review #46           codex    ✓      +.02 $0.08│ │
│ │ 14:12  Merge PR #46              —        ✓      +.01 $0.00│ │
│ │ 14:13  Issue Pickup #47          claude   ◷      ...   ...  │ │
│ └──────────────────────────────────────────────────────────────┘ │
│ ┌─ Epic Closure ────────────┐ ┌─ Budget ───────────────────────┐ │
│ │ Auth   8/10  ████████░░  │ │ Spent: $0.84 / $200.00        │ │
│ │ API    6/10  ██████░░░░  │ │ Est. remaining: ~$0.60        │ │
│ │ Tests  4/10  ████░░░░░░  │ │ Plays: 5 of ~12 est.         │ │
│ └───────────────────────────┘ └────────────────────────────────┘ │
│                                                                   │
│ [P]ause  [R]eport  [I]ssues  [O]verride  [A]pprovals  [L]earn [Q]uit │
│ ▸ RL confidence: 0.82  Next likely: Code Review #47              │
└───────────────────────────────────────────────────────────────────┘
```

## Keybindings

| Key | Action |
|-----|--------|
| `p` | Pause/resume the orchestrator loop |
| `r` | Generate and open a session report |
| `i` | Open work queue — issues/PRs grouped by status |
| `o` | Override next play selection (presents play picker) |
| `q` | Graceful shutdown (triggers End Session play) |
| `l` | Open Learnings View overlay |
| `d` | Toggle detail panel for selected play |
| `?` | Help overlay |

## Human Override Flow

Pressing `o` pauses the orchestrator loop and presents a play picker overlay showing the active action order with masked plays disabled (with `mask_reasons` text). After selecting an eligible play type, a parameter selection overlay shows heuristic defaults for play-specific parameters (e.g., which issue, which agent) and allows the user to accept or override each. The confirmed play + parameters are pushed to the override queue, and the orchestrator resumes to execute the override instead of the RL selection.

## Feedback Checkpoints

Feedback checkpoints pause the orchestrator and present a modal with three actions: Add Budget (inline USD input), Stop (graceful drain), or Hard Stop. By default only escalation triggers (budget, loops, stagnation) cause checkpoints; cadence-based checkpoints are opt-in via `feedback.cadence_plays` / `feedback.cadence_minutes`.
