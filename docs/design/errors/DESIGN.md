# Error Handling — Functional Design

## Responsibility

Define how AgentShore detects, classifies, recovers from, and escalates errors. A single agent failure must not crash the orchestrator.

## Failure Classification

Every completed play that did not fully succeed is assigned a `FailureCategory` from a four-way taxonomy: `code_error` (bug in generated code), `test_failure` (tests fail or coverage regresses), `alignment_drift` (work diverges from beads targets), and `agent_error` (infrastructure/agent-level failure). Error types are mapped to these categories at play completion time by inspecting the play type and the phase in which the failure occurred; phase-dependent play failures (e.g., Issue Pickup during implementation vs. test-writing) resolve to the category matching the failing phase.

## Recovery Strategy

### Tiered Escalation

```
Level 0 — Automatic retry
    Agent API errors (429, 500, 502, 503)
    Retry up to 3 times with exponential backoff (1s, 4s, 16s)

Level 1 — RL absorbs the failure
    Play failures, agent crashes, timeout, IssueInflationDetected
    Record negative reward. RL learns to route around the failure.

Level 2 — Pause and escalate to human
    Triggers: budget exhaustion, 7x loop detection, IntakeParseError,
    all agents in error state, explicit human request.
    Cadence-based checkpoints are opt-in (feedback.cadence_plays / cadence_minutes).

Level 3 — Shutdown
    Database corruption, unrecoverable system error.
    Persist what can be persisted. Generate crash report. Exit.
```

### Circuit Breaker

Per-agent circuit breaker: `CLOSED → OPEN → HALF_OPEN`. Threshold: 3 failures in 5 minutes opens the breaker. OPEN duration: 60 seconds. HALF_OPEN allows one task; success closes, failure re-opens.

## Stagnation

Stagnation fires when alignment OR throughput flatlines (the more restrictive signal governs):
- **5 plays** with zero alignment improvement or zero issues closed → warning, boost RL entropy
- **10 plays** → surface to human
- **15 plays** → auto-pause, human must resume

## Loop Detection

Same-play-type failure streaks, distinct from stagnation (which is cross-play-type). Counter resets when a different play type succeeds.

| Streak | Action |
|--------|--------|
| 3 | Log `loop_detected`, RL receives loop penalty |
| 5 | Mask the failing play type for next selection |
| 7 | Pause orchestrator, escalate to human (Level 2) |
