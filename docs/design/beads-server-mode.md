# Beads Server Mode — Evaluation

Status: **Evaluation — recommend staying embedded, not implemented** · Branch: `beads_improvements` ·
Related: bd pin bump 1.0.4 → 1.1.0, `src/agentshore/beads/__init__.py`, `src/agentshore/beads/setup.py`.

## Question

AgentShore runs `bd init` with no flags — embedded mode. The real topology is
multi-writer: the orchestrator process issues its own `bd` calls, and every
dispatched CLI agent (Claude Code, Codex, Grok, Antigravity) *also* shells out
to `bd create` / `bd update` / `bd link` / `bd close` / `bd set-external-ref`
directly from its skill-template instructions (confirmed in
`agentshore-groom-backlog`, `agentshore-prune`, `agentshore-seed-project`,
`agentshore-calibrate-alignment`, `agentshore-design-audit`). Given that, should
AgentShore move to `bd init --server` / shared-server mode
(`BEADS_DOLT_SHARED_SERVER=1`)?

## Current concurrency reality

Orchestrator-issued `bd` calls are serialized through one process-local
`asyncio.Lock` (`_BD_LOCK`, `src/agentshore/beads/__init__.py:107-108`,
acquired in `bd()` at line 305) — "C5" in the code comments. That lock only
covers calls the orchestrator itself makes. It does **not** cover the N agent
CLI subprocesses: a skill template's `bd create --external-ref gh-9` runs in
the agent's own process, with no coordination against the orchestrator or
against a sibling agent running the same skill concurrently.

Embedded mode is single-writer, enforced via file lock
([DOLT.md](https://github.com/gastownhall/beads/blob/main/docs/DOLT.md)
lines 60-68: "Single-writer (one process at a time)"). Under real contention
this surfaces as `"database is locked"` errors
(DOLT.md line 352, `TROUBLESHOOTING.md` "Lock Contention (Embedded Mode)").
AgentShore already absorbs this for **reads**: `_read_graph_raw`
(`src/agentshore/beads/__init__.py:584-640`) retries a transient `BdError` up
to 3 times with a 0.5s delay, explicitly because "lock contention, a parse
blip" are worth retrying (line 594) — but a `BdTimeoutError` is never retried,
since the command already paid its full budget (#237). Point-mutation calls
made directly by orchestrator plays have no such retry: e.g.
`reconcile_merged_pr`'s per-task `bd update ... --status closed`
(`src/agentshore/plays/skill_backed/_merge_reconcile.py:122-146`) just logs a
warning and moves to the next task on failure. Agent-issued `bd` calls from
skill templates have no retry logic at all — whatever the CLI agent's own
tool-error handling does is what happens.

So today: lock contention on reads self-heals; a lost write (orchestrator
point-mutation or agent skill-template call) fails loud and is not retried.
Nothing has silently diverged — the failure is visible.

## What server mode changes

`bd init --server` / `BEADS_DOLT_SHARED_SERVER=1` moves the backend to a
`dolt sql-server` process, giving true concurrent writers with Dolt's
cell-level merge (`TROUBLESHOOTING.md`: "Dolt handles merge conflicts natively
with cell-level merge"). The docs frame this as the intended fix for exactly
AgentShore's shape: "Switch to server mode when you need: Multiple agents
writing simultaneously" (DOLT.md lines 99-102). On paper this is the right
tool for a multi-writer topology.

## Operational costs

1. **No lifecycle owner.** `bd dolt start/stop/status/logs` exist as bd
   subcommands, but the reference user of that lifecycle in the docs is Gas
   Town's own orchestrator (`gt dolt start`, DOLT.md lines 475-485) — a whole
   subsystem for starting-before-first-use, health-checking, and
   restarting-on-crash. AgentShore has no equivalent today and would need to
   build one, including for the desktop app, which ships `bd` as a
   sidecar binary with no service manager.
2. **`bd dolt status` is not a real health check.** It only reads the PID
   file: `"A 'running' status does not guarantee the server is reachable on
   the expected port"` (`TROUBLESHOOTING.md` line 551). A precondition check
   built on it can pass while the server is actually unreachable.
3. **Circuit-breaker stuck-open failure mode.** State lives in
   `/tmp/beads-dolt-circuit-<host>-<port>.json`, shared across every `bd`
   process for that host:port. Once tripped, `dolt circuit breaker is open:
   server appears down, failing fast (cooldown 30s)` is returned to *every*
   `bd` invocation until a successful probe resets it (`TROUBLESHOOTING.md`
   lines 545-572). Fix requires deleting the file and restarting the server:
   `rm /tmp/beads-dolt-circuit-*.json; bd dolt stop; bd dolt start; bd list`.
   On macOS, `/tmp` symlinks to `/private/tmp`, "which is not always cleared
   on restart" (line 575) — a stuck breaker can outlive a reboot. In an
   AgentShore session this would present as every play touching beads failing
   identically for the rest of the run, with no automatic recovery — exactly
   the class of silent-stall risk the project has hit before with beads
   (compare the unrecorded dependency-cycle stall that ended theta_rl via the
   20-minute idle backstop rather than real completion).
4. **Auto-commit semantics flip.** Server mode defaults `dolt.auto-commit` to
   OFF — "the server manages its own transaction lifecycle... firing
   `DOLT_COMMIT` after every write under concurrent load causes 'database is
   read only' errors" (per bd 1.1.0 behavior notes). AgentShore's own bd calls
   currently pass `--dolt-auto-commit=on` explicitly on every write
   (`_merge_reconcile.py:126-129`, and the calibrate-alignment skill assumes
   local auto-commit throughout). This is not a drop-in swap; every existing
   `--dolt-auto-commit=on` call site would need re-verification under server
   mode.

## The sandbox question — detection mechanism confirmed, consequence unconfirmed

`bd`'s sandbox auto-detection (`cmd/bd/sandbox_unix.go`, source-verified) is a
syscall probe, not an environment-variable check:

```go
// Try to send signal 0 (existence check) to our own process.
err := syscall.Kill(os.Getpid(), 0)
if err == syscall.EPERM {
    return true // sandboxed
}
```

If a process can't even signal itself, bd concludes it's sandboxed and, unless
the invocation explicitly passed `--sandbox` itself
(`cmd/bd/main.go:959-964`: the auto-detect branch is skipped only when
`cmd.Root().PersistentFlags().Changed("sandbox")` is true), sets its internal
`sandboxMode` flag and prints `"Sandbox detected, using direct mode"`. This
was added in v0.21.1+ tracking upstream
[#353](https://github.com/gastownhall/beads/issues/353). Windows has a
separate detection path (`cmd/bd/sandbox_windows.go`, not fetched here).
Claude Code and Codex CLI agents run every skill-templated `bd` call inside
their own OS-level sandbox — precisely the profile this probe is built to
catch — so `sandboxMode` is `true` for essentially every agent-issued `bd`
call in AgentShore's fleet.

**Where this doc's confidence is lower than the rest of it:** the published
docs (`TROUBLESHOOTING.md` lines 910-929) state that sandbox mode "uses
embedded database mode (no server needed)" and disables auto-export/import —
i.e. that it silently downgrades the storage backend. I went looking for the
code path that does that downgrade and could not find one. A repo-wide search
for every consumer of the `sandboxMode` variable and the `isSandboxMode()`
accessor (`cmd/bd/context.go:253-259`) turns up exactly two production call
sites: the flag registration itself (`main.go:600`, help text: `"Sandbox
mode: disables Dolt auto-push"`) and a single guard in
`cmd/bd/dolt_autopush.go:106-108` that skips `maybeAutoPush` when
`isSandboxMode()` is true. I also checked the files that plausibly own
embedded-vs-server backend selection — `cmd/bd/store_factory.go`,
`internal/doltserver/servermode.go`, `cmd/bd/init.go` — and none reference
sandbox state at all. So at the current `main` HEAD, the only *confirmed*
effect of auto-detected sandbox mode is disabling Dolt auto-push, not backend
selection; whether the "forces embedded mode" claim in the docs is stale,
describes a different release, or is implemented somewhere my searches
missed is genuinely unresolved. I'm flagging this rather than asserting
either version confidently — the docs and the source disagree, and a false
"discovery" here would be worse than an honest gap.

**Why this still matters for the recommendation either way:** if the docs are
right and I simply didn't find the code, the consequence is as bad as
originally described — a silent per-agent store split, invisible because the
whole point of the fallback is to avoid erroring. If the docs are stale and
the source is right, the real exposure is narrower but still real: every
sandboxed agent call would keep writing to the shared server (no split), but
with Dolt auto-push silently skipped for that call — a durability gap, not a
correctness one, and one that's arguably fine since AgentShore doesn't rely
on agent-issued `bd` calls to push to a remote anyway. Either way, this must
be settled empirically (checklist item 1) before server mode is trustworthy
for AgentShore's mixed sandboxed/non-sandboxed writer population — it isn't
safe to assume the more benign reading.

## Config reference (confirmed from docs, for a future attempt)

Environment variables (`DOLT.md` lines 385-392, matches `CONFIG.md` line 65):
`BEADS_DOLT_SERVER_MODE=1` (enable), `BEADS_DOLT_SERVER_HOST` (default
`127.0.0.1`), `BEADS_DOLT_SERVER_PORT` (default `3307`, or `3308` in shared
mode), `BEADS_DOLT_SERVER_TLS`, `BEADS_DOLT_SERVER_USER`,
`BEADS_DOLT_SHARED_SERVER=1`, `BEADS_DOLT_PASSWORD` (highest-priority
credential source). CLI flags mirror these 1:1 plus `--server-socket
<path>` (Unix domain socket, overrides host/port — no corresponding env var
found in the docs). None of these change the sandbox-detection findings
above; the probe is process-signal-based, not connection-method-based.

## Recommendation

**Stay embedded.** The prior holds up under evidence more strongly than
expected going in, on grounds independent of the unresolved sandbox question.
Today's measured failure class — lock contention under concurrent writers —
is real but already absorbed for reads via retry, and fails loud rather than
silently diverging for writes. Server mode would trade that for a circuit
breaker that can survive a reboot and blocks every `bd` call machine-wide
until manually cleared, plus no lifecycle owner for the `dolt sql-server`
process today (start-before-use, health-check, crash-restart). Those two
points alone justify staying embedded regardless of how the sandbox question
resolves; if it resolves toward the docs' "silent store split" reading, that
would be a third, independently-sufficient reason.

## Migration/verification checklist (if revisited)

1. **Resolve the sandbox question empirically.** From inside an actual
   Claude-Code- or Codex-sandboxed process, configure a project for server
   mode and run a write (`bd create`), then inspect `.beads/` to see whether
   the write landed in `.beads/dolt/` (server) or `.beads/embeddeddolt/`
   (embedded fallback), and separately check whether auto-push ran. This is
   the single experiment that resolves the open question above and would
   materially change this doc's confidence, not necessarily its conclusion.
2. Confirm the `dolt.auto-commit=off` (server default) /
   `--dolt-auto-commit=on` (AgentShore's current explicit flag) interaction
   doesn't hit `"database is read only"` at AgentShore's actual write call
   sites under concurrent load.
3. Build a real lifecycle owner: start-before-first-use, a health probe
   deeper than the PID-only `bd dolt status` (e.g. an actual `bd list` or SQL
   probe), crash-detect + restart, and a circuit-breaker-file sweep
   (`rm -f /tmp/beads-dolt-circuit-*.json`) before starting the server each
   session, since a stale file from a prior crash can wedge a fresh start.
4. Load-test with a fleet-sized number of concurrent agent `bd` writes
   (create/update/link/close, not just single-field edits) against one
   server-mode store and confirm Dolt's cell-level merge behaves for
   AgentShore's actual write shapes.
5. Re-run this recommendation only after 1-4 are answered.
