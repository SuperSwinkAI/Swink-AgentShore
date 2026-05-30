# Dashboard — Pixel-Art Browser Visualization

## Purpose

A browser-based pixel-art dashboard that visualizes AgentShore's agent instances as animated characters in a virtual office. Agents walk to workstations corresponding to their current play, animate while working, and return to idle positions when finished. The dashboard connects to AgentShore's existing IPC endpoint and renders state in real time.

Inspired by [pixel-agents](https://github.com/pablodelucca/pixel-agents), but purpose-built for AgentShore's fixed 22-action space and agent model — no layout editor, no VS Code dependency, no transcript scraping.

## Architecture

```
┌──────────────────────────────────┐
│         AgentShore Core             │
│  (orchestrator + RL + plays)     │
└──────────┬───────────────────────┘
           │ Unix domain socket (NDJSON)
           │ state_update, play_event,
           │ agent_changed, feedback_requested
           ▼
┌──────────────────────────────────┐
│      WebSocket Bridge            │
│  (Python, starlette + uvicorn)   │
│  Reads IPC endpoint, relays to  │
│  browser clients over WS.       │
│  Forwards commands back to IPC.  │
└──────────┬───────────────────────┘
           │ WebSocket (JSON)
           │ same message envelope
           ▼
┌──────────────────────────────────┐
│      Browser Client              │
│  Canvas 2D + vanilla TypeScript  │
│  Pixel-art office, animated      │
│  agent characters, HUD overlays  │
└──────────────────────────────────┘
```

### Why a bridge process?

Browsers cannot connect to Unix domain sockets directly, and AgentShore's IPC stream is NDJSON rather than WebSocket. The bridge is a lightweight relay — it opens the IPC endpoint as a client, fans out every NDJSON line to connected WebSocket clients, and forwards inbound commands back.

## Component 1: WebSocket Bridge

### Transport

- Connects to the AgentShore IPC endpoint from `--socket` or `--ipc-host` / `--ipc-port` (same endpoint used by agent-mode hosts).
- Listens for HTTP on `localhost:9400` (configurable via `--port`).
- Upgrades `/ws` to a WebSocket connection per browser tab.
- Serves static dashboard assets from `/` (`src/agentshore/dashboard/static/`).

### Message relay

**Outbound (AgentShore → browser):** Each NDJSON line from the IPC endpoint is parsed for bridge-side caching, then the original line is sent to every connected WebSocket client. Browser code normalizes both the documented `{"type", "id", "timestamp", "payload"}` envelope and bridge-generated flat helper messages.

**Inbound (browser → AgentShore):** JSON messages from the WebSocket are validated against the IPC command schema (`ipc/commands.py:VALID_COMMANDS`), then written to the IPC endpoint as NDJSON. This enables the dashboard to send `pause`, `resume`, `override_play`, `feedback_response`, etc.

### Reconnection

If the IPC endpoint disconnects, the bridge sends a `{"type": "connection_lost"}` message to all browser clients and retries the IPC connection with exponential backoff (1s, 2s, 4s, max 30s). On reconnect, it sends `{"type": "connection_restored"}`.

### IPC auto-discovery

AgentShore has a 1:1 relationship with a project directory. Session state lives at `~/.config/swink/agentshore/sessions/<project-hash>/` where `<project-hash>` is a stable hash of the project's absolute path. `agentshore start` writes the socket here; `agentshore dashboard` reads it. Multiple projects can run simultaneously without collision.

## Zone-to-Play Mapping

Each play maps to a zone. When a play starts, the assigned agent walks to that zone's workstation. Healthy idle agents return to the Zen Garden; `take_break` is a cooldown/recovery play and routes to Recovery Bay.

| Zone | Plays routed here |
|------|-------------------|
| **War Room** | `refine_task_breakdown`, `seed_project`, `groom_backlog`, `calibrate_alignment` |
| **Workshop** | `issue_pickup`, `unblock_pr`, `systematic_debugging`, `cleanup` |
| **Science Lab** | `run_qa`, `browser_verification` |
| **Launch Control** | `merge_pr` |
| **Editor's Desk** | `code_review`, `write_implementation_plan`, `design_audit` |
| **Recovery Bay** | Failed agent-associated play events, `agent_changed`/snapshot `error`, `reconcile_state`, `take_break` |
| **Zen Garden** | Idle agents |
| **Front Desk** | Spawn/wait target for `instantiate_agent`; exit target for `end_agent`, `end_session` |
| **Agent Lifecycle** | `instantiate_agent` spawns at the Front Desk then walks to Zen Garden. `end_agent` and `end_session` walk to the Front Desk exit and fade out. `future_6`, `future_7`, and `future_8` are reserved, masked action slots with no dashboard routing. |

## HUD Decisions

- **Bottom Plays Tray**: Lifecycle-ordered action surface showing startup/setup on the left, main delivery in the middle, and session-control on the right. Only currently available or running plays are shown; masked plays are hidden.
- **Epic Closure Bar**: One mini progress bar per epic from `state.graph.epics` colored by `closure_ratio`, plus the `global_closure_ratio`. When `state.graph` is `null`, shows "graph not initialised".

## Demo Transport

The dashboard includes a client-side demo transport (`dashboard/src/demoTransport.ts`) activated via `?demo=1&scenario=<name>` query params, allowing visual testing without a running AgentShore session.
