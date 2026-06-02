## Bucket 05: data/ (stores, schema, migrations)

Scope: all of `src/agentshore/data/` (28 files, 1402 LOC excl. worktree registry) plus
`src/agentshore/agents/worktree/registry.py` (which is the de-facto 22nd-table store).

Review verdict up front: the layer is *clean per-method* but suffers from one massive,
mechanical duplication axis (the column list × the `INSERT/SELECT/_row_to_X` triple,
repeated ~17 times) and — far more seriously — that duplication has already produced two
**write-only columns** where data is persisted but silently dropped on read. The
`base_ref` case is a live functional bug, not cosmetic. There is no base-store / row-mapper
abstraction; the mixin split is real isolation but it scattered the column-name source of
truth across 28 files with no single owner, which is exactly how `base_ref` and
`mask_reason` rotted.

---

## Critical

### C1. `pull_requests.base_ref` is write-only — persisted but never read back (silent functional bug)
**Severity:** Critical
**Location:**
- written: `data/store/helpers.py:21,50,99` (upsert SQL + COALESCE + row tuple)
- dropped on read: `data/store/mixins/pull_requests.py:193-204,208-225,229-247,261-278,296-320` (all 5 SELECT column lists omit `base_ref`)
- silently swallowed: `data/store/rows.py:246-247` reads `head_sha`/`mergeable` but **never reads `base_ref`** — and the guarded siblings use `row["x"] if "x" in keys else None`, so a missing column degrades to `None` with no error.
- model field: `data/models.py:121` (`base_ref: str | None = None`)

**Problem:** `record_pull_request` / `cache_pull_requests` write `base_ref` and the
`ON CONFLICT` clause preserves it. But every read path (`get_pull_request`,
`list_open/active/recently_merged/approved_pull_requests`) selects a fixed 21-column list
that excludes `base_ref`, and `_row_to_pull_request` never assigns it. Result:
`PullRequestRecord.base_ref` is **always `None` for any PR loaded from the DB**.

This is not dead code — `base_ref` is actively consumed by live policy logic:
- `rl/eligibility.py:210-212` — base-ref-drift eligibility gate
- `plays/executor.py:1056,1178-1190` — `retarget_base` mutation decision
- `plays/candidates.py:281-286` — candidate filtering by base branch
- `core/mixins/snapshots.py:216` — state projection into `state.base_ref`

All of these see `None` whenever the PR came through the store rather than a fresh adapter
fetch. The base-ref retarget / drift feature is silently inert on cached PRs. The
`row["base_ref"] if "base_ref" in keys` defensive style is what *hid* this — a hard
`row["base_ref"]` access would have raised on first read and the bug would never have
shipped.

**Code-judo remedy:** Two-part fix that also kills the duplication root cause.
1. Add `base_ref` to the shared column list (see C2 — define it once).
2. In `_row_to_pull_request`, read `base_ref` unconditionally and **delete every
   `"x" in keys` guard** (they are dead defensiveness — see H1). Net: one new column
   reference, ~15 guards removed, one live feature un-broken.

---

### C2. The PR column list is duplicated verbatim 6× and is the single point of failure that produced C1
**Severity:** Critical
**Location:** `data/store/mixins/pull_requests.py:193-197, 210-214, 231-235, 263-267, 298-302` (5 identical SELECT column lists) + the upsert in `data/store/helpers.py:16-22` + the tuple builder `helpers.py:79-102` + the reader `rows.py:224-250`.

**Problem:** The PR schema has 22 columns. The exact same 21-column projection is hand-typed
five times across the read methods, plus a sixth time (with the *correct* 22 columns,
including `base_ref`) in the upsert. The five readers and the one writer disagree about
the column set — that disagreement *is* bug C1. Any future column addition requires editing
6 locations in 2 files with no compiler help; the next addition will rot exactly like
`base_ref` did.

**Code-judo remedy:** Define the projection **once** and derive everything from it.
- Add `_PULL_REQUEST_COLUMNS: tuple[str, ...]` (the canonical 22-column ordered list) next
  to `_PULL_REQUEST_UPSERT_SQL` in `helpers.py`.
- Build the SELECT prefix once: `_PR_SELECT = f"SELECT {', '.join(_PULL_REQUEST_COLUMNS)} FROM pull_requests"`.
- The 5 read methods become `f"{_PR_SELECT} WHERE ... ORDER BY ..."` — the column list
  vanishes from all 5.
- `_row_to_pull_request` indexes positionally/by-name against that same tuple.

Removes ~80 lines of duplicated column text, makes the upsert/select/read column sets
**structurally identical**, and makes C1-class bugs impossible (a missing column is a
single-list edit, not a 6-site coordination problem).

---

### C3. `rl_experience.mask_reason` is write-only — same pattern as C1
**Severity:** Critical (data-integrity class; functional impact is diagnostics-only, so judge severity by the *pattern*, which is identical to C1)
**Location:**
- written: `data/store/mixins/rl.py:28,43` (`record_experience` INSERTs `mask_reason`)
- dropped on read: `data/store/mixins/rl.py:133-141, 149-157` — **both** `iter_experience_for_replay` SELECTs omit `mask_reason`
- never assigned: `data/store/rows.py:342-359` (`_row_to_experience` has no `mask_reason=`)
- model field: `data/models.py:282`

**Problem:** Migration v2→v3 (`migrations/__init__.py:42-60`) and a baseline column exist
solely to persist `mask_reason` (the dominant per-tick mask summary, produced by
`core/experience_recorder.py:246` for post-hoc "why wasn't merge_pr selected?" analysis).
It is faithfully written and then **never read** — `ExperienceRecord.mask_reason` is always
`None` on replay. The entire v2→v3 migration + the recorder plumbing is dead-on-read.
Lower functional blast radius than C1 (diagnostics, not policy), but it is the *same latent
defect* and proves the pattern is systemic, not a one-off.

**Code-judo remedy:** Either (a) add `mask_reason` to both replay SELECTs and to
`_row_to_experience`, finishing the feature; or (b) if post-hoc mask analysis was abandoned,
**delete the column, the migration v2→v3, the model field, and the recorder write** — that
removes a whole migration step and ~10 lines of recorder plumbing. Pick one; the current
"write but never read" state is the worst of both.

---

## High

### H1. `_row_to_pull_request` defensive `"x" in keys` guards are dead — and they hide bugs (see C1)
**Severity:** High
**Location:** `data/store/rows.py:202-250` (every `row["x"] if "x" in keys else …`); also `_row_to_agent_record` `rows.py:77-90`, `_row_to_github_issue` `rows.py:294`.

**Problem:** `_row_to_pull_request` is called from exactly 5 sites (verified:
`pull_requests.py:204,225,247,278,320`), all of which select an identical fixed column set.
The ~15 `"x" in keys` existence checks therefore guard against a row shape that can never
occur. They are not robustness — they are the mechanism that converted "I forgot to select
`base_ref`" (a crash) into "`base_ref` is silently `None`" (a shipped bug). The comments
claim back-compat with "older DBs", but the schema is `CREATE ... IF NOT EXISTS` +
forward-only migrations — every column in these readers exists in every supported DB.

**Code-judo remedy:** Delete every `"x" in keys` guard in `_row_to_pull_request`,
`_row_to_agent_record`, and `_row_to_github_issue`; read columns directly. If a column is
genuinely optional across DB generations, that belongs in a migration, not in a per-read
`try`. Removes ~25 conditional expressions and turns column-list drift back into a loud
`KeyError` at the first read (which is what you want). `_row_to_agent_record`'s
`model_tier/display_name/dispatch_count` guards (rows.py:88-90) are covered by baseline
schema + the migration story — drop them too.

### H2. INSERT/UPDATE/commit + manual field-tuple boilerplate repeated across all 16 mixins with no base abstraction
**Severity:** High
**Location:** Every mixin: e.g. `sessions.py:26-53`, `plays.py:25-60`, `agents.py:21-46`, `learnings.py:22-46`, `feedback.py:21-42`, `trajectory.py:21-40`, `scope.py:21-37`, `archive.py:21-43`, `external_mutations.py:19-40`, `review_patterns.py:21-49`. The exact shape — `INSERT INTO T (cols…) VALUES (?…)` then a hand-aligned `(record.field, …)` tuple then `await self._conn.commit()` — appears ~30 times.

**Problem:** There is genuinely *no* shared insert helper. Each mixin re-implements:
manual column list, manual `?`-placeholder count (must match by eye), manual field tuple in
matching order, `commit()`, and (for autoincrement tables) the identical
`if cursor.lastrowid is None: raise RuntimeError("INSERT did not return a row ID")` block —
which is copy-pasted **verbatim** in `plays.py:57-59`, `rl.py:51-53`, `rl.py:73-75`,
`feedback.py:39-41`, `learnings.py:43-45`, `work_claims` (RETURNING variant elsewhere). The
field-order coupling between the column list and the value tuple is exactly the fragility
that bit C1/C3 — it is enforced only by careful reading.

**Code-judo remedy:** Add a tiny insert helper on `_DataStoreBase` (the natural home, it
already owns `_conn`):
```python
async def _insert(self, table: str, **cols: object) -> int:
    names = ", ".join(cols)
    qs = ", ".join("?" * len(cols))
    cur = await self._conn.execute(
        f"INSERT INTO {table} ({names}) VALUES ({qs})", tuple(cols.values()))
    await self._conn.commit()
    if cur.lastrowid is None:
        raise DatabaseError(f"INSERT into {table} returned no row id")
    return cur.lastrowid
```
With `**cols` keyed by column name, the column-list-vs-value-tuple drift class is gone by
construction (you can't misalign a dict). Mixin inserts collapse from ~25 lines to ~3.
Combined with C2's column-list-once approach for the read side, you can delete the
duplicated `lastrowid is None` block from 5+ sites and ~150 lines of insert boilerplate
net. (Note: `**cols` dicts preserve insertion order in 3.12, so column order is stable.)

### H3. The `worktrees` table is a second, parallel store implementation outside the DataStore mixin pattern
**Severity:** High
**Location:** `agents/worktree/registry.py:84-153` and the rest of that file — free functions reaching into `store._conn` with `# noqa: SLF001` (e.g. `registry.py:110,135`); the table itself is defined in `data/schema.sql:332-355` alongside the other 21.

**Problem:** 21 tables go through `data/store/mixins/*`; the 22nd (`worktrees`) is I/O'd by
standalone functions in a *different package* that bypass the store API and poke `_conn`
directly. This is two competing conventions for the same job:
- Mixins: hand-written SELECT column lists, `aiosqlite.Row` dict access, `"x" in keys`
  guards, no status validation.
- Registry: `INSERT … RETURNING` (cleaner, no `lastrowid` dance), typed `WorktreeRow`
  frozen dataclass, an explicit `status not in _VALID_STATUSES` `ValueError` guard
  (`registry.py:64-66`) — i.e. *better* than the mixins.

So the codebase already contains the better pattern (typed rows + `RETURNING` + validation)
but applied to only one table, in the wrong package, behind `SLF001` suppressions. This
both fragments the data layer and means the worktrees row mapper can't be found by anyone
auditing `data/`.

**Code-judo remedy:** Move `registry.py`'s I/O into a `_WorktreesMixin` under
`data/store/mixins/worktrees.py`, mapping rows via the existing `WorktreeRow` (move it to
`models.py`). Drop the `SLF001` noqas (it's now a real mixin method using `self._conn`).
Then **adopt the registry's better idioms across the layer**: prefer `INSERT … RETURNING`
over the `lastrowid is None` block (lets you delete that copy-pasted guard from H2), and add
the same kind of status-`ValueError` guard for the other status-bearing tables
(`work_claims`, `review_queue`). Net: the data layer has one home and one convention, and
the strictest existing pattern wins.

---

## Medium

### M1. `get_dispatch_replay` returns an untyped `dict[str, object]` — the one row-shape with no DTO
**Severity:** Medium
**Location:** `data/store/mixins/work_claims.py:370-388` (returns `{k: row[k] for k in keys}`); consumed at `core/mixins/completion.py:558`.

**Problem:** Every other table has a typed `*Record`/`*Row` DTO. `dispatch_replay` alone is
returned as a stringly-keyed dict, so the consumer in `completion.py` accesses fields by
string literal with no type safety and no single definition of the shape. `dispatch_replay`
is a normal table (`schema.sql:182-194`) with a clear column set.

**Code-judo remedy:** Add a `DispatchReplayRecord` dataclass to `models.py` and a
`_row_to_dispatch_replay` in `rows.py`; return it. Removes the bespoke dict-comprehension
and gives the one untyped read path the same guarantees as the other 16.

### M2. `_ACTIVE_WORK_CLAIM_STATUSES` placeholder-expansion + `"\n".join((...))` SQL assembly repeated ~10× in work_claims.py
**Severity:** Medium
**Location:** `data/store/mixins/work_claims.py:181-197, 199-225, 227-255, 257-291, 293-334, 416-445, 447-472, 473-488`; also `plays.py:137-192` for the same status-set expansion.

**Problem:** Eight+ methods independently build
`",".join("?" for _ in _ACTIVE_WORK_CLAIM_STATUSES)` and then assemble SQL via
`"\n".join((...))` line tuples, splicing the placeholder string into a `status IN (...)`
clause and re-passing `*_ACTIVE_WORK_CLAIM_STATUSES` as params. It's correct but extremely
repetitive and the `"\n".join` line-tuple style is harder to read than a triple-quoted
string with an interpolated placeholder var.

**Code-judo remedy:** Two small helpers in `base.py` or a `work_claims` module scope:
`_active_status_clause() -> tuple[str, tuple[str,...]]` returning
`("status IN (?,?,?,?)", _ACTIVE_WORK_CLAIM_STATUSES)`, and use plain triple-quoted SQL with
the clause f-string-spliced. Collapses ~8 repeated 3-line placeholder builders and
de-`"\n".join`s the queries. Low risk (pure refactor), meaningful readability win on the
single most complex mixin (488 LOC).

### M3. Manual JSON (de)serialization scattered across mixins/rows with three different failure policies
**Severity:** Medium
**Location:** writes — `plays.py:53,105`, `issues.py:92`, `pull_requests.py:89,111`, `helpers.py:75,89`. reads — `rows.py:173-199` (`_decode_artifacts`, full defensive), `rows.py:208-223` (PR labels/links, try/except→default), `rows.py:292-293` (`_row_to_github_issue` labels: bare `json.loads(raw_labels)` with **no** try/except).

**Problem:** JSON columns (`artifacts`, `labels`, `linked_issue_numbers`) are encoded with
ad-hoc `json.dumps(x) if x else None` at 7 sites and decoded with **three different**
robustness levels: `_decode_artifacts` is fully defensive; PR labels swallow
`JSONDecodeError`→`[]`; but `_row_to_github_issue` (rows.py:293) does a bare
`json.loads(raw_labels)` that will **raise** on a malformed value and blow up the entire
`get_open_issues`/`list_all_issues` read. Inconsistent failure policy across columns of the
same logical type.

**Code-judo remedy:** Two helpers in `rows.py`: `_dump_json_list(x) -> str | None` and
`_load_json_str_list(raw) -> list[str]` (defensive: bad JSON / non-list → `[]`). Route all
label/list columns through them. Unifies the 7 write sites and 3 divergent read sites to one
policy, and closes the `_row_to_github_issue` crash hole.

### M4. `reset_session_scoped_tables` table list is a hard-coded SQL string that silently drifts from the schema
**Severity:** Medium
**Location:** `data/store/core.py:237-251` (13 hard-coded `DELETE FROM`); the preserved-vs-truncated split is documented only in a docstring (`core.py:226-229`).

**Problem:** The session-scoped truncation enumerates 13 tables by name in a SQL literal.
The schema has 22 tables; the split between "truncate" and "preserve" lives nowhere except
this string + a prose docstring. Add a new session-scoped table and you must remember to add
a `DELETE` here — nothing enforces it. Notably `worktrees` (a session-scoped table,
`schema.sql:334`) is **not** in the reset list, and `dispatch_replay` (also session-scoped)
isn't either — whether that's intentional is impossible to verify from code.

**Code-judo remedy:** Make the partition explicit and machine-checkable. Define
`_PRESERVED_TABLES: frozenset[str]` (the cross-session set named in the docstring) and
derive the truncation set from the live `sqlite_master` table list minus preserved minus
`schema_*`. Then a new table is truncated-by-default (the safe direction) unless explicitly
preserved, and the audit lives in one named set instead of a SQL string. Also resolves the
open question of whether `worktrees`/`dispatch_replay` should be reset.

---

## Low

### L1. `import structlog` / `import os` done lazily inside methods
**Severity:** Low
**Location:** `core.py:146,274,304,320` (`import structlog`, `import os` mid-function); `pull_requests.py:258`, `issues.py:166` (`from datetime import …` inside method).

**Problem:** Function-body imports for stdlib/structlog scattered through `core.py`'s
lifecycle methods. The module already imports plenty at top; these in-body imports are habit,
not necessity (no circular-import reason exists for `os`/`structlog`/`datetime`).

**Code-judo remedy:** Hoist to module top. Removes ~6 in-body import lines and the repeated
`import structlog` (appears 4× in `core.py` alone).

### L2. `mergeable` column documented as a 3-value enum but typed/stored as free `str | None`
**Severity:** Low
**Location:** `models.py:120` + `schema.sql:117` (`mergeable TEXT -- "MERGEABLE" | "CONFLICTING" | "UNKNOWN"`); same for `last_review_status` (`models.py:125`, "PASS"|"BLOCK"|None), `review_decision`, work-claim/review `status`.

**Problem:** Several columns are effectively enums (the valid set is in a comment) but are
plain `str | None` everywhere, so an invalid value is silently storable/readable. The
worktrees registry already demonstrates the better pattern (`Literal` type +
`_VALID_STATUSES` guard, `registry.py:23-28,64-66`); the mixin tables don't follow it.

**Code-judo remedy:** Promote the comment-documented enums to `Literal[...]` aliases in
`models.py` and add a `_VALID_*` guard in the corresponding `_row_to_*` (mirroring
`_row_to_worktree`). Catches bad writes at the boundary instead of letting them rot in the DB.

### L3. `CheckpointRecord` / `ReviewQueueRecord` have hand-inlined row mapping, breaking the `_row_to_*` convention
**Severity:** Low
**Location:** `rl.py:108-115` (`load_latest_checkpoint` builds `CheckpointRecord` inline instead of a `_row_to_checkpoint`); `external_mutations.py:53-63,124-134` (`ExternalMutationRecord` built inline at **two** sites with identical field lists).

**Problem:** Most tables have a `_row_to_X` in `rows.py`; checkpoints and external_mutations
instead inline the dataclass construction (external_mutations does it twice, duplicating the
9-field mapping). Inconsistent with the rest of the layer and duplicative within
`external_mutations.py`.

**Code-judo remedy:** Add `_row_to_checkpoint` and `_row_to_external_mutation` to `rows.py`;
call from the (3) inline sites. Removes one in-file duplication and restores the one-mapper-
per-table convention so future column adds have a single edit point.

---

## Cross-cutting note (docs, out of strict scope but flagged)
`CLAUDE.md` claims "schema version 13 / 21 tables / 22-action". The actual schema is
**version 3** (`schema.sql:363`, `migrations/__init__.py`), with **22 tables**
(`schema.sql` defines `worktrees` as the 22nd). The migration chain is only
`v1→v2→v3` (`core.py:169-170`). The version/table-count mismatch isn't a code defect but it
will mislead anyone reasoning about migration state; worth correcting the doc or, better,
asserting the count in a test so the doc can't drift.
