# IPC decouple + coalesce — decision record

This note records *why* engine→consumer state delivery is decoupled and
coalesced. For the current transport mechanics (file paths, rotation, command
catalog), see `docs/design/ipc/DESIGN.md` — this note covers only the rationale
that doc references.

## Problem

State delivery had been pushed engine-side: every `state_update` was streamed
over the IPC socket to each connected client. A slow WebSocket consumer could
stall the engine-side write. The drain-timeout/abort policy meant to guard
against that instead froze the dashboard well into long sessions — the bridge's
read loop sat head-of-line-blocked just under the per-send timeout, so fresh
snapshots were dropped while the consumer still looked healthy. The incident
surfaced as the dashboard running far behind reality with a storm of
drop warnings.

The root issue: a consumer's read pace could exert backpressure on the asyncio
core loop, which must never block on anyone watching it.

## Decision

Decouple the core loop from every state consumer through the `StateProvider`
protocol (`src/agentshore/state.py`); the orchestrator emits snapshots and
events and never knows who reads them. The IPC provider is one concrete
implementation, and it persists state to files rather than streaming it.

State out is **file-backed and coalesced**, not socket-streamed. The provider
writes the latest full snapshot to a single state file (atomically replaced, so
a reader never sees a half-written file) and appends ordered lifecycle events to
an NDJSON log. The dashboard bridge tails both at its own pace. The IPC socket
is reduced to **inbound commands only** — a low-volume, request/response channel
where a connection-oriented transport fits.

### Principles

1. **A consumer can never back-pressure the core loop.** Readers pull from
   files at their own rate; a slow or absent reader costs the engine nothing.
   Blocking file I/O is deferred to a thread, honoring the no-blocking-in-core
   rule.
2. **State snapshots coalesce; events do not.** The snapshot is a full
   replacement — only the latest matters, so an outpaced reader simply skips
   stale intermediates. Lifecycle events are ordered semantics and are preserved
   in append order, with bounded growth via head-truncation (history remains
   recoverable from `agentshore.db`).
3. **On-demand reads stay fast.** Each latest snapshot is mirrored into the IPC
   server's in-memory cache so a `get_state` command answers immediately instead
   of waiting on the file tail.

## Status

Shipped. The earlier engine-side streaming path (per-client writer streams with
a drain-timeout abort) is fully removed in favor of the file-backed model above.
The wire envelope (NDJSON) and dashboard JS are unchanged.
