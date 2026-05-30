# Core — Functional Design

## Responsibility

The core orchestrator is AgentShore's composition root. It owns session lifecycle, state refresh, RL selection, play execution, reward persistence, policy updates, feedback/drain handling, and state publication to TUI or IPC.

## Orchestrator Loop

1. Refresh GitHub, beads, agents, budget, stats, and masks into `AgentShoreState`.
2. Publish state to the configured provider.
3. If draining, continue ending idle agents until shutdown can complete.
4. Ask the RL selector for a play and parameters, unless a user override is queued.
5. Execute the play through `PlayExecutor`.
6. Refresh GitHub/beads-derived state affected by the play.
7. Compute alignment delta, reward, failure/streak updates, and policy experience.
8. Persist play, reward, and RL experience.
9. Run online PPO updates/checkpoints when configured intervals are reached.
10. Check budget, loop, terminal no-work, drain, and shutdown conditions.

Scope validation enforces issue-inflation limits and leaves artifact drift as evidence-only until AgentShore has reliable beads-native path boundaries.

## Startup

`agentshore start` resolves config, detects environment capabilities, opens the database and session, builds all subsystems, and enters the orchestrator loop. `agentshore init` is the explicit setup command for config, skills, identities, and beads.

## Play Execution

The executor dispatches skill-backed plays through an agent (context file → skill render → dispatch → parse result → update cache → `PlayOutcome`) and runs internal plays (`INSTANTIATE_AGENT`, `END_AGENT`, `END_SESSION`, `TAKE_BREAK`, reserved slots) directly without invoking a coding agent.

## Work Claims

The resolver and store use `work_claims` to prevent duplicate issue pickup, duplicate PR review/unblock/merge, and to serialize session-scoped work; claims are superseded when issues/PRs close or work is abandoned at shutdown.

## Feedback And Drain

Default operation is autonomous. Human feedback is requested only for escalation cases (budget exhaustion, loop escalation, stagnation, ambiguous intake, or explicit user commands).

Graceful stop uses drain mode — new work stops, running agents finish, `END_AGENT` handles cleanup. Hard stop bypasses drain and terminates immediately.

## Loop Detection

The orchestrator tracks both `same_type_failure_streak` and `same_type_streak`.

| Threshold | Behavior |
|-----------|----------|
| Failure streak `>= 3` | Loop penalty begins and warning state is surfaced. |
| Failure streak `>= 5` | Stagnation entropy boost raises exploration — the repeating play type is *not* force-masked; the policy diversifies on its own. |
| Failure streak `>= 7` | Human escalation. |
| Any-outcome streak `>= 6` | Reward penalty discourages collapse onto cheap successful loops. |

Forced masks are reflected in `AgentShoreState.forced_mask_zeros` and in IPC/dashboard mask reasons.

## State Providers

The core publishes through the `StateProvider` protocol: TUI provider (Textual solo mode), IPC provider (embedded/headless agent mode), and null provider (tests). Dashboard mode is a bridge on top of IPC.
