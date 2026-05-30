# IPC decouple + coalesce — decision record

## Problem

During the 2026-05-15 session, the dashboard froze ~70 minutes behind reality.
The IPC server logged 588+ `broadcast_dropped` warnings. A slow WebSocket
consumer (draining in ~1.5 s/iteration) kept the bridge's read loop
head-of-line-blocked just under the per-send timeout, so every new
`state_update` was dropped while the consumer appeared nominally healthy.

## Decision

Decouple IPC read and write paths with per-client streams. Applied symmetrically
in both `ipc/server.py` and `dashboard/bridge.py`.

### Principles

1. **Read path never waits on write path.** The producer enqueues synchronously
   and returns immediately; a dedicated writer coroutine per client drains async.
2. **State snapshots coalesce; events do not.** `state_update` is a full
   replacement (only latest matters). `play_event`, `feedback_request`,
   `session_*`, and `error` are ordered semantic events preserved in a bounded
   deque (maxlen 128).
3. **Slow consumers are evicted, not buffered.** Writer catches timeout /
   connection errors, sets `closed = True`, and exits. The broadcast side removes
   the stream.
4. **Telemetry replaces per-drop log storms.** Periodic (60 s) aggregate
   `coalesced_states` + `dropped_events` summary per module replaces the
   per-event `broadcast_dropped` warning.

## Status

Shipped in v0.12.21. Wire protocol (NDJSON) and dashboard JS unchanged.
