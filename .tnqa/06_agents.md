## Bucket 06: agents/ (cli_agent, identity, manager, api)

Scope: all 19 `.py` files under `src/agentshore/agents/` (including the `worktree/`
subpackage). Reviewed against the brief's special foci: per-agent-type branching,
subprocess-lifecycle + env-overlay + token security, and the claimed
"httpx vs subprocess" shared base.

Headline structural facts that frame the findings:

- **There is no API/httpx agent adapter in this layer at all.** `manager.dispatch`
  unconditionally calls `dispatch_cli`. `AgentAPIError` is caught defensively in
  the `except` ladder (`manager.py:359`) but is never raised anywhere in
  `src/agentshore/agents/`. No `dispatch_api`, no `httpx.AsyncClient` for agent
  dispatch exists in the repo (the only httpx use is `model_catalog.py`'s model
  listing). So the brief's "(c) httpx vs subprocess paths sharing a leaky common
  base" risk does not exist — the opposite problem does: the docstrings/CLAUDE.md
  describe a second path that isn't here.
- **Per-agent-type branching is largely already table-driven** (`build_argv` is the
  one real `if claude / elif codex / elif gemini` argv builder; capabilities,
  model tiers, and YOLO flags are all dict-keyed). The remaining branching is the
  per-agent JSONL parsers, which are legitimately different wire formats.

---

### CRITICAL

#### C1. `_kill_process` uses `"pgid" not in locals()` as control flow — fragile process-kill guard
**Location:** `cli_agent.py:1209-1215`
**Problem:** The function does `with contextlib.suppress(ProcessLookupError): pgid = os.getpgid(proc.pid)` then branches on `if "pgid" not in locals():`. Using `locals()` membership as the success/failure signal of a suppressed call is a code smell that is genuinely dangerous here: this is the orchestrator's *only* hard-kill path for runaway agent subprocesses, and the brief and MEMORY both flag orphaned subprocesses as having "cost hundreds of dollars." If a future edit adds an earlier `pgid` binding, the guard silently inverts. It also can't distinguish "pid is None" from "process already gone." `proc.pid` can be `None` (the `os.getpgid(None)` would raise `TypeError`, which is *not* suppressed).
**Code-judo remedy:** Replace with an explicit value:
```python
try:
    pgid = os.getpgid(proc.pid)
except (ProcessLookupError, TypeError):
    _close_process_transport(proc)
    return
```
Removes the `locals()` introspection entirely (~3 lines), makes the None-pid case safe, and makes the kill path auditable. ~6 lines net simpler, one whole failure mode closed.

#### C2. Per-dispatch token re-resolution + live `gh repo view` preflight on the hot path
**Location:** `manager.py:285-296` (also duplicated at `manager.py:198-203` in `instantiate`, and again in `plays/executor.py:1279`)
**Problem:** Every single `dispatch()` re-runs `resolve_identity_env(..., strict=True)` (which on a cache miss shells out to `gh auth token` and `gh api user`) **and** `verify_identity_repo_access` (which shells out to `gh repo view`) via `asyncio.to_thread`, on the critical path before each agent invocation. It is "cached" by `IdentityResolver`'s four in-memory dicts/sets — but that cache is keyed on token/login and never invalidated, so it papers over the fact that the same security preflight is conceptually re-executed per dispatch. This is security-sensitive sequential orchestration that belongs at agent instantiation, not per-play: `instantiate()` already does the identical preflight (`manager.py:196-214`). The result is two `gh` subprocess round-trips serialized in front of every dispatch on a cold cache, and the validation result is recomputed rather than carried on the `AgentHandle`.
**Code-judo remedy:** Resolve the identity overlay **once** in `instantiate()`, store the validated `dict[str, str]` overlay (sans repo-access recheck) on the `AgentHandle` (new field `identity_env: dict[str,str]`), and have `dispatch()` read it. Drop the per-dispatch `verify_identity_repo_access` call entirely (it is a startup/instantiate concern; `report_identity_repo_access` already covers boot). Removes ~15 lines from `dispatch`, eliminates one `gh` subprocess per dispatch, and makes the token-resolution invariant "resolved exactly once per agent" instead of "resolved per play and hopefully cached."

---

### HIGH

#### H1. Dead "API agent" abstraction scattered across docstrings, `__init__`, and the exception ladder
**Location:** `__init__.py:1` ("API adapters"), `manager.py:359` (`AgentAPIError` branch), repo-wide docstrings ("API agents use httpx")
**Problem:** The layer advertises a two-transport design (CLI subprocess + httpx API) that does not exist in code. `AgentManager` has exactly one dispatch path. The `AgentAPIError`/`AgentRateLimitError`/`AgentAPIError` arms of the `except (OrchestratorError, OSError, RuntimeError)` ladder (`manager.py:355-360`) classify errors that `dispatch_cli` cannot produce. This is speculative generality: it makes readers hunt for an API agent class that was never built.
**Code-judo remedy:** Either (a) delete the API-only error arms and fix the docstrings to say "CLI subprocess agents only," removing ~4 lines and one import; or (b) if API agents are genuinely planned, leave a single one-line `# reserved for future api_* agents` marker and delete the rest. Given the repo's stated "no backward-compat / no speculative code" policy (MEMORY: `feedback_no_legacy_refs`), prefer (a).

#### H2. Two parallel families of "backward-compatible free functions" exist only for tests
**Location:** `cli_agent.py:1148-1187` (10 thin wrappers delegating to `CliOutputParser`), `identity.py:502-541` + `738-747` (6 wrappers delegating to `_default_resolver`)
**Problem:** Both modules refactored logic into a class (`CliOutputParser`, `IdentityResolver`) but kept a full shadow layer of module-level free functions whose only callers are the test suite (confirmed: `tests/test_cli_agent.py`, `tests/test_identity_resolver.py`, `tests/test_cli_identity.py` import/patch `_extract_text_from_codex_jsonl`, `_validate_github_token`, `_read_keychain_token`, etc.; no `src/` caller outside the defining module). That is ~16 wrapper functions (~70 lines) existing solely so tests can patch a module global. The header comments literally say "Backward-compatible free functions" — but there is no external consumer to be compatible with.
**Code-judo remedy:** Point the tests at the class (`CliOutputParser.parse_codex`, `resolver.validate_github_token` on an injected `IdentityResolver`) and delete both wrapper blocks. Removes ~70 lines across two files and collapses each module to a single public surface. The `IdentityResolver` already exists specifically to make "test isolation cleaner" (its own docstring) — the free-function shims undercut that goal.

#### H3. `CliOutputParser` is a namespace-only class wrapping stateless statics — and the per-format parsers are copy-paste siblings
**Location:** `cli_agent.py:817-1119`
**Problem:** Two issues compound. (1) `CliOutputParser` is a class with only `@staticmethod`s and no state ("All methods are static since parsers are stateless" — its own docstring). That is a module masquerading as a class. (2) `parse_codex` (914), `parse_gemini` (969), and `parse_claude` (1018) each re-implement the same `for line in map(str.strip, raw.splitlines()): if not line: continue; try: json.loads ... except JSONDecodeError: continue` JSONL scan loop — the brief's "copy-pasted parsing" smell. The dispatch into them is itself a per-agent-type `if/elif` chain in `_read_output` (`cli_agent.py:716-724`).
**Code-judo remedy:** (a) Demote `CliOutputParser` to module-level functions or, better, define a tiny `CliOutputFormat` protocol with one `parse(raw) -> (text, usage, session_id)` method and a `dict[AgentType, CliOutputFormat]` registry, so `_read_output` becomes `text, usage, sid = _PARSERS[agent_type].parse(raw)` — deletes the `if/elif` dispatch (716-724) and the corresponding branch in the read loop (688-695). (b) Extract the shared "iterate non-blank JSON lines" loop into one `_iter_json_events(raw)` generator the three parsers consume. Net: removes the class shell, the dispatch branch, and ~3 copies of the scan loop (~30-40 lines), and makes adding a 4th agent type a registry entry instead of editing four sites.

#### H4. Token-validation result is silently downgraded when `strict=False`
**Location:** `identity.py:267-275` (`validate_resolution`) consumed by `resolve_env` (`identity.py:421`) called with `validate=strict`
**Problem:** When `strict=False`, `validate_resolution` returns `token_valid=True` **without ever calling GitHub** (`if not validate: return _TokenResolution(..., token_valid=True, resolved_login=expected_login ...)`). The token is injected into the subprocess env regardless. So a non-strict dispatch path will happily inject an unvalidated (possibly wrong-account) `GH_TOKEN`/`GITHUB_TOKEN` and report it as valid. Today `manager.dispatch` and `executor` always pass `strict=True`, so this is latent — but it's a security-shaped silent fallback: the field name `token_valid` asserts an invariant the code did not check. The brief explicitly asks to flag "Optional/Any/casts papering over invariants" and "silent fallbacks"; this is the token-handling instance.
**Code-judo remedy:** Rename `token_valid` to `token_validated` and have non-strict resolutions return `token_validated=False` (they were not validated). Callers that inject the token (`resolve_env`) already gate the *hard failure* on `strict`; the only behavioral change is the field stops lying. Alternatively, drop the `strict=False` env-resolution path entirely if no production caller uses it (grep shows every `src/` caller passes `strict=True`) — that would delete the whole `if not validate:` branch (~8 lines) and the `validate` parameter threading through `resolve_token_details` / `validate_resolution`.

#### H5. `manager.instantiate` performs a non-atomic two-phase agent registration
**Location:** `manager.py:184-214`
**Problem:** The handle is registered into `self._handles` and the DB (`register_agent`) **before** the identity/repo-access preflight runs; on preflight failure the agent is left in `self._handles` in `AgentStatus.ERROR` (184-214), not removed. So a misconfigured-identity agent becomes a live, selectable-looking handle that `_selection.py` will skip only because it isn't IDLE — but it still consumes a circuit breaker, a DB row, and shows up in snapshots. The lifecycle state (`ERROR` set via `transition_to` at 206) and the store record (created at 184, never marked terminated) diverge. This is the brief's "non-atomic lifecycle state updates."
**Code-judo remedy:** Run the identity preflight **before** mutating `self._handles`/`self._circuit_breakers`/DB (it already has everything it needs by line 196), and either raise or never-register on failure. Reorders existing code, removes the half-constructed-agent state; no net new lines, one invariant restored ("a handle in `_handles` has passed its preflight").

---

### MEDIUM

#### M1. `cli_agent.dispatch_cli` is a 320-line function carrying five concerns
**Location:** `cli_agent.py:313-636`
**Problem:** One function does: argv build + resume-path rebuild, logging/clamping, subprocess spawn + env overlay, the read-task/idle-task race with four distinct completion branches (453-532), kill/cleanup across three `except` arms + `finally`, and post-exit error classification + cost. The four-way `asyncio.wait` result handling (read done / timeout / idle-exc post-response / idle-exc generic) is the densest spot and is where the duplicated 8-tuple unpack appears twice (480-489 and 523-532).
**Code-judo remedy:** Extract three helpers: `_build_dispatch_argv(...)` (argv + resume rebuild, ~30 lines out), `_await_output_or_timeout(read_task, idle_task, ...) -> _ReadOutputResult` (the wait/race block + tuple unpack, deduping the two unpack sites), and `_finalize_nonzero_exit(...)` (567-599). Leaves `dispatch_cli` as a readable spawn→await→finalize skeleton (~120 lines). No behavior change; removes one duplicated 8-field unpack.

#### M2. 8-field positional tuple (`_ReadOutputResult`) threaded through three functions
**Location:** `cli_agent.py:29`, returned by `_read_output` (727-736), unpacked twice in `dispatch_cli` (480-489, 523-532)
**Problem:** `type _ReadOutputResult = tuple[str, int, int, int, int, int, int, str | None]` — a positional 8-tuple where positions 1-7 are all `int` token counts. Adding/removing a usage field means editing the alias, the return, and two unpack sites in lockstep; a transposition of two adjacent ints is undetectable by mypy.
**Code-judo remedy:** Return a frozen `@dataclass _ReadOutput(raw: str, usage: _UsageTotals, session_id: str | None)` — `_UsageTotals` already exists and already holds all six int fields. Collapses the 8-tuple to 3 fields, deletes the type alias, and makes the two unpack sites `res.raw, res.usage, res.session_id`. Removes the entire class of positional-misalignment bugs.

#### M3. Dead export `all_known_worktree_paths`
**Location:** `worktree/registry.py:251-265`
**Problem:** Defined and documented as "Used by `reap_git_orphans`," but `reap_git_orphans` actually calls `live_worktree_paths` (reaper.py:210), and `all_known_worktree_paths` has zero callers in `src/` or `tests/`. It is not even in registry's `__all__`. Pure dead code with a misleading docstring.
**Code-judo remedy:** Delete the function (~15 lines).

#### M4. `read_keychain_token` default `warn_missing=True` is never used true; warning is dead
**Location:** `identity.py:176-204`, only caller `resolve_token_details` passes `warn_missing=False` (355)
**Problem:** The sole production caller always passes `warn_missing=False`, so the `if not warn_missing: return None` / else-warn branch (197-201) only the `False` arm ever executes in `src/`. The `True` default and its `identity_keychain_token_empty` warning are reachable only from tests.
**Code-judo remedy:** Drop the `warn_missing` parameter and the warn branch; return `None` on empty. Removes ~5 lines and one never-fired log event. (Low-risk; verify no test asserts the warning first.)

#### M5. Identity overlay mutates `GH_CONFIG_DIR` with an empty-string fallback
**Location:** `identity.py:436-439`
**Problem:** `overlay["GH_CONFIG_DIR"] = _expanded_gh_config_dir(ident.gh_config_dir) or ""` — if `gh_config_dir` is set but expands to falsy, the subprocess gets `GH_CONFIG_DIR=""`, which `gh` interprets as "use cwd/empty config," a silent auth-context change rather than a clear error. This is the env-mutation-with-silent-fallback the brief asks to flag.
**Code-judo remedy:** Only set the key when the expansion is truthy; otherwise fall through to the `_isolated_gh_config_dir(name)` branch (the existing `else`). One-line guard; removes the empty-string footgun.

#### M6. `worktree/manager.py` allocate-pr-scoped and allocate-branch-creating are near-identical (~90 lines duplicated)
**Location:** `worktree/manager.py:457-545` vs `555-639`
**Problem:** `_allocate_pr_scoped_locked` and `_allocate_branch_creating_locked` differ only in: lookup function (`lookup_by_branch` vs `lookup_by_prebranch_key`), the key kind (`branch_name` vs `pre_branch_key`), and `base_ref` (`origin/{branch}` vs `origin/HEAD`). The reuse-existing → ensure → touch → return, and the insert → conflict-relookup → best-effort-remove → return skeletons are copy-pasted verbatim, including the two near-identical `WorktreeAllocation(...)` constructions per method.
**Code-judo remedy:** Parameterize a single `_allocate_locked(*, lookup, key_field, key_value, base_ref, scope, play_type)` that builds the `WorktreeAllocation` once. The two public wrappers (`_allocate_pr_scoped`, `_allocate_branch_creating`) keep their distinct lock-key derivation and call the shared body. Removes ~70 lines and the risk of the two paths drifting (they already differ subtly in detached-vs-branch handling that's easy to get wrong).

---

### LOW

#### L1. `dispatch_cli` keeps a `--resume` path that the module docstring says is banned
**Location:** `cli_agent.py:376-386`, 533-535
**Problem:** `build_argv`'s docstring and the module header say `--resume` is intentionally unsupported (state-rot), yet `dispatch_cli` rebuilds argv with `--resume` for a "narrow JSON-retry path (desktop-dy2j)" gated on `resume_session_id is not None and CLAUDE_CODE`. The resulting `_observed_session_id` is captured (535) and returned but, per the comment, general resume "is still banned." The feature is half-present and contradicts the stated invariant.
**Code-judo remedy:** If the JSON-retry path is live, document it as the *one* sanctioned resume use in the module header so the "banned" comment isn't self-contradictory; if it's vestigial, delete the `resume_session_id` rebuild (376-386) and the parameter. Reduces reader confusion / dead branch.

#### L2. `manager.dispatch` builds two inline closures per call
**Location:** `manager.py:297-309`
**Problem:** `on_spawned` / `on_exited` closures are redefined on every dispatch purely to adapt the callback signature (prepend `agent_id, agent_type`). Minor per-dispatch allocation + noise.
**Code-judo remedy:** Bind once with `functools.partial` or pre-wrap in `__init__` since `handle.agent_id`/`agent_type` are stable per agent. Cosmetic; ~10 lines → 2.

#### L3. `_safe_int` accepts `bytes`/`bytearray` that JSON usage dicts can't contain
**Location:** `cli_agent.py:1198-1206`
**Problem:** `isinstance(value, int | float | str | bytes | bytearray)` — usage values come from `json.loads`, which never yields `bytes`. The extra types widen the surface for no real input.
**Code-judo remedy:** Narrow to `int | float | str`. Trivial.

#### L4. `models_for_agent` sync wrapper silently returns stale `KNOWN_MODELS` inside an event loop
**Location:** `model_catalog.py:183-191`
**Problem:** When called from within a running loop it skips the live fetch and returns only the curated list, logging at `debug`. A caller expecting live models gets a silent partial answer. Acceptable as a fallback but worth an `info`-level signal or an explicit async-only contract.
**Code-judo remedy:** Either make the function async-only (callers `await models_for_agent_async`) or bump the log to `info`. Low priority.

---

### Notes on things that are GOOD (resisted the urge to flag)

- `capabilities.py`, `model_tiers.py`, `model_catalog.py`'s `KNOWN_MODELS`, and
  `_DEFAULT_YOLO_FLAGS` are already the capability/config tables the brief hoped
  for — per-agent-type data, not scattered conditionals. No action.
- `_selection.py` is a clean, well-documented pure rule chain; `_PLAY_ALLOWED_TIERS`
  is a table. Leave it.
- `context_writer.py`, `costs.py`, `circuit_breaker.py` are tight and single-purpose.
- The `worktree/` reaper/registry/rekey split is sound; the only structural debt
  there is M6 (duplicated allocate methods) and M3 (one dead query).
