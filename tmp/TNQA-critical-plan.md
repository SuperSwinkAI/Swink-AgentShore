# Plan — Remaining Critical TNQA findings

## Context

Batch 1 (`fix/tnqa-quick-wins`) cleared five Criticals (pricing, budget validator, atomic issue-close, dead `merge_pr`, schema-doc) and Codex's concurrent pass cleared the action-space-table Critical plus a swath of Low/Medium items. **Three Critical findings remain**, and they live in mostly disjoint directories (`core/`, `plays/`, `rl/`), so they parallelize the same way batch 1 did. The asymmetry: **P1 (SessionRuntime) is a large, invasive refactor and the long pole**; P2 and P3 are contained.

Remaining Criticals:
1. **SessionRuntime** — collapse `host=self` + 6 `_*Host` Protocols + the `getattr(self._host, …)` guards into one owned state object (`core/`).
2. **Candidate dedup** — unify the two parallel `PlayCandidate` implementations (`plays/candidates.py`).
3. **POLICY_VERSION naming** — disambiguate the three version integers and where the weights file is named (`rl/`).

---

## P1 — SessionRuntime (core/) — the big one

**Problem.** The mixin→composition refactor separated the 8 components but not the shared mutable state: ~40 orchestrator latches are read/written through `self._host.*`, guarded by `getattr(self._host, "_x", default)` because tests build the host via `__new__`. The 6 `_*Host` Protocols are a typed index of the spaghetti, not a fix.

**Files.** `core/base.py` (the `_OrchestratorBase` field wall + the 6 `host=self` constructions), `core/mixins/{completion,loop,dispatch,drain,state,lifecycle,snapshots}.py`.

**Approach (sequence within the workstream — do NOT do it all in one diff):**
1. **Introduce `core/session_runtime.py`** — a `@dataclass` `SessionRuntime` owning the genuinely-shared latches the Protocols enumerate: `draining`, `drain_reason`, `drain_initialized`, `stop_requested`, `stopped`, `idle_streak`, `last_selection_digest`, `natural_exit_reason`, `pause_event`, `pause_deadline`, the budget-override fields, in-flight/dispatch maps. (Derive the exact set from the `_*Host` Protocol bodies — they already list every cross-component field.)
2. **Construct one `SessionRuntime` in `_OrchestratorBase.__init__`** and pass it to each component instead of `host=self`. Keep `host` temporarily for any non-latch method access (`_safe_call`, `begin_drain`) — those are behavior, not state; inject them as callables/refs.
3. **Migrate components one at a time** (completion → loop → drain → dispatch → state → lifecycle → snapshots): replace every `self._host.<latch>` read/write and every `getattr(self._host, "_x", default)` with `self._runtime.<x>`. Run that component's targeted tests after each.
4. **Delete the 6 `_*Host` Protocols, the `getattr`/`hasattr` re-init blocks, and the `base.py:77-108` defaults wall.**
5. **Fix tests** that build the orchestrator via `__new__` to construct a real `SessionRuntime` (a small test-helper factory).

**Risk: HIGH.** Touches the hottest path (tick loop, drain, completion). The full suite is the safety net; every component migration must keep its targeted tests green before moving on. Recommend this gets a **dedicated worktree and its own careful pass** rather than being rushed alongside the smaller two.

**Tests.** `tests/` core/loop/drain/completion/lifecycle suites incrementally; full `uv run pytest tests/` at the end.

---

## P2 — Candidate dedup (plays/candidates.py)

**Problem.** `PlayCandidateAnalyzer.build()` (state-only) and `PlayCandidateService.candidates_for()` (with store/GitHub fallbacks) independently re-derive the same per-play candidate logic; CODE_REVIEW is triplicated; `pr_merge_ready`/`pr_reviewable` docstrings spend paragraphs explaining the "kept in sync by hand" mirror.

**Files.** `plays/candidates.py` (and its tests).

**Approach.** Make `PlayCandidateService.candidates_for(pt)` call `build_candidate_plan(state).candidates_for(pt)` first, then add ONLY the service-specific tail: reviewer-pinning (`target_agent_id`), store-backed PR list when state is stale, GitHub live fallback. Delete the primary duplicate loops (`_merge_pr_candidates`/`_unblock_pr_candidates` primary bodies; keep their `_github_*` tails). Collapse the three CODE_REVIEW paths into one "candidates → pin reviewer → fallback" pipeline.

**Risk: MEDIUM-HIGH.** Feeds both the RL mask and live claiming — behavior must be identical. Lean on the existing candidate/mask/resolver tests; add a test asserting `build()` and `candidates_for()` agree for each play type on a fixture state.

**Tests.** candidate/mask/resolver suites; full suite at the end.

---

## P3 — POLICY_VERSION naming (rl/)

**Problem.** Three version ints — `ACTION_SPACE_VERSION=13`, `OBSERVATION_VERSION=13`, `POLICY_VERSION=5` — and `POLICY_VERSION` (which gates the *config head*, not the play action space) names the canonical weights file `policy_v5.pt` while everything else is "13". The coincidental 13s disguise which version names the file.

**Files.** `rl/action_space.py` (`ConfigKey`, `MAX_CONFIG_INDEX_SIZE`, `build_config_index`, `POLICY_VERSION` at ~41-82), `rl/checkpoint_store.py` (filename + quarantine), `rl/selector.py`, `rl/observation.py`, `rl/policy.py` (importers).

**Approach — two parts:**
- **(a) Safe refactor (do this):** move `POLICY_VERSION`, `ConfigKey`, `MAX_CONFIG_INDEX_SIZE`, `build_config_index` out of `action_space.py` into a new `rl/config_head.py` — they're the config-head action space, not the play action space. Update importers. Pure move; low risk. This alone removes the "why is POLICY_VERSION in action_space.py" confusion.
- **(b) Filename disambiguation (DECISION REQUIRED — see below):** encode all three versions in the weights filename (e.g. `policy_a{A}_o{O}_p{P}.pt`) so a mismatch is visible on disk.

**Risk:** (a) LOW. (b) MEDIUM — `checkpoint_store.py` already quarantines `policy_v{N}.pt` where `N != POLICY_VERSION` → `policy_legacy_v{N}.pt`, so a filename-scheme change would treat the **existing canonical `policy_v5.pt` as legacy and quarantine it → effective cold-start of the shared trained lineage.**

**Tests.** `tests/test_rl_action_space.py`, checkpoint-store/selector tests; full suite.

---

## Open decision (blocks P3 part b only)

**Rename the weights file to encode all three versions?**
- **Yes** → clearest on-disk, but quarantines the current `policy_v5.pt` canonical (cold-start the shared lineage). Acceptable if the trained weights aren't precious.
- **No** → keep `policy_v{POLICY_VERSION}.pt`; do only P3(a) (move to `config_head.py`) and rely on the existing quarantine logic. Zero operational impact.

Recommendation: **do P3(a) now; defer P3(b)** unless you're fine cold-starting the canonical. (Note: the "no back-compat code" project rule argues against adding a dual-filename fallback, which reinforces "either rename-and-quarantine or leave the scheme alone.")

---

## Execution model

Consistent with batch 1 — disjoint dirs → parallel worktree agents, main agent owns the authoritative gate + merge:

- **Now (low-risk, parallel):** spawn **P2** and **P3(a)** as two worktree-isolated agents. Each edits its files via `Edit`, runs targeted tests (`uv run pytest <paths> -n0` — note `-p no:xdist` errors against this repo's `addopts`; use `-n0` for serial), commits in-worktree.
- **Separately (high-risk, careful):** run **P1 (SessionRuntime)** in its own worktree with the staged sequence above, migrating one component at a time. Given its blast radius, consider driving it directly rather than fully hands-off, and gate it on its own.
- **Integration:** main agent merges the conflict-free worktree branches into a fresh branch off `integration` (e.g. `fix/tnqa-criticals`), then runs the full gate: `uv run pytest tests/`, `uv run ruff check src/ tests/`, `uv run ruff format --check src/ tests/`, `uv run mypy src/`.
- **Coordination:** check `git status` for a concurrent Codex pass before branching (batch 1 collided on shared files). P1/P2/P3 dirs (`core/`/`plays/candidates.py`/`rl/`) are the likely overlap surface.
- **Rules:** never run any `agentshore` CLI; branch off `integration` (never commit to it directly); subagents edit + targeted-test only, main agent owns the full gate + commit.

## Verification

1. Full suite green (`uv run pytest tests/`), coverage ≥ 80%.
2. `ruff check`, `ruff format --check`, `mypy src/` clean. *(Pre-existing: `plays/resolver.py` fails `ruff format --check` independent of this work — fix opportunistically or leave noted.)*
3. P1: grep shows zero `getattr(self._host` and zero `_*Host` Protocol classes; `SessionRuntime` is the single owner of the migrated latches.
4. P2: a test asserts `build()` and `candidates_for()` agree per play type; no `_merge_pr_candidates` primary duplicate remains.
5. P3: `POLICY_VERSION`/`build_config_index` import from `rl/config_head.py`; `action_space.py` no longer defines them.
