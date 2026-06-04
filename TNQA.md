# AgentShore — Open TNQA Backlog (ROI-ranked)

The still-open findings from the thermo-nuclear code-quality review, ordered by **value /
ROI** rather than by source file. **Zero confirmed live defects remain** — every item is
maintainability, type-safety, decomposition, or a behavior-divergent change deferred *by
design*.

Because nothing here is a live bug, **value** = future-bug prevention + unblocking other
work + readability, and **cost** = LOC + behavior/wire/perf risk. Full rationale and exact
line ranges live in `.tnqa/01..10_*.md` (referenced inline as `NN`).

**Open: 2 High · 12 Medium · 14 Low (28).** Last full suite (integrated `tnqa/staging`):
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

Each item: **`NN` severity** · `location` — problem → fix. *(deferral / ROI note.)*

---

## Tier 3 — Marginal / opportunistic

Real value but high cost or behavior-change risk — **do these when you're next editing the
file anyway**, not as standalone efforts.

- **`06 M1`** · `agents/cli_agent.py` — 320-line `dispatch_cli` decomposition. *(Large; hot
  file.)*
- **`01 M3`** · `plays/executor.py` — 76-line procedural `_reconcile_issue_pickup_publish`
  → extract a reconciler. *(Recovery state machine; design judgment.)*
- **`07 M1`** · `sidecar/` — 95-line `session.stop` teardown duplicated by `_supervise`.
  *(Touches the build-ESR-before-store-close ordering invariant.)*
- **`10 H1`** · `reports/collector.py` — ~1300-line static-helper god-class → decompose.
  *(Large, but low-risk: reports aren't hot-path.)*
- **`07 M3`** · `sidecar/server.py` — inbound IPC loop: split parse/validate/route.
  *(Re-architecture of the connection loop.)*
- **`08 M1`** · `cli/init.py` — 5-phase inline wizard with repeated
  `if not install_skills_only` → decompose. *(Large.)*
- **`08 L2`** · `cli/start.py` — stale/misnumbered phase comments (`-- 0.`, `-- 2.`,
  `-- 11a.`). *(Near-zero standalone; resolves for free with `08 M1`/`start()` decomp.)*
- **`01 M2`** · `plays/candidates.py` — 4-way duplicated CODE_REVIEW/PR candidate builders
  → consolidate. *(Reviewer-pinning behavior-change risk.)*
- **`02 M2`** · `plays/resolver.py` — `_resolve_override` second hand-rolled dispatcher →
  table-driven. *(Behavior-change risk.)*
- **`01 M4`** · `plays/executor.py` — 11-phase `isinstance`-sentinel `execute` lifecycle →
  typed dispatch. *(Touches PPO dispatch control flow.)*
- **`03 L2`** · `core/` — ~21 tests bypass `Orchestrator.__new__`, blocking deletion of the
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

**Suggested sweep:** Tier 1 + the two Tier-2 type-safety items (`04 M4`, `07 M4`) form one
coherent "type-safety & shim-removal" PR — same test-patch-migration mechanics throughout,
and it de-risks the recovery code that just shipped.
