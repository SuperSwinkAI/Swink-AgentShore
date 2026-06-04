# AgentShore — Open TNQA Backlog (ROI-ranked)

The still-open findings from the thermo-nuclear code-quality review, ordered by **value /
ROI** rather than by source file. **Zero confirmed live defects remain** — every item is
maintainability, type-safety, decomposition, or a behavior-divergent change deferred *by
design*.

Because nothing here is a live bug, **value** = future-bug prevention + unblocking other
work + readability, and **cost** = LOC + behavior/wire/perf risk. Full rationale and exact
line ranges live in `.tnqa/01..10_*.md` (referenced inline as `NN`).

**Open: 1 High · 6 Medium · 13 Low (20).** Last full suite (integrated `tnqa/staging`):
3207 passed / 0 failed / 2 skipped, 84% coverage, ruff + mypy strict clean.

**Tier-1 sweep merged** (branch `tnqa/typesafety-shim-removal`): `work_availability` re-export
shim deleted, `04 M3` `ErrorClass` StrEnum (14 members) landed, `10 M2` `SessionProcessController`
collapsed into its free functions. `03 M3` dropped as substantially resolved — the re-export wall /
`_core_pkg` dance / `_LoggerProxy` are already gone; the lone residual `_ppo_selector_cls()` is
load-bearing for torch-free cold start, not a test shim.

**Tier-2 sweep merged** (branch `tnqa/staging`): `04 M4/L3` — `MaskReason` str-impersonation
shim deleted, `_only_capacity_waiting` rewritten against typed `MaskReason` objects; new
`MaskSource.SPAWN` member distinguishes spawn-gate cooldowns from general `PRECONDITION`
reasons (deliberate severity change: instantiate-cooldown all-masked case moves warning →
debug). `07 M4` — `TokenSource` StrEnum + per-source resolver dict in `sidecar/identities.py`;
`_apply_source` helper deduplicates the two if/elif chains. `09 L4` — `DashboardBridge`
`socket_path` legacy arg removed; `ipc_endpoint: IpcEndpoint` is the single required kwarg.
`07 L1` — `issue_availability` same-value alias deleted from serializer, `StateUpdate` type,
`TopBarHud`, and e2e fixture.

**Tier-3 Phase A merged** (branch `tnqa/staging`): `10 H1` — `ReportDataCollector` god-class
decomposed into `reports/_repo_url.py`, `reports/_loop_incidents.py`, `reports/_aggregations.py`;
class keeps only the 4 public `collect_*` methods. `07 M3` — `ipc/server.py` inbound loop
duplicated `json.dumps+write+drain` blocks replaced with `_write_line` helper routing through
`wire.frame`. `01 M3` — `_reconcile_issue_pickup_publish` (~90 lines) extracted to
`IssuePickupPublishReconciler` collaborator in `plays/_publish_reconciler.py`. `02 M2` —
`_resolve_override` 5-branch if-chain replaced by `_OVERRIDE_SPECS` table (mirrors
`_SKILL_SPECS` from `dispatch.py`). `01 M4` — three `isinstance`-sentinel union-return guards
in `execute()` replaced by `_SkipDispatchError` exception; phase methods now `raise` instead
of returning `T | PlayOutcome`. `08 M1` — `init()` restructured with an early
`install_skills_only` return; `config_yaml` path computed once; four scattered
`if not install_skills_only:` guards eliminated. `06 M1` — `dispatch_cli` (~314 lines)
decomposed into `_build_dispatch_argv` / `_await_output_or_timeout` / `_finalize_nonzero_exit`
helpers; `_DispatchArgv` frozen dataclass packages argv + log-preview fields. `08 L2` closed
as moot — the stale phase comments (`-- 0.`, `-- 2.`, `-- 11a.`) were already removed by the
`session/bootstrap.py` extraction that shipped with the Tier-2 sweep.

Deferred to **Tier-3 Phase B**: `01 M2` (reviewer-pinning dedup — behavior-change risk at
anti-confirmation-bias call sites), `07 M1` (sidecar teardown merge — build-ESR-before-store-close
ordering invariant), `03 L2` (22-file `__new__` test migration — high churn, low value).

Each item: **`NN` severity** · `location` — problem → fix. *(deferral / ROI note.)*

---

## Tier 3 — Marginal / opportunistic (Phase B — deferred)

Phase A items are recorded in the "Tier-3 Phase A merged" note above. Three items remain,
deferred for high risk or churn relative to payoff:

- **`01 M2`** · `plays/candidates.py` — 4-way duplicated CODE_REVIEW/PR candidate builders
  → consolidate. *(Reviewer-pinning behavior-change risk: `pick_reviewer_for_pr`
  anti-confirmation-bias pinning has 3 call sites with differing `sort_key` tuples.)*
- **`07 M1`** · `sidecar/` — 95-line `session.stop` teardown duplicated by `_supervise`.
  *(Touches the build-ESR-before-store-close ordering invariant.)*
- **`03 L2`** · `core/` — ~22 tests bypass `Orchestrator.__new__`, blocking deletion of the
  `getattr(self,"_x",default)` guards. *(Unblocked, but high churn / low value, and one
  guard may protect real optionality.)*

## Tier 4 — Low ROI / defer

High cost or risk versus payoff, behavior-divergent (needs a design call), or essentially
won't-fix. Don't pick these up without a forcing function.

**Large / perf — only with a forcing function:**
- **`04 H2`** · `rl/observation.py:90-578` — imperative feature-packing → declarative
  `FeatureBlock` registry. *(Large rewrite + RL-correctness risk + observation version is
  wire-pinned — do only when bumping that version.)*
- **`04 M2`** · `rl/` — `confirm()` rebuilds plan+mask up to 22×/tick. *(Perf-sensitive hot
  path; no measured problem — only if profiling proves it.)*

**Behavior-divergent / design-call required:**
- **`05 M4`** · `data/` — schema-derived `reset_session_scoped_tables` would newly truncate
  `worktrees`/`dispatch_replay`.
- **`07 M5`** · `sidecar/` — two competing YAML writers (ruamel comment-preserving vs
  PyYAML comment-stripping) — changes whether user comments survive.
- **`02 M5`** · `plays/` — two divergent `PlayParams` serializers → unify (the inline one is
  an intentional smaller projection; reconciling changes `context.json`).
- **`09 M4`** · `ui/app.py` — in-place `_latest_state` mutation. *(A latency optimization;
  removal isn't behavior-preserving.)*
- **`10 M3`** · `reports/` — `_PLAY_LOG_ORDER` registry derivation from `rl/action_space.py`.
  *(Risks report ordering/values.)*
- **`04 M5`** · `rl/` — duplicated loop-escalation ladders. *(Design-level coupling.)*
- **`04 M6`** · `rl/` — overlapping config-mask filters. *(Design-level coupling.)*
- **`07 L3`** · `sidecar/` — `os.chdir` cwd anchor → explicit `cwd=`. *(Behavior-divergent
  process supervision.)*
- **`05 L2`** · `data/` — `Literal` + `_VALID_*` column guards. *(Adds validation that can
  raise on previously-tolerated values.)*
- **`02 L2`** · `plays/` — type `needs_review(pr)` + collapse SHA logic. *(Proposed collapsed
  tail is NOT behavior-equivalent; crosses into `candidates.py`.)*
- **`01 L2`** · `plays/` — duplicate issue/PR number+URL parsers → one coercer. *(Divergent
  signatures/regexes; not behavior-preserving-trivial.)*

**Minor / cosmetic / mypy-load-bearing:**
- **`04 L5`** · `rl/training.py` — None re-checks. *(Load-bearing for mypy-strict narrowing.)*
- **`04 L6`** · `rl/` — `INDEX_TO_PLAY` redundancy. *(Deletion rewrites public-API assertions.)*
- **`06 L2`** · `agents/` — `on_spawned`/`on_exited` closures. *(Cosmetic; capture the
  per-dispatch handle, so `functools.partial`-in-`__init__` doesn't apply.)*
- **`09 L2`** · `ui/escalation.py` — mount-anchor refactor remainder. *(Changes widget
  mounting/binding; the import hoist is done.)*
- **`09 L3`** · `dashboard/bridge.py` — `_ = broadcast` contract ambiguity. *(Needs
  live-vs-prime judgment.)*
- **`07 L2`** · `sidecar/` — per-dispatcher `except Exception` guards. *(Gated on the
  deferred unified serve loop.)*

**Won't-fix / blocked:**
- **`06 L1`** · `agents/` — `--resume` JSON-retry path. *(Actively retained per the
  desktop-dy2j comment — not vestigial.)*
- **`10 L2`** · `beads/` — parser alias trim. *(Blocked: needs a real `bd --json` payload to
  verify, and `bd` can't be run per CLAUDE.md.)*

---

**Suggested next:** Tier 3 Phase B (`01 M2` reviewer-pinning dedup) is the highest-value
remaining item, but needs its own focused PR given the anti-confirmation-bias invariants.
`07 M1` and `03 L2` follow the same pattern — high-effort moves best done as a forcing
function (next time the relevant files are open for a feature change).
