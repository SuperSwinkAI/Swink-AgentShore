# Agent Manager — Functional Design

## Responsibility

The agent layer owns coding-agent lifecycle and dispatch:

- Instantiate CLI agents (`claude`, `codex`, `gemini`) as subprocess-backed handles. All supported agents are CLI-harnessed; raw HTTP/API agents are not supported.
- Track health, status, cost, token totals, task history, GitHub identity, model, model tier, and reasoning effort.
- Dispatch rendered AgentShore skill prompts and return raw output to the play executor.
- Clear or recover agents when sessions drain, agents fail, or circuit breakers cool down.

The agent manager does not decide which play should run. The RL selector chooses the play, the parameter resolver chooses the concrete target, and the executor asks the agent manager to invoke the selected agent.

## Agent Types

| Type | Runtime | Notes |
|------|---------|-------|
| `claude_code` | CLI subprocess | Uses Claude Code with JSON/stream output. |
| `codex` | CLI subprocess | Uses Codex CLI. |
| `gemini` | CLI subprocess | Supports Gemini model tiers. |

Agent capabilities are declared in `src/agentshore/agents/capabilities.py`. Merge and issue creation are treated as AgentShore-mediated repository/GitHub operations, so they are not stranded behind one provider.

## Model Tiers

Agents can expose `small`, `medium`, and `large` model tiers. Tiers are configured under each agent's `model_tiers` section and resolved by `src/agentshore/agents/model_tiers.py`.

Per-play tier eligibility is a hard selection rule in `src/agentshore/agents/_selection.py`:

| Play group | Allowed tiers |
|------------|---------------|
| Cheap mechanical checks and already-gated merges | `small`, `medium` |
| Coding and strategic work | `medium`, `large` |
| Heavyweight validation and project graph work | usually `large`; see source for exact play mapping |

Representative current rules:

| Play | Allowed tiers |
|------|---------------|
| `CLEANUP` | `small`, `medium` |
| `ISSUE_PICKUP`, `UNBLOCK_PR`, `CODE_REVIEW`, `REFINE_TASK_BREAKDOWN`, `SYSTEMATIC_DEBUGGING`, `GROOM_BACKLOG` | `medium`, `large` |
| `RUN_QA`, `WRITE_IMPLEMENTATION_PLAN`, `SEED_PROJECT`, `DESIGN_AUDIT`, `CALIBRATE_ALIGNMENT`, `RECONCILE_STATE` | `large` |
| `MERGE_PR` | `small`, `medium` |

## Skill Dispatch

Skill-backed plays render a project-local skill prompt, write a play-specific context file, then dispatch to the chosen agent. The result parser extracts the last valid result-shaped JSON object from raw agent output.

Current skill templates (installed to `.agents/skills/<skill-name>/SKILL.md`):

- `agentshore-calibrate-alignment`
- `agentshore-cleanup`
- `agentshore-code-review`
- `agentshore-design-audit`
- `agentshore-groom-backlog`
- `agentshore-issue-pickup`
- `agentshore-merge-pr`
- `agentshore-reconcile-state`
- `agentshore-refine-tasks`
- `agentshore-run-qa`
- `agentshore-seed-project`
- `agentshore-systematic-debugging`
- `agentshore-unblock-pr`
- `agentshore-write-plan`

## Selection Rules

Agent selection (`select_agent_for()` in `src/agentshore/agents/_selection.py`) applies hard filters first (pinned agent/type, anti-confirmation identity guard for code review, user excludes, tier eligibility, required capability), then breaks ties with soft preferences (branch exposure, user affinity, cost, least history).

`RUN_QA` has no anti-confirmation rule. It validates trunk state rather than a single author's branch.

## Instantiate Agent

`INSTANTIATE_AGENT` expands capacity by `(agent_type, model_tier)`. A config can be spawned when seed project has completed, budget and cooldown allow it, live-agent counts are below caps, the config is enabled and not auth-blocked, and no idle same type/tier agent already exists. Busy same-config agents do not block another spawn.

## Health And Recovery

Circuit breaker defaults: `3` failures in `300` seconds, `60` second cooldown. `TAKE_BREAK` can recover agents whose error class is `rate_limit` or `unknown` after the break interval.

## Concurrency And Handoffs

Multiple agents and plays can be in flight simultaneously. Work claims prevent duplicate issue/PR assignment. Handoff records track context transfer estimates when work moves between agents around `END_AGENT`.
