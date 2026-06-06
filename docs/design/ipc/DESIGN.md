# IPC — Functional Design

## Responsibility

IPC lets AgentShore run headless or as an embedded agent process, decoupling the
core orchestration loop from any state consumer (TUI, dashboard, other agents).
The orchestrator emits state and lifecycle events through the `StateProvider`
protocol (`src/agentshore/state.py`); it never knows or cares who consumes them.
The IPC layer is one concrete provider implementation.

## Why Two Channels

State delivery and control input have opposite failure modes, so they use
separate transports:

- **State out is file-backed, not socket-streamed.** An earlier design pushed
  every `state_update` over the socket to connected clients. A slow consumer
  could stall the engine-side write; the drain-timeout/abort policy that
  guarded against this froze the dashboard ~20 minutes into long sessions. The
  current design writes state to files in the session directory and lets
  consumers pull at their own pace — a slow reader can never back-pressure the
  core loop. See `docs/design/ipc-decouple-coalesce.md` for the decision record.
- **Commands in use the socket.** Control is inherently request/response and
  low-volume, so a connection-oriented channel fits.

## State-Out Transport (file-backed)

`IpcStateProvider` (`src/agentshore/ipc/provider.py`) serializes each snapshot
and event, then hands them to `StateWriter` (`src/agentshore/ipc/state_writer.py`),
which owns two files in the session directory. The dashboard bridge
(`src/agentshore/dashboard/bridge.py`) tails both and fans them out to browser
WebSockets.

| File | Behavior |
|------|----------|
| `dashboard_state.json` | Current full state snapshot. Replaced atomically (tmp-write + `os.replace`) so a reader never sees a half-written file. **Coalesced** — only the latest snapshot is ever retained. |
| `dashboard_events.ndjson` | Append-only event log, one JSON object per line. Head-truncated once it exceeds 5 MiB, keeping the trailing ~1 MiB on a line boundary; full history remains in `agentshore.db`. |

Both writes defer blocking file I/O to a thread and are serialized by an
in-process lock, honoring the no-blocking-in-core-loop rule. Files are reset at
session start so a prior session for the same project cannot replay a stale
`session_ended`.

## Command-In Transport (socket)

`IpcServer` (`src/agentshore/ipc/server.py`) listens for inbound NDJSON command
lines, parses and validates them, and places them on a queue the orchestrator
consumes. It does **not** stream state. Its only outbound traffic is error
replies for malformed commands and the on-demand `get_state` reply.

| Field | Behavior |
|-------|----------|
| Default Unix path | `<sessions-dir>/<hash>/socket.sock`, where `<sessions-dir>` is the platformdirs user-config sessions directory (e.g. `~/Library/Application Support/agentshore/sessions` on macOS) and `<hash>` is the first 16 hex chars of `sha256(project_path)`. |
| Override | `--socket <path>` on `agentshore start` / `agentshore dashboard`. |
| TCP | `--ipc-host` / `--ipc-port`, mainly for platforms without Unix sockets; an ephemeral `port=0` request resolves to the concrete bound port. |
| Framing | NDJSON, one JSON object per line. |
| Socket permissions | Unix socket is chmod `0600`. |
| `get_state` | The provider mirrors each latest `state_update` envelope into the server's in-memory cache, so a `get_state` command is answered immediately rather than waiting on the file tail. |

## Serialization Contract

The serializer (`src/agentshore/ipc/serializer.py`) is a pure data→dict layer
with no I/O or sockets: it converts domain objects (`OrchestratorState`,
`PlayOutcome`, …) to plain dicts, lowering every enum to its string `.value` and
preserving `None`. `make_message` wraps a payload in the envelope below.

Framing (`src/agentshore/ipc/wire.py`) is shared by both the IPC path and the
sidecar JSON-RPC path: serialize to a single line, append exactly one `\n`, and
encode with `allow_nan=False` after nulling out non-finite floats — so every
framed line is strict-parseable JSON (notably for the browser `JSON.parse`).

Envelope fields: `type`, `id` (uuid4), `timestamp` (ISO-8601 UTC), `seq` (a
process-monotonic counter incremented once per outbound message), and `payload`.

## Outbound Message Catalog

Emitted via the provider; all are written to the events file except
`state_update`, which replaces the state file.

| Type | When |
|------|------|
| `state_update` | Full `OrchestratorState` snapshot after each play cycle (coalesced). |
| `play_event` | Play started / completed / failed (`status` distinguishes them). |
| `agent_changed` | Agent status transition. |
| `agent.subprocess_spawned` / `agent.subprocess_exited` | CLI-agent subprocess lifecycle (with pid / exit code). |
| `feedback_requested` | Escalation needing human feedback; `reason` is mapped to a `trigger` class (budget_exhaustion / loop_escalation / stagnation / ambiguous_intake). |
| `session_paused` / `session_draining` / `session_ended` | Session lifecycle (pause, graceful drain begun, clean completion). |
| `bootstrap_phase` | Startup phase progress for the dashboard loading modal. |

The `state_update` payload carries the locked-order `action_mask` — a
22-element array (`1` = selectable, `0` = masked) — alongside `mask_reasons` and
the full agent/issue/PR/graph/budget/trajectory/stats snapshot.

## Inbound Command Catalog

Commands are flat NDJSON objects (`src/agentshore/ipc/commands.py`); each must
carry a `command` key drawn from the validated set:

- **Lifecycle**: `start`, `pause`, `resume`, `shutdown`, `drain`, `hard_stop`
- **Control**: `adjust_budget` (requires positive `delta_usd`), `add_budget`
  (additive live budget; ≥1 of positive `delta_usd` / `delta_minutes` — backs
  `agentshore add-budget`; the client follows with `get_state` to read the
  applied caps), `abort_play`, `rescan_issues`, `generate_report`
- **Responses**: `feedback_response` (requires `action`),
  `verification_response` (requires `checkpoint_id`, `passed`)
- **Session/state**: `archive_session`, `list_archives`, `get_state`

Unknown commands and missing/ill-typed required params are rejected with an
error reply; valid commands are enqueued for the orchestrator.
