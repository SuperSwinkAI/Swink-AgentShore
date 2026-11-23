# Skills, prompts and RL loop review — November 2026

Scope: how an AgentShore run could deliver more merged progress per dollar and per hour, looking at
the skill prompts, the RL loop, and whether new action slots or skills are warranted.

Ground rule applied throughout: **the PPO policy makes every directional decision; deterministic
code backstops hard wedges and never drives.** Nothing below proposes reserving agents, forcing a
play, or masking a play because another one "should" run. The proposals fix what the policy is
told (reward, observation), what it pays for an action (prompts, flags, models), and what it is
allowed to learn from (experience bookkeeping).

## Bottom line

1. **The reward pays for keeping the fleet busy, not for merging.** With 18 agents all busy, any
   successful dispatch play earns about +6.8 from the concurrency bonus alone; the merge bonus is
   +2.5 and one closed issue in a 250-issue backlog is +0.008. A plan and a review are worth nearly
   the same. The plan-first flood on 2026-11-17 is the policy doing what this reward says.
2. **What the policy learns from is noisy.** Outcomes are credited by diffing global issue counts
   across a play's whole 10-minute-to-3-hour window, so each play is paid for what other agents
   did. One PPO importance-ratio bug is fixed on this branch.
3. **The skill prompts are in decent shape for current models** (low emphasis density, good
   end-of-prompt contract). The real defects are contradictions between the dispatch preamble and
   the skill bodies, a validator that rejects what two skills instruct, and no route for an
   oversized issue. Those are fixed on this branch.
4. **No new action slot is recommended yet.** The three reserved slots are worth holding until the
   reward is repaired; today a new play would be learned under the same activity-dominated signal.

## Evidence base

| Source | What it gave | Limits |
|---|---|---|
| Code read (skills, renderer, reward, candidates, selector) | Mechanisms cited below | — |
| Independent RL-loop review (second model) | Loop walk-through, reward table, slot checklist | Read-only; reward sizes computed from defaults |
| GitHub issue history (233 issues, 40 read in full) | Recurring failure classes and their costs | Most issues judged by title only |
| `/monitor_run` and related session transcripts | Run numbers, user decisions | Partial: one run read in depth, the 25-agent scaling test not reached |
| Vendor documentation research | Current models, flags, prompting guidance | Several pages read via summaries; flagged where unverified |

Reference run (SuperSwink-Worker, 2026-11-17, session 127dcebf): 912 plays, $651, 15 PRs merged.
About $450 and 2.75 hours passed before the first merge; 191 of 221 large-tier plays were
`write_implementation_plan` across 178 issues, 12 PRs sat unreviewed, then the open-PR cap masked
`issue_pickup`. The policy recovered on its own roughly an hour later.

## 1. What the policy is told to want

Reward terms at defaults (`rl/reward.py`, `config/models.py:291-320`):

| Term | Size | Depends on the play chosen? |
|---|---|---|
| Concurrency bonus | 0.1 × (busy − 1) × {1, 2, 4}: **+2.0 at 6 agents, +6.8 at 18** | No — any successful dispatch play |
| Velocity bonus | +0.5 | No |
| Type-diversity bonus | +0.3 | No |
| `merge_pr` / `run_qa` / pickup-or-review bonus | +2.5 / +2.0 / +0.5 | Yes |
| Anti-confirmation | +0.3 on every code review, including failed ones (`core/helpers.py:205-206`) | Constant |
| Issue throughput | 2.0 × closed ÷ open: +0.008 per close at 250 open | Yes, but tiny |
| Cost / time penalty | at most −0.5 / −0.25, waived for pickup, review, merge, QA | Weak |
| Failure | −1.0 | Yes |

Consequences:

- At fleet sizes the desktop app actually runs, 70–90% of a successful play's reward is the same
  whichever play was chosen. The learned preference between plan, review and merge rests on a small
  residue.
- The concurrency term rewards re-dispatching an idle agent immediately to anything available.
  Planning is always available on a large intake backlog; review needs an eligible PR and reviewer.
- `unblock_pr` converts blocked PRs into merges but is not a progress play: it pays the cost and
  time penalties and earns no merge credit when it merges.
- `run_qa` earns +2.0 for running, whether or not anything merged since the last run, and the
  issues it files then count toward the inflation penalty.
- `rolling_velocity` is not rolling: the window start is permanently play 1
  (`core/velocity_tracker.py:35-37`), so the bonus decays as the session lengthens.
- Dollars never enter as an absolute. Nothing measures cost per merged PR.

**Proposal R1 (reward, needs your decision).** Keep every decision with the policy and change only
the signal: remove the concurrency, diversity and velocity terms from per-play reward (they
describe fleet state, not the decision); pay absolute, attributed outcomes (per merged PR and per
bead closed by that merge, credited to the plays that touched that PR); charge dollars to every
play; give `unblock_pr` the merge credit when it merges. This rescales value estimates, so expect
a short warm-start regression and plan to regenerate `bootstrap_policy.pt`.

## 2. What the policy learns from

| # | Finding | Evidence | Proposal |
|---|---|---|---|
| L1 | Outcomes are credited by global diff over the play's window, so concurrent plays are paid for each other's closes and creates. No delayed credit reaches the pickup or review whose PR merges later. | `core/helpers.py:175-196`; `state_before` is the dispatch-time state | Attribute by the play's own target (params and artifacts); add an outcome ledger so a later merge credits the plays still in the buffer |
| L2 | Steps enter the rollout buffer in completion order, so GAE bootstraps from an unrelated play's observation. | `rl/selector.py:741`, `rl/experience.py:103-110` | Order by dispatch time, or treat plays as options and discount by elapsed time |
| L3 | **Fixed here.** After a clean re-pick the stored action mask was the full mask while the log-probability came from the narrowed one, biasing the PPO ratio below 1. | `rl/selector.py` `_PendingStep(mask=...)` | Store the mask actually sampled under |
| L4 | 16-step updates with minibatches of 4 give roughly 30 small updates in a 480-play session; advantages are normalised over 16 samples. | `config/models.py:324-370` | Larger rollouts (64+), and offline training across sessions from `rl_experience` |
| L5 | The bundled seed policy has a zero-width config head, so installs that warm-start from it never learn agent-config choice; the shared weights then inherit that. | Reviewer inspected `bootstrap_policy.pt` metadata; `rl/policy.py:302` | On load, graft a cold-start-biased config head when sizes differ |
| L6 | Observation lacks what a stage-balancing decision needs: planned-but-unimplemented count, PR age, minutes since last merge, large-tier demand versus supply, in-flight counts per play. Slots 2–7 are zero-filled and free. | `rl/observation.py` | Fill the dead slots; batch with R1 since both invalidate checkpoints |
| L7 | Large tier is the shared bottleneck: plan, review, QA, seed, design audit and calibrate are large-only. | `agents/_selection.py:57-83` | Revisit whether review of small diffs and planning of simple issues still need large tier on current models (see §4) |
| L8 | `DONE_WITH_CONCERNS` and `NEEDS_CONTEXT` from `issue_pickup` are read by nothing; a partial PR that says `Closes #N` closes the issue on merge. | No consumer in `src/` | Either consume the status or drop it from the skill |
| L9 | A successful `unblock_pr` records a review PASS at the new head, so fix commits can merge without an independent review. | `plays/skill_backed/unblock_pr.py:74-98` | Keep for pure rebase or stale-review dismissal; require review when code changed. Your call — it trades speed for the anti-confirmation invariant |

Recurring classes from issue history that single fixes have not closed: per-target re-picks with
no state-change-aware cooldown (#222, #312, #357, #479, #521); agents paid to rediscover knowable
state such as dependency blocks and umbrella issues (#14, #96, #191, #521); error text misread as
auth (#19, #190, #332, #519, #520); worktree reaped mid-play (eight issues); trunk dirtied by
scratch files (#310, #330, #344, #478). Each is a validity or bookkeeping matter, not a policy
decision, and each would benefit from one shared mechanism rather than another per-instance patch.

## 3. Skills and prompts

What is already right: emphasis is restrained (0–4 capitalised imperatives per template), the
result contract is restated at the end of the prompt where it is most likely to be honoured, and
fragile git and `bd` operations are given as exact commands.

**Fixed on this branch**

| Defect | Effect in a run | Change |
|---|---|---|
| Skill bodies read the shared `.agentshore/context.json` while the preamble warns it may belong to another play (18 references) | With many plays in flight an agent can read another play's params, issue list or review state | Renderer rewrites body references to the per-play context file |
| `issue-pickup` said "kill anything too slow" while the preamble says never kill a command still making progress | Long builds abandoned, work discarded | Sentence removed; one consistent rule |
| `issue-pickup` named `manage_task`, an Antigravity-only tool, to every vendor | Noise for five agent types | Removed; the Antigravity-specific directive already covers it |
| `write-plan` and `systematic-debugging` instruct filing a prerequisite or newly found bug, but the inflation guard allowed 0 | The whole play is rejected after a full large-tier turn (two seen on 2026-11-17) | Allowance of 1 for both plays |
| No route for an issue too large for one PR; review hard-blocks diffs over 2500 lines and nothing can shrink them | Oversized PRs become permanent queue occupants | `issue-pickup` now stops and requests `agentshore/needs-refinement` |
| `write-plan` always produced the full per-task TDD checklist | A long large-tier turn per issue, including issues that need no plan | Plan depth now matches the issue; simple issues get a short plan |

**Proposed, not done**

- **P1 Schema-enforced results.** Claude Code, Codex and Antigravity document a JSON-schema flag
  for the final message. Roughly 8% of plays once failed on a missing or malformed result block
  (#223), and the retry machinery in `plays/skill_backed/base.py` exists for that. Needs a spike:
  Claude's flag is documented with `--output-format json`, and AgentShore uses `stream-json`.
- **P2 Shrink `reconcile-state` (26 KB) and `groom-backlog` (22 KB).** Both carry incident history
  and stacked exceptions. Move rarely-needed branches to `references/` files the agent opens on
  demand, which the installer already supports.
- **P3 Give reasons, keep "never" for true invariants.** Current Claude and GPT guidance says the
  newest models treat "never" as absolute and stop early on strong boundary language. The
  forbidden lists carry no reasons.
- **P4 Stable prompt prefix.** The prompt opens with `$ARGUMENTS: <n>` and a per-play path, so no
  two dispatches share a cacheable prefix. Put static discipline and the skill body first and the
  per-play values last.
- **P5 Stop double discovery.** Codex, Copilot, Grok and Antigravity all scan `.agents/skills`, so
  the 15 templates load as skill metadata in addition to being inlined.
- **P6 Scope `run-qa` to what changed.** It re-audits the full branch each run and may file eight
  issues each time, which feeds the backlog the planning flood then works through.

## 4. Models and dispatch flags

- **Catalog is behind.** `claude-fable-5-2` is absent from `models.yaml` and `pricing.yaml`; Codex
  defaults are `gpt-5.6-*` while OpenAI's current list is GPT-6.x; all three Grok tiers map to
  `grok-4.5`; Antigravity's large tier is a legacy Gemini 3.1 row. Live discovery softens this, but
  cost accounting uses the price table. Verify ids and prices against each vendor before editing.
- **Claude capability gaps in the argv builder.** `agents/cli/argv.py` states Claude has no
  tool-deny flag and no pinnable session id; both exist. Today the code-review write denial is
  advisory on Claude.
- **Effort is per tier, never per play.** Vendor guidance places early stops and skipped
  verification at low and medium effort. Consider effort as a per-play property for
  `issue_pickup` and `unblock_pr`.
- **Unused backstops.** No dispatch sets a turn or budget cap; Claude Code offers both.
- **In-dispatch subagents** are disabled only for Grok. Decide deliberately for the others, since
  their spend lands in the play's cost.

## 5. New action slots and skills

Filling a reserved slot in place keeps `ACTION_SPACE_VERSION` and existing weights valid; the slot
starts at its low cold-start prior. A 23rd slot or any new observation feature resets every
checkpoint and the bundled seed. The touchpoint list is about 17 places (enum, registry, resolver,
candidates, tier routing, reward sets, cold-start weight, template, dashboard constants, docs,
tests).

| Candidate | Verdict | Why |
|---|---|---|
| `address_review` | Fold into `unblock_pr` | It already handles requested changes; the gap is that an AgentShore BLOCK verdict only reaches it when posted as a GitHub change request |
| `fix_trunk` (red default branch) | **Hold a slot** | A genuinely new directional option: nothing today lets the policy respond to a broken trunk except indirectly through QA-filed issues |
| `fanout_pickup` (one dispatch, several small independent ready tasks via the CLI's own subagents) | **Hold a slot**, decide after R1 | Raises throughput past the one-agent-one-issue ceiling; needs multi-issue claims and attribution first |
| `verify_and_close` | Fold into merge reconcile and QA | A dispatch per merge is the opposite of the goal |
| `split_issue` | Covered | `refine_tasks`, now reachable from `issue_pickup` |
| `rebase_stack` / merge queue | Use GitHub's | Mechanical |
| `research_spike`, `retrospective`, `escalate_to_human` | Skip | Not convertible to merged progress, or already handled by learnings and human-park labels |

Recommendation: add no play now. Repair the signal (R1, L1), measure one run, then decide between
`fix_trunk` and `fanout_pickup` with data.

## 6. Suggested order

1. Land this branch's fixes.
2. Decide R1. It is the largest lever and the one most consistent with keeping the policy in
   charge; L1 and L6 belong in the same checkpoint-resetting change.
3. Refresh the model catalog and the Claude argv gaps (§4).
4. Spike schema-enforced results (P1).
5. Revisit new plays with one post-R1 run as the baseline.
