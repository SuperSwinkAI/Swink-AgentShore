# AgentShore V1 Contract

This file is the canonical implementation contract for the alpha. Component
designs and planning docs should conform to this file when they disagree.

## Product Model

- AgentShore is a PPO/RL-based orchestrator from the first runnable alpha.
- The policy selects one of 22 play types (19 active + 3 permanently masked
  reserved slots). Parameters are resolved by a deterministic resolver after
  play selection.
- GitHub issues are the default work queue and control surface.
- The product posture is YOLO/autonomous by design: AgentShore is expected to
  create, edit, label, decompose, and close GitHub issues during normal
  operation. Human approval is not the default control loop.
- SEED_PROJECT is the first play of any new session: it invokes `bd` to build
  the epic→story→task BEADS graph from open GitHub issues. Subsequent work is
  driven by `bd ready` output via ISSUE_PICKUP. There is no `goals.yaml`.
- `alignment_delta` is `float | None`: `None` means BEADS has not been
  initialised yet; a `float` value is the global_closure_ratio delta per tick.
- Scope validation enforces issue-inflation limits. Artifact drift is evidence-only until AgentShore has reliable beads-native path boundaries.

## Play Action Space

The action index order is fixed for checkpoints, replay, and reports:

| Index | Play | Notes |
|---:|---|---|
| 0 | Instantiate Agent | spawn capacity only when an enabled type/tier has no idle same-config agent |
| 1 | Unblock PR | |
| 2 | Write Implementation Plan | |
| 3 | End Agent | |
| 4 | Issue Pickup | |
| 5 | Code Review | |
| 6 | Merge Pull Request | |
| 7 | Run QA | |
| 8 | Systematic Debugging | |
| 9 | Design Audit | audits design/spec requirements and creates/links gap issues |
| 10 | End Session | |
| 11 | Reconcile State | event-driven self-heal; masked except during wedge conditions |
| 12 | Refine Task Breakdown | |
| 13 | Cleanup | |
| 14 | Browser Verification | |
| 15 | Take Break | |
| 16 | Groom Backlog | |
| 17 | Seed Project | |
| 18 | Calibrate Alignment | |
| 19 | Future 6 | reserved / permanently masked |
| 20 | Future 7 | reserved / permanently masked |
| 21 | Future 8 | reserved / permanently masked |

Any policy checkpoint must include this action-space version:
`{"action_space_version": 13, "num_actions": 22, "policy_version": 5}`

`Instantiate Agent` uses the config head to choose `(agent_type, model_tier)`.
A type/tier config is spawnable only when the session is seeded, budget and
cooldown gates pass, total live agents are below `agent_spawn.max_total`, live
agents for that same type/tier are below `agent_spawn.max_per_config`, and no
idle agent of that type/tier already exists. Busy agents do not block spawning
additional same-config capacity.

## PPO-First Alpha

PPO is active in v1 alpha. `learning` policy mode is the default execution path;
`audit-replay` is a replay/debug mode for inspecting policy choices.

The alpha policy must use:

- cold-start logits biased toward Issue Pickup, Code Review, Run QA, and
  Merge PR (see cold-start weights table below);
- hard action masks from play preconditions;
- PPO updates after `rl.update_every` completed plays, default `16`;
- reward clipping to `[-10, 10]`;
- checkpoint rollback on NaN or invalid logits;
- `audit-replay` mode with greedy masked selection and policy updates disabled
  for audit/debug runs.

Each PPO experience row must persist:

- observation vector;
- action index;
- reward;
- next observation vector;
- done flag;
- old action log probability;
- value estimate;
- action mask at selection time;
- policy version or checkpoint id;
- action-space version;
- config hash;
- episode/session id and step index.

Offline PPO training may only train on trajectories that include these fields.

### Cold-Start Play Weights

The actor bias is initialised from `DEFAULT_PLAY_WEIGHTS` in
`src/agentshore/rl/cold_start.py`. Reserved slots are masked at runtime via
preconditions and are never selected in practice.

| Play | Cold-Start Weight |
|---|---:|
| Issue Pickup | 0.2233 |
| Code Review | 0.1365 |
| Run QA | 0.0992 |
| Merge Pull Request | 0.0992 |
| Write Implementation Plan | 0.0744 |
| Unblock PR | 0.0682 |
| Refine Task Breakdown | 0.0496 |
| Systematic Debugging | 0.0434 |
| Instantiate Agent | 0.0285 |
| Groom Backlog | 0.0248 |
| Seed Project | 0.0186 |
| Calibrate Alignment | 0.0186 |
| Cleanup | 0.0120 |
| Browser Verification | 0.0120 |
| Design Audit | 0.0114 |
| End Agent | 0.0114 |
| Reconcile State | 0.0114 |
| Future 6 (reserved) | 0.0114 |
| Future 7 (reserved) | 0.0114 |
| Future 8 (reserved) | 0.0114 |
| End Session | 0.0057 |
| Take Break | 0.0057 |

## Autonomy And Hard Gates

AgentShore intentionally has a riskier autonomous posture. The implementation
should favor moving work forward and creating traceable GitHub artifacts over
asking for permission. Hard gates are limited to correctness invariants and
operations where proceeding would corrupt the session state.

- Code Review reviewer must not be the PR author.
- Run QA is large-tier validation of trunk/default-branch state; it is not identity-blocked.
- Merge PR requires an approved PR and passing CI, then dispatches on small/medium tiers because the expensive validation work is already captured by review and QA gates.
- Budget exhaustion masks all plays except End Session and approved budget
  adjustment paths.
- Human override cannot bypass anti-confirmation-bias constraints.

GitHub writes are expected in normal operation:

- issue creation, issue edits, labels, PR creation, and merge go through a
  AgentShore-owned GitHub adapter for auditability and idempotency;
- every external mutation receives an idempotency key and an audit record;
- autonomous merge is allowed in v1 when the merge hard gate is satisfied;
- artifact drift evidence remains non-blocking until AgentShore has reliable
  beads-native path boundaries.

## Skill Map

Skill-backed plays use these canonical skill names:

| Play | Skill |
|---|---|
| Seed Project | `agentshore-seed-project` |
| Unblock PR | `agentshore-unblock-pr` |
| Write Implementation Plan | `agentshore-write-plan` |
| Issue Pickup | `agentshore-issue-pickup` |
| Code Review | `agentshore-code-review` |
| Merge Pull Request | `agentshore-merge-pr` |
| Run QA | `agentshore-run-qa` |
| Systematic Debugging | `agentshore-systematic-debugging` |
| Design Audit | `agentshore-design-audit` |
| Refine Task Breakdown | `agentshore-refine-tasks` |
| Cleanup | `agentshore-cleanup` |
| Browser Verification | `agentshore-browser-verify` |
| Groom Backlog | `agentshore-groom-backlog` |
| Calibrate Alignment | `agentshore-calibrate-alignment` |
| Reconcile State | `agentshore-reconcile-state` |

## AgentShore State Snapshot

`AgentShoreState` is the shared state contract for TUI, IPC, reports, and tests.
It must include:

- session id, state, run mode, and policy mode;
- action-space version (13), policy version (5), and policy checkpoint id;
- current play and recent play history;
- agents and agent status;
- BEADS graph summary (epic/story/task counts) and open issue counts;
- open issues and open PR summaries;
- budget snapshot;
- metrics snapshot including `alignment_delta` (`float | None`);
- trajectory snapshot;
- loop detection state;
- seed freshness (replaces intake freshness);
- learnings count;

## V1 Persistence

Schema version: 13. BEADS graph state is owned by the `bd` tool and is not
replicated into AgentShore SQLite. The schema (22 tables) must support the safety
and PPO contracts, persisting at minimum: sessions; plays with params, output,
artifacts, reward, failure category, and checkpoint id; agents and task history;
GitHub issues cache; PR/branch/commit authorship for anti-confirmation-bias
checks; external mutation audit records; dispatch replay/idempotency records;
scope evidence; PPO experience rows with the full fields
listed above; policy checkpoints; session learnings and review patterns.
