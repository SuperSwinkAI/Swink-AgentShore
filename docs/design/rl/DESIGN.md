# RL Engine — Functional Design

## Responsibility

The RL engine is the sole driver of orchestration direction. A custom PPO
actor-critic selects the next play from AgentShore's fixed 22-action space,
masks structurally-invalid actions, samples a spawn config when the play is
`INSTANTIATE_AGENT`, records experience, computes rewards, and trains and
checkpoints the policy. Deterministic code only removes genuinely-unavailable
options (the mask); within the valid set, every directional choice — what to do
next, which tier to spawn, when to end the session — is the policy's.

See [V1 contract](../V1_CONTRACT.md) for the frozen tensor-shape guarantees
and [plays design](../plays/DESIGN.md) for what each play does.

## Why PPO Drives Everything

The play space is small and the reward is delayed and noisy (a merged PR may be
several plays removed from the pickup that produced it). PPO's advantage
estimation handles that credit assignment, and the masked stochastic policy
explores the play space without hand-written heuristics deciding direction. The
mask is a *validity* filter, never a *preference* one: it answers "can this play
run right now?", not "should it?".

## Observation

`OBSERVATION_DIM = 246`, `OBSERVATION_VERSION = 13`.

A 246-dim float32 vector encoding session state. High-level blocks (exact slot
layout lives in `observation.py`):

| Slots | Block | What |
|-------|-------|------|
| 0–1 | dependency | beads blocked- / ready-task ratios (v13) |
| 2–7 | retired | permanently zero-filled |
| 8–11 | epic | global + top-3 epic closure ratios (beads-native) |
| 12–16 | issue | open/closed/created/net-velocity/scope-completion |
| 17–32 | tier-fleet | 3 tiers × 5 stats + active-count |
| 33–36 | budget | remaining/spent/avg-cost/sufficiency |
| 37–52 | history | last-5 play types + successes + rolling stats + drift |
| 53–55 | time | session / since-calibration / since-seed |
| 56–58 | PR | open / awaiting-review / approved-unmerged |
| 59–62 | health | stagnation / streak / loop-level / agents-in-error |
| 63–64 | handoff | avg context-loss / avg rampup |
| 65–67 | trajectory | projected alignment / est-plays / est-cost |
| 68–70 | learnings | count / avg-confidence / injection-rate |
| 71 | churn | issue churn over last 10 plays |
| 72–167 | per-config | 32 configs × (idle, busy, success-rate), zero-padded |
| 168–171 | PR-author | open + awaiting-review per claude_code / codex authorship |
| 172–178 | pressure | velocity, busy-agents, unreviewed, mergeable, in-flight, skip-rate, PR-pressure |
| 179–244 | specialization | 3 tiers × 22 plays success rates (0.5 default) |
| 245 | version marker | stable per-version constant |

The per-config block (slots 72–167) and the config policy head share one
deterministic config index: configured agent order outer, model-tier priority
inner, so every component (observation, mask, cold-start, selector, resolver)
sees the same indices.

## Action Space

`ACTION_SPACE_VERSION = 13`, `NUM_ACTIONS = 22` (19 active plays + 3 reserved).
The `PlayType` enum declaration order *is* the canonical action ordering; an
import-time check guards against reordering.

| Index | Play | | Index | Play |
|-------|------|-|-------|------|
| 0 | `INSTANTIATE_AGENT` | | 11 | `RECONCILE_STATE` |
| 1 | `UNBLOCK_PR` | | 12 | `REFINE_TASK_BREAKDOWN` |
| 2 | `WRITE_IMPLEMENTATION_PLAN` | | 13 | `CLEANUP` |
| 3 | `END_AGENT` | | 14 | `FUTURE_4` *(reserved)* |
| 4 | `ISSUE_PICKUP` | | 15 | `TAKE_BREAK` |
| 5 | `CODE_REVIEW` | | 16 | `GROOM_BACKLOG` |
| 6 | `MERGE_PR` | | 17 | `SEED_PROJECT` |
| 7 | `RUN_QA` | | 18 | `CALIBRATE_ALIGNMENT` |
| 8 | `SYSTEMATIC_DEBUGGING` | | 19 | `PRUNE` |
| 9 | `DESIGN_AUDIT` | | 20 | `FUTURE_7` *(reserved)* |
| 10 | `END_SESSION` | | 21 | `FUTURE_8` *(reserved)* |

`FUTURE_4`, `FUTURE_7`, and `FUTURE_8` are permanently masked reserved slots.
They hold tensor positions so a future play can be filled in place without
bumping `ACTION_SPACE_VERSION`, preserving learned weights.

## Policy Network

`POLICY_VERSION = 5`. A shared-trunk MLP (`246 → 128 → 128`, ReLU), under ~120K
parameters, CPU-only, with three heads:

- **actor** — 22 play logits.
- **value** — scalar, for GAE bootstrap.
- **config** — `num_configs` logits over `(agent_type, model_tier)` pairs.
  Conditional: sampled and trained only on steps where the play head selected
  `INSTANTIATE_AGENT`. A degenerate stub head is created when no agents are
  configured so the state-dict key set stays stable.

`POLICY_VERSION` is bumped independently of `ACTION_SPACE_VERSION` when the
config head's shape or semantics change.

## Masking

The mask is a boolean vector over the 22-action order (`True` = selectable).

The **base mask** — all validity gates (preconditions, agent eligibility,
candidate-required, instantiate-config viability, end-session/take-break gating,
wedged-`END_AGENT` re-enable) — is computed by a single `EligibilityAuthority`
in `eligibility.py`. It is the one source of truth for validity, used both to
present options to the policy and at confirm time to validate the selected play.

On top of the base mask the builder layers policy overlays only, in order:

1. **Consecutive-failure breaker** — bench a play under the 3-strikes circuit
   breaker until a cooldown elapses (option-removal only).
2. **Reserved slots** — zero `FUTURE_4` / `FUTURE_7` / `FUTURE_8`.
3. **Drain / main-repo-pause short-circuits** — when draining, force
   `END_AGENT`-only; during a trunk pause, withhold everything but `END_AGENT`
   and `RECONCILE_STATE`.
4. **Reverse failsafe** — if the composed mask is empty but open work and idle
   capacity exist, lift a constrained fallback menu.

The repeating play type is *not* force-masked: on a same-type failure streak the
stagnation entropy boost raises exploration so the policy diversifies itself.
The config head has its own mask over the config index; if no config is eligible
the authority has already excluded `INSTANTIATE_AGENT`.

## Reward

`reward.py` computes a weighted sum clipped to the configured PPO bounds
(default `[-10, 10]`). Components: issue throughput, alignment delta, cost and
time penalties, completion / stagnation / failure signals, issue-inflation and
loop penalties, anti-confirmation bonus, progress-play bonuses, per-play success
bonuses (debug, reconcile, instantiate, cleanup), a PR-pressure bonus (mirrors
the slot-178 observation feature), and multi-agent / velocity incentives. Cost
and time penalties are waived for progress plays; dispatch-only bonuses do not
apply to internal lifecycle plays.

## Cold Start

Before any training, the actor bias is seeded from log-renormalized default play
weights so that argmax on an all-zero observation with an all-true mask selects
`ISSUE_PICKUP`; the weight matrix is zeroed and de-zeroed by gradients during
training. The config head is seeded analogously from tier-only priors (medium >
large > small) — provider does not bias spawn preference; provider availability
is enforced by the config mask. Reserved slots carry a low anchor weight only to
keep the renormalization sum near 1.0; they are never selectable.

## Checkpoint Compatibility Contract

Checkpoints store `action_space_version`, `policy_version`, `observation_version`,
`obs_dim`, `num_actions`, and `num_configs`. Load raises
`IncompatibleCheckpointError` on any version mismatch (action-space, policy, or
observation) or `num_configs` disagreement — a hard reset by design. Filling a
reserved slot in place is shape-preserving and does **not** bump
`ACTION_SPACE_VERSION`, so existing learned weights keep loading.

## Policy Modes

`learning` (default) keeps stochastic masked sampling plus PPO updates and
checkpointing enabled. With `rl.policy_mode: audit-replay` (or
`agentshore start --policy-mode audit-replay`) both the play and config heads use
masked argmax, online updates are disabled, and entropy stops driving
exploration. This makes policy choices reproducible for the same weights and
observation trajectory; agent execution can still vary because LLMs, tests,
GitHub state, timing, and external services are not deterministic.

## Alignment Delta

`alignment_delta` is `float | None`:

- `None` — no beads graph existed around the play. `SEED_PROJECT` receives a
  small flat bonus in this case; other plays treat it as zero.
- `float` — change in `ProjectGraph.global_closure_ratio` after the play.

This keeps seeding useful without rewarding unrelated work for a missing graph.
