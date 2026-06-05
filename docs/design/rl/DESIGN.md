# RL Engine — Functional Design

## Responsibility

The RL engine selects the next play from AgentShore's fixed 22-action space, masks invalid actions, records PPO experience, computes rewards, and trains/checkpoints the policy.

## Observation

`OBSERVATION_DIM = 246`, `OBSERVATION_VERSION = 13`.

246-dim float32 vector (v13) encoding: epic closure (4), issues (5), tier-fleet stats (16), budget (4), play history (16), time (3), PR state (3), health (4), handoffs (2), trajectory (3), learnings (3), churn (1), per-config specialization (96), PR author split (4), pressure (5+1), tier×play specialization (66), version marker (1). See `observation.py` for exact slot layout.

The config block aligns with the instantiate-agent config head. The config index is deterministic: configured agent order, then model-tier priority.

## Action Space

`ACTION_SPACE_VERSION = 13`, `NUM_ACTIONS = 22`.

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

`FUTURE_4`, `FUTURE_7`, and `FUTURE_8` are permanently masked reserved slots.

## Masking

The mask is a boolean vector aligned to the 22-action order (`True` = selectable). Reserved slots and draining-session restrictions are always applied first. Precondition masks gate each play on current state (e.g., no open issues masks `ISSUE_PICKUP`; no spawnable config masks `INSTANTIATE_AGENT`; no eligible reviewer masks `CODE_REVIEW`). The repeating play type is *not* force-masked; on a same-type failure streak the stagnation entropy boost raises exploration so the policy diversifies on its own. See `masking.py` for the full rule set.

## Reward

The reward function (see `reward.py`) is clipped to the configured PPO bounds (default `[-10, 10]`). It combines throughput, alignment delta, cost/time penalties, completion/stagnation/failure signals, loop penalties, progress-play bonuses, and utilization/diversity incentives. Cost and time penalties are waived for progress plays; dispatch-only bonuses do not apply to internal lifecycle plays.

## Policy Modes

When `rl.policy_mode: audit-replay` or `agentshore start --policy-mode audit-replay` is used:

- Play head uses masked argmax.
- Config head uses masked argmax.
- Online updates are disabled.
- Entropy does not drive exploration.

This makes policy choices reproducible for the same weights and observation trajectory. Agent execution can still vary because LLMs, tests, GitHub state, timing, and external services are not deterministic. `learning` remains the default mode and keeps stochastic masked policy sampling plus PPO updates/checkpointing enabled.

## Alignment Delta

`alignment_delta` is `float | None`:

- `None`: no beads graph existed around the play. `SEED_PROJECT` receives a small flat bonus in this case; other plays treat it as zero.
- `float`: change in `ProjectGraph.global_closure_ratio` after the play.

This keeps seeding useful without rewarding unrelated work for a missing graph.
