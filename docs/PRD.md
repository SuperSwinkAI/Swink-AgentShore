# Product Requirements Document: AgentShore

## Overview

AgentShore is an RL-based orchestrator that coordinates multiple LLM coding agents (Claude, Codex, Gemini, etc.) to autonomously progress coding projects. A reinforcement learning agent selects high-level "plays" — discrete skills that advance the project — while the human's control surface is the GitHub issue tracker, not AgentShore-specific configuration.

The RL agent does not write code. It decides *what to do next* and *which agent does it*.

> **Design documentation**: [High-Level Design](design/HLD.md) — architecture overview, tech stack, data flow diagrams, and component map linking to all 13 component design docs.

## Operating Philosophy

**Autonomous by default.** AgentShore works from GitHub issues as its source of truth. It picks up issues, decomposes large ones, implements, reviews, tests, and merges — without asking for permission at each step. Human approval is an escalation path, not the default flow.

**YOLO intent is explicit.** AgentShore is intentionally a risk-tolerant autonomous operator. Creating and editing GitHub issues, opening PRs, filing QA/review follow-ups, merging eligible PRs, and running cleanup work are normal behavior, not exceptional behavior. Auditability and hard invariants matter, but the product should not drift toward approval-heavy orchestration by default.

**Issues are both input and output.** The work queue is the GH issue tracker. AgentShore reads issues to find work, and creates new issues when QA finds regressions, code review surfaces follow-ups, or task refinement decomposes large work. AgentShore-created issues are labeled by source where applicable (`agentshore/intake`, `agentshore/qa`, `agentshore/review`).

**The human steers via GitHub.** Close an issue to deprioritize it. Open an issue to add work. Reprioritize labels. Drop a PRD or spec file in the repo. AgentShore adapts. No AgentShore-specific approval UI needed — the issue tracker is the shared control surface.

## Roles

| Role | Responsibility |
|------|---------------|
| **Human** | Provide seed material (issues, PRDs, specs). Steer via issue triage. Override when needed. |
| **RL Agent (AgentShore)** | Select plays, assign agents, optimize progression through the beads-backed project graph. |
| **Coding Agents** | Execute assigned work: write code, run tests, review PRs, perform QA. Each agent runs at a `small`, `medium`, or `large` tier (see Agent Tiers). |

## Agent Tiers

> Design doc: [Agent Manager](design/agents/DESIGN.md)

Coding agents are organized into three cost/capability tiers so the policy can match work to the cheapest sufficient agent. Tiers default to:

| Tier | Claude Code | Codex | Gemini | Typical use |
|------|-------------|-------|--------|-------------|
| `small` | Haiku | `gpt-5.4-mini` | `flash-lite` | Cheap mechanical checks — cleanup |
| `medium` | Sonnet | `gpt-5.3-codex` | `auto` | Default workhorse — implementation, code review, refinement, debugging, groom backlog |
| `large` | Opus | `gpt-5.5` with high reasoning | `pro` | Heavy validation and project graph work — QA, planning, seed project, design audit, calibrate alignment |

Per-play tier eligibility is a hard constraint enforced at agent selection time. Plays declare which tier band they accept; the parameter resolver only considers idle agents within that band, then PPO learns affinity *within* the band over time.

Agent expansion follows the same type/tier lifecycle. `Instantiate Agent` is masked when budget, cooldown, auth/model, or slot limits fail, or when every enabled type/tier already has an idle agent. Busy agents do not block spawning another agent of the same type/tier under the configured caps.

## Plays (Skills)

> Design docs: [Play System](design/plays/DESIGN.md) | [RL Engine](design/rl/DESIGN.md) | [Agent Manager](design/agents/DESIGN.md)

Each play is an atomic action the RL agent can select. Plays are the unit of decision-making. The action space has 22 slots (action-space version 13); 19 are active plays and 3 remain permanently reserved/masked (FUTURE_4, FUTURE_7, FUTURE_8).

### Complete Play Table (declaration order, idx 0–21)

| Idx | Play | Tier | Description |
|-----|------|------|-------------|
| 0 | **Instantiate Agent** | any | Spin up a new coding agent instance when no idle agent of the requested type/tier is available. |
| 1 | **Unblock PR** | medium+ | Resolve a PR that is blocked on review feedback, merge conflicts, or CI failures. |
| 2 | **Write Implementation Plan** | large | Write a detailed implementation plan comment on a GitHub issue before pickup begins. |
| 3 | **End Agent** | any | Terminate an agent instance and release its resources. |
| 4 | **Issue Pickup** | medium+ | Fetch highest-priority `bd ready` issue, implement solution, write tests, open PR. |
| 5 | **Code Review** | medium+ | Review a PR using a *different* GitHub identity than the PR author. May create follow-up GH issues labeled `agentshore/review`. |
| 6 | **Merge PR** | small/medium | Merge an approved PR into the target branch. Close the associated issue. |
| 7 | **Run QA** | large | Execute an independent quality assurance cycle. May create bug/regression GH issues labeled `agentshore/qa`. |
| 8 | **Systematic Debugging** | medium+ | Methodically diagnose and fix a persistent failure across multiple files or components. |
| 9 | **Design Audit** | large | Audit project design files/specs/PRDs against source/tests/GitHub/beads and create/link issues for unmet requirements. |
| 10 | **End Session** | any | Gracefully shut down all agents, persist state, and produce a session summary. |
| 11 | **Reconcile State** | medium+ | Parse recent failure logs and reconcile local state (branches, worktrees, stale locks) to unblock subsequent plays. Armed by prior play failures. |
| 12 | **Refine Task Breakdown** | medium+ | Re-analyze open issues, decompose oversized ones into sub-issues, re-prioritize based on current state. |
| 13 | **Cleanup** | small/medium/large | Remove stale branches, tidy transient artifacts, and close obsolete issues. |
| 14 | **FUTURE_4** | — | Reserved / permanently masked. |
| 15 | **Take Break** | any | Brief pause between intensive plays to avoid rate limits or context degradation. |
| 16 | **Groom Backlog** | medium+ | Review and re-prioritize the open issue queue in the beads graph. |
| 17 | **Seed Project** | large | Call `bd` to build the full epic → story → task hierarchy from seed material; bootstrap the beads graph for a new session. |
| 18 | **Calibrate Alignment** | large | Measure `global_closure_ratio` against the beads graph and emit an `alignment_delta` signal. |
| 19 | **Prune** | small/medium/large | Retire stale worktrees, merged/closed branches, and dead beads to clear accumulated infrastructure debt. Armed only when measurable debt exists. |
| 20 | **FUTURE_7** | — | Reserved / permanently masked. |
| 21 | **FUTURE_8** | — | Reserved / permanently masked. |

## Reward Function

> Design doc: [RL Engine — Reward Function](design/rl/DESIGN.md#reward-function)

After each play, AgentShore computes a clipped weighted sum of reward components (range `[-10, +10]`). Issue throughput, alignment delta, and per-play progress bonus are the primary positive signals; loop, inflation, and stagnation penalties are the primary negatives. Cost and time penalties act as regularizers and are waived on progress plays.

Key shaping signals beyond throughput:

- **Concurrent-agent bonus** — rewards keeping more of the fleet in flight rather than serializing.
- **Type-diversity bonus** — discourages monoculture (e.g., reviewing nine PRs in a row when implementation work is queued).
- **Velocity bonus** — reinforces compounding throughput (closed issues + PRs per play).
- **Anti-confirmation bonus** — positive when Code Review is assigned to a different GitHub identity than the PR author.
- **Loop penalty** — escalates linearly with `same_type_failure_streak`.

Reward weights and shaping knobs are configurable in `agentshore.yaml`.

## Issue Lifecycle

```
Seed material (PRD / spec / existing issues / repo with no issues)
    ↓
Seed Project (bd builds epic → story → task hierarchy; mirrors to GH issues)
    ↓
Refine Task Breakdown (decompose oversized stories → sub-tasks as needed)
    ↓
Issue Pickup (bd ready → implement → test → PR)
    ↓
Code Review (may create follow-up issues: agentshore/review)
    ↓
Run QA (may create bug issues: agentshore/qa)
    ↓
Merge PR & close issue
    ↓
New issues feed back into the queue; Calibrate Alignment updates global_closure_ratio
```

## Observability & Metrics

> Design docs: [Metrics](design/metrics/DESIGN.md) | [Logging](design/logging/DESIGN.md) | [Reports](design/reports/DESIGN.md)

The system must track the following to inform RL decisions and enable human oversight:

1. **Time cost per play** — Wall-clock and token cost for each play execution.
2. **Alignment delta** — Live per-tick delta from `state.graph.global_closure_ratio` (`float | None`; `None` when beads is not initialised).
3. **Agent handoff efficiency** — Overhead when transferring work between agents (context loss, ramp-up time).
4. **Failure classification** — Categorize failures by type: code errors, test failures, alignment drift, agent errors.
5. **Issue throughput** — Issues opened, closed, and net velocity per session.
6. **Cumulative cost vs. scope** — Running total spend relative to remaining open issues.
7. **Agent specialization scores** — Per-agent effectiveness by task type (review, implementation, QA).
8. **Stagnation and loop detection** — Identify looping behaviors, repeated failed plays, and same-play-type failure streaks.
9. **Human feedback checkpoints** — Escalation-triggered moments for human review and course correction (budget exhaustion, loop detection, stagnation, ambiguous intake) plus opt-in cadence checkpoints when `feedback.cadence_plays` or `feedback.cadence_minutes` is configured.
10. **Trajectory validation** — Compare projected completion path against the requested end state.
11. **Code review feedback patterns** — Parse recurring reviewer comments to improve future implementation prompts.
12. **Session knowledge accumulation** — Track codebase-specific patterns discovered during play execution for cross-play learning.

## Success Criteria

> Design docs: [RL Engine](design/rl/DESIGN.md) (reward function) | [Core](design/core/DESIGN.md) (orchestrator loop) | [Error Handling](design/errors/DESIGN.md) (anti-confirmation enforcement)

- **Issue throughput**: Open issues decrease over time. AgentShore closes more issues than it creates.
- **Cost efficiency**: Total cost stays within budget constraints; the RL agent prefers cheaper paths when alignment impact is equivalent.
- **Autonomous operation**: Human intervention is the exception. The system runs unattended for the majority of a session.
- **Anti-confirmation bias**: Code review is always performed by a different GitHub identity than the PR author, preventing self-confirming review loops. QA runs against trunk/default-branch state and is not identity-blocked in the current implementation.

## CLI Design

AgentShore ships a flat CLI under a single binary: `agentshore <subcommand>`. Current subcommands: `init`, `start`, `stop`, `status`, `dashboard`, `configure`, `identity`, `archive`, `report`, `train`, `trusted-ids`.

**`dev` namespace decision:** A nested `dev` namespace (e.g. `agentshore dev start`) was considered but rejected for v0.1.0. A second vertical (`ops`, `support`) would justify namespacing, but forcing one namespace before it has a sibling adds ceremony without clarity. The CLI stays flat until a second distinct operator surface exists.

## Constraints

> Design docs: [Data Layer](design/data/DESIGN.md) (persistence, schema) | [Error Handling](design/errors/DESIGN.md) (recovery, escalation)

- The RL agent selects plays but does not generate code directly.
- Humans can override any RL decision at any time via TUI or by triaging issues on GitHub.
- Agent instantiation is bounded by cost parameters.
- All play executions are logged for auditability.
- Issues are the unit of work. AgentShore creates, decomposes, and closes GH issues — not a parallel task system.
- Human approval is escalation-only: budget exhaustion, repeated failure (7x loop), or ambiguous seed material.
- Sessions can be archived per project for cross-session comparison.
