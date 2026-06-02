## Bucket 01: plays/executor.py + plays/candidates.py

Thermo-nuclear maintainability/abstraction audit. Scope: `src/agentshore/plays/executor.py` (1546 lines) and `src/agentshore/plays/candidates.py` (1364 lines). `base.py` and `registry.py` read for context only.

Both files are well over the 1000-line smell line and both contain *literal* duplication of logic that lives elsewhere (or, in candidates.py, twice inside the same file). The single biggest win is deleting whole functions, not rearranging them.

---

## Critical

### C1. `candidates.py` ships a dead, duplicated free-function layer (~110 lines deletable)
**Location:** `candidates.py:791-908` (the "Backward-compatible free functions" block) plus `_base_issue_available` at `1286-1303`.

**Problem:** `PlayCandidateAnalyzer` (lines 338-788) already implements `issue_available_for_plan`, `issue_available_for_pickup`, `issue_available_for_refine`, `issue_available_for_debug`, `beads_groom_needed`, and `_base_issue_available` as methods. Lines 796-908 + 1286-1303 reimplement *every one of them again* as module-level free functions with hand-threaded keyword args (`open_pr_issue_numbers=`, `merged_pr_issue_numbers=`, `in_flight_issue_numbers=`, `bead_in_progress_issue_numbers=`, …). This is exactly the "loose functions with repeated keyword-argument threading" the analyzer's own docstring (lines 338-343) claims to have replaced — yet both layers coexist. I grepped the whole tree: `issue_available_for_plan`, `issue_available_for_pickup`, `issue_available_for_refine`, `issue_available_for_debug`, `beads_groom_needed`, and `_base_issue_available` have **zero non-comment callers** outside this file. They are dead duplication that must be kept in lockstep by hand with the analyzer methods (note the divergence risk: the free `issue_available_for_pickup` doesn't apply the `_base_issue_available` body, while the method version routes plan/refine/debug through it — the two filter families can silently drift, which the in-file comments at 478-489 explicitly worry about).

**Code-judo remedy:** Delete `issue_available_for_plan` (796-818), `issue_available_for_pickup` (821-841), `issue_available_for_refine` (844-863), `issue_available_for_debug` (866-886), `beads_groom_needed` (889-908), and `_base_issue_available` (1286-1303) outright. The `PlayCandidateAnalyzer` methods are the single source of truth. Removes ~110 lines and an entire class of "keep-in-sync-by-hand" maintenance hazard. (The `noqa: B009 getattr(...)` smell in the analyzer body is a separate nit; not blocking.)

### C2. Audit-freshness free functions are thin per-call analyzer allocations; the whole shim chain should collapse
**Location:** `candidates.py:911-937` (`seed_audit_is_fresh`, `design_audit_is_fresh`, `terminal_audits_are_fresh`, `qa_ran_within_terminal_window`, `build_candidate_plan`) plus the pure re-export module `src/agentshore/work_availability.py`.

**Problem:** Each of these four free functions does `return PlayCandidateAnalyzer(state).<method>(...)` — constructing a *full analyzer* (which eagerly computes open-issue/PR sets, graph task lists, backlog-sync candidates, etc. in `__init__`, lines 345-424) just to read one cooldown counter off `state`. That's a real per-call cost on the RL mask path: `rl/mask.py` calls `terminal_audits_are_fresh(state)` and `qa_ran_within_terminal_window(state, …)` (mask.py:178, 267, 268) every mask build. On top of that, `work_availability.py` exists *only* to re-export these same four names plus `WorkAvailability` and a one-line `summarize_work_availability`. Two indirection layers (free fn → analyzer; re-export module → free fn) wrap what are fundamentally three-line reads of `state.last_play_success_by_type` / `state.plays_since_last_play_type`.

**Code-judo remedy:** The freshness predicates don't depend on any of the precomputed analyzer sets — they only read `state`. Make them genuine module-level functions over `state` (or `@staticmethod`s that don't allocate the analyzer), so callers stop building a throwaway analyzer per mask tick. Then delete the `work_availability.py` re-export shim and point its 2-3 importers (`rl/mask.py`, ESR/IPC consumers) directly at `candidates`. Removes one module and the per-call allocation; ~15-25 lines plus a measurable hot-path win on mask builds.

---

## High

### H1. `_wire_deferrals` is a 170-line method with a 130-line nested PR-authoring block — wrong layer, untestable
**Location:** `executor.py:930-1099` (the `pull_request`/`pr` artifact branch runs 954-1090).

**Problem:** A single `for artifact in outcome.artifacts` loop carries: handoff recording, PR-number-from-URL regex extraction, branch-leak warning logging, branch-exposure recording, author handle/type/login resolution with a KeyError fallback, `record_pull_request`, a *second* GitHub round-trip (`fetch_pull_request_by_number`) that re-does base-retargeting (duplicating `_maybe_retarget_pr_base`'s logic at 1046-1076 vs 1157-1193), `enqueue_review`, and author-label application. This is six distinct responsibilities nested 4-5 indents deep inside one loop inside one method. It can't be unit-tested in pieces, the happy path is invisible, and the create-time base retarget (1046-1076) is a near-verbatim copy of `_maybe_retarget_pr_base` (1157-1193) — two retarget implementations that must agree.

**Code-judo remedy:** Extract a `_PRArtifactRecorder` helper (or three methods: `_record_pr_authorship(artifact, params, outcome) -> int|None`, `_enrich_and_retarget_pr(pr_number, author_*)`, `_enqueue_and_label_pr(pr_number, author_type)`), and fold both retarget bodies into one `_retarget_pr_to_target(pr_number, current_base) -> bool`. Drop the commit branch into `_record_commit_artifact`. Reduces `_wire_deferrals` to a ~25-line dispatcher over artifact type and removes one of the two duplicate retarget implementations (~30 lines + the divergence hazard).

### H2. Stringly-typed error classification: three overlapping marker-substring tables + a substring-matching category inferer
**Location:** `executor.py:93-117` (`_POLICY_DISALLOWED_ERROR_MARKERS`, `_AUTH_ERROR_MARKERS`, `_PR_PUBLISH_ERROR_MARKERS`) and `executor.py:1443-1476` (`_infer_failure_category`).

**Problem:** Failure semantics are reverse-engineered from `outcome.error.lower()` via `any(marker in error …)` substring scans across four separate constant tuples. `_infer_failure_category` is a 33-line ladder of `if any(kw in error for kw in (...))` that maps free-text error strings back into a `FailureCategory` the plays *already knew* when they failed. This is the canonical "we threw away the type at the boundary and now reconstruct it by grepping prose" anti-pattern: a reworded skill error silently reclassifies a play (e.g. PPO reward filtering, dashboard styling, and ESR rollups all key off `failure_category`). `_AUTH_ERROR_MARKERS` is even spread into `_PR_PUBLISH_ERROR_MARKERS` via splat (116) and re-scanned independently in `_reconcile_issue_pickup_publish` (1212, 1264) — three call sites, one fragile table.

**Code-judo remedy:** Give `PlayOutcome` a typed `failure_kind: FailureKind | None` (enum: AUTH, TEST, GATE, SCOPE, AGENT_ERROR, CODE_ERROR) that plays/agents set at the point of failure, where the cause is actually known. `_infer_failure_category` collapses to a 1-line enum→string map (fallback to the substring inferer only for legacy/uncaught `Exception` paths). The auth/publish substring tables shrink to one `_AUTH_ERROR_MARKERS` used solely by the genuinely-untyped reconcile path. Removes the 33-line ladder and two of the three marker tuples; eliminates the "reword the error, change the reward" hazard.

### H3. Method-presence `getattr(...callable...)` duck-typing on the `DataStore` it owns
**Location:** `executor.py:726-747` (`_start_claim_group` / `_finish_claim_group`), echoed at `1152-1154` (`add_issue_labels`).

**Problem:** The executor holds a concrete `DataStore` (`self._store`, typed in `__init__`). Yet `_start_claim_group` does `getattr(self._store, "start_work_claim_group", None)` then `if not callable(method): return True`, and `_finish_claim_group` does the same for `finish_work_claim_group`, and `_apply_issue_labels` does it for `add_issue_labels`. This defends against a store that lacks methods its own type declares — a silent no-op fallback that papers over an unclear invariant (which stores actually implement these?). If a real store is missing the method, the claim group is silently treated as "started=True" and work-claim accounting just vanishes with no error. Under mypy strict this is also a tell that the `DataStore` protocol/type is lying about its surface.

**Code-judo remedy:** Put `start_work_claim_group` / `finish_work_claim_group` / `add_issue_labels` on the `DataStore` type (they clearly belong there) and call them directly. Delete the three `getattr/callable` guards and the `isinstance(started, bool)` re-coercion at 739. Removes ~12 lines and converts three silent-no-op paths into ordinary typed calls. If test doubles are the reason, fix the double, not the production guard.

---

## Medium

### M1. `_failed()` duplicates `PlayOutcome.failed()` to smuggle a dropped `failure_category` arg
**Location:** `executor.py:1423-1440` (`_failed`), called at 286 and 500; cf. `PlayOutcome.failed` in `state.py:159-182`.

**Problem:** `_failed` rebuilds a `PlayOutcome` field-by-field — identical to `PlayOutcome.failed()` except it takes a `failure_category` positional that it then **silently discards** (PlayOutcome has no such field; line 1429-1440 never uses `failure_category`). Both call sites already make a *separate* `_persist_play(..., failure_category=...)` / `_record_pre_dispatch_skip(...)` call that carries the real category. So the third arg of `_failed` is pure decoration that misleads readers into thinking the category rides on the outcome.

**Code-judo remedy:** Delete `_failed` (1423-1440); call `PlayOutcome.failed(play_type, error, agent_id=...)` at the two sites and let the existing persist call own `failure_category`. Removes 18 lines and a misleading parameter.

### M2. `candidates.py` resolver-vs-state code review candidate building is duplicated four ways
**Location:** `candidates.py:611-645` (state-plan CODE_REVIEW), `1010-1094` (`_code_review_candidates`), `1171-1216` (`_github_code_review_candidates`), and the generic `1218-1252` (`_github_pr_candidates`).

**Problem:** Four blocks build CODE_REVIEW/PR `PlayCandidate`s with the same skeleton: iterate PRs/queue rows → skip in-flight → compute `pr_resource_keys_for_pr` → `resource_conflict_reason` filter → append a `PlayCandidate(... sort_key=(index, pr_number))`. The pending-review-queue traversal in particular appears in both `build()` (611-632) and `_code_review_candidates` (1021-1057) with subtly different reviewer-pinning. `_github_code_review_candidates` and `_github_pr_candidates` are 90% identical (same try/except over `list_pull_requests` + `filter_trusted_pull_requests` + conflict filter); the only real difference is the reviewer pick.

**Code-judo remedy:** Extract one `_pr_candidate(pr_or_number, play_type, *, index, extra_keys=(), params_overrides) -> PlayCandidate|None` builder that owns resource-key + conflict-filter + sort-key, and one `_github_pr_fallback(state, predicate, *, build, limit, log_key)` that owns the try/except + trust filter. Re-express the four blocks as calls to those two. Folds `_github_code_review_candidates` into `_github_pr_candidates` (reviewer pick becomes part of the `build` callback). Removes an estimated 50-70 lines and three near-identical try/except bodies.

### M3. `_reconcile_issue_pickup_publish` is a 76-line procedural recovery flow with scattered early-returns
**Location:** `executor.py:1195-1270`.

**Problem:** Nine sequential guard/early-return points (play type, success, github-None, tests_passed, issue/branch presence, error-marker match, identity resolution, find-existing-PR, remote-branch-exists, create-PR, auth-mark) interleave logging, GitHub calls, identity resolution, and outcome-rewriting. It's a state machine ("did local work + tests but failed at publish → find/create the PR") encoded as a straight-line ladder. The auth-error handling is split across two places (1218-1221 and 1264-1269).

**Code-judo remedy:** This is a recovery *policy*; lift it out of the executor into a small `IssuePickupPublishReconciler` (or free function) taking `(github, manager, cfg, project_path)` so the executor just calls `await reconciler.reconcile(params, outcome, skill_result, state)`. Internally split into `_locate_existing_pr`, `_create_recovery_pr`, `_record_auth_failure` — each independently testable. Doesn't delete much net, but removes ~76 lines of recovery policy from the lifecycle file and makes both halves testable in isolation. Lower-priority than C1/C2 but the file is 1546 lines and this is one of the cleaner extractions available.

### M4. `PlayExecutor.execute` lifecycle is 11 sequential phases hand-threaded through `_ExecutionSetup`
**Location:** `executor.py:225-657` (the `execute` → `_prepare_dispatch` → `_select_skill_agent` → `_prepare_execution_context` → `_run_finalize_and_persist` chain).

**Problem:** Each phase returns `Stage | PlayOutcome` and the caller does `if isinstance(x, PlayOutcome): return x` (lines 239, 252, 262). That's a hand-rolled short-circuit monad: four `isinstance(..., PlayOutcome)` early-exit checks plus a frozen `_ExecutionSetup` carrier (120-128) that exists only to shuttle 7 values between two methods. `_run_finalize_and_persist` (557-657) alone is 100 lines doing run + worktree finalize + alignment reload + scope check + deferral wiring + planned-issue cleanup + mutation persist + play persist + claim-group finish + outcome stamp.

**Code-judo remedy:** This is more rearrange-than-delete, so it's Medium not High, but the win is real: the four `isinstance(prepared, PlayOutcome)` guards (the "skip outcome" sentinel) are the spaghetti. Model the pre-dispatch stages as raising a single internal `_SkipDispatch(outcome)` exception caught once at the top of `execute`, instead of every stage returning a union and every caller re-checking it. That deletes the union return types and the four isinstance checks, and lets `_ExecutionSetup` stay as a plain phase boundary. Net: ~4 branch deletions and clearer phase signatures; doesn't shrink the file much but removes a real readability tax.

---

## Low

### L1. `pr_merge_ready` / `pr_unblockable` re-`getattr` the same 8 PR fields via `_string_or_none`/`_bool_or_none` coercers
**Location:** `candidates.py:246-330`, helpers at `1316-1327`.

**Problem:** Both predicates call `blocked_reasons(...)` with the same eight `getattr(pr, ...)` reads wrapped in `_string_or_none`/`_bool_or_none`/`_labels`. The duck-typed `getattr(pr, "x", None)` against `object` defeats type checking on what are concrete `PullRequestRecord`s in most call paths.

**Code-judo remedy:** Extract `_pr_blocked_reasons(pr) -> list[str]` that does the eight-field gather once; both predicates call it. If callers always pass `PullRequestRecord`, type the param and drop the `_string_or_none`/`_bool_or_none` coercers entirely. ~10 lines and removes the `object`-typed duck-typing.

### L2. `_issue_number_from_value` / `_pr_number_from_payload` / URL-`/pull/(\d+)` regex repeated
**Location:** `executor.py:143-150` (`_issue_number_from_value`), `1496-1507` (`_pr_number_from_payload`), and the inline `re.search(r"/pull/(\d+)", url)` at `969-972`.

**Problem:** Three slightly-different "coerce an int / extract a PR number from a URL" parsers. The inline regex at 969-972 duplicates `_pr_number_from_payload`'s URL branch.

**Code-judo remedy:** Have `_wire_deferrals` call `_pr_number_from_payload(artifact)` instead of the inline regex; consolidate the two int-coercers into one `_coerce_issue_or_pr_number(value, url_pattern)` if practical. Minor; ~8 lines.

---

## Summary of deletable surface
- **C1:** ~110 lines of dead duplicated free functions — delete outright.
- **C2:** one module (`work_availability.py`) + per-mask-tick analyzer allocations — collapse the shim chain.
- **H1:** ~30 lines + one of two duplicate PR-base-retarget implementations.
- **H2:** 33-line substring ladder + 2 of 3 marker tables — replace with a typed `failure_kind`.
- **H3:** ~12 lines of `getattr/callable` no-op guards on an owned store.
- **M1-M4 / L1-L2:** another ~120-150 lines of consolidatable duplication.

The two files contain roughly **300-400 lines that are duplication of logic living elsewhere** (or twice in the same file). The headline action is C1 (delete the dead free-function layer) and H2 (stop reconstructing failure semantics from error prose).
