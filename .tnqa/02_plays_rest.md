## Bucket 02: plays/ (resolver, dispatch, registry, base, override, scope, selector) + play_rules.py

Scope reviewed:
- `src/agentshore/plays/resolver.py` (873)
- `src/agentshore/plays/dispatch.py` (441)
- `src/agentshore/plays/registry.py` (135)
- `src/agentshore/plays/base.py` (122)
- `src/agentshore/plays/override.py` (97)
- `src/agentshore/plays/scope.py` (80)
- `src/agentshore/plays/selector.py` (54)
- `src/agentshore/play_rules.py` (36)

Headline: resolver.py is not 873 lines of essential logic. ~250 of those lines are dead code (no production caller, some test-only), a defensive `getattr`/`callable`/`isinstance` test-mock-compatibility layer that erodes the real DataStore types, and seven byte-identical "candidate loop" methods that should be one. After the code-judo below, resolver.py should land around 500-550 lines and the type story for the store boundary becomes honest.

---

## CRITICAL

### C1. resolver.py carries ~250 lines of dead / test-only code behind production-looking APIs
**Severity:** Critical
**Location:** `resolver.py:683-689` (`_eligible_issue_candidates`), `:725-749` (`_first_open_pr_with_reviewer`), `:751-822` (`_first_open_pr_matching` + 2 `@overload`s), `:831-840` (`_resolve_pr_from_github`), `:842-855` (`_resolve_blocked_pr_from_github`)

**Problem:** Grepping `src/` and `tests/` for callers:
- `_eligible_issue_candidates` — **zero callers anywhere.** Dead.
- `_first_open_pr_with_reviewer` — **zero callers anywhere.** Dead. Reaches into `self._candidate_service._github_code_review_candidates` (private) to do it.
- `_resolve_pr_from_github` — **zero callers anywhere.** Dead.
- `_resolve_blocked_pr_from_github` — **zero production callers**; only referenced in a test *docstring* (`tests/test_pr_gate.py:270`), not actually invoked. Dead.
- `_first_open_pr_matching` (+ its two `@overload` stubs and the dual-shape `isinstance`/`TypeError` body) — **called only from tests** (`test_trusted_ids.py:395`, two `mock.patch` targets in `test_parameter_resolver.py`). No production path. The whole 70-line method, including the `state: OrchestratorState | Callable[...]` union parameter, the synthetic-`OrchestratorState` construction, the `lambda _pr: True` defensive branch, and the `raise TypeError` arm, exists to satisfy tests that patch/exercise a method nothing ships.

These five methods all delegate into `PlayCandidateService` (the real resolution path the live `_resolve_*` methods use), so they are not even alternative implementations — they are stale shims left behind when resolution moved into `candidates.py`.

**Code-judo remedy:** Delete all five methods and the two `@overload` decorators. Delete or rewrite the three tests that pin `_first_open_pr_matching` to instead exercise `PlayCandidateService._github_pr_candidates` directly (that is what they actually want to test). Drop the now-unused imports: `overload`, `Callable`, `PullRequestRecord`, `pr_unblockable`, and the `SessionState`/`OrchestratorState` synthetic-construction usage if not otherwise needed. **Removes ~145 lines from resolver.py and one genuinely confusing dual-shape-union method.**

---

## HIGH

### H1. Seven byte-identical "candidate loop" resolvers — collapse to one table-driven dispatch
**Severity:** High
**Location:** `resolver.py:629-634, 636-645, 647-654, 656-681, 691-698, 700-723, 824-829`

**Problem:** `_resolve_unblock_pr`, `_resolve_write_implementation_plan`, `_resolve_systematic_debugging`, `_resolve_issue_pickup`, `_resolve_refine_tasks`, `_resolve_merge_pr`, and `_resolve_code_review` are the same five lines repeated seven times:

```python
for candidate in await self._candidate_service.candidates_for(<PLAY_TYPE>, state):
    claimed = await self._claim_candidate(state, candidate)
    if claimed is not None:
        return claimed
return None
```

The only variation is the `PlayType` literal, plus `_resolve_code_review` passing `idle_reviewers=idle_can_review_agents(state)`. The large docstrings on `_resolve_issue_pickup` and `_resolve_code_review` describe behavior that now lives entirely in `candidates.py` — they document a sibling module, not this code.

**Code-judo remedy:** Delete all seven methods. In `resolve()`'s match arms, replace each with a single private helper:

```python
async def _resolve_via_candidates(self, play_type, state, **kw):
    for candidate in await self._candidate_service.candidates_for(play_type, state, **kw):
        claimed = await self._claim_candidate(state, candidate)
        if claimed is not None:
            return claimed
    return None
```

The `match` cases (`resolver.py:205-223`) for UNBLOCK_PR / WRITE_IMPLEMENTATION_PLAN / SYSTEMATIC_DEBUGGING / ISSUE_PICKUP / CODE_REVIEW / MERGE_PR / REFINE_TASK_BREAKDOWN all call `_resolve_via_candidates(play_type, state)`, with CODE_REVIEW adding `idle_reviewers=...`. Move the eligibility docstrings to the `candidates.py` analyzer where the logic actually lives. **Removes ~70 lines and 6 redundant methods; the call sites become self-documenting (the PlayType is right there in the match).**

### H2. `getattr(self._store, "...", None)` + `callable()` + `isinstance` test-mock layer erodes the real DataStore type
**Severity:** High
**Location:** `resolver.py:147-149, 472-492, 501-511, 536-548, 550-555, 593-597`

**Problem:** `self._store` is typed `DataStore`. Every one of these methods — `release_work_claim_group`, `acquire_work_claims`, `work_claim_group_is_active`, `get_pull_request`, `claim_review_with_work_claims`, `claim_pending_review_for_pr` — **exists as a real, typed method on `DataStore`** (verified in `src/agentshore/data/store/core.py`; all six found). Yet resolver fetches each via `getattr(self._store, name, None)`, guards with `if not callable(method)` / `if callable(method)`, then re-validates return values with `isinstance(claimed, str)` / `isinstance(active, bool)`. The comment at `:547` is explicit about why: *"Unconfigured AsyncMock in older unit tests: preserve legacy resolution."*

This is production type-safety sacrificed to test fakes. Consequences:
- mypy cannot verify any of these calls (the `getattr` returns `Any`, the guards are unreachable in reality).
- Every call has a dead "method missing" branch that returns the wrong thing silently (`_claim_params` returns the *unclaimed* `params` at `:548` when `acquire_work_claims` is "missing", i.e. dispatches a play without holding its work claim — a real concurrency hazard if ever hit).
- The `isinstance(claimed, str)` / `claimed is None` / "else preserve legacy" tri-state at `:540-548` only makes sense because an AsyncMock can return a `MagicMock`. Against the real store, `acquire_work_claims` returns `str | None`, full stop.

**Code-judo remedy:** Call the methods directly and typed: `await self._store.acquire_work_claims(...)`, `await self._store.get_pull_request(...)`, etc. Delete every `getattr`/`callable`/`isinstance`-on-return guard and the "preserve legacy resolution" fallback branches. Fix the offending tests to use a typed fake/spec'd mock (`AsyncMock(spec=DataStore)`) so missing-method access raises instead of silently returning a truthy MagicMock. **Removes ~25 lines of guard scaffolding, deletes a silent claim-bypass hazard at :548, and restores mypy coverage across the store boundary.**

---

## MEDIUM

### M1. `OverrideEntry.as_tuple` and `PlayParams.empty()` are dead / redundant thin wrappers
**Severity:** Medium
**Location:** `override.py:75-78` (`as_tuple`), `base.py:64-66` (`PlayParams.empty`)

**Problem:**
- `OverrideEntry.as_tuple` ("Legacy 2-tuple view for code paths not yet migrated") has **zero callers** in `src/` or `tests/`. The migration it bridges is complete; the bridge is dead.
- `PlayParams.empty()` is a classmethod returning `cls()` — literally identical to calling `PlayParams()`. It is used only in tests; production code uses `PlayParams()` directly (e.g. `resolver.py:228, 249, 391, 396, 408, 413`). It is an identity abstraction.

**Code-judo remedy:** Delete `as_tuple` outright. Delete `PlayParams.empty` and replace the ~6 test call sites with `PlayParams()`. **Removes 2 dead/no-value wrappers (~9 lines) and the inconsistency where production says `PlayParams()` but tests say `PlayParams.empty()`.**

### M2. `_resolve_override` is a hand-rolled per-PlayType dispatcher bolted onto the main `match`
**Severity:** Medium
**Location:** `resolver.py:366-415`

**Problem:** `_resolve_override` re-classifies `play_type` through a second ladder of `if play_type in _PR_WORK_PLAY_TYPES / _ISSUE_WORK_PLAY_TYPES / == SYSTEMATIC_DEBUGGING / == BROWSER_VERIFICATION / else`, each branch deciding "is the required field present? if not, recurse into `self.resolve(..., override=PlayParams())`; if a sentinel branch, set `bypass_preconditions`; then `_claim_params`." This duplicates the play-type taxonomy that the main `match` in `resolve()` already encodes, and the "field missing → recurse with empty override" pattern (`:391, :396, :408, :413`) is repeated four times with slightly different field checks (`pr_number` / `issue_number` / `branch`). The SYSTEMATIC_DEBUGGING branch has a nested `if override.branch is None` inside `if override.issue_number is None` — an ad-hoc special case.

**Code-judo remedy:** Lift the "required override field per play type" into the same typed model that drives `_SKILL_SPECS` in `dispatch.py` (which already maps PlayType → ordered arg field names). A small table `{PlayType: required_override_field}` collapses the four near-identical "field present? else re-resolve" branches into one loop, and the bypass/recurse decisions become data, not control flow. **Removes ~20 lines and the second, divergent copy of the PR/issue/branch play-type taxonomy.**

### M3. `_find_pr` return type is `object | None`, losing the PR type through the whole claim path
**Severity:** Medium
**Location:** `resolver.py:589-597` (and consumers `:423, :460, :562`)

**Problem:** `_find_pr` is typed `-> object | None` and reaches the PR via `getattr(pr, "pr_number", None)` even though `state.pull_requests` is a typed list and `self._store.get_pull_request` returns a typed record. Because the return is `object`, downstream code (`_resolve_specific_pr` at `:444` `getattr(pr, "github_author", None)`, `_claim_code_review_params` at `:461`) is forced into `getattr` + `isinstance(author, str)` defensive reads on what are actually typed fields.

**Code-judo remedy:** Type `_find_pr -> PullRequestSnapshot | PullRequestRecord | None` (or a shared `Protocol` with `pr_number`/`github_author`), drop the `getattr` reads in the three consumers, and let mypy verify field access. Pairs naturally with H2 (same "store returns `Any`" root cause). **Removes ~3 `getattr`/`isinstance` defensive reads and makes the reviewer-selection path type-checked.**

### M4. `dispatch.py`: `_context_discipline` and `_strip_full_learnings_reads` take an unused `skill_name`
**Severity:** Medium
**Location:** `dispatch.py:182-190`

**Problem:** Both helpers accept `skill_name` and ignore it. `_context_discipline(skill_name, *, context_path)` formats a constant template with only `context_path`. `_strip_full_learnings_reads(skill_name, skill_content)` filters lines without ever reading `skill_name`. These are pass-through parameters that imply per-skill behavior that does not exist. `_context_discipline` is also a one-line wrapper around `str.format` used at exactly one call site (`:162`).

**Code-judo remedy:** Drop the unused `skill_name` params. Inline `_context_discipline` into `render_skill_prompt` (it is `_CONTEXT_DISCIPLINE_TEMPLATE.format(context_path=context_path)` — no helper earns its name). **Removes one thin wrapper and two misleading parameters.**

### M5. `dispatch.py`: two nearly-identical PlayParams → dict serializers
**Severity:** Medium
**Location:** `dispatch.py:276-301` (`params_to_json_safe_dict`) and `:346-357` (the inline `"params": {...}` block inside `serialize_state_for_skill`)

**Problem:** `serialize_state_for_skill` builds a `params` dict (agent_id, issue_number, pr_number, branch, num_commits, url, seed_path, scope, reason, extras) by hand — a strict subset of the fields `params_to_json_safe_dict` already produces. Two hand-maintained field lists for the same dataclass means adding a `PlayParams` field requires remembering both spots (and the inline one silently drops the new field from context.json).

**Code-judo remedy:** Have `serialize_state_for_skill` call `params_to_json_safe_dict(params)` and, if the smaller projection is intentional, derive it by popping the runtime-only/target_* keys rather than re-listing every field. **Removes one of two divergent field lists and the maintenance trap.**

---

## LOW

### L1. `_json_safe_extras` does function-local imports of `dataclasses`/`enum`
**Severity:** Low
**Location:** `dispatch.py:257-258`

**Problem:** `import dataclasses as _dc` and `from enum import Enum` inside the function body. The module already does `from dataclasses import dataclass` at top; there is no import-cycle reason for these to be local. Just adds noise on every call.

**Code-judo remedy:** Hoist both to module top. (~2 lines.)

### L2. `play_rules.py`: `needs_review(pr: object)` uses `getattr` triple-read instead of a typed param
**Severity:** Low
**Location:** `play_rules.py:10-35`

**Problem:** Takes `pr: object` and does `getattr(pr, "review_decision"/"head_sha"/"last_reviewed_sha", None)`. Same untyped-PR pattern as M3 — the callers pass real PR snapshots. The two `last_reviewed_sha is None / head_sha is None` branches at `:28-35` also duplicate the SHA-comparison logic in both the APPROVED and non-APPROVED arms.

**Code-judo remedy:** Type `pr` as the shared PR protocol (see M3) and collapse the duplicated SHA comparison: after the APPROVED short-circuit, the tail is exactly `last_reviewed_sha is not None and head_sha is not None and head_sha != last_reviewed_sha` — one expression instead of two nested `if` ladders. (~6 lines.)

### L3. `registry.py`: `preconditions_met` swallows missing-play as `False`
**Severity:** Low
**Location:** `registry.py:59-65`

**Problem:** `preconditions_met` catches `KeyError` from `get()` and returns `False` (treating an unregistered play as "preconditions not met"). Since `build_default_registry` registers all 22 PlayTypes and freezes, an unregistered lookup is a programming error, not a runtime "not eligible" condition — silently returning `False` hides registry-wiring bugs as benign masking.

**Code-judo remedy:** Let the `KeyError` propagate (or assert full coverage at `freeze()`), so a missing registration fails loudly instead of silently masking the play. (~4 lines, removes a silent-fallback.)

---

## Notes / non-findings

- `scope.py` (80 lines) is clean and honest: the `_EXPECTED_ISSUES` table + single `_check_issue_inflation` is the right shape; the docstring is upfront that drift blocking is intentionally not implemented. No action. (Minor: `_PR_BODY_ISSUE_RE` at `:27` appears unused within this file — worth a one-line confirm before deletion, but out of high-conviction scope.)
- `selector.py` (plays/, 54 lines) is a clean Protocol + a test-only `FixedPlanSelector`. No action.
- `base.py` `PlayParams` / `Play` protocol are well-designed; the `_runtime_allocation` private-field decision (excluded from repr/compare, omitted from JSON) is correct and well-documented. Only `PlayParams.empty` (M1) is cruft.
- `override.py` `OverrideEntry` typed model is a good abstraction (it replaced a bare 2-tuple) — keep it; only `as_tuple` (M1) is dead.
- The `_SKILL_SPECS` single-source-of-truth table in `dispatch.py:60-82` (with `PLAY_SKILL_MAP` / `_SKILL_ARGS` derived) is exemplary; M2 should reuse this pattern, not fight it.
