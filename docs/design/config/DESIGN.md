# Config — Functional Design

## Responsibility

AgentShore configuration controls project discovery, agent setup, spawn limits, budget enforcement, RL parameters, session behavior, logging, UI, browser verification, learnings, and skill installation.

Configuration is loaded into frozen dataclasses in `src/agentshore/config/models.py`. The source code is canonical for field names and defaults; this document records the user-facing contract.

## Files And Precedence

| Source | Scope | Precedence |
|--------|-------|------------|
| CLI flags | Current invocation | Highest |
| Explicit config path or `<project_root>/agentshore.yaml` | Project | Middle |
| Built-in default YAML | Package default | Lowest |

`agentshore start` can run without a config file. When no file is present, the built-in default YAML is parsed through the same validation path as project config. `agentshore init` writes or merges a detected `agentshore.yaml` that the user can edit.

## Spawn Limits

`agent_spawn` controls `INSTANTIATE_AGENT`:

| Field | Default | Meaning |
|-------|---------|---------|
| `cooldown_plays` | `2` | Completed plays required between successful instantiate plays. |
| `max_per_config` | `5` | Max live agents for one `(agent_type, model_tier)`. |
| `max_total` | `10` | Max live agents across the session. |

A type/tier is spawnable only when it is enabled, within both slot caps, not blocked by auth/model errors, and no idle same type/tier agent already exists. Busy agents do not block another same-config spawn.

## Budget

Budget enforcement is opt-in. `budget.enabled: false` disables the spending cap even if `budget.total` is set.

When enabled, `budget.total` must be at least the configured minimum enforced by `load_config` (`MIN_ENABLED_BUDGET_USD`, currently `$20.00`). CLI `--no-budget` disables enforcement for a run.

## Hot Reload

The orchestrator reloads `agentshore.yaml` on SIGHUP; invalid reloads are rejected and the previous frozen config instance remains active.
