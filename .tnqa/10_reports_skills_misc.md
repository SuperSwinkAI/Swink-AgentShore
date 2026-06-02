## Bucket 10: reports/ + skills/ + config/ + github/ + beads/ + root modules

Scope reviewed (every .py): `reports/{collector,generator,types,__init__}.py`,
`skills/__init__.py` (templates/*.md are not code — skipped), `config/{_parsers,models,__init__}.py`,
`github/{adapter,labels,pr_links,trust,__init__}.py`, `beads/{__init__,setup}.py`, and root
modules `state.py, result_parser.py, pr_state.py, paths.py, logging.py, learnings.py,
identity_names.py, errors.py, environment.py, power.py, session_path.py, seed_input.py,
work_availability.py, utils.py, archive.py`.

Overall the bucket is in good shape: typed boundaries, narrow excepts, frozen dataclasses,
NDJSON logging. The two >1000-line files are the main structural smells, plus a genuine
silent-data-loss bug in `learnings.py` and a config double-source-of-truth.

---

### Critical

#### C1 — `learnings.py:110-162` — `decay()` and `reinforce()` silently drop the `scope` field (data loss)
**Problem.** `Learning` (line 29-39) has a `scope: str = "project"` field. Both `decay()`
and `reinforce()` rebuild a fresh `Learning(...)` enumerating fields by hand, but neither
passes `scope=`. Every entry that passes through decay or reinforce silently has its `scope`
reset to the `"project"` default. `decay()` additionally re-passes `last_reinforced_play_id`
but omits `scope`; `reinforce()` omits it too. Because these run every session
(decay-after-N, reinforce-on-pattern-match), any non-default scope (e.g. a "global" learning)
is destroyed on first decay/reinforce — a correctness bug, not a style nit. This is exactly
the manual-field-copy hazard that frozen dataclasses are supposed to avoid.
**Code-judo remedy.** Make `Learning` `@dataclass(frozen=True, slots=True)` and replace the
two hand-rolled rebuilds with `dataclasses.replace(e, confidence=..., sessions_since_use=...)`.
That guarantees every field (including `scope` and any future field) is carried forward.
Removes ~24 lines of field-by-field copying across the two functions and closes the bug class
permanently.

---

### High

#### H1 — `reports/collector.py` (1317 lines) — god-class aggregator; decompose into per-report modules
**Problem.** `ReportDataCollector` is a single 1100-line class with 4 public `collect_*`
methods and ~30 private `_compute_*` statics. The four reports share almost nothing at the
class level — they each call a disjoint subset of the `_compute_*` helpers — so the class is a
namespace, not an object (every helper is `@staticmethod`; `self` is used only to reach
`self._store`). The file mixes pure aggregation with subprocess I/O (`_git_remote_url`,
`_resolve_repo_url`) and a 100-line hand-written state machine (`_compute_loop_incidents`,
1996-1120) that is far denser than the rest.
**Code-judo remedy.** Split into a small package:
- `reports/collector.py` keeps only the 4 `collect_*` orchestration methods (the store fan-out
  + assembly of the TypedDicts) — ~250 lines.
- `reports/_aggregations.py` holds the `_compute_*` free functions (they are already static and
  pure) — they become module-level functions taking explicit args, dropping the fake `self`.
- `reports/_loop_incidents.py` holds the streak state machine as one cohesive unit (the nested
  `_emit`/`_classify_*` closures become module functions on a small `_StreakState` dataclass).
- `reports/_repo_url.py` holds `_git_remote_url`/`_repo_url_from_github_child_url`/
  `_normalize_repo_url` (the only subprocess/regex code in the file).
Net: no class shrinks below readability, the 100-line state machine is testable in isolation,
and the I/O is no longer buried in a "pure-data" module whose own docstring claims "No
dependency on … IPC".

#### H2 — `github/adapter.py:235-364` — `list_pull_requests` and `fetch_pull_request_by_number` duplicate ~50 lines of identical `PullRequestRecord` construction
**Problem.** The two methods build a `PullRequestRecord` from the same `gh pr` JSON shape with
the same `--json` field list (the field list string is itself copy-pasted verbatim at lines
250-254 and 322-326), the same `author.login` extraction, the same `infer_pr_issue_links` call,
and the same 18-field constructor. Any field added to the PR record (or the `--json` list) must
be edited in two places; they have already drifted in the past per the desktop-08a948ed comment.
**Code-judo remedy.** Extract `_PR_JSON_FIELDS: str` constant and a module-level
`_pr_record_from_json(self._session_id, item) -> PullRequestRecord | None` helper. Both methods
collapse to a fetch + map. Removes ~45 duplicated lines and the duplicated field-list string;
makes the record schema single-sourced.

#### H3 — `config/__init__.py:95-308` + `config/_parsers.py:382-1009` — defaults are encoded twice (embedded `_DEFAULT_YAML` vs. per-field `.get(key, DEFAULT)`)
**Problem.** Every default value lives in two places: the ~210-line `_DEFAULT_YAML` string
literal, and the `raw.get("...", DEFAULT)` second argument inside each `_parse_*` function
(e.g. `cost_per_1k_input` default `0.003` appears in both; `warn_after: 1`, `escalate_after: 7`,
`max_per_config: 2`, etc. all duplicated). They are kept in sync by hand. The brief flags
`_parsers.py` specifically as hand-rolled YAML→dataclass parsing with deep nested validation —
this double-source-of-truth is the concrete cost of that hand-rolling. `load_config(None)`
parses the embedded YAML through the same `_parse_*` path, so the per-field defaults are what
actually win; the YAML defaults only matter for the file `generate_default_config` writes — but
if the two drift, the generated config silently disagrees with runtime behavior.
**Code-judo remedy.** Two viable directions, pick one:
  (a) **Declarative schema** — define each config dataclass with its defaults *on the dataclass*
      (they largely already are in `models.py`), then drive parsing + coercion from a small
      table of `(yaml_key, field, coercer, validator)` per section instead of ~40 bespoke
      `_parse_*` functions. The validators (budget/rl/scope/worktrees range checks) become
      reusable coercers (`positive_int`, `unit_float`, `enum_of(...)`). This is the
      schema-driven parser the brief asks about and would cut `_parsers.py` from ~1090 to an
      estimated ~450 lines while removing the validation copy-paste (the
      "must be a non-negative integer" message template recurs 5×: lines 962, 968, 1005,
      836, 842).
  (b) **Generate `_DEFAULT_YAML` from the dataclasses** (`yaml.safe_dump` of a default
      `RuntimeConfig`) so there is exactly one source. Smaller change, kills the drift risk,
      but keeps the bespoke per-field parsers.
Recommend (a) for the validators; at minimum do (b) to kill the literal duplication.

#### H4 — `reports/collector.py:308-313, 233-256` — `total_cost` computed two different ways across reports (semantic inconsistency)
**Problem.** `collect_session_summary` uses `_compute_overview` which takes
`session.total_cost` (line 397). `collect_end_session_report` calls the same
`_compute_overview` then *overwrites* `overview["total_cost"] = sum(p.dollar_cost for p in plays)`
(line 313). So the two reports can report different total costs for the same session, depending
on whether `session.total_cost` and the play-sum agree. The override exists silently with no
comment; whichever is authoritative should be authoritative everywhere.
**Code-judo remedy.** Decide the single definition (play-sum is the safer, self-consistent one
since it's derived from the same rows the play log shows) and put it inside `_compute_overview`
so all four reports agree. Removes the post-hoc mutation of a TypedDict (an antipattern in
"pre-computed dicts ready for rendering") and the divergence.

---

### Medium

#### M1 — `config/_parsers.py:78-79, 614-631` — `_RawTrustedIds` TypedDict is out of sync with its parser
**Problem.** `_RawTrustedIds` declares only `github_logins`, but `_parse_trusted_ids` reads and
validates `raw.get("pr_allow_list", [])` (lines 614-629) and returns it on
`TrustedIdsConfig.pr_allow_list`. The TypedDict — whose stated purpose (module docstring) is to
type the raw YAML boundary — lies about the accepted shape; a typed consumer of `_RawTrustedIds`
has no idea `pr_allow_list` is supported. Same class of latent drift as H3.
**Code-judo remedy.** Add `pr_allow_list: list[object]` to `_RawTrustedIds`. One line; restores
the TypedDict as a truthful contract. (Worth a grep for other parsers reading keys absent from
their `_Raw*` TypedDict.)

#### M2 — `session_path.py:545-589` — backward-compat free-function shims duplicate the class API for no caller benefit
**Problem.** After `SessionProcessController` was introduced, the module keeps 6 module-level
wrappers (`request_drain`, `stop_dashboard_process`, `hard_stop_session`, `cleanup_session`,
`_process_alive`, `_signal_group`, `_terminate_process_tree`) that each just construct a
`SessionProcessController` and call one method, plus `stop_session = hard_stop_session`. Per the
project's "no legacy refs / no backward-compat code" rule (MEMORY.md), these "backward-compatible
free functions" are exactly the kind of compat layer that's disallowed.
**Code-judo remedy.** Grep callers; either (a) point them at the controller and delete the shims
(~45 lines), or (b) if the free-function form is genuinely the preferred public API, delete the
class and keep the functions — but do not keep both. The duplicated `_process_alive`/
`_signal_group`/`_terminate_process_tree` module aliases (lines 580-589) are pure indirection to
`@staticmethod`s and should go regardless.

#### M3 — `reports/collector.py:111-134` + `state.py:64-104` — `_PLAY_LOG_ORDER` re-encodes the PlayType registry as a parallel literal
**Problem.** `_PLAY_LOG_ORDER` hard-codes all 22 play types with their string value,
LABEL, action_index, phase, and future-flag. `PlayType` (state.py) and the action-space module
already own the value↔index mapping and which slots are FUTURE. The two must be kept in sync by
hand; the comment at collector.py:597-606 even narrates a past bug from a hardcoded denominator.
The action_index column here duplicates `rl/action_space.py`.
**Code-judo remedy.** Derive the order table from the action-space registry + a small
`{PlayType: (label, phase)}` presentation map (only the display label and phase grouping are
genuinely report-specific). The `future` flag and `action_index` come from the registry. Removes
the 22-row literal's drift risk and the bespoke `is_future`/internal-filter recomputation
scattered across `_compute_play_log_*` (lines 500-606).

#### M4 — `pr_state.py:41-150` — three near-identical recursive walkers over the statusCheckRollup payload
**Problem.** `status_rollup_has_failure`, `status_rollup_summary` (via `_status_values`), and
`_status_values` each independently recurse the arbitrary-nested `dict|list` rollup pulling
`status`/`conclusion`. Three traversals of the same structure with the same
`str(...).upper()` extraction.
**Code-judo remedy.** One `_collect_rollup_states(raw) -> set[str]` walker; `has_failure` becomes
`bool(states & FAILED_STATUS_STATES)` and `summary` consumes the same set. Collapses ~3 recursive
functions into 1 + two pure predicates (~30 lines → ~15), and they can no longer disagree about
what counts as a state.

---

### Low

#### L1 — `result_parser.py:177-271` — repeated "list-of-JSON-objects" coercion pattern (6×)
**Problem.** The blocks for `artifacts`, `issues_created`, `requested_mutations`,
`verification_evidence`, `review_patterns`, `issues_closed` each repeat the same
"if not list → [], iterate, `_json_object(item)`/coerce, append" shape. Not fragile (the
JSON-object extraction is robust, contra the brief's worry — this parser is solid), just verbose.
**Code-judo remedy.** A `_json_object_list(data, key) -> list[JsonObject]` helper for the four
object-list fields collapses ~40 lines to ~8. The int-list cases (`issues_created`,
`issues_closed`) share a `_json_int_list`. Optional polish, not a defect.

#### L2 — `beads/__init__.py:304-465` — `_parse_bead`/`_parse_epic_status` tolerate an unusually wide alias surface
**Problem.** The bead/epic parsers accept many alternative key spellings (`id`/`bead_id`,
`title`/`name`/`summary`/`epic_title`, `total_children`/`total`, nested `epic.*`). This is
defensive against bd revisions and is reasonable, but it's a lot of speculative fallback. If the
pinned bd version (`setup.py:39` `REQUIRED_BD_VERSION = "1.0.4"`) is enforced at init, most of
these aliases describe shapes that the pinned binary never emits.
**Code-judo remedy.** Since `_check_bd_version` already hard-pins bd, narrow the parsers to the
keys 1.0.4 actually emits and delete the speculative aliases (the comments claim "forward/back
compatibility" — but the version pin makes that compat unreachable). Verify against a real
1.0.4 `bd list --json` before trimming. Defer if bd unpinning is on the roadmap.

#### L3 — `skills/__init__.py:73-83` — hand-rolled version comparison instead of `packaging.version`
**Problem.** `_should_overwrite` parses `agentshore_version` into `tuple(int(x) ...)` and falls
back to raw string compare on `ValueError`. The docstring even says "packaging-style version
strings". A real version like `1.2.0rc1` or `1.2.0.dev3` hits the `ValueError` path and does a
lexicographic string compare, which is wrong (`"1.2.0" > "1.10.0"` lexically).
**Code-judo remedy.** `from packaging.version import Version` and compare `Version(source_ver) >
Version(existing_ver)`. Removes the int-tuple/string-fallback branch (~10 lines) and handles
pre-release/dev stamps correctly. (`packaging` is already a transitive dep via hatch/pip tooling;
confirm it's a runtime dep before relying on it.)

#### L4 — `paths.py:24` vs `session_path.py:124` docstring drift
**Problem.** `session_dir`'s docstring says `~/.config/swink/agentshore/sessions/<hash>/` but the
path is computed from `GLOBAL_SESSIONS_DIR` = `platformdirs.user_config_dir("agentshore","swink")`,
which is *not* `~/.config/swink/agentshore` on macOS (it's `~/Library/Application Support/...`).
Pure doc nit; the code is correct.
**Code-judo remedy.** Replace the hardcoded literal in the docstring with a reference to
`GLOBAL_SESSIONS_DIR` / platformdirs so it can't drift per-platform.

---

### Notes / non-findings (explicitly checked, no action)
- **Report templates are clean of legacy project names** — grep for `swink-desktop`,
  `RLDevAgent`, etc. across `reports/templates/` returned nothing; only `agentshore` appears.
  The hard project rule is satisfied.
- **`result_parser.py` is NOT fragile** — the brief flagged it as a likely
  regex/string-parsing risk. It is in fact a robust balanced-brace JSON extractor with explicit
  string/escape handling and bool-subclass-of-int guards. Only L1 (verbosity) applies.
- **`config/_parsers.py` validators are correct** (bool-before-int guards on every int field,
  range checks on ratios). The issue is duplication/structure (H3/M1), not correctness.
- **`power.py`, `identity_names.py`, `pr_links.py`, `trust.py`, `errors.py`, `environment.py`,
  `utils.py`, `seed_input.py`, `archive.py`, `work_availability.py`** are all appropriately
  sized, single-purpose, and need no structural change.
- Broad-except usage across `github/adapter.py` and `beads/__init__.py` is consistently
  narrowed to `(OSError, TimeoutError)` / `(BdError, json.JSONDecodeError, ValueError)` with
  logging — not the "silent broad except" antipattern.
