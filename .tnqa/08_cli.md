## Bucket 08: cli/ + cli_identity/cli_helpers/command/budget/availability

THERMO-NUCLEAR CODE QUALITY REVIEW — CLI surface.

Scope reviewed (all read in full):
- `src/agentshore/cli/__init__.py`, `agent_select.py`, `caffeinate.py`, `constants.py`, `helpers.py`, `identity_helpers.py`, `runtime.py`, `seed.py`
- `src/agentshore/cli/commands/`: `__init__.py`, `archive.py`, `configure.py`, `dashboard.py`, `identity.py`, `init.py`, `report.py`, `start.py`, `stop.py`, `train.py`, `trusted_ids.py`
- `src/agentshore/cli_identity.py`, `cli_helpers.py`, `command.py`, `budget.py`, `availability.py`

Overall: the package split is clean in intent, but it is held together by a **patch-compatibility tax** — every command body resolves helpers through the `agentshore.cli` package namespace (`_cli_pkg._foo`) purely so legacy `patch("agentshore.cli._foo", …)` tests keep working. That contract drives the 237-line re-export `__init__.py` and forces indirection through `_cli_pkg.` everywhere. The single biggest structural smell is `cli_identity.py` at 1202 lines doing five unrelated jobs in one module. Below, high-conviction structural findings only.

---

## Critical

### C1. `cli_identity.py` (1202 lines) is five modules wearing a trench coat
**Location:** `src/agentshore/cli_identity.py:1-1203`
**Problem:** A single module mixes: (a) gh-account detection + parsing (`GhAccount`, `parse_gh_auth_status`, `detect_gh_accounts`, the 5 module-level regexes, `looks_like_pat`), (b) keychain I/O (`_store_in_keychain`, `_migrate_keychain_token`, `_keychain_has_token`, `_keychain_backend_label`, `_managed_keychain_service`, plus the `KeychainManager` static-method shim), (c) the interactive wizard engine (`IdentityWizard`, `_prompt_choice`, `_prompt_token_strategy`, `_prompt_env_var_name`, `_prompt_new_login`, `_collect_identity_details`), (d) the YAML patcher (`_split_leading_comments`, `patch_yaml_with_bindings`, `normalize_trusted_ids_*`, `_identity_to_yaml_dict`, `_agent_bound_identity_logins`), and (e) the report renderer + orchestration entry point (`echo_identity_report`, `echo_repo_access_report`, `run_identity_wizard`, `_echo_post_wizard_report`). These layers share almost no state and change for different reasons.
**Code-judo remedy:** Split into a package `agentshore/identity_wizard/`:
- `gh_accounts.py` — detection + parsing + PAT/login regexes (~120 lines)
- `keychain.py` — all keyring ops; **delete `KeychainManager` entirely** (see C2) (~80 lines)
- `wizard.py` — `IdentityWizard` + prompt helpers (~330 lines)
- `yaml_patch.py` — patcher + trusted-ids normalization (~190 lines)
- `report.py` — `echo_identity_report` / `echo_repo_access_report` + the shared `_resolve_bad_identity_rows` helper from C3 (~120 lines)
- `__init__.py` re-exports `run_identity_wizard`, `run_wizard`, `detect_gh_accounts`, `IdentityBinding`, `WizardResult` for callers/tests.
No behavior change; ~1200-line file becomes five ~100-330 line files each with one reason to change. This is the prerequisite for C2/C3/H1.

### C2. `KeychainManager` is a pure passthrough shim with zero callers in scope
**Location:** `src/agentshore/cli_identity.py:266-289`
**Problem:** `KeychainManager` is a class of five `@staticmethod`s that each do `return _module_level_function(...)` with no added behavior, no state, no instances. Its own docstring admits the only reason it exists is "delegates to module-level functions so monkeypatching in tests works transparently" — i.e. it is test-shaped indirection over functions the tests could patch directly. Grep shows the wizard code calls the **module-level** functions (`_keychain_has_token`, `_store_in_keychain`, `_managed_keychain_service`), not the class. The class is dead weight on the public surface.
**Code-judo remedy:** Delete the class (24 lines). If any test references `KeychainManager.store`, retarget it at `keychain.store`. Net: -24 lines, one fewer "two ways to call the same thing" trap.

### C3. Triplicated "configured-identity-failed-validation" filter — business logic copy-pasted across 3 command sites
**Location:** `src/agentshore/cli/commands/start.py:372-378`, `src/agentshore/cli/commands/identity.py:72-78`, `src/agentshore/cli_identity.py:1160-1167`
**Problem:** The exact predicate
```python
r.identity_name is not None and r.token_source not in {"ambient", "none"} and not r.token_valid
```
is written out three times to compute "which identity rows are bad." This is a domain rule about identity health living inline in three Click/echo bodies. If the rule changes (e.g. a new neutral token_source), all three must change in lockstep — a classic drift bug. The `missing` refinement in `cli_identity.py:1167` is a fourth variant of the same logic.
**Code-judo remedy:** Add `bad_identity_rows(rows) -> list[IdentityStatus]` and `missing_token_rows(rows) -> list[IdentityStatus]` to the new `identity_wizard/report.py` (or `agents/identity.py` next to `report_identities`, which is the true canonical home). Replace all three call sites with `if bad_identity_rows(rows): …`. Removes ~18 duplicated lines and centralizes the health rule where `IdentityStatus` is defined.

---

## High

### H1. `start()` command body is a ~360-line orchestration script — business logic leaking into the CLI layer
**Location:** `src/agentshore/cli/commands/start.py:122-523`
**Problem:** The Click handler does far more than parse args and dispatch. It performs: budget resolution + validation (164-171), session-already-running check (204-210), socket/IPC endpoint resolution incl. symlink management with ELOOP guard (216-243), config generation + load + YAML-error formatting + override application via `dataclasses.replace` (287-336), a 24-line bootstrap-summary printer (339-362), the full identity-resolution banner with token validation + repo-access + SSH-key preflight + two-distinct-identity precondition (364-437), and finally mode dispatch (439-522). Almost none of this is CLI-specific; it is session-bootstrap policy that belongs in the service layer (`Orchestrator.bootstrap` / a `SessionBootstrap` builder). The same logic is partially re-implemented by the desktop sidecar (the file is littered with `desktop-*` parity comments), which is direct evidence the logic is in the wrong layer.
**Code-judo remedy:** Extract a `agentshore/session/bootstrap.py` (or extend `Orchestrator.bootstrap`) that takes the parsed options dataclass and returns a `ResolvedSession` (cfg, endpoints, repo_root, preflight result). Move the IPC/symlink block into `session_path.py` (it already owns `default_ipc_endpoint`/`session_socket_path`). Move the identity-preflight block (364-437) into a single `preflight_identities(cfg, repo_root) -> PreflightResult` in the identity layer — `identity.py` and the desktop wizard can then call the same function instead of duplicating it. `start()` shrinks to: parse → `bootstrap()` → print summary via a renderer → dispatch. Target: ~360-line body → ~80 lines; eliminates the start/desktop parity-comment maintenance burden.

### H2. Repo-access renderer exists twice, identity-report renderer split across two modules
**Location:** `src/agentshore/cli/helpers.py:69-84` (`_echo_repo_access_rows`) vs `src/agentshore/cli_identity.py:1057-1072` (`echo_repo_access_report`)
**Problem:** Two functions render `RepoAccessStatus` rows with the same `[repo: ok]` / `[repo: BLOCKED — …]` format and the same `max(len(agent_key))` width computation — one in `cli/helpers.py`, one in `cli_identity.py`. `start.py` and `identity.py` call the `helpers.py` one; the wizard calls the `cli_identity.py` one. Two implementations of identical output formatting guarantees the two banners drift. Same story for identity rows: `echo_identity_report` lives in `cli_identity.py` but the "bad rows" gating around it is re-derived per caller (see C3).
**Code-judo remedy:** Keep exactly one renderer for each row type in the new `identity_wizard/report.py`. Delete `_echo_repo_access_rows` from `cli/helpers.py` and repoint `start.py`/`identity.py` at the canonical `echo_repo_access_report`. Net: -16 lines and one renderer to reason about. (Per the prompt's "single renderer" focus: there should be one identity renderer module, full stop.)

### H3. Three near-identical ruamel round-trip writers in `init.py`
**Location:** `src/agentshore/cli/commands/init.py:84-157`
**Problem:** `_write_target_branch_to_yaml`, `_write_max_per_config_to_yaml`, and `_read_max_per_config_from_yaml` each re-implement the same ruamel boilerplate: `rt = YAML(); rt.preserve_quotes = True; existing = read_text() if exists else ""; data = rt.load(...) or {}; ...; rt.dump(data, buf); write_text(buf.getvalue())`. Only the nested key path (`project.target_branch`, `agent_spawn.max_per_config`) differs. The docstrings even note they mirror `sidecar.project._write_target_branch` — so the same boilerplate is also duplicated in the sidecar.
**Code-judo remedy:** Add a shared `ruamel_set_nested(config_path, ("project", "target_branch"), value)` / `ruamel_get_nested(path, keys)` pair in a small `config/yaml_io.py` and have both the CLI and the sidecar call it. Collapses three functions (~70 lines) to ~15 lines of call sites plus one ~25-line helper, and kills CLI↔sidecar drift.

### H4. DataStore lifecycle + "resolve last session" boilerplate copy-pasted across 5 read commands
**Location:** `src/agentshore/cli/commands/archive.py:34-53,74-106,130-141`, `report.py:39-65`, `stop.py:33-53`, `train.py:79-145`
**Problem:** Every DB-backed command repeats the same scaffold: `db_path` existence check + `Error: No database found at {db_path}` echo + `SystemExit(1)` (5 sites — confirmed by grep), then an inner `async def _run(): store = DataStore(db_path); await store.initialize(); try: … finally: await store.close()` wrapped in `asyncio.run(_run())` (7 `asyncio.run` sites). The "default to last session" logic (`sessions = await store.list_sessions(); if not sessions: error; sess_id = sessions[0].session_id`) is itself duplicated in `archive.py:81-88`, `report.py:48-54`, and `stop.py:42-48`.
**Code-judo remedy:**
1. A context manager `@asynccontextmanager async def open_store(db_path) -> DataStore` that does existence-check (raising `click.ClickException(f"No database found at {db_path}")`), `initialize`, and guaranteed `close`. Replaces the 5 hand-written existence checks + 6 try/finally blocks.
2. A `resolve_session_id(store, explicit) -> str` helper for the "last session" default (removes 3 copies).
Net: ~40 duplicated lines removed; the read commands become ~10 lines each. Also unifies the inconsistent error path (`stop.py` raises `RuntimeError`, the rest `click.echo + SystemExit`).

### H5. The `_cli_pkg._foo` indirection + 237-line re-export `__init__` is a test-shaped architecture
**Location:** `src/agentshore/cli/__init__.py:1-237`, and every `from agentshore import cli as _cli_pkg` consumer (`start.py`, `init.py`, `stop.py`, `identity.py`, `configure.py`, `runtime.py`, `identity_helpers.py`)
**Problem:** The package re-exports ~70 private names and a giant `__all__`, and command bodies call helpers as `_cli_pkg._resolve_policy_mode_override(...)`, `_cli_pkg._detect_agents()`, `_cli_pkg._logger`, etc., explicitly so `patch("agentshore.cli._foo")` keeps intercepting. Multiple module docstrings state this verbatim (`start.py:1-9`, `init.py:1-7`, `runtime.py:1-8`, `stop.py:1-7`, `identity.py:1-5`, `identity_helpers.py:1-6`). This inverts the dependency: production code is contorted to preserve a test patch target. It also creates real import-cycle pressure (the `__init__` comment at 131-137 explains subcommands avoid importing back into `__init__` to dodge a cycle) and makes the namespace a 70-name god-module.
**Code-judo remedy:** Migrate tests to patch each helper at its real home (`agentshore.cli.helpers._resolve_policy_mode_override`, `agentshore.cli_helpers._detect_agents`) and import helpers as normal module-level names in the command bodies. Then shrink `__init__.py` to just the `main` group + its `add_command` calls and a handful of genuine public exports. This is a test-refactor with no runtime behavior change, but it removes the `_cli_pkg.` prefix from ~7 modules and deletes ~200 lines of re-export plumbing. High value, but gated on a test-suite pass since it touches the patch contract.

---

## Medium

### M1. `init()` body inlines a five-phase wizard pipeline that should be a step list
**Location:** `src/agentshore/cli/commands/init.py:300-445`
**Problem:** The handler is a long linear script guarded by repeated `if not install_skills_only:` (appears 4×) interleaving: config gen/merge, skills install, target-branch prompt, max-per-config prompt, availability refresh, agent-select wizard, identity wizard, gitignore patching, beads init. Each phase re-reads `project_path / "agentshore.yaml"` from scratch (`config_path`/`target_yaml_path`/`_yaml_path`/`config_path` again — four separate locals for the same path). The repeated `if not install_skills_only` guard is the tell that two commands (`init` and `init --install-skills`) are crammed into one body.
**Code-judo remedy:** Compute `yaml_path` once. Extract the non-skills phases into named functions (`_run_post_config_wizards(project_path, yaml_path)`, already partly factored) and early-return for the `--install-skills` path: `if install_skills_only: install_skills(...); return`. Removes the 4 repeated guards and the 4 duplicate path locals; clarifies that `--install-skills` is a distinct, much smaller command.

### M2. Two incompatible `_str_or_none` helpers with the same name
**Location:** `src/agentshore/cli/helpers.py:94-104` (`_str_or_none(d, key)`) vs `src/agentshore/cli_identity.py:880-881` (`_str_or_none(value)`), plus a third inline `_value_str_or_none` in `identity_helpers.py:146-147`
**Problem:** Same name, two different signatures (dict+key vs single value), and a third anonymous twin inside `_existing_identities_from_yaml`. A reader grepping `_str_or_none` gets three different contracts. The dict-form is also doing silent coercion (`str(value)` fallback) that the value-form does not.
**Code-judo remedy:** Standardize on one `str_or_none(value: object) -> str | None` (the value form) in a shared `config/coerce.py`; let callers do `str_or_none(d.get(key))`. Delete the other two. -1 helper, one contract, removes the silent `str()` coercion divergence.

### M3. `_find_free_dashboard_port` reinvents free-port discovery that `session_path.find_free_tcp_port` already provides
**Location:** `src/agentshore/cli/runtime.py:384-395` vs `agentshore.session_path.find_free_tcp_port` (imported and used in `start.py:220`)
**Problem:** `runtime.py` hand-rolls a bind-loop over a hardcoded `[9400, 9410)` range while `session_path` already exposes `find_free_tcp_port(host)`. Two free-port finders with different semantics (range-scan vs OS-assigned). The hardcoded range silently returns a *busy* `start` port (9400) when all are taken (395), which can produce a confusing bind failure downstream.
**Code-judo remedy:** Either delete `_find_free_dashboard_port` in favor of `find_free_tcp_port`, or if the 9400-range affinity is intentional for the dashboard, move it next to `find_free_tcp_port` in `session_path.py` as `find_dashboard_port()` so both live in one place. Removes a duplicated networking primitive from the CLI layer.

### M4. `_dispatch_command` is a 100-line `elif` ladder mixing transport with orchestrator internals
**Location:** `src/agentshore/cli/runtime.py:38-138`
**Problem:** A flat `if/elif` over `command` string values, several branches reaching into orchestrator privates (`orch._refresh_issues`, `orch._in_flight`, `orch._store`, `orch._repo_root`, `orch._session_id`). The CLI's IPC dispatcher knowing about `orch._in_flight.values()` and constructing `ReportGenerator(orch._store)` is logic that belongs on the orchestrator, not in the CLI runtime. Adding a command means editing this ladder and reaching for more privates.
**Code-judo remedy:** Give `Orchestrator` public methods (`handle_ipc_command(cmd)` or discrete `refresh_issues()`, `generate_report(type)`, `archive_session()`, `abort_in_flight()`), and reduce `_dispatch_command` to a thin `await orch.handle_ipc_command(cmd)` or a small dict-dispatch `{command: handler}` table. Removes ~10 private-attribute reach-ins from the CLI and makes the command set testable on the orchestrator directly.

### M5. `availability._record_to_dict` duplicates the `TypedDict` field lists it already declares
**Location:** `src/agentshore/availability.py:124-169`
**Problem:** Three `TypedDict`s declare the serialized shape, then `_record_to_dict` re-lists every field by hand to build dicts of exactly those shapes, and `_record_from_dict` re-lists them a third time to parse. Three statements of the same field set; adding a field means editing all three.
**Code-judo remedy:** The `AvailabilityRecord` and its rows are already dataclasses (from `agentshore.config`). Use `dataclasses.asdict` for serialization (drop `_record_to_dict` + the three TypedDicts, ~45 lines) and keep only the defensive `_record_from_dict` for untrusted YAML. Net ~45 lines removed; one place defines the shape.

---

## Low

### L1. `_resolve_seed_input_path` is a 4-line thin wrapper
**Location:** `src/agentshore/cli/seed.py:15-25`
**Problem:** Entire module exists to wrap `resolve_seed_input` and translate `SeedInputError → click.BadParameter`. A whole file + re-export entry for one try/except.
**Code-judo remedy:** Inline the try/except at the single call site (`start.py:254`) or fold the helper into `cli/helpers.py`. Deletes a file and one `__init__` re-export. (Low because it's harmless; only worth doing during the `__init__` slimming of H5.)

### L2. `start.py` numbered-comment scaffolding (`-- 0.`, `-- 2.`, `-- 11a.`) is stale and misnumbered
**Location:** `src/agentshore/cli/commands/start.py:180,191,427` (no `-- 1.`; jumps 0→2; `11a` sub-step)
**Problem:** The hand-numbered phase comments (`-- 0.`, `-- 2.` with no `1.`, `-- 11a.`) are a symptom of the god-function in H1 and are already out of sync. They document structure that should be functions.
**Code-judo remedy:** Resolved for free by H1's extraction — each numbered phase becomes a named function call. Until then, not worth touching in isolation.

### L3. `train.py` imports `load_config` twice in the same try/except
**Location:** `src/agentshore/cli/commands/train.py:53-60`
**Problem:** `from agentshore.config import load_config` appears in both the `try` (53) and the `except` (58) blocks of the config-load fallback. The except re-imports the same symbol.
**Code-judo remedy:** Hoist the import above the `try`. Also share the identical `Warning: config load failed …` fallback with `start.py:320-321` (duplicated string) via a small `load_config_or_default(path) -> RuntimeConfig` helper. -1 redundant import, -1 duplicated warning string.

### L4. `_int_or_none` in `cli/helpers.py` is unused within scope and asymmetric with `_str_or_none`
**Location:** `src/agentshore/cli/helpers.py:107-121`
**Problem:** Exported in `__init__.__all__` but no in-scope caller; it raises on uncoercible types while its sibling `_str_or_none` silently coerces — inconsistent contracts for sibling helpers.
**Code-judo remedy:** Confirm no external caller (it's in `__all__`, so check tests) and delete, or move alongside the consolidated coercion helper from M2 with a matching contract. Low until M2/H5 land.
