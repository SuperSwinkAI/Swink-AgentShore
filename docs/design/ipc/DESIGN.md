# IPC — Functional Design

## Responsibility

IPC lets AgentShore run headless or as an embedded agent process. It streams state to clients and accepts control commands.

## Transport

| Field | Behavior |
|-------|----------|
| Default Unix path | `~/.config/swink/agentshore/sessions/<hash>/socket.sock`, where `<hash>` is the first 16 hex chars of `sha256(project_path)`. |
| Override | `agentshore start --socket <path>` or `agentshore dashboard --socket <path>`. |
| TCP | Supported through `--ipc-host` and `--ipc-port`, mainly for platforms without Unix sockets. |
| Framing | NDJSON, one JSON object per line. |
| Socket permissions | Unix socket is chmod `0600`. |
| Heartbeat | Latest state is rebroadcast every 5 seconds. |
| Replay | New clients immediately receive the latest cached `state_update` if one exists. |

## Outbound Message Types

`state_update`, `play_event`, `feedback_requested`, `session_draining`, `session_ended`, `connection_lost`, plus dashboard replay/auth/read-only helper messages in the bridge.

`action_mask` is a 22-element array aligned with the locked action order. `1` means selectable and `0` means masked.

## Inbound Commands

Inbound commands are flat NDJSON objects covering lifecycle control (`start`, `pause`, `resume`, `shutdown`, `drain`, `hard_stop`), budget adjustment, play overrides, issue rescans, feedback/verification responses, report generation, play abort, session archival, and state queries.
