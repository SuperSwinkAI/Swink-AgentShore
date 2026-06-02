# AgentShore — Remaining TNQA Findings (open backlog)

This file tracks the **still-open** findings from the thermo-nuclear code-quality
review. The review found 146 findings across 10 buckets; **105 are resolved** (all
Critical, 37 of 40 High, 30 Medium, 20 Low) across the original 12-agent
remediation, the `08 C1` cli_identity split, a later cheap-findings pass, and the
core-consolidation wave (the `03 C2` god-object teardown + its `core/` cluster).
**Zero confirmed live defects remain.** What's left below is deferred *by design*:
re-architectures, perf-sensitive hot-path changes, behavior-divergent fixes, or
items gated on the remaining large Highs. Full rationale + exact line ranges live in
`.tnqa/01..10_*.md`.

Last verified suite (post core-consolidation wave, on `tnqa/staging`): **3150 passed /
0 failed / 2 skipped**, coverage above the 80% gate, ruff + mypy strict clean.

---

## Remaining counts

| Severity | Open |
|---|---|
| **Critical** | **0** |
| **High** | **3** |
| Medium | 19 |
| Low | 19 |
| **Total** | **41** |

A few items are the unfinished half of a partially-completed finding (noted inline).

---

## The big 3 — the only remaining Highs

These are the deliberate large-scope deferrals; they want their own scoped effort,
not a fan-out cleanup. (The lone Critical `03 C2` and `03 H2` were landed by the
core-consolidation wave.)

| Finding | Location | What | Why open |
|---|---|---|---|
| **04 H2** | `rl/observation.py:90-578` | imperative feature-packing → declarative `FeatureBlock` registry | Large rewrite; observation version is wire-pinned |
| **08 H5** | `cli/__init__.py:1-237` | re-export module + `_cli_pkg._foo` indirection → delete, repoint **122 test patches** | Test-shaped-architecture migration (see T1) |
| **10 H1** | `reports/collector.py` | ~1300-line static-helper god-class → decompose | Large; only the `total_cost` fix landed here |

One cross-domain follow-up also remains: the `work_availability.py` re-export shim
deletion (`summarize_work_availability` → `build_candidate_plan(...).work_availability`).
The `_dispatch_play` pre-flight gate-move was completed in the core-consolidation wave —
the paused / end-session / shutdown gates now live as short-circuit mask stages in
`rl/mask.py` (`_stage_main_repo_paused`, `_stage_end_session_in_flight`).

---

## Themes still in play

Only the cross-cutting patterns with open instances remain here.

### T1. Test-shaped architecture (shadow layers / re-export indirection for monkeypatching)
Production code contorted to preserve `patch("module._foo")` targets, violating the
project's no-backward-compat rule. Open instances:
- `cli/__init__.py` re-export god-module + `_cli_pkg._foo` (08 H5).
- `core/` `_ppo_selector_cls` selector-class indirection for `isinstance` test patches
  (03 M3 remainder; the `_LoggerProxy` half was already deleted in the consolidation wave).
- `session_path.py` public free-function shims (10 M2).
Remedy across the board: migrate tests to patch symbols at their real homes, delete the shims.

### T4. God-objects sharded to a per-file LOC budget
- `reports/collector.py` — namespace-class of static helpers (10 H1).

(`core/base.py` — the 44-field, ~50-stub orchestrator across 7 mixins — was dissolved
into `_host`-protocol composition by the consolidation wave; 03 C2 closed.)

### T5. Stringly-typed reconstruction of types thrown away at the boundary
- `MaskReason` impersonates `str` so substring matching survives, re-introducing the
  free-text the typed `classification` field was created to kill (04 M4 + L3).
- `last_error_class` magic strings not yet promoted to a StrEnum (04 M3 remainder).

### T7. Files still over ~1000 LOC (decomposition targets)
The big-3 cover `observation.py` and `collector.py` (`base.py` and `loop.py` were
decomposed by the consolidation wave). Others remain large but were not in
decomposition scope: `executor.py`, `sidecar/server.py`, `completion.py`,
`cli_agent.py`, `selector.py`, `_parsers.py`, `phases.py`.

---

## Open findings by bucket

Each item: severity · location · one-line problem · why it's deferred. Full detail in the referenced `.tnqa/` file.

### Bucket 01 — `plays/executor.py` + `plays/candidates.py` · `.tnqa/01_plays_core.md`
- **M2** — 4-way duplicated CODE_REVIEW/PR candidate builders → consolidate. *(re-architecture; behavior-change risk in reviewer-pinning.)*
- **M3** — 76-line procedural `_reconcile_issue_pickup_publish` → extract a reconciler. *(recovery state machine; design judgment.)*
- **M4** — 11-phase `isinstance`-sentinel `execute` lifecycle → typed dispatch. *(touches the PPO dispatch control flow.)*
- **L2** — duplicate issue/PR number+URL parsers → one coercer. *(divergent signatures/regexes; not behavior-preserving-trivial.)*

### Bucket 02 — `plays/` rest + `resolver.py` + `play_rules.py` · `.tnqa/02_plays_rest.md`
- **M2** — `_resolve_override` second hand-rolled dispatcher → table-driven. *(behavior-change risk.)*
- **M5** — two divergent PlayParams serializers → unify. *(the inline one is an intentional smaller projection; reconciling changes `context.json`.)*
- **L2** — type `needs_review(pr)` + collapse SHA logic. *(proposed collapsed tail is NOT behavior-equivalent; also crosses into `candidates.py`.)*

### Bucket 03 — `core/` · `.tnqa/03_core.md`
- **M3 (remainder)** — `_ppo_selector_cls` selector-class indirection still used for `isinstance` test patches. *(needs the H5-style test migration — T1; the `_LoggerProxy` half is done.)*
- **L2** — ~21 tests bypass `Orchestrator.__new__`, blocking deletion of the `getattr(self,"_x",default)` guards. *(C2 now makes this possible, but deferred: high churn, low value, and one guard may protect real optionality.)*

(C2, H2, M1, M2, M5, L1, L3, L5 were all landed by the core-consolidation wave; L3 was
resolved as "keep both WAL-lag shadows + add regression tests" — the deletion premise
was found half-invalid.)

### Bucket 04 — `rl/` · `.tnqa/04_rl.md`
- **H2** — `observation.py:90-578` declarative `FeatureBlock` registry. *(large rewrite; observation version wire-pinned.)*
- **M2** — `confirm()` rebuilds plan+mask up to 22×/tick. *(perf-sensitive hot path.)*
- **M3 (remainder)** — `last_error_class` magic strings → StrEnum. *(cross-module design change; the `.status.value` portion is done.)*
- **M4** — `MaskReason` str-impersonation shim → route consumers through `classification` (with **L3** `_only_capacity_waiting` substring matching).
- **M5** — duplicated loop-escalation ladders. *(design-level coupling.)*
- **M6** — overlapping config-mask filters. *(design-level coupling.)*
- **L5** — `training.py` None re-checks. *(load-bearing for mypy-strict narrowing.)*
- **L6** — `INDEX_TO_PLAY` redundancy. *(Minor; deletion would rewrite public-API assertions.)*

### Bucket 05 — `data/` · `.tnqa/05_data.md`
- **M4** — schema-derived `reset_session_scoped_tables`. *(would newly truncate `worktrees`/`dispatch_replay` — behavior change, needs a design call.)*
- **L2** — `Literal` + `_VALID_*` column guards. *(adds boundary validation that can raise on previously-tolerated values.)*

### Bucket 06 — `agents/` · `.tnqa/06_agents.md`
- **M1** — 320-line `dispatch_cli` decomposition. *(large.)*
- **L1** — `--resume` JSON-retry path. *(actively retained per the desktop-dy2j comment, not vestigial.)*
- **L2** — `on_spawned`/`on_exited` closures. *(cosmetic; capture the per-dispatch handle, so `functools.partial`-in-`__init__` doesn't apply.)*

### Bucket 07 — `sidecar/` + `ipc/` · `.tnqa/07_sidecar_ipc.md`
- **M1** — 95-line `session.stop` teardown duplicated by `_supervise`. *(touches the build-ESR-before-store-close ordering invariant.)*
- **M3** — inbound IPC loop parse/validate/route split. *(re-architecture; connection-loop control flow.)*
- **M4 (remainder)** — token-source if/elif chains → `TokenSource` enum + resolver dict. *(the keyring double-`except` dedup is done; the enum is the larger, riskier part.)*
- **M5** — two competing yaml writers (ruamel comment-preserving vs PyYAML comment-stripping). *(behavior-divergent — changes whether user comments survive.)*
- **L1** — `issue_availability` alias removal. *(dashboard `TopBarHud.tsx`/`types.ts`/e2e still read it as a fallback.)*
- **L2** — per-dispatcher `except Exception` guards. *(depend on the deferred C1 unified serve loop.)*
- **L3** — `os.chdir` cwd anchor → explicit `cwd=`. *(behavior-divergent process supervision.)*

### Bucket 08 — `cli/` · `.tnqa/08_cli.md`
- **H5** — `cli/__init__.py:1-237` re-export module + `_cli_pkg._foo` indirection (122 test patches) → delete + repoint. *(see T1.)*
- **M1** — `init()` 5-phase inline wizard with repeated `if not install_skills_only`. *(large.)*
- **M2** — two incompatible `_str_or_none`. *(clean fix lands in `config/` or is H5 territory.)*
- **M3** — `_find_free_dashboard_port` reinvents `find_free_tcp_port`. *(re-export-coupled; the 9400-range affinity may be intentional for the dashboard.)*
- **L1-L4** — all gated on the H5 `cli/__init__` re-export slimming.

(M4 — `_dispatch_command`'s elif reach-ins — was closed by the consolidation wave: the
orchestrator now exposes `refresh_issues`/`abort_in_flight`/`generate_report`/`archive_session`
and `cli/runtime.py` routes through them.)

### Bucket 09 — `ui/` + `dashboard/` python · `.tnqa/09_ui_dashboard.md`
- **M4** — `app.py` mutates `_latest_state` in place (shadow state in the view layer). *(it's a latency optimization; removing it is not behavior-preserving.)*
- **L2 (remainder)** — `escalation.py` mount-anchor refactor. *(changes widget mounting/binding semantics; the import hoist is done.)*
- **L3** — `bridge.py` `_ = broadcast` contract ambiguity. *(needs live-vs-prime judgment.)*
- **L4** — `bridge.py` dual `socket_path`/`ipc_endpoint` constructor args. *(public signature across 3 call sites + tests.)*

(M3 — `MainDashboard` recomputing `loop_level_for_streak` in the render path — was closed
by the consolidation wave: `OrchestratorState` now carries a precomputed `loop_level`.)

### Bucket 10 — `reports/` + `skills/` + `config/` + `github/` + `beads/` + root · `.tnqa/10_reports_skills_misc.md`
- **H1** — `reports/collector.py` ~1300-line god-class decomposition. *(large; see T4.)*
- **M2 (main)** — `session_path.py` public free-function shims (`request_drain`, `stop_dashboard_process`, `hard_stop_session`, `cleanup_session`, `stop_session`) → class/API decision. *(actively called + test-patched; needs caller and patch-target rewrites. The two dead private aliases are already deleted.)*
- **M3** — `_PLAY_LOG_ORDER` registry derivation. *(couples the report module to `rl/action_space.py`; risks report ordering/values.)*
- **L2** — beads parser alias trim. *(needs a real `bd --json` payload to verify safely; cannot run `bd` per CLAUDE.md.)*
