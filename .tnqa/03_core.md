## Bucket 03: core/ (phases, mixins, loop)

Scope: all 18 `.py` files under `src/agentshore/core/`. ~8,600 LOC. Three files
exceed 1000 lines (`mixins/completion.py` 1371, `phases.py` 1053, `mixins/loop.py`
1029). The core engine is a 7-mixin god-object split across files over a shared
`_OrchestratorBase`. This review is biased toward structural deletion ("code
judo") over rearrangement.

---

### Executive verdict on the mixin architecture

`Orchestrator` is **one class with ~44 mutable instance attributes and ~50
methods, physically sharded into 7 mixin files** purely to satisfy a per-file LOC
budget (`orchestrator.py:9` says so explicitly: "Behavioural code lives in the
mixins so each file stays under the LOC budget"). This is the single most
important finding in the bucket: the mixins are **not** independent
collaborators. Every mixin subclasses `_OrchestratorBase`, re-declares the same
`self._*` attributes for mypy, and freely calls sibling-mixin methods through
implicit MRO dispatch. There is no encapsulation boundary — `_LoopMixin` reaches
into `_DispatchMixin._consume_override`, `_CompletionMixin._harvest_completed`,
`_LifecycleMixin._should_terminate`, `_StateMixin._build_state`, and base
accessors, all via `self`. The split makes state flow *harder* to trace than a
single 3,000-line file would, because the reader must hold the whole attribute
surface in their head while jumping files, and `base.py`'s ~50 `raise
NotImplementedError` stubs (base.py:479-692) are pure boilerplate that exists only
to make the implicit cross-mixin contract type-check.

The honest refactor is **composition**: extract genuine collaborators that own
their own state (a `GitHubSyncer`, a `MainRepoGuard`, an `OverrideQueue`, a
`SelectionGate`/`IdleBackoff` state machine) and have the loop call them with
explicit arguments instead of sharing 44 fields through `self`. That deletes
`base.py`'s stub wall and the per-mixin attribute re-declarations entirely.
Individual high-conviction extractions are itemized below.

---

## Critical

### C1. `_process_completion` is an 8-step sequential pipeline of side-effecting `self`-mutators with no atomicity and tangled ordering
**Severity:** Critical
**Location:** `mixins/completion.py:304-331` (and the 9 helpers it drives, 304-870)

**Problem:** `_process_completion` calls eight helpers in a fixed order
(`_pop_completed_dispatch` → `_check_main_repo_after_completion` →
`_handle_skipped_completion` → `_record_completion_bookkeeping` →
`_schedule_retry_if_requested` → `_record_unblock_attempt_if_needed` →
`_run_completion_control_checks` → `_record_completion_experience` →
`_publish_completion_results` → `_handle_end_session_completion`). Each helper
mutates orchestrator state (`_last_play_id`, `_recent_play_completions`,
`_recent_applied_labels`, `_velocity_events`, `_break_recovery_failures`,
`_stop_requested`, `_natural_exit_reason`, the override queue, …) and several
early-return out of the whole pipeline (`_handle_skipped_completion` returns True
to abort; `_schedule_retry_if_requested` returns True to abort). The control flow
is a hand-rolled chain-of-responsibility where the ordering invariants are only
documented in prose comments. `_run_completion_control_checks` *itself* re-runs
`_build_state` + `_should_terminate` + `_begin_budget_reserve_drain` +
forward-progress + stagnation + feedback-cadence — duplicating logic that
`_run_loop_body` already runs every tick (loop.py:874-891). So termination is
evaluated twice per completed play, once here and once on the next loop iteration,
against two separately-rebuilt `OrchestratorState` snapshots.

**Code-judo remedy:** This pipeline is begging to be a typed `CompletionResult`
dataclass + a thin sequencer. (1) Make each step return a small typed verdict
(`Skip | Retry | Continue`) instead of a bare `bool`/early-return, and let one
`match` statement in `_process_completion` drive control flow — removes the
"return True means abort" convention scattered across 3 helpers. (2) Collapse the
**duplicated** terminate/budget/stagnation evaluation: `_run_loop_body` already
does it next tick; the completion-path copy (completion.py:633-655) exists only to
react faster, but it rebuilds state a second time. Have completion record the
outcome and set a `_needs_terminate_check` flag the loop consumes, deleting
`_run_completion_control_checks`'s duplicate `_should_terminate`/`_pause_with_reason`
block (~20 lines and one redundant `_build_state`). (3) `_publish_completion_results`
calls `_build_state` a **third** time (completion.py:831) just to publish a post
snapshot — fold into the single state already built. Net: ~3 `_build_state` calls
per completion collapse to 1, and the abort-convention spaghetti becomes a typed
dispatch.

### C2. `_OrchestratorBase` is a 44-field god-object state bag with a ~50-method NotImplementedError stub wall
**Severity:** Critical
**Location:** `base.py:69-692` (attributes 78-225; stub methods 479-692)

**Problem:** Every piece of orchestrator state lives on one base class, and every
cross-mixin call is declared as a `raise NotImplementedError` stub so mypy can see
it. base.py:479-692 is ~210 lines of pure type-checker appeasement — 50 method
signatures that are never executed (they exist solely because the mixins call each
other through `self`). The attribute block (78-225) mixes genuinely-shared loop
state (`_in_flight`, `_pause_event`) with single-consumer fields that have no
business being on a shared base: `_break_recovery_failures` (read/written only by
`_CompletionMixin` + `_StateMixin`), `_rate_limit_recovery_enqueued`
(`_CompletionMixin` only), `_idle_agent_claim_ticks` (`_StateMixin` only),
`_recent_applied_labels` (`_CompletionMixin` write, `_StateMixin` read),
`_velocity_events`/`_recent_agent_types` (velocity calc only),
`_pre_play_branches`/`_default_branch`/`_main_repo_dispatch_paused` (main-repo
guard only). The comment thicket (every field carries a paragraph of `desktop-XXX`
history) signals how much accidental coupling has accreted here.

**Code-judo remedy:** Extract collaborator objects that own their own state, then
delete both the corresponding base attributes *and* the NotImplementedError stubs
for their methods:
- `MainRepoGuard(repo_root, default_branch)` owning `_pre_play_branches`,
  `_default_branch`, `_main_repo_dispatch_paused`, and the
  `_check_main_repo_invariant` / pre-play-snapshot / reconcile-clear logic
  (currently smeared across dispatch.py:730-747, completion.py:231-302, 358-404).
  Deletes ~3 base fields + 1 stub.
- `VelocityTracker` owning `_velocity_events`, `_velocity_window_start_play_id`,
  `_recent_agent_types`, `_compute_rolling_velocity`,
  `_executor_skip_window`/`_record_selection_repicks`. Deletes ~5 base fields + the
  base accessor methods (base.py:427-468).
- `RecoveryTracker` owning `_break_recovery_failures`,
  `_rate_limit_recovery_enqueued`, and `_handle_take_break_outcome` /
  `_maybe_enqueue_rate_limit_recovery`.
Each extraction removes 3-5 fields from the base and its matching stubs. Target:
shrink `base.py` from 692 to <250 lines and the stub wall from 50 methods to ~10
genuine lifecycle hooks.

---

## High

### H1. `_refresh_issues` is a 155-line GitHub-sync monolith embedded in `_CompletionMixin`
**Severity:** High
**Location:** `mixins/completion.py:1134-1300` (+ `_ensure_ssh_key_fresh`,
`_mark_worktrees_stale_for_closed_prs`, `_sweep_closed_pr_worktrees` 1291-1371)

**Problem:** `_refresh_issues` does five unrelated jobs in one function: (a)
issue full/incremental sync mode selection, (b) issue cache write, (c)
duplicate-bead close sweep (an entire beads graph walk, 1210-1245), (d) PR
open-list fetch + "missing PR" resync, (e) worktree-stale marking + TTL reap +
SSH-key refresh in the `finally`. It duplicates almost verbatim the GitHub fetch
logic already in `phases._phase_fetch_github` (phases.py:722-819) — both construct
a `GitHubAdapter`, probe, `list_issues`, `cache_github_issues`,
`set_last_issue_sync_at`, `list_pull_requests`, `filter_trusted_pull_requests`,
`cache_pull_requests`. There are two copies of the GitHub-cache write path.

**Code-judo remedy:** Extract a `GitHubSyncer` collaborator (constructed once in
bootstrap, holding the adapter + store + cfg) with `sync_session_start()` and
`refresh(completing_play, force_full_sync)` methods. Both `_phase_fetch_github`
and `_refresh_issues` become 3-line delegations. The duplicate-bead sweep and the
worktree-stale/TTL logic move onto the syncer (or a `WorktreeReaper` the syncer
calls). Removes ~150 lines of duplicated fetch/cache plumbing and pulls a major
non-completion responsibility out of `_CompletionMixin`. This also kills the
"declared module-level so renaming doesn't drift them away from the constants the
original monolithic core.py referenced" `_PR_LIMIT`/`_DUPLICATE_BEAD_TITLE_RE`
note (completion.py:92-95) — clear evidence this code was lifted wholesale from a
dead monolith and never restructured.

### H2. `run_until_idle` / `_run_loop_body` re-implements pause/idle/select/dispatch as a 220-line straight-line procedure with 6 distinct exit conditions
**Severity:** High
**Location:** `mixins/loop.py:722-1029`

**Problem:** `_run_loop_body` (809-1029) is a 220-line single method with a deeply
nested decision tree: pause-gate-with-deadline (820-853), stop check, drain-init,
harvest, periodic refresh, build state, budget drain, terminate check, pause
check, digest gate (908-922), override consume, select, repick record, selector-None
branch (with two sub-branches for in-flight vs idle), idle-streak reset, the
INSTANTIATE-under-pressure log, shutdown-only-END_AGENT rewrite (984-998),
end-session revalidation, dispatch, post-dispatch wait. It returns `bool`
(should-break) with at least 8 distinct `return True/False` sites whose meaning
depends on local context. The "digest gate" path (910-920) and the "selector
returned None" path (933-949) are near-identical (`_idle_streak += 1`; if in_flight
wait else `_continue_if_selector_idle_work_remains`), differing only in the log
line.

**Code-judo remedy:** Model the tick as an explicit small state machine /
typed `TickAction` (`Dispatch(play) | WaitInFlight | WaitIdle | Break(reason) |
Continue`). Split into: `_resolve_tick() -> TickAction` (pure-ish: pause/harvest/
build/terminate/select) and `_apply_tick_action(action)` (the wait/dispatch/break
side effects). The two duplicate idle paths (910-920 and 933-949) collapse into a
single `WaitIdle` action. The shutdown-only-END_AGENT rewrite (984-998) and the
end-session revalidation (999-1006) are mask/eligibility concerns that belong in
`_consume_override`/eligibility, not bolted into the dispatch flow. Estimated
~60-line reduction and one nesting level removed.

### H3. Three overlapping "the loop is wedged/stalled, stop it" mechanisms duplicate skip-classification and drain-trigger logic
**Severity:** High
**Location:** `mixins/loop.py` — `_continue_if_selector_idle_work_remains`
(444-573), `_auto_stop_unanswered_pause` (611-639), `_loop_liveness_watchdog`
(670-720), `_handle_tick_failure` (770-807); plus `ForwardProgressMonitor`
(progress_monitor.py) and the wedge auto-stop (472-489)

**Problem:** There are now **five** independent autonomous-stop paths: (1)
forward-progress monitor (the documented "single autonomous-stop signal" per
progress_monitor.py:1), (2) wedged-trunk-pause auto-stop (`_wedged_idle_ticks`,
loop.py:472-489), (3) unanswered-feedback-pause auto-stop (loop.py:611-639), (4)
loop-liveness watchdog (loop.py:670-720), (5) tick-failure circuit breaker
(loop.py:770-807). Each sets `_draining`/`_drain_reason` and (mostly) calls
`begin_drain` with its own bespoke logging. Meanwhile mask-reason computation +
skip classification is duplicated three times:
`_emit_structured_play_skipped_for_current_tick` (308-349),
`_continue_if_selector_idle_work_remains` (491-525), and the inline block — all
build a candidate plan, run `compute_mask_reasons`, `Counter(...).most_common(5)`,
and `_classify_play_skipped_reason`.

**Code-judo remedy:** (a) Extract the mask-reason/skip-classification triple into
one `_compute_skip_diagnosis(state) -> SkipDiagnosis` helper and call it from all
three sites — removes ~40 duplicated lines. (b) The five stop paths share one
shape: "detect condition → log → set drain reason → begin_drain". The progress
monitor doc claims to *replace* the streak/spin detectors; finish that
consolidation by routing all autonomous stops through a single
`_initiate_autonomous_stop(reason)` that does the drain-flag set + `begin_drain` +
`_natural_exit_reason`. The wedged-trunk and unanswered-pause variants differ only
in their trigger condition, not their action.

### H4. Override-mask handling is a 4-branch classification ladder duplicated across consume / handle / release with string-fallback type-sniffing
**Severity:** High
**Location:** `mixins/dispatch.py:159-437` (`_consume_override`,
`_mask_reason_is_transient`, `_mask_reason_is_indefinite_wait`,
`_handle_masked_override`, `_release_masked_override`)

**Problem:** The override pipeline reproduces eligibility logic the
`EligibilityAuthority` already owns: `_consume_override` constructs an
`EligibilityAuthority` and calls `confirm` (253), but *also* has the
`wait_for_play_type` gate (213-231), the shutdown-only filter (198-205), and the
first-play-override special case (175-192) layered on top. `_mask_reason_is_transient`
and `_mask_reason_is_indefinite_wait` each branch on `isinstance(reason,
MaskReason)` then fall back to **substring-sniffing a raw string** ("no idle",
"rate_limit", "cooldown", "waiting for", …) for "the remaining legacy emission
sites yet to be migrated" — an admitted half-finished migration. `_handle_masked_override`
is a 4-case ladder (BOOTSTRAP never drops / INDEFINITE_WAIT requeue / TRANSIENT
bounded requeue / else release) that re-derives classification the MaskReason
already carries as `.classification`.

**Code-judo remedy:** (1) Delete the string-fallback branches — make
`MaskReason.classification` the single source of truth and require callers to pass
typed reasons (the eligibility refactor already produces them). Removes both
`_mask_reason_is_*` string-sniff bodies (~25 lines) and the `MaskReason | str`
union from ~8 signatures. (2) Pull `_consume_override` + `_handle_masked_override`
+ `_release_masked_override` + the queue into an `OverrideQueue` collaborator that
owns `_override_queue`, `_first_play_override`, `_pending_override_kind`,
`_override_dispatched_play_ids` (all currently base fields). The loop asks it for
the next eligible override given a state; it internally handles requeue/drop. This
removes 4 fields from `base.py` and isolates the requeue taxonomy in one testable
unit.

### H5. `_dispatch_play` is a 280-line method mixing 5 pre-flight rejection gates, worktree allocation, snapshotting, and task creation
**Severity:** High
**Location:** `mixins/dispatch.py:493-775`

**Problem:** Despite the docstring claiming the method is "now purely
side-effecting" after the eligibility refactor, it still performs five distinct
pre-dispatch *rejections* (main-repo-paused 531-541, end-session-in-flight
542-556, shutdown-only 557-564, backslash-space path 565-595, worktree-manager-
unavailable 609-622), each calling `_drop_selected_play_before_dispatch` with a
bespoke reason/event string. These are validity checks the method's own docstring
says were moved upstream to the `EligibilityAuthority` — yet here they are,
re-litigating eligibility at dispatch time. The `revalidate` parameter is
accepted-and-ignored (`del revalidate`, 523) and threaded through callers
(loop.py:1007-1014 computes `should_revalidate` only to have it discarded) — dead
plumbing.

**Code-judo remedy:** (1) Delete the `revalidate` parameter end-to-end:
`_dispatch_play` ignores it, so loop.py:1007-1014's `should_revalidate`
computation and the base stub's `revalidate` arg are all dead — remove ~10 lines
and one `isinstance(self._selector, _ppo_selector_cls())` call per tick. (2) The
five rejection gates are eligibility predicates; move main-repo-paused,
shutdown-only, and end-session-in-flight into the same authority/mask path that
already gates PPO (the docstring asserts this is where they belong). What remains
of `_dispatch_play` is: allocate worktree, stamp params, snapshot active_play +
pre-play ref, create task. The backslash-space check belongs on the
`WorktreeManager` allocation path, not the dispatcher. Estimated reduction from
280 to ~120 lines.

---

## Medium

### M1. `phases.py` interleaves two unrelated concerns: bootstrap sequencing and GitHub/git/beads I/O bodies
**Severity:** Medium
**Location:** `phases.py:1-1054`

**Problem:** The file mixes thin orchestration phases (`_phase_init_datastore`,
`_phase_init_metrics` — a few lines each) with heavyweight I/O bodies inlined:
`_phase_git_safety_sweep` (449-575, 126 lines doing gitignore + ssh + default-
branch + branch-restore + escape-scan), `_phase_fetch_github` (722-819, the
duplicate of `_refresh_issues` per H1), `_phase_queue_agent_instantiation`
(904-1054, 150 lines with a nested `_first_enabled_for_tier` and two near-identical
INSTANTIATE_AGENT-override-enqueue blocks for seed vs no-seed). The
`_resolve_policy_path`/`_resolve_seed_path` resolvers don't belong with bootstrap
phases at all.

**Code-judo remedy:** (1) The two override-enqueue blocks in
`_phase_queue_agent_instantiation` (954-986 no-seed, 988-1048 seed) share the
large-agent spawn; factor a `_enqueue_instantiate(orch, agent_type, tier,
wait_for=None)` helper and the function halves. (2) `_phase_git_safety_sweep`'s
five sub-steps each already have a pure helper in `git_safety.py`; the phase is
just sequencing + logging — fine, but move the gitignore commit cluster
(474-493) into `git_safety` as one `ensure_and_commit_gitignore()` so the phase
stops orchestrating three separate git helpers inline. (3) Move `_resolve_*_path`
to a `bootstrap_resolvers.py` or into the config layer.

### M2. `_fetch_state_data` splits a gather into two coroutines solely to dodge mypy's 5-arg overload, with a prose apology
**Severity:** Medium
**Location:** `mixins/state.py:150-248`

**Problem:** `_fetch_state_data` runs 10 independent DB reads but is forced into
`_fetch_group1`/`_fetch_group2` (each ≤5 args) inside an outer 2-way gather
"because `asyncio.gather` stubs only carry typed overloads for up to five
arguments" (state.py:158-160). This is the type-checker dictating runtime
structure. The nested-tuple unpacking (197-212) is hard to read and brittle to
reorder.

**Code-judo remedy:** Replace the two artificial groups with a single explicit
list of awaitables gathered via `asyncio.gather(*coros)` (typed as
`list[object]` then narrowed at the assignment site), or define one
`@dataclass _RawReads` and assign fields from indexed results. Either removes the
two wrapper coroutines and the contrived grouping (~25 lines) while preserving
full parallelism.

### M3. Test-patchability indirection (`_LoggerProxy`, `_ppo_selector_cls`, `_core_pkg` re-export wall) imposes runtime cost and a 50-name re-export surface on every call site
**Severity:** Medium
**Location:** `helpers.py:40-112` (`_LoggerProxy`, `_ppo_selector_cls`),
`__init__.py:18-128` (the re-export `__all__`), and the `from agentshore import
core as _core_pkg` dance at orchestrator.py:113, phases.py:90/123/631,
drain.py:302, loop.py:1023

**Problem:** To let tests `patch("agentshore.core.X")` against a *former* monolith
layout, the package re-exports ~50 private symbols (`__init__.py`), every
`isinstance(self._selector, _ppo_selector_cls())` call walks a package attribute
indirection (helpers.py:96-112) on a hot path executed per tick, and `_logger`
is a `__getattr__`-proxy that re-resolves `agentshore.core._logger` on **every
log call** (helpers.py:59-68). This is production complexity paying for a test
ergonomic that no longer needs the monolith shape.

**Code-judo remedy:** This is legacy-compat scaffolding the project's own
"no legacy refs / no backward-compat code" stance (per user memory) would reject.
Migrate the tests to patch the real symbols where they live
(`agentshore.rl.selector.PPOSelector`, module-level `_logger`), then delete:
`_LoggerProxy` (use a plain module `_logger`), `_ppo_selector_cls` indirection
(import `PPOSelector` directly or keep one lazy import for torch-avoidance without
the package-attribute walk), and most of `__init__.py`'s re-export `__all__`.
Removes ~80 lines and a per-tick attribute walk. (Scope note: this is a
cross-cutting test-coupling change; sequence it after the structural extractions.)

### M4. `_compute_session_stats` duplicates the per-type aggregation body for the PlayType-enum vs raw-string cases
**Severity:** Medium
**Location:** `mixins/snapshots.py:510-546`

**Problem:** The loop building `PlayTypeStatsSnapshot` rows has the **same**
`PlayTypeStatsSnapshot(...)` construction written twice — once inside the
`with suppress(ValueError)` enum branch (518-531) and once in the fallback raw-
string branch (534-546) — differing only in whether `play_type` is the enum or
the raw string.

**Code-judo remedy:** Resolve `play_type` once
(`try: PlayType(raw) except ValueError: raw`), then build the row a single time.
Deletes one ~13-line copy.

### M5. `_should_terminate` embeds a "drain completed with mergeable PRs" forensic log inside a pure predicate
**Severity:** Medium
**Location:** `mixins/lifecycle.py:70-113`

**Problem:** `_should_terminate` is named and used as a pure
`state -> (bool, reason)` predicate (called every tick and in completion), but the
DRAINING branch (74-98) does a `build_candidate_plan` + `mergeable_pr_count`
computation and emits a `drain_complete_with_mergeable_prs` ERROR as a side effect.
A predicate that builds a candidate plan and logs is neither cheap nor pure, and
it runs on the hot path.

**Code-judo remedy:** Move the forensic check to the single drain-completion site
(where `drain_complete` is acted on) rather than recomputing it inside the
per-tick predicate. Keeps `_should_terminate` a cheap pure function.

---

## Low

### L1. `_handle_skipped_completion` re-maps `skip_category` → `reason` with a 5-branch if/elif that exists in two forms
**Severity:** Low
**Location:** `mixins/completion.py:406-433` vs `mixins/loop.py:235-263`
**Problem:** The executor-time skip-category→PlaySkipReason mapping
(completion.py:417-424) and the loop-time classification
(`_classify_play_skipped_reason`, loop.py) are two encodings of the same
"why was nothing dispatched" taxonomy that desktop-85ex explicitly tried to unify.
**Remedy:** One `skip_category_to_reason(category) -> PlaySkipReason` mapping table
shared by both sites.

### L2. `adjust_budget` returns a 4-condition boolean expression mixing `getattr` defensiveness with direct field access
**Severity:** Low
**Location:** `mixins/drain.py:144-159`
**Problem:** The `return` expression (150-156) mixes `getattr(self, "_draining",
False)` with direct `self._pause_event.is_set()` for fields all guaranteed by
`__init__` — the `getattr` defensiveness is for the `Orchestrator.__new__` test
path and papers over the fact that base attributes aren't reliably initialized in
that path. Same pattern recurs (completion.py:1101, loop.py:119-122).
**Remedy:** Once C2 extracts state into collaborators with real constructors, the
`__new__`-bypass test path disappears and these `getattr(self, "_x", default)`
guards can become plain attribute access.

### L3. `_merge_recent_completions` / `_merge_recent_applied_labels` are two near-identical WAL-lag shadow-merge helpers
**Severity:** Low
**Location:** `mixins/state.py:47-123`
**Problem:** Both exist to paper over SQLite WAL-flush visibility lag by overlaying
an in-memory deque onto a DB read, with parallel dedup-by-key logic. Two shadows
(`_recent_play_completions`, `_recent_applied_labels`) plus two merge functions.
**Remedy:** Not a pure dedup (different key/merge semantics), so leave merged
separately — but the deeper fix is to make the DB read consistent (read-your-writes
via the same connection / a synchronous post-write checkpoint) and delete *both*
shadows + merges. Flagged as a smell: production logic compensating for a storage
consistency gap rather than fixing it.

### L4. `_persist_alignment_scores` is a documented no-op kept as an async method + base stub
**Severity:** Low
**Location:** `mixins/completion.py:1013-1015`, called at 736-738, base stub 603
**Problem:** Pure no-op ("No-op in v0.10.0") still wired through `_safe_call`, a
`CALIBRATE_ALIGNMENT`-success branch, and a NotImplementedError stub.
**Remedy:** Delete the method, the call site branch (completion.py:735-738), and
the stub. If CALIBRATE_ALIGNMENT no longer persists anything, the special-case
goes too.

### L5. `_forced_mask_play_types` is retained-but-always-empty dead state threaded through state assembly + IPC
**Severity:** Low
**Location:** `base.py:308-312` (`= ()` always), `mixins/state.py:142,453`
**Problem:** Comment at base.py:308 says "always empty... Kept so `_assemble_state`
and the IPC serializer have a stable field." It's plumbed into `OrchestratorState.
forced_mask_zeros` purely for wire-compat with a removed feature.
**Remedy:** If nothing populates it, drop the field from base + the state
assembly; bump the state/IPC version (the project already versions aggressively at
schema v13 / obs v13).
