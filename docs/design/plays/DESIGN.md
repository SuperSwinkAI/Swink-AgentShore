# Play System — Functional Design

## Responsibility

The play system defines AgentShore's 22-action RL surface: the play catalog, each play's preconditions, parameter schema, execution path, and outcome mapping. A play is *what to do next* — the unit the RL policy chooses on every tick.

The action order is locked by `PlayType` in `src/agentshore/state.py` and `V1_ACTION_ORDER` in `src/agentshore/rl/action_space.py`. The 22-slot tensor is the policy head's output shape (see [../rl/DESIGN.md](../rl/DESIGN.md)); order and width must never drift from the enum.

## Design Choices

**Two-stage selection.** The RL policy selects only the play *type* (a slot index). It does not pick targets. A separate parameter resolver then chooses the concrete agent / issue / PR for that type. This keeps the policy's job small and stable — 22 discrete actions — while target heuristics evolve independently of the learned weights. The resolver runs after the masked policy output, so it only ever resolves a play the preconditions already admitted.

**Preconditions are the mask.** Each play reports its own `preconditions()` as a list of mask reasons; an empty list means the slot is eligible this tick. The RL engine ANDs these into the action mask so the policy can never select an invalid play (e.g. merging a PR that does not exist, or reviewing when no PR is pending). This is the single source of truth for "is this play legal right now" — there is no second gate downstream.

**Anti-confirmation invariant (Code Review).** `CODE_REVIEW` is masked unless an idle `can_review` agent exists whose GitHub identity differs from the PR author. A reviewer cannot approve its own work. This is a hard invariant enforced in the precondition, not a soft preference. `RUN_QA` validates trunk/default-branch state and is deliberately *not* identity-blocked — any identity is a valid QA runner.

**Reserved headroom.** Three slots (`FUTURE_4` @ 14, `FUTURE_7` @ 20, `FUTURE_8` @ 21) are permanent no-op placeholders. They keep the tensor shape and checkpoint layout fixed so the action space can grow without a mass migration or weight reset. Reserved plays always report `RESERVED_SLOT` from `preconditions()` and are therefore structurally masked; if ever selected they fail closed. Slot 14 previously hosted browser verification and was emptied back to reserved in place (no version bump — tensor shape unchanged). Slots 11 (`RECONCILE_STATE`) and 19 (`PRUNE`) were formerly `FUTURE_5`/`FUTURE_6` and were filled in place.

## The Play Contract

Every play implements the `Play` protocol (`src/agentshore/plays/base.py`):

- `play_type` — the `PlayType` this play backs (one play per slot).
- `preconditions(state)` — mask reasons; empty ⇒ eligible.
- `estimated_cost(state)` — projected budget spend, fed to budget-aware masking and reward shaping.
- `execute(state, params, ctx)` — perform the play, returning a `PlayOutcome`.

Plays are instantiated and registered into a frozen `PlayRegistry` in enum order, keeping `V1_ACTION_ORDER` and the registry in lockstep. Lookup is by `PlayType`; the default registry registers all 22 and freezes.

## Play Categories

| Category | Count | Plays |
|----------|-------|-------|
| Skill-backed | 15 | `UNBLOCK_PR`, `WRITE_IMPLEMENTATION_PLAN`, `ISSUE_PICKUP`, `CODE_REVIEW`, `MERGE_PR`, `RUN_QA`, `SYSTEMATIC_DEBUGGING`, `DESIGN_AUDIT`, `REFINE_TASK_BREAKDOWN`, `CLEANUP`, `GROOM_BACKLOG`, `SEED_PROJECT`, `CALIBRATE_ALIGNMENT`, `RECONCILE_STATE`, `PRUNE` |
| Active internal | 4 | `INSTANTIATE_AGENT`, `END_AGENT`, `END_SESSION`, `TAKE_BREAK` |
| Reserved | 3 | `FUTURE_4`, `FUTURE_7`, `FUTURE_8` |

19 active plays + 3 reserved = 22 slots. Skill-backed plays delegate real work to a coding agent; active internal plays are orchestrator-side capacity/lifecycle moves with no coding agent. Reserved slots are no-op placeholders, always masked.

## Skill Dispatch

Skill-backed plays dispatch a project-local skill from `.agents/skills/<skill-name>/SKILL.md`. AgentShore injects only the minimal target parameters and writes a play-specific context file. The coding agent is responsible for repository/GitHub discovery, implementation, validation, and the final JSON result block.

The result parser extracts the last valid result-shaped JSON object from raw agent output. The parsed `SkillResult` is mapped to `PlayOutcome`, persisted, and scored by the reward function.

## Action Space

| Index | Play |
|-------|------|
| 0 | `INSTANTIATE_AGENT` |
| 1 | `UNBLOCK_PR` |
| 2 | `WRITE_IMPLEMENTATION_PLAN` |
| 3 | `END_AGENT` |
| 4 | `ISSUE_PICKUP` |
| 5 | `CODE_REVIEW` |
| 6 | `MERGE_PR` |
| 7 | `RUN_QA` |
| 8 | `SYSTEMATIC_DEBUGGING` |
| 9 | `DESIGN_AUDIT` |
| 10 | `END_SESSION` |
| 11 | `RECONCILE_STATE` |
| 12 | `REFINE_TASK_BREAKDOWN` |
| 13 | `CLEANUP` |
| 14 | `FUTURE_4` |
| 15 | `TAKE_BREAK` |
| 16 | `GROOM_BACKLOG` |
| 17 | `SEED_PROJECT` |
| 18 | `CALIBRATE_ALIGNMENT` |
| 19 | `PRUNE` |
| 20 | `FUTURE_7` |
| 21 | `FUTURE_8` |

`ACTION_SPACE_VERSION = 13`.

## Skill-Backed Plays

| Play | Skill | Key gates |
|------|-------|-----------|
| `SEED_PROJECT` | `agentshore-seed-project` | Not in-flight; 50-play cooldown (bypassed on failure) |
| `GROOM_BACKLOG` | `agentshore-groom-backlog` | Beads has epics; idle `can_create_issues` agent; 20-play cooldown; urgent bypass for unlinked tasks |
| `DESIGN_AUDIT` | `agentshore-design-audit` | Beads has epics; idle `can_create_issues` agent; 20-play cooldown |
| `CALIBRATE_ALIGNMENT` | `agentshore-calibrate-alignment` | Beads has epics; 20-play cooldown; large-only |
| `RECONCILE_STATE` | `agentshore-reconcile-state` | Beads has epics; idle agent; cooldown-gated |
| `REFINE_TASK_BREAKDOWN` | `agentshore-refine-tasks` | Open issue with `agentshore/needs-refinement` |
| `WRITE_IMPLEMENTATION_PLAN` | `agentshore-write-plan` | Idle `can_implement` agent; unplanned issue not covered by open PR |
| `ISSUE_PICKUP` | `agentshore-issue-pickup` | Eligible open issue; idle `can_implement` agent; PR count below backpressure threshold; pre-session PRs drained |
| `CODE_REVIEW` | `agentshore-code-review` | Pending review or unreviewed PR; idle `can_review` agent; reviewer identity differs from PR author |
| `UNBLOCK_PR` | `agentshore-unblock-pr` | Idle `can_implement` agent; blocked PR exists; not manual-required |
| `MERGE_PR` | `agentshore-merge-pr` | Idle `can_merge` agent; small/medium tier only; PR approved + `MERGEABLE` |
| `RUN_QA` | `agentshore-run-qa` | Idle `can_test` agent; 25-play cooldown; <10 open issues; no anti-confirmation rule |
| `SYSTEMATIC_DEBUGGING` | `agentshore-systematic-debugging` | Idle `can_implement` agent; open issue with debug trigger label; not root-cause-found |
| `CLEANUP` | `agentshore-cleanup` | Idle `can_implement` agent; 20-play cooldown; <15 open issues |

## Internal Plays

### Instantiate Agent

Expand capacity by spawning an enabled `(agent_type, model_tier)`. Gates: seed project completed, budget and cooldown allow, live-agent counts below caps, config enabled and not auth-blocked, no idle same type/tier agent exists. Busy same-config agents do not block another spawn.

### End Agent

Terminate an idle agent and free its slot. Gates: at least two active agents (outside drain), idle candidate exists, candidate exceeds minimum play count (bypassed during drain).

### Take Break

Pause for `session.break_duration_minutes`, then recover agents in `rate_limit` or `unknown` error. Targets exactly one erroring agent; masked when none exists; duplicate breaks for the same agent are blocked.

### End Session

Shut down the session and persist final state/report artifacts. Masked until seed/design-audit freshness and alignment/terminal conditions permit, or failure/no-work terminal logic selects it.
