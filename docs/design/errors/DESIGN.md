# Error Handling — Functional Design

## Responsibility

Define how AgentShore detects, classifies, recovers from, and escalates errors. A single agent or play failure must never crash the orchestrator: the core loop is wrapped so one throwing tick is contained, and exceptions carry enough metadata to decide whether to retry, route around, escalate, or shut down.

Cross-references: [HLD](../HLD.md) · circuit-breaker / loop-detection live in the [core](../core/DESIGN.md) loop.

## Design Choices

- **Recoverability is a property of the error, not the call site.** Every orchestrator exception declares `recoverable: bool` and a human-readable `recovery_action`. This keeps the recovery intent next to the failure definition rather than scattered across handlers. Most non-recoverable cases are operator-facing config/safety failures (bad config, anti-confirmation violation, revert failure, no valid actions) where automatic continuation would be unsafe.
- **Failure signal is structured first, inferred second.** A play that knows *why* it failed sets a typed `FailureKind` at the failure site; the persisted `FailureCategory` string is derived from it. A substring inferer over the error text is only the fallback for legacy or uncaught-exception paths that never set a kind. The structured path is preferred because the inferer is brittle and exists only to keep older paths classified.
- **Classification exists to feed RL and recovery, not for display.** The `FailureCategory` persisted on each play row is consumed by reward filtering, dashboard styling, and end-of-session rollups, so the policy can learn to route around recurring failure shapes (e.g. an agent that keeps timing out).

## Exception Hierarchy

All orchestrator exceptions derive from `OrchestratorError`, which carries `error_type`, `recoverable`, and `recovery_action`. Subclasses group by origin:

- **Config** — non-recoverable; require fixing config and restarting.
- **Agent** — process crash, timeout (`PlayTimeoutError` carries a timeout sub-class via `ErrorClass`), invalid output, API error, rate limit (recoverable, back off), auth error (non-recoverable, halt and surface).
- **Play** — precondition failure (skip and reselect), execution failure (RL absorbs), anti-confirmation violation (non-recoverable hard block), instantiation denied, fresh-start failure, revert failure (non-recoverable), learning-extraction failure, intake parse error (non-recoverable, escalate), issue-inflation detected (recoverable, RL penalty).
- **RL** — policy NaN (rollback to checkpoint), no valid actions (non-recoverable, force end), reward-computation failure (use zero reward).
- **System** — database error (non-recoverable, pause), socket error (recoverable, continue headless).

Worktree allocation defines its own `OrchestratorError` subclasses (create failure, stale worktree) co-located with the allocator/store code that raises them.

## Failure Classification

Every completed play that did not fully succeed is assigned a `FailureCategory` from a five-value taxonomy:

| Category | Meaning |
|----------|---------|
| `code_error` | Bug in generated code (default fallback bucket). |
| `test_failure` | Tests fail, lint fails, or coverage regresses. |
| `alignment_drift` | Work diverges from beads targets / scope. |
| `agent_error` | Infrastructure or agent-level failure (auth, timeout, crash, malformed output). |
| `gate_rejection` | A gate blocked the play (needs different reviewer, pending checks, ambiguous, blocked dependency, merge conflicts). |

The category is resolved at play completion: if the play set a `FailureKind` (`AUTH`, `TEST`, `GATE`, `SCOPE`, `AGENT_ERROR`, `CODE_ERROR`), it maps deterministically to its category (`AUTH`→`agent_error`, `SCOPE`→`alignment_drift`, etc.). Otherwise the substring inferer scans the error text against marker sets in priority order (auth → test → scope/approval → transient/agent → gate phrases), defaulting to `code_error`.

Agent-level failures are sub-typed separately by `ErrorClass` (rate limit, auth, timeout and its variants — wall-clock / post-response / stream-idle / transient — invalid model, codex rollout, transient network, OOM/signal crash, invalid output, unknown). `ErrorClass` is a string enum so it threads through `last_error_class` and `frozenset[str]` membership tests unchanged; it feeds health monitoring and circuit-breaker decisions.

### Backend-auth early abort and type suppression

A CLI agent's **backend** session (the model-provider auth its harness uses, distinct from its GitHub identity token) can expire mid-run. When the Codex CLI's cached token dies it prints `failed to renew cache TTL` / `failed to refresh available models` to stderr and then hangs reading from stdin, so the dispatch would otherwise run to the full stream-idle timeout (~1800–3600s) and be mislabelled `timeout`. A live stderr sniffer matches those signatures on a bounded tail and aborts the dispatch in well under a second, classified `ErrorClass.AUTH` (a config-class error, so `attempt_recovery()` correctly declines to re-probe the agent). The same marker set classifies a pre-launch probe, so an expired backend reads as `AUTH` in both places. Beyond per-agent recovery, an `AUTH` classification is escalated to a **session-wide agent-type suppression**: the type is added to a grow-only suppression set and the play-candidate analyzer masks every further dispatch to it (including spawning a fresh agent of the type) for the rest of the session, since a new backend token requires a new session. This stops one expired token from burning every subsequent dispatch to the timeout.

Grok launch wedges use the same type-suppression path without changing their
error class to auth. If a Grok subprocess produces no first stdout byte before
the first-byte watchdog fires, the dispatch remains `timeout_stream_idle`, but
the Grok type is suppressed (a bounded cooldown) so it auto-recovers later. The
Grok first-byte deadline is **240s**, not the 120s global default: the Grok CLI
(0.2.32) was measured at 30–70s to first byte for `grok-build` — model/relay
latency, not local startup — so the original 45s cap killed ~100% of Grok
dispatches as false launch wedges. 240s clears the measured distribution with
margin while still bounding a genuine hang; Grok is also dispatched with
`--no-memory --no-plan` to trim that latency on its ephemeral single-turn runs.

## Recovery Strategy

### Tiered Escalation

- **Level 0 — Automatic retry.** Transient agent API errors back off and retry before counting as a failed play.
- **Level 1 — RL absorbs the failure.** Play failures, agent crashes, timeouts, and issue-inflation record a negative reward; the policy learns to route around the failure.
- **Level 2 — Pause and escalate to human.** Triggers: budget exhaustion, loop-detection escalation, `IntakeParseError`, all agents in error state, or explicit human request. Cadence-based check-ins are opt-in (`feedback.cadence_plays` / `cadence_minutes`). An unanswered pause auto-stops via a clean drain after `feedback.unanswered_timeout_seconds`, and an independent watchdog force-drains a hard-frozen loop.
- **Level 3 — Shutdown.** Database corruption or unrecoverable system error: persist what can be persisted, drain, and exit.

### Per-Agent Circuit Breaker

A `CLOSED → OPEN → HALF_OPEN` breaker per agent. Defaults: 3 failures within a 300s rolling window trips OPEN; after a 60s cooldown it moves to HALF_OPEN and admits a single probe; success closes it, failure re-opens. Recovery probes back off exponentially (cooldown × 2^attempts, capped). A separate liveness check benches an agent that has completed zero tasks but already accumulated a timeout or failure run.

## Stagnation

Stagnation measures whole minutes during which **all** agents are idle (any busy agent resets the counter to zero; a session with no agents accrues against wall-clock since the last play). Thresholds are configurable; defaults:

- **warn_after (1 min)** → log, boost RL exploration entropy.
- **alert_after (3 min)** → surface to human with a remediation suggestion.
- **pause_after (5 min)** → auto-pause; human must resume.

Escalation is edge-triggered per stage so a sustained stall does not re-emit every tick.

## Loop Detection

Distinct from stagnation: this tracks a same-play-type failure streak (the counter resets when a different play type succeeds). Thresholds are configurable; defaults:

| Streak | Action |
|--------|--------|
| 3 (`warn_after`) | Log `loop_detected`, RL receives a loop penalty. |
| 5 (`force_switch_after`) | Mask the failing play type for the next selection. |
| 7 (`escalate_after`) | Pause and escalate to human (Level 2). |

A separate `fleet_idle_threshold` counts consecutive selector-idle ticks with no in-flight work and emits a single `fleet_idle_persistent` event on each enter/exit transition (never per tick).
