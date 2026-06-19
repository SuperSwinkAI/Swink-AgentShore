# Product Requirements Document: AgentShore

## Overview

AgentShore is an RL-based orchestrator that coordinates multiple LLM coding agents (Claude, Codex, Grok, Antigravity, etc.) to autonomously progress coding projects. A reinforcement learning agent selects high-level "plays" — discrete skills that advance the project — while the human's control surface is the GitHub issue tracker, not AgentShore-specific configuration.

The RL agent does not write code. It decides *what to do next* and *which agent does it*. The PPO policy drives all direction; deterministic code only backstops invalid plays and never gates with human-in-the-loop.

AgentShore spans three layers: **BEADS** is the canonical project graph (epics → stories → tasks); **GitHub** is the human conversation surface (each issue/PR mirrored via `external_ref="gh-N"`); **AgentShore SQLite** holds session-scoped RL state. The RL stack pins action-space version 13 (22-slot head), observation version 14 (250 features), policy version 5, and SQLite schema version 4.

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

Coding agents are organized into `small`, `medium`, and `large` tiers so the policy can match work to the cheapest sufficient agent. Exact model defaults live in `src/agentshore/agents/model_catalog.py` and `src/agentshore/agents/model_tiers.py`.

Per-play tier eligibility is a hard constraint enforced at agent selection time. The parameter resolver only considers idle agents within the eligible band, then PPO learns affinity *within* the band over time.

Agent expansion follows the same type/tier lifecycle. `Instantiate Agent` is masked when budget, cooldown, auth/model, or slot limits fail, or when every enabled type/tier already has an idle agent. Busy agents do not block spawning another agent of the same type/tier under the configured caps.

## Plays (Skills)

> Design docs: [Play System](design/plays/DESIGN.md) | [RL Engine](design/rl/DESIGN.md) | [Agent Manager](design/agents/DESIGN.md)

Each play is an atomic action the RL agent can select. The action space has 22 slots (action-space version 13); 19 are active plays and 3 remain permanently reserved/masked. The canonical declaration order is `PlayType` in `src/agentshore/state.py`; the human-readable contract is [V1_CONTRACT.md](design/V1_CONTRACT.md).

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

Seed material, existing issues, and design docs become a beads graph through Seed Project. Ready beads drive Issue Pickup, which produces PRs; Code Review, QA, and Merge PR feed new or closed issues back into the same GitHub/beads queue. Calibrate Alignment updates `global_closure_ratio` as the graph changes.

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

AgentShore ships a flat CLI under a single binary: `agentshore <subcommand>`. Current registered subcommands are `init`, `start`, `stop`, `dashboard`, `identity`, and `trusted-ids`; `src/agentshore/cli/__init__.py` is authoritative.

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
