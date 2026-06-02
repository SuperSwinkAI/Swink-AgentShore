## Bucket 07: sidecar/ + ipc/

Scope: all 15 files under `src/agentshore/sidecar/` and all 6 under `src/agentshore/ipc/` (5,334 LOC). The headline smell — `server.py` at 1,419 lines — is real, and the worst of it is **two complete, divergent copies of the stdio serve loop** of which production runs only one. Below, grouped by severity. Line numbers are against the `integration` branch as read.

---

## CRITICAL

### C1. Two complete stdio serve loops; the async/health/bridge one is dead in production
**Location:** `sidecar/server.py:1162-1310` (`_serve_async`) vs `sidecar/server.py:1331-1419` (`serve_async` + `stdio_pump` + `health_emitter` + `run_async`)

**Problem:** There are two independent implementations of "read line-framed JSON-RPC from stdin, dispatch, write responses to stdout":

- `_serve_async` (the one `run()` → `serve()` actually uses in production via `__main__`/PyInstaller entrypoint) — has the request-cancellation machinery (`$/cancelRequest`, `in_flight`, `cancelled_ids`, `_schedule_drain`, `run_request`), a loop exception handler, and a threaded reader.
- `serve_async`/`stdio_pump`/`health_emitter`/`run_async` — a *second* loop with a `write_lock`, an executor-based `stdin.readline` pump, and the `sidecar.health` heartbeat. It has **no** cancellation support, **no** in-flight tracking, different concurrency semantics.

I grepped every caller: `serve_async` and `run_async` are referenced **only by `tests/sidecar/test_server_async.py`** — never by any production entry point. Production is `sidecar_entrypoint.py`/`__main__.py` → `run()` → `serve()` → `_serve_async()`. So the heartbeat (`sidecar.health`, documented as the shell's stalled-sidecar detector per DESIGN §5.1), the `run_async(bridge=...)` single-process topology, and the write-lock all live in a code path the shipping sidecar never executes. Either the docstring's "single-process topology where the JSON-RPC server and the dashboard WebSocket bridge share one asyncio loop" is a lie, or the wrong loop is wired to the entrypoint.

This is the single biggest maintainability hazard in the bucket: bug fixes (the half-dozen `desktop-*` cancellation/double-write fixes) land in `_serve_async` only; the heartbeat/bridge-hosting logic lives only in the unused twin. Two loops drift; tests pin the dead one.

**Code-judo remedy:** Pick **one** loop. Keep the executor-pump + heartbeat shape from `serve_async` (it's simpler and has the documented health ping) and graft the cancellation block from `_serve_async` into it behind one `_TransportLoop` class with a single `_emit(text)` method holding the `write_lock`. Delete the loser plus its tests. Net: removes ~150 lines of server.py and an entire parallel test surface, and makes the heartbeat/bridge-hosting actually reachable. If `run_async`/bridge-hosting is genuinely the intended prod path, fix `run()`/`sidecar_entrypoint.py` to call it and delete `serve`/`_serve_async` instead — but you cannot keep both.

---

## HIGH

### H1. `handle_request` is a 90-line if/elif method-prefix ladder instead of a dispatch table
**Location:** `sidecar/server.py:1021-1114`, feeding 8 `_dispatch_*` functions (`_dispatch_session`, `_dispatch_project_rpc`, `_dispatch_archive`, `_dispatch_recents_rpc`, `_dispatch_config_rpc`, `_dispatch_identities_rpc`, `_dispatch_agents_rpc`, `_dispatch_custom_method`)

**Problem:** Routing is a hand-maintained chain: explicit-method checks (`app.handshake`), set-membership checks (`{"session.start", ...}`, `_PROJECT_METHODS`, `{"archive.*"}`, ...), and prefix checks (`method.startswith("identities.")`, `"agents."`). Each branch re-passes the identical 5-tuple of kwargs (`payload.get("params")`, `req_id=`, `is_notification=`, `notify=`, `state=`). Adding a method means editing both the ladder and the target `_dispatch_*`. The `is_notification: return None` guard is re-implemented at the top of nearly every `_dispatch_*` (and inconsistently — see H4). Capabilities are *also* maintained by hand in `handshake.capabilities()` (`sidecar/handshake.py:32-69`), a third independent list of the same method names that silently drifts (note: `project.set_seed_paths` and `agents.detect`/`agents.catalog`/`agents.get_spawn_limits`/`agents.set_spawn_limits`/`identities.check_keychain` are dispatchable but **absent** from `capabilities()`).

**Code-judo remedy:** Build one `HANDLERS: dict[str, Handler]` registry where each entry is a small dataclass `Route(fn, needs_notify, needs_state)`. `handle_request` becomes: parse envelope → `route = HANDLERS.get(method)` → if none, `METHOD_NOT_FOUND` → if `is_notification and not route.notify_ok: return None` → `route.fn(...)`. Prefix families (`identities.*`, `agents.*`) keep their single fan-out function but register under one key each via a thin `RouteGroup`. Generate `capabilities()` from `HANDLERS.keys()` so the handshake list can never drift again. Removes the 90-line ladder, the per-dispatcher `is_notification` boilerplate, and the third method-name list (~120 lines net, plus closes a real correctness gap).

### H2. Manual JSON-RPC envelope assembly + line framing duplicated across 6+ sites
**Location:** `sidecar/server.py:152-157` (`_error`/`_result`), the 7 `build_*_notification` helpers (`server.py:428-499`), and the inline `stdout.write(json.dumps(...) + "\n")` framing at `server.py:1208, 1221, 1226-1228, 1234-1235, 1250, 1262-1264, 1304`; plus `session_lifecycle._progress` (`session_lifecycle.py:124-160`) and `server._progress_notification` (`server.py:313-329`) building **the same** `$/progress` notification two different ways.

**Problem:** The JSON-RPC framing rule ("serialize, append exactly one `\n`, flush") is copy-pasted at every write site in both serve loops. The notification builders are hand-rolled dict literals (`{"jsonrpc": "2.0", "method": ..., "params": ...}`) repeated 7× in server.py + 2× in emitters + 2× for `$/progress`. There are even two `$/progress` builders with subtly different signatures (`server._progress_notification` takes `percent`; `session_lifecycle._progress` derives percent from a `status` string) — the start/stop progress in server.py and the phase progress in session_lifecycle are the same wire message authored twice.

**Code-judo remedy:** One `frame(obj: Mapping) -> str` (json.dumps + "\n", `allow_nan=False`) used by every write site, and one `notification(method, params) -> JsonRpcNotification` factory replacing the 7 `build_*_notification` functions and both `_progress` builders. Collapse the two `$/progress` authors into `session_lifecycle._progress` and have server.py call it. Removes the inline framing from ~7 call sites and ~9 dict-literal builders (~80 lines), and guarantees `allow_nan=False`/`_json_safe` discipline is applied uniformly (today the sidecar stdout writes use bare `json.dumps`, while only `serializer.make_message` enforces `allow_nan=False` — an inconsistency, see H3).

### H3. Two unrelated wire framings ("ipc NDJSON envelope" vs "sidecar JSON-RPC") with no shared safety contract
**Location:** `ipc/serializer.py:366-383` (`make_message`, `_json_safe`, `allow_nan=False`) vs all sidecar stdout writes (`json.dumps` with default `allow_nan=True`, no `_json_safe`)

**Problem:** The brief asks whether framing/serialization is duplicated between sidecar and ipc. It is *worse* than duplicated — it is **inconsistently** implemented. The ipc path correctly routes every payload through `_json_safe` (NaN/Inf → null) and `allow_nan=False` so the browser JSON parser never chokes. The sidecar JSON-RPC path does neither: any `float('inf')`/`nan` that reaches a sidecar response (e.g. via `archive.list`'s `final_alignment`, or `project.set_budget` echoes) is emitted as bare `Infinity`/`NaN`, which is invalid JSON and trips the exact `JSONDecodeError: Extra data` failure mode `_configure_sidecar_logging` was written to prevent. The two paths share `import json` and nothing else.

**Code-judo remedy:** Promote `_json_safe` + the `allow_nan=False` discipline into a single `agentshore/ipc/wire.py` (or a `transport` module) used by both `make_message` and the sidecar `frame()` from H2. One sanitizer, one dumps policy, both transports. ~0 net new lines (move + 2 call-site edits) but closes a latent protocol-corruption bug and removes the divergence.

### H4. Inconsistent notification handling: some `_dispatch_*` silently swallow notifications, some error, some act
**Location:** `_dispatch_recents_rpc` (`server.py:810-833`) executes side effects (`touch_recent`/`remove_recent`) **before** the `is_notification` check at line 831; every other `_dispatch_*` returns `None` immediately on `is_notification`. `_dispatch_session` (`server.py:701-713`) emits `$/progress` side effects for a `session.stop` notification but then returns None.

**Problem:** Notification semantics are decided per-handler with no single rule. `recents.touch` as a notification mutates disk and returns nothing; `config.write` as a notification is a silent no-op (line 844 short-circuits before the write); `identities.add` as a notification is a no-op. This is exactly the "silent fallback" class the review targets — a client that omits `id` gets wildly different behavior per method, and nobody can tell which methods honor notifications without reading each handler.

**Code-judo remedy:** Decide notification support **once**, in the H1 registry (`notify_ok: bool` per route, default False → `INVALID_REQUEST` for methods that require a response, since these are all request/response RPCs). Drop every in-handler `if is_notification: return None`. The behavior becomes declarative and uniform, and the recents double-mutate-then-discard path disappears.

---

## MEDIUM

### M1. `session.stop` orchestrator teardown is a 95-line tangle of nested try/except/suppress in the request path
**Location:** `sidecar/server.py:502-596` (`_build_session_stop_response`)

**Problem:** One function does: param extraction (2 manual `isinstance` walks), drain-vs-hard branching, `asyncio.wait_for(shield(...))` with TimeoutError→cancel fallback, ESR payload build, deferred `orch.stop()`, six pieces of `state` reset, bridge teardown, and pid/info cleanup with a function-body-local `from pathlib import Path as _Path` import (line 590). It is also near-duplicated by the natural-exit path in `session_lifecycle._supervise` (`session_lifecycle.py:571-630`), which independently re-implements "build ESR before stop(), fall back to minimal payload, then stop()". Two copies of the delicate "build-payload-before-store-close" ordering invariant.

**Code-judo remedy:** Extract `drain_or_cancel(orch, orch_task, mode, timeout) -> None`, `reset_session_state(state) -> None`, and a single `finalize_esr(context, orch, exit_reason, exit_code) -> payload` shared by both `_build_session_stop_response` and `_supervise`. Hoist the `pathlib`/`session_path` imports to module top (they're not torch-tainted). Removes ~60 lines and collapses the two copies of the store-close ordering invariant into one.

### M2. UDS-vs-TCP branching is scattered, not behind one transport abstraction
**Location:** `ipc/server.py:78-117` (`start()` branches on `self._endpoint.kind == "unix"` four separate times — stale-symlink, stale-socket, bind, chmod — interleaved with the TCP `start_server` branch), and `ipc/server.py:126-131` (`stop()` re-branches on `kind == "unix"`). `session_lifecycle._make_bridge` (`session_lifecycle.py:667-688`) re-derives host/port from a raw dict and rejects non-tcp; `run_session_start` (`session_lifecycle.py:408-422`) re-parses the same `ipc_endpoint` dict by hand a third time.

**Problem:** "Is this unix or tcp?" is answered ad hoc at ≥6 sites with hand-rolled dict probing (`state.ipc_endpoint.get("kind")`, `.get("host")`, `.get("port")` with per-site isinstance guards). The `IpcEndpoint` dataclass exists but the dict round-trip (`to_json()` / manual reconstruction) defeats it — the typed object is flattened to `dict[str, object]` on `ServerState.ipc_endpoint` and re-parsed everywhere.

**Code-judo remedy:** Keep `IpcEndpoint` typed end-to-end on `ServerState.ipc_endpoint` (don't store the dict). Add `IpcEndpoint.from_json(d)` so the three re-parse sites (`run_session_start`, `_make_bridge`, server) become one call. In `IpcServer`, split `_prepare_unix_path()` and `_bind()` so `start()` is a 3-line `prepare → bind → log`. Removes the interleaved branching and ~30 lines of repeated dict probing.

### M3. Inbound IPC command path: parse/validate/route inline in the connection loop, plus a special-cased `get_state`
**Location:** `ipc/server.py:158-219` (`_handle_client`)

**Problem:** The per-line loop mixes decode, `parse_command`+`validate_command`, the `get_state` reply-from-cache special case (lines 193-209), error-response framing (manual `json.dumps({"type":"error",...}) + "\n"` at 185 and 200, duplicated), and queue enqueue — all guarded by two near-identical `try: writer.write/drain except (ConnectionError, OSError): break` blocks. `get_state` is the only command answered inline; everything else is queued, so a reader can't tell which commands are synchronous without reading the loop.

**Code-judo remedy:** Extract `_write_line(writer, obj) -> bool` (returns False on disconnect) to kill the two duplicated write/drain/except blocks. Move the `get_state` cache-reply into a tiny `_INLINE_REPLIES: dict[str, Callable]` so the inline-vs-queued distinction is one table, not buried control flow. ~25 lines and the framing duplication (which also overlaps the sidecar's framing per H2 — same `obj + "\n"` rule, third copy).

### M4. `_token_for_identity` is a 40-line if/elif over token-source kinds, repeated against `add`/`update`
**Location:** `sidecar/identities.py:125-164` (`_token_for_identity`), with the source-field set logic re-spelled in `add_identity:245-251` and `update_identity:288-293`

**Problem:** Three token-source kinds (`gh_token_login`, `gh_token_env`, `gh_token_keychain`) drive three separate dispatch sites: resolution (`_token_for_identity`), creation (`add_identity`), and mutation (`update_identity`), each with its own if/elif chain over the same three strings. Two near-identical `import keyring; except KeyringError; except Exception` blocks (lines 152-163 and inside `add_identity` 253-261) — and `_keychain_has_token` is a *third* copy of the keyring-get-with-double-except (lines 90-103). The `except KeyringError / except Exception` double-catch returning the same value is a broad-except smell in all three.

**Code-judo remedy:** A `TokenSource` enum + small per-source resolver dict (`{login: ..., env: ..., keychain: ...}`) collapses the resolution if/elif and lets `add`/`update` share one `apply_source(entry, source, canonical)` helper. One `_keyring_get(service) -> str | None` swallows the keyring import + double-except in a single place used by both `_keychain_has_token` and `_token_for_identity`. ~40 lines and three copies → one.

### M5. ruamel.yaml round-trip writer copy-pasted four times in project.py
**Location:** `sidecar/project.py:344-364` (`_write_target_branch`), `393-413` (`_write_seed_paths`), `463-485` (`_write_budget`) — plus `_atomic_write_text` (329-341); and a *separate* atomic-yaml writer (`_write_yaml_atomic` using `yaml.safe_dump`, no comment preservation) duplicated verbatim in `agents.py:100-114` and `identities.py:73-87`.

**Problem:** Three writers in project.py share the identical 8-line preamble (`YAML(); preserve_quotes=True; load-or-{}; get-section-or-create; set; dump to StringIO`). Two *other* modules (agents.py, identities.py) carry a byte-identical `_write_yaml_atomic` that uses `yaml.safe_dump` — so the codebase has two competing "atomically write agentshore.yaml" implementations (one comment-preserving via ruamel, one comment-destroying via PyYAML) and which one a given RPC uses is incidental. Editing `agents.configure` silently strips the user's YAML comments; editing `project.set_target_branch` preserves them.

**Code-judo remedy:** One `update_yaml_section(yaml_path, section_key, mutate: Callable[[dict], None])` helper in a shared `sidecar/yaml_io.py` built on ruamel (comment-preserving) + `_atomic_write_text`. The three project.py writers become 3-line `mutate` lambdas; agents.py and identities.py drop their duplicate `_write_yaml_atomic` and route through the same helper, gaining comment preservation for free. Removes ~70 lines and resolves the comment-stripping inconsistency.

### M6. `notification_emitters.py` is documented as entirely unwired dead code
**Location:** `sidecar/notification_emitters.py:16-21` ("Currently the connectors are not invoked anywhere") — though `build_session_completed_emitter` and `build_esr_ready_emitter` *are* now used by `session_lifecycle._start_orchestrator`. `build_agent_subprocess_callbacks` (`notification_emitters.py:41-74`) and the two underlying `build_agent_subprocess_*_notification` builders (`server.py:477-499`) have no non-test caller.

**Problem:** The module docstring is stale (two of three emitters are wired), and the third (`build_agent_subprocess_callbacks`) plus its two server-side notification builders are genuinely dead — the orchestrator-inside-sidecar `AgentManager` wiring that would call them "is still deferred." Dead adapters + stale docs.

**Code-judo remedy:** Delete `build_agent_subprocess_callbacks` and the two `build_agent_subprocess_*_notification` functions until the AgentManager wiring lands (recover from git when needed), and rewrite the module docstring to reflect that session_completed/esr_ready are live. ~60 lines. If the subprocess events are imminent, at minimum fix the docstring so it stops claiming nothing is invoked.

---

## LOW

### L1. `serialize_state` is a 50-key hand-built dict; one drift away from the dashboard contract
**Location:** `ipc/serializer.py:262-310`

**Problem:** Every `OrchestratorState` field is manually copied into the wire dict with per-field None-guards and `.value` enum unwrapping. Plus `work_availability` is written to **two** keys (`"work_availability"` and `"issue_availability"`, lines 278-279) — a same-value alias that smells like an un-removed legacy field name. No structural fix demanded, but the dual-key alias should be deleted once the dashboard reads the canonical name (per the project's zero-legacy-refs rule).

**Code-judo remedy:** Drop the `"issue_availability"` alias after confirming the dashboard reads `"work_availability"`. Longer term, drive `serialize_state` off `dataclasses.asdict` + a small enum-unwrap pass rather than 50 hand-written keys.

### L2. Broad `except Exception` defensive guards with `# pragma: no cover`
**Location:** `server.py:805-806` (`_dispatch_project_rpc`), `1006-1007` and `1013-1014` (`_dispatch_custom_method`), `1283` (`_serve_async` last-resort), `session_lifecycle.py:251-253, 364, 576-577, 606, 624` (orchestrator boot/supervise)

**Problem:** Several `except Exception` → INTERNAL_ERROR backstops. The serve-loop one (`server.py:1283`) is defensible (keeps the loop alive). The per-dispatcher ones (`_dispatch_project_rpc:805`, `_dispatch_custom_method:1006`) are redundant *with* the serve-loop guard — they catch the same class one frame deeper and turn a structured `project_rpc.ProjectError` path into a generic `type(exc).__name__` string. `session_lifecycle`'s `except Exception` around `bd_init_project` (251) and `orch.run_until_idle` (576) discard the exception type.

**Code-judo remedy:** Keep the single serve-loop backstop (C1's unified loop); delete the redundant per-dispatcher `except Exception` guards now covered by it. Where the engine boot wraps `orch.*`, catch and re-raise as a typed `SessionStartError` with the original `from exc` (already done in most places) and drop the bare-Exception duplicates.

### L3. `os.chdir` side effect inside `project.select`
**Location:** `server.py:247-248` (`_finalize_project_select`)

**Problem:** `project.select` mutates **process-global** cwd (`os.chdir(resolved)`) to work around macOS TCC prompts. Documented, but it means an RPC that reads as pure ("select a project") silently repoints every subsequent relative-path operation and any other concurrent handler. In the (currently single-threaded) loop this is survivable, but it's a hidden global mutation that any decomposition must preserve carefully.

**Code-judo remedy:** No deletion, but the cwd anchor belongs in `session.start` (where subprocesses actually spawn) or passed explicitly as `cwd=` to subprocess spawns, not as a side effect of project selection. Track as a follow-up; flag so the H1 registry refactor doesn't accidentally lose it.

### L4. `asyncio.get_event_loop()` in a done-callback
**Location:** `session_lifecycle.py:659` (`_on_orchestrator_done` → `asyncio.get_event_loop().create_task(...)`)

**Problem:** `get_event_loop()` is deprecated for this use and can raise/return the wrong loop outside a running loop in 3.12. Done-callbacks run on the loop, so `asyncio.get_running_loop()` is correct and won't warn.

**Code-judo remedy:** Replace with `asyncio.get_running_loop().create_task(...)`. One-line.

---

## Summary of structural wins available
- **C1** alone deletes one of two ~150-line serve loops and a whole parallel test file, and fixes the heartbeat/bridge-hosting being unreachable.
- **H1+H2+H3+H4** convert the routing ladder + scattered framing + divergent NaN policy + ad-hoc notification rules into one registry + one `frame()`/`notification()` + one `wire.py` sanitizer (~300 lines removed, plus closes the capabilities-drift and Infinity-in-JSON bugs).
- **M5** removes two competing agentshore.yaml writers (comment-preserving vs comment-stripping) — a correctness inconsistency, not just dedupe.
- Target: `server.py` should land well under 700 lines once C1+H1+H2+M1 are applied.
