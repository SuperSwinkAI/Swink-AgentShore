# Thermo-Nuclear Code Quality Review — AgentShore (`integration`)

**Scope:** Full-codebase audit. `integration` was branched from an empty "Initial Setup" commit, so the entire project — 256 source files, ~59k LOC — is "the branch's changes." Reviewed in 10 parallel buckets covering every `.py` file under `src/agentshore/` plus `schema.sql`. Per-bucket detail lives in `.tnqa/01..10_*.md`.

**Date:** 2026-06-01 · **Reviewers:** 10 agents, thermo-nuclear standard (code-judo simplification, >1k-line decomposition, spaghetti/branching, boundary/type cleanliness, canonical-layer leaks).

> **REMEDIATION COMPLETE (updated 2026-06-01).** A 12-agent worktree fleet resolved the in-scope Critical/High findings across two waves; all 12 branches are merged to `tnqa/staging`. Status legend: ✅ **RESOLVED** (merged to `tnqa/staging`, combined suite green) · ⏳ **DEFERRED** (out of this pass per scoping decision: surgical Highs only / cheap shims only) · ❌ **FALSE POSITIVE** (verified not a real defect).

---

## Verdict

The codebase is **clean per-method** — typed boundaries, narrow excepts, frozen dataclasses, NDJSON logging, table-driven agent config — but carries heavy **structural debt of three recurring kinds**, and that debt has already produced **real shipped bugs**, not just smells.

| Severity | Count | ✅ Resolved | ⏳ Deferred |
|---|---|---|---|
| **Critical** | **18** | 17 | 1 |
| **High** | **40** | 36 | 4 |
| Medium | 49 | 25* | 24 |
| Low | 39 | 17 | 22 |
| **Total** | **146** | 95 | 51 |

\* A later **cheap-findings pass** (10-agent fleet, one per bucket) worked the deferred Medium/Low backlog for the genuinely easy/cheap items — behavior-preserving, file-local cleanups only (dead-code deletion, helper dedup, untyped→typed, enum-vs-string fixes, import hoists). It resolved **22 Mediums + 17 Lows** (a few partial); see the per-bucket ✅ marks below. The remaining deferred items are re-architectures, perf-sensitive hot-path changes, behavior-divergent fixes, or items gated on the 5 deferred large C/H. Combined suite after the pass: **3134 passed / 0 failed / 2 skipped** (+1 timing test flaky only under full-suite parallel load; passes solo), coverage **84.04%**, ruff + mypy strict clean. (The earlier 3 folded-in Mediums — `05 M3` json crash guard, `10 M1` TypedDict, incidental — are included in the 25.)

**Critical/High pass scope:** 53 of 58 in-scope (5 large re-architectures deferred). **ALL 53 RESOLVED — both waves complete plus the 08 C1 follow-up; all branches merged to `tnqa/staging`, combined suite 3102 passed / 0 failed, coverage 84.26%, ruff + mypy clean.** Branches are presented for review; nothing auto-merged to `integration`. **Zero Critical findings remain.**

The single highest-leverage observation: a large fraction of the Critical/High findings are the *same three patterns* repeated across modules. Fixing the patterns — not the instances — is the win.

---

## Cross-cutting themes (fix the pattern, not the instance)

### T1. Test-shaped architecture: shadow layers & indirection that exist only for monkeypatching
The dominant smell in the repo. Production code is contorted to preserve `patch("module._foo")` test targets:
- **Free-function shadow layers** duplicating class methods, with zero `src/` callers: `candidates.py` (~110 lines, C1), `resolver.py` (~145 lines, C1), `cli_agent.py` + `identity.py` (~70 lines, H2), `cli_identity.py` `KeychainManager` (C2), `session_path.py` shims (M2).
- **Re-export god-modules + `_pkg.` indirection**: `cli/__init__.py` (237 lines, H5) and `core/__init__.py` + `_LoggerProxy`/`_ppo_selector_cls` (M3) re-export ~70/50 private names and route every call through a package-attribute walk *on hot paths* purely so legacy patch targets resolve.
- This directly violates the project's own **no-backward-compat-code** rule (MEMORY: `feedback_no_legacy_refs`). Remedy across the board: migrate tests to patch symbols at their real homes, delete the shims.

### T2. Dead parallel implementations (two of everything, tests pin the dead one)
Whole second implementations shipped and unreachable, often *drifted* from the live one:
- `mask.py` `_stage_*` pipeline — 116 dead lines, already drifted (C1, bucket 04).
- `sidecar/server.py` — **two complete stdio serve loops**; the one with the documented health heartbeat + bridge co-hosting is dead in production (C1, bucket 07).
- `ui/` — `ActivePlayWidget` (C1), `RevertConfirmModal` (H4), `toggle_pause`/`show_learnings` actions (H1) — built, exported, unit-tested, never mounted/bound.
- `agents/` — an entire **API/httpx agent transport** described in docstrings/CLAUDE.md that does not exist in code (H1).

### T3. Write/read drift from hand-duplicated field lists → actual data bugs
The same column/field set hand-typed in N places, and the copies disagree:
- `pull_requests` column list duplicated 6× → **`base_ref` is write-only** → base-ref drift/retarget silently inert on every cached PR (C1/C2, bucket 05 — *live functional bug*).
- `rl_experience.mask_reason` write-only, same pattern (C3, bucket 05).
- `compute_reward` triplicates 19 terms → a dropped sum term silently vanishes from the total (H4, bucket 04).
- Config defaults encoded twice (`_DEFAULT_YAML` vs per-field `.get`) (H3, bucket 10).
- PR-record builder + `--json` field list duplicated in `github/adapter.py` (H2, bucket 10).

### T4. God-objects sharded to satisfy a per-file LOC budget
- **`core/` is one ~44-field, ~50-method orchestrator class split across 7 mixins** purely "so each file stays under the LOC budget" (its own comment). The mixins share `self` state through implicit MRO and force a ~210-line `NotImplementedError` stub wall in `base.py`. This makes state flow *harder* to trace, not easier (C1/C2, bucket 03).
- ~~`cli_identity.py` (1202 lines) is five unrelated modules in a trench coat (C1, bucket 08).~~ ✅ **RESOLVED** — split into the `identity_wizard/` package.
- `reports/collector.py` (1317 lines) is a namespace-class of static helpers (H1, bucket 10).

### T5. Stringly-typed reconstruction of types thrown away at the boundary
- Failure semantics rebuilt by grepping `error.lower()` substrings (H2, bucket 01) instead of a typed `failure_kind` set at the failure site.
- `MaskReason` deliberately impersonates `str` so substring matching survives (M4, bucket 04) — re-introducing the free-text matching the typed `classification` field was created to kill.
- `.status.value == "error"` string compares instead of the `AgentStatus` enum used 30 lines away (M3, bucket 04).
- Override-mask handling falls back to substring-sniffing raw strings (H4, bucket 03).

### T6. `getattr/callable` duck-typing on owned, typed objects
`getattr(self._store, "method", None)` + `callable()` guards against a `DataStore` whose type *declares* the method (executor H3, resolver H2). Erodes mypy coverage and creates silent no-op fallbacks — and the `"x" in keys` variant in `rows.py` is precisely what turned the `base_ref` omission from a crash into a shipped bug (H1, bucket 05).

### T7. Files over 1000 lines (decomposition targets)
`executor.py` 1546 · `server.py` 1419 · `completion.py` 1371 · `candidates.py` 1364 · `collector.py` 1317 · `cli_agent.py` 1272 · `cli_identity.py` 1202 · `selector.py` 1193 · `_parsers.py` 1090 · `phases.py` 1053 · `loop.py` 1029. Concrete per-file decompositions are in each bucket file.

### T8. Documentation drift
CLAUDE.md says **schema version 13 / 21 tables**; the actual schema is **version 3 / 22 tables** (`schema.sql:363`, migration chain only v1→v2→v3). Recommend correcting the doc and asserting the table count in a test.

---

## Confirmed live defects (not just smells) — fix first

These are behavioral bugs verified by the reviewers, ordered by blast radius. Two of the original eleven turned out to be false alarms on verification (see #7, #8).

1. ✅ **RESOLVED** — **`pull_requests.base_ref` is write-only** (`data/store/mixins/pull_requests.py` SELECTs + `rows.py:246`). Every PR loaded from the DB had `base_ref=None`, silently disabling base-ref-drift/retarget logic. → bucket 05 C1. *Fixed by A5; round-trip regression test added.*
2. ✅ **RESOLVED** — **`learnings.decay()`/`reinforce()` drop the `scope` field** (`learnings.py:110-162`). → bucket 10 C1. *Fixed by A12 (frozen dataclass + `dataclasses.replace`); regression test added.*
3. ✅ **RESOLVED** — **`_kill_process` could raise `TypeError` on the only hard-kill path** (`cli_agent.py:1209-1215`). → bucket 06 C1. *Fixed by A7 (explicit `except (ProcessLookupError, TypeError)`, `locals()` control flow removed).*
4. ✅ **RESOLVED** — **Loop-alert banner advertised dead keys** (`ui/widgets/alert_bar.py`). → bucket 09 H3/H4. *Fixed by A11: rewrote the banner to `"[Q]uit or wait for auto-stop"` and deleted the dead `RevertConfirmModal`. Wiring real `r`/`o` revert/override was deliberately NOT done — a user-driven override would violate the PPO-driver invariant (deterministic code never drives). A11 flagged the genuine product gap (loop-wedge has no operator recovery beyond quit/auto-stop) for a future core-domain decision.*
5. ✅ **RESOLVED** — **TUI could not pause** (`ui/app.py`). → bucket 09 H1. *Fixed by A11 (bound `p`→`toggle_pause`, `l`→`show_learnings`).*
6. ✅ **RESOLVED** — **NaN/Inf → invalid JSON on the sidecar path** (`sidecar/server.py` used bare `json.dumps`). → bucket 07 H3. *Fixed by A8 (shared `ipc/wire.py`, `allow_nan=False`); verified `float('inf')` now serializes as `null` under strict `json.loads`.*
7. ❌ **FALSE POSITIVE** — ~~Handshake `capabilities()` has drifted~~. Verified: the 6 "missing" methods are an **intentional forward-looking TODO list** (per the comment in `handshake.py`), not a drift bug. The `07 H1` routing-registry refactor was still done by A8; `capabilities()` was deliberately left as-is.
8. ❌ **FALSE POSITIVE (code) / docstring-only** — ~~Observation version marker uses wrong divisor~~. Verified: `observation.py:576` `/13.0` is **correct** (`OBSERVATION_VERSION=13`, marker = 1.0). Only the docstring (`:34`) was stale. *A4 fixed the docstring and made the marker self-normalizing (`OBSERVATION_VERSION / float(OBSERVATION_VERSION)`) so it can't drift on a future bump.*
9. ✅ **RESOLVED** — **`_row_to_github_issue` can crash issue reads** (`rows.py:292-293`, bare `json.loads`). → bucket 05 M3. *Fixed by A5 (guarded loader); regression test added.*
10. ✅ **RESOLVED** — **`token_valid=True` returned without validating** when `strict=False` (`identity.py`). → bucket 06 H4. *Fixed by A7 (renamed `_TokenResolution.token_valid`→`token_validated`, returns `False` when unvalidated).*
11. ✅ **RESOLVED** — **`total_cost` computed two ways** (`reports/collector.py:308-313`). → bucket 10 H4. *Fixed by A12 (single play-sum in `_compute_overview`); both reports now agree.*

---

## Findings by bucket

Each item: severity · location · one-line problem → remedy. Full rationale in the referenced `.tnqa/` file.

### Bucket 01 — `plays/executor.py` (1546) + `plays/candidates.py` (1364) · `.tnqa/01_plays_core.md` — ✅ all resolved (A1, `tnqa/a1-plays-executor`)
- ✅ **C1** `candidates.py:791-908,1286-1303` — dead duplicated free-function layer (~110 lines), zero callers, drift hazard vs `PlayCandidateAnalyzer` methods → deleted.
- ✅ **C2** `candidates.py:911-937` + `work_availability.py` — freshness fns allocated a full analyzer per call on the mask hot path → now `state`-only functions (no per-tick allocation). *`work_availability.py` kept as a thin re-export shim to avoid a cross-agent edit on `rl/mask.py`; full shim deletion deferred.*
- ✅ **H1** `executor.py:930-1099` — `_wire_deferrals` 170-line method with nested PR-authoring block + 2nd copy of `_maybe_retarget_pr_base` → extracted helpers, unified retarget into one `_retarget_pr_to_target`.
- ✅ **H2** `executor.py:93-117,1443-1476` — failure classification reverse-engineered from error prose → typed `FailureKind` (in `errors.py`) + `PlayOutcome.failure_kind`, set at the failure site; `failure_category` kept as a derived value so consumers are unaffected.
- ✅ **H3** `executor.py:726-747,1152-1154` — `getattr/callable` no-op duck-typing on the owned `DataStore` → direct typed calls, guards deleted.
- ✅ **M1** `executor.py` — `_failed` helper duplicating `PlayOutcome.failed()` → deleted; both sites call `PlayOutcome.failed(..., failure_kind=...)` directly. *(cheap-findings pass)*
- ✅ **L1** `candidates.py` — repeated 8-field blocked-reason gather → extracted `_pr_blocked_reasons`; `pr_merge_ready`/`pr_unblockable` both delegate. *(cheap-findings pass)*
- **M2-M4 / L2** ⏳ still deferred — 4-way duplicated PR-candidate builders (M2, re-architecture); 76-line procedural `_reconcile_issue_pickup_publish` (M3); 11-phase `isinstance`-sentinel lifecycle (M4, touches dispatch control flow); duplicate number/URL parsers (L2, divergent signatures — not behavior-preserving-trivial).

### Bucket 02 — `plays/` rest + `play_rules.py` · `.tnqa/02_plays_rest.md` — ✅ all resolved (A2, `tnqa/a2-plays-resolver`)
- ✅ **C1** `resolver.py:683-855` — ~145 lines of dead/test-only methods → deleted; 3 tests retargeted at `PlayCandidateService`. (`resolver.py` 874→645.)
- ✅ **H1** `resolver.py:629-829` — seven byte-identical "candidate loop" resolvers → one `_resolve_via_candidates(play_type, state, *, idle_reviewers=None)`.
- ✅ **H2** `resolver.py:147,472-597` — `getattr/callable/isinstance` test-mock layer + silent claim-bypass → direct typed calls; tests fixed to `AsyncMock(spec=DataStore)`.
- ✅ **M1** dead `OverrideEntry.as_tuple` + identity-wrapper `PlayParams.empty()` → deleted; 6 test sites migrated to `PlayParams()`. ✅ **M3** `_find_pr -> object` → typed `PullRequestSnapshot | PullRequestRecord | None`; `getattr`+isinstance in `_resolve_specific_pr` → direct typed access. ✅ **M4** unused `skill_name` params removed; 1-line `_context_discipline` wrapper inlined. *(cheap-findings pass)*
- ✅ **L1** `dispatch.py` function-local imports hoisted. ✅ **L3** `preconditions_met` no longer swallows missing-play `KeyError` (propagates; sole caller already catches+logs). *(cheap-findings pass)*
- **M2 / M5 / L2** ⏳ still deferred — `_resolve_override` dispatcher (M2, table-driven refactor, behavior-change risk); two PlayParams serializers (M5, intentional smaller projection — reconciling changes context.json); `needs_review` collapse (L2, proposed tail is NOT behavior-equivalent + crosses into candidates.py).

### Bucket 03 — `core/` (18 files) · `.tnqa/03_core.md` — ✅ resolved (A3, `tnqa/a3-core`), 2 deferred
> **Headline:** the 7-mixin split is one god-object sharded to a per-file LOC budget. The honest fix is composition. **C2 (full decomposition) is deferred** per scope (surgical Highs only); H1's `GitHubSyncer` is a first step.
- ✅ **C1** `completion.py:304-331` — `_process_completion` "return True = abort" pipeline → typed `_CompletionVerdict`. *Narrowed: the 3 `_build_state` snapshots were left distinct (folding them changes published-snapshot/shutdown timing — not behavior-preserving).*
- ⏳ **C2** `base.py:69-692` — 44-field state bag + ~50-method stub wall → **DEFERRED** (large re-architecture).
- ✅ **H1** `completion.py:1134-1300` — `_refresh_issues` duplicated `_phase_fetch_github` → extracted `GitHubSyncer` collaborator; both delegate.
- ⏳ **H2** `loop.py:722-1029` — `_run_loop_body` → typed `TickAction` → **DEFERRED** (large rewrite).
- ✅ **H3** `loop.py:444-807` + `progress_monitor.py` — five autonomous-stop paths + 3× skip-classification → one `_initiate_autonomous_stop(reason, ...)` + `_compute_skip_diagnosis`.
- ✅ **H4** `dispatch.py:159-437` — override-mask string-substring type-sniffing → `MaskReason.classification` is now the source of truth; fallback bodies deleted, `MaskReason | str` union dropped from those signatures.
- ✅ **H5** `dispatch.py:493-775` — dead `revalidate` param + `should_revalidate` plumbing → deleted end-to-end. *The 5 pre-flight gate-move into `EligibilityAuthority` was deferred (that logic lives in `rl/`, out of core's domain) — tracked as follow-up.*
- ✅ **M4** `_compute_session_stats` enum-vs-string row dup → resolve `PlayType` once, build the snapshot row a single time. ✅ **L4** documented-no-op `_persist_alignment_scores` → deleted end-to-end (method, call-site branch, base stub). *(cheap-findings pass)*
- **M1-M3 / M5 / L1-L3,L5** ⏳ still deferred — phases.py sequencing/I-O interleave (M1, re-arch); `_fetch_state_data` overload split (M2, fragile); `_LoggerProxy`/`_ppo_selector_cls` re-export wall (M3, needs H5-style test migration); `_should_terminate` plan-in-a-predicate (M5, per-tick design change); the remaining Lows are gated on the C2 god-object decomposition or the IPC wire version (state-layer changes).

### Bucket 04 — `rl/` (15 files) · `.tnqa/04_rl.md` — ✅ C1/H1/H3/H4/H5 resolved (A4, `tnqa/a4-rl`); ⏳ H2 deferred
- ✅ **C1** `mask.py:494-609` — dead `_stage_*` mask pipeline (~116 LOC) deleted; 9 tests retargeted at `EligibilityAuthority`/`ActionMaskBuilder`.
- ✅ **H1** `observation.py` — ❌ *code `/13.0` was **correct**, not a bug.* Stale docstring fixed + marker made self-normalizing (`OBSERVATION_VERSION / float(OBSERVATION_VERSION)`) so it can't drift.
- ⏳ **H2** `observation.py:90-578` — declarative `FeatureBlock` registry → **DEFERRED** (large rewrite).
- ✅ **H3** `eligibility.py`/`mask.py` — 3 overlapping taxonomies → `CANDIDATE_REQUIRED_PLAY_TYPES`/`LIVE_CONFIRM_PLAY_TYPES` single-sourced in `play_rules.py`.
- ✅ **H4** `reward.py:166-424` — reward sum/log now derived from `RewardBreakdown` fields (`_SUMMED_TERMS` + `asdict`); bit-identical output, dropped terms can't vanish.
- ✅ **H5** `selector.py` — file-locking + checkpoint I/O extracted to new `rl/checkpoint_store.py`; selector delegates (re-exports kept for `core/phases.py`).
- ✅ **M1** PR-pressure `10.0` in 3+ places → single `SAT_OPEN_PRS_COUNT` constant in `rl/constants.py`; `observation.py`/`reward.py` import it. *(cheap-findings pass)*
- ✅ **M3** (status-enum portion) `.status.value == "error"` string compares → typed `AgentStatus` enum in `eligibility.py` (4 sites) + `observation.py` (5 sites). *(`last_error_class` magic-string→StrEnum left deferred — cross-module design change.)*
- ✅ **L1** `metrics.py` function-local imports (`AgentStatus`/`time`/`datetime`) hoisted to module top. ✅ **L2** `metrics.py` churn-window rebind + always-zero `recent_created` term cleaned. ✅ **L4** `replay.py` `store._conn` reach → typed `DataStore.distinct_experience_session_ids(...)`. *(cheap-findings pass)*
- **M2 / M4-M6 / L3,L5,L6** ⏳ still deferred — `confirm()` plan+mask rebuild (M2, perf-sensitive hot path); `MaskReason` str-shim (M4) + `_only_capacity_waiting` substring (L3) need consumer re-routing through `classification`; loop-escalation ladders (M5) + config-mask filters (M6) are design-level coupling; `training.py` None re-checks (L5, load-bearing for mypy narrowing); `INDEX_TO_PLAY` redundancy (L6, Minor).

### Bucket 05 — `data/` (28 files + worktree registry) · `.tnqa/05_data.md` — ✅ all resolved (A5, `tnqa/a5-data`)
- ✅ **C1** `pull_requests.py` SELECTs + `rows.py:246` — **`base_ref` write-only** bug → fixed; `base_ref` round-trips (regression test added).
- ✅ **C2** `pull_requests.py` + `helpers.py` — PR column list duplicated 6× → `_PULL_REQUEST_COLUMNS` defined once, SELECT/upsert/row-map derived.
- ✅ **C3** `rl.py:43` + `rows.py:342` — **`mask_reason` write-only** → finished the read path (added to both replay SELECTs + `_row_to_experience`); regression test added.
- ✅ **H1** `rows.py:202-294` — dead `"x" in keys` guards → deleted, columns read directly.
- ✅ **H2** all mixins — INSERT/field-tuple/commit/`lastrowid` boilerplate → `_DataStoreBase._insert(table, **cols)` helper; 11 single-row inserts collapsed.
- ✅ **H3** `agents/worktree/registry.py` — parallel store via `store._conn` → moved into a real `_WorktreesMixin` (+ `WorktreeRow`/`WorktreeStatus` to `models.py`); `registry.py` now delegates.
- ✅ **M3** (folded in earlier) — bare `json.loads` crash in `_row_to_github_issue` → guarded loader.
- ✅ **M1** untyped `get_dispatch_replay` dict → `DispatchReplayRecord` dataclass + `_row_to_dispatch_replay`; consumer + test updated. ✅ **M2** repeated status-placeholder SQL + `"\n".join` assembly → `_status_in_clause()` helper in `base.py`; ~9 builders collapsed. *(cheap-findings pass)*
- ✅ **L1** lazy in-method imports (`structlog`/`os`/`datetime`) hoisted. ✅ **L3** inline row builds → `_row_to_checkpoint`/`_row_to_external_mutation` in `rows.py`. *(cheap-findings pass)*
- **M4 / L2** ⏳ still deferred — schema-derived `reset_session_scoped_tables` (M4) would newly truncate `worktrees`/`dispatch_replay` (behavior change, needs design call); `Literal` + `_VALID_*` column guards (L2) add boundary validation that can raise on previously-tolerated values.

### Bucket 06 — `agents/` (19 files) · `.tnqa/06_agents.md` — ✅ all resolved (A6 `tnqa/a6-agents-manager` + A7 `tnqa/a7-agents-cli`)
- ✅ **C1** `cli_agent.py:1209-1215` — `_kill_process` `locals()` control flow + unsuppressed `TypeError` → explicit `except (ProcessLookupError, TypeError)`. *(live defect, A7)*
- ✅ **C2** `manager.py:285-296` — per-`dispatch()` token resolution + `gh repo view` preflight → resolved once at `instantiate`, cached on `AgentHandle.identity_env`; two `gh` round-trips removed from the hot path. *(A6)*
- ✅ **H1** `manager.py` + `__init__.py` — dead API/httpx abstraction → API-only error arms + docstrings deleted. *(A6)*
- ✅ **H2** `cli_agent.py`, `identity.py` — free-function test shims deleted; tests retargeted at the classes (`bad_identity_rows`/`missing_token_rows` preserved). *(A7)*
- ✅ **H3** `cli_agent.py:817-1119` — `CliOutputParser` static class + copy-pasted JSONL loop → `dict[AgentType, CliOutputFormat]` registry + shared `_iter_json_events`. *(A7)*
- ✅ **H4** `identity.py` — `_TokenResolution.token_valid`→`token_validated`, returns `False` when unvalidated. *(live defect, A7)*
- ✅ **H5** `manager.py:184-214` — non-atomic `instantiate` → preflight now runs before any registration; failed agents not registered. *(A6)*
- ✅ **M2** 8-field positional `_ReadOutputResult` tuple → frozen `_ReadOutput` dataclass. ✅ **M3** dead `all_known_worktree_paths` wrapper → deleted. ✅ **M4** dead `warn_missing` branch → removed from `read_keychain_token`. ✅ **M5** `GH_CONFIG_DIR=""` silent fallback → falls through to isolated config dir. ✅ **M6** ~90-line duplicated worktree allocate methods → one parameterized `_allocate_locked(...)`. *(cheap-findings pass)*
- ✅ **L3** `_safe_int` isinstance narrowed (JSON never yields bytes). ✅ **L4** in-loop `models_for_agent` stale-catalog log → `info`. *(cheap-findings pass)*
- **M1 / L1-L2** ⏳ still deferred — 320-line `dispatch_cli` decomposition (M1, large); `--resume` JSON-retry path (L1, actively-retained per desktop-dy2j comment); `on_spawned`/`on_exited` closures (L2, cosmetic, capture per-dispatch handle).

### Bucket 07 — `sidecar/` (15) + `ipc/` (6) · `.tnqa/07_sidecar_ipc.md` — ✅ all resolved (A8, `tnqa/a8-sidecar`)
- ✅ **C1** `server.py:1162-1419` — two divergent stdio serve loops → unified into one (`_serve_async` keeps cancellation + now fires the health heartbeat); dead twin + its dead-only tests removed. (`server.py` 1419→1360.)
- ✅ **H1** `server.py:1021-1114` — 90-line routing ladder → `HANDLERS` registry + `_ROUTE_GROUPS`. ❌ *The "capabilities() drift" sub-claim was a **false positive** (intentional TODO list); `capabilities()` left as-is.*
- ✅ **H2** `server.py` — copy-pasted JSON-RPC framing + two `$/progress` authors → one `frame()` (`ipc/wire.py`) + one `notification()` factory.
- ✅ **H3** `ipc/serializer.py` vs sidecar writes — NaN/Inf corruption → new `ipc/wire.py` (`json_safe`+`allow_nan=False`) shared by both transports; verified `float('inf')`→`null`. *(live defect fixed)*
- ✅ **H4** `server.py:810-833` — per-handler notification handling → declarative `Route.notify_ok`; `recents.touch` mutate-then-discard removed.
- ✅ **M2** UDS-vs-TCP branching in `IpcServer.start()` → `_prepare_unix_path()` + `_bind()` helpers. ✅ **M4** (partial) tripled keyring import + double-`except` → one `_keyring_get()` helper (the larger `TokenSource`-enum part left deferred). ✅ **M6** dead `notification_emitters.py` builders → deleted; stale docstring corrected. *(cheap-findings pass)*
- ✅ **L4** `get_event_loop().create_task` → `get_running_loop().create_task` (3.12 deprecation). *(cheap-findings pass)*
- **M1 / M3 / M5 / L1-L3** ⏳ still deferred — 95-line `session.stop` teardown (M1, ESR-ordering invariant); inbound IPC parse/validate/route split (M3, re-arch); two competing yaml writers (M5, behavior-divergent); `issue_availability` alias (L1, dashboard still reads it); per-dispatcher excepts (L2, depend on deferred C1); `os.chdir` cwd anchor (L3, behavior-divergent supervision).

### Bucket 08 — `cli/` (19) + `cli_identity.py`/… · `.tnqa/08_cli.md` — ✅ C1/C2/C3/H1/H2/H3/H4 (A9 `tnqa/a9-cli-identity` + A10 `tnqa/a10-cli-commands` + `tnqa/c1-cli-identity-split`); ⏳ H5 deferred
- ✅ **C1** `cli_identity.py:1-1203` — 1175-line god-module → split into the `identity_wizard/` package (`gh_accounts`/`keychain`/`wizard`/`yaml_patch`/`report` + `__init__`), each one concern. Behavior-preserving move (no logic changes); acyclic DAG; clean break — `cli_identity.py` deleted, all 7 src importers + 4 test files repointed, patch strings retargeted, zero remaining module refs. *(branch `tnqa/c1-cli-identity-split`, follow-up pass.)*
- ✅ **C2** `cli_identity.py:266-289` — dead `KeychainManager` passthrough → deleted.
- ✅ **C3** — triplicated identity-health predicate → canonical `bad_identity_rows(rows)`/`missing_token_rows(rows)` in `agents/identity.py`; in-domain call sites repointed (A10 consumes it in `start.py`).
- ✅ **H1** `start.py:122-523` — ~360-line bootstrap policy → new `session/bootstrap.py`; `start.py` 522→301. *(A10)*
- ✅ **H2** `cli/helpers.py` vs `cli_identity.py` — duplicate repo-access renderer → canonical `echo_repo_access_report` kept; `_echo_repo_access_rows` deleted, `start.py` repointed. *(A9 + A10)*
- ✅ **H3** `init.py:84-157` — three ruamel round-trip writers → new `config/yaml_io.py` (`ruamel_set/get_nested`). *(A10)*
- ✅ **H4** `archive/report/stop/train` — DataStore open/close + last-session boilerplate → `open_store()` CM + `resolve_session_id()`; error paths unified to `ClickException`. *(A10)*
- ⏳ **H5** `cli/__init__.py:1-237` — re-export module + `_cli_pkg._foo` indirection (122 test patches) → **DEFERRED** (cheap-shims-only scope).
- ✅ **M5** `availability._record_to_dict` duplicated three `TypedDict` field lists → replaced serializer + TypedDicts with `dataclasses.asdict(record)` in `save()`; on-disk shape unchanged. *(cheap-findings pass)*
- **M1-M4 / L1-L4** ⏳ still deferred — `init()` 5-phase wizard (M1, large); two incompatible `_str_or_none` (M2, clean fix lands in `config/` — owned elsewhere / H5 territory); `_find_free_dashboard_port` vs `find_free_tcp_port` (M3, re-export-coupled + possibly-intentional 9400 affinity); `_dispatch_command` 100-line elif (M4, reaches orchestrator privates); the 4 Low are all gated on the H5 `cli/__init__` re-export slimming.

### Bucket 09 — `ui/` (24) + `dashboard/` python (2) · `.tnqa/09_ui_dashboard.md` — ✅ all resolved (A11, `tnqa/a11-ui`, −315 LOC)
- ✅ **C1** `ui/widgets/active_play.py` — never-mounted twin widget → deleted (file + tests).
- ✅ **C2** `screens/issues.py` vs `widgets/work_queue.py` — divergent lifecycle grouping → one `OrchestratorState.work_queue() -> WorkQueueView`; both widgets now pure formatters.
- ✅ **H1** `app.py` — `action_toggle_pause`/`action_show_learnings` unbound → bound (`p`/`l`). *(live defect)*
- ✅ **H2** `screens/shutdown.py` — dead `set_summary`/`set_play` pipeline → deleted.
- ✅ **H3 + H4** `widgets/alert_bar.py` + `screens/revert.py` — banner advertised dead `[R]evert [O]verride` keys → **banner rewritten to `"[Q]uit or wait for auto-stop"` + dead `RevertConfirmModal` deleted.** Real revert/override was deliberately not wired: it would violate the PPO-driver invariant (deterministic code never drives). *Product gap flagged for a future core decision: loop-wedge has no operator recovery beyond quit/auto-stop.* *(live defect)*
- ✅ **M1** 3 divergent PlayType→label formatters + widget→screen coupling → one `ui/play_labels.py` (`play_label`/`play_short_label`); `app.py`/`rl_state.py`/`dashboard.py` repointed. ✅ **M2** duplicated `_truncate` → shared `ui/format.py:truncate()` (`_as_int`/ISO helpers already gone with `active_play.py`). *(cheap-findings pass)*
- ✅ **L1** 12× in-method `OrchestratorApp` import in `provider.py` hoisted. ✅ **L2** (partial) thrice-local `QueryError` import in `escalation.py` hoisted. *(cheap-findings pass)*
- **M3-M4 / L2(mount),L3-L4** ⏳ still deferred — `loop_level_for_streak` recompute (M3) needs a `loop_level` field on the core snapshot (state-layer change); in-place `_latest_state` mutation (M4) is a latency optimization (not behavior-preserving to remove); the `escalation.py` mount refactor (L2) changes mounting semantics; `bridge.py` broadcast-contract (L3) + dual constructor args (L4) touch public signatures across 3 call sites.

### Bucket 10 — `reports/` + `skills/` + `config/` + `github/` + `beads/` + root modules · `.tnqa/10_reports_skills_misc.md` — ✅ C1/H2/H3/H4 (A12); ⏳ H1 deferred
- ✅ **C1** `learnings.py:110-162` — `decay()`/`reinforce()` drop `scope` → frozen dataclass + `dataclasses.replace`; regression test added. *(live defect fixed)*
- ⏳ **H1** `reports/collector.py:1-1317` — god-class decomposition → **DEFERRED** (large; only the H4 `total_cost` fix was applied to this file).
- ✅ **H2** `github/adapter.py:235-364` — duplicated `PullRequestRecord` construction → `_PR_JSON_FIELDS` constant + `_pr_record_from_json` helper.
- ✅ **H3** `config/__init__.py` + `_parsers.py` — defaults encoded twice → kept `_DEFAULT_YAML` as single source + added a drift-guard test pinning `generate_default_config()` to `load_config(None)`. *(Full dataclass-generation was rejected as it would change `load_config(None)`; also fixed M1: `pr_allow_list` added to `_RawTrustedIds`.)*
- ✅ **H4** `reports/collector.py:308-313` — `total_cost` computed two ways → unified in `_compute_overview` (play-sum). *(live inconsistency fixed)*
- ✅ **M4** `pr_state.py` three near-identical rollup walkers → one `_collect_rollup_states()`; both summaries derive from the same set. ✅ **M2** (partial) `session_path.py` dead `_signal_group`/`_terminate_process_tree` aliases → deleted (public free-function shims left — they're live + test-patched). *(cheap-findings pass)*
- ✅ **L1** `result_parser.py` three identical object-list coercers → `_json_object_list()` helper. ✅ **L3** `skills/__init__.py` hand-rolled version compare → `packaging.version.Version` (fixes `1.2.0 > 1.10.0` lexical bug; added `packaging` to runtime deps). ✅ **L4** `session_path.py` macOS-wrong docstrings corrected. *(cheap-findings pass)*
- **M2(main) / M3 / L2** ⏳ still deferred — `session_path` free-function shims (M2, public-API design + caller/test-patch rewrites); `_PLAY_LOG_ORDER` registry derivation (M3, couples to action_space, risks report ordering); beads parser alias trim (L2, needs a real `bd --json` payload to verify safely).
- **Verified non-findings:** report templates clean of legacy names; `result_parser.py` robust; `_parsers.py` validators correct; broad-except narrowed + logged.

---

## Remediation progress (12-agent worktree fleet)

**Wave 1 (✅ merged to `tnqa/staging`, combined suite 3112 passed / 0 failed, coverage 83.9%, mypy clean):**
- A1 plays/executor · A2 plays/resolver · A3 core · A5 data · A6 agents/manager · A8 sidecar · A9 cli/identity · A12 reports/config/learnings — 8 branches, file-disjoint, merged with zero conflicts.

**Wave 2 (✅ merged to `tnqa/staging`):**
- A4 rl · A7 agents/cli_agent · A10 cli/commands · A11 ui — 4 branches, file-disjoint, merged with zero conflicts.

**Combined result (all 12 + integration cleanup on `tnqa/staging`):** 99 files changed, net ≈ −90 LOC; **3102 passed / 0 failed / 2 skipped**, coverage **84.26%**, `ruff check` + `ruff format` + `mypy` all clean.

**Confirmed bug fixes landed (with regression tests where applicable):** `base_ref` write-only, `mask_reason` write-only, `learnings.scope` data-loss, `_row_to_github_issue` json crash, `total_cost` divergence, NaN/Inf-in-JSON corruption, `_kill_process` TypeError, `token_valid` mislabel, TUI pause binding, loop-alert dead-key banner.

**False positives retired:** sidecar `capabilities()` "drift" (intentional TODO); observation `/13.0` divisor (code correct — docstring-only, now self-normalizing).

**CLAUDE.md doc fixed:** "schema v13 / 21 tables" → **v3 / 22 tables** (already pinned by `tests/test_schema_fresh_db.py::test_fresh_db_schema_version_is_3` + `test_fresh_db_has_all_expected_tables`).

**Deferred to a follow-up pass (5 C/H + all M/L):** `03 C2` core mixins→composition · `03 H2` TickAction · `04 H2` observation registry · `08 H5` cli re-export removal (+122-patch migration) · `10 H1` collector decomposition. Also: the `_dispatch_play` gate-move into `EligibilityAuthority` (cross-domain), and the `work_availability.py` shim deletion. *(`08 C1` cli_identity split is now ✅ RESOLVED — see bucket 08.)*

**Delivery:** all work is on `tnqa/staging` (+ the 12 per-domain `tnqa/a*` branches and the `tnqa/c1-cli-identity-split` follow-up). **Nothing auto-merged to `integration`** — branches are presented for review/merge. **Zero Critical findings remain;** the only deferred Critical (`03 C2`) is the lone remaining one.

Per-bucket files under `.tnqa/` retain the full rationale, exact line ranges, and code-judo remedies for all 146 findings.
