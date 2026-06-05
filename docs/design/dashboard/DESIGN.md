# Dashboard — Pixel-Art Browser Visualization

## Purpose

A browser-based dashboard that visualizes a AgentShore session: agent instances as animated pixel-art characters in a virtual office, plus kanban and stats views over the same live state. Agents walk to workstations corresponding to their current play, animate while working, and return to idle positions when finished. The dashboard is a browser bridge over AgentShore's IPC/state stream and renders in real time.

Inspired by [pixel-agents](https://github.com/pablodelucca/pixel-agents), but purpose-built for AgentShore's fixed 22-action space and agent model — no layout editor, no VS Code dependency, no transcript scraping.

## Why a bridge process

Browsers cannot read AgentShore's session files or its Unix-domain command socket directly. The bridge is a lightweight relay that exposes session state over WebSocket and forwards browser commands back to the orchestrator.

A key design decision is that the **outbound state path is file-backed, not a live socket relay**. The orchestrator writes a coalesced `dashboard_state.json` snapshot and appends every lifecycle event to `dashboard_events.ndjson` in the session directory (via `agentshore.ipc.state_writer.StateWriter`). The bridge tails both files and fans their contents out to WebSocket clients. This deliberately eliminates the engine-side stall guard (a drain-timeout `transport.abort()`) that used to freeze the dashboard roughly 20 minutes into every long session. Only the **inbound command path** still uses the legacy IPC command socket.

## Architecture

Three layers:

1. **AgentShore Core** (orchestrator + RL + plays) writes `dashboard_state.json` and `dashboard_events.ndjson` to the session directory and accepts commands on its IPC command socket.
2. **WebSocket Bridge** (`src/agentshore/dashboard/bridge.py`, `DashboardBridge`, Python/starlette/uvicorn) tails the two state files, broadcasts changes to browser clients over WebSocket, serves the static dashboard assets over HTTP, and forwards validated commands to the IPC command socket.
3. **Browser Client** (`dashboard/src/`, React + Canvas 2D + TypeScript) renders the office floorplan, kanban, and stats views.

The bridge runs either standalone (the `agentshore dashboard` path) or embedded inside the desktop sidecar process (`src/agentshore/sidecar/embedded_bridge.py`, `EmbeddedBridge`), where the orchestrator, IPC server, and bridge share one asyncio loop as cooperative tasks. The embedded form auto-selects a loopback port and advertises the resulting endpoint back to the desktop WebView via the `session.start` RPC response.

## Component 1: WebSocket Bridge

### Transport

- Tails `dashboard_state.json` (mtime poll) and `dashboard_events.ndjson` (byte-offset tail) from the session directory.
- Listens for HTTP on `localhost:9400` by default (configurable; the embedded bridge auto-selects a free loopback port).
- Upgrades a WebSocket route per browser tab and serves the static dashboard assets from `src/agentshore/dashboard/static/`.
- Primes its caches from any state already on disk so the first tab renders immediately, before the engine emits a new event.

### Message relay

**Outbound (AgentShore → browser):** new state snapshots and tailed event lines are broadcast to every connected WebSocket client. The client (`dashboard/src/ws.ts`) normalizes both the documented `{type, id, timestamp, payload}` envelope and bridge-generated flat/synthetic messages into the flat shape the app expects, and ignores unknown message types.

**Inbound (browser → AgentShore):** JSON commands from the WebSocket are validated against the IPC command schema (`ipc/commands.py`) and written to the IPC command socket as NDJSON. This carries `pause`, `resume`, `override_play`, `feedback_response`, budget adjustments, drain/stop, etc.

### Auth and control model

A session can have multiple tabs open but only one controller. On connect, the bridge issues a per-bridge `auth_token` and a `read_only` flag to each client. The first connected tab is granted the token and may send commands; later tabs are read-only until promoted (e.g. when the controlling tab disconnects). The client captures the token during the handshake, attaches it to every outbound command, and clears it on disconnect so a reconnect starts a fresh handshake rather than leaking a stale token.

### Reconnection

If the WebSocket drops, the client logs the close (including the silent `1006` Tauri-policy case), reverts to read-only, and retries with exponential backoff (1s, 2s, 4s, 8s, 16s, 30s, capped). The bridge emits `connection_lost` / `connection_restored` to surface transport state in the UI.

### Session discovery

AgentShore has a 1:1 relationship with a project directory. Session state (state file, events file, command socket) lives in the per-project session directory keyed by a stable hash of the project's absolute path, so multiple projects can run simultaneously without collision.

## Component 2: Browser Views

The client offers three views, switched via the Stage Tabs surface (`Office` / `Kanban` / `Stats`):

- **Office** — the pixel-art floorplan. Agents are animated characters that path to the workstation for their current play (see Zone-to-Play Mapping), animate while working, and return to idle when done.
- **Kanban** — issue/PR cards derived from the beads graph mirror, showing bead status, mirror linkage, PR review/check state, and the reviewer agent. Surfaces the GitHub conversation layer against the canonical beads layer.
- **Stats** — session metrics (plays, success rate, cost, tokens, average duration, failure streak) plus per-epic alignment and closure ratios.

## Zone-to-Play Mapping

Each play maps to an office zone (`dashboard/src/office/zones.ts`, `PLAY_TO_ZONE`). When a play starts, the assigned agent walks to that zone's workstation. Healthy idle agents return to the Zen Garden; `take_break` is a cooldown/recovery play and routes to Recovery Bay.

| Zone | Plays routed here |
|------|-------------------|
| **War Room** | `refine_task_breakdown`, `seed_project`, `groom_backlog`, `calibrate_alignment` |
| **Workshop** | `issue_pickup`, `unblock_pr`, `systematic_debugging`, `cleanup` |
| **Science Lab** | `run_qa` |
| **Launch Control** | `merge_pr` |
| **Editor's Desk** | `code_review`, `write_implementation_plan`, `design_audit` |
| **Recovery Bay** | `reconcile_state`, `take_break`, `prune`; plus failed agent-associated play events and `agent_changed`/snapshot `error` |
| **Zen Garden** | Idle agents |
| **Front Desk** | Spawn/arrival target for `instantiate_agent`; exit target for `end_agent`, `end_session` |

### Agent lifecycle routing

`instantiate_agent` spawns at the Front Desk and then walks to the Zen Garden. `end_agent` and `end_session` walk to the Front Desk exit and fade out (the state manager routes these specially rather than as ordinary workstation visits).

### Reserved slots

The action space pins three permanently masked, reserved slots — `future_4` (slot 14, "Reserved 4"), `future_7` ("Reserved 7"), and `future_8` ("Reserved 8"). They have **no zone routing**: the office treats them as current-location no-ops, and the Plays Panel italicizes them as always-masked. (The former `browser_verification` play no longer exists.)

## HUD Decisions

- **Bottom Plays Tray**: a lifecycle-ordered action surface (`PLAY_TRAY_KEYS`) showing startup/setup on the left, main delivery in the middle, and session-control on the right. The tray flows by column, so adjacent pairs are the top/bottom cells of one visual column. Only currently available or running plays are actionable; masked plays render disabled/italicized.
- **Epic Closure Bar**: one mini progress bar per epic from `state.graph.epics`, colored by `closure_ratio`, plus the `global_closure_ratio`. When `state.graph` is `null`, shows "graph not initialised".

## Demo & Mock Modes

- **Demo transport** (`dashboard/src/demoTransport.ts`): a client-side transport activated via `?demo=1&scenario=<name>`, allowing visual testing without a running AgentShore session. Scenarios: `active`, `empty`, `feedback`, `disconnected`, `stress`, `bootstrap` (defaults to `active` for unknown values). It implements the same `DashboardTransport` interface as the real WebSocket client and synthesizes state updates, play events, and command acknowledgements.
- **Mock WebSocket server** (`dashboard/tests/e2e/mockAgentShoreServer.mjs`): a standalone WebSocket server serving canned data, used for E2E tests against the real transport path.
