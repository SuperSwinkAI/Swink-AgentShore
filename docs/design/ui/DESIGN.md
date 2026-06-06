# UI / TUI — Functional Design

## Responsibility

The TUI is the solo-mode interface: a [Textual](https://textual.textualize.io/) application that renders live orchestrator state and accepts a small set of session-control inputs. It is the human-facing surface when AgentShore runs standalone in a terminal. In embedded/headless agent mode the TUI is not used — state streams over IPC and is rendered by the host (see [../ipc](../ipc)). The browser variant of the same stream is the dashboard (see [../dashboard](../dashboard)).

## Design Choices

**Why a TUI.** Solo operators run AgentShore from a terminal alongside their editor; a TUI keeps monitoring in-terminal without a browser, and degrades gracefully over SSH.

**StateProvider decoupling.** The core orchestrator never imports the UI. It emits events through the `StateProvider` protocol (`src/agentshore/state.py`). `TuiStateProvider` (`ui/provider.py`) is the TUI's adapter: each protocol callback turns into a Textual `Message` posted to `OrchestratorApp`. The same protocol backs the IPC/dashboard providers, so all three surfaces consume one event contract. Provider events cover state snapshots, play start/complete, agent status and subprocess spawn/exit, feedback requests, pause/drain/end transitions, and bootstrap-phase progress.

**Display-only widgets.** Widgets render from the latest snapshot and hold no orchestrator references; control actions live on the app/screens and call orchestrator methods. This keeps rendering side-effect-free and the control surface auditable.

## Application Shell

`OrchestratorApp` (`ui/app.py`) owns the screen stack and the orchestrator handle. On mount it shows the startup screen, bootstraps the orchestrator, then swaps to the main dashboard and launches the run loop as a background task. It caches the latest state snapshot, applies eager agent-status hints, and forwards every provider message to the active screen. The title shows `AgentShore [REPLAY]` in audit-replay policy mode.

## Screens

| Screen | Type | Shows |
|--------|------|-------|
| Session Startup | full-screen | ASCII banner, live pre-flight checklist (per bootstrap phase, with elapsed ms), and session id / project / mode once ready |
| Main Dashboard | full-screen | Header, alert bar, and the seven dashboard widgets (below); routes provider messages to widgets |
| Issue Work Queue (`i`) | modal | Issues and PRs grouped into TO DO / IN PROGRESS / IN REVIEW / DONE, plus orphan review PRs; per-item bead status, priority, checks, blocked reasons |
| Epic Closure / Goals (`g`) | modal | Global closure ratio bar, ready/total tasks, and per-epic closure detail from the beads `ProjectGraph` |
| Agent Detail (`d`) | modal | One agent at a time (left/right to page): type, tier, model, status, context-fill bar, cost, tokens, tasks completed/failed |
| Learnings (`l`) | modal | Top session learnings by confidence, with pattern, category, and source play |
| Help (`?`) | modal | Keyboard-shortcut reference |
| Escalation | modal | Feedback-checkpoint actions (see below) |
| Session End | full-screen | Drain reason, live agent list during drain, teardown checklist; on completion offers report (`r`) and quit (`q`) |

## Dashboard Widgets

The Main Dashboard composes seven widgets, each fed by the cached state snapshot.

| Widget | Shows |
|--------|-------|
| Alert Bar | Hidden until raised; surfaces info/warning/error banners and a full-width loop-detected escalation. Shared across alert sources |
| Agent Panel | Dense table of up to ten agents: status symbol, name, state, current play label + target (issue/PR/branch), elapsed, context size, cost. The active play is shown inline per agent rather than in a separate panel |
| Play History | `DataTable` of recent completed plays (id, play, result, alignment Δ, cost, duration, message). Visible row count adapts to terminal width |
| Epic Closure | Per-epic text progress bars and global closure ratio from the beads graph; colour reflects alignment level |
| Budget | Spent / total with a 20-cell fill bar, percent remaining, avg cost per play, and trajectory projection (remaining plays/cost, projected alignment at budget end). When a wall-clock time cap is set, also shows remaining time (`Nh Mm left`). Handles the unlimited case per dimension (dollars and time are independent soft caps) |
| Work Queue | Counts of open/ready/in-progress issues, open/blocked/draft PRs, queued reviews, and the next issue |
| RL / Session State | One compact block: session state, policy mode, play counts, success rate, total cost, failure/same-type streaks, last play, eligible vs. masked action count, drain reason, and loop warnings |

### Responsive Layout

The dashboard switches layout classes by terminal width: standard (≥100 cols), narrow (≥60), minimal (≥40), and an error state below 40 cols that asks the user to widen the terminal. Narrower layouts reduce the number of visible play-history rows.

### Play Labels

`ui/play_labels.py` is the canonical `PlayType → label` map shared across UI surfaces. It provides a full label (`ISSUE_PICKUP → "Issue Pickup"`) and a compact label (`→ "Pickup"`). The reserved/masked action slots (`FUTURE_4`, `FUTURE_7`, `FUTURE_8`) all map to `"Reserved"`.

## Keybindings

Bound at the application level (active on the dashboard):

| Key | Action |
|-----|--------|
| `ctrl+q` | End session — graceful drain (triggers End Session) |
| `ctrl+shift+q` | End session — hard stop |
| `p` | Pause / resume the orchestrator loop |
| `g` | Epic closure / goals modal |
| `d` | Agent detail modal |
| `i` | Issue work-queue modal |
| `l` | Learnings modal |
| `?` | Help overlay |

The Session End screen adds `r` (generate report) and `q` (quit). Modals close on `Esc` (or, where noted, their opening key).

## Feedback Checkpoints

When the orchestrator requests human feedback it pauses and the app raises the Escalation modal, which offers three actions: **Add Budget** (inline USD input; on a positive amount the session resumes), **Stop (graceful)** (drain), and **Hard Stop**. Dismissing without choosing leaves the session paused. By default only escalation triggers (budget, loops, stagnation) raise checkpoints; cadence-based checkpoints are opt-in via `feedback.cadence_plays` / `feedback.cadence_minutes`.
